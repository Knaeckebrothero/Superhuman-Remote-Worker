-- migration:     0072_jobs_failed_at.sql
-- description:   Record WHEN a job failed, because `updated_at` cannot be
--                trusted to say so.
--
--                `jobs.updated_at` is maintained by the `update_jobs_updated_at`
--                BEFORE UPDATE trigger, which fires on ANY update to the row —
--                including ones nobody wrote deliberately. `gc_offline_agents`
--                (retention_hours=24) deletes agent rows a day after their last
--                heartbeat, and `jobs.assigned_agent_id` is ON DELETE SET NULL,
--                so that DELETE issues an UPDATE against every job the agent
--                held and the trigger stamps `updated_at = now()`.
--
--                Net effect: every job held by a dead agent has its `updated_at`
--                rewritten to EXACTLY 24 hours after that agent's last
--                heartbeat. Job c6dd288d read `2026-07-28 09:50:12` but actually
--                failed `2026-07-27 ~09:28` — an incident review anchored on
--                `updated_at` lands a full day late and correlates against the
--                wrong window. That cost real investigation time.
--
--                `completed_at` already exists but does not cover this:
--                `update_job_status` never sets it on the failure path (only
--                `fail_llm_outage_job` and `approve_job` do), so a failed job
--                has no trustworthy timestamp at all.
--
--                NOT BACKFILLED. Existing failed rows keep failed_at NULL,
--                because the only available source would be `updated_at` — the
--                very value this column exists to distrust. NULL honestly means
--                "failed before this migration; the time is not recoverable
--                from this row", which is strictly better than a confident
--                wrong answer. Date those from context.vm.last_heartbeat,
--                lease_expires_at, and the agent_audit tail instead.
--
--                docs/issues/transient_db_error_hard_fails_job_and_destroys_vm.md
--                (Defect 4).
-- depends-on:    0071_jobs_wake_pending_idx.notx.sql
-- expected:      < 1s. Bare ADD COLUMN of a nullable column with no default —
--                metadata-only in PG11+, no table rewrite, no scan.
-- locks:         Brief ACCESS EXCLUSIVE on jobs for the ADD COLUMN.
-- transactional: yes
-- ============================================================================

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ;

COMMENT ON COLUMN jobs.failed_at IS
    'When the job entered ''failed'', set by update_job_status on the '
    'transition. Use this, NEVER updated_at, to date a failure: the '
    'update_jobs_updated_at trigger fires on FK cascades from '
    'gc_offline_agents, which rewrites updated_at to exactly 24h after the '
    'assigned agent''s last heartbeat. NULL on rows that failed before '
    'migration 0072 — the time is genuinely unknown, not zero. Design: '
    'docs/superpowers/specs/2026-07-28-transient-infra-failure-handling-design.md.';

COMMIT;
