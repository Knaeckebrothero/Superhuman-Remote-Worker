-- Superhuman Remote Worker — Autonomous Agent System
-- PostgreSQL Schema
--
-- This file defines all tables for the Superhuman Remote Worker system.
-- Run with: python src/scripts/app_init.py --force-reset
--
-- Tables:
--   users             - User identity with optional password authentication
--   sessions          - Session-based authentication
--   auth_tokens       - Email verification and password reset tokens
--   projects          - Resource hubs grouping jobs, repos, datasources, members
--   project_members   - User-project membership with roles (owner, editor, viewer)
--   project_repositories - Repositories linked to projects (jobs, source, reference)
--   jobs              - Job tracking and orchestration
--   agents            - Registered agent pods for orchestration
--   requirements      - Primary storage for extracted requirements
--   datasources       - External database connections for agent jobs
--   builder_sessions  - Instruction builder chat sessions
--   builder_messages  - Messages within builder sessions
--
-- Note: Citation Engine tables (sources, citations, etc.) and vector tables (memories, knowledge_index) are in the Vector DB (vector_schema.sql).
-- Note: LLM logging is handled by MongoDB (llm_archiver.py).
-- Note: Agent checkpointing is handled by LangGraph's AsyncPostgresSaver.
-- Note: Agent workspace is handled by filesystem (workspace_manager.py).

-- ============================================================================
-- EXTENSIONS
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- 0. USERS TABLE
-- User identity with password-based authentication.
-- password_hash and email_verified are required for login.
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

-- Migration: Add password and verification columns to users table
DO $$ BEGIN
    ALTER TABLE users ADD COLUMN password_hash TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE users ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add is_admin flag to users table
DO $$ BEGIN
    ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- ============================================================================
-- 0e. AUTH TOKENS TABLE
-- Verification codes and password reset tokens for production auth mode.
-- ============================================================================

CREATE TABLE IF NOT EXISTS auth_tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    token_type VARCHAR(20) NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    used_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT valid_token_type CHECK (token_type IN ('verification', 'password_reset'))
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_token ON auth_tokens(token);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_email ON auth_tokens(email);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_expires ON auth_tokens(expires_at);

-- ============================================================================
-- 0g. MCP TOKENS TABLE
-- API tokens for MCP server authentication. Users generate tokens via the
-- cockpit settings page; the MCP server validates them on each request.
-- ============================================================================

CREATE TABLE IF NOT EXISTS mcp_tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix VARCHAR(12) NOT NULL,
    scope TEXT NOT NULL DEFAULT 'user',
    expires_at TIMESTAMP WITH TIME ZONE,
    revoked_at TIMESTAMP WITH TIME ZONE,
    last_used_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mcp_tokens_user ON mcp_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_hash ON mcp_tokens(token_hash);

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

-- NOTE: The NOT NULL constraint on users.default_project_id is applied by
-- init.py after seeding default projects, to avoid ordering issues.

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
-- 4. DATASOURCES TABLE
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
-- 5. BUILDER TABLES (Instruction Builder Chat)
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
    steps JSONB,                        -- agent reasoning steps (assistant only)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Migration: Add steps column to builder_messages table
DO $$ BEGIN
    ALTER TABLE builder_messages ADD COLUMN steps JSONB;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Index for efficient message retrieval
CREATE INDEX IF NOT EXISTS idx_builder_messages_session ON builder_messages(session_id, created_at);

-- ============================================================================
-- 6. HELPER FUNCTIONS
-- ============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- ============================================================================
-- 7. TRIGGERS
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
-- 8. SUDO APPROVAL GATE
-- ============================================================================

-- Status enum for sudo approval requests
DO $$ BEGIN
    CREATE TYPE sudo_request_status AS ENUM (
        'pending', 'approved', 'denied', 'expired', 'auto_approved', 'auto_denied'
    );
EXCEPTION WHEN duplicate_object THEN null;
END $$;

CREATE TABLE IF NOT EXISTS sudo_approval_requests (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id              UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    vm_name             VARCHAR(255) NOT NULL,
    command             TEXT NOT NULL,
    arguments           TEXT[] DEFAULT '{}',
    working_directory   TEXT,
    requesting_user     VARCHAR(255) NOT NULL,
    target_user         VARCHAR(255) NOT NULL DEFAULT 'root',
    status              sudo_request_status NOT NULL DEFAULT 'pending',
    requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at          TIMESTAMPTZ,
    decided_by          VARCHAR(255),
    decision_reason     TEXT,
    ttl_seconds         INTEGER NOT NULL DEFAULT 300,
    expires_at          TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '300 seconds'),
    nats_reply_subject  TEXT,
    metadata            JSONB DEFAULT '{}'
);

-- Hot path: UI polling and SSE push for pending requests
CREATE INDEX IF NOT EXISTS idx_sudo_pending
    ON sudo_approval_requests (status, requested_at DESC)
    WHERE status = 'pending';
-- Job-scoped views in cockpit job detail panel
CREATE INDEX IF NOT EXISTS idx_sudo_job
    ON sudo_approval_requests (job_id, requested_at DESC);
-- Expiration sweeper: find pending requests past their TTL
CREATE INDEX IF NOT EXISTS idx_sudo_expiry
    ON sudo_approval_requests (expires_at)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS sudo_auto_rules (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    pattern     TEXT NOT NULL,
    action      VARCHAR(20) NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 100,
    description TEXT,
    created_by  VARCHAR(255),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT valid_action CHECK (action IN ('approve', 'deny', 'review'))
);

CREATE INDEX IF NOT EXISTS idx_sudo_rules_active
    ON sudo_auto_rules (priority ASC)
    WHERE enabled = TRUE;

-- ============================================================================
-- 9. VIEWS
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
