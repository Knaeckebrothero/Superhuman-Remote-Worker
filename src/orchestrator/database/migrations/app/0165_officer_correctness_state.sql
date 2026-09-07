-- BP-07/BP-08/BP-10 durable correctness state.
--
-- 0162 is a deployed Officer ticket-claim boundary and must not be edited.
-- This migration adds only the two ledgers that cannot be represented safely
-- by best-effort thread JSON: canonical knowledge synchronization attempts and
-- replica-safe backlog-floor wake episodes.  Job provisioning preflight uses
-- the existing born-paused jobs row + server-owned context, so it needs no new
-- public job status.

CREATE TABLE knowledge_materialization_intents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    note_id TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    job_id UUID,
    canonical_state TEXT NOT NULL DEFAULT 'pending_sync'
        CHECK (canonical_state IN ('pending_sync', 'canonical', 'failed', 'superseded')),
    projection_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (projection_state IN ('pending', 'synced', 'projection_only', 'failed')),
    retry_state TEXT NOT NULL DEFAULT 'retryable'
        CHECK (retry_state IN ('none', 'retryable', 'permanent')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    attempt_token UUID,
    lease_expires_at TIMESTAMPTZ,
    last_attempted_at TIMESTAMPTZ,
    next_retry_at TIMESTAMPTZ,
    last_error_class TEXT,
    last_error TEXT,
    repo TEXT,
    branch TEXT,
    path TEXT,
    operation TEXT,
    canonical_at TIMESTAMPTZ,
    projected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE knowledge_materialization_intents IS
    'BP-08 durable canonical-file and projection convergence ledger. The file in the project KB repository is authoritative; pending/failed rows are ineligible for backlog dispatch.';

CREATE INDEX idx_knowledge_materialization_retry
    ON knowledge_materialization_intents (next_retry_at, lease_expires_at, created_at)
    WHERE canonical_state = 'pending_sync' AND retry_state = 'retryable';

-- Coalesce only an unresolved equivalent attempt. A byte-for-byte payload can
-- become desired again after a later mutation (resolved -> active -> resolved),
-- so canonical history must not be a permanent idempotency barrier.
CREATE UNIQUE INDEX uq_knowledge_materialization_unresolved
    ON knowledge_materialization_intents (project_id, note_id, content_hash)
    WHERE canonical_state IN ('pending_sync', 'failed');

CREATE INDEX idx_knowledge_materialization_project_recent
    ON knowledge_materialization_intents (project_id, updated_at DESC);

CREATE TABLE officer_floor_wake_episodes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    officer_incarnation UUID NOT NULL,
    pool TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    wake_event_id BIGINT REFERENCES session_wake_events(id) ON DELETE SET NULL,
    state TEXT NOT NULL DEFAULT 'retryable'
        CHECK (state IN ('retryable', 'queued', 'delivered', 'permanent_failed', 'superseded')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempted_at TIMESTAMPTZ,
    last_queued_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    failure_class TEXT,
    last_error TEXT,
    next_retry_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE officer_floor_wake_episodes IS
    'BP-10 durable backlog-floor wake policy outcomes. Policy debounce starts at last_queued_at; transient retry timing is next_retry_at.';

CREATE UNIQUE INDEX uq_officer_floor_wake_active_episode
    ON officer_floor_wake_episodes (project_id, officer_incarnation, pool)
    WHERE resolved_at IS NULL;

CREATE INDEX idx_officer_floor_wake_project_recent
    ON officer_floor_wake_episodes (project_id, created_at DESC);

CREATE INDEX idx_officer_floor_wake_retry
    ON officer_floor_wake_episodes (next_retry_at, created_at)
    WHERE state = 'retryable' AND resolved_at IS NULL;
