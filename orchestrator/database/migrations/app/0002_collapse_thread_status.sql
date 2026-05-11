-- migration:     0002_collapse_thread_status.sql
-- description:   Collapse persistent thread lifecycle from 4 states to 3.
--                'idle' was always functionally identical to 'ended' (both
--                resumable, both equally non-active). It existed only because
--                nothing flipped sessions to 'ended' automatically. The new
--                model: idle-timeout flips straight to 'ended'. Backfill any
--                lingering idle rows, then tighten the CHECK constraint.
-- depends-on:    0001_initial.sql
-- expected:      < 1s; threads is a small table (active session metadata).
-- locks:         RowExclusiveLock on threads for the backfill UPDATE;
--                AccessExclusiveLock briefly for DROP/ADD CONSTRAINT (retried);
--                ShareUpdateExclusiveLock for VALIDATE CONSTRAINT.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

UPDATE threads
SET status   = 'ended',
    ended_at = COALESCE(ended_at, last_activity, CURRENT_TIMESTAMP)
WHERE status = 'idle';

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
            ALTER TABLE threads DROP CONSTRAINT IF EXISTS valid_thread_status;
            ALTER TABLE threads
                ADD CONSTRAINT valid_thread_status
                    CHECK (status IN ('created', 'active', 'ended')) NOT VALID;
            done := true;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            delay_ms := round(random() * least(cap_ms, base_ms * 2 ^ i));
            PERFORM pg_sleep(delay_ms::numeric / 1000);
        END;
    END LOOP;
    IF NOT done THEN
        RAISE EXCEPTION 'lock acquisition failed after % attempts', max_attempts;
    END IF;
END $$;

ALTER TABLE threads VALIDATE CONSTRAINT valid_thread_status;

COMMIT;
