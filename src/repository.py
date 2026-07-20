from __future__ import annotations

import os
import datetime as dt
from contextlib import contextmanager
from typing import Any, Optional

from pgvector.psycopg2 import register_vector
import psycopg2
from psycopg2 import errorcodes
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool


class RepositoryError(Exception):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code


def _db_config(user: Optional[str] = None, password: Optional[str] = None) -> dict[str, Any]:
    return {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": os.getenv("DB_PORT", "5432"),
        "dbname": os.getenv("DB_NAME", "clinic"),
        "user": user or os.getenv("DB_USER", "ai_bot_registrar"),
        "password": password if password is not None else os.getenv("DB_PASSWORD", ""),
        "sslmode": os.getenv("DB_SSLMODE", "prefer"),
        "client_encoding": os.getenv("DB_CLIENT_ENCODING", "UTF8"),
        "options": f"-c timezone={os.getenv('DB_TIMEZONE', 'Europe/Moscow')}",
    }


class ClinicRepository:
    """Пул соединений + методы доступа. Один экземпляр на процесс/роль."""

    def __init__(
            self,
            minconn: int = 1,
            maxconn: int = 5,
            user: Optional[str] = None,
            password: Optional[str] = None,
    ):
        try:
            self._pool = ThreadedConnectionPool(minconn, maxconn, **_db_config(user, password))
        except psycopg2.OperationalError as e:
            raise RepositoryError(
                "Не удалось подключиться к базе данных. Проверьте параметры в .env "
                f"(хост, порт, имя БД, пользователь, пароль) и доступность сервера. Техдетали: {e}",
                code="connect",
            )
        except UnicodeDecodeError:
            raise RepositoryError(
                "Ошибка кодировки сообщения от сервера БД (частый случай на Windows). "
                "Проверьте реквизиты в .env и client_encoding=UTF8; помогает также "
                "lc_messages='C' в postgresql.conf.",
                code="encoding",
            )

    def close(self) -> None:
        if getattr(self, "_pool", None):
            self._pool.closeall()

    @contextmanager
    def _cursor(self, *, write: bool = False):
        conn = self._pool.getconn()
        try:
            register_vector(conn)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                yield cur
            conn.commit() if write else conn.rollback()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ======================================================================
    #  Общие/справочные чтения
    # ======================================================================
    def healthcheck(self) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute("SELECT current_database() AS db, session_user, current_user;")
            return dict(cur.fetchone())

    def list_specialties(self) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT id, code, name FROM clinic.specialties ORDER BY name;")
            return [dict(r) for r in cur.fetchall()]

    def list_active_doctors(self) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT id, full_name FROM clinic.doctors WHERE is_active = TRUE ORDER BY full_name;"
            )
            return [dict(r) for r in cur.fetchall()]

    def search_doctors(self, name: Optional[str] = None, specialty: Optional[str] = None) -> list[dict]:
        sql = """ \
              SELECT d.id, \
                     d.full_name, \
                     d.is_active, \
                     ARRAY_AGG(s.name ORDER BY s.name) AS specialties
              FROM clinic.doctors d \
                       JOIN clinic.doctor_specialties ds ON ds.doctor_id = d.id \
                       JOIN clinic.specialties s ON s.id = ds.specialty_id \
              WHERE d.is_active = TRUE
        """
        params: list[Any] = []
        if name:
            sql += " AND d.full_name ILIKE %s"
            params.append(f"%{name.strip()}%")
        if specialty:
            sql += " AND (s.name ILIKE %s OR s.code = %s)"
            params += [f"%{specialty.strip()}%", specialty.strip().lower()]
        sql += " GROUP BY d.id, d.full_name, d.is_active ORDER BY d.full_name;"
        with self._cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def get_available_slots(
            self,
            specialty: Optional[str] = None,
            doctor_name: Optional[str] = None,
            date: Optional[str] = None,
            limit: int = 50,
    ) -> list[dict]:
        """Свободные слоты: открыт (is_available) И без активной записи."""
        target_date = self._parse_date(date) if date else None
        sql = """ \
              SELECT sl.id       AS slot_id, \
                     d.full_name AS doctor, \
                     s.name      AS specialty, \
                     s.code      AS specialty_code, \
                     sl.starts_at, \
                     sl.ends_at
              FROM clinic.slots sl \
                       JOIN clinic.doctors d ON d.id = sl.doctor_id \
                       JOIN clinic.doctor_specialties ds ON ds.doctor_id = d.id \
                       JOIN clinic.specialties s ON s.id = ds.specialty_id \
              WHERE d.is_active = TRUE \
                AND sl.is_available = TRUE \
                AND sl.starts_at >= now() \
                AND NOT EXISTS (SELECT 1 \
                                FROM clinic.appointments a
                                WHERE a.slot_id = sl.id \
                                  AND a.status <> 'cancelled')
        """
        params: list[Any] = []
        if target_date is not None:
            sql += " AND sl.starts_at::date = %s"
            params.append(target_date)
        if doctor_name:
            sql += " AND d.full_name ILIKE %s"
            params.append(f"%{doctor_name.strip()}%")
        if specialty:
            sql += " AND (s.name ILIKE %s OR s.code = %s)"
            params += [f"%{specialty.strip()}%", specialty.strip().lower()]
        sql += " ORDER BY sl.starts_at LIMIT %s;"
        params.append(int(limit))
        with self._cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def get_doctors_and_slots(
            self,
            specialty: Optional[str] = None,
            doctor_name: Optional[str] = None,
            target_date: Optional[str] = None,
    ) -> list[dict]:
        """
        Свободные слоты, сгруппированные по врачу (для речевых шаблонов бота).
        Возвращает [{doctor, specialty, date, free_times:[HH:MM,...]}].
        """
        if not target_date:
            target_date = dt.date.today().strftime("%Y-%m-%d")
        rows = self.get_available_slots(
            specialty=specialty, doctor_name=doctor_name, date=target_date, limit=200
        )
        grouped: dict[tuple, dict] = {}
        for r in rows:
            key = (r["doctor"], r["specialty"])
            g = grouped.setdefault(key, {"doctor": r["doctor"], "specialty": r["specialty"],
                                         "date": target_date, "free_times": []})
            g["free_times"].append(self._fmt_time(r["starts_at"]))
        return list(grouped.values())

    def find_patient(self, last_name: str, birth_date: str) -> Optional[dict]:
        bd = self._parse_date(birth_date)
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id, last_name, first_name, middle_name, birth_date, phone
                FROM clinic.patients
                WHERE lower(last_name) = lower(%s)
                  AND birth_date = %s
                ORDER BY id LIMIT 1;
                """,
                (last_name.strip(), bd),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def find_patient_by_id(self, patient_id: int) -> Optional[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT id, last_name, first_name, middle_name, birth_date, phone "
                "FROM clinic.patients WHERE id = %s;",
                (int(patient_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_patient_appointments(self, patient_id: int) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT a.id        AS appointment_id,
                       d.full_name AS doctor,
                       s.name      AS specialty,
                       sl.starts_at,
                       a.status
                FROM clinic.appointments a
                         JOIN clinic.slots sl ON sl.id = a.slot_id
                         JOIN clinic.doctors d ON d.id = sl.doctor_id
                         JOIN clinic.specialties s ON s.id = a.specialty_id
                WHERE a.patient_id = %s
                  AND a.status <> 'cancelled'
                ORDER BY sl.starts_at;
                """,
                (int(patient_id),),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_doctor_schedule(self, doctor_id: int, include_cancelled: bool = True) -> list[dict]:
        """Записи к врачу (для панели врача/регистратора)."""
        sql = """ \
              SELECT a.id                               AS appointment_id, \
                     sl.starts_at, \
                     p.last_name || ' ' || p.first_name AS patient, \
                     p.phone, \
                     a.status, \
                     s.name                             AS specialty
              FROM clinic.appointments a \
                       JOIN clinic.slots sl ON sl.id = a.slot_id \
                       JOIN clinic.patients p ON p.id = a.patient_id \
                       JOIN clinic.specialties s ON s.id = a.specialty_id \
              WHERE sl.doctor_id = %s
        """
        if not include_cancelled:
            sql += " AND a.status <> 'cancelled'"
        sql += " ORDER BY sl.starts_at;"
        with self._cursor() as cur:
            cur.execute(sql, (int(doctor_id),))
            return [dict(r) for r in cur.fetchall()]

    # ======================================================================
    #  Запись пациента (регистрация)
    # ======================================================================
    def register_patient(
            self,
            last_name: str,
            first_name: str,
            birth_date: str,
            phone: Optional[str] = None,
            middle_name: Optional[str] = None,
    ) -> dict:
        bd = self._parse_date(birth_date)
        try:
            with self._cursor(write=True) as cur:
                cur.execute(
                    """
                    INSERT INTO clinic.patients (last_name, first_name, middle_name, birth_date, phone)
                    VALUES (%s, %s, %s, %s, %s) RETURNING id, last_name, first_name, middle_name, birth_date, phone;
                    """,
                    (last_name.strip(), first_name.strip(),
                     (middle_name.strip() if middle_name else None), bd,
                     (phone.strip() if phone else None)),
                )
                return dict(cur.fetchone())
        except psycopg2.errors.UniqueViolation:
            raise RepositoryError("Пациент с такими данными уже есть в системе.", code="duplicate")
        except psycopg2.errors.CheckViolation as e:
            raise RepositoryError(self._explain_check(e), code="check")
        except psycopg2.errors.InsufficientPrivilege:
            raise RepositoryError("Недостаточно прав для регистрации пациента.", code="privilege")
        except psycopg2.Error as e:
            raise RepositoryError(f"Ошибка регистрации пациента: {e.pgerror or e}", code="db")

    # ======================================================================
    #  Записи на приём (через хранимые функции)
    # ======================================================================
    def book_nearest_slot(self, patient_id: int, specialty: str,
                          from_datetime: Optional[str] = None, notes: Optional[str] = None) -> dict:
        specialty_id = self._resolve_specialty_id(specialty)
        p_from = self._parse_datetime(from_datetime) if from_datetime else None
        try:
            with self._cursor(write=True) as cur:
                cur.execute(
                    "SELECT * FROM clinic.book_nearest_slot(%s, %s, COALESCE(%s, now()), %s);",
                    (int(patient_id), specialty_id, p_from, notes),
                )
                appt = dict(cur.fetchone())
                return {"appointment_id": appt["id"], "patient_id": appt["patient_id"],
                        "status": appt["status"], **self._enrich(cur, appt["slot_id"], specialty_id)}
        except psycopg2.Error as e:
            raise self._map_booking_error(e)

    def book_specific_slot(self, patient_id: int, slot_id: int, notes: Optional[str] = None) -> dict:
        try:
            with self._cursor(write=True) as cur:
                cur.execute("SELECT * FROM clinic.book_specific_slot(%s, %s, %s);",
                            (int(patient_id), int(slot_id), notes))
                appt = dict(cur.fetchone())
                return {"appointment_id": appt["id"], "patient_id": appt["patient_id"],
                        "status": appt["status"], **self._enrich(cur, appt["slot_id"], appt["specialty_id"])}
        except psycopg2.errors.UniqueViolation:
            raise RepositoryError("Этот слот уже занят. Выберите другое время.", code="slot_taken")
        except psycopg2.errors.UndefinedFunction:
            raise RepositoryError("Функция записи на конкретный слот не установлена.", code="not_installed")
        except psycopg2.Error as e:
            if getattr(e, "pgcode", None) == errorcodes.NO_DATA_FOUND:
                raise RepositoryError("Слот не найден или закрыт. Обновите список слотов.", code="slot_missing")
            raise self._map_booking_error(e)

    def book_appointment_by_time(self, patient_id: int, doctor_name: str, date: str,
                                 time_slot: str, notes: Optional[str] = None) -> dict:
        """
        Запись к конкретному врачу на конкретные дату+время (модель шаблонов бота).
        Резолвит свободный слот и оформляет через clinic.book_specific_slot.
        """
        target_date = self._parse_date(date)
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT sl.id AS slot_id
                FROM clinic.slots sl
                         JOIN clinic.doctors d ON d.id = sl.doctor_id
                WHERE d.full_name ILIKE %s
                  AND sl.starts_at:: date = %s
                  AND to_char(sl.starts_at
                    , 'HH24:MI') = %s
                  AND sl.is_available
                  AND NOT EXISTS (SELECT 1 FROM clinic.appointments a
                    WHERE a.slot_id = sl.id
                  AND a.status <> 'cancelled')
                ORDER BY sl.starts_at LIMIT 1;
                """,
                (f"%{doctor_name.strip()}%", target_date, time_slot.strip()),
            )
            row = cur.fetchone()
        if not row:
            raise RepositoryError(
                "Свободный слот не найден: проверьте врача, дату и время (возможно, уже занято).",
                code="slot_missing",
            )
        return self.book_specific_slot(patient_id, row["slot_id"], notes)

    def book_adhoc(self, patient_id: int, doctor_id: int, starts_at,
                   duration_min: int = 30, notes: Optional[str] = None) -> dict:
        """
        Запись «вне сетки»: создаёт слот (или переиспользует существующий) и
        оформляет запись через SECURITY DEFINER-функцию clinic.book_adhoc.
        Доступно registrar/admin (у них нет прямого INSERT в slots).
        """
        ts = starts_at if isinstance(starts_at, dt.datetime) else self._parse_datetime(str(starts_at))
        try:
            with self._cursor(write=True) as cur:
                cur.execute("SELECT * FROM clinic.book_adhoc(%s, %s, %s, %s, NULL, %s);",
                            (int(patient_id), int(doctor_id), ts, int(duration_min), notes))
                appt = dict(cur.fetchone())
                return {"appointment_id": appt["id"], "patient_id": appt["patient_id"],
                        "status": appt["status"], **self._enrich(cur, appt["slot_id"], appt["specialty_id"])}
        except psycopg2.errors.UniqueViolation:
            raise RepositoryError("На это время у пациента/слота уже есть запись.", code="slot_taken")
        except psycopg2.errors.ExclusionViolation:
            raise RepositoryError("Это время пересекается с другим слотом врача.", code="overlap")
        except psycopg2.errors.UndefinedFunction:
            raise RepositoryError("Функция clinic.book_adhoc не установлена.", code="not_installed")
        except psycopg2.errors.InsufficientPrivilege:
            raise RepositoryError("Недостаточно прав для записи вне сетки.", code="privilege")
        except psycopg2.Error as e:
            raise self._map_booking_error(e)

    def cancel_appointment(self, appointment_id: int, reason: Optional[str] = None) -> dict:
        """Мягкая отмена через clinic.cancel_appointment (registrar/admin)."""
        try:
            with self._cursor(write=True) as cur:
                cur.execute("SELECT * FROM clinic.cancel_appointment(%s, %s);",
                            (int(appointment_id), reason))
                appt = dict(cur.fetchone())
                return {"appointment_id": appt["id"], "status": appt["status"]}
        except psycopg2.errors.InsufficientPrivilege:
            raise RepositoryError("Недостаточно прав для отмены записи.", code="privilege")
        except psycopg2.Error as e:
            if getattr(e, "pgcode", None) == errorcodes.NO_DATA_FOUND:
                raise RepositoryError("Запись не найдена или уже отменена.", code="not_found")
            raise RepositoryError(f"Не удалось отменить запись: {e.pgerror or e}", code="db")

    # ======================================================================
    #  Работа с базой знаний
    # ======================================================================

    def get_medical_knowledge_base_size(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM clinic.medical_knowledge_base;")
            row = cur.fetchone()
        return row["count"] if row else 0

    def add_specialty_to_medical_knowledge_base(
            self,
            doctor: str,
            title: str,
            chunk: str,
            embedding: list[float],
    ) -> None:
        with self._cursor(write=True) as cur:
            cur.execute(
                """INSERT INTO clinic.medical_knowledge_base
                      (specialty, wiki_page_title, chunk_text, embedding)
                   VALUES (%s, %s, %s, %s)""",
                (doctor, title, chunk, embedding)
            )

    def get_RAG_top_k(self, query_embedding, top_k):
        try:
            with self._cursor() as cur:
                cur.execute("""
                            SELECT specialty, wiki_page_title, chunk_text, (embedding <=> %s::vector) AS dist
                            FROM clinic.medical_knowledge_base
                            ORDER BY embedding <=> %s::vector LIMIT %s;
                            """, (query_embedding, query_embedding, top_k))
                return cur.fetchall()
        except Exception as e:
            # TODO errors
            print(f"ошибка бд {e}")
            return []

    # ======================================================================
    #  Приватные помощники
    # ======================================================================
    def _enrich(self, cur, slot_id: int, specialty_id: int) -> dict:
        """Врач + специальность + время по слоту (в рамках текущей транзакции)."""
        cur.execute(
            """
            SELECT d.full_name AS doctor, s.name AS specialty, sl.starts_at
            FROM clinic.slots sl
                     JOIN clinic.doctors d ON d.id = sl.doctor_id
                     JOIN clinic.specialties s ON s.id = %s
            WHERE sl.id = %s;
            """,
            (specialty_id, slot_id),
        )
        return dict(cur.fetchone())

    def _resolve_specialty_id(self, specialty: str) -> int:
        term = (specialty or "").strip()
        term = (specialty or "").strip()
        if not term:
            raise RepositoryError("Не указана специальность.", code="specialty")
        with self._cursor() as cur:
            cur.execute(
                "SELECT id FROM clinic.specialties WHERE code = %s OR name ILIKE %s "
                "ORDER BY (code = %s) DESC LIMIT 1;",
                (term.lower(), f"%{term}%", term.lower()),
            )
            row = cur.fetchone()
        if not row:
            available = ", ".join(s["name"] for s in self.list_specialties())
            raise RepositoryError(
                f"Специальность «{specialty}» не найдена. Доступны: {available}.",
                code="specialty_not_found",
            )
        return int(row["id"])

    def _map_booking_error(self, e: psycopg2.Error) -> RepositoryError:
        code = getattr(e, "pgcode", None)
        table = {
            errorcodes.NO_DATA_FOUND: ("Свободных слотов нужной специальности на выбранный период нет.", "no_slots"),
            errorcodes.FOREIGN_KEY_VIOLATION: ("Пациент, врач или специальность не найдены.", "fk"),
            errorcodes.LOCK_NOT_AVAILABLE: ("Слишком много одновременных записей, попробуйте ещё раз.", "busy"),
            errorcodes.INSUFFICIENT_PRIVILEGE: ("Недостаточно прав для записи на приём.", "privilege"),
        }
        if code in table:
            msg, c = table[code]
            return RepositoryError(msg, code=c)
        return RepositoryError(f"Не удалось оформить запись: {e.pgerror or e}", code="db")

    @staticmethod
    def _explain_check(e: psycopg2.errors.CheckViolation) -> str:
        name = getattr(getattr(e, "diag", None), "constraint_name", "") or ""
        return {
            "ck_patients_phone": "Некорректный номер телефона.",
            "ck_patients_birth": "Некорректная дата рождения.",
            "ck_patients_names": "Фамилия и имя не должны быть пустыми.",
            "ck_patients_passport": "Некорректный формат паспорта.",
        }.get(name, "Данные пациента не прошли проверку корректности.")

    @staticmethod
    def _parse_date(value) -> dt.date:
        if isinstance(value, dt.date):
            return value
        try:
            return dt.datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
        except ValueError:
            raise RepositoryError(f"Ожидается дата в формате ГГГГ-ММ-ДД, получено «{value}».", code="date")

    @staticmethod
    def _parse_datetime(value) -> dt.datetime:
        if isinstance(value, dt.datetime):
            return value
        s = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(s, fmt)
            except ValueError:
                continue
        raise RepositoryError(f"Ожидается дата/время ГГГГ-ММ-ДД [ЧЧ:ММ], получено «{value}».", code="datetime")

    @staticmethod
    def _fmt_time(value) -> str:
        return value.strftime("%H:%M") if isinstance(value, dt.datetime) else str(value)
