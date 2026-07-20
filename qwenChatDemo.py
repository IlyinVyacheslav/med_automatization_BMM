# -*- coding: utf-8 -*-
import os
import json
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
import ollama

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("MedAssistantConsole")

MODEL_NAME = "qwen2.5:7b"

DB_CONFIG = {
    "dbname": "qwenTest",
    "user": "postgres",
    "password": "admin",
    "host": "127.0.0.1",
    "port": "5432"
}

CLASSIFIER_SYSTEM_PROMPT = """
Ты - опытный медицинский регистратор.
Твоя задача - определить, к какому врачу пациенту следует обратиться ПЕРВЫМ по описанной жалобе.

Используй ТОЛЬКО следующие специальности:
Терапевт
Кардиолог
Травматолог
Невролог
Гастроэнтеролог
ЛОР
Дерматолог

Другие специальности использовать запрещено.

Перед тем как выбрать ответ, мысленно выполни следующий алгоритм:
1. Проанализируй все симптомы, описанные пациентом.
2. Определи, сколько специальностей из списка могут разумно объяснить данную жалобу.
3. Если жалоба явно относится только к одной специальности - верни только её.
4. Если жалоба может относиться сразу к нескольким специальностям, первым всегда укажи Терапевта, а затем перечисли остальные подходящие специальности через запятую в порядке убывания вероятности.
5. Не пытайся угадать диагноз.
6. Если информации недостаточно, верни только Терапевта.

Формат ответа:
- Используй только специальности из приведённого списка.
- Ответ должен содержать только названия специальностей.
- Если специальностей несколько, разделяй их запятыми.
- Не добавляй никаких пояснений, комментариев или знаков препинания в конце ответа.
"""


