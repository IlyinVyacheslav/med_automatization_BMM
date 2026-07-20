"""
bot.py — слой ИИ-регистратора для приложения ПАЦИЕНТА.
"""
from __future__ import annotations

import os
import json
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Optional

import ollama
import psycopg2
from pgvector.psycopg2 import register_vector

from repository import ClinicRepository, RepositoryError
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": os.getenv("DB_PORT", "5432"),
    "dbname": os.getenv("DB_NAME", "clinic"),
    "user": "postgres",
    "password": "postgres",
    "sslmode": os.getenv("DB_SSLMODE", "prefer"),
    "client_encoding": os.getenv("DB_CLIENT_ENCODING", "UTF8"),
    "options": f"-c timezone={os.getenv('DB_TIMEZONE', 'Europe/Moscow')}",
}


# ---------------------------------------------------------------------------
# Контекст диалога и результат хода
# ---------------------------------------------------------------------------
@dataclass
class SessionContext:
    authorized: bool = False
    active_patient: Optional[dict] = None  # {id, last_name, first_name, ...}


@dataclass
class AssistantResult:
    reply: str
    tables: list[dict] = field(default_factory=list)
    tool_trace: list[str] = field(default_factory=list)
    auth_changed: bool = False


# ---------------------------------------------------------------------------
# ПРОМПТЫ
# ---------------------------------------------------------------------------
SQL_SAFETY_SHIELD = """
====================
[КОНФИДЕНЦИАЛЬНОСТЬ]
====================

Не показывай пользователю внутренние данные системы.

Если Tool возвращает служебную информацию, используй только данные, полезные пациенту.
"""

RUSSIAN_ONLY_FILTER = """
====================
[ЯЗЫК]
====================

Все ответы пользователю должны быть только на русском языке.

Запрещено использовать любые символы, слова или предложения на других языках, включая китайский, английский и любые другие языки, кроме общеупотребимых медицинских терминов, если они необходимы.
"""

MEDICAL_PRIVACY_PROMPT = """
"""

