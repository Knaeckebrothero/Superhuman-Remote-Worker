-- migration:     0141_job_completion_sweep_routing.sql
-- description:   Gate-3 routed rescue substrate: one authoritative unfinished
--                completion command per job, plus durable and deduplicated
--                sweep actions keyed by the job-local reap attempt.
-- depends-on:    0140_job_completion_commands.sql
-- expected:      < 5s. One catalog-only constant-default jobs column; the new
--                action table and its indexes start empty. Replacing the view
--                takes only brief catalog locks.
-- locks:         Brief ACCESS EXCLUSIVE on jobs for the metadata-only column
--                addition and on job_completion_sweep_exclusions while its
--                definition is replaced.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- A route action is allocated while the jobs row is locked. The cursor makes
-- `(job_id, attempt)` a monotonic, durable dedup key without relying on a
-- process-local counter or a non-commit-ordered sequence.
ALTER TABLE jobs
    ADD COLUMN completion_sweep_attempt_hwm BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN jobs.completion_sweep_attempt_hwm IS
    'Highest job_completion_sweep_actions.attempt allocated for this job. '
    'Routing increments it while holding the jobs row lock; the resulting '
    '(job_id, attempt) pair is the reap-action dedup key.';

CREATE TABLE job_completion_sweep_actions (
    job_id           UUID   NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt          BIGINT NOT NULL,
    command_id       UUID   NOT NULL
                            REFERENCES job_completion_commands(id) ON DELETE CASCADE,
    command_attempt  INT    NOT NULL,
    route            TEXT   NOT NULL,
    source           TEXT   NOT NULL,

    state            TEXT NOT NULL DEFAULT 'pending',
    claimed_by       TEXT,
    claimed_at       TIMESTAMPTZ,
    claim_expires_at TIMESTAMPTZ,

    result           JSONB,
    error_code       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ,

    PRIMARY KEY (job_id, attempt),
    CONSTRAINT uq_job_completion_sweep_command_attempt
        UNIQUE (command_id, command_attempt),
    CONSTRAINT job_completion_sweep_attempt_positive
        CHECK (attempt > 0),
    CONSTRAINT job_completion_sweep_command_attempt_nonnegative
        CHECK (command_attempt >= 0),
    CONSTRAINT job_completion_sweep_route_value
        CHECK (route IN ('resume_finalizer', 'park_alert', 'alert_only')),
    CONSTRAINT job_completion_sweep_source_nonempty
        CHECK (btrim(source) <> ''),
    CONSTRAINT job_completion_sweep_state_value
        CHECK (state IN ('pending', 'claimed', 'done')),
    CONSTRAINT job_completion_sweep_result_object
        CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
    CONSTRAINT job_completion_sweep_error_nonempty
        CHECK (error_code IS NULL OR btrim(error_code) <> ''),
    CONSTRAINT job_completion_sweep_action_shape
        CHECK (
            (state = 'pending'
             AND claimed_by IS NULL
             AND claimed_at IS NULL
             AND claim_expires_at IS NULL
             AND result IS NULL
             AND error_code IS NULL
             AND completed_at IS NULL)
            OR
            (state = 'claimed'
             AND claimed_by IS NOT NULL
             AND btrim(claimed_by) <> ''
             AND claimed_at IS NOT NULL
             AND claim_expires_at IS NOT NULL
             AND claim_expires_at > claimed_at
             AND result IS NULL
             AND error_code IS NULL
             AND completed_at IS NULL)
            OR
            (state = 'done'
             AND claimed_by IS NULL
             AND claim_expires_at IS NULL
             AND claimed_at IS NOT NULL
             AND completed_at IS NOT NULL
             AND completed_at >= claimed_at
             AND (result IS NOT NULL OR error_code IS NOT NULL))
        )
);

COMMENT ON TABLE job_completion_sweep_actions IS
    'Durable class-1 rescue actions for unfinished completion commands. One '
    'job-local attempt is allocated under the jobs-row lock; the action claim '
    'has a visibility lease, and UNIQUE(command_id, command_attempt) lets a '
    'pending action change route without a second reap firing.';

COMMENT ON COLUMN job_completion_sweep_actions.command_attempt IS
    'Finalizer attempt observed when this reap action was allocated. Together '
    'with command_id it deduplicates competing rescuers for that exact attempt.';

COMMENT ON COLUMN job_completion_sweep_actions.route IS
    'Actionable route from job_completion_sweep_exclusions: resume_finalizer, '
    'park_alert, or alert_only. stand_down never creates an action row.';

COMMENT ON COLUMN job_completion_sweep_actions.source IS
    'Class-1 rescuer that first materialized the action (orphan, job lease, '
    'stale-agent, pause redispatch, or registration recovery).';

COMMENT ON COLUMN job_completion_sweep_actions.claim_expires_at IS
    'Visibility deadline for the action claimant. An expired claimed row is '
    'eligible for takeover; claimed_by alone is never an ownership fence.';

CREATE INDEX idx_job_completion_sweep_actions_claim
    ON job_completion_sweep_actions (state, claim_expires_at, created_at)
    WHERE state IN ('pending', 'claimed');

-- Finalization authority is report order, so every rescuer sees only the
-- oldest unfinished command for a job. The route classification is the single
-- shared predicate: callers never reproduce these state/lease/cap clauses.
-- job_id remains the first column for compatibility with the 0140 view.
CREATE OR REPLACE VIEW job_completion_sweep_exclusions AS
WITH authoritative AS (
    SELECT command.*,
           row_number() OVER (
               PARTITION BY command.job_id
               ORDER BY command.report_seq
           ) AS command_order
    FROM job_completion_commands AS command
    WHERE command.state IN ('pending', 'finalizing', 'parked')
)
SELECT command.job_id,
       command.id AS command_id,
       command.report_seq,
       command.state AS command_state,
       command.attempts AS command_attempts,
       command.max_attempts,
       command.run_after,
       command.lease_expires_at,
       command.deadline_at,
       CASE
           WHEN command.state = 'parked' THEN 'alert_only'
           WHEN command.deadline_at <= now()
                OR command.attempts >= command.max_attempts THEN 'park_alert'
           WHEN command.lease_expires_at > now() THEN 'stand_down'
           ELSE 'resume_finalizer'
       END AS route
FROM authoritative AS command
WHERE command.command_order = 1;

COMMENT ON VIEW job_completion_sweep_exclusions IS
    'Single source of truth for class-1 completion rescue routing. One row is '
    'the oldest pending, finalizing, or parked command per job: parked alerts '
    'only; deadline/retry-cap rows park and alert; live leases stand down; all '
    'other rows resume the finalizer from durable effect progress.';

COMMIT;
