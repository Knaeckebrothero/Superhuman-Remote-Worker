-- migration:     0143_job_completion_accept_status.sql
-- description:   Persist the jobs-row status observed under the completion
--                admission lock, fail closed for unproven legacy commands,
--                and make superseded command rows fully terminal.
-- depends-on:    0142_job_completion_sweep_route_precedence.sql
-- expected:      < 10s. One nullable catalog-only column addition, a bounded
--                backfill from the stable S1 effect row, and a table scan to
--                replace one CHECK constraint.
-- locks:         Brief ACCESS EXCLUSIVE on job_completion_commands for the
--                column and constraint changes; row locks during backfill.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE job_completion_commands
    ADD COLUMN accepted_job_status TEXT;

COMMENT ON COLUMN job_completion_commands.accepted_job_status IS
    'jobs.status observed while admission held the jobs row lock. Nullable '
    'only for legacy commands: a completed late_callback_guard S1 journal row '
    'is the sole accepted backfill proof; an unproven NULL fails closed to '
    'whole-command supersession rather than guessing from current job state.';

-- Some commands may already have crossed S1 when this migration lands. Only
-- that command-owned, completed journal output proves their original entry
-- status; the current jobs row may have moved and is never a valid backfill.
UPDATE job_completion_commands AS command
SET accepted_job_status = btrim(
        effect.detail #>> '{output,entry_status}'
    )
FROM completion_effects AS effect
WHERE command.accepted_job_status IS NULL
  AND effect.producer_kind = 'job_completion'
  AND effect.producer_id = command.id
  AND effect.effect_name = 'late_callback_guard'
  AND effect.effect_group = 'entry'
  AND effect.state = 'done'
  AND jsonb_typeof(effect.detail #> '{output,entry_status}') = 'string'
  AND btrim(effect.detail #>> '{output,entry_status}') <> '';

-- The original 0140 shape allowed a superseded row without an error code and
-- did not require its claim fields to be clear. Normalize any such historical
-- rows before replacing the canonical terminal-shape constraint.
UPDATE job_completion_commands
SET error_code = COALESCE(
        NULLIF(btrim(error_code), ''),
        'entry_status_superseded'
    ),
    finalizing_by = NULL,
    lease_expires_at = NULL
WHERE state = 'superseded';

ALTER TABLE job_completion_commands
    ADD CONSTRAINT job_completion_accepted_status_nonempty CHECK (
        accepted_job_status IS NULL OR btrim(accepted_job_status) <> ''
    ),
    DROP CONSTRAINT job_completion_terminal_shape,
    ADD CONSTRAINT job_completion_terminal_shape CHECK (
        (state IN ('pending', 'finalizing')
         AND outcome IS NULL AND finalized_at IS NULL AND error_code IS NULL)
     OR (state = 'done'
         AND outcome IS NOT NULL AND finalized_at IS NOT NULL
         AND error_code IS NULL)
     OR (state = 'force_resolved'
         AND outcome IS NOT NULL AND finalized_at IS NOT NULL)
     OR (state = 'superseded'
         AND outcome IS NOT NULL AND finalized_at IS NOT NULL
         AND error_code IS NOT NULL AND btrim(error_code) <> ''
         AND finalizing_by IS NULL AND lease_expires_at IS NULL)
     OR (state = 'parked'
         AND error_code IS NOT NULL
         AND outcome IS NULL AND finalized_at IS NULL)
    );

COMMIT;
