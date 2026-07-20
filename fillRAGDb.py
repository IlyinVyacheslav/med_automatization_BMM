import json
import os
import re
import ollama
import psycopg2
from psycopg2.extras import execute_values
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter
from pgvector.psycopg2 import register_vector

CONFIG_FILE = "config_data.json"
TXT_DIR = "data_txt"
DB_CONFIG = {"dbname": "qwenTest", "user": "postgres", "password": "admin", "host": "localhost", "port": "5432"}

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100


def clean_text(text: str) -> str:
    return re.sub(r'[^\w\s\.\,\!\?\-\(\)\[\]]', '', text).strip()


def get_embedding(text: str) -> list:
    try:
        response = ollama.embed(model="bge-m3", input=text)
        return response["embeddings"][0]
    except Exception as e:
        print(f"🚨 Ошибка векторизации: {e}")
        raise e


def load_and_chunk_data():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=200)
    db_records = []

    for item in config["wiki_dataset"]:
        title, doctor = item["title"], item["doctor"]
        file_path = os.path.join(TXT_DIR, f"{title}.txt")

        if not os.path.exists(file_path):
            print(f"Файл не найден: {file_path}")
            continue

        with open(file_path, "r", encoding="utf-8") as f_txt:
            full_text = f_txt.read()

        clean_full_text = re.sub(r'[^\w\s\.\,\!\?\-\(\)\[\]]', '', full_text)

        enriched_base_text = f"Статья: {title}. Специалист: {doctor}. Описание: {clean_full_text}"

        sub_chunks = text_splitter.split_text(enriched_base_text)

        print(f"Обработка '{title}': создано {len(sub_chunks)} чанков")

        for chunk in sub_chunks:
            embedding = get_embedding(chunk)
            db_records.append((doctor, title, chunk, embedding))

    return db_records


def insert_into_postgres(records):
    if not records: return

    conn = psycopg2.connect(**DB_CONFIG)
    register_vector(conn)
    cur = conn.cursor()

    cur.execute("TRUNCATE TABLE medical_knowledge_base;")

    insert_query = "INSERT INTO medical_knowledge_base (specialty, wiki_page_title, chunk_text, embedding) VALUES %s;"

    try:
        execute_values(cur, insert_query, records)
        conn.commit()
        print(f"✅ Успешно записано {len(records)} чанков.")
    except Exception as e:
        conn.rollback()
        print(f"🚨 Ошибка БД: {e}")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    records = load_and_chunk_data()
    insert_into_postgres(records)