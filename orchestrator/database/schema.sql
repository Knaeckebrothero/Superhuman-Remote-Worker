-- Graph-RAG Autonomous Agent System
-- PostgreSQL Schema
--
-- This file defines all tables for the Graph-RAG system.
-- Run with: python src/scripts/app_init.py --force-reset
--
-- Tables:
--   users             - User identity (no auth, just display names)
--   sessions          - Session-based authentication
--   projects          - Resource hubs grouping jobs, repos, datasources, members
--   project_members   - User-project membership with roles (owner, editor, viewer)
--   project_repositories - Repositories linked to projects (jobs, source, reference)
--   jobs              - Job tracking and orchestration
--   agents            - Registered agent pods for orchestration
--   requirements      - Primary storage for extracted requirements
--   datasources       - External database connections for agent jobs
--   sources           - Document sources for citations (CitationEngine)
--   job_sources        - Job-source mapping (many-to-many, CitationEngine v2)
--   citations         - Citation records linking claims to sources (CitationEngine)
--   source_annotations - Source annotations (CitationEngine v2)
--   source_tags        - Source tags (CitationEngine v2)
--   source_embeddings  - Vector embeddings for semantic search (CitationEngine v3)
--   schema_migrations - Schema versioning for CitationEngine
--   builder_sessions  - Instruction builder chat sessions
--   builder_messages  - Messages within builder sessions
--   memories          - Agent memory storage with hybrid search (RecallStore)
--
-- Note: LLM logging is handled by MongoDB (llm_archiver.py).
-- Note: Agent checkpointing is handled by LangGraph's AsyncPostgresSaver.
-- Note: Agent workspace is handled by filesystem (workspace_manager.py).

-- ============================================================================
-- EXTENSIONS
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- 0. USERS TABLE
-- Minimal user identity (no auth, just "pick who you are" from a dropdown).
-- ============================================================================

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    display_name TEXT NOT NULL,
    avatar_color VARCHAR(7) DEFAULT '#89b4fa',
    email TEXT UNIQUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Migration: Add email column to existing databases
DO $$ BEGIN
    ALTER TABLE users ADD COLUMN email TEXT UNIQUE;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- ============================================================================
-- 0b. SESSIONS TABLE
-- Session-based authentication for the cockpit.
-- ============================================================================

CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_activity TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    csrf_token TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- ============================================================================
-- 0c. PROJECTS TABLE
-- Resource hub grouping jobs, repositories, datasources, and members.
-- ============================================================================

CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    description TEXT,
    goal TEXT,
    status VARCHAR(50) DEFAULT 'active',
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    default_config_name VARCHAR(100),
    default_config_override JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT valid_project_status CHECK (status IN ('active', 'paused', 'completed', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);

-- ============================================================================
-- 0d. PROJECT MEMBERS TABLE
-- Maps users to projects with roles (owner, editor, viewer).
-- ============================================================================

CREATE TABLE IF NOT EXISTS project_members (
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(50) NOT NULL DEFAULT 'editor',
    added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project_id, user_id),

    CONSTRAINT valid_member_role CHECK (role IN ('owner', 'editor', 'viewer'))
);

CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id);

-- ============================================================================
-- 0e. PROJECT REPOSITORIES TABLE
-- Repositories linked to a project (jobs, source, reference).
-- ============================================================================

CREATE TABLE IF NOT EXISTS project_repositories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    repo_url TEXT NOT NULL,
    credentials JSONB DEFAULT '{}',
    role VARCHAR(50) NOT NULL DEFAULT 'source',
    read_only BOOLEAN NOT NULL DEFAULT FALSE,
    is_managed BOOLEAN NOT NULL DEFAULT FALSE,
    branch TEXT DEFAULT 'main',
    clone_path TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT valid_repo_role CHECK (role IN ('jobs', 'source', 'reference')),
    CONSTRAINT chk_reference_read_only CHECK (role != 'reference' OR read_only = TRUE)
);

CREATE INDEX IF NOT EXISTS idx_project_repos_project ON project_repositories(project_id);

-- Exactly one jobs repo per project
CREATE UNIQUE INDEX IF NOT EXISTS uq_project_jobs_repo ON project_repositories(project_id) WHERE role = 'jobs';

