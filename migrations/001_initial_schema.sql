-- Graph-RAG Autonomous Agent System
-- Initial PostgreSQL Schema
-- Version: 1.0
-- Based on: masterplan.md v1.1

-- Enable UUID extension
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
    document_content BYTEA,  -- Store document binary if needed
    context JSONB DEFAULT '{}',

    -- Status tracking
    status VARCHAR(50) NOT NULL DEFAULT 'created',
    -- Possible values: created, processing, completed, failed, cancelled

    creator_status VARCHAR(50) DEFAULT 'pending',
    -- Possible values: pending, processing, completed, failed

    validator_status VARCHAR(50) DEFAULT 'pending',
    -- Possible values: pending, processing, completed, failed

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

    -- Indexes for common queries
    CONSTRAINT valid_status CHECK (status IN ('created', 'processing', 'completed', 'failed', 'cancelled')),
    CONSTRAINT valid_creator_status CHECK (creator_status IN ('pending', 'processing', 'completed', 'failed')),
    CONSTRAINT valid_validator_status CHECK (validator_status IN ('pending', 'processing', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_creator_status ON jobs(creator_status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

-- ============================================================================
-- 2. REQUIREMENT_CACHE TABLE
-- Shared queue between Creator and Validator agents
-- ============================================================================

CREATE TABLE IF NOT EXISTS requirement_cache (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,

    -- Requirement data
    candidate_id VARCHAR(100),  -- Original candidate ID from Creator
    text TEXT NOT NULL,
    name VARCHAR(500),
    type VARCHAR(100),  -- functional, compliance, constraint, etc.
    priority VARCHAR(50),  -- high, medium, low

    -- Source tracking
    source_document TEXT,
    source_location JSONB,  -- {article, paragraph, page, section}

    -- GoBD/GDPR relevance
    gobd_relevant BOOLEAN DEFAULT FALSE,
    gdpr_relevant BOOLEAN DEFAULT FALSE,

    -- Research data
    citations JSONB DEFAULT '[]',  -- List of citation IDs
    mentioned_objects JSONB DEFAULT '[]',  -- BusinessObject references
    mentioned_messages JSONB DEFAULT '[]',  -- Message references
    reasoning TEXT,
    research_notes TEXT,
    confidence FLOAT DEFAULT 0.0,

    -- Processing status
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- Possible values: pending, validating, integrated, rejected, failed

    -- Validator output
    validation_result JSONB,
    graph_node_id VARCHAR(100),  -- rid if integrated
    rejection_reason TEXT,

    -- Retry tracking
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,

    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    validated_at TIMESTAMP WITH TIME ZONE,

    -- Requirement tagging for traceability
    tags JSONB DEFAULT '[]',

    CONSTRAINT valid_req_status CHECK (status IN ('pending', 'validating', 'integrated', 'rejected', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_req_cache_job_id ON requirement_cache(job_id);
CREATE INDEX IF NOT EXISTS idx_req_cache_status ON requirement_cache(status);
CREATE INDEX IF NOT EXISTS idx_req_cache_created_at ON requirement_cache(created_at);

-- Compound index for validator polling
CREATE INDEX IF NOT EXISTS idx_req_cache_pending_poll
ON requirement_cache(job_id, status, created_at)
WHERE status = 'pending';

-- ============================================================================
-- 3. LLM_REQUESTS TABLE
-- Logs all LLM API requests for analysis and debugging
-- ============================================================================

CREATE TABLE IF NOT EXISTS llm_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,
    requirement_id UUID REFERENCES requirement_cache(id) ON DELETE SET NULL,

    -- Request metadata
    agent VARCHAR(50) NOT NULL,  -- creator, validator, orchestrator
    model VARCHAR(100) NOT NULL,

    -- Request data
    messages JSONB NOT NULL,
    tools JSONB,
    temperature FLOAT,
    max_tokens INTEGER,

    -- Response data
    response JSONB,
    completion_tokens INTEGER,
    prompt_tokens INTEGER,
    total_tokens INTEGER,

    -- Timing
    request_started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    request_completed_at TIMESTAMP WITH TIME ZONE,
    duration_ms INTEGER,

    -- Error tracking
    error BOOLEAN DEFAULT FALSE,
    error_message TEXT,

    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_llm_requests_job_id ON llm_requests(job_id);
CREATE INDEX IF NOT EXISTS idx_llm_requests_agent ON llm_requests(agent);
CREATE INDEX IF NOT EXISTS idx_llm_requests_created_at ON llm_requests(created_at DESC);

-- ============================================================================
-- 4. AGENT_CHECKPOINTS TABLE
-- Stores agent state for recovery from failures
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_checkpoints (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    agent VARCHAR(50) NOT NULL,  -- creator, validator

    -- Checkpoint data
    thread_id VARCHAR(100) NOT NULL,  -- LangGraph thread ID
    checkpoint_ns VARCHAR(100) DEFAULT '',
    checkpoint_id VARCHAR(100) NOT NULL,
    parent_checkpoint_id VARCHAR(100),

    -- State
    checkpoint_data JSONB NOT NULL,
    metadata JSONB DEFAULT '{}',

    -- Timing
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    -- Ensure unique checkpoint per thread
    UNIQUE(thread_id, checkpoint_ns, checkpoint_id)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_job_agent ON agent_checkpoints(job_id, agent);
CREATE INDEX IF NOT EXISTS idx_checkpoints_thread ON agent_checkpoints(thread_id);

-- ============================================================================
-- 5. CANDIDATE_WORKSPACE TABLE
-- Temporary storage for Creator Agent's working data
-- ============================================================================

CREATE TABLE IF NOT EXISTS candidate_workspace (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,

    -- Workspace type
    workspace_type VARCHAR(50) NOT NULL,  -- chunks, candidates, research, todo

    -- Data
    data JSONB NOT NULL,

    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_workspace_job_type ON candidate_workspace(job_id, workspace_type);

-- ============================================================================
-- 6. HELPER FUNCTIONS
-- ============================================================================

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply trigger to tables with updated_at
CREATE TRIGGER update_jobs_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_requirement_cache_updated_at
    BEFORE UPDATE ON requirement_cache
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_workspace_updated_at
    BEFORE UPDATE ON candidate_workspace
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- 7. VIEWS FOR COMMON QUERIES
-- ============================================================================

-- Job summary view
CREATE OR REPLACE VIEW job_summary AS
SELECT
    j.id,
    j.status,
    j.creator_status,
    j.validator_status,
    j.created_at,
    j.completed_at,
    COUNT(DISTINCT rc.id) FILTER (WHERE rc.status = 'pending') as pending_requirements,
    COUNT(DISTINCT rc.id) FILTER (WHERE rc.status = 'validating') as validating_requirements,
    COUNT(DISTINCT rc.id) FILTER (WHERE rc.status = 'integrated') as integrated_requirements,
    COUNT(DISTINCT rc.id) FILTER (WHERE rc.status = 'rejected') as rejected_requirements,
    COUNT(DISTINCT rc.id) FILTER (WHERE rc.status = 'failed') as failed_requirements,
    j.total_tokens_used,
    j.total_requests
FROM jobs j
LEFT JOIN requirement_cache rc ON j.id = rc.job_id
GROUP BY j.id;

-- Agent activity view
CREATE OR REPLACE VIEW agent_activity AS
SELECT
    agent,
    DATE_TRUNC('hour', created_at) as hour,
    COUNT(*) as request_count,
    SUM(total_tokens) as tokens_used,
    AVG(duration_ms) as avg_duration_ms
FROM llm_requests
WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '24 hours'
GROUP BY agent, DATE_TRUNC('hour', created_at)
ORDER BY hour DESC, agent;

-- ============================================================================
-- NOTES
-- ============================================================================
--
-- Citation Engine tables (sources, citations) are created automatically
-- by the Citation Engine package when initialized. They share this same
-- PostgreSQL database.
--
-- To run this migration:
-- psql -h localhost -U graphrag -d graphrag -f migrations/001_initial_schema.sql
--
