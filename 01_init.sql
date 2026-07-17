-- ============================================================================
--  Регулировщик — схема БД клиники (PostgreSQL 14+)
--  Сущности: пациенты, врачи, специальности, расписание (слоты),
--            записи на прием. Роли: ai_bot_registrar, registrar, admin.
--  Идентификаторы — англ. snake_case; комментарии — рус.
--  Запускать под привилегированной ролью (владелец схемы) — она станет
--  владельцем SECURITY DEFINER-функции book_nearest_slot.
-- ============================================================================

-- Расширения (ставятся один раз на базу).
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- нечеткий поиск врачей по ФИО
CREATE EXTENSION IF NOT EXISTS btree_gist;   -- exclusion-constraint на пересечение слотов

CREATE SCHEMA IF NOT EXISTS clinic;
SET search_path = clinic, public;

-- ---------- ENUM статусов приема ----------
DO $$ BEGIN
    CREATE TYPE clinic.appointment_status
        AS ENUM ('booked', 'cancelled', 'completed', 'no_show');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ---------- Общая триггерная функция updated_at ----------
CREATE OR REPLACE FUNCTION clinic.set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- ============================================================================
--  Справочник специальностей
-- ============================================================================
CREATE TABLE clinic.specialties (
    id         integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code       text        NOT NULL,          -- машинный код: 'cardiology'
    name       text        NOT NULL,          -- отображаемое имя: 'Кардиолог'
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_specialties_code UNIQUE (code),
    CONSTRAINT uq_specialties_name UNIQUE (name),
    CONSTRAINT ck_specialties_code CHECK (code ~ '^[a-z0-9_]+$')
);

CREATE TRIGGER trg_specialties_updated BEFORE UPDATE ON clinic.specialties
    FOR EACH ROW EXECUTE FUNCTION clinic.set_updated_at();

-- ============================================================================
--  Врачи
-- ============================================================================
CREATE TABLE clinic.doctors (
    id          integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    last_name   text        NOT NULL,
    first_name  text        NOT NULL,
    middle_name text,
    -- вычисляемое ФИО для поиска
    full_name   text        GENERATED ALWAYS AS (
                    btrim(last_name || ' ' || first_name || coalesce(' ' || middle_name, ''))
                ) STORED,
    is_active   boolean     NOT NULL DEFAULT true,   -- мягкое «увольнение»
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_doctors_names CHECK (
        length(btrim(last_name)) > 0 AND length(btrim(first_name)) > 0
    )
);

CREATE INDEX idx_doctors_full_name_trgm            -- под ILIKE '%...%'
    ON clinic.doctors USING gin (full_name gin_trgm_ops);
CREATE INDEX idx_doctors_last_name                 -- под префиксный поиск
    ON clinic.doctors (last_name);

CREATE TRIGGER trg_doctors_updated BEFORE UPDATE ON clinic.doctors
    FOR EACH ROW EXECUTE FUNCTION clinic.set_updated_at();

-- ============================================================================
--  Врач <-> специальность  (многие-ко-многим)
-- ============================================================================
CREATE TABLE clinic.doctor_specialties (
    doctor_id    integer NOT NULL REFERENCES clinic.doctors(id)     ON DELETE CASCADE,
    specialty_id integer NOT NULL REFERENCES clinic.specialties(id) ON DELETE RESTRICT,
    PRIMARY KEY (doctor_id, specialty_id)
);
-- обратный поиск «врачи по специальности» (нужен для записи)
CREATE INDEX idx_docspec_specialty ON clinic.doctor_specialties (specialty_id, doctor_id);

-- ============================================================================
--  Пациенты
-- ============================================================================
CREATE TABLE clinic.patients (
    id                integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    last_name         text        NOT NULL,
    first_name        text        NOT NULL,
    middle_name       text,             -- отчество если есть
    birth_date        date        NOT NULL,
    passport          text,             -- серия+номер; только если указан
    telegram_id       bigint,           -- 64-битный user id из Telegram
    telegram_username text,             -- без '@', сравнение регистронезависимое
    phone             text,
    created_by        text        NOT NULL DEFAULT session_user,  -- аудит: кто создал
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_patients_names CHECK (
        length(btrim(last_name)) > 0 AND length(btrim(first_name)) > 0
    ),
    CONSTRAINT ck_patients_birth CHECK (birth_date >= DATE '1900-01-01'),
    CONSTRAINT ck_patients_passport CHECK (
        passport IS NULL OR passport ~ '^[0-9A-Za-z]{5,20}$'
    ),
    CONSTRAINT ck_patients_tg_id CHECK (telegram_id IS NULL OR telegram_id > 0),
    CONSTRAINT ck_patients_tg_username CHECK (
        telegram_username IS NULL OR telegram_username ~ '^[A-Za-z0-9_]{5,32}$'
    ),
    CONSTRAINT ck_patients_phone CHECK (
        phone IS NULL OR phone ~ '^\+?[0-9][0-9 ()\-]{4,19}$'
    )
);

