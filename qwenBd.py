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
logger = logging.getLogger("MedAssistant")

MODEL_NAME = "qwen2.5:7b"

DB_CONFIG = {
    "dbname": "qwenTest",
    "user": "postgres",
    "password": "admin",
    "host": "localhost",
    "port": "5432"
}


def get_free_slots(doctor_job_or_name: str) -> str:
    logger.info(f"Вызов get_free_slots() для аргумента: '{doctor_job_or_name}'")

    try:
        logger.info("Подключение к базе данных PostgreSQL...")
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
        logger.info(f"Выполнение SQL-запроса поиска слотов с параметром: '{search_param}'")

        cursor.execute(query, (search_param, search_param))
        rows = cursor.fetchall()

        logger.info(f"Запрос выполнен успешно. Найдено строк: {len(rows)}")

        cursor.close()
        conn.close()
        logger.info("Соединение с БД успешно закрыто.")

        if not rows:
            return json.dumps({"message": f"Свободных слотов для '{doctor_job_or_name}' не найдено."},
                              ensure_ascii=False)

        for row in rows:
            row['time'] = row['time'].strftime('%Y-%m-%d %H:%M')

        return json.dumps(rows, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Критическая ошибка в get_free_slots(): {str(e)}", exc_info=True)
        return json.dumps({"error": f"Ошибка при работе с БД: {str(e)}"}, ensure_ascii=False)


def book_appointment(doctor_job_or_name: str, patient_name: str, date_time_str: str) -> str:
    logger.info(
        f"Вызов book_appointment(). Параметры: Врач='{doctor_job_or_name}', Пациент='{patient_name}', Время='{date_time_str}'")

    try:
        logger.info("Подключение к базе данных PostgreSQL...")
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()

        logger.info(f"Проверка существования пациента '{patient_name}' в БД...")
        cursor.execute("SELECT id FROM patient WHERE LOWER(name) = LOWER(%s);", (patient_name,))
        patient_row = cursor.fetchone()

        if patient_row:
            patient_id = patient_row[0]
            logger.info(f"Пациент найден. ID в базе: {patient_id}")
        else:
            logger.info(f"Пациент '{patient_name}' не найден. Создание новой записи...")
            cursor.execute("INSERT INTO patient (name) VALUES (%s) RETURNING id;", (patient_name,))
            patient_id = cursor.fetchone()[0]
            logger.info(f"Новый пациент успешно создан. Присвоен ID: {patient_id}")

        logger.info(f"Поиск врача по запросу '{doctor_job_or_name}'...")
        search_param = f"%{doctor_job_or_name}%"
        cursor.execute(
            "SELECT id, name FROM doctor WHERE LOWER(job) LIKE LOWER(%s) OR LOWER(name) LIKE LOWER(%s) LIMIT 1;",
            (search_param, search_param)
        )
        doctor_row = cursor.fetchone()

        if not doctor_row:
            logger.warning(f"Врач по запросу '{doctor_job_or_name}' не обнаружен в таблице doctor!")
            cursor.close()
            conn.close()
            return json.dumps({"error": f"Врач или специальность '{doctor_job_or_name}' не найдены в базе."},
                              ensure_ascii=False)

        doctor_id, doctor_real_name = doctor_row
        logger.info(f"Найден врач: {doctor_real_name} (ID: {doctor_id})")

        logger.info(f"Проверка доступности слота на время '{date_time_str}' у врача ID {doctor_id}...")
        cursor.execute(
            "SELECT isbooked FROM slots WHERE doctorid = %s AND time = %s::timestamp;",
            (doctor_id, date_time_str)
        )
        slot_row = cursor.fetchone()

        if not slot_row:
            logger.warning(
                f"Слот на время '{date_time_str}' у врача {doctor_real_name} вообще отсутствует в расписании (таблице slots)!")
            cursor.close()
            conn.close()
            return json.dumps(
                {"error": f"Слот на время '{date_time_str}' у врача {doctor_real_name} не существует в расписании."},
                ensure_ascii=False)

        if slot_row[0] is True:
            logger.warning(f"Слот на {date_time_str} у врача {doctor_real_name} уже занят другим пациентом!")
            cursor.close()
            conn.close()
            return json.dumps({"error": f"Извините, слот на {date_time_str} у врача {doctor_real_name} уже занят."},
                              ensure_ascii=False)

        logger.info(f"Слот свободен. Выполнение UPDATE для записи пациента {patient_id}...")
        cursor.execute(
            """
            UPDATE slots
            SET isbooked  = TRUE,
                patientid = %s
            WHERE doctorid = %s
              AND time = %s:: timestamp;
            """,
            (patient_id, doctor_id, date_time_str)
        )
        conn.commit()
        logger.info("Транзакция успешно зафиксирована (COMMIT).")

        cursor.close()
        conn.close()
        logger.info("Соединение с БД успешно закрыто.")

        return json.dumps({
            "status": "success",
            "message": f"Пациент {patient_name} успешно записан к врачу {doctor_real_name} на {date_time_str}."
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Критическая ошибка в book_appointment(): {str(e)}", exc_info=True)
        return json.dumps({"error": f"Ошибка транзакции записи: {str(e)}"}, ensure_ascii=False)


available_functions = {
    "get_free_slots": get_free_slots,
    "book_appointment": book_appointment
}

tools = [
    {
        'type': 'function',
        'function': {
            'name': 'get_free_slots',
            'description': 'Позволяет получить список всех свободных слотов у конкретного врача. Искать можно как по специальности (например, "Кардиолог"), так и по фамилии.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'doctor_job_or_name': {
                        'type': 'string',
                        'description': 'Специальность врача или его фамилия на русском языке. Например: "Терапевт", "Иванова".',
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
            'description': 'Записывает (бронирует слот) пациента к выбранному врачу на конкретное время и дату.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'doctor_job_or_name': {
                        'type': 'string',
                        'description': 'Специальность врача или его фамилия. Например: "Кардиолог", "Иванова".',
                    },
                    'patient_name': {
                        'type': 'string',
                        'description': 'Полное ФИО пациента, которого нужно записать. Например: "Иванов Иван Иванович".',
                    },
                    'date_time_str': {
                        'type': 'string',
                        'description': 'Дата и время приема строго в формате "YYYY-MM-DD HH:MM:SS". Например: "2026-07-16 10:00:00".',
                    },
                },
                'required': ['doctor_job_or_name', 'patient_name', 'date_time_str'],
            },
        },
    }
]

