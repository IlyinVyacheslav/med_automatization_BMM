SET search_path = clinic, public;

INSERT INTO clinic.specialties (code, name)
VALUES ('therapy', 'Терапевт'),
       ('cardiology', 'Кардиолог'),
       ('ophthalmology', 'Офтальмолог'),
       ('neurology', 'Невролог')
ON CONFLICT (code) DO NOTHING;

INSERT INTO clinic.doctors (last_name, first_name, middle_name, is_active)
VALUES ('Иванов', 'Иван', 'Иванович', true),
       ('Петрова', 'Анна', 'Сергеевна', true),
       ('Сидоров', 'Дмитрий', 'Петрович', true),
       ('Яковлева', 'Елена', 'Михайловна', true)
ON CONFLICT DO NOTHING;

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

INSERT INTO clinic.patients (last_name, first_name, middle_name, birth_date, phone, telegram_id, telegram_username)
VALUES ('Регистраторов', 'Егор', 'Сергеевич', '1995-08-24', '89117814619', 123456789, 'egor_reg'),
       ('Сидорова', 'Мария', 'Игоревна', '1989-11-12', '+79991234567', NULL, NULL),
       ('Петров', 'Алексей', 'Владимирович', '2001-03-05', NULL, 987654321, 'alex_petrov')
ON CONFLICT DO NOTHING;

-- Для каждого врача создаём слоты с 09:00 до 17:00, интервал 30 минут.
-- Функция generate_slots пропускает выходные (пн–пт) и уже существующие слоты.
DO $$
DECLARE
    v_today   date := CURRENT_DATE;
    v_end     date := v_today + 7;
BEGIN
    PERFORM clinic.generate_slots((SELECT id FROM clinic.doctors WHERE last_name = 'Иванов' LIMIT 1), v_today, v_end, 30, '09:00', '17:00');
    PERFORM clinic.generate_slots((SELECT id FROM clinic.doctors WHERE last_name = 'Петрова' LIMIT 1), v_today, v_end, 30, '09:00', '17:00');
    PERFORM clinic.generate_slots((SELECT id FROM clinic.doctors WHERE last_name = 'Сидоров' LIMIT 1), v_today, v_end, 30, '09:00', '17:00');
    PERFORM clinic.generate_slots((SELECT id FROM clinic.doctors WHERE last_name = 'Яковлева' LIMIT 1), v_today, v_end, 30, '09:00', '17:00');
END;
$$;