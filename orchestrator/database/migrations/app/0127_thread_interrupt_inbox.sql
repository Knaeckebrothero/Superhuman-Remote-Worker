-- migration:     0127_thread_interrupt_inbox.sql
-- description:   Exact-lease, exact-turn interrupt inbox for stateless
--                sessions. The orchestrator admits only while the serving
--                executor's run_queue window is open; that same lease owner
--                applies the signal and writes its durable journal receipt.
-- depends-on:    0126_canvas_editor_awareness.sql
-- expected:      < 5s. Nullable queue/event columns are catalog-only, the
--                request table starts empty, and existing-table constraints
--                land NOT VALID for the 0129 validation pass. The receipt
--                index is built concurrently by 0128.
-- locks:         Brief ACCESS EXCLUSIVE locks on run_queue and thread_events,
--                plus threads for the request FK, acquired together with
--                bounded retries before any DDL.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Acquire the complete existing-table lock set in one retryable
-- subtransaction. A busy table rolls the subtransaction back, releasing any
-- earlier locks before the jittered retry, so this migration never waits for
-- one catalog lock while retaining another.
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
            LOCK TABLE threads, run_queue, thread_events
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
            'lock acquisition failed on interrupt-inbox tables after % attempts',
            max_attempts;
    END IF;
END $$;

-- This nullable pair is an admission window, not a work watermark. The lease
-- owner opens it only after the concrete turn and consumer are ready, and
-- clears it before its final drain. REST admission locks this same row, so an
-- interrupt either commits before closure for that exact owner to drain or is
-- refused after closure. No successor may inherit the signal.
ALTER TABLE run_queue
    ADD COLUMN interrupt_admission_lease_token BIGINT,
    ADD COLUMN interrupt_admission_turn_id INTEGER,
    ADD CONSTRAINT run_queue_interrupt_admission_shape
        CHECK (
            (interrupt_admission_lease_token IS NULL
             AND interrupt_admission_turn_id IS NULL)
            OR
            (interrupt_admission_lease_token IS NOT NULL
             AND interrupt_admission_turn_id IS NOT NULL
             AND unit_kind = 'session_turn'
             AND state = 'leased'
             AND interrupt_admission_lease_token = lease_token
             AND interrupt_admission_lease_token > 0
             AND interrupt_admission_turn_id > 0)
        ) NOT VALID;

COMMENT ON COLUMN run_queue.interrupt_admission_lease_token IS
    'NULL closes stateless interrupt admission. While open, this is the exact '
    'current session_turn lease token and never transfers to a successor. '
    'Admission and closure serialize on the run_queue row.';

COMMENT ON COLUMN run_queue.interrupt_admission_turn_id IS
    'Concrete active turn accepted by the exact interrupt lease window. NULL '
    'means closed. This is deliberately not a queue watermark: an interrupt '
    'for one turn must never wake or cancel a later turn.';

CREATE TABLE thread_interrupt_requests (
    id                   UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    thread_id            UUID        NOT NULL
                                     REFERENCES threads(id) ON DELETE CASCADE,
    client_request_id    UUID        NOT NULL,
    target_turn_id       INTEGER     NOT NULL,
    accepted_lease_token BIGINT      NOT NULL,
    accepted_leased_by   TEXT        NOT NULL,
    requested_by         TEXT        NOT NULL,
    requested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    outcome              TEXT,
    result               JSONB,
    applied_mode         TEXT,
    applied_at           TIMESTAMPTZ,
    applied_lease_token  BIGINT,
    journal_epoch        INTEGER,
    journal_seq          BIGINT,
    acknowledged_at      TIMESTAMPTZ,
    error_code           TEXT,

    CONSTRAINT uq_thread_interrupt_client_request
        UNIQUE (thread_id, client_request_id),
    CONSTRAINT uq_thread_interrupt_identity
        UNIQUE (id, thread_id),
    CONSTRAINT thread_interrupt_target_turn_positive
        CHECK (target_turn_id > 0),
    CONSTRAINT thread_interrupt_lease_token_positive
        CHECK (accepted_lease_token > 0),
    CONSTRAINT thread_interrupt_leased_by_nonempty
        CHECK (btrim(accepted_leased_by) <> ''),
    CONSTRAINT thread_interrupt_outcome_value
        CHECK (outcome IS NULL OR outcome IN ('applied', 'rejected')),
    CONSTRAINT thread_interrupt_mode_value
        CHECK (applied_mode IS NULL OR applied_mode IN ('hard', 'graceful')),
    CONSTRAINT thread_interrupt_result_object
        CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
    CONSTRAINT thread_interrupt_exact_lease_receipt
        CHECK (applied_lease_token IS NULL
               OR applied_lease_token = accepted_lease_token),
    CONSTRAINT thread_interrupt_terminal_shape
        CHECK (
            (outcome IS NULL
             AND result IS NULL
             AND applied_mode IS NULL
             AND applied_at IS NULL
             AND applied_lease_token IS NULL
             AND journal_epoch IS NULL
             AND journal_seq IS NULL
             AND acknowledged_at IS NULL
             AND error_code IS NULL)
            OR
            (outcome = 'applied'
             AND result IS NOT NULL
             AND applied_mode IS NOT NULL
             AND applied_at IS NOT NULL
             AND applied_lease_token = accepted_lease_token
             AND journal_epoch IS NOT NULL
             AND journal_seq IS NOT NULL
             AND acknowledged_at IS NOT NULL
             AND error_code IS NULL)
            OR
            (outcome = 'rejected'
             AND result IS NOT NULL
             AND applied_mode IS NULL
             AND applied_at IS NOT NULL
             AND applied_lease_token = accepted_lease_token
             AND journal_epoch IS NOT NULL
             AND journal_seq IS NOT NULL
             AND acknowledged_at IS NOT NULL
             AND error_code IS NOT NULL
             AND btrim(error_code) <> '')
        )
);

