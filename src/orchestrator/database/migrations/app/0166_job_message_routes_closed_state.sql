-- migration:     0166_job_message_routes_closed_state.sql
-- description:   Add the terminal 'closed' state to job_message_routes. Ruling
--                (2026-08-19): when a job reaches a terminal status
--                (completed/failed/cancelled) every still-open worker-message
--                route of that job is auto-closed with an audit stamp
--                ("closed automatically: job cancelled") instead of haunting
--                the officer sitrep/inbox as "open" forever — there is
--                deliberately NO manual close verb (officer ack refuses
--                blocking routes; a human reply would risk resuming a dead
--                job). 'closed' is terminal: it joins no open-state filter,
--                so the sitrep section, the officer inbox listing, both
--                reconciler deadline scans, and the redelivery legs all drop
--                the route naturally. History (message_log thread +
--                transitions audit) is preserved untouched.
-- depends-on:    0165_officer_correctness_state.sql
-- expected:      < 1s. One ALTER swapping the CHECK constraint on a small
--                table (full-scan validation is cheap at this size).
-- locks:         AccessExclusiveLock on job_message_routes (brief, retried
--                with backoff like 0163's ALTER).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

DO $$
DECLARE
    max_attempts CONSTANT int := 30;
    cap_ms       CONSTANT bigint := 60000;
    base_ms      CONSTANT bigint := 10;
    delay_ms              bigint;
    done                  boolean := false;
BEGIN
    FOR i IN 1..max_attempts LOOP
        BEGIN
            ALTER TABLE public.job_message_routes
                DROP CONSTRAINT job_message_routes_state_check,
                ADD CONSTRAINT job_message_routes_state_check CHECK (state IN (
                    'pending_officer', 'pending_both', 'user_direct',
                    'escalated_to_user', 'resolved_by_officer',
                    'resolved_by_user', 'timed_out', 'delivery_failed',
                    'closed'
                ));
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

COMMENT ON COLUMN public.job_message_routes.state IS
    'pending_officer/pending_both/user_direct -> resolved_by_officer | '
    'resolved_by_user | escalated_to_user | timed_out; pre-delivery states '
    'may pass through delivery_failed. Any open state -> closed when the job '
    'reaches a terminal status (auto-close; see the transitions audit for '
    'the stamp). CAS-only transitions.';

COMMIT;
