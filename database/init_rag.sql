-- 1. Включаем расширение (теперь оно у тебя точно скомпилировано и доступно!)
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Удаляем старую таблицу, если она была создана в ходе экспериментов
DROP TABLE IF EXISTS clinic.medical_knowledge_base CASCADE;

-- 3. Создаем таблицу со сжатой размерностью векторов (1536 вместо 3584)
CREATE TABLE clinic.medical_knowledge_base (
    id SERIAL PRIMARY KEY,
    specialty VARCHAR(100) NOT NULL,      -- К какому врачу привязана статья
    wiki_page_title VARCHAR(255),         -- Название статьи на Википедии
    chunk_text TEXT NOT NULL,             -- Смысловой кусок текста
    embedding VECTOR(1024) NOT NULL      -- Сжатый вектор (подходит под лимиты индексов)
);

-- 4. Создаем супер-быстрый HNSW индекс для косинусного поиска
-- Он будет искать нужного врача за миллисекунды на любых объемах данных
CREATE INDEX IF NOT EXISTS idx_medical_knowledge_embedding
ON clinic.medical_knowledge_base
USING hnsw (embedding vector_cosine_ops);

-- 5. Обычный индекс для фильтрации по специальностям (на всякий случай)
CREATE INDEX IF NOT EXISTS idx_medical_knowledge_specialty
ON clinic.medical_knowledge_base (specialty);