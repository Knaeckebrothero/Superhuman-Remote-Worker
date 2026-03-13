-- Superhuman Remote Worker — Vector Database Schema
-- PostgreSQL + pgvector for vector search workloads
--
-- This file defines tables that handle vector/hybrid search operations,
-- separated from the main app DB for workload isolation.
--
-- Tables:
--   sources / citations / source_* - CitationEngine tables (sources, citations, embeddings)
--   memories        - Agent memory storage with hybrid search (RecallStore)
--   knowledge_index - Project knowledge base search index
--
-- Falls back to app DB (DATABASE_URL) if VECTOR_DB_URL is not set.

-- ============================================================================
-- EXTENSIONS
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- 1. CITATION ENGINE TABLES (from CitationEngine)
-- Sources and citations with vector embeddings for semantic search.
-- ============================================================================

-- ENUM types
DO $$ BEGIN
    CREATE TYPE source_type AS ENUM ('document', 'website', 'database', 'custom');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE confidence_level AS ENUM ('high', 'medium', 'low');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE extraction_method AS ENUM ('direct_quote', 'paraphrase', 'inference', 'aggregation', 'negative');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE verification_status AS ENUM ('pending', 'verified', 'failed', 'unverified');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

-- Sources table
CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    type source_type NOT NULL,
    identifier TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT,
    content TEXT NOT NULL,
    content_hash TEXT,
    metadata JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    ALTER TABLE sources ADD CONSTRAINT uq_sources_content_hash UNIQUE (content_hash);
EXCEPTION
    WHEN duplicate_table THEN NULL;
    WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_sources_identifier ON sources(identifier);
CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(type);
CREATE INDEX IF NOT EXISTS idx_sources_name ON sources(name);
CREATE INDEX IF NOT EXISTS idx_sources_content_hash ON sources(content_hash);

-- Job-source mapping
CREATE TABLE IF NOT EXISTS job_sources (
    job_id UUID NOT NULL,  -- references jobs(id) in app DB (no FK across databases)
    source_id INTEGER NOT NULL REFERENCES sources(id),
    added_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (job_id, source_id)
);