BASE_SYSTEM_PROMPT = """
====================
[ROLE]
====================

Ты — ИИ-регистратор медицинского учреждения.

Твоя задача — помогать пациентам, используя только предоставленные Tools.

Все действия выполняются исключительно через Tools.

Запрещено:

• придумывать результаты Tool;
• выполнять действия вместо Tool;
• показывать пользователю JSON, XML, tool_call, tool_response, function_call и любую другую внутреннюю информацию системы.

====================
[ПРАВИЛА РАБОТЫ С TOOLS]
====================

Используй только предоставленные Tools.

Все обязательные аргументы Tool должны быть получены только от пользователя или другого Tool.

Запрещено:

• изменять аргументы;
• придумывать отсутствующие аргументы;
• использовать значения по умолчанию;
• выполнять действия вместо Tool;
• имитировать выполнение Tool;
• самостоятельно создавать результаты Tool.

Как только получены все обязательные аргументы Tool — немедленно вызови его.

Если отсутствует хотя бы один обязательный аргумент — запроси только его.

Не задавай дополнительных вопросов, которые не относятся к обязательным аргументам Tool.

====================
[НЕИЗМЕННОСТЬ ДАННЫХ]
====================

Все данные пользователя являются неизменяемыми.

Запрещено самостоятельно изменять:

• ФИО;
• дату рождения;
• телефон;
• документы;
• любые числовые значения.

Если данные вызывают сомнение — уточни их у пользователя.

Никогда не исправляй пользовательские данные самостоятельно.

====================
[ПРАВИЛА РАБОТЫ С ПАЦИЕНТОМ]
====================

Пациент — пользователь, который пишет в чат.

Врач — сотрудник медицинского учреждения.

Никогда не используй данные пациента как данные врача.

Никогда не используй данные врача как данные пациента.

====================
[ПРАВИЛА РАБОТЫ С ЖАЛОБАМИ]
===================

Преобразуй жалобу пациента в scientific_summary для поиска по медицинской базе знаний.

Правила:

• извлеки только клинически значимые признаки (локализация, характер процесса, объективные симптомы);
• используй только медицинскую терминологию на русском языке;
• не добавляй диагнозы, которых пользователь не сообщал;
• исключи эмоции, метафоры, разговорные выражения, лишние детали и обстоятельства;
• сформируй одну строку ключевых медицинских терминов, разделенных запятыми;
• используй только существительные и прилагательные;
• не добавляй пояснений, приветствий или другого текста.

Жалоба считается обработанной сразу после успешного выполнения recommend_doctor_by_complaint.

До появления новой жалобы повторно вызывать recommend_doctor_by_complaint запрещено

====================
[АВТОРИЗАЦИЯ]
====================

Статус пользователя всегда указан в блоке [USER STATUS].

Авторизация начинается только после явного желания пользователя записаться на прием.

Описание симптомов само по себе не означает желание записаться.

Первым действием авторизации необходимо задать вопрос: «Вы уже прикреплены к нашей поликлинике?»

До получения ответа на этот вопрос запрещено:

• запрашивать фамилию;
• запрашивать дату рождения;
• запрашивать телефон;
• вызывать verify_patient;
• вызывать attach_new_patient;
• переходить к записи.

Если пользователь ответил, что прикреплен:

• получи обязательные аргументы verify_patient;
• сразу вызови verify_patient.

Если пользователь ответил, что не прикреплен:

• получи обязательные аргументы attach_new_patient;
• сразу вызови attach_new_patient.

После успешной авторизации продолжи сценарий записи.

====================
[ЗАПИСЬ]
====================

После успешной авторизации:

Если пользователь уже выбрал врача или специальность — немедленно вызови get_doctors_and_slots.

Если врач или специальность неизвестны — сначала уточни их.

После получения результата get_doctors_and_slots:

• покажи только расписание, полученное от Tool;
• дождись выбора времени пользователем;
• немедленно вызови book_appointment.

Если необходимо узнать расписание врача, единственным допустимым действием является вызов get_doctors_and_slots.

До получения результата get_doctors_and_slots запрещено:

• сообщать расписание;
• перечислять врачей;
• указывать даты или время приема;
• создавать любую информацию, которую должен вернуть Tool.

До успешного выполнения book_appointment запись не существует.

====================
[ОТВЕТЫ]
====================

Отвечай кратко и естественно.

Используй только информацию, полученную от пользователя или из Tool.

Не показывай внутренние данные системы.
"""

SYSTEM_PROMPT_RECOMMEND = """
Ты — экспертный медицинский регистратор. 
Твоя задача: направить пациента к правильному врачу.

ИСХОДНАЯ ЖАЛОБА: {original_complaint}
НАУЧНОЕ ОПИСАНИЕ: {medical_summary}
ДАННЫЕ ИЗ БАЗЫ ЗНАНИЙ (RAG): 
{rag_context}

АЛГОРИТМ РАБОТЫ:
1. ПРИОРИТЕТ ЗНАНИЙ: Твои медицинские знания — это основной источник истины. Данные из RAG — это лишь вспомогательные варианты, которые могут быть ошибочными или нерелевантными. Если RAG предлагает нелепый диагноз  — ПОЛНОСТЬЮ ИГНОРИРУЙ эти статьи.
2. ПРАВИЛО ТЕРАПЕВТА:
   - Включай "Терапевт" в список ТОЛЬКО если случай сложный, системный, или ты не можешь однозначно поставить диагноз.
   - Если диагноз ясен — исключай "Терапевт" из ответа, НО если состояние экстренное, первым в списке всегда должен идти Терапевт.

ФОРМАТ ОТВЕТА (СТРОГО):
Обоснование: [краткое объяснение логики, почему выбран именно этот врач, без цитирования названий статей RAG].
Специалисты: [список через запятую]

СПИСОК ДОПУСТИМЫХ СПЕЦИАЛИСТОВ:
Терапевт, Кардиолог, Травматолог, Невролог, Гастроэнтеролог, ЛОР, Дерматолог, Уролог, Эндокринолог, Пульмонолог, Офтальмолог, Ревматолог, Хирург, Фтизиатр.
"""