-- Migration: Add default_project_id to users table
DO $$ BEGIN
    ALTER TABLE users ADD COLUMN default_project_id UUID REFERENCES projects(id);
EXCEPTION WHEN duplicate_column THEN null;
END $$;

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
    resolved_config JSONB DEFAULT NULL,
    assigned_agent_id UUID,  -- FK added after agents table creation

    -- Scheduling
    priority INTEGER NOT NULL DEFAULT 5,

    CONSTRAINT valid_status CHECK (status IN ('created', 'processing', 'completed', 'failed', 'cancelled', 'pending_review', 'paused', 'reviewing', 'waiting')),
    CONSTRAINT valid_creator_status CHECK (creator_status IN ('pending', 'processing', 'completed', 'failed')),
    CONSTRAINT valid_validator_status CHECK (validator_status IN ('pending', 'processing', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_creator_status ON jobs(creator_status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_config_name ON jobs(config_name);
CREATE INDEX IF NOT EXISTS idx_jobs_assigned_agent ON jobs(assigned_agent_id);

-- Migration: Add resolved_config column to existing databases
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN resolved_config JSONB DEFAULT NULL;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add user_id FK to jobs table
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN user_id UUID REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id);

-- Migration: Add project_id FK to jobs table (nullable for existing jobs)
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN project_id UUID REFERENCES projects(id);
EXCEPTION WHEN duplicate_column THEN null;
END $$;
CREATE INDEX IF NOT EXISTS idx_jobs_project_id ON jobs(project_id);

-- Migration: Add branch_name to jobs table
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN branch_name VARCHAR(200);
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add merge_status to jobs table
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN merge_status VARCHAR(50);
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add repo_merge_statuses to jobs table
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN repo_merge_statuses JSONB DEFAULT '{}';
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add freeze_data column to jobs table
-- Stores freeze/completion JSON in DB so endpoints don't depend on Gitea file reads.
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN freeze_data JSONB DEFAULT NULL;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add parent_job_id column to jobs table
-- Supports job hierarchy: verification/critic jobs reference the job that spawned them.
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN parent_job_id UUID REFERENCES jobs(id) DEFAULT NULL;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
CREATE INDEX IF NOT EXISTS idx_jobs_parent_job_id ON jobs(parent_job_id);

-- Migration: Add priority column to jobs table (for priority queue scheduling)
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 5;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
CREATE INDEX IF NOT EXISTS idx_jobs_priority ON jobs(priority DESC);

-- Migration: Add repo_name column to jobs table (per-job Gitea repos)
-- Stores the Gitea repo name for root jobs (e.g. "job-ec38de5d").
-- Subjobs inherit the root job's repo and work on branches.
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN repo_name VARCHAR(200);
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add 'paused' to valid job statuses
-- (CHECK constraint is defined in CREATE TABLE; this handles existing databases)
DO $$ BEGIN
    ALTER TABLE jobs DROP CONSTRAINT IF EXISTS valid_status;
    ALTER TABLE jobs ADD CONSTRAINT valid_status
        CHECK (status IN ('created', 'processing', 'completed', 'failed', 'cancelled', 'pending_review', 'paused'));
END $$;

-- Migration: Add 'reviewing' and 'waiting' statuses for critic feedback loop
-- reviewing = main job actively being reviewed by critic
-- waiting = critic job waiting for main job to address feedback
DO $$ BEGIN
    ALTER TABLE jobs DROP CONSTRAINT IF EXISTS valid_status;
    ALTER TABLE jobs ADD CONSTRAINT valid_status
        CHECK (status IN ('created', 'processing', 'completed', 'failed',
                          'cancelled', 'pending_review', 'paused',
                          'reviewing', 'waiting'));
END $$;

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
    last_completed_at TIMESTAMP WITH TIME ZONE,  -- Set on job completion (not pause), used for cooldown

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

-- Migration: Add last_completed_at to agents table (for dispatch cooldown)
DO $$ BEGIN
    ALTER TABLE agents ADD COLUMN last_completed_at TIMESTAMP WITH TIME ZONE;
EXCEPTION WHEN duplicate_column THEN null;
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
-- Sources are shared across jobs via the job_sources join table (CitationEngine v2)
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

-- Unique constraint on content_hash for deduplication
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

-- Job-source mapping (many-to-many, shared source library)
CREATE TABLE IF NOT EXISTS job_sources (
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    added_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (job_id, source_id)
);

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

-- Full-text search indexes (CitationEngine v2)
CREATE INDEX IF NOT EXISTS idx_citations_claim_fts ON citations USING GIN (to_tsvector('english', claim));
CREATE INDEX IF NOT EXISTS idx_sources_content_fts ON sources USING GIN (to_tsvector('simple', content));

-- Source annotations table (CitationEngine v2)
CREATE TABLE IF NOT EXISTS source_annotations (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id UUID NOT NULL,
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

-- Source tags table (CitationEngine v2)
CREATE TABLE IF NOT EXISTS source_tags (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id UUID NOT NULL,
    tag TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE(source_id, job_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_tags_tag ON source_tags(tag);
CREATE INDEX IF NOT EXISTS idx_tags_job ON source_tags(job_id);

-- Schema migrations table (for CitationEngine versioning)
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    description TEXT
);

-- Record v1 and v2 as already applied (schema above includes both)
INSERT INTO schema_migrations (version, description)
VALUES (1, 'Initial schema with sources and citations tables')
ON CONFLICT (version) DO NOTHING;

INSERT INTO schema_migrations (version, description)
VALUES (2, 'Shared source library with annotations and tags')
ON CONFLICT (version) DO NOTHING;

-- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
-- CitationEngine v3: Source Embeddings (pgvector)
-- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS source_embeddings (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id UUID NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_text TEXT NOT NULL,
    embedding vector(1536),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE(source_id, job_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_source_embeddings_source ON source_embeddings(source_id);
CREATE INDEX IF NOT EXISTS idx_source_embeddings_job ON source_embeddings(job_id);

-- HNSW index for cosine similarity search
CREATE INDEX IF NOT EXISTS idx_source_embeddings_vector ON source_embeddings
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

INSERT INTO schema_migrations (version, description)
VALUES (3, 'Vector search with source embeddings')
ON CONFLICT (version) DO NOTHING;

-- ============================================================================
-- 5. DATASOURCES TABLE
-- External database connections that agents can use during job execution.
-- See docs/datasources.md for the full connector system design.
-- ============================================================================

CREATE TABLE IF NOT EXISTS datasources (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- User-provided metadata
    name TEXT NOT NULL,                    -- Label (e.g. "Production Analytics DB")
    description TEXT,                      -- What this datasource contains (included in agent context)

    -- Connection details
    type TEXT NOT NULL,                    -- 'postgresql', 'neo4j', 'mongodb'
    connection_url TEXT NOT NULL,          -- Full connection string
    credentials JSONB DEFAULT '{}',       -- Additional auth details beyond the URL

    -- Access control
    read_only BOOLEAN NOT NULL DEFAULT TRUE,

    -- Scope: NULL = global (available to all jobs), UUID = job-specific
    job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Migration: Add project_id to datasources table
DO $$ BEGIN
    ALTER TABLE datasources ADD COLUMN project_id UUID REFERENCES projects(id);
EXCEPTION WHEN duplicate_column THEN null;
END $$;
CREATE INDEX IF NOT EXISTS idx_datasources_project_id ON datasources(project_id);

-- Three-level scope uniqueness: one datasource of each type per (job, project, global).
-- Drop old index if it exists, then create the new scope-aware one.
DROP INDEX IF EXISTS uq_datasource_type_job;
CREATE UNIQUE INDEX IF NOT EXISTS uq_datasource_type_scope ON datasources (
    type,
    COALESCE(job_id, '00000000-0000-0000-0000-000000000000'),
    COALESCE(project_id, '00000000-0000-0000-0000-000000000000')
);

CREATE INDEX IF NOT EXISTS idx_datasources_type ON datasources(type);
CREATE INDEX IF NOT EXISTS idx_datasources_job_id ON datasources(job_id);

-- ============================================================================
-- 6. BUILDER TABLES (Instruction Builder Chat)
-- Chat sessions for the AI-powered instruction builder in the cockpit.
-- ============================================================================

-- Chat sessions for the instruction builder
CREATE TABLE IF NOT EXISTS builder_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID,                        -- NULL at creation, set after job submission via update
    expert_id VARCHAR(100),             -- expert used as starting point (nullable)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    summary TEXT                         -- auto-summary of older messages (for context compaction)
);
-- No FK on job_id: the job may not exist yet (lazy linking after submission)

-- Migration: Add user_id FK to builder_sessions table
DO $$ BEGIN
    ALTER TABLE builder_sessions ADD COLUMN user_id UUID REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
CREATE INDEX IF NOT EXISTS idx_builder_sessions_user_id ON builder_sessions(user_id);

-- Chat messages within a builder session
CREATE TABLE IF NOT EXISTS builder_messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID REFERENCES builder_sessions(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,          -- 'user', 'assistant'
    content TEXT,                        -- conversational text
    tool_calls JSONB,                   -- structured artifact mutations (assistant only)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for efficient message retrieval
CREATE INDEX IF NOT EXISTS idx_builder_messages_session ON builder_messages(session_id, created_at);

-- ============================================================================
-- 7. MEMORIES TABLE (Memory Light — RecallStore)
-- Agent memory storage with hybrid search (dense vector + sparse keyword + recency).
-- See docs/features/memory_light.md for full architecture.
-- ============================================================================

CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    agent_id VARCHAR(100),

    -- Content
    content TEXT NOT NULL,
    summary VARCHAR(500),

    -- Classification
    memory_type VARCHAR(50) DEFAULT 'factual',
    source VARCHAR(50) DEFAULT 'observer',

    -- Search channels
    keywords TEXT[] DEFAULT '{}',
    embedding vector(1536),
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
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 256);

-- Hybrid search function: RRF-based fusion of dense, sparse, and recency channels
CREATE OR REPLACE FUNCTION memory_hybrid_search(
    query_text text,
    query_embedding vector(1536),
    job_id_param uuid,
    match_count int DEFAULT 10,
    dense_weight float DEFAULT 0.6,
    sparse_weight float DEFAULT 0.3,
    recency_weight float DEFAULT 0.1,
    rrf_k int DEFAULT 50
) RETURNS SETOF memories LANGUAGE sql AS $$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> query_embedding) AS rank_ix
    FROM memories WHERE job_id = job_id_param AND embedding IS NOT NULL
    ORDER BY rank_ix LIMIT match_count * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(sparse_keywords, websearch_to_tsquery('english', query_text)) DESC) AS rank_ix
    FROM memories WHERE job_id = job_id_param AND sparse_keywords @@ websearch_to_tsquery('english', query_text)
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE job_id = job_id_param
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
-- 8. HELPER FUNCTIONS
-- ============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- ============================================================================
-- 9. TRIGGERS
-- ============================================================================

DROP TRIGGER IF EXISTS update_jobs_updated_at ON jobs;
CREATE TRIGGER update_jobs_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_requirements_updated_at ON requirements;
CREATE TRIGGER update_requirements_updated_at
    BEFORE UPDATE ON requirements
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_datasources_updated_at ON datasources;
CREATE TRIGGER update_datasources_updated_at
    BEFORE UPDATE ON datasources
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_builder_sessions_updated_at ON builder_sessions;
CREATE TRIGGER update_builder_sessions_updated_at
    BEFORE UPDATE ON builder_sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_projects_updated_at ON projects;
CREATE TRIGGER update_projects_updated_at
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_project_repositories_updated_at ON project_repositories;
CREATE TRIGGER update_project_repositories_updated_at
    BEFORE UPDATE ON project_repositories
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- 10. VIEWS
-- ============================================================================

-- Drop and recreate: adding project columns requires view recreation
DROP VIEW IF EXISTS job_summary;
CREATE OR REPLACE VIEW job_summary AS
SELECT
    j.id,
    j.status,
    j.creator_status,
    j.validator_status,
    j.config_name,
    j.assigned_agent_id,
    j.user_id,
    j.project_id,
    j.parent_job_id,
    j.priority,
    j.branch_name,
    j.repo_name,
    j.merge_status,
    j.freeze_data,
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