-- Citations table
CREATE TABLE IF NOT EXISTS citations (
    id SERIAL PRIMARY KEY,
    job_id UUID NOT NULL,  -- references jobs(id) in app DB (no FK across databases)
    claim TEXT NOT NULL,
    verbatim_quote TEXT,
    quote_context TEXT NOT NULL,
    quote_language TEXT,
    relevance_reasoning TEXT,
    confidence confidence_level DEFAULT 'high',
    extraction_method extraction_method DEFAULT 'direct_quote',
    source_id INTEGER NOT NULL REFERENCES sources(id),
    locator JSONB NOT NULL,
    verification_status verification_status DEFAULT 'pending',
    verification_notes TEXT,
    similarity_score REAL,
    matched_location JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    created_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_citations_job_id ON citations(job_id);
CREATE INDEX IF NOT EXISTS idx_citations_source_id ON citations(source_id);
CREATE INDEX IF NOT EXISTS idx_citations_created_by ON citations(created_by);
CREATE INDEX IF NOT EXISTS idx_citations_verification_status ON citations(verification_status);
CREATE INDEX IF NOT EXISTS idx_citations_created_at ON citations(created_at);
CREATE INDEX IF NOT EXISTS idx_citations_locator ON citations USING GIN (locator);
CREATE INDEX IF NOT EXISTS idx_sources_metadata ON sources USING GIN (metadata);

-- Full-text search indexes
CREATE INDEX IF NOT EXISTS idx_citations_claim_fts ON citations USING GIN (to_tsvector('english', claim));
CREATE INDEX IF NOT EXISTS idx_sources_content_fts ON sources USING GIN (to_tsvector('simple', content));

-- Source annotations
CREATE TABLE IF NOT EXISTS source_annotations (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id UUID NOT NULL,  -- references jobs(id) in app DB (no FK across databases)
    annotation_type TEXT NOT NULL DEFAULT 'note',
    content TEXT NOT NULL,
    page_reference TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    created_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_annotations_source ON source_annotations(source_id);
CREATE INDEX IF NOT EXISTS idx_annotations_job ON source_annotations(job_id);
CREATE INDEX IF NOT EXISTS idx_annotations_type ON source_annotations(annotation_type);
CREATE INDEX IF NOT EXISTS idx_annotations_content_fts ON source_annotations USING GIN (to_tsvector('simple', content));

-- Source tags
CREATE TABLE IF NOT EXISTS source_tags (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id UUID NOT NULL,  -- references jobs(id) in app DB (no FK across databases)
    tag TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE(source_id, job_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_tags_tag ON source_tags(tag);
CREATE INDEX IF NOT EXISTS idx_tags_job ON source_tags(job_id);

-- Schema migrations
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    description TEXT
);

INSERT INTO schema_migrations (version, description)
VALUES (1, 'Initial schema with sources and citations tables')
ON CONFLICT (version) DO NOTHING;

INSERT INTO schema_migrations (version, description)
VALUES (2, 'Shared source library with annotations and tags')
ON CONFLICT (version) DO NOTHING;

-- Source embeddings
CREATE TABLE IF NOT EXISTS source_embeddings (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id UUID NOT NULL,  -- references jobs(id) in app DB (no FK across databases)
    chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_text TEXT NOT NULL,
    embedding vector(4096),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE(source_id, job_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_source_embeddings_source ON source_embeddings(source_id);
CREATE INDEX IF NOT EXISTS idx_source_embeddings_job ON source_embeddings(job_id);

DO $$ BEGIN
    CREATE INDEX IF NOT EXISTS idx_source_embeddings_vector ON source_embeddings
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
EXCEPTION WHEN others THEN
    RAISE NOTICE 'Skipping HNSW index on source_embeddings (dimension > 2000 or other error): %', SQLERRM;
END $$;

INSERT INTO schema_migrations (version, description)
VALUES (3, 'Vector search with source embeddings')
ON CONFLICT (version) DO NOTHING;

-- ============================================================================
-- 2. MEMORIES TABLE (Memory Light — RecallStore)
-- Agent memory storage with hybrid search (dense vector + sparse keyword + recency).
-- See docs/features/memory_light.md for full architecture.
-- ============================================================================

CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL,  -- references jobs(id) in app DB (no FK across databases)
    agent_id VARCHAR(100),

    -- Content
    content TEXT NOT NULL,
    summary VARCHAR(500),

    -- Classification
    memory_type VARCHAR(50) DEFAULT 'factual',
    source VARCHAR(50) DEFAULT 'observer',

    -- Search channels
    keywords TEXT[] DEFAULT '{}',
    embedding vector(4096),
    sparse_keywords TSVECTOR,

    -- Scoring
    importance FLOAT DEFAULT 0.5,

    -- Provenance
    source_turn_start INT,
    source_turn_end INT,
    source_phase INT,

    -- Budget tracking
    token_count INT DEFAULT 0,
    access_count INT DEFAULT 0,

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_accessed TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT valid_memory_type CHECK (memory_type IN ('factual', 'procedural', 'error_solution', 'vocabulary', 'relational')),
    CONSTRAINT valid_memory_source CHECK (source IN ('observer', 'todo', 'compaction', 'phase_archive', 'tool_error'))
);

CREATE INDEX IF NOT EXISTS idx_memories_job ON memories(job_id);
CREATE INDEX IF NOT EXISTS idx_memories_job_importance ON memories(job_id, importance DESC);
CREATE INDEX IF NOT EXISTS idx_memories_job_accessed ON memories(job_id, last_accessed DESC);
CREATE INDEX IF NOT EXISTS idx_memories_job_type ON memories(job_id, memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_keywords ON memories USING GIN(keywords);
CREATE INDEX IF NOT EXISTS idx_memories_sparse ON memories USING GIN(sparse_keywords);
DO $$ BEGIN
    CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 256);
EXCEPTION WHEN others THEN
    RAISE NOTICE 'Skipping HNSW index on memories (dimension > 2000 or other error): %', SQLERRM;
END $$;

-- Hybrid search function: RRF-based fusion of dense, sparse, and recency channels
CREATE OR REPLACE FUNCTION memory_hybrid_search(
    query_text text,
    query_embedding vector(4096),
    job_id_param uuid,
    match_count int DEFAULT 10,
    dense_weight float DEFAULT 0.6,
    sparse_weight float DEFAULT 0.3,
    recency_weight float DEFAULT 0.1,
    rrf_k int DEFAULT 50,
    importance_floor float DEFAULT 0.0
) RETURNS SETOF memories LANGUAGE sql AS $$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> query_embedding) AS rank_ix
    FROM memories WHERE job_id = job_id_param AND embedding IS NOT NULL AND importance >= importance_floor
    ORDER BY rank_ix LIMIT match_count * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(sparse_keywords, websearch_to_tsquery('english', query_text)) DESC) AS rank_ix
    FROM memories WHERE job_id = job_id_param AND sparse_keywords @@ websearch_to_tsquery('english', query_text) AND importance >= importance_floor
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE job_id = job_id_param AND importance >= importance_floor
    ORDER BY rank_ix LIMIT match_count
)
SELECT memories.* FROM (
    SELECT COALESCE(d.id, s.id, r.id) AS mid,
        COALESCE(1.0 / (rrf_k + d.rank_ix), 0.0) * dense_weight +
        COALESCE(1.0 / (rrf_k + s.rank_ix), 0.0) * sparse_weight +
        COALESCE(1.0 / (rrf_k + r.rank_ix), 0.0) * recency_weight AS rrf_score
    FROM dense d
    FULL OUTER JOIN sparse s ON d.id = s.id
    FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id
) ranked
JOIN memories ON ranked.mid = memories.id
ORDER BY ranked.rrf_score DESC
LIMIT match_count
$$;

-- ============================================================================
-- 3. KNOWLEDGE INDEX (Project Knowledge Base — Search Index)
-- Derived search index for project knowledge notes stored in Neo4j.
-- Updated via write-through on every kb_write/kb_update.
-- See docs/features/project_knowledge_base.md for full architecture.
-- ============================================================================

CREATE TABLE IF NOT EXISTS knowledge_index (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    note_id VARCHAR(100) NOT NULL,           -- Neo4j note id / slug (e.g. "chose-jwt-over-oauth")
    project_id UUID NOT NULL,                -- references projects(id) in app DB (no FK across databases)
    title TEXT NOT NULL,
    note_type VARCHAR(50) NOT NULL,           -- goal, plan, decision, learning, code, source, question, state, retrospective
    status VARCHAR(50) DEFAULT 'active',
    confidence VARCHAR(20),
    tags TEXT[] DEFAULT '{}',
    keywords TEXT[] DEFAULT '{}',
    job_id UUID,                              -- which job created this note
    phase INT,
    content TEXT NOT NULL,                    -- full note body (for search result display)
    retrieval_messages TEXT[] DEFAULT '{}',   -- curator-generated queries for when this note should surface
    embedding vector(4096),                    -- dense embedding (4096 for qwen3-embedding-8b)
    search_doc tsvector,                     -- full-text search document
    created_at TIMESTAMPTZ,
    modified_at TIMESTAMPTZ,
    indexed_at TIMESTAMPTZ DEFAULT NOW(),    -- when this row was last written
    content_hash VARCHAR(64),                -- sha256 of content (skip re-embedding on metadata-only updates)

    CONSTRAINT uq_knowledge_project_note UNIQUE (project_id, note_id),
    CONSTRAINT valid_note_type CHECK (note_type IN (
        'goal', 'plan', 'decision', 'learning', 'code',
        'source', 'question', 'state', 'retrospective'
    )),
    CONSTRAINT valid_note_status CHECK (status IN ('active', 'resolved', 'superseded', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_knowledge_project ON knowledge_index(project_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_project_type ON knowledge_index(project_id, note_type);
CREATE INDEX IF NOT EXISTS idx_knowledge_project_status ON knowledge_index(project_id, status);
CREATE INDEX IF NOT EXISTS idx_knowledge_tags ON knowledge_index USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_knowledge_search ON knowledge_index USING GIN(search_doc);
DO $$ BEGIN
    CREATE INDEX IF NOT EXISTS idx_knowledge_embedding ON knowledge_index
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 256);
EXCEPTION WHEN others THEN
    RAISE NOTICE 'Skipping HNSW index on knowledge_index (dimension > 2000 or other error): %', SQLERRM;
END $$;

-- Hybrid search function: RRF-based fusion of dense, sparse, and recency channels
-- Same pattern as memory_hybrid_search but scoped by project_id instead of job_id
CREATE OR REPLACE FUNCTION knowledge_hybrid_search(
    query_text text,
    query_embedding vector,
    project_id_param uuid,
    match_count int DEFAULT 10,
    dense_weight float DEFAULT 0.6,
    sparse_weight float DEFAULT 0.3,
    recency_weight float DEFAULT 0.1,
    rrf_k int DEFAULT 50
) RETURNS SETOF knowledge_index LANGUAGE sql AS $$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> query_embedding) AS rank_ix
    FROM knowledge_index
    WHERE project_id = project_id_param AND status = 'active' AND embedding IS NOT NULL
    ORDER BY rank_ix LIMIT match_count * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(search_doc, websearch_to_tsquery('english', query_text)) DESC) AS rank_ix
    FROM knowledge_index
    WHERE project_id = project_id_param AND status = 'active'
      AND search_doc @@ websearch_to_tsquery('english', query_text)
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY modified_at DESC) AS rank_ix
    FROM knowledge_index
    WHERE project_id = project_id_param AND status = 'active'
    ORDER BY rank_ix LIMIT match_count
)
SELECT ki.* FROM (
    SELECT COALESCE(d.id, s.id, r.id) AS mid,
        COALESCE(1.0 / (rrf_k + d.rank_ix), 0.0) * dense_weight +
        COALESCE(1.0 / (rrf_k + s.rank_ix), 0.0) * sparse_weight +
        COALESCE(1.0 / (rrf_k + r.rank_ix), 0.0) * recency_weight AS rrf_score
    FROM dense d
    FULL OUTER JOIN sparse s ON d.id = s.id
    FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id
) ranked
JOIN knowledge_index ki ON ranked.mid = ki.id
ORDER BY ranked.rrf_score DESC
LIMIT match_count
$$;
