-- migration:     0004_thread_events.sql
-- description:   Append-only event log for persistent-session subscribers.
--
--                Phase 2 of headless persistent sessions
--                (docs/features/headless_persistent_sessions.md). The
--                agent's persistent loop writes every frame the cockpit
--                would have seen — streaming tokens, tool events, turn
--                lifecycle, status — to this table. Reconnecting clients
--                request replay since their last seen `(epoch, seq)` via
--                the SSE endpoint at GET /api/threads/{id}/stream.
--
--                Per-thread cursor is (epoch, seq):
--                  - seq is monotonic per (thread_id, epoch), allocated by
--                    the agent.
--                  - epoch bumps on agent pod restart with cold checkpoint
--                    (and lives on the threads.events_epoch column added
--                    by this migration). A client cursor whose epoch
--                    doesn't match the server forces a full re-sync via
--                    the GONE_BEYOND_HORIZON event — mirrors Discord's
--                    gateway resume protocol, prevents the discord-net
--                    `#938` infinite-loop class of bug.
--
--                Retention is tiered (driven by the prune sweeper):
--                  - 7 days for threads in active/awaiting_user/suspended
--                  - 24 h  for threads in 'ended' (transcript lives in
--                          thread_messages, which is durable)
--                Long enough that a Friday-evening notification opened
--                Monday morning still replays cleanly.
--
--                Distinct from thread_messages: that table holds
--                completed-turn transcripts (one row per assistant/user
--                message). This one holds the wire-level frame stream
--                (tokens, tool starts, tool results, status, etc.).
-- depends-on:    0003_agent_intents.sql
-- expected:      < 1s on empty DB; on populated DB, < 5s (one CREATE TABLE,
--                two indexes, one ADD COLUMN — all metadata-only).
-- locks:         AccessExclusiveLock briefly for CREATE TABLE / ALTER
--                TABLE ADD COLUMN (NOT NULL with default is metadata-only
--                on PG 11+). Index creation is not concurrent — the table
--                is freshly created so no contention.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS thread_events (
    id          BIGSERIAL    PRIMARY KEY,
    thread_id   UUID         NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    epoch       INTEGER      NOT NULL,
    seq         BIGINT       NOT NULL,
    kind        TEXT         NOT NULL,
    payload     JSONB        NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Replay path: cursor lookup is always (thread_id, epoch, seq).
-- Unique because the agent serializes seq allocation per epoch.
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_events_thread_epoch_seq
    ON thread_events (thread_id, epoch, seq);

-- Retention prune path: WHERE thread_id IN (...) AND created_at < $cutoff.
CREATE INDEX IF NOT EXISTS idx_thread_events_thread_created
    ON thread_events (thread_id, created_at);

COMMENT ON TABLE thread_events IS
    'Append-only wire-frame log for persistent-session SSE replay. '
    'See docs/features/headless_persistent_sessions.md.';
COMMENT ON COLUMN thread_events.epoch IS
    'Bumped on agent pod restart with cold checkpoint. Client cursors '
    'whose epoch != current force a full re-sync (GONE_BEYOND_HORIZON).';
COMMENT ON COLUMN thread_events.seq IS
    'Monotonic per (thread_id, epoch). Allocated by the agent.';
COMMENT ON COLUMN thread_events.kind IS
    'Frame method: token, thinking, tool.started, tool.completed, '
    'turn.started, turn.completed, permission.request, ready, error, '
    'session.ended, etc. Same vocabulary as the legacy WS frames.';

-- The current epoch for each thread. Bumped by the agent on attach when
-- the previous epoch already has events (i.e. a cold-checkpoint restart).
ALTER TABLE threads
    ADD COLUMN IF NOT EXISTS events_epoch INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN threads.events_epoch IS
    'Current event-log epoch for this thread. Agent bumps on attach when '
    'a cold checkpoint restart loses its in-memory seq counter; client '
    'cursors from older epochs trigger a full re-sync.';

COMMIT;
