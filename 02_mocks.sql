-- ============================================================================
--  02_mocks.sql — Наполнение тестовыми данными схемы clinic
-- ============================================================================

-- На всякий случай явно переключаем контекст на нужную схему
SET search_path = clinic, public;

-- ----------------------------------------------------------------------------
--  1. Специфичные специальности (clinic.specialties)
-- ----------------------------------------------------------------------------
INSERT INTO clinic.specialties (code, name) VALUES
('therapy', 'Терапевт'),
('cardiology', 'Кардиолог'),
('ophthalmology', 'Офтальмолог'),
('neurology', 'Невролог')
ON CONFLICT (code) DO NOTHING;

-- ----------------------------------------------------------------------------
--  2. Врачи (clinic.doctors)
-- ----------------------------------------------------------------------------
-- Поля id генерируются автоматически (GENERATED ALWAYS AS IDENTITY),
-- поэтому вставляем без явного указания id.
INSERT INTO clinic.doctors (last_name, first_name, middle_name, is_active) VALUES
('Иванов', 'Иван', 'Иванович', true),
('Петрова', 'Анна', 'Сергеевна', true),
('Сидоров', 'Дмитрий', 'Петрович', true),
('Яковлева', 'Елена', 'Михайловна', true)
ON CONFLICT DO NOTHING;

-- ----------------------------------------------------------------------------
--  3. Врач <-> специальность (clinic.doctor_specialties)
--  Связываем врачей с их направлениями (используя подзапросы для надежности)
-- ----------------------------------------------------------------------------
INSERT INTO clinic.doctor_specialties (doctor_id, specialty_id)
VALUES
(
    (SELECT id FROM clinic.doctors WHERE last_name = 'Иванов' LIMIT 1),
    (SELECT id FROM clinic.specialties WHERE code = 'therapy' LIMIT 1)
),
(
    (SELECT id FROM clinic.doctors WHERE last_name = 'Петрова' LIMIT 1),
    (SELECT id FROM clinic.specialties WHERE code = 'cardiology' LIMIT 1)
),
(
    (SELECT id FROM clinic.doctors WHERE last_name = 'Сидоров' LIMIT 1),
    (SELECT id FROM clinic.specialties WHERE code = 'ophthalmology' LIMIT 1)
),
(
    (SELECT id FROM clinic.doctors WHERE last_name = 'Яковлева' LIMIT 1),
    (SELECT id FROM clinic.specialties WHERE code = 'neurology' LIMIT 1)
)
ON CONFLICT (doctor_id, specialty_id) DO NOTHING;

-- ----------------------------------------------------------------------------
--  4. Пациенты (clinic.patients)
-- ----------------------------------------------------------------------------
INSERT INTO clinic.patients (last_name, first_name, middle_name, birth_date, phone, telegram_id, telegram_username) VALUES
('Регистраторов', 'Егор', 'Сергеевич', '1995-08-24', '89117814619', 123456789, 'egor_reg'),
('Сидорова', 'Мария', 'Игоревна', '1989-11-12', '+79991234567', NULL, NULL),
('Петров', 'Алексей', 'Владимирович', '2001-03-05', NULL, 987654321, 'alex_petrov')
ON CONFLICT DO NOTHING;

-- ----------------------------------------------------------------------------
--  5. Генерация слотов (clinic.slots)
--  Используем вашу замечательную встроенную функцию generate_slots
--  для автоматического создания расписания врачам на ближайшие дни
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    v_doc_record RECORD;
BEGIN
    -- Для каждого активного врача генерируем расписание
    -- на ближайшие 3 рабочих дня (по умолчанию 30-минутные слоты с 09:00 до 17:00)
    FOR v_doc_record IN SELECT id FROM clinic.doctors WHERE is_active = true LOOP
        PERFORM clinic.generate_slots(
            p_doctor_id    := v_doc_record.id,
            p_from_date    := CURRENT_DATE,
            p_to_date      := CURRENT_DATE + INTERVAL '3 days',
            p_slot_minutes := 30,
            p_day_start    := '09:00'::time,
            p_day_end      := '17:00'::time
        );
    END LOOP;
END $$;