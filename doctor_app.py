import streamlit as st
import psycopg2
import pandas as pd
import os
from datetime import datetime, date

st.set_page_config(page_title="Кабинет Врача Гатчинской КМБ", page_icon="🩺", layout="wide")
st.title("🩺 Панель управления расписанием (Врач / Регистратор)")


# Подключение под ролью registrar (для просмотра расписания, отмены и создания записей)
def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        database=os.getenv("DB_NAME", "clinic_bot_db"),
        user="registrar",
        password=os.getenv("REGISTRAR_PASSWORD", "registrar"),
        port=os.getenv("DB_PORT", "5432")
    )


# 1. Загрузка списка врачей для фильтрации
try:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, full_name FROM clinic.doctors WHERE is_active = true ORDER BY full_name")
    doctors = cur.fetchall()
    cur.close()
    conn.close()

    doc_dict = {doc[1]: doc[0] for doc in doctors}
    selected_doc_name = st.sidebar.selectbox("Выберите врача:", list(doc_dict.keys()))
    selected_doc_id = doc_dict[selected_doc_name]
except Exception as e:
    st.error(f"Не удалось подключиться к базе данных: {e}")
    st.stop()

# 2. Вывод расписания выбранного врача
st.subheader(f"📅 Записи на прием к специалисту: {selected_doc_name}")

conn = get_db_connection()
query = """
        SELECT a.id                               as "ID Записи",
               sl.starts_at::date as "Дата", TO_CHAR(sl.starts_at, 'HH24:MI') as "Время",
               p.last_name || ' ' || p.first_name as "Пациент",
               p.phone                            as "Телефон",
               a.status                           as "Статус"
        FROM clinic.appointments a
                 JOIN clinic.slots sl ON a.slot_id = sl.id
                 JOIN clinic.patients p ON a.patient_id = p.id
        WHERE sl.doctor_id = %s
        ORDER BY sl.starts_at;
        """
try:
    df = pd.read_sql_query(query, conn, params=(selected_doc_id,))
    if not df.empty:
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("Нет активных записей к данному врачу.")
except Exception as e:
    st.error(f"Ошибка получения расписания: {e}")
finally:
    conn.close()

# 3. ИЗМЕНЕННЫЙ БЛОК: Принудительное создание новой записи (новая строка в БД)
st.markdown("---")
st.subheader("➕ Создать новую запись на прием (В обход сетки расписания)")

b_col1, b_col2, b_col3 = st.columns(3)

with b_col1:
    # Заменили выбор из списка на ручной ввод СНИЛС
    patient_snils = st.text_input("СНИЛС пациента:", placeholder="Введите СНИЛС или ID пациента")

with b_col2:
    # Позволяем врачу выбрать абсолютно любой день
    booking_date = st.date_input("Выберите дату приема:", value=date.today())

with b_col3:
    # Позволяем указать абсолютно любое время
    booking_time = st.time_input("Выберите время приема:")

if st.button("Записать пациента", type="primary"):
    if not patient_snils.strip():
        st.warning("Пожалуйста, укажите СНИЛС пациента для оформления записи.")
    else:
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # 1. Получаем специальность врача для вставки в appointments
            cur.execute("SELECT specialty_id FROM clinic.doctor_specialties WHERE doctor_id = %s LIMIT 1",
                        (selected_doc_id,))
            spec_row = cur.fetchone()
            specialty_id = spec_row[0] if spec_row else None

            if not specialty_id:
                st.error("Критическая ошибка: у данного врача в базе данных не указана специальность.")
            else:
                # Объединяем выбранную дату и время в формат TIMESTAMP
                starts_at = datetime.combine(booking_date, booking_time)

                # 2. Генерируем новую СТРОКУ (слот) в таблице clinic.slots.
                # Ставим is_available = false, так как этот слот создается сразу под запись.
                cur.execute("""
                            INSERT INTO clinic.slots (doctor_id, starts_at, is_available)
                            VALUES (%s, %s, false) RETURNING id;
                            """, (selected_doc_id, starts_at))
                new_slot_id = cur.fetchone()[0]

                # 3. Генерируем новую СТРОКУ (запись) в таблице clinic.appointments.
                # Подставляем ручной ввод СНИЛС напрямую в поле связи с пациентом.
                cur.execute("""
                            INSERT INTO clinic.appointments (patient_id, slot_id, specialty_id, status)
                            VALUES (%s, %s, %s, 'scheduled');
                            """, (patient_snils.strip(), new_slot_id, specialty_id))

                conn.commit()
                st.success(
                    f"Новая строка успешно создана! Пациент со СНИЛС '{patient_snils}' записан на {booking_date} в {booking_time}.")

                cur.close()
                conn.close()
                st.rerun()

        except Exception as e:
            if 'conn' in locals(): conn.rollback()
            st.error(f"Ошибка при принудительной вставке записи в БД: {e}")

# 4. Инструмент отмены записи
st.markdown("---")
st.subheader("❌ Отмена приема")
col1, col2 = col1, col2 = st.columns(2)

with col1:
    appt_to_cancel = st.number_input("Укажите ID Записи для отмены:", min_value=1, step=1)
with col2:
    cancel_reason = st.text_input("Укажите причину отмены:")

if st.button("Отменить запись"):
    if not cancel_reason:
        st.warning("Пожалуйста, укажите причину отмены.")
    else:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT clinic.cancel_appointment(%s, %s);", (appt_to_cancel, cancel_reason))
            conn.commit()
            st.success(f"Запись №{appt_to_cancel} успешно отменена.")
            cur.close()
            conn.close()
            st.rerun()
        except Exception as e:
            st.error(f"Ошибка выполнения транзакции: {e}")