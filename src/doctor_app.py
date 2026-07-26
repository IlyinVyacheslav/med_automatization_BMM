"""
doctor_app.py — панель врача/регистратора.
"""

import os
from datetime import date, datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from repository import ClinicRepository, RepositoryError

load_dotenv()

st.set_page_config(page_title="Кабинет врача — ГАТЧИНСКАЯ КМБ", page_icon="🩺", layout="wide")
st.title("🩺 Панель управления расписанием (Врач / Регистратор)")


@st.cache_resource(show_spinner=False)
def get_repo() -> ClinicRepository:
    # роль registrar: логин совпадает с именем роли, пароль — REGISTRAR_PASSWORD
    return ClinicRepository(
        user="registrar",
        password=os.getenv("REGISTRAR_PASSWORD"),
    )


try:
    repo = get_repo()
    doctors = repo.list_active_doctors()
except RepositoryError as e:
    st.error(f"Не удалось подключиться к базе данных: {e.message}")
    st.stop()

if not doctors:
    st.warning("В базе нет активных врачей.")
    st.stop()

doc_dict = {d["full_name"]: d["id"] for d in doctors}
selected_doc_name = st.sidebar.selectbox("Выберите врача:", list(doc_dict.keys()))
selected_doc_id = doc_dict[selected_doc_name]
with st.sidebar:
    try:
        info = repo.healthcheck()
        st.caption(f"БД: `{info['db']}` · роль: `{info['session_user']}`")
    except RepositoryError:
        pass

# ---------------------------------------------------------------------------
# 1. Расписание выбранного врача
# ---------------------------------------------------------------------------
st.subheader(f"📅 Записи на прием к специалисту: {selected_doc_name}")
try:
    rows = repo.get_doctor_schedule(selected_doc_id)
    if rows:
        df = pd.DataFrame([{
            "ID записи": r["appointment_id"],
            "Дата": r["starts_at"].strftime("%Y-%m-%d"),
            "Время": r["starts_at"].strftime("%H:%M"),
            "Пациент": r["patient"],
            "Телефон": r["phone"] or "",
            "Специальность": r["specialty"],
            "Статус": r["status"],
        } for r in rows])
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("Нет записей к данному врачу.")
except RepositoryError as e:
    st.error(f"Ошибка получения расписания: {e.message}")

# ---------------------------------------------------------------------------
# 2. Создать новую запись (в т.ч. вне сетки расписания)
# ---------------------------------------------------------------------------
st.markdown("---")
st.subheader("➕ Создать новую запись на прием")

st.caption("Сначала найдите пациента (по фамилии и дате рождения) или укажите его ID. "
           "Регистрация нового пациента — во вкладке ниже.")

pcol1, pcol2, pcol3 = st.columns([2, 2, 1])
with pcol1:
    p_last = st.text_input("Фамилия пациента:")
with pcol2:
    p_birth = st.text_input("Дата рождения (ГГГГ-ММ-ДД):", placeholder="1990-05-15")
with pcol3:
    st.write("")
    st.write("")
    do_find = st.button("Найти пациента")

if "found_patient" not in st.session_state:
    st.session_state.found_patient = None

if do_find:
    try:
        p = repo.find_patient(p_last, p_birth) if p_last and p_birth else None
        if p:
            st.session_state.found_patient = p
            st.success(f"Найден пациент №{p['id']}: {p['last_name']} {p['first_name']}")
        else:
            st.session_state.found_patient = None
            st.warning("Пациент не найден. Проверьте данные или зарегистрируйте нового ниже.")
    except RepositoryError as e:
        st.error(e.message)

with st.expander("Зарегистрировать нового пациента"):
    rc1, rc2, rc3, rc4 = st.columns(4)
    with rc1:
        n_last = st.text_input("Фамилия", key="reg_last")
    with rc2:
        n_first = st.text_input("Имя", key="reg_first")
    with rc3:
        n_birth = st.text_input("Дата рождения", key="reg_birth", placeholder="1990-05-15")
    with rc4:
        n_phone = st.text_input("Телефон", key="reg_phone", placeholder="+79001234567")
    if st.button("Зарегистрировать"):
        try:
            p = repo.register_patient(n_last, n_first, n_birth, phone=n_phone or None)
            st.session_state.found_patient = p
            st.success(f"Пациент зарегистрирован: №{p['id']} {p['last_name']} {p['first_name']}")
        except RepositoryError as e:
            st.error(e.message)

fp = st.session_state.found_patient
default_pid = fp["id"] if fp else 0

b_col1, b_col2, b_col3 = st.columns(3)
with b_col1:
    patient_id = st.number_input("ID пациента:", min_value=0, step=1, value=int(default_pid))
with b_col2:
    booking_date = st.date_input("Дата приёма:", value=date.today())
with b_col3:
    booking_time = st.time_input("Время приёма:")

reason = st.text_input("Причина/жалоба (необязательно):", key="adhoc_notes")

if st.button("Записать пациента", type="primary"):
    if not patient_id:
        st.warning("Укажите ID пациента (найдите или зарегистрируйте его выше).")
    else:
        try:
            starts_at = datetime.combine(booking_date, booking_time)
            res = repo.book_adhoc(
                patient_id=int(patient_id), doctor_id=selected_doc_id,
                starts_at=starts_at, notes=reason or None,
            )
            st.success(
                f"Запись №{res['appointment_id']} создана: {res['doctor']} "
                f"({res['specialty']}) — {res['starts_at'].strftime('%Y-%m-%d %H:%M')}.")
            st.rerun()
        except RepositoryError as e:
            st.error(f"Не удалось создать запись: {e.message}")

# ---------------------------------------------------------------------------
# 3. Отмена приёма
# ---------------------------------------------------------------------------
st.markdown("---")
st.subheader("❌ Отмена приема")
c1, c2 = st.columns(2)
with c1:
    appt_to_cancel = st.number_input("ID записи для отмены:", min_value=1, step=1)
with c2:
    cancel_reason = st.text_input("Причина отмены:")

if st.button("Отменить запись"):
    if not cancel_reason:
        st.warning("Пожалуйста, укажите причину отмены.")
    else:
        try:
            repo.cancel_appointment(int(appt_to_cancel), cancel_reason)
            st.success(f"Запись №{appt_to_cancel} успешно отменена.")
            st.rerun()
        except RepositoryError as e:
            st.error(f"Ошибка отмены: {e.message}")