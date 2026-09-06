-- migration:     0142_job_completion_sweep_route_precedence.sql
-- description:   Preserve a live exact finalizer term on its last permitted
--                attempt before routing non-live deadline/cap rows to park.
-- depends-on:    0141_job_completion_sweep_routing.sql
-- expected:      < 5s. Catalog-only view replacement.
-- locks:         Brief ACCESS EXCLUSIVE on job_completion_sweep_exclusions.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- The last permitted claim increments attempts to max_attempts before it
-- executes.  That live exact term remains authoritative until lease expiry;
-- only then may the retry-cap route park it.  Pending rows cannot legitimately
-- carry a finalizer lease, so the live branch is deliberately state-specific.
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
           WHEN command.state = 'finalizing'
                AND command.lease_expires_at > now() THEN 'stand_down'
           WHEN command.deadline_at <= now()
                OR command.attempts >= command.max_attempts THEN 'park_alert'
           ELSE 'resume_finalizer'
       END AS route
FROM authoritative AS command
WHERE command.command_order = 1;

COMMENT ON VIEW job_completion_sweep_exclusions IS
    'Single source of truth for class-1 completion rescue routing. One row is '
    'the oldest pending, finalizing, or parked command per job: parked alerts '
    'only; live finalizer leases stand down; non-live deadline/retry-cap rows '
    'park and alert; all others resume from durable effect progress.';

COMMIT;
