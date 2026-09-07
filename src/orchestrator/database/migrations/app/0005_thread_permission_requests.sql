-- migration:     0005_thread_permission_requests.sql
-- description:   Thread-scoped tool permission requests + LISTEN/NOTIFY trigger.
--
--                Phase 3 of headless persistent sessions
--                (docs/features/headless_persistent_sessions.md). Before
--                this migration, an agent waiting for tool-call approval
--                blocked on an in-memory `asyncio.Queue` populated by the
--                WebSocket handler. When the WS closed mid-prompt the
--                queue had no producer left — the agent silently waited
--                out its 300s timeout and denied the call.
--
--                With this table, every permission request is durable:
--                the agent INSERTs a row, LISTENs for status changes,
--                and resumes on the first NOTIFY whose payload references
--                its row. Approval can come from the WS-attached cockpit,
--                a REST POST from MCP or a future SSE-based cockpit, or
--                (Phase 4) an email magic-link landing page — all paths
--                converge on `UPDATE thread_permission_requests SET
--                status = ...` and the trigger.
--
--                Distinct from sudo_approval_requests (job-scoped, VM
--                command gate, NATS-mediated). This is for the
--                persistent agent's own tool-permission flow.
-- depends-on:    0004_thread_events.sql
-- expected:      < 1s. One CREATE TABLE, three indexes, one function,
--                one trigger. All metadata-only on a fresh table.
-- locks:         AccessExclusiveLock briefly for CREATE TABLE.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS thread_permission_requests (
    id            UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    thread_id     UUID         NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    tool_call_id  TEXT         NOT NULL,
    tool_name     TEXT         NOT NULL,
    tool_args     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    status        TEXT         NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    requested_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    decided_at    TIMESTAMPTZ,
    decided_by    TEXT,
    expires_at    TIMESTAMPTZ  NOT NULL DEFAULT (now() + interval '300 seconds')
);

-- Hot path: agent SELECTs the most-recent pending row for a thread on
-- legacy WS approve/deny calls that don't carry an explicit approval_id.
CREATE INDEX IF NOT EXISTS idx_tpr_thread_pending
    ON thread_permission_requests (thread_id, requested_at DESC)
    WHERE status = 'pending';

-- Expiration sweep (future Phase 5-ish): find rows whose TTL elapsed.
CREATE INDEX IF NOT EXISTS idx_tpr_pending_expiry
    ON thread_permission_requests (expires_at)
    WHERE status = 'pending';

-- All-status thread view (cockpit history panel).
CREATE INDEX IF NOT EXISTS idx_tpr_thread_requested
    ON thread_permission_requests (thread_id, requested_at DESC);

COMMENT ON TABLE thread_permission_requests IS
    'Per-thread tool-permission requests. Agent INSERTs on permission_check, '
    'updates flow via UPDATE → trigger → NOTIFY → agent LISTEN.';
COMMENT ON COLUMN thread_permission_requests.tool_call_id IS
    'The LangChain tool_call id from the AI message — opaque, used to '
    'correlate the decision back to the originating call.';
COMMENT ON COLUMN thread_permission_requests.decided_by IS
    'User id, MCP token id, or "system" (for timeout-driven expiry).';

-- NOTIFY trigger: emits on every status change so any LISTENing agent
-- can resume. Payload carries the row id and the new status; the agent
-- filters by id to match its own pending wait.
--
-- Channel name is global (one channel for all threads) — keeps the
-- listener count to one per agent regardless of how many requests fire.
-- Volume is tiny (one notify per permission resolve, well below the
-- Recall.ai-class LISTEN/NOTIFY scaling concern called out in §1 of the
-- feature doc).
CREATE OR REPLACE FUNCTION notify_thread_permission_update()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status <> OLD.status THEN
        PERFORM pg_notify(
            'thread_permission_updates',
            json_build_object(
                'id', NEW.id,
                'thread_id', NEW.thread_id,
                'status', NEW.status
            )::text
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS thread_permission_notify_trigger
    ON thread_permission_requests;
CREATE TRIGGER thread_permission_notify_trigger
    AFTER UPDATE ON thread_permission_requests
    FOR EACH ROW
    EXECUTE FUNCTION notify_thread_permission_update();

COMMIT;
