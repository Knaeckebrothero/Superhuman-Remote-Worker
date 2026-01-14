-- =============================================================================
-- Graph-RAG Autonomous Agent System
-- PostgreSQL Docker Initialization Script
-- =============================================================================
--
-- This script runs automatically when the PostgreSQL container starts for
-- the first time. It creates the database schema required by the agents.
--
-- Note: This is the same as migrations/001_initial_schema.sql but is placed
-- here for Docker's init.d mechanism.
-- =============================================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- 1. JOBS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    prompt TEXT NOT NULL,
    document_path TEXT,
    document_content BYTEA,
    context JSONB DEFAULT '{}',
    status VARCHAR(50) NOT NULL DEFAULT 'created',
    creator_status VARCHAR(50) DEFAULT 'pending',
    validator_status VARCHAR(50) DEFAULT 'pending',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    error_details JSONB,
    total_tokens_used INTEGER DEFAULT 0,
    total_requests INTEGER DEFAULT 0,
    CONSTRAINT valid_status CHECK (status IN ('created', 'processing', 'completed', 'failed', 'cancelled')),
    CONSTRAINT valid_creator_status CHECK (creator_status IN ('pending', 'processing', 'completed', 'failed')),
    CONSTRAINT valid_validator_status CHECK (validator_status IN ('pending', 'processing', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_creator_status ON jobs(creator_status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

-- ============================================================================
-- 2. REQUIREMENT_CACHE TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS requirement_cache (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    candidate_id VARCHAR(100),
    text TEXT NOT NULL,
    name VARCHAR(500),
    type VARCHAR(100),
    priority VARCHAR(50),
    source_document TEXT,
    source_location JSONB,
    gobd_relevant BOOLEAN DEFAULT FALSE,
    gdpr_relevant BOOLEAN DEFAULT FALSE,
    citations JSONB DEFAULT '[]',
    mentioned_objects JSONB DEFAULT '[]',
    mentioned_messages JSONB DEFAULT '[]',
    reasoning TEXT,
    research_notes TEXT,
    confidence FLOAT DEFAULT 0.0,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    validation_result JSONB,
    graph_node_id VARCHAR(100),
    rejection_reason TEXT,
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    validated_at TIMESTAMP WITH TIME ZONE,
    tags JSONB DEFAULT '[]',
    CONSTRAINT valid_req_status CHECK (status IN ('pending', 'validating', 'integrated', 'rejected', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_req_cache_job_id ON requirement_cache(job_id);
CREATE INDEX IF NOT EXISTS idx_req_cache_status ON requirement_cache(status);
CREATE INDEX IF NOT EXISTS idx_req_cache_created_at ON requirement_cache(created_at);
CREATE INDEX IF NOT EXISTS idx_req_cache_pending_poll
ON requirement_cache(job_id, status, created_at)
WHERE status = 'pending';

-- ============================================================================
-- 3. LLM_REQUESTS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS llm_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,
    requirement_id UUID REFERENCES requirement_cache(id) ON DELETE SET NULL,
    agent VARCHAR(50) NOT NULL,
    model VARCHAR(100) NOT NULL,
    messages JSONB NOT NULL,
    tools JSONB,
    temperature FLOAT,
    max_tokens INTEGER,
    response JSONB,
    completion_tokens INTEGER,
    prompt_tokens INTEGER,
    total_tokens INTEGER,
    request_started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    request_completed_at TIMESTAMP WITH TIME ZONE,
    duration_ms INTEGER,
    error BOOLEAN DEFAULT FALSE,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_llm_requests_job_id ON llm_requests(job_id);
CREATE INDEX IF NOT EXISTS idx_llm_requests_agent ON llm_requests(agent);
CREATE INDEX IF NOT EXISTS idx_llm_requests_created_at ON llm_requests(created_at DESC);

-- ============================================================================
-- 4. AGENT_CHECKPOINTS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_checkpoints (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    agent VARCHAR(50) NOT NULL,
    thread_id VARCHAR(100) NOT NULL,
    checkpoint_ns VARCHAR(100) DEFAULT '',
    checkpoint_id VARCHAR(100) NOT NULL,
    parent_checkpoint_id VARCHAR(100),
    checkpoint_data JSONB NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(thread_id, checkpoint_ns, checkpoint_id)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_job_agent ON agent_checkpoints(job_id, agent);
CREATE INDEX IF NOT EXISTS idx_checkpoints_thread ON agent_checkpoints(thread_id);

-- ============================================================================
-- 5. CANDIDATE_WORKSPACE TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS candidate_workspace (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    workspace_type VARCHAR(50) NOT NULL,
    data JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_workspace_job_type ON candidate_workspace(job_id, workspace_type);

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

DROP TRIGGER IF EXISTS update_jobs_updated_at ON jobs;
CREATE TRIGGER update_jobs_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_requirement_cache_updated_at ON requirement_cache;
CREATE TRIGGER update_requirement_cache_updated_at
    BEFORE UPDATE ON requirement_cache
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_workspace_updated_at ON candidate_workspace;
CREATE TRIGGER update_workspace_updated_at
    BEFORE UPDATE ON candidate_workspace
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
    j.prompt,
    j.created_at,
    j.updated_at,
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
-- Grant permissions (if running as root user)
-- ============================================================================

-- Grant all privileges to the graphrag user
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'graphrag') THEN
        GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO graphrag;
        GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO graphrag;
    END IF;
END
$$;

-- Log completion
DO $$
BEGIN
    RAISE NOTICE 'Graph-RAG database schema initialized successfully';
END
$$;
