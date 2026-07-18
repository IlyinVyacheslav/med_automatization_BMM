"""
Тесты для repository.py: функциональная логика, разграничение прав и
конкурентная (многопоточная) запись.

Запуск (нужен живой PostgreSQL со схемой clinic):
    pip install pytest
    pytest test_repository.py -v

Подключения задаются через окружение:
  Бот (проверяемая роль)   — DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD  (как у приложения)
  Админ (сидинг/очистка)   — DB_ADMIN_USER (по умолч. postgres) + DB_ADMIN_PASSWORD
Каждый тест работает в СВОЁМ изолированном наборе данных (уникальная
специальность/врач/слоты/пациент) и убирает его за собой — реальные данные не
затрагиваются.
"""

from __future__ import annotations

import os
import uuid
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest

from src.repository import ClinicRepository, RepositoryError


# ---------------------------------------------------------------------------
# Конфигурация подключений
# ---------------------------------------------------------------------------
def _bot_dsn() -> dict:
    return dict(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "clinic"),
        user=os.getenv("DB_USER", "ai_bot_registrar"),
        password=os.getenv("DB_PASSWORD", ""),
        sslmode=os.getenv("DB_SSLMODE", "disable")
    )


def _admin_dsn() -> dict:
    d = _bot_dsn()
    d["user"] = os.getenv("DB_ADMIN_USER", "postgres")
    d["password"] = os.getenv("DB_ADMIN_PASSWORD", os.getenv("DB_PASSWORD", ""))
    return d


FUTURE_DAY = dt.datetime(2030, 1, 7, 9, 0, 0)  # заведомо будущее, будни


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def admin_conn():
    try:
        conn = psycopg2.connect(**_admin_dsn())
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Нет админ-подключения к БД для сидинга тестов: {e}")
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture()
def repo():
    # try:
    r = ClinicRepository(minconn=1, maxconn=16)
    # except RepositoryError as e:
    #     print(e.message)
    #     pytest.skip(f"Нет подключения бота к БД: {e.message}")
    yield r
    r.close()


@pytest.fixture()
def bot_conn():
    """Сырое соединение под ролью бота — для негативных проверок прав."""
    try:
        conn = psycopg2.connect(**_bot_dsn())
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Нет подключения бота к БД: {e}")
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture()
def make_env(admin_conn):
    """
    Фабрика изолированного окружения: уникальная специальность + врач + k слотов
    + пациент. Возвращает dict с id/именами. Всё удаляется по завершении теста.
    """
    created: list[dict] = []

    def _make(k: int = 5):
        tag = uuid.uuid4().hex[:8]
        code = f"autotest_{tag}"
        name = f"Autotest {tag}"
        doc_last = f"Тестов{tag}"
        cur = admin_conn.cursor()
        cur.execute(
            "INSERT INTO clinic.specialties(code, name) VALUES(%s,%s) RETURNING id;",
            (code, name),
        )
        spec_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO clinic.doctors(last_name, first_name) VALUES(%s,'Тест') RETURNING id;",
            (doc_last,),
        )
        doc_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO clinic.doctor_specialties(doctor_id, specialty_id) VALUES(%s,%s);",
            (doc_id, spec_id),
        )
        slot_ids = []
        for i in range(k):
            start = FUTURE_DAY + dt.timedelta(minutes=30 * i)
            end = start + dt.timedelta(minutes=30)
            cur.execute(
                "INSERT INTO clinic.slots(doctor_id, starts_at, ends_at) VALUES(%s,%s,%s) RETURNING id;",
                (doc_id, start, end),
            )
            slot_ids.append(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO clinic.patients(last_name, first_name, birth_date) "
            "VALUES(%s,'Пациент','1990-01-01') RETURNING id;",
            (f"Пац{tag}",),
        )
        patient_id = cur.fetchone()[0]
        cur.close()
        env = dict(
            code=code, name=name, spec_id=spec_id, doc_id=doc_id,
            doc_last=doc_last, slot_ids=slot_ids, patient_id=patient_id,
        )
        created.append(env)
        return env

    yield _make

    # teardown в обратном порядке зависимостей
    cur = admin_conn.cursor()
    for env in created:
        cur.execute("DELETE FROM clinic.appointments WHERE slot_id = ANY(%s);", (env["slot_ids"],))
        cur.execute("DELETE FROM clinic.appointments WHERE patient_id = %s;", (env["patient_id"],))
        cur.execute("DELETE FROM clinic.slots WHERE doctor_id = %s;", (env["doc_id"],))
        cur.execute("DELETE FROM clinic.doctor_specialties WHERE doctor_id = %s;", (env["doc_id"],))
        cur.execute("DELETE FROM clinic.patients WHERE id = %s;", (env["patient_id"],))
        cur.execute("DELETE FROM clinic.doctors WHERE id = %s;", (env["doc_id"],))
        cur.execute("DELETE FROM clinic.specialties WHERE id = %s;", (env["spec_id"],))
    cur.close()


# ---------------------------------------------------------------------------
# 1. Функциональные проверки
# ---------------------------------------------------------------------------
def test_healthcheck(repo):
    info = repo.healthcheck()
    assert "session_user" in info and info["db"]


