-- migration:     0020_thread_messages_window_index.notx.sql
-- description:   Composite index for windowed hydration (spec D4): last-N and
--                scroll-back pagination order by (thread_id, turn_number,
--                created_at). CREATE INDEX CONCURRENTLY -> runs outside a txn
--                (.notx), no AccessExclusiveLock, safe on a populated table.
-- depends-on:    0019_thread_messages_components.sql
-- expected:      seconds on dev (small table).
-- locks:         ShareUpdateExclusiveLock only (CONCURRENTLY).
-- transactional: NO. CONCURRENTLY can't run inside a transaction block;
--                hence the .notx.sql suffix the runner recognises.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_thread_messages_thread_turn_created
    ON thread_messages (thread_id, turn_number, created_at);
