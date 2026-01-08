-- Graph-RAG Autonomous Agent System
-- Vector Search Schema (Optional - requires pgvector extension)
--
-- This adds vector search capabilities for workspace files.
-- Only run this if you have pgvector installed:
--   CREATE EXTENSION vector;

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- WORKSPACE_EMBEDDINGS TABLE
-- Stores vector embeddings for workspace file contents
-- ============================================================================

CREATE TABLE IF NOT EXISTS workspace_embeddings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    job_id VARCHAR(100) NOT NULL,
    file_path VARCHAR(500) NOT NULL,
    chunk_index INTEGER DEFAULT 0,

    content_hash VARCHAR(64),
    content_preview TEXT,
    char_count INTEGER,

    embedding vector(1536),  -- OpenAI text-embedding-3-small
    embedding_model VARCHAR(100) DEFAULT 'text-embedding-3-small',

    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(job_id, file_path, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_workspace_embeddings_job_id ON workspace_embeddings(job_id);
CREATE INDEX IF NOT EXISTS idx_workspace_embeddings_file_path ON workspace_embeddings(job_id, file_path);

-- HNSW index for fast approximate nearest neighbor search
CREATE INDEX IF NOT EXISTS idx_workspace_embeddings_vector
ON workspace_embeddings USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- Trigger for updated_at
DROP TRIGGER IF EXISTS update_workspace_embeddings_updated_at ON workspace_embeddings;
CREATE TRIGGER update_workspace_embeddings_updated_at
    BEFORE UPDATE ON workspace_embeddings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- HELPER FUNCTIONS
-- ============================================================================

CREATE OR REPLACE FUNCTION cleanup_job_embeddings(p_job_id VARCHAR(100))
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM workspace_embeddings WHERE job_id = p_job_id;
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION search_workspace_similar(
    p_job_id VARCHAR(100),
    p_query_embedding vector(1536),
    p_limit INTEGER DEFAULT 10,
    p_threshold FLOAT DEFAULT 0.7
)
RETURNS TABLE(
    file_path VARCHAR(500),
    chunk_index INTEGER,
    content_preview TEXT,
    similarity FLOAT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        we.file_path,
        we.chunk_index,
        we.content_preview,
        1 - (we.embedding <=> p_query_embedding) AS similarity
    FROM workspace_embeddings we
    WHERE we.job_id = p_job_id
      AND 1 - (we.embedding <=> p_query_embedding) >= p_threshold
    ORDER BY we.embedding <=> p_query_embedding
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql;