def classify_complaint(complaint_text: str) -> str:
    logger.info(f"[Классификатор] Анализ жалобы: '{complaint_text}'")
    try:
        response = ollama.chat(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user",
                 "content": f"Жалоба: '{complaint_text}'. Назови специальность врача одним словом на русском:"}
            ],
            options={"temperature": 0.1}
        )
        doctor = response['message']['content'].strip()

        for char in [".", ",", "!", "?"]:
            if doctor.endswith(char):
                doctor = doctor[:-1]

        recommended = doctor.capitalize()
        logger.info(f"[Классификатор] Рекомендация: {recommended}")
        return json.dumps({"recommended_specialty": recommended}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[Классификатор] Ошибка при классификации: {e}")
        return json.dumps({"recommended_specialty": "Терапевт"}, ensure_ascii=False)


def get_free_slots(doctor_job_or_name: str) -> str:
    logger.info(f"[БД] Вызов get_free_slots для: '{doctor_job_or_name}'")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        query = """ \
                SELECT d.name as doctor_name, d.job, s.time \
                FROM slots s \
                         JOIN doctor d ON s.doctorid = d.id \
                WHERE s.isbooked = FALSE \
                  AND (LOWER(d.job) LIKE LOWER(%s) OR LOWER(d.name) LIKE LOWER(%s)) \
                ORDER BY s.time ASC;
        """
        search_param = f"%{doctor_job_or_name}%"
        cursor.execute(query, (search_param, search_param))
        rows = cursor.fetchall()

        cursor.close()
        conn.close()

        if not rows:
            return json.dumps({"message": f"Свободных слотов для '{doctor_job_or_name}' не найдено."},
                              ensure_ascii=False)

        for row in rows:
            row['time'] = row['time'].strftime('%Y-%m-%d %H:%M')

        return json.dumps(rows, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[БД] Ошибка в get_free_slots: {e}", exc_info=True)
        return json.dumps({"error": f"Ошибка БД: {str(e)}"}, ensure_ascii=False)


def book_appointment(doctor_job_or_name: str, patient_name: str, date_time_str: str) -> str:
    logger.info(
        f"[БД] Вызов book_appointment. Врач: '{doctor_job_or_name}', Пациент: '{patient_name}', Время: '{date_time_str}'")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM patient WHERE LOWER(name) = LOWER(%s);", (patient_name,))
        patient_row = cursor.fetchone()
        if patient_row:
            patient_id = patient_row[0]
        else:
            cursor.execute("INSERT INTO patient (name) VALUES (%s) RETURNING id;", (patient_name,))
            patient_id = cursor.fetchone()[0]

        search_param = f"%{doctor_job_or_name}%"
        cursor.execute(
            "SELECT id, name FROM doctor WHERE LOWER(job) LIKE LOWER(%s) OR LOWER(name) LIKE LOWER(%s) LIMIT 1;",
            (search_param, search_param)
        )
        doctor_row = cursor.fetchone()
        if not doctor_row:
            cursor.close()
            conn.close()
            return json.dumps({"error": f"Врач или специальность '{doctor_job_or_name}' не найдены."},
                              ensure_ascii=False)

        doctor_id, doctor_real_name = doctor_row

        cursor.execute(
            "SELECT isbooked FROM slots WHERE doctorid = %s AND time = %s::timestamp;",
            (doctor_id, date_time_str)
        )
        slot_row = cursor.fetchone()
        if not slot_row:
            cursor.close()
            conn.close()
            return json.dumps({"error": f"Слот на {date_time_str} не существует в расписании."}, ensure_ascii=False)

        if slot_row[0] is True:
            cursor.close()
            conn.close()
            return json.dumps({"error": f"Слот на {date_time_str} уже занят."}, ensure_ascii=False)

        cursor.execute(
            "UPDATE slots SET isbooked = TRUE, patientid = %s WHERE doctorid = %s AND time = %s::timestamp;",
            (patient_id, doctor_id, date_time_str)
        )
        conn.commit()
        cursor.close()
        conn.close()

        return json.dumps({
            "status": "success",
            "message": f"Пациент {patient_name} успешно записан к врачу {doctor_real_name} на {date_time_str}."
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[БД] Ошибка в book_appointment: {e}", exc_info=True)
        return json.dumps({"error": f"Ошибка транзакции: {str(e)}"}, ensure_ascii=False)


available_functions = {
    "get_free_slots": get_free_slots,
    "book_appointment": book_appointment,
    "classify_complaint": classify_complaint
}

tools = [
    {
        'type': 'function',
        'function': {
            'name': 'get_free_slots',
            'description': 'Позволяет получить список свободных слотов у врача по его специальности или фамилии.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'doctor_job_or_name': {
                        'type': 'string',
                        'description': 'Специальность врача (например, "Кардиолог") или его фамилия.',
                    },
                },
                'required': ['doctor_job_or_name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'book_appointment',
            'description': 'Записывает пациента к врачу на выбранную дату и время.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'doctor_job_or_name': {
                        'type': 'string',
                        'description': 'Специальность или имя врача.',
                    },
                    'patient_name': {
                        'type': 'string',
                        'description': 'ФИО пациента.',
                    },
                    'date_time_str': {
                        'type': 'string',
                        'description': 'Время приема в формате "YYYY-MM-DD HH:MM:SS".',
                    },
                },
                'required': ['doctor_job_or_name', 'patient_name', 'date_time_str'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'classify_complaint',
            'description': 'Анализирует жалобы пациента на здоровье и определяет, к какому врачу (из списка разрешенных) ему стоит обратиться.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'complaint_text': {
                        'type': 'string',
                        'description': 'Текст жалобы пациента на его симптомы или самочувствие. Например: "У меня сильно болит ухо и заложен нос".',
                    },
                },
                'required': ['complaint_text'],
            },
        },
    }
]

