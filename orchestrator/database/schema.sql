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

-- Migration: Add keycloak_sub for OIDC user linking (SSO Phase 2)
DO $$ BEGIN
    ALTER TABLE users ADD COLUMN keycloak_sub TEXT UNIQUE;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
CREATE INDEX IF NOT EXISTS idx_users_keycloak_sub ON users(keycloak_sub);

-- Migration: Add settings JSONB for user preferences (model, autonomy, etc.)
DO $$ BEGIN
    ALTER TABLE users ADD COLUMN settings JSONB DEFAULT '{}';
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
    origin TEXT,  -- NULL='manual', 'oauth:Claude', 'oauth:ChatGPT', etc.
    expires_at TIMESTAMP WITH TIME ZONE,
    revoked_at TIMESTAMP WITH TIME ZONE,
    last_used_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mcp_tokens_user ON mcp_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_hash ON mcp_tokens(token_hash);

-- ============================================================================
-- 0h. USER API KEYS TABLE
-- Per-user API keys for LLM and tool providers.
-- Resolution chain at dispatch: user key > project key > env var.
-- ============================================================================

CREATE TABLE IF NOT EXISTS user_api_keys (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider VARCHAR(50) NOT NULL,
    api_key TEXT NOT NULL,
    key_prefix VARCHAR(12) NOT NULL,
    label TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT valid_user_api_key_provider CHECK (provider IN (
        'openai', 'anthropic', 'google', 'groq', 'openrouter', 'tavily', 'vision'
    ))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_api_keys_provider ON user_api_keys(user_id, provider);
CREATE INDEX IF NOT EXISTS idx_user_api_keys_user ON user_api_keys(user_id);

-- ============================================================================
-- 0i. USER LLM ENDPOINTS
-- Per-user OpenAI-compatible LLM endpoints (vLLM, Ollama, private gateways)
-- and the model IDs they serve. Replaces the single-LLM_BASE_URL mechanism.
-- Custom-endpoint keys travel inline on the endpoint row — they are not
-- merged into resolve_api_keys_for_job() which only covers named providers.
-- ============================================================================

-- user_id is NULL for system-scoped rows (seeded by helm or created via
-- Admin → Providers); non-NULL for per-user rows.
CREATE TABLE IF NOT EXISTS user_llm_endpoints (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_key TEXT,
    key_prefix VARCHAR(12),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Drop the plain unique constraint if an older deployment still has it, and
-- replace it with two partial indexes so user rows are unique-per-user while
-- system rows (user_id IS NULL) are globally unique on label.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_user_llm_endpoint_label'
          AND conrelid = 'user_llm_endpoints'::regclass
    ) THEN
        ALTER TABLE user_llm_endpoints DROP CONSTRAINT uq_user_llm_endpoint_label;
    END IF;
END$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'user_llm_endpoints'::regclass
          AND attname = 'user_id'
          AND attnotnull = TRUE
    ) THEN
        ALTER TABLE user_llm_endpoints ALTER COLUMN user_id DROP NOT NULL;
    END IF;
END$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_llm_endpoint_label_user
    ON user_llm_endpoints(user_id, label)
    WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_llm_endpoint_label_system
    ON user_llm_endpoints(label)
    WHERE user_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_user_llm_endpoints_user ON user_llm_endpoints(user_id);