def test_list_specialties_contains_env(repo, make_env):
    env = make_env()
    names = {s["name"] for s in repo.list_specialties()}
    assert env["name"] in names


def test_search_doctors_by_specialty(repo, make_env):
    env = make_env()
    docs = repo.search_doctors(specialty=env["code"])
    assert any(env["doc_last"] in d["full_name"] for d in docs)


def test_available_slots_count(repo, make_env):
    env = make_env(k=4)
    slots = repo.get_available_slots(specialty=env["code"], limit=100)
    assert len(slots) == 4
    assert all(s["specialty"] == env["name"] for s in slots)


def test_register_and_find_patient(repo, admin_conn):
    tag = uuid.uuid4().hex[:8]
    p = repo.register_patient(f"Реглов{tag}", "Иван", "1988-06-15", phone="+79001234567")
    try:
        assert p["id"] and p["first_name"] == "Иван"
        found = repo.find_patient(f"Реглов{tag}", "1988-06-15")
        assert found and found["id"] == p["id"]
    finally:
        admin_conn.cursor().execute("DELETE FROM clinic.patients WHERE id = %s;", (p["id"],))


def test_register_invalid_phone_raises(repo):
    with pytest.raises(RepositoryError) as ei:
        repo.register_patient("Кто", "То", "1990-01-01", phone="не-телефон")
    assert ei.value.code == "check"


def test_register_future_birthdate_raises(repo):
    future = (dt.date.today() + dt.timedelta(days=365)).strftime("%Y-%m-%d")
    with pytest.raises(RepositoryError):
        repo.register_patient("Буд", "Ущий", future)


def test_unknown_specialty_raises(repo, make_env):
    env = make_env()
    with pytest.raises(RepositoryError) as ei:
        repo.book_nearest_slot(env["patient_id"], "НесуществующаяСпец")
    assert ei.value.code == "specialty_not_found"


def test_book_nearest_then_next(repo, make_env):
    env = make_env(k=3)
    b1 = repo.book_nearest_slot(env["patient_id"], env["code"], notes="первый")
    b2 = repo.book_nearest_slot(env["patient_id"], env["code"], notes="второй")
    assert b1["appointment_id"] != b2["appointment_id"]
    assert b1["starts_at"] < b2["starts_at"]  # ближайший, затем следующий
    left = repo.get_available_slots(specialty=env["code"], limit=100)
    assert len(left) == 1  # из 3 слотов заняты 2


def test_book_until_exhausted_raises_no_slots(repo, make_env):
    env = make_env(k=2)
    repo.book_nearest_slot(env["patient_id"], env["code"])
    repo.book_nearest_slot(env["patient_id"], env["code"])
    with pytest.raises(RepositoryError) as ei:
        repo.book_nearest_slot(env["patient_id"], env["code"])
    assert ei.value.code == "no_slots"


def test_patient_appointments_lists(repo, make_env):
    env = make_env(k=2)
    repo.book_nearest_slot(env["patient_id"], env["code"])
    appts = repo.get_patient_appointments(env["patient_id"])
    assert len(appts) == 1
    assert appts[0]["specialty"] == env["name"]


def test_list_doctors_contains_env(repo, make_env):
    env = make_env()
    names = {d["full_name"] for d in repo.list_active_doctors()}
    assert any(env["doc_last"] in n for n in names)


def test_doctor_schedule_shows_booking(repo, make_env):
    env = make_env(k=2)
    repo.book_nearest_slot(env["patient_id"], env["code"], notes="жалоба")
    sched = repo.get_doctor_schedule(env["doc_id"])
    assert len(sched) == 1
    assert sched[0]["specialty"] == env["name"] and sched[0]["status"] == "booked"


def test_bot_cannot_cancel_via_repository(repo, make_env):
    """Роль бота (ai_bot_registrar) не имеет права отменять записи."""
    env = make_env(k=2)
    booked = repo.book_nearest_slot(env["patient_id"], env["code"])
    with pytest.raises(RepositoryError) as ei:
        repo.cancel_appointment(booked["appointment_id"], "тест")
    assert ei.value.code == "privilege"


def test_book_specific_slot_and_double(repo, make_env):
    env = make_env(k=3)
    target = env["slot_ids"][1]  # конкретный слот
    try:
        res = repo.book_specific_slot(env["patient_id"], target)
    except RepositoryError as e:
        if e.code == "not_installed":
            pytest.skip("clinic.book_specific_slot не установлена")
        raise
    assert res["status"] == "booked"
    # повторная запись на тот же слот запрещена
    with pytest.raises(RepositoryError) as ei:
        repo.book_specific_slot(env["patient_id"], target)
    assert ei.value.code == "slot_taken"


