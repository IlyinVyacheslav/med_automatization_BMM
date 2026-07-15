import psycopg2
from psycopg2 import sql

# Конфигурация подключения (используйте учетную запись суперадмина для создания таблиц)
DB_CONFIG = {
    "host": "127.0.0.1",
    "database": "clinic_bot_db",
    "user": "postgres",
    "password": "123",
    "port": "5432",
    "sslmode": "disable"
}


def init_medical_database():
    try:
        # Подключение к PostgreSQL
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
        cur = conn.cursor()

        print("Подключение успешно. Начинаю создание структуры таблиц...")

        # 1. Сброс старых таблиц (в обратном порядке зависимостей, чтобы избежать конфликтов FK)
        cur.execute("DROP TABLE IF EXISTS admissions CASCADE;")
        cur.execute("DROP TABLE IF EXISTS insurance_policies CASCADE;")
        cur.execute("DROP TABLE IF EXISTS doctors CASCADE;")
        cur.execute("DROP TABLE IF EXISTS patients CASCADE;")
        print("[-] Старые таблицы успешно удалены (если они существовали).")

        # 2. Таблица врачей (doctors)
        cur.execute("""
                    CREATE TABLE doctors
                    (
                        doctor_id  SERIAL PRIMARY KEY,
                        full_name  VARCHAR(150) NOT NULL, -- ФИО (например, Кучибоев Шохрухбек Шаробиддинович)
                        specialty  VARCHAR(100),          -- Специальность
                        department VARCHAR(150)           -- Отделение (например, Приемное отделение)
                    );
                    """)
        print("[+] Таблица 'doctors' создана.")

        # 3. Таблица пациентов (patients)
        # Хранит исчерпывающие данные, включая прикрепление, паспорт, адрес и Telegram ID для бота.
        cur.execute("""
                    CREATE TABLE patients
                    (
                        patient_id           SERIAL PRIMARY KEY,
                        telegram_id          BIGINT UNIQUE,            -- ID чата для работы ИИ-агента
                        external_user_id     VARCHAR(50),              -- ID Пользователя в МИС (например, 101764000)

                        -- Личные данные
                        last_name            VARCHAR(100) NOT NULL,
                        first_name           VARCHAR(100) NOT NULL,
                        middle_name          VARCHAR(100),
                        birth_date           DATE         NOT NULL,
                        gender               VARCHAR(10),              -- муж / жен
                        birth_place          TEXT,
                        snils                VARCHAR(20) UNIQUE,       -- СНИЛС (например, 165-454-976 02)
                        phone_number         VARCHAR(20),

                        -- Паспортные данные
                        passport_series      VARCHAR(10),
                        passport_number      VARCHAR(10),
                        passport_issued_by   TEXT,
                        passport_issue_date  DATE,
                        passport_dept_code   VARCHAR(10),              -- Код подразделения (например, 470-021)
                        citizenship          VARCHAR(50) DEFAULT 'Россия',
                        is_capable           BOOLEAN     DEFAULT TRUE, -- Дееспособность (да/нет)

                        -- Данные прикрепления к клинике (Сценарий 1)
                        attached_clinic      VARCHAR(150),             -- ГБУЗ ЛО "ГАТЧИНСКАЯ КМБ"
                        attachment_date      DATE,                     -- Дата прикрепления (например, 2026-04-13)

                        -- Адрес прописки / проживания
                        address_region       VARCHAR(100),             -- Ленинградская область
                        address_district     VARCHAR(100),             -- Гатчинский район
                        address_locality     VARCHAR(100),             -- Сиверский городской поселок
                        address_street       VARCHAR(150),             -- Военный городок
                        address_building     VARCHAR(20),              -- д. 73
                        address_apartment    VARCHAR(20),              -- кв. 8
                        address_full_comment TEXT                      -- Полный склеенный адрес
                    );
                    """)
        print("[+] Таблица 'patients' создана.")

        # 4. Таблица страховых полисов (insurance_policies)
        # Связь: Один Пациент -> Несколько полисов (ОМС / ДМС)
        cur.execute("""
                    CREATE TABLE insurance_policies
                    (
                        policy_id         SERIAL PRIMARY KEY,
                        patient_id        INTEGER     NOT NULL REFERENCES patients (patient_id) ON DELETE CASCADE,
                        policy_code       VARCHAR(20),          -- Шифр полиса (например, 5.08.0)
                        insurer           VARCHAR(100),         -- Страховщик (например, Согаз ОМС)
                        insurance_type    VARCHAR(100),         -- Бумажный полис ОМС единого образца
                        policy_series     VARCHAR(20),          -- Серия полиса (ЕП)
                        policy_number     VARCHAR(50) NOT NULL, -- Номер полиса
                        valid_from        DATE,                 -- Действителен от
                        valid_until       DATE,                 -- Действителен до
                        verification_date DATE                  -- Дата сверки
                    );
                    """)
        print("[+] Таблица 'insurance_policies' создана.")

        # 5. Таблица поступлений и медицинских случаев (admissions)
        # Связь: Пациент -> Поступление, Врач -> Поступление
        cur.execute("""
                    CREATE TABLE admissions
                    (
                        admission_id             SERIAL PRIMARY KEY,
                        patient_id               INTEGER            NOT NULL REFERENCES patients (patient_id) ON DELETE CASCADE,
                        doctor_id                INTEGER            REFERENCES doctors (doctor_id) ON DELETE SET NULL,

                        -- Учетные данные случая
                        history_number           VARCHAR(50) UNIQUE NOT NULL, -- Номер И/Б (например, 119573)
                        iemk_id                  VARCHAR(50),                 -- Идентификатор случая ИЭМК (34679576000)

                        -- Временные рамки
                        admission_time           TIMESTAMP          NOT NULL, -- Время поступления
                        discharge_time           TIMESTAMP,                   -- Время выбытия
                        discharge_type           VARCHAR(100),                -- Вид выбытия (Амбулаторное лечение)
                        outcome                  VARCHAR(100),                -- Исход (Улучшение)

                        -- Протокол первичного приема
                        medical_care_form        VARCHAR(50),                 -- Форма оказания (экстренная / плановая)
                        delivered_by             VARCHAR(150),                -- Кем доставлен (бригадой СМП)
                        transportation_type      VARCHAR(50),                 -- Вид транспортировки (на кресле / на каталке)
                        admission_term           VARCHAR(100),                -- Срок поступления от начала заболевания

                        -- Диагнозы (МКБ-10)
                        admission_diagnosis_code VARCHAR(20),                 -- Код диагноза поступления (S20.2)
                        admission_diagnosis_desc TEXT,                        -- Описание (Ушиб грудной клетки)
                        discharge_diagnosis_code VARCHAR(20),                 -- Код диагноза выписки (I42.8)
                        discharge_diagnosis_desc TEXT,                        -- Описание (Другие кардиомиопатии)

                        disease_character        VARCHAR(50),                 -- Характер заболевания (Острое)
                        external_cause_code      VARCHAR(20),                 -- Код внешней причины (W08.4)
                        external_cause_desc      TEXT,                        -- Описание причины (Падение на улице...)
                        injury_character         VARCHAR(100)                 -- Характер травмы (Непроизводственная - Уличная)
                    );
                    """)
        print("[+] Таблица 'admissions' создана.")

        print("\nИнициализация завершена! Все таблицы созданы, связи установлены корректно.")

    except Exception as e:
        print(f"\n[Ошибка] Не удалось инициализировать базу данных: {e}")
    finally:
        if 'conn' in locals() and conn:
            cur.close()
            conn.close()


if __name__ == "__main__":
    init_medical_database()