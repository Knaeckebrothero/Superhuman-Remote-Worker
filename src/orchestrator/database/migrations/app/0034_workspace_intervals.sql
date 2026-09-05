-- migration:     0034_workspace_intervals.sql
-- description:   Workspace compute-interval bookkeeping (Slice 4b). The mutable
--                open/close state for a workspace pod's lifetime; a materializer
--                loop turns each CLOSED interval into immutable usage_events rows
--                (vcpu-hour + gib-hour), keeping the auditdb ledger append-only.
--                Compute basis = requests × wall-clock (observability_and_quotas
--                decision 5). Lives in the app DB (control-plane bookkeeping the
--                orchestrator mutates); the finalized cost rows go to srw-auditdb.
-- depends-on:    0001_initial.sql
-- expected:      < 1s. One empty table + two partial indexes.
-- locks:         AccessExclusiveLock on the new table only.
-- transactional: yes

SET LOCAL lock_timeout      = '2s';
SET LOCAL statement_timeout = '5min';

CREATE TABLE IF NOT EXISTS workspace_intervals (
    id              BIGSERIAL    PRIMARY KEY,
    owner_kind      TEXT         NOT NULL,    -- 'job' | 'session'
    owner_id        UUID         NOT NULL,    -- job_id | thread_id
    tier            TEXT,                     -- 'sandbox' (v1) | 'vm' (later)
    cpu_millicores  INTEGER      NOT NULL,    -- requested CPU (the billed basis)
    mem_bytes       BIGINT       NOT NULL,    -- requested memory
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),  -- pod created
    ended_at        TIMESTAMPTZ,              -- pod deleted; NULL = open interval
    materialized_at TIMESTAMPTZ,              -- written to usage_events; NULL = pending
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- At most one OPEN interval per owner: makes the open emit idempotent (a re-create
-- of a still-open workspace ON CONFLICT DO NOTHING keeps the original started_at),
-- and a fresh create after a close opens a new interval (suspend/restore → two
-- intervals, billed only for the live periods).
CREATE UNIQUE INDEX IF NOT EXISTS workspace_intervals_open_uq
    ON workspace_intervals (owner_kind, owner_id) WHERE ended_at IS NULL;

-- The materializer's work queue: closed but not yet written to the ledger.
CREATE INDEX IF NOT EXISTS workspace_intervals_pending_idx
    ON workspace_intervals (ended_at)
    WHERE ended_at IS NOT NULL AND materialized_at IS NULL;

COMMENT ON TABLE workspace_intervals IS
    'Open/close bookkeeping for workspace-pod compute (Slice 4b). One row per '
    'pod lifetime; a materializer loop converts CLOSED rows into immutable '
    'usage_events (vcpu-hour + gib-hour) and stamps materialized_at. App DB '
    '(orchestrator mutates it); the cost ledger is in srw-auditdb.';