-- «уникально, если указано» — частичные уникальные индексы + быстрый поиск
CREATE UNIQUE INDEX uq_patients_passport
    ON clinic.patients (passport)            WHERE passport IS NOT NULL;
CREATE UNIQUE INDEX uq_patients_telegram_id
    ON clinic.patients (telegram_id)         WHERE telegram_id IS NOT NULL;
CREATE UNIQUE INDEX uq_patients_telegram_username
    ON clinic.patients (lower(telegram_username)) WHERE telegram_username IS NOT NULL;
CREATE INDEX idx_patients_name ON clinic.patients (last_name, first_name);

-- запрет даты рождения из будущего (CURRENT_DATE не IMMUTABLE -> нельзя в CHECK)
CREATE OR REPLACE FUNCTION clinic.check_birth_not_future()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.birth_date > CURRENT_DATE THEN
        RAISE EXCEPTION 'Дата рождения % не может быть в будущем', NEW.birth_date
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_patients_birth BEFORE INSERT OR UPDATE ON clinic.patients
    FOR EACH ROW EXECUTE FUNCTION clinic.check_birth_not_future();

CREATE TRIGGER trg_patients_updated BEFORE UPDATE ON clinic.patients
    FOR EACH ROW EXECUTE FUNCTION clinic.set_updated_at();

-- ============================================================================
--  Расписание: дискретные слоты приема врача
-- ============================================================================
CREATE TABLE clinic.slots (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doctor_id    integer     NOT NULL REFERENCES clinic.doctors(id) ON DELETE CASCADE,
    starts_at    timestamptz NOT NULL,
    ends_at      timestamptz NOT NULL,
    is_available boolean     NOT NULL DEFAULT true,  -- false = слот закрыт админом
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_slots_interval CHECK (ends_at > starts_at),
    -- один врач не может иметь пересекающиеся слоты
    CONSTRAINT ex_slots_no_overlap EXCLUDE USING gist (
        doctor_id  WITH =,
        tstzrange(starts_at, ends_at) WITH &&
    )
);

CREATE INDEX idx_slots_doctor_start ON clinic.slots (doctor_id, starts_at);
CREATE INDEX idx_slots_start_available
    ON clinic.slots (starts_at) WHERE is_available;

CREATE TRIGGER trg_slots_updated BEFORE UPDATE ON clinic.slots
    FOR EACH ROW EXECUTE FUNCTION clinic.set_updated_at();

-- ============================================================================
--  Записи на прием
-- ============================================================================
CREATE TABLE clinic.appointments (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slot_id       bigint      NOT NULL REFERENCES clinic.slots(id)       ON DELETE RESTRICT,
    patient_id    integer     NOT NULL REFERENCES clinic.patients(id)    ON DELETE RESTRICT,
    specialty_id  integer     NOT NULL REFERENCES clinic.specialties(id) ON DELETE RESTRICT,
    status        clinic.appointment_status NOT NULL DEFAULT 'booked',
    notes         text,                      -- жалоба / примечание регистратора
    cancel_reason text,
    cancelled_at  timestamptz,
    created_by    text        NOT NULL DEFAULT session_user,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    -- согласованность мягкой отмены: cancelled_at заполнен <=> status='cancelled'
    CONSTRAINT ck_appt_cancel CHECK (
        (status = 'cancelled') = (cancelled_at IS NOT NULL)
    )
);

-- ГЛАВНАЯ защита от двойной записи:
-- на один слот не более ОДНОЙ не-отмененной записи.
CREATE UNIQUE INDEX uq_appt_active_slot
    ON clinic.appointments (slot_id) WHERE status <> 'cancelled';

CREATE INDEX idx_appt_patient   ON clinic.appointments (patient_id);
CREATE INDEX idx_appt_specialty ON clinic.appointments (specialty_id);
CREATE INDEX idx_appt_slot      ON clinic.appointments (slot_id);