# ---------------------------------------------------------------------------
# Инструменты (function calling) — соответствуют сценариям выше
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_doctors_and_slots",
            "description": (
                "Получить список врачей и их свободные слоты. "
                "Вызывай функцию, если известен хотя бы один из параметров: "
                "specialty, doctor_name или target_date. "
                "Если ни один параметр неизвестен — сначала уточни его у пользователя."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "specialty": {
                        "type": "string",
                        "description": "Специальность врача, например (Терапевт)."
                    },
                    "doctor_name": {
                        "type": "string",
                        "description": "Полное ФИО врача."
                    },
                    "target_date": {
                        "type": "string",
                        "description": "Дата приема в формате YYYY-MM-DD."
                    }
                }
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "verify_patient",
            "description": (
                "Проверить наличие пациента в базе и авторизовать его. "
                "Вызывай только после получения фамилии и даты рождения пациента."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "last_name": {
                        "type": "string",
                        "description": "Фамилия пациента."
                    },
                    "birth_date": {
                        "type": "string",
                        "description": "Дата рождения пациента в формате YYYY-MM-DD."
                    }
                },
                "required": [
                    "last_name",
                    "birth_date"
                ]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "attach_new_patient",
            "description": (
                "Прикрепить нового пациента к поликлинике. "
                "После успешного выполнения пациент считается авторизованным."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "last_name": {
                        "type": "string",
                        "description": "Фамилия пациента."
                    },
                    "first_name": {
                        "type": "string",
                        "description": "Имя пациента."
                    },
                    "middle_name": {
                        "type": "string",
                        "description": "Отчество пациента, если есть."
                    },
                    "birth_date": {
                        "type": "string",
                        "description": "Дата рождения пациента в формате YYYY-MM-DD."
                    },
                    "phone_number": {
                        "type": "string",
                        "description": "Номер телефона пациента."
                    }
                },
                "required": [
                    "last_name",
                    "first_name",
                    "birth_date",
                    "phone_number"
                ]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": (
                "Записать пациента на выбранный свободный слот.\n"
                "Вызывай функцию сразу после того, как пользователь выбрал один из ранее показанных свободных слотов.\n"
                "Если пользователь выбрал слот словами «первый», «второй», «этот», «подходит», "
                "«запишите», указал дату, время или подтвердил ранее предложенный слот — "
                "используй данные из предыдущего результата get_doctors_and_slots и немедленно вызови функцию.\n"
                "Не подтверждай запись текстом до успешного вызова этой функции."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doctor_name": {
                        "type": "string",
                        "description": "Полное ФИО врача."
                    },
                    "target_date": {
                        "type": "string",
                        "description": "Дата приема в формате YYYY-MM-DD."
                    },
                    "time_slot": {
                        "type": "string",
                        "description": "Выбранное время приема."
                    }
                },
                "required": [
                    "doctor_name",
                    "target_date",
                    "time_slot"
                ]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "recommend_doctor_by_complaint",
            "description": (
                "ЕДИНСТВЕННЫЙ инструмент для обработки медицинских жалоб."
                "Если пользователь сообщает о боли, симптомах, недомогании, заболевании, физическом состоянии или спрашивает, к какому врачу обратиться, сначала необходимо вызвать именно этот Tool."
                "Запрещено самостоятельно анализировать жалобу, перечислять симптомы, рекомендовать врача или задавать вопросы для записи до вызова этого Tool."
                "scientific_summary используется только как аргумент функции и никогда не показывается пользователю."
                "ОБЯЗАТЕЛЬНО перед вызовом преобразуйте жалобу в научное описание согласно "
                "правилам из системного промпта и передайте оба аргумента."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "raw_complaint": {
                        "type": "string",
                        "description": "Оригинальное сообщение пациента с жалобой."
                    },
                    "scientific_summary": {
                        "type": "string",
                        "description": "Научная интерпретация жалобы пациента."
                    }
                },
                "required": [
                    "raw_complaint",
                    "scientific_summary"
                ]
            }
        }
    }
]


