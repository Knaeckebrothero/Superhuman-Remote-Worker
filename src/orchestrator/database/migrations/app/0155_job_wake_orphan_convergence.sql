-- migration:     0155_job_wake_orphan_convergence.sql
-- description:   Converge wakes orphaned by an old orchestrator in the rollout
--                interval after 0151's backfill but before 0154 installed the
--                thread-delete trigger. The trigger prevents new orphans;
--                this forward-only pass retires the finite interval residue.
-- depends-on:    0154_thread_delete_wake_guard.sql
-- expected:      One exact guarded UPDATE of legacy orphan wakes. Rows outside
--                the open wake states, and ordinary jobs that did not request
--                a completion wake, are untouched.
-- locks:         ROW EXCLUSIVE on jobs; ordinary reads and writes continue.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

UPDATE jobs
SET wake_state = 'undeliverable',
    wake_claimed_at = NULL,
    updated_at = CURRENT_TIMESTAMP
WHERE wake_on_complete
  AND created_by_thread_id IS NULL
  AND wake_state IN ('none', 'pending', 'sending');

COMMIT;
