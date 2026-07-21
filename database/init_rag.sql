CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS clinic.medical_knowledge_base CASCADE;

CREATE TABLE clinic.medical_knowledge_base (
    id SERIAL PRIMARY KEY,
    specialty VARCHAR(100) NOT NULL,
    wiki_page_title VARCHAR(255),
    chunk_text TEXT NOT NULL,
    embedding VECTOR(1024) NOT NULL
);


CREATE INDEX IF NOT EXISTS idx_medical_knowledge_embedding
ON clinic.medical_knowledge_base
USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_medical_knowledge_specialty
ON clinic.medical_knowledge_base (specialty);

GRANT SELECT ON ALL TABLES IN SCHEMA clinic TO registrar, ai_bot_registrar;