COMMENT ON TABLE thread_interrupt_requests IS
    'Durable stateless interrupt inbox. Each request is admitted only for the '
    'exact run_queue lease and concrete active turn captured on the row. The '
    'lease owner applies the idempotent signal and writes the journal result '
    'with its in-process sequence allocator; a successor never applies it. '
    'outcome remains NULL until that receipt is durable and owner-fenced.';

COMMENT ON COLUMN thread_interrupt_requests.client_request_id IS
    'Browser-generated idempotency and acknowledgement correlation key. It is '
    'unique per thread; concurrent tabs keep distinct rows and receipts.';

COMMENT ON COLUMN thread_interrupt_requests.accepted_lease_token IS
    'Immutable exact stateless owner credential captured while the matching '
    'run_queue admission window is locked. No later lease may adopt it.';

COMMENT ON COLUMN thread_interrupt_requests.accepted_leased_by IS
    'Pod identity captured for diagnostics only. Correctness is fenced by '
    'accepted_lease_token; hostname or pod name is never an owner credential.';

COMMENT ON COLUMN thread_interrupt_requests.applied_lease_token IS
    'Lease that wrote the durable result frame. The table constraint requires '
    'it to equal accepted_lease_token for every terminal result.';

CREATE INDEX idx_thread_interrupt_pending_exact
    ON thread_interrupt_requests (
        thread_id,
        accepted_lease_token,
        target_turn_id,
        requested_at,
        id
    )
    WHERE outcome IS NULL;

-- The durable receipt is an explicit relation, not a JSON scan. The composite
-- FK prevents a request from being linked to another thread; 0128 adds the
-- concurrent partial unique index enforcing one receipt per request.
ALTER TABLE thread_events
    ADD COLUMN interrupt_request_id UUID,
    ADD CONSTRAINT thread_events_interrupt_request_thread_fkey
        FOREIGN KEY (interrupt_request_id, thread_id)
        REFERENCES thread_interrupt_requests(id, thread_id) NOT VALID;

COMMENT ON COLUMN thread_events.interrupt_request_id IS
    'Durable result link for one exact-lease interrupt request. A committed '
    'receipt lets the same owner recover finalization without emitting a '
    'duplicate journal frame; it never transfers application authority to a '
    'successor lease.';

-- Notification is a latency hint only. The executor drains before waiting and
-- on a bounded poll; correctness never depends on LISTEN delivery.
CREATE OR REPLACE FUNCTION notify_thread_interrupt_request()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'thread_interrupt_requests',
        json_build_object(
            'id', NEW.id,
            'thread_id', NEW.thread_id,
            'lease_token', NEW.accepted_lease_token,
            'turn_id', NEW.target_turn_id
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER thread_interrupt_request_notify_trigger
    AFTER INSERT ON thread_interrupt_requests
    FOR EACH ROW
    EXECUTE FUNCTION notify_thread_interrupt_request();

COMMIT;
