-- migration:     0150_job_wake_undeliverable.sql
-- description:   Represent completion wakes whose exact creating thread was
--                hard-deleted as a distinct terminal outcome. `dead` remains
--                reserved for retry exhaustion and its operator alert.
-- depends-on:    0149_thread_permission_validate_constraints.sql
-- expected:      < 1s. Catalog-only constraint replacement; validation and
--                legacy-orphan backfill are deliberately deferred to 0151 so
--                data work cannot extend this ACCESS EXCLUSIVE transaction.
-- locks:         Brief ACCESS EXCLUSIVE on jobs for the constraint swap.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

DO $$
DECLARE
    max_attempts CONSTANT int    := 30;
    cap_ms       CONSTANT bigint := 60000;
    base_ms      CONSTANT bigint := 10;
    delay_ms              bigint;
    done                  boolean := false;
BEGIN
    FOR i IN 1..max_attempts LOOP
        BEGIN
            ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_wake_state_known;
            ALTER TABLE jobs
                ADD CONSTRAINT jobs_wake_state_known CHECK (
                    wake_state IN (
                        'none', 'pending', 'sending', 'sent', 'dead',
                        'undeliverable'
                    )
                ) NOT VALID;
            done := true;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            delay_ms := round(random() * least(cap_ms, base_ms * 2 ^ i));
            PERFORM pg_sleep(delay_ms::numeric / 1000);
        END;
    END LOOP;
    IF NOT done THEN
        RAISE EXCEPTION 'lock acquisition failed on jobs after % attempts',
            max_attempts;
    END IF;
END $$;

COMMENT ON COLUMN jobs.wake_state IS
    'Wake outbox state: none|pending|sending|sent|dead|undeliverable. dead is '
    'retry exhaustion; undeliverable means the exact creating thread was '
    'hard-deleted before the wake could settle. Claimed by an atomic UPDATE '
    '... FOR UPDATE SKIP LOCKED before the non-idempotent send.';

COMMIT;