SYSTEM_PROMPT = """
Ты — вежливый ИИ-ассистент в регистратуре клиники. Твоя задача — помогать пациентам узнавать расписание врачей и записываться на прием.

ПРАВИЛА ПОВЕДЕНИЯ:
1. Если пациент хочет узнать, когда принимает врач (или когда есть свободное время), вызови функцию `get_free_slots`.
2. Если пациент хочет записаться, у тебя должны быть ТРИ вещи: Специальность/Имя врача, ФИО пациента и точное время записи.
3. Если пациент просит записать его, но не указал точное время, сначала вызови `get_free_slots`, чтобы показать ему доступные варианты, и попроси его выбрать.
4. Если запись прошла успешно (функция `book_appointment` вернула успех), дружелюбно подтверди это пациенту.
5. Отвечай всегда строго на русском языке, вежливо и лаконично.
"""


def clean_model_output(text: str) -> str:
    # """Убирает артефакты генерации локальной модели."""
    # if not text:
    #     return ""
    # for word in ["olicit", "</tool_call>", "<tool_call>"]:
    #     text = text.replace(word, "")
    return text.strip()


def ask_assistant(user_message: str, chat_history=[]):
    if not chat_history:
        logger.info("Инициализация нового диалога. Добавление SYSTEM_PROMPT.")
        chat_history.append({"role": "system", "content": SYSTEM_PROMPT})

    logger.info(f"Получено сообщение от пользователя: '{user_message}'")
    chat_history.append({"role": "user", "content": user_message})

    try:
        logger.info(f"Отправка запроса в Ollama (модель: {MODEL_NAME})...")
        response = ollama.chat(
            model=MODEL_NAME,
            messages=chat_history,
            tools=tools,
            options={"temperature": 0.0}
        )

        raw_content = response.message.content
        logger.info(f"Ответ от Ollama получен. Сырой текст ответа: '{raw_content}'")

        if response.message.tool_calls:
            logger.info(f"Ollama приняла решение вызвать инструмент(ы): {response.message.tool_calls}")

            for tool in response.message.tool_calls:
                func_name = tool.function.name
                func_args = tool.function.arguments

                logger.info(f"Распознан вызов функции: '{func_name}' с аргументами: {func_args}")

                if func_name in available_functions:
                    db_func = available_functions[func_name]

                    db_result_str = db_func(**func_args)
                    logger.info(f"Результат выполнения функции {func_name} из БД: {db_result_str}")

                    chat_history.append(response.message)
                    chat_history.append({
                        'role': 'tool',
                        'content': db_result_str,
                    })

                    logger.info(
                        "Отправка результата выполнения инструмента обратно в Ollama для формулирования ответа...")
                    final_response = ollama.chat(
                        model=MODEL_NAME,
                        messages=chat_history,
                        options={"temperature": 0.0}
                    )

                    final_text = clean_model_output(final_response.message.content)
                    logger.info(f"Итоговый ответ сформулирован: '{final_text}'")
                    print(f"\nАссистент: {final_text}")

                    chat_history.append(final_response.message)
                    return chat_history
                else:
                    logger.error(f"Модель попыталась вызвать несуществующую функцию: '{func_name}'")
        else:
            final_text = clean_model_output(raw_content)
            logger.info(f"Вызов инструментов не потребовался. Ответ пользователю: '{final_text}'")
            print(f"\nАссистент: {final_text}")
            chat_history.append(response.message)
            return chat_history

    except Exception as e:
        logger.error(f"Произошла ошибка в цикле ask_assistant(): {str(e)}", exc_info=True)
        print("\nАссистент: Произошла техническая ошибка. Пожалуйста, попробуйте позже.")
        return chat_history


if __name__ == "__main__":
    logger.info("=== Запуск медицинского ассистента ===")
    history = []

    history = ask_assistant("Здравствуйте! Подскажите, когда принимает кардиолог?", history)

    history = ask_assistant("Запишите меня (я Ковалев Сергей Павлович) на 16 июля на 13:00 к кардиологу", history)