SYSTEM_PROMPT = """
Ты — вежливый ИИ-ассистент в регистратуре клиники. Твоя задача — помогать пациентам строго на основе реальных данных и прописанных правил.

ГЛАВНЫЕ ПРАВИЛА:
1. Тебе КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдумывать факты. Всю информацию о врачах и времени приема ты обязан получать ТОЛЬКО из результатов работы функций.
2. Ты медицинский бот-регистратор. В твою компетенцию входят только задачи, связанные с жалобами на здоровье или с записью к врачу. На любые другие темы (математика, программирование, посторонние вопросы) вежливо отказывай.
3. Ты существуешь для работы с людьми. Если жалоба на здоровье поступает не от человека, а от животного, мифического существа или механизма, вежливо откажи в обслуживании.
4. Ты должен отвечать СТРОГО на русском языке, вежливо и лаконично.
5. Тебе КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдумывать ФИО пациента (например, "Иванов Иван Иванович"). Спрашивай ФИО только при подтверждении бронирования конкретного времени. 
6. Если пациент пишет бред, не относящийся к здоровью, не запоминай его и вежливо возвращай к теме записи.
7. Если пациент просит записать его к конкретному специалисту (например, "хочу к терапевту"), тебе ЗАПРЕЩЕНО расспрашивать его о симптомах или просить жалобы. СРАЗУ вызывай функцию `get_free_slots` для этой специальности.
8. Ты должен строго следовать формату вывода данных.

9. ТЕБЕ КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО самостоятельно выдумывать свободные слоты, даты или время приема. Расписание ты обязан получать ТОЛЬКО через вызов функции `get_free_slots`.
10. Если пациент описывает свои симптомы ("болит голова", "кашель"), ты должен ОБЯЗАТЕЛЬНО вызвать функцию `classify_complaint`.
11. Как только ты узнал специальность врача, ты обязан СРАЗУ ЖЕ вызвать функцию `get_free_slots` для этой специальности, чтобы узнать реальное расписание в БД. НЕ спрашивай у пользователя ФИО на этом этапе! Просто молча вызывай функцию и показывай свободные слоты.
12. ПРОЦЕДУРА ЗАПИСИ (СТРОГИЙ ДВУХШАГОВЫЙ АЛГОРИТМ):
    - Шаг 1 (Запрос имени): Когда пациент выбрал конкретную дату и время из списка, ты обязан СНАЧАЛА спросить в чате его ФИО. На этом шаге вызывать функцию `book_appointment` тебе КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО, так как у тебя еще нет имени!
    - Шаг 2 (Вызов функции): Только после того, как пациент в ответ написал свое ФИО, ты обязан МГНОВЕННО вызвать функцию `book_appointment` для фиксации записи в БД. Использовать вымышленные имена или пропускать этот шаг строго запрещено.
    Для вызова функции `book_appointment` тебе необходимы: специальность врача, реальное ФИО пациента из чата и выбранное время в формате "YYYY-MM-DD HH:MM:00".

ФОРМАТ ВЫВОДА ДАННЫХ В ДИАЛОГ (НАРУШЕНИЕ СТРОГО ЗАПРЕЩЕНО):
1. ФОРМАТ ВРЕМЕНИ В ЧАТЕ: В ответах пользователю пиши даты и время строго в числовом формате "ГГГГ-ММ-ДД ЧЧ:ММ" (например: 2026-07-16 10:00). Писать месяцы словами (например, "июля") или дни недели ("четверг") категорически запрещено. Выводи свободные слоты простым списком.
"""


def clean_model_output(text: str) -> str:
    if not text:
        return ""
    for word in ["olicit", "</tool_call>", "<tool_call>"]:
        text = text.replace(word, "")
    return text.strip()


def ask_assistant(user_message: str, chat_history):
    chat_history.append({"role": "user", "content": user_message})

    try:
        max_iterations = 5
        iteration = 0

        while iteration < max_iterations:
            response = ollama.chat(
                model=MODEL_NAME,
                messages=chat_history,
                tools=tools,
                options={"temperature": 0.0}
            )

            if response.message.tool_calls:
                chat_history.append(response.message)

                for tool in response.message.tool_calls:
                    func_name = tool.function.name
                    func_args = tool.function.arguments

                    if func_name in available_functions:
                        db_func = available_functions[func_name]

                        result_str = db_func(**func_args)

                        chat_history.append({
                            'role': 'tool',
                            'content': result_str,
                        })

                iteration += 1
                continue

            else:
                final_text = clean_model_output(response.message.content)
                chat_history.append(response.message)
                return final_text, chat_history

        return "Прошу прощения, система выполнила слишком много внутренних запросов. Давайте попробуем ещё раз.", chat_history

    except Exception as e:
        logger.error(f"Ошибка в ask_assistant: {e}", exc_info=True)
        return "Произошла техническая ошибка. Пожалуйста, повторите запрос.", chat_history


if __name__ == "__main__":
    while True:
        try:
            print("\n" + "=" * 50)
            print("  ДОБРО ПОЖАЛОВАТЬ В ТЕСТОВУЮ КОНСОЛЬ КЛИНИКИ!")
            print("=" * 50)
            print("Вы можете общаться с ботом на свободные темы,")
            print("жаловаться на здоровье или просить записать вас.")
            print("Для завершения сеанса введите: 'выход' или 'exit'\n")

            history = [{"role": "system", "content": SYSTEM_PROMPT}]

            while True:
                try:
                    user_input = input("Вы: ").strip()
                    user_input = user_input.encode('utf-8', 'surrogateescape').decode('utf-8', 'ignore')

                    if not user_input:
                        continue

                    if user_input.lower() in ["выход", "exit", "quit"]:
                        print("\nСессия завершена. Спасибо за обращение!")
                        print("=" * 50 + "\n")
                        break

                    reply, history = ask_assistant(user_input, history)
                    print(f"\nАссистент: {reply}\n")

                except KeyboardInterrupt:
                    print("\nСессия прервана.")
                    break

        except KeyboardInterrupt:
            print("\nВыключение терминала клиники. До свидания!")
            break
