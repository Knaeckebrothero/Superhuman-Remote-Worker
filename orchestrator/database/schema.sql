-- Graph-RAG Autonomous Agent System
-- PostgreSQL Schema
--
-- This file defines all tables for the Graph-RAG system.
-- Run with: python src/scripts/app_init.py --force-reset
--
-- Tables:
--   jobs              - Job tracking and orchestration
--   agents            - Registered agent pods for orchestration
--   requirements      - Primary storage for extracted requirements
--   sources           - Document sources for citations (CitationEngine)
--   citations         - Citation records linking claims to sources (CitationEngine)
--   schema_migrations - Schema versioning for CitationEngine
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
    description TEXT NOT NULL,
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

    -- Job configuration (which agent config to use)
    config_name VARCHAR(100) DEFAULT 'default',
    config_override JSONB DEFAULT NULL,
    assigned_agent_id UUID,  -- FK added after agents table creation

    CONSTRAINT valid_status CHECK (status IN ('created', 'processing', 'completed', 'failed', 'cancelled', 'pending_review')),
    CONSTRAINT valid_creator_status CHECK (creator_status IN ('pending', 'processing', 'completed', 'failed')),
    CONSTRAINT valid_validator_status CHECK (validator_status IN ('pending', 'processing', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_creator_status ON jobs(creator_status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_config_name ON jobs(config_name);
CREATE INDEX IF NOT EXISTS idx_jobs_assigned_agent ON jobs(assigned_agent_id);

-- ============================================================================
-- 2. AGENTS TABLE
-- Tracks registered agent pods for orchestration
-- ============================================================================

CREATE TABLE IF NOT EXISTS agents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Agent identification
    config_name VARCHAR(100) NOT NULL,     -- Agent configuration name
    hostname VARCHAR(255),                  -- Pod/host name
    pod_ip VARCHAR(45),                     -- IPv4 or IPv6, used to send commands to agent
    pod_port INTEGER DEFAULT 8001,          -- Agent API port

    -- Process info
    pid INTEGER,                            -- Process ID (for debugging)

    -- Status tracking
    status VARCHAR(20) NOT NULL DEFAULT 'booting',
    current_job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,

    -- Timestamps
    registered_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_heartbeat TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    -- Extensible metadata
    metadata JSONB DEFAULT '{}',

    CONSTRAINT valid_agent_status CHECK (status IN ('booting', 'ready', 'working', 'completed', 'failed', 'offline'))
);

CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS idx_agents_last_heartbeat ON agents(last_heartbeat);
CREATE INDEX IF NOT EXISTS idx_agents_current_job ON agents(current_job_id);

-- Add FK constraint for jobs.assigned_agent_id now that agents table exists
DO $$ BEGIN
    ALTER TABLE jobs ADD CONSTRAINT fk_jobs_assigned_agent
        FOREIGN KEY (assigned_agent_id) REFERENCES agents(id) ON DELETE SET NULL;
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

-- ============================================================================
-- 3. REQUIREMENTS TABLE
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

    -- ========================================================================
    -- CREATOR FIELDS (filled by Creator Agent)
    -- ========================================================================

    -- Requirement identification
    requirement_id VARCHAR(100),  -- Globally unique ID assigned by Creator

    -- Requirement content
    name VARCHAR(500),            -- Short, content-appropriate designation
    text TEXT NOT NULL,           -- Full requirement description (atomic, verifiable)
    type VARCHAR(100),            -- functional, compliance, constraint, etc.
    priority VARCHAR(50),         -- high, medium, low

    -- Source tracking
    source_document TEXT,         -- Document path/name
    source_location JSONB,        -- {page, section, paragraph, line, marginal_number}

    -- Compliance relevance
    gobd_relevant BOOLEAN DEFAULT FALSE,
    gdpr_relevant BOOLEAN DEFAULT FALSE,

    -- Creator research data
    citations JSONB DEFAULT '[]',      -- Citation IDs linking to citations table
    reasoning TEXT,                    -- Creator's extraction reasoning
    research_notes TEXT,               -- Additional notes from Creator

    -- ========================================================================
    -- VALIDATOR FIELDS (filled by Validator Agent)
    -- ========================================================================

    -- Quality assessment
    quality_score FLOAT,                        -- Numeric quality score (0.0-1.0)
    quality_class VARCHAR(50),                  -- Quality classification (A/B/C or similar)

    -- ISO/IEC/IEEE 29148:2018 evaluation (9 criteria)
    -- Each criterion: necessary, appropriate, unambiguous, complete, singular,
    --                 feasible, verifiable, correct, conforming
    iso_29148_evaluation JSONB,                 -- {criterion: {score, notes}, ...}

    -- Fulfillment assessment against domain model
    fulfillment_status VARCHAR(50),             -- FULFILLED, PARTIALLY_FULFILLED, NOT_FULFILLED, UNCLEAR
    fulfillment_justification TEXT,             -- Explanation for the status

    -- Domain model mapping
    found_model_elements JSONB,                 -- BusinessObjects, attributes, services found
    attribute_quality_assessment JSONB,         -- Attribute quality checks per found element

    -- Graph integration
    neo4j_id VARCHAR(100),                      -- Neo4j node ID after integration
    graph_query TEXT,                           -- Cypher query used for validation/integration

    -- Recommendations
    recommendations TEXT,                       -- Improvement suggestions from Validator

    -- Legacy/compatibility (may be removed in future)
    validation_result JSONB,                    -- Deprecated: use structured fields above
    rejection_reason TEXT,                      -- Deprecated: use fulfillment_justification

    -- ========================================================================
    -- PROCESSING METADATA
    -- ========================================================================

    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    validated_at TIMESTAMP WITH TIME ZONE,

    tags JSONB DEFAULT '[]',

    CONSTRAINT valid_req_status CHECK (status IN ('pending', 'validating', 'integrated', 'rejected', 'failed')),
    CONSTRAINT valid_fulfillment_status CHECK (fulfillment_status IS NULL OR fulfillment_status IN ('FULFILLED', 'PARTIALLY_FULFILLED', 'NOT_FULFILLED', 'UNCLEAR'))
);

CREATE INDEX IF NOT EXISTS idx_requirements_job_id ON requirements(job_id);
CREATE INDEX IF NOT EXISTS idx_requirements_status ON requirements(status);
CREATE INDEX IF NOT EXISTS idx_requirements_neo4j_id ON requirements(neo4j_id);
CREATE INDEX IF NOT EXISTS idx_requirements_created_at ON requirements(created_at);
-- Partial index for efficient polling of unprocessed requirements
CREATE INDEX IF NOT EXISTS idx_requirements_unprocessed ON requirements(job_id, created_at) WHERE neo4j_id IS NULL;

-- ============================================================================
-- 4. CITATION ENGINE TABLES (from CitationEngine)
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
-- Each source belongs to a specific job for isolation between agents
CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    type source_type NOT NULL,
    identifier TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT,
    content TEXT NOT NULL,
    content_hash TEXT,
    metadata JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sources_job_id ON sources(job_id);
CREATE INDEX IF NOT EXISTS idx_sources_identifier ON sources(identifier);
CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(type);
CREATE INDEX IF NOT EXISTS idx_sources_name ON sources(name);

-- Citations table: links claims to their supporting evidence
-- Each citation belongs to a specific job for isolation between agents
CREATE TABLE IF NOT EXISTS citations (
    id SERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
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

-- Schema migrations table (for CitationEngine versioning)
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    description TEXT
);

INSERT INTO schema_migrations (version, description)
VALUES (1, 'Initial schema with sources and citations tables')
ON CONFLICT (version) DO NOTHING;

-- ============================================================================
-- 5. HELPER FUNCTIONS
-- ============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- ============================================================================
-- 6. TRIGGERS
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
-- 7. VIEWS
-- ============================================================================

CREATE OR REPLACE VIEW job_summary AS
SELECT
    j.id,
    j.status,
    j.creator_status,
    j.validator_status,
    j.config_name,
    j.assigned_agent_id,
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