# ---------------------------------------------------------------------------
# 2. Разграничение прав (роль бота не должна уметь менять/удалять данные)
# ---------------------------------------------------------------------------
FORBIDDEN_SQL = [
    ("UPDATE patients", "UPDATE clinic.patients SET phone = '000' WHERE id = 1"),
    ("DELETE patients", "DELETE FROM clinic.patients WHERE id = 1"),
    ("direct INSERT appointments",
     "INSERT INTO clinic.appointments(slot_id, patient_id, specialty_id) VALUES (1, 1, 1)"),
    ("UPDATE slots", "UPDATE clinic.slots SET is_available = false WHERE id = 1"),
    ("EXECUTE cancel_appointment", "SELECT clinic.cancel_appointment(1)"),
    ("EXECUTE generate_slots", "SELECT clinic.generate_slots(1, CURRENT_DATE, CURRENT_DATE)"),
]


@pytest.mark.parametrize("label,sql", FORBIDDEN_SQL, ids=[x[0] for x in FORBIDDEN_SQL])
def test_bot_role_is_restricted(bot_conn, label, sql):
    cur = bot_conn.cursor()
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute(sql)
    cur.close()


def test_bot_role_can_read_and_register(bot_conn, admin_conn):
    """Позитивный контроль: то, что боту РАЗРЕШЕНО, действительно работает."""
    cur = bot_conn.cursor()
    cur.execute("SELECT 1 FROM clinic.doctors LIMIT 1;")   # SELECT разрешён
    tag = uuid.uuid4().hex[:8]
    cur.execute(
        "INSERT INTO clinic.patients(last_name, first_name, birth_date) "
        "VALUES(%s,'Ок','1991-02-03') RETURNING id;",
        (f"Позит{tag}",),
    )
    new_id = cur.fetchone()[0]
    assert new_id  # INSERT в patients разрешён
    cur.close()
    admin_conn.cursor().execute("DELETE FROM clinic.patients WHERE id = %s;", (new_id,))


# ---------------------------------------------------------------------------
# 3. Конкурентная запись (защита от двойного бронирования)
# ---------------------------------------------------------------------------
def test_concurrent_booking_no_double(repo, make_env):
    """
    K свободных слотов, T>K потоков одновременно бронируют ближайший слот той же
    специальности. Ожидаем: ровно K успешных записей на РАЗНЫЕ слоты, остальные
    получают 'no_slots'. Двойного бронирования быть не должно.
    """
    K, T = 5, 12
    env = make_env(k=K)

    successes: list[dict] = []
    failures: list[str] = []

    def worker(_):
        try:
            return ("ok", repo.book_nearest_slot(env["patient_id"], env["code"]))
        except RepositoryError as e:
            return ("err", e.code)

    with ThreadPoolExecutor(max_workers=T) as ex:
        for kind, payload in ex.map(worker, range(T)):
            (successes if kind == "ok" else failures).append(payload)

    booked_slots = [s["appointment_id"] for s in successes]
    # 1) ровно K успешных
    assert len(successes) == K, f"ожидали {K} успешных, получили {len(successes)}"
    # 2) все записи уникальны (нет двойного бронирования)
    starts = [s["starts_at"] for s in successes]
    assert len(set(starts)) == K, "обнаружено двойное бронирование одного слота"
    assert len(set(booked_slots)) == K
    # 3) остальные — честный отказ по отсутствию слотов
    assert len(failures) == T - K
    assert all(code == "no_slots" for code in failures)

    # контроль на стороне БД: свободных слотов не осталось
    assert repo.get_available_slots(specialty=env["code"], limit=100) == []


# ---------------------------------------------------------------------------
# 4. Путь врача/регистратора: запись вне сетки и отмена (роль registrar)
# ---------------------------------------------------------------------------
@pytest.fixture()
def reg_repo():
    try:
        r = ClinicRepository(
            minconn=1, maxconn=4,
            user=os.getenv("REGISTRAR_USER", "registrar"),
            password=os.getenv("REGISTRAR_PASSWORD", "registrar"),
        )
        r.healthcheck()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Нет подключения registrar: {e}")
    yield r
    r.close()


def test_book_adhoc_and_cancel(reg_repo, make_env):
    env = make_env(k=1)
    # заведомо будущее время, не пересекающееся с сеткой env-слотов
    ts = (FUTURE_DAY + dt.timedelta(days=1, hours=3)).strftime("%Y-%m-%d %H:%M")
    res = reg_repo.book_adhoc(env["patient_id"], env["doc_id"], ts, notes="вне сетки")
    assert res["status"] == "booked" and res["appointment_id"]
    # отмена этой же записи
    out = reg_repo.cancel_appointment(res["appointment_id"], "тест отмены")
    assert out["status"] == "cancelled"


def test_registrar_cannot_delete(reg_repo):
    """registrar тоже не имеет DELETE (проверяем через сырое соединение роли)."""
    import psycopg2 as _pg
    conn = _pg.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "clinic"),
        user=os.getenv("REGISTRAR_USER", "registrar"),
        password=os.getenv("REGISTRAR_PASSWORD", "registrar"),
        sslmode=os.getenv("DB_SSLMODE", "prefer"), client_encoding="UTF8",
    )
    conn.autocommit = True
    try:
        with pytest.raises(_pg.errors.InsufficientPrivilege):
            conn.cursor().execute("DELETE FROM clinic.patients WHERE id = 1")
    finally:
        conn.close()