CREATE TABLE IF NOT EXISTS user_llm_endpoint_models (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    endpoint_id UUID NOT NULL REFERENCES user_llm_endpoints(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    family TEXT,
    context_window INT,
    reasoning_level TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_endpoint_model UNIQUE (endpoint_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_user_llm_endpoint_models_endpoint ON user_llm_endpoint_models(endpoint_id);
CREATE INDEX IF NOT EXISTS idx_user_llm_endpoint_models_id ON user_llm_endpoint_models(model_id);

-- ============================================================================
-- 0j. SYSTEM API KEYS
-- Provider-level API keys not tied to a specific user. Consulted by the
-- resolver after user and project keys; replaces the env-var fallback.
-- Seeded by helm on fresh install; mutable via /api/admin/providers/keys.
-- ============================================================================

CREATE TABLE IF NOT EXISTS system_api_keys (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider VARCHAR(50) NOT NULL UNIQUE,
    api_key TEXT NOT NULL,
    key_prefix VARCHAR(12) NOT NULL,
    label TEXT,
    seeded_from TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT valid_system_api_key_provider CHECK (provider IN (
        'openai', 'anthropic', 'google', 'groq', 'openrouter', 'tavily', 'vision'
    ))
);

-- Default LLM model IDs (builder, browser, citation) piggy-back on the
-- existing `system_settings` table defined in section 9d. Keys follow the
-- convention ``llm.default_<kind>_model`` with JSONB value ``{"model": "..."}``.
-- See db.get_default_llm_model() / db.set_default_llm_model().

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

-- Migration: Nextcloud Group Folder ID (NULL for personal/default projects)
-- LEGACY: superseded by main_cloud_backend + main_cloud_folder_handle below.
-- Kept for one release as a read-only fallback; dropped per §9 of the design doc.
DO $$ BEGIN
    ALTER TABLE projects ADD COLUMN nextcloud_folder_id INTEGER;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Cloud storage read-only default for the project
DO $$ BEGIN
    ALTER TABLE projects ADD COLUMN cloud_storage_read_only BOOLEAN NOT NULL DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Main cloud abstraction — pluggable backend per project
-- (see docs/features/main_cloud_abstraction.md §4.4 and §6 for the
-- non-destructive switching rule).
DO $$ BEGIN
    ALTER TABLE projects ADD COLUMN main_cloud_backend TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE projects ADD COLUMN main_cloud_folder_handle TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Backfill from the legacy column: every existing Nextcloud-backed project
-- gets main_cloud_backend='nextcloud' + main_cloud_folder_handle set to the
-- old integer folder id stringified. The handle's vendor_meta (mountpoint) is
-- re-derived at read time by NextcloudBackend when the row's mountpoint is
-- needed, so the backfill does not need to recompute it.
UPDATE projects
   SET main_cloud_backend = 'nextcloud',
       main_cloud_folder_handle = nextcloud_folder_id::text
 WHERE nextcloud_folder_id IS NOT NULL
   AND main_cloud_folder_handle IS NULL;

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
-- 0i. PROJECT API KEYS TABLE
-- Per-project API keys (fallback when user has no key for a provider).
-- ============================================================================

CREATE TABLE IF NOT EXISTS project_api_keys (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    provider VARCHAR(50) NOT NULL,
    api_key TEXT NOT NULL,
    key_prefix VARCHAR(12) NOT NULL,
    label TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT valid_project_api_key_provider CHECK (provider IN (
        'openai', 'anthropic', 'google', 'groq', 'openrouter', 'tavily', 'vision'
    ))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_project_api_keys_provider ON project_api_keys(project_id, provider);
CREATE INDEX IF NOT EXISTS idx_project_api_keys_project ON project_api_keys(project_id);

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

    CONSTRAINT valid_status CHECK (status IN ('created', 'processing', 'completed', 'failed', 'cancelled', 'pending_review', 'paused', 'reviewing', 'waiting', 'waiting_for_reply'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
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

-- Migration: Add creation_order to jobs table (subagent delegation merge ordering)
-- 0-based index within sibling group; NULL for non-delegation jobs (critic/scholar).
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN creation_order SMALLINT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add worktree_path to jobs table (git worktree location for delegation subagents)
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN worktree_path VARCHAR(500);
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add delegation_context to jobs table (shared context string from parent)
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN delegation_context TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add 'reviewing' and 'waiting' statuses for critic feedback loop
-- reviewing = main job actively being reviewed by critic
-- waiting = critic job waiting for main job to address feedback
DO $$ BEGIN
    ALTER TABLE jobs DROP CONSTRAINT IF EXISTS valid_status;
    ALTER TABLE jobs ADD CONSTRAINT valid_status
        CHECK (status IN ('created', 'processing', 'completed', 'failed',
                          'cancelled', 'pending_review', 'paused',
                          'reviewing', 'waiting', 'waiting_for_reply'));
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

    CONSTRAINT valid_agent_status CHECK (status IN ('booting', 'available', 'ready', 'working', 'draining', 'completed', 'failed', 'offline'))
);

-- Migration: Add 'draining' to valid_agent_status constraint (graceful shutdown)
DO $$ BEGIN
    ALTER TABLE agents DROP CONSTRAINT valid_agent_status;
    ALTER TABLE agents ADD CONSTRAINT valid_agent_status
        CHECK (status IN ('booting', 'ready', 'working', 'session', 'draining', 'completed', 'failed', 'offline'));
EXCEPTION WHEN undefined_object THEN null;
END $$;

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

-- Migration: Add agent_mode column for persistent agent support
DO
$$
BEGIN
ALTER TABLE agents
    ADD COLUMN agent_mode VARCHAR(20) NOT NULL DEFAULT 'worker';
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add thread_id column for persistent agent sessions
DO
$$
BEGIN
ALTER TABLE agents
    ADD COLUMN thread_id UUID;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
CREATE INDEX IF NOT EXISTS idx_agents_thread_id ON agents(thread_id);
CREATE INDEX IF NOT EXISTS idx_agents_mode ON agents(agent_mode);

-- ============================================================================
-- 4b. THREADS TABLE
-- Interactive persistent agent sessions (parallel to jobs for workers).
-- ============================================================================

CREATE TABLE IF NOT EXISTS threads
(
    id
    UUID
    PRIMARY
    KEY
    DEFAULT
    uuid_generate_v4
(
),

    -- Session identification
    title TEXT DEFAULT 'Untitled Session',

    -- Owner
    user_id UUID REFERENCES users
(
    id
) ON DELETE SET NULL,
    project_id UUID REFERENCES projects
(
    id
)
  ON DELETE SET NULL,

    -- Agent binding
    agent_id UUID REFERENCES agents
(
    id
)
  ON DELETE SET NULL,

    -- Status
    status VARCHAR
(
    20
) NOT NULL DEFAULT 'created',

    -- Permission mode
    permission_mode VARCHAR
(
    20
) NOT NULL DEFAULT 'supervised',

    -- Config used for this session
    config_name VARCHAR
(
    100
),

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP
  WITH TIME ZONE,

      -- Extensible metadata
      metadata JSONB DEFAULT '{}',
      CONSTRAINT valid_thread_status CHECK (
      status IN ('created', 'active', 'idle', 'ended')
    ),
    CONSTRAINT valid_permission_mode CHECK
(
    permission_mode
    IN
(
    'supervised',
    'auto_accept',
    'autonomous'
)
    )
    );

-- Session tracking columns
ALTER TABLE threads
    ADD COLUMN IF NOT EXISTS total_turns INTEGER DEFAULT 0;
ALTER TABLE threads
    ADD COLUMN IF NOT EXISTS total_tokens INTEGER DEFAULT 0;

-- Nextcloud session folder columns
-- LEGACY: superseded by main_cloud_* columns below. Kept for one release.
DO $$ BEGIN
    ALTER TABLE threads ADD COLUMN nc_session_folder TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE threads ADD COLUMN nc_share_id INTEGER;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Main cloud abstraction — per-thread backend dispatch for session folders.
DO $$ BEGIN
    ALTER TABLE threads ADD COLUMN main_cloud_backend TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE threads ADD COLUMN main_cloud_session_handle TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE threads ADD COLUMN main_cloud_share_handle TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Backfill existing Nextcloud sessions into the new columns.
UPDATE threads
   SET main_cloud_backend       = 'nextcloud',
       main_cloud_session_handle = nc_session_folder,
       main_cloud_share_handle   = nc_share_id::text
 WHERE nc_session_folder IS NOT NULL
   AND main_cloud_session_handle IS NULL;

CREATE INDEX IF NOT EXISTS idx_threads_user ON threads(user_id);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);
CREATE INDEX IF NOT EXISTS idx_threads_agent ON threads(agent_id);
CREATE INDEX IF NOT EXISTS idx_threads_project ON threads(project_id);

-- ============================================================================
-- 4c. THREAD MESSAGES TABLE
-- Persistent message storage for interactive agent sessions.
-- Follows the same pattern as builder_messages.
-- ============================================================================

CREATE TABLE IF NOT EXISTS thread_messages
(
    id
    UUID
    PRIMARY
    KEY
    DEFAULT
    uuid_generate_v4
(
),
    thread_id UUID NOT NULL REFERENCES threads
(
    id
) ON DELETE CASCADE,
    role VARCHAR
(
    20
) NOT NULL,
    content TEXT,
    tool_calls JSONB,
    turn_number INTEGER,
    metrics JSONB,
    created_at TIMESTAMP
  WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
      );
CREATE INDEX IF NOT EXISTS idx_thread_messages_thread ON thread_messages(thread_id);

-- Migration: add metrics column to existing databases
ALTER TABLE thread_messages
    ADD COLUMN IF NOT EXISTS metrics JSONB;

-- ============================================================================
-- 3. DATASOURCES TABLE
-- External database connections that agents can use during job execution.
-- See docs/datasources.md for the full connector system design.
-- ============================================================================

CREATE TABLE IF NOT EXISTS datasources (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- User-provided metadata
    name TEXT NOT NULL,                    -- Label (e.g. "Production Analytics DB")
    description TEXT,                      -- What this datasource contains (included in agent context)

    -- Connection details
    type TEXT NOT NULL,                    -- 'generic', 'repository', 'postgresql', 'neo4j', 'mongodb', 'webdav'
    connection_url TEXT,                   -- Connection string (nullable for generic datasources using env vars only)
    credentials JSONB DEFAULT '{}',       -- Auth details: env_vars dict (generic), auth_method+token/ssh_key (repository), type-specific (managed)
    cli_hint TEXT,                         -- Suggested CLI command (e.g. "psql $DATABASE_URL")
    default_branch TEXT,                   -- Default branch to clone (repository type only)

    -- Ownership & visibility
    created_by UUID REFERENCES users(id),   -- Owner (NULL for system-seeded datasources)
    is_global BOOLEAN NOT NULL DEFAULT FALSE, -- TRUE = visible to all users

    -- Legacy scope column (kept for backward compat, will be removed)
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

-- Migration: Add cli_hint and default_branch columns
DO $$ BEGIN
    ALTER TABLE datasources ADD COLUMN cli_hint TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
DO $$ BEGIN
    ALTER TABLE datasources ADD COLUMN default_branch TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Make connection_url nullable
ALTER TABLE datasources ALTER COLUMN connection_url DROP NOT NULL;

-- Migration: Drop read_only from datasources (now project-level only via project_datasources)
ALTER TABLE datasources DROP COLUMN IF EXISTS read_only;

-- Migration: Add ownership and visibility columns
DO $$ BEGIN
    ALTER TABLE datasources ADD COLUMN created_by UUID REFERENCES users(id);
EXCEPTION WHEN duplicate_column THEN null;
END $$;
DO $$ BEGIN
    ALTER TABLE datasources ADD COLUMN is_global BOOLEAN NOT NULL DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
CREATE INDEX IF NOT EXISTS idx_datasources_created_by ON datasources(created_by);

-- Migration: Remove one-per-type constraint, allow multiple datasources of the same type.
-- Per-owner uniqueness prevents accidental exact duplicates (same name+type per owner).
DROP INDEX IF EXISTS uq_datasource_type_scope;
DROP INDEX IF EXISTS uq_datasource_type_job;
CREATE UNIQUE INDEX IF NOT EXISTS uq_datasource_name_type_owner ON datasources (
    name, type, COALESCE(created_by, '00000000-0000-0000-0000-000000000000')
);

CREATE INDEX IF NOT EXISTS idx_datasources_type ON datasources(type);
CREATE INDEX IF NOT EXISTS idx_datasources_job_id ON datasources(job_id);

-- ============================================================================
-- 3b. PROJECT ↔ DATASOURCE JUNCTION TABLE (N:M)
-- A datasource can be shared across multiple projects.
-- ============================================================================

CREATE TABLE IF NOT EXISTS project_datasources (
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    datasource_id UUID NOT NULL REFERENCES datasources(id) ON DELETE CASCADE,
    linked_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,

    -- Project-level settings
    read_only BOOLEAN,                     -- Managed connectors: TRUE = tools only (no CLI/env vars), FALSE/NULL = CLI mode
    description TEXT,                      -- Project-specific usage context for the AI

    PRIMARY KEY (project_id, datasource_id)
);

CREATE INDEX IF NOT EXISTS idx_project_datasources_ds ON project_datasources(datasource_id);

-- Migration: Add read_only and description columns to project_datasources (for existing tables)
ALTER TABLE project_datasources ADD COLUMN IF NOT EXISTS read_only BOOLEAN;
ALTER TABLE project_datasources ADD COLUMN IF NOT EXISTS description TEXT;

-- Migrate legacy datasources.project_id rows into junction table
INSERT INTO project_datasources (project_id, datasource_id)
SELECT project_id, id FROM datasources WHERE project_id IS NOT NULL
ON CONFLICT DO NOTHING;

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

-- Migration: Add title column to builder_sessions table
DO $$ BEGIN
    ALTER TABLE builder_sessions ADD COLUMN title VARCHAR(120);
EXCEPTION WHEN duplicate_column THEN null;
END $$;

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

DROP TRIGGER IF EXISTS update_user_api_keys_updated_at ON user_api_keys;
CREATE TRIGGER update_user_api_keys_updated_at
    BEFORE UPDATE ON user_api_keys
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_project_api_keys_updated_at ON project_api_keys;
CREATE TRIGGER update_project_api_keys_updated_at
    BEFORE UPDATE ON project_api_keys
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

-- Discriminates between VM-gate sudo requests ('sudo_command') and
-- container-level freeze requests ('vm_upgrade').
DO $$ BEGIN
    ALTER TABLE sudo_approval_requests
        ADD COLUMN request_type VARCHAR(20) NOT NULL DEFAULT 'sudo_command';
EXCEPTION WHEN duplicate_column THEN null;
END $$;

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
-- 9. MESSAGE LOG (Agent-Human Communication)
-- Stores all messages between agents and humans for audit trail
-- and rate limit enforcement. Messages are also stored as workspace files.
-- ============================================================================

CREATE TABLE IF NOT EXISTS message_log (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id           UUID REFERENCES jobs(id) ON DELETE CASCADE,
    user_id          UUID REFERENCES users(id) ON DELETE SET NULL,
    thread_id        VARCHAR(12) NOT NULL,
    direction        VARCHAR(10) NOT NULL,          -- 'outbound' or 'inbound'
    recipient_email  TEXT,
    subject          TEXT NOT NULL,
    message          TEXT NOT NULL,
    mode             VARCHAR(10),                   -- 'async', 'blocking' (outbound only)
    status           VARCHAR(20) NOT NULL,          -- sent, failed, rate_limited, delivered
    error_message    TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_message_log_job ON message_log(job_id);
CREATE INDEX IF NOT EXISTS idx_message_log_thread ON message_log(thread_id);
CREATE INDEX IF NOT EXISTS idx_message_log_user_created ON message_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_message_log_rate ON message_log(job_id, created_at)
    WHERE direction = 'outbound';

-- Migration: Add email_message_id for IMAP reply correlation (Phase 2)
DO $$ BEGIN
    ALTER TABLE message_log ADD COLUMN email_message_id TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
CREATE INDEX IF NOT EXISTS idx_message_log_email_msgid
    ON message_log(email_message_id) WHERE email_message_id IS NOT NULL;

-- Migration: Add read_at for notification read tracking (Phase 3)
DO $$ BEGIN
    ALTER TABLE message_log ADD COLUMN read_at TIMESTAMPTZ;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- ============================================================================
-- 9b. EXTERNAL CONTACTS (Phase 3 Live Communication)
-- ============================================================================

CREATE TABLE IF NOT EXISTS external_contacts (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id       UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    display_name     TEXT NOT NULL,
    email            TEXT NOT NULL,
    added_by         UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ext_contact_project_email
    ON external_contacts(project_id, email);
CREATE INDEX IF NOT EXISTS idx_ext_contacts_project
    ON external_contacts(project_id);

-- ============================================================================
-- 9c. NOTIFICATION QUEUE (Phase 3 Live Communication)
-- ============================================================================

CREATE TABLE IF NOT EXISTS notification_queue (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id           UUID REFERENCES jobs(id) ON DELETE CASCADE,
    thread_id        VARCHAR(12),
    subject          TEXT NOT NULL,
    message          TEXT NOT NULL,
    channels         JSONB NOT NULL DEFAULT '{}',
    queued_at        TIMESTAMPTZ DEFAULT NOW(),
    delivered_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_notif_queue_pending
    ON notification_queue(user_id, queued_at) WHERE delivered_at IS NULL;

-- ============================================================================
-- 9d. SYSTEM SETTINGS (main cloud abstraction Phase 1)
-- Runtime overrides for deployment-wide configuration, written by the admin
-- settings UI in Phase 4. Credentials are never stored inline — only a
-- credentials_ref pointer (env var name in dev, Vault path in prod).
-- ============================================================================

CREATE TABLE IF NOT EXISTS system_settings (
    key             TEXT PRIMARY KEY,
    value           JSONB NOT NULL,
    credentials_ref TEXT,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_by      TEXT
);

-- ============================================================================
-- 10. VIEWS
-- ============================================================================

-- Drop and recreate: adding project columns requires view recreation
DROP VIEW IF EXISTS job_summary;
CREATE OR REPLACE VIEW job_summary AS
SELECT
    j.id,
    j.status,
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
    j.total_tokens_used,
    j.total_requests
FROM jobs j;
