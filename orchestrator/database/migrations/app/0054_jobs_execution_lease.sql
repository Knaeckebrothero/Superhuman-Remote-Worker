-- migration:     0054_jobs_execution_lease.sql
-- description:   Job execution lease — liveness by renewal, not inference
--                (docs/features/job_execution_lease.md). claim_job_for_agent
--                sets a short pickup lease; agent heartbeats renew it; an
--                expired lease IS the definition of an orphaned job, with no
--                dependency on the agents table or sweep ordering (the
--                2026-07-11 stale-agent-detector incident). Backfills a
--                grace lease onto in-flight jobs so they either renew (live
--                agents heartbeat within seconds) or expire cleanly.
-- depends-on:    0053_jobs_runner_kind.sql
-- expected:      < 1s on normal tables; brief table lock for ADD COLUMN.
-- locks:         Brief ACCESS EXCLUSIVE on jobs.
-- transactional: yes
-- ============================================================================

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;

-- The expiry sweep's scan: only processing rows carry a live lease.
CREATE INDEX IF NOT EXISTS jobs_lease_expiry_idx
    ON jobs (lease_expires_at)
    WHERE status = 'processing';

-- In-flight jobs at deploy time: grace lease so live agents' heartbeats take
-- over renewal and dead ones expire into recovery instead of sitting NULL
-- (the sweep deliberately ignores NULL leases — recover_orphaned_jobs keeps
-- covering those as the legacy path).
UPDATE jobs
   SET lease_expires_at = NOW() + interval '5 minutes'
 WHERE status = 'processing'
   AND lease_expires_at IS NULL;
