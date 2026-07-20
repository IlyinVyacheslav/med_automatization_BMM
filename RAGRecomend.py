import os
import json
import ollama
import psycopg2

MODEL_NAME = "qwen2.5:7b"
EMBEDDING_MODEL = "bge-m3"

DB_CONFIG = {
    "dbname": "qwenTest",
    "user": "postgres",
    "password": "admin",
    "host": "localhost",
    "port": "5432",
}

REWRITE_PROMPT = """
Ты — медицинский эксперт-архивариус. Твоя задача — преобразовать жалобу пациента в максимально точный поисковый запрос для медицинской базы знаний.

ПРАВИЛА ТРАНСФОРМАЦИИ:
1. КОНЦЕНТРАЦИЯ СУТИ: Извлеки только клинически значимые данные: анатомическую локализацию, характер патологического процесса и специфические признаки.
2. УДАЛЕНИЕ ШУМА: Полностью игнорируй метафоры, эмоциональные описания, личные переживания пациента и временные обстоятельства (если они не имеют прямого отношения к физиологии).
3. ПРОФЕССИОНАЛИЗМ: Используй исключительно принятую в медицинской литературе терминологию (анатомические названия, клинические синдромы).
4. ТЕРМИНОЛОГИЧЕСКАЯ ЧИСТОТА: Оставляй только существительные и прилагательные, описывающие объективную картину. Никаких глаголов действий или лишних оборотов.

ЯЗЫКОВЫЕ ОГРАНИЧЕНИЯ:
- Пиши ТОЛЬКО на русском языке.
- Запрещено использование англицизмов и транслитерации.

ФОРМАТ ВЫВОДА:
Сформируй строку из ключевых медицинских понятий, разделенных запятыми. 
Это должен быть структурированный набор терминов, описывающий клинический случай. 
Никаких пояснений, введений или приветствий.
"""

SYSTEM_PROMPT_TEMPLATE = """
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


def rewrite_complaint(raw_text: str) -> str:
    print(f"  📝 Лог LLM-Переводчика: Перевод жалобы на научный язык...")
    try:
        response = ollama.chat(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": REWRITE_PROMPT},
                {"role": "user", "content": f"Пациент говорит: '{raw_text}'. Переведи на медицинский язык:"}
            ],
            options={"temperature": 0.0}
        )
        rewritten_text = response['message']['content'].strip()
        print(f"  📝 Лог LLM-Переводчика: Результат -> '{rewritten_text}'")
        return rewritten_text
    except Exception as e:
        print(f"  🚨 Лог LLM-Переводчика: Ошибка перевода, используем оригинал: {e}")
        return raw_text


def get_embedding(text: str) -> list:
    try:
        response = ollama.embed(model=EMBEDDING_MODEL, input=text)
        return response["embeddings"][0]
    except Exception as e:
        print(f"  🚨 Лог: Ошибка генерации эмбеддинга: {e}")
        raise e


def retrieve_rag_context(scientific_text: str, top_k: int = 3) -> str:
    print(f"  🔍 Лог RAG: Поиск в базе (Top-{top_k})...")
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    query_embedding = get_embedding(scientific_text)
    select_query = """
                   SELECT specialty, wiki_page_title, chunk_text, (embedding <=> %s::vector) AS distance
                   FROM medical_knowledge_base
                   ORDER BY embedding <=> %s::vector
                       LIMIT %s;
                   """
    cur.execute(select_query, (query_embedding, query_embedding, top_k))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    context_chunks = []
    print(f"  📊 Лог RAG: Анализ совпадений:")

    for idx, (specialty, title, chunk_text, distance) in enumerate(rows, 1):
        similarity = 1 - distance

        if similarity < 0.512:
            continue

        context_chunks.append(
            f"--- ВАРИАНТ {idx} ---\n"
            f"Специалист: {specialty}\n"
            f"Диагноз: {title}\n"
            f"Описание: {chunk_text}\n"
            f"Релевантность: {similarity:.2f}"
        )

        print(f"    [{idx}] ВЗЯТО -> Спец: {specialty} | Статья: {title} | Сходство: {similarity:.4f}")

    if not context_chunks:
        print("  ⚠️ Лог RAG: Релевантных данных не найдено.")
        return "Специфическая медицинская информация отсутствует."

    return "\n\n".join(context_chunks)


def get_doctor_from_qwen(raw_complaint: str) -> str:
    scientific_complaint = rewrite_complaint(raw_complaint)

    rag_context = retrieve_rag_context(scientific_complaint, top_k=3)

    dynamic_system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        original_complaint=raw_complaint,
        medical_summary=scientific_complaint,
        rag_context=rag_context
    )

    print(f"  🤖 Лог LLM-Регистратора: Анализ и выбор врача...")
    try:
        response = ollama.chat(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": dynamic_system_prompt},
                {"role": "user", "content": "Проанализируй предоставленные данные и сформируй список врачей."}
            ],
            options={"temperature": 0.1}
        )

        raw_doctor = response['message']['content'].strip()
        print(f"  🤖 Лог LLM-Регистратора: Сырой ответ: '{raw_doctor}'")

        doctor = raw_doctor.strip(" .!?,*\n")
        return doctor

    except Exception as e:
        print(f"  🚨 Лог LLM-Регистратора: Ошибка: {e}")
        return "Терапевт"


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def run_terminal_emulator():
    while True:
        clear_screen()
        print("=" * 60)
        print("🏥 ДОБРО ПОЖАЛОВАТЬ В ЭЛЕКТРОННУЮ РЕГИСТРАТУРУ КЛИНИКИ")
        print("=" * 60)
        print("Введите вашу жалобу на здоровье, чтобы система подобрала врача.")
        print("(Для завершения сеанса и перезапуска терминала введите: exit или выход)\n")

        user_input = input("Пациент: ").strip()

        if not user_input:
            continue

        if user_input.lower() in ['exit', 'выход', 'quit']:
            print("\n🔄 Завершение сеанса... Подготовка терминала к новому пациенту...")
            import time
            time.sleep(1.5)
            continue

        print("\n--- [СТАРТ ОБРАБОТКИ ЗАПРОСА] ---")
        recommended_doctor = get_doctor_from_qwen(user_input)

        print("\n" + "=" * 40)
        print(f" РЕКОМЕНДАЦИЯ: Вам необходимо обратиться к: {recommended_doctor.upper()}")
        print("=" * 40)


if __name__ == "__main__":
    run_terminal_emulator()