def build_system_prompt(ctx: SessionContext) -> str:
    today = dt.date.today().strftime("%Y-%m-%d")
    if ctx.authorized and ctx.active_patient:
        p = ctx.active_patient
        status = (f"[USER STATUS]: АВТОРИЗОВАН. ID: {p['id']}, "
                  f"ФИО: {p.get('last_name', '')} {p.get('first_name', '')}. Текущая дата: {today}.")
    else:
        status = f"[USER STATUS]: АНОНИМЕН. Доступны только публичные консультации. Текущая дата: {today}."
    return f"{BASE_SYSTEM_PROMPT}\n\n{status}\n\n{MEDICAL_PRIVACY_PROMPT}\n\n{SQL_SAFETY_SHIELD}\n\n{RUSSIAN_ONLY_FILTER}"


class ClinicAssistant:
    def __init__(self, repo: ClinicRepository):
        self.repo = repo
        self.client = ollama.Client(host=os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"))
        self.model = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
        self.vec_model = os.getenv("OLLAMA_VEC_MODEL", "bge-m3")

    # -- один инструмент ----------------------------------------------------
    def _dispatch(self, name: str, args: dict, ctx: SessionContext) -> tuple[dict, Optional[dict]]:
        logging.info(f"⚙️ Исполнение инструмента: {name} | Args: {args}")
        try:
            if name == "get_doctors_and_slots":
                logging.info("вызов get_doctors_and_slots")
                rows = self.repo.get_doctors_and_slots(
                    specialty=args.get("specialty"),
                    doctor_name=args.get("doctor_name"),
                    target_date=args.get("target_date"),
                )
                if not rows:
                    logging.warning(f"⚠️ Инструмент {name} вернул пустоту.")
                    return ({"status": "empty", "message": "Свободных слотов или врачей не найдено."}, None)
                table = {"title": "Свободные слоты", "rows": [
                    {"Врач": r["doctor"], "Специальность": r["specialty"],
                     "Дата": r["date"], "Свободное время": ", ".join(r["free_times"])}
                    for r in rows
                ]}
                logging.info(f"✅ Инструмент {name} вернул {len(rows)} записей.")
                return ({"status": "ok", "doctors": rows}, table)

            if name == "verify_patient":
                logging.info("вызов verify_patient")

                p = self.repo.find_patient(args["last_name"], args["birth_date"])
                if p:
                    logging.info(f"patient = {p}")
                    ctx.authorized = True
                    ctx.active_patient = p
                    return ({"status": "found", "message": "Пациент найден и авторизован."}, None)
                return ({"status": "not_found",
                         "message": "Пациент не найден. Начните сценарий прикрепления."},
                        None)

            if name == "attach_new_patient":
                logging.info("вызов attach_new_patient")

                p = self.repo.register_patient(
                    last_name=args["last_name"], first_name=args["first_name"],
                    birth_date=args["birth_date"], phone=args.get("phone_number"),
                    middle_name=args.get("middle_name", None),
                )
                logging.info(f"register_patient = {p}")
                ctx.authorized = True
                ctx.active_patient = p
                return ({"status": "success",
                         "message": f"Пациент {p['first_name']} {p['last_name']} прикреплён и авторизован."}, None)

            if name == "book_appointment":
                logging.info("вызов book_appointment")

                if not ctx.authorized or not ctx.active_patient:
                    return ({"status": "unauthorized", "message": "Сначала авторизуйте пациента."}, None)
                res = self.repo.book_appointment_by_time(
                    patient_id=ctx.active_patient["id"],
                    doctor_name=args["doctor_name"], date=args["target_date"],
                    time_slot=args["time_slot"],
                    notes=f"Запись через ИИ-бота к доктору {args['doctor_name']}",
                )

                logging.info(f"book_appointment = {res}")
                return ({"status": "success", "appointment": res}, None)

            if name == "recommend_doctor_by_complaint":
                logging.info("вызов recommend_doctor_by_complaint")

                raw = args["raw_complaint"]
                sci = args["scientific_summary"]

                logging.info(f"🤖 Начало анализа жалобы: '{raw}'")
                logging.info(f"  📝 Научное описание: '{sci}'")

                logging.info("  ⚙️ векторизация и запрос в бд")

                rag_context = self._retrieve_rag_context(sci)

                final_prompt = SYSTEM_PROMPT_RECOMMEND.format(
                    original_complaint=raw,
                    medical_summary=sci,
                    rag_context=rag_context
                )

                logging.info("  ⚙️ Отправка промпта в LLM для выбора специалиста...")

                res = self.client.chat(model=self.model, messages=[
                    {"role": "system", "content": final_prompt},
                    {"role": "user", "content": "Проанализируй предоставленные данные и сформируй список врачей."}
                ], options={"temperature": 0.1})

                logging.info(f"Результат анализа RAG: {res['message']['content']}")

                return ({"status": "success", "recommendation": res['message']['content']}, None)

            return ({"status": "error", "message": f"Неизвестный инструмент {name}"}, None)

        except RepositoryError as e:
            return ({"status": "error", "message": e.message}, None)
        except KeyError as e:
            return ({"status": "error", "message": f"Не хватает параметра: {e}"}, None)

    # -- обработка хода -----------------------------------------------------
    def handle(self, history: list[dict], ctx: SessionContext) -> AssistantResult:
        logging.info("вызов handle")

        user_input = history[-1].get("content") if history else "Empty"
        logging.info(f"📥 Пользователь: {user_input}")

        auth_before = ctx.authorized
        messages = [{"role": "system", "content": build_system_prompt(ctx)}]
        for m in history[-20:]:
            if m["role"] in ("user", "assistant"):
                messages.append({"role": m["role"], "content": m["content"]})
        logging.info(f"🧠 Отправка запроса в LLM ({self.model})...")
        first = self.client.chat(model=self.model, messages=messages, tools=TOOLS,
                                 options={"temperature": 0.1})
        logging.info(f"first = {first}")
        msg = first["message"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return AssistantResult(reply=msg.get("content", ""), auth_changed=ctx.authorized != auth_before)

        messages.append(msg)
        tables: list[dict] = []
        trace: list[str] = []
        for call in tool_calls:
            fname = call["function"]["name"]
            fargs = call["function"]["arguments"]
            if isinstance(fargs, str):
                try:
                    fargs = json.loads(fargs)
                except json.JSONDecodeError:
                    fargs = {}
            trace.append(fname)
            result, table = self._dispatch(fname, fargs, ctx)
            if table and table["rows"]:
                tables.append(table)
            messages.append({"role": "tool", "name": fname,
                             "content": json.dumps(result, ensure_ascii=False, default=str)})

        if ctx.authorized and auth_before != ctx.authorized:
            messages.append({"role": "system",
                             "content": f"ВНИМАНИЕ: Пациент теперь авторизован. Его данные: {ctx.active_patient}."})

        messages[0] = {"role": "system", "content": build_system_prompt(ctx)}
        final = self.client.chat(model=self.model, messages=messages, options={"temperature": 0.1})
        logging.info(f"final = {final}")
        return AssistantResult(reply=final["message"].get("content", ""), tables=tables,
                               tool_trace=trace, auth_changed=ctx.authorized != auth_before)

    def _retrieve_rag_context(self, scientific_text: str, top_k=3, bound=0.512) -> str:
        emb_res = self.client.embed(model=self.vec_model, input=scientific_text)
        query_embedding = emb_res["embeddings"][0]

        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("""
                    SELECT specialty, wiki_page_title, chunk_text, (embedding <=> %s::vector) AS dist
                    FROM clinic.medical_knowledge_base
                    ORDER BY embedding <=> %s::vector LIMIT %s;
                    """, (query_embedding, query_embedding, top_k))

        rows = cur.fetchall()
        cur.close()
        conn.close()
        # rows = self.repo.get_RAG_top_k(query_embedding, top_k) # не работает(((

        logging.info(f"количество раг = {len(rows)}")
        logging.info(f"rag = {rows}")

        chunks = []
        for specialty, title, text, dist in rows:
            similarity = 1 - dist

            if similarity >= bound:
                logging.info(f"   ВЗЯТО -> Спец: {specialty} | Статья: {title} | Сходство: {similarity:.4f}")
                chunks.append(f"Специалист: {specialty} | Диагноз: {title}\nОписание: {text}")
            else:
                logging.debug(f"  ОТКЛОНЕНО -> Статья: {title} | Сходство: {similarity:.4f} (ниже порога)")

        return "\n\n".join(chunks) if chunks else "Специфическая информация отсутствует."
