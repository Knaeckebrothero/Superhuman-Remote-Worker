-- migration:     0140_job_completion_commands.sql
-- description:   Gate-3 completion command substrate: commit-ordered job
--                report admission, per-effect progress, an expiring finalizer
--                leader lease, and the shared sweep-exclusion predicate.
-- depends-on:    0133_thread_session_durable_state.sql
-- expected:      < 5s. One catalog-only constant-default jobs column; all
--                tables and their indexes are new and empty.
-- locks:         Brief ACCESS EXCLUSIVE on jobs for the metadata-only column
--                addition. New relations have no concurrent users.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Admission increments this cursor while holding the jobs row lock. A
-- per-parent high-water mark orders commits; an IDENTITY/sequence cannot.
ALTER TABLE jobs
    ADD COLUMN completion_seq_hwm BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN jobs.completion_seq_hwm IS
    'Highest commit-ordered job_completion_commands.report_seq allocated for '
    'this job. Admission increments it while holding the jobs row lock; never '
    'allocate completion order from an IDENTITY/sequence.';

CREATE TABLE job_completion_commands (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id               UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    report_seq           BIGINT NOT NULL,
    client_report_id     UUID   NOT NULL,
    payload              JSONB  NOT NULL,
    payload_digest       TEXT   NOT NULL,
    reported_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    accepted_lease_token BIGINT,
    accepted_agent_id    UUID,

    origin               TEXT NOT NULL DEFAULT 'agent',
    requested_by         TEXT NOT NULL,

    state                TEXT NOT NULL DEFAULT 'pending',
    attempts             INT  NOT NULL DEFAULT 0,
    max_attempts         INT  NOT NULL DEFAULT 5,
    run_after            TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at     TIMESTAMPTZ,
    deadline_at          TIMESTAMPTZ NOT NULL,
    finalizing_by        TEXT,
    code_version         TEXT NOT NULL,

    outcome              JSONB,
    finalized_at         TIMESTAMPTZ,
    error_code           TEXT,

    CONSTRAINT uq_job_completion_seq    UNIQUE (job_id, report_seq),
    CONSTRAINT uq_job_completion_client UNIQUE (job_id, client_report_id),

    CONSTRAINT job_completion_fence_exactly_one CHECK (
        (origin = 'operator'
         AND accepted_lease_token IS NULL AND accepted_agent_id IS NULL)
     OR (origin <> 'operator'
         AND ((accepted_lease_token IS NOT NULL AND accepted_agent_id IS NULL)
           OR (accepted_lease_token IS NULL AND accepted_agent_id IS NOT NULL)))),

    CONSTRAINT job_completion_state_value CHECK (state IN (
        'pending', 'finalizing', 'done', 'parked', 'superseded',
        'force_resolved')),
    CONSTRAINT job_completion_terminal_shape CHECK (
        (state IN ('pending', 'finalizing')
         AND outcome IS NULL AND finalized_at IS NULL AND error_code IS NULL)
     OR (state = 'done'
         AND outcome IS NOT NULL AND finalized_at IS NOT NULL
         AND error_code IS NULL)
     OR (state = 'force_resolved'
         AND outcome IS NOT NULL AND finalized_at IS NOT NULL)
     OR (state = 'superseded'
         AND finalized_at IS NOT NULL AND outcome IS NOT NULL)
     OR (state = 'parked'
         AND error_code IS NOT NULL
         AND outcome IS NULL AND finalized_at IS NULL))
);

COMMENT ON TABLE job_completion_commands IS
    'Durable, commit-ordered completion reports for both pinned and stateless '
    'job lanes. Agent-origin reports are fenced by exactly one lane-specific '
    'credential; operator-origin terminal paths carry neither.';

CREATE INDEX idx_job_completion_drain
    ON job_completion_commands (run_after)
    WHERE state IN ('pending', 'finalizing');

CREATE TABLE completion_effects (
    producer_kind TEXT NOT NULL,
    producer_id   UUID NOT NULL,
    scope_id      UUID,
    effect_name   TEXT NOT NULL,
    effect_group  TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'pending',
    attempts      INT  NOT NULL DEFAULT 0,
    max_attempts  INT  NOT NULL DEFAULT 5,
    run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    intent_at     TIMESTAMPTZ,
    complete_by   TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code    TEXT,
    PRIMARY KEY (producer_kind, producer_id, effect_name)
);

COMMENT ON TABLE completion_effects IS
    'One stable-name progress row per completion effect. Polymorphic by '
    'producer_kind and deliberately has no foreign key or state-driven '
    'partial index; retention is explicit and shared with session producers.';

-- River-style expiring leader election. The exact (leader_id, elected_at)
-- pair is the term: renewals must key on both so an old term cannot extend a
-- successor. Expired rows are deleted before INSERT ... ON CONFLICT retries.
CREATE TABLE completion_finalizer_leases (
    lease_name  TEXT PRIMARY KEY,
    leader_id   TEXT NOT NULL,
    elected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,

    CONSTRAINT completion_finalizer_lease_name_nonempty
        CHECK (btrim(lease_name) <> ''),
    CONSTRAINT completion_finalizer_leader_id_nonempty
        CHECK (btrim(leader_id) <> ''),
    CONSTRAINT completion_finalizer_lease_expiry_order
        CHECK (expires_at > elected_at)
);

COMMENT ON TABLE completion_finalizer_leases IS
    'Observable expiring leader leases for completion finalizer drains. '
    'Election inserts a named row, renewal fences on leader_id plus elected_at, '
    'and failover reaps only an expired row.';

-- The decision-(6) exclusion predicate is defined once. Re-dispatch rescuers
-- stand down only for a live completion lease or an operator-held parked row;
-- expired pending/finalizing rows remain visible to finalizer-resume routing.
CREATE VIEW job_completion_sweep_exclusions AS
SELECT DISTINCT command.job_id
FROM job_completion_commands AS command
WHERE command.state IN ('pending', 'finalizing', 'parked')
  AND (
      command.lease_expires_at > now()
      OR command.state = 'parked'
  );

COMMENT ON VIEW job_completion_sweep_exclusions IS
    'Single source of truth for jobs-side sweep exclusion: live finalizer '
    'leases and parked operator work only. Expired commands must route to the '
    'finalizer resume path rather than disappear from all rescuers.';

COMMIT;
