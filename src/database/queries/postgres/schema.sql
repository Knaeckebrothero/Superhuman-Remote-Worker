-- Graph-RAG Autonomous Agent System
-- PostgreSQL Schema
--
-- This file defines all tables for the Graph-RAG system.
-- Run with: python src/scripts/app_init.py --force-reset
--
-- Tables:
--   jobs              - Job tracking and orchestration
--   requirements      - Primary storage for extracted requirements
--   sources           - Document sources for citations (citation_tool)
--   citations         - Citation records linking claims to sources (citation_tool)
--   schema_migrations - Schema versioning for citation_tool
--
-- Note: LLM logging is handled by MongoDB (llm_archiver.py).
-- Note: Agent checkpointing is handled by LangGraph's AsyncPostgresSaver.
-- Note: Agent workspace is handled by filesystem (workspace_manager.py).

-- ============================================================================
-- EXTENSIONS
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- 1. JOBS TABLE
-- Tracks all processing jobs submitted to the system
-- ============================================================================

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Input data
    prompt TEXT NOT NULL,
    document_path TEXT,
    document_content BYTEA,
    context JSONB DEFAULT '{}',

    -- Status tracking
    status VARCHAR(50) NOT NULL DEFAULT 'created',
    creator_status VARCHAR(50) DEFAULT 'pending',
    validator_status VARCHAR(50) DEFAULT 'pending',

    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE,

    -- Error tracking
    error_message TEXT,
    error_details JSONB,

    -- Resource tracking
    total_tokens_used INTEGER DEFAULT 0,
    total_requests INTEGER DEFAULT 0,

    CONSTRAINT valid_status CHECK (status IN ('created', 'processing', 'completed', 'failed', 'cancelled', 'pending_review')),
    CONSTRAINT valid_creator_status CHECK (creator_status IN ('pending', 'processing', 'completed', 'failed')),
    CONSTRAINT valid_validator_status CHECK (validator_status IN ('pending', 'processing', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_creator_status ON jobs(creator_status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

-- ============================================================================
-- 2. REQUIREMENTS TABLE
-- Primary storage for extracted requirements.
--
-- Workflow:
--   1. Creator Agent extracts requirements from documents and stores them here
--   2. Validator Agent queries for requirements with neo4j_id IS NULL (unprocessed)
--   3. After validation, Validator updates neo4j_id to link to the Neo4j node
--
-- Human-queryable: This table serves as the authoritative source for all
-- extracted requirements and their validation status.
-- ============================================================================

CREATE TABLE IF NOT EXISTS requirements (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,

    -- Requirement identification
    requirement_id VARCHAR(100),  -- Optional external/canonical ID

    -- Requirement content
    text TEXT NOT NULL,
    name VARCHAR(500),
    type VARCHAR(100),            -- functional, compliance, constraint, etc.
    priority VARCHAR(50),         -- high, medium, low

    -- Source tracking
    source_document TEXT,
    source_location JSONB,        -- {article, paragraph, page, etc.}

    -- Compliance relevance
    gobd_relevant BOOLEAN DEFAULT FALSE,
    gdpr_relevant BOOLEAN DEFAULT FALSE,

    -- Research data (from Creator)
    citations JSONB DEFAULT '[]',
    mentioned_objects JSONB DEFAULT '[]',
    mentioned_messages JSONB DEFAULT '[]',
    reasoning TEXT,
    research_notes TEXT,
    confidence FLOAT DEFAULT 0.0,

    -- Validation (from Validator)
    -- neo4j_id: The Neo4j node rid after the requirement is integrated into the graph.
    -- Validator queries WHERE neo4j_id IS NULL to find unprocessed requirements.
    neo4j_id VARCHAR(100),
    validation_result JSONB,
    rejection_reason TEXT,

    -- Processing metadata
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    validated_at TIMESTAMP WITH TIME ZONE,

    tags JSONB DEFAULT '[]',

    CONSTRAINT valid_req_status CHECK (status IN ('pending', 'validating', 'integrated', 'rejected', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_requirements_job_id ON requirements(job_id);
CREATE INDEX IF NOT EXISTS idx_requirements_status ON requirements(status);
CREATE INDEX IF NOT EXISTS idx_requirements_neo4j_id ON requirements(neo4j_id);
CREATE INDEX IF NOT EXISTS idx_requirements_created_at ON requirements(created_at);
-- Partial index for efficient polling of unprocessed requirements
CREATE INDEX IF NOT EXISTS idx_requirements_unprocessed ON requirements(job_id, created_at) WHERE neo4j_id IS NULL;

-- ============================================================================
-- 3. CITATION ENGINE TABLES (from citation_tool)
-- Used by Creator agent for document citations and source tracking
-- ============================================================================

-- Create ENUM types if they don't exist
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

-- Sources table: canonical documents, websites, databases, or custom artifacts
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

CREATE INDEX IF NOT EXISTS idx_sources_identifier ON sources(identifier);
CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(type);
CREATE INDEX IF NOT EXISTS idx_sources_name ON sources(name);

-- Citations table: links claims to their supporting evidence
CREATE TABLE IF NOT EXISTS citations (
    id SERIAL PRIMARY KEY,
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

CREATE INDEX IF NOT EXISTS idx_citations_source_id ON citations(source_id);
CREATE INDEX IF NOT EXISTS idx_citations_created_by ON citations(created_by);
CREATE INDEX IF NOT EXISTS idx_citations_verification_status ON citations(verification_status);
CREATE INDEX IF NOT EXISTS idx_citations_created_at ON citations(created_at);
CREATE INDEX IF NOT EXISTS idx_citations_locator ON citations USING GIN (locator);
CREATE INDEX IF NOT EXISTS idx_sources_metadata ON sources USING GIN (metadata);

-- Schema migrations table (for citation_tool versioning)
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    description TEXT
);

INSERT INTO schema_migrations (version, description)
VALUES (1, 'Initial schema with sources and citations tables')
ON CONFLICT (version) DO NOTHING;

-- ============================================================================
-- 4. HELPER FUNCTIONS
-- ============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- ============================================================================
-- 5. TRIGGERS
-- ============================================================================

DROP TRIGGER IF EXISTS update_jobs_updated_at ON jobs;
CREATE TRIGGER update_jobs_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_requirements_updated_at ON requirements;
CREATE TRIGGER update_requirements_updated_at
    BEFORE UPDATE ON requirements
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- 6. VIEWS
-- ============================================================================

CREATE OR REPLACE VIEW job_summary AS
SELECT
    j.id,
    j.status,
    j.creator_status,
    j.validator_status,
    j.created_at,
    j.completed_at,
    COUNT(DISTINCT r.id) FILTER (WHERE r.status = 'pending') as pending_requirements,
    COUNT(DISTINCT r.id) FILTER (WHERE r.status = 'validating') as validating_requirements,
    COUNT(DISTINCT r.id) FILTER (WHERE r.status = 'integrated') as integrated_requirements,
    COUNT(DISTINCT r.id) FILTER (WHERE r.status = 'rejected') as rejected_requirements,
    COUNT(DISTINCT r.id) FILTER (WHERE r.status = 'failed') as failed_requirements,
    j.total_tokens_used,
    j.total_requests
FROM jobs j
LEFT JOIN requirements r ON j.id = r.job_id
GROUP BY j.id;
