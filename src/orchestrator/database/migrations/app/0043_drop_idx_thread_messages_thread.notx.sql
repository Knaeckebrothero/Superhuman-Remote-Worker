-- migration:     0043_drop_idx_thread_messages_thread.notx.sql
-- description:   Drop idx_thread_messages_thread — thread_messages(thread_id)
--                (0001_initial.sql:897) is a prefix-subset of both
--                idx_thread_messages_thread_turn_created (0020) and
--                idx_thread_messages_thread_seq (0023); the planner uses
--                either composite for thread_id-only lookups. Redundant
--                index on the hottest write table (every persisted session
--                message maintains it). QW-3,
--                docs/features/database_roadmap.md Phase 1.
-- depends-on:    0020_thread_messages_window_index.notx.sql,
--                0023_thread_messages_seq.sql
-- expected:      < 1s on dev. CONCURRENTLY waits out in-flight queries.
-- locks:         non-blocking — DROP INDEX CONCURRENTLY doesn't block
--                reads/writes on thread_messages.
-- transactional: NO. DROP INDEX CONCURRENTLY can't run inside a transaction
--                block; hence the .notx.sql suffix (one statement per file).

DROP INDEX CONCURRENTLY IF EXISTS idx_thread_messages_thread;
