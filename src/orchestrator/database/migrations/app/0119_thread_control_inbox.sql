-- migration:     0119_thread_control_inbox.sql
-- description:   Durable, commit-ordered session control inbox for both the
--                pinned and stateless execution lanes. Control requests are
--                owner-applied and owner-journaled; the orchestrator never
--                allocates a live runtime's event sequence.
-- depends-on:    0117_run_queue_affinity.sql
-- expected:      < 5s. Constant-default cursor columns are catalog-only on
--                PG11+; narration is nullable and the new request table starts
--                empty. Existing-table constraints land NOT VALID here and
--                are backfilled/validated by 0121 after this transaction
--                releases its ACCESS EXCLUSIVE locks. The receipt index is
--                built concurrently by 0120.
-- locks:         Brief ACCESS EXCLUSIVE locks on threads, run_queue and
--                thread_events, acquired together with bounded retries before
--                any DDL so a failed later lock never sleeps holding an
--                earlier table lock.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Acquire every existing-table lock in one retryable subtransaction. If any
-- table is busy, PostgreSQL rolls the subtransaction back and releases the
-- locks already acquired before the jittered sleep. Once this succeeds, the
-- metadata-only ALTERs below cannot queue behind application traffic while
-- holding a partial lock set.
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
            'lock acquisition failed on control-inbox tables after % attempts',
            max_attempts;
    END IF;
END $$;

-- Admission takes the threads row FOR UPDATE, increments this cursor, inserts
-- the request and (for stateless threads) advances run_queue.control_input_seq
-- in one transaction. Unlike an IDENTITY value, that lock-serialized cursor
-- is commit ordered: a later allocator cannot pass the earlier transaction.
ALTER TABLE threads
    ADD COLUMN control_seq_hwm BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN control_admission_agent_id UUID;

-- Permission mode was already first-class. Narration must be too once a
-- request changes it, but old threads may inherit narration from a selected
-- expert/account layer that SQL cannot resolve. NULL is therefore an explicit
-- "not materialized" sentinel: attach/snapshot fall back to resolved config
-- until creation or a control writes a first-class value.
ALTER TABLE threads
    ADD COLUMN narration_mode TEXT;

ALTER TABLE threads
    ADD CONSTRAINT valid_narration_mode
        CHECK (narration_mode IN ('silent', 'verbose', 'auto')) NOT VALID;

COMMENT ON COLUMN threads.control_seq_hwm IS
    'Highest commit-ordered thread_control_requests.request_seq allocated for '
    'this thread. Admission increments it while holding the threads row lock; '
    'never allocate request order from an IDENTITY/sequence.';

COMMENT ON COLUMN threads.control_admission_agent_id IS
    'Exact pinned-owner capability and teardown fence. NULL is closed. An '
    'inbox-capable reciprocal owner writes its own agent UUID after attach '
    'and clears it before its final drain. A stale credential never transfers '
    'to a different owner generation.';

COMMENT ON COLUMN threads.narration_mode IS
    'Durable interactive narration mode (silent | verbose | auto). NULL means '
    'the thread still inherits its resolved config; creation and the serving '
    'control owner materialize an explicit value.';

-- A control committed while a stateless turn is leased must make completion
-- requeue the unit even when no newer human input exists. These are independent
-- watermarks because thread_messages.seq and control request_seq are separate
-- commit-ordered domains.
ALTER TABLE run_queue
    ADD COLUMN control_input_seq BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN control_consumed_seq BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN run_queue.control_input_seq IS
    'Newest control request_seq admitted for this stateless unit. Admission '
    'advances it without disturbing a live lease; completion requeues while '
    'it is ahead of control_consumed_seq.';

COMMENT ON COLUMN run_queue.control_consumed_seq IS
    'Newest contiguous control request_seq whose owner-written journal result '
    'is durable and whose request row has been terminalized under the current '
    'lease fence.';