CREATE TRIGGER trg_appts_updated BEFORE UPDATE ON clinic.appointments
    FOR EACH ROW EXECUTE FUNCTION clinic.set_updated_at();

-- ============================================================================
--  ФУНКЦИЯ: запись на ближайший свободный слот по специальности
--  Конкурентная безопасность обеспечивается тремя слоями:
--   1) FOR UPDATE OF s SKIP LOCKED — параллельные транзакции не берут один
--      и тот же слот, а сразу перескакивают на следующий свободный;
--   2) частичный уникальный индекс uq_appt_active_slot — жесткий барьер БД;
--   3) цикл с перехватом unique_violation — если кто-то занял слот в обход
--      функции, пробуем следующий.
--  SECURITY DEFINER: бот получает только EXECUTE и не имеет прямого INSERT в
--  appointments. created_by пишется через session_user -> реальный вызывающий.
-- ============================================================================
CREATE OR REPLACE FUNCTION clinic.book_nearest_slot(
    p_patient_id   integer,
    p_specialty_id integer,
    p_from         timestamptz DEFAULT now(),
    p_notes        text        DEFAULT NULL
) RETURNS clinic.appointments
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = clinic, pg_temp
AS $$
DECLARE
    v_slot_id  bigint;
    v_appt     clinic.appointments%ROWTYPE;
    v_attempts integer := 0;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM clinic.patients WHERE id = p_patient_id) THEN
        RAISE EXCEPTION 'Пациент id=% не найден', p_patient_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM clinic.specialties WHERE id = p_specialty_id) THEN
        RAISE EXCEPTION 'Специальность id=% не найдена', p_specialty_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    LOOP
        v_attempts := v_attempts + 1;
        EXIT WHEN v_attempts > 5;

        -- ближайший свободный слот врача нужной специальности
        SELECT s.id
          INTO v_slot_id
          FROM clinic.slots s
          JOIN clinic.doctor_specialties ds ON ds.doctor_id = s.doctor_id
         WHERE ds.specialty_id = p_specialty_id
           AND s.is_available
           AND s.starts_at >= p_from
           AND NOT EXISTS (
                 SELECT 1 FROM clinic.appointments a
                  WHERE a.slot_id = s.id
                    AND a.status <> 'cancelled'
               )
         ORDER BY s.starts_at
         FOR UPDATE OF s SKIP LOCKED
         LIMIT 1;

        IF v_slot_id IS NULL THEN
            RAISE EXCEPTION 'Нет свободных слотов для специальности id=% после %',
                p_specialty_id, p_from
                USING ERRCODE = 'no_data_found';
        END IF;

        BEGIN
            INSERT INTO clinic.appointments (slot_id, patient_id, specialty_id, status, notes)
            VALUES (v_slot_id, p_patient_id, p_specialty_id, 'booked', p_notes)
            RETURNING * INTO v_appt;
            RETURN v_appt;                       -- успех
        EXCEPTION WHEN unique_violation THEN
            CONTINUE;                            -- слот заняли в гонке — берем следующий
        END;
    END LOOP;

    RAISE EXCEPTION 'Не удалось записать после % попыток (высокая конкуренция)', v_attempts - 1
        USING ERRCODE = 'lock_not_available';
END;
$$;

-- ============================================================================
--  ФУНКЦИЯ: мягкая отмена записи (без физического удаления)
--  SECURITY INVOKER: выполняется под правами вызывающего -> нужен UPDATE на
--  appointments. У ai_bot_registrar его нет, поэтому даже при наличии EXECUTE
--  отменить он не сможет; EXECUTE ему и не выдается.
-- ============================================================================
CREATE OR REPLACE FUNCTION clinic.cancel_appointment(
    p_appointment_id bigint,
    p_reason         text DEFAULT NULL
) RETURNS clinic.appointments
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = clinic, pg_temp
AS $$
DECLARE
    v_appt clinic.appointments%ROWTYPE;
BEGIN
    UPDATE clinic.appointments
       SET status = 'cancelled', cancelled_at = now(), cancel_reason = p_reason
     WHERE id = p_appointment_id
       AND status <> 'cancelled'
    RETURNING * INTO v_appt;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Запись id=% не найдена или уже отменена', p_appointment_id
            USING ERRCODE = 'no_data_found';
    END IF;
    RETURN v_appt;
END;
$$;

