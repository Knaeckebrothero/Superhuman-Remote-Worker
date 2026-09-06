-- migration:     0147_thread_permission_lease_receipts.sql
-- description:   Bind stateless permission prompts to the exact accepted
--                session lease and link owner-loss retirement to one durable
--                permission.resolved journal receipt.
-- depends-on:    0145_session_turn_memory_effects.sql (0146 is an independent
--                concurrent index applied in the runner's .notx pass)
-- expected:      < 5s. Both columns are nullable metadata-only additions;
--                the small permission table gains one identity constraint.
--                Existing rows remain NULL and deliberately fail closed.
-- locks:         Brief ACCESS EXCLUSIVE on thread_permission_requests and
--                thread_events, acquired together with bounded retries.
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
            LOCK TABLE thread_permission_requests, thread_events
                IN ACCESS EXCLUSIVE MODE;
            done := true;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            delay_ms := round(random() * least(cap_ms, base_ms * 2 ^ i));
            PERFORM pg_sleep(delay_ms::numeric / 1000);
        END;
    END LOOP;
    IF NOT done THEN
        RAISE EXCEPTION
            'lock acquisition failed on permission-retirement tables after % attempts',
            max_attempts;
    END IF;
END $$;

ALTER TABLE thread_permission_requests
    ADD COLUMN accepted_lease_token BIGINT,
    ADD CONSTRAINT uq_thread_permission_request_identity
        UNIQUE (id, thread_id),
    ADD CONSTRAINT thread_permission_accepted_lease_positive
        CHECK (accepted_lease_token IS NULL OR accepted_lease_token > 0)
        NOT VALID;

COMMENT ON COLUMN thread_permission_requests.accepted_lease_token IS
    'Immutable exact stateless session_turn lease captured while admission '
    'holds the threads -> run_queue locks. NULL identifies pinned or legacy '
    'rows and is never guessed or retired by a lease-loss consumer.';

ALTER TABLE thread_events
    ADD COLUMN permission_request_id UUID,
    ADD CONSTRAINT thread_events_permission_request_thread_fkey
        FOREIGN KEY (permission_request_id, thread_id)
        REFERENCES thread_permission_requests(id, thread_id) NOT VALID;

COMMENT ON COLUMN thread_events.permission_request_id IS
    'Durable permission.resolved receipt link for one exact-lease permission '
    'request retired after proven owner loss. The partial unique index added '
    'by 0148 permits at most one linked receipt per request.';

COMMIT;
