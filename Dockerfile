FROM python:3.11-slim
 
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    streamlit \
    psycopg2-binary \
    pandas \
    dotenv \
    ollama \
    pytest \
    langchain-text-splitters \
    pgvector \
    beautifulsoup4 \
    lxml \
    requests

COPY . .