-- ============================================================================
--  ФУНКЦИЯ: генерация слотов расписания (утилита администратора)
--  Будни, интервал по умолчанию 30 мин, окно 09:00–17:00.
-- ============================================================================
CREATE OR REPLACE FUNCTION clinic.generate_slots(
    p_doctor_id    integer,
    p_from_date    date,
    p_to_date      date,
    p_slot_minutes integer DEFAULT 30,
    p_day_start    time    DEFAULT '09:00',
    p_day_end      time    DEFAULT '17:00'
) RETURNS integer
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = clinic, pg_temp
AS $$
DECLARE
    v_count integer := 0;
    d       date := p_from_date;
    ts      timestamptz;
    ts_end  timestamptz;
BEGIN
    WHILE d <= p_to_date LOOP
        IF extract(isodow FROM d) BETWEEN 1 AND 5 THEN     -- пн-пт
            ts := (d + p_day_start)::timestamptz;
            WHILE ts + make_interval(mins => p_slot_minutes) <= (d + p_day_end)::timestamptz LOOP
                ts_end := ts + make_interval(mins => p_slot_minutes);
                BEGIN
                    INSERT INTO clinic.slots (doctor_id, starts_at, ends_at)
                    VALUES (p_doctor_id, ts, ts_end);
                    v_count := v_count + 1;
                EXCEPTION WHEN exclusion_violation OR unique_violation THEN
                    NULL;                                  -- слот уже есть — пропускаем
                END;
                ts := ts_end;
            END LOOP;
        END IF;
        d := d + 1;
    END LOOP;
    RETURN v_count;
END;
$$;

-- ============================================================================
--  РОЛИ И ПРАВА
-- ============================================================================

-- Удаляем старые роли, если они вдруг остались от прошлых запусков, чтобы не было конфликтов
DROP ROLE IF EXISTS ai_bot_registrar;
DROP ROLE IF EXISTS registrar;
DROP ROLE IF EXISTS admin;

-- Создаем прикладные роли с возможностью входа (LOGIN) и паролями
-- (Пароли жестко синхронизированы с вашим .env)
CREATE ROLE ai_bot_registrar WITH LOGIN PASSWORD 'notahorse';
CREATE ROLE registrar WITH LOGIN PASSWORD 'registrar';
CREATE ROLE admin WITH LOGIN PASSWORD 'admin';

-- Убираем «широкие» дефолтные права для всех
REVOKE ALL ON SCHEMA clinic FROM PUBLIC;
GRANT USAGE ON SCHEMA clinic TO ai_bot_registrar, registrar, admin;

-- ---------- admin: полные права + права на будущие объекты ----------
GRANT ALL PRIVILEGES ON ALL TABLES    IN SCHEMA clinic TO admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA clinic TO admin;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA clinic TO admin;
GRANT CREATE ON SCHEMA clinic TO admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA clinic GRANT ALL PRIVILEGES ON TABLES    TO admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA clinic GRANT ALL PRIVILEGES ON SEQUENCES TO admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA clinic GRANT EXECUTE       ON FUNCTIONS  TO admin;

-- ---------- registrar: чтение всего + ведение пациентов и записей ----------
GRANT SELECT ON ALL TABLES IN SCHEMA clinic TO registrar;
GRANT INSERT, UPDATE ON clinic.patients     TO registrar;
GRANT INSERT, UPDATE ON clinic.appointments TO registrar;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA clinic TO registrar;

-- ---------- ai_bot_registrar: только чтение + регистрация; запись — через функцию ----------
GRANT SELECT ON clinic.patients, clinic.doctors, clinic.specialties,
                clinic.doctor_specialties, clinic.slots, clinic.appointments
    TO ai_bot_registrar;
GRANT INSERT ON clinic.patients TO ai_bot_registrar;

-- ---------- права на функции (забираем у PUBLIC и выдаем точечно) ----------
REVOKE ALL ON FUNCTION clinic.book_nearest_slot(integer,integer,timestamptz,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION clinic.cancel_appointment(bigint,text)                      FROM PUBLIC;
REVOKE ALL ON FUNCTION clinic.generate_slots(integer,date,date,integer,time,time)  FROM PUBLIC;

GRANT EXECUTE ON FUNCTION clinic.book_nearest_slot(integer,integer,timestamptz,text)
    TO ai_bot_registrar, registrar, admin;
GRANT EXECUTE ON FUNCTION clinic.cancel_appointment(bigint,text)
    TO registrar, admin;
GRANT EXECUTE ON FUNCTION clinic.generate_slots(integer,date,date,integer,time,time)
    TO admin;