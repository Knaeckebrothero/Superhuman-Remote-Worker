-- migration:     0023_thread_messages_seq.sql
-- description:   Add a monotonic per-row insertion-order column `seq` to
--                thread_messages, plus a (thread_id, seq) index. `seq` is the
--                exact, tie-free cursor the persistent-session resume path keys
--                off: it lets a compaction summary record the precise message it
--                ends at (boundary_seq) and lets resume load
--                `summary + seq > boundary_seq` — the agent's real live working
--                set — instead of whole post-boundary turns or the entire
--                append-only log (the exit-137 OOM wedge on big threads).
--                (turn_number, created_at) can't do this: one turn is many
--                messages and parallel tool calls share a created_at.
--
--                Phase 1 of docs/issues/persistent_session_midturn_message_loss.md.
--                This migration only adds the column + index; the boundary_seq
--                write and seq-resume read land in later phases.
--
--                BIGSERIAL carries a volatile nextval() default, so this ADD
--                COLUMN rewrites the table to backfill existing rows. Backfill
--                order is physical (heap) order ≈ insertion order — good enough
--                for old threads, which resume via the boundary_turn fallback
--                anyway. New rows get a strictly-monotonic seq going forward.
-- depends-on:    0020_thread_messages_window_index.notx.sql
-- expected:      sub-second on dev (small table). Scales with row count because
--                of the rewrite; thread_messages is modest here.
-- locks:         AccessExclusiveLock on thread_messages for the ADD COLUMN
--                rewrite + the (non-concurrent) CREATE INDEX. Runs at startup
--                under the migration advisory lock, so no concurrent writers.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '5s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE thread_messages
    ADD COLUMN IF NOT EXISTS seq BIGSERIAL;

COMMENT ON COLUMN thread_messages.seq IS
    'Monotonic per-row insertion order (global BIGSERIAL). The resume cursor: a '
    'role=''summary'' row records metrics.boundary_seq = seq of the last message '
    'it covers, and resume loads summary + rows with seq > boundary_seq. Backfilled '
    'in ≈ insertion order on existing rows; strictly monotonic for new rows.';

CREATE INDEX IF NOT EXISTS idx_thread_messages_thread_seq
    ON thread_messages (thread_id, seq);

COMMIT;
