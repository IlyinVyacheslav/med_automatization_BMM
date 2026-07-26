import json
import logging
import os
import re
import requests
import streamlit as st
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from ollama import Client

from repository import ClinicRepository, RepositoryError

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


@st.cache_resource(show_spinner=False)
def get_repo() -> ClinicRepository:
    # роль суперпользователя
    return ClinicRepository(
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


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
        repo = get_repo()
        if repo.get_medical_knowledge_base_size() > 0 and rewrite_RAG != "true":
            logger.info("База знаний уже заполнена. Пропуск инициализации.")
            st.info("База знаний уже заполнена")
            st.stop()
            return
    except RepositoryError as e:
        logger.error(f"Не удалось подключиться к БД для построения RAG: {e}")
        st.error(f"Не удалось подключиться к базе данных: {e.message}")
        st.stop()

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
                repo.add_specialty_to_medical_knowledge_base(
                    doctor=doctor,
                    title=title,
                    chunk=chunk,
                    embedding=embedding
                )

            logger.info(f"✅ Статья '{title}' успешно добавлена.")

        except Exception as e:
            logger.error(f"❌ Ошибка при обработке '{title}': {e}. Продолжаю работу.")

    repo.close()
    logger.info("Процесс инициализации завершен.")


if __name__ == "__main__":
    main()