CREATE TABLE thread_control_requests (
    id                  UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    thread_id           UUID        NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    request_seq         BIGINT      NOT NULL,
    client_request_id   UUID        NOT NULL,
    verb                TEXT        NOT NULL,
    payload             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    requested_by        TEXT        NOT NULL,
    requested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Non-NULL only for a request admitted on the pinned lane. It is the exact
    -- journal-writer credential, not a routing hint. A successor may adopt an
    -- unreceipted request under the reciprocal binding; a receipted request
    -- keeps the writer that produced the durable event for attribution.
    accepted_agent_id   UUID,

    outcome             TEXT,
    result              JSONB,
    applied_at          TIMESTAMPTZ,
    applied_lease_token BIGINT,
    applied_agent_id    UUID,
    journal_epoch       INTEGER,
    journal_seq         BIGINT,
    acknowledged_at     TIMESTAMPTZ,
    error_code          TEXT,

    CONSTRAINT uq_thread_control_request_seq
        UNIQUE (thread_id, request_seq),
    CONSTRAINT uq_thread_control_client_request
        UNIQUE (thread_id, client_request_id),
    CONSTRAINT uq_thread_control_identity
        UNIQUE (id, thread_id),
    CONSTRAINT thread_control_payload_object
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT thread_control_outcome_value
        CHECK (outcome IS NULL OR outcome IN ('applied', 'rejected')),
    CONSTRAINT thread_control_terminal_shape
        CHECK (
            (outcome IS NULL
             AND result IS NULL
             AND applied_at IS NULL
             AND applied_lease_token IS NULL
             AND applied_agent_id IS NULL
             AND journal_epoch IS NULL
             AND journal_seq IS NULL
             AND acknowledged_at IS NULL
             AND error_code IS NULL)
            OR
            (outcome IS NOT NULL
             AND result IS NOT NULL
             AND applied_at IS NOT NULL
             AND ((applied_lease_token IS NOT NULL AND applied_agent_id IS NULL)
                  OR (applied_lease_token IS NULL AND applied_agent_id IS NOT NULL))
             AND journal_epoch IS NOT NULL
             AND journal_seq IS NOT NULL
             AND acknowledged_at IS NOT NULL)
        )
);

COMMENT ON TABLE thread_control_requests IS
    'Durable session control inbox shared by pinned and stateless lanes. The '
    'orchestrator admits an owner-authorized request; only the exact serving '
    'owner applies it and writes its journal result. outcome remains NULL '
    'until the result event is durably committed and terminalization proves '
    'the current lease token or exact pinned agent binding.';

COMMENT ON COLUMN thread_control_requests.accepted_agent_id IS
    'Exact pinned journal-writer credential. Captured under the admission row '
    'lock and transferable only to a reciprocal successor before any receipt; '
    'NULL for stateless requests. Hostname, IP and pod name are not ownership '
    'credentials.';

CREATE INDEX idx_thread_control_pending
    ON thread_control_requests (thread_id, request_seq)
    WHERE outcome IS NULL;

-- Recovery after "journal commit, request terminalization crash" is an indexed
-- lookup, not a JSON payload scan. One durable result frame per request also
-- makes duplicate owner retries fail loud.
ALTER TABLE thread_events
    ADD COLUMN control_request_id UUID,
    ADD CONSTRAINT thread_events_control_request_thread_fkey
        FOREIGN KEY (control_request_id, thread_id)
        REFERENCES thread_control_requests(id, thread_id) NOT VALID;

COMMENT ON COLUMN thread_events.control_request_id IS
    'Durable receipt link for an owner-written control result frame. A pending '
    'request with this event already committed is validated and finalized '
    'without emitting a duplicate frame; the current owner then converges its '
    'in-memory scalar from the validated result.';

-- Latency hint only. Every owner drains the table before waiting and on a
-- bounded timeout; correctness never depends on delivery of this notification.
CREATE OR REPLACE FUNCTION notify_thread_control_request()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'thread_control_requests',
        json_build_object(
            'id', NEW.id,
            'thread_id', NEW.thread_id,
            'request_seq', NEW.request_seq
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER thread_control_request_notify_trigger
    AFTER INSERT ON thread_control_requests
    FOR EACH ROW
    EXECUTE FUNCTION notify_thread_control_request();

COMMIT;
