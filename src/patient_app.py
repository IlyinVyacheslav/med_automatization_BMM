"""
patient_app.py — Streamlit-интерфейс ИИ-регистратора для ПАЦИЕНТА.

Тонкий слой: подключение под ролью ai_bot_registrar (из .env), чат с ботом.
Вся логика БД — в repository.py, вся логика ИИ — в bot.py.
"""
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from repository import ClinicRepository, RepositoryError
from bot import ClinicAssistant, SessionContext

load_dotenv()

st.set_page_config(page_title="ИИ-Регистратор ГАТЧИНСКАЯ КМБ", page_icon="🏥")


@st.cache_resource(show_spinner=False)
def get_repo() -> ClinicRepository:
    return ClinicRepository(
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )


@st.cache_resource(show_spinner=False)
def get_assistant() -> ClinicAssistant:
    return ClinicAssistant(get_repo())


if "ctx" not in st.session_state:
    st.session_state.ctx = SessionContext()
if "messages" not in st.session_state:
    st.session_state.messages = [{
        "role": "assistant",
        "content": "Здравствуйте! Я ИИ-регистратор ГБУЗ ЛО «ГАТЧИНСКАЯ КМБ». Могу подсказать "
                   "нужного врача по вашим симптомам, найти специалиста или записать вас на прием. "
                   "Чем могу помочь?",
        "tables": [],
    }]

st.title("🏥 Локальный ИИ-Регистратор Клиники")

with st.sidebar:
    st.header("Статус сессии")
    try:
        info = get_repo().healthcheck()
        st.caption(f"БД: `{info['db']}` · роль: `{info['session_user']}`")
    except RepositoryError as e:
        st.error(e.message)

    ctx: SessionContext = st.session_state.ctx
    if ctx.authorized and ctx.active_patient:
        p = ctx.active_patient
        st.success(f"🔐 Пациент: {p.get('first_name','')} {p.get('last_name','')}")
        if st.button("Выйти"):
            st.session_state.ctx = SessionContext()
            st.rerun()
    else:
        st.warning("🔒 Анонимный режим")


def render_tables(tables):
    for t in tables or []:
        if t.get("rows"):
            st.caption(t.get("title", ""))
            st.dataframe(pd.DataFrame(t["rows"]), hide_index=True, use_container_width=True)


for msg in st.session_state.messages:
    if msg["role"] == "system":
        continue
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        render_tables(msg.get("tables"))


if user_input := st.chat_input("Ваш запрос..."):
    st.session_state.messages.append({"role": "user", "content": user_input, "tables": []})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Анализ запроса..."):
            try:
                result = get_assistant().handle(st.session_state.messages, st.session_state.ctx)
                if result.tool_trace:
                    st.caption("⚙️ " + ", ".join(f"`{t}`" for t in result.tool_trace))
                st.markdown(result.reply or "…")
                render_tables(result.tables)
                st.session_state.messages.append(
                    {"role": "assistant", "content": result.reply, "tables": result.tables})
                if result.auth_changed:
                    st.rerun()
            except Exception as e:
                st.error(f"Ошибка выполнения: {e}")