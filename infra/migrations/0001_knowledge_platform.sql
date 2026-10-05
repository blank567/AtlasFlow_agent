CREATE EXTENSION IF NOT EXISTS vector;

-- SQLAlchemy owns the portable table definitions. These two database-native
-- indexes document and optimize the PostgreSQL retrieval path.
CREATE INDEX IF NOT EXISTS knowledge_chunks_v2_generation_kind_idx
    ON knowledge_chunks_v2 (generation_id, chunk_kind);

CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_space_source_idx
    ON knowledge_documents (space_id, source_id);

ALTER TABLE knowledge_chunks_v2
    ADD COLUMN IF NOT EXISTS search_vector TSVECTOR
    GENERATED ALWAYS AS (to_tsvector('simple', lexical_text)) STORED;

CREATE INDEX IF NOT EXISTS knowledge_chunks_v2_search_idx
    ON knowledge_chunks_v2 USING GIN (search_vector);

-- Embedding dimensions are profile-specific in v0.6.0. A generation-specific
-- HNSW index is created only after the profile dimension is known.
