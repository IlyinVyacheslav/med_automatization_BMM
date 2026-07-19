import json
import logging
import os
import re
import ollama
import psycopg2
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pgvector.psycopg2 import register_vector
from ollama import Client

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv()
vec_model = os.getenv("OLLAMA_VEC_MODEL", "bge-m3")
RAG_json = os.getenv("RAG_JSON_PATH", "config_data.json")
rewrite_RAG = os.getenv("REWRITE_RAG", "false")

ollama_client = Client(host='http://ollama:11434')

# TODO config??
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": os.getenv("DB_PORT", "5432"),
    "dbname": os.getenv("DB_NAME", "clinic"),
    "user":  "postgres",
    "password": "postgres",
    "sslmode": os.getenv("DB_SSLMODE", "prefer"),
    "client_encoding": os.getenv("DB_CLIENT_ENCODING", "UTF8"),
    "options": f"-c timezone={os.getenv('DB_TIMEZONE', 'Europe/Moscow')}",
}


def get_db_connection():
    conn = psycopg2.connect(**DB_CONFIG)
    register_vector(conn)
    return conn


def extract_clean_text(html_content: str, title: str) -> str:
    soup = BeautifulSoup(html_content, "lxml")
    main_content = soup.find(id="bodyContent") or soup.find(class_="mw-parser-output") or soup
    for selector in ["script", "style", "table", "div.navbox", "sup.reference"]:
        for element in main_content.select(selector):
            element.decompose()
    text = re.sub(r"\s+", " ", main_content.get_text(separator=" ", strip=True))
    text = re.sub(r"\[\d+\]", "", text)
    return f"# {title}\n\n{text}"


def get_embedding(text: str) -> list:
    response = ollama_client.embed(model=vec_model, input=text)
    return response["embeddings"][0]


def main():
    try:
        with open(RAG_json, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"Не удалось загрузить {RAG_json}: {e}")
        return

    try:
        conn = get_db_connection()
        cur = conn.cursor()
    except Exception as e:
        logger.error(f"Не удалось подключиться к БД для построения RAG: {e}" )
        return

    cur.execute("SELECT COUNT(*) FROM clinic.medical_knowledge_base;")
    if cur.fetchone()[0] > 0:
        if rewrite_RAG != "true":
            logger.info("База знаний уже заполнена. Пропуск инициализации.")
            cur.close()
            conn.close()
            return

    logger.info("База пуста, начинаю процесс заполнения...")
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=200)

    for item in config["wiki_dataset"]:
        title, url, doctor = item["title"], item["url"], item["doctor"]

        try:
            logger.info(f"Обработка статьи: {title}")

            resp = requests.get(url, headers={"User-Agent": "ClinicBot/1.0"}, timeout=15)
            resp.raise_for_status()
            clean_text = extract_clean_text(resp.text, title)

            enriched_text = f"Статья: {title}. Специалист: {doctor}. Описание: {clean_text}"
            sub_chunks = text_splitter.split_text(enriched_text)

            for chunk in sub_chunks:
                embedding = get_embedding(chunk)
                cur.execute("""
                            INSERT INTO clinic.medical_knowledge_base
                                (specialty, wiki_page_title, chunk_text, embedding)
                            VALUES (%s, %s, %s, %s)
                    """,
                            (doctor, title, chunk, embedding)
                            )

            conn.commit()
            logger.info(f"✅ Статья '{title}' успешно добавлена.")

        except Exception as e:
            logger.error(f"❌ Ошибка при обработке '{title}': {e}. Продолжаю работу.")
            conn.rollback()

    cur.close()
    conn.close()
    logger.info("Процесс инициализации завершен.")


if __name__ == "__main__":
    main()
