-- migration:     0151_job_wake_undeliverable_validate.sql
-- description:   Retire legacy wakes orphaned by the historical creator FK,
--                then validate the expanded jobs wake-state constraint after
--                0150's ACCESS EXCLUSIVE transaction has committed.
-- depends-on:    0150_job_wake_undeliverable.sql
-- expected:      One guarded legacy-orphan UPDATE plus proportional CHECK
--                validation. The accepted set only expanded, so every row
--                accepted by the old constraint remains valid.
-- locks:         ROW EXCLUSIVE for the backfill and SHARE UPDATE EXCLUSIVE for
--                validation; ordinary reads and writes continue.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Rows orphaned by the historical ON DELETE SET NULL behavior predate the
-- atomic hard-delete settlement in the application. Claims deliberately
-- require a non-NULL creator, so these three open states otherwise remain
-- silently unclaimable forever. A non-wake job with no creator is ordinary;
-- sent/dead are already terminal and retain their exact meaning.
UPDATE jobs
SET wake_state = 'undeliverable',
    wake_claimed_at = NULL,
    updated_at = CURRENT_TIMESTAMP
WHERE wake_on_complete
  AND created_by_thread_id IS NULL
  AND wake_state IN ('none', 'pending', 'sending');

ALTER TABLE jobs VALIDATE CONSTRAINT jobs_wake_state_known;

COMMIT;
