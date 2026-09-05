-- migration:     0118_jobs_execution_lane.sql
-- description:   Stateless-agents S3 coexistence partition
--                (docs/features/stateless_agents.md §5.4.4): give jobs an
--                execution-plane class independent of runner_kind's grant
--                semantics, so exactly one dispatch/recovery authority owns
--                each job during the pinned/stateless soak.
-- depends-on:    0117_run_queue_affinity.sql
-- expected:      < 5s. Constant-default ADD COLUMN is catalog-only since PG 11.
-- locks:         Brief ACCESS EXCLUSIVE on jobs for the ADD COLUMN.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Do not overload jobs.runner_kind: that column is the grant/capability class
-- (user | lifecycle | service), not the runtime control plane. App-validated
-- ('pinned' | 'stateless') to match threads.execution_lane; deliberately no
-- CHECK on this existing hot table, following migration 0115's lane precedent.
ALTER TABLE jobs
    ADD COLUMN execution_lane TEXT NOT NULL DEFAULT 'pinned';

COMMENT ON COLUMN jobs.execution_lane IS
    'Which execution plane owns this job: ''pinned'' (registered-agent '
    'dispatch and jobs-row lease recovery, the default) or ''stateless'' '
    '(worker_batch run_queue claim and reaper). App-validated by design; '
    'exactly one plane may dispatch or recover a job. See '
    'docs/features/stateless_agents.md §5.4.4.';

COMMIT;
