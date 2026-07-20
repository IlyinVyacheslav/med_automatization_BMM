import ollama
import psycopg2
from pgvector.psycopg2 import register_vector

DB_CONFIG = {"dbname": "qwenTest", "user": "postgres", "password": "admin", "host": "localhost", "port": "5432"}

SIMILARITY_THRESHOLD = 0.80


def get_embedding(text: str) -> list:
    response = ollama.embed(model="nomic-embed-text", input=text)
    return response["embeddings"][0]


def query_medical_base(user_query: str):
    """Возвращает врача и контекст, если сходство выше порога."""
    conn = psycopg2.connect(**DB_CONFIG)
    register_vector(conn)
    cur = conn.cursor()

    query_vec = get_embedding(user_query)

    cur.execute("""
                SELECT wiki_page_title,
                       chunk_text,
                       specialty,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM medical_knowledge_base
                ORDER BY similarity DESC LIMIT 1;
                """, (query_vec,))

    result = cur.fetchone()
    cur.close()
    conn.close()

    if result and result[3] >= SIMILARITY_THRESHOLD:
        title, text, doctor, sim = result
        return {
            "status": "found",
            "doctor": doctor,
            "title": title,
            "text": text,
            "similarity": sim
        }
    return {"status": "not_found"}


if __name__ == "__main__":
    test_queries = [
        # ЛОР
        "сильные боли в лобной части при наклоне головы",  # Синусит
        "болит горло, трудно глотать, красные миндалины",  # Ангина
        "постоянная заложенность носа и чихание весной",  # Аллергический ринит

        # Гастроэнтерология
        "резкая боль в правом боку, отдающая в ногу",  # Аппендицит
        "тянущие боли в желудке после приема пищи",  # Гастрит
        "частые приступы диареи и спазмы в кишечнике",  # Синдром раздражённого кишечника

        # Кардиология / Неврология (Критичные)
        "давящая боль за грудиной, одышка",  # Инфаркт миокарда
        "внезапная слабость в руке, перекосило лицо",  # Инсульт
        "сильное сердцебиение, высокое давление",  # Артериальная гипертензия

        # Эндокринология / Дерматология
        "постоянная жажда и сухость во рту",  # Сахарный диабет
        "сильная сыпь на лице, угри",  # Акне
        "высыпания на коже, сильный зуд",  # Псориаз

        # Неврология
        "пульсирующая боль в одной половине головы",  # Мигрень
        "потеря сознания и судороги",  # Эпилепсия

        # "Тест на дурака" (не должно находиться)
        "как починить разбитый телефон",
        "какой прогноз погоды на завтра",
        "рецепт приготовления блинов"
    ]

    for q in test_queries:
        res = query_medical_base(q)
        print(f"\n🔍 Запрос: {q}")
        if res["status"] == "found":
            print(f"✅ Рекомендованный врач: {res['doctor']}")
            print(f"📖 Статья: {res['title']} (Сходство: {res['similarity']:.2f})")
        else:
            print("❌ Информация не найдена в базе знаний.")