-- migration:     0042_drop_idx_threads_user_id.notx.sql
-- description:   Drop idx_threads_user_id — a true duplicate of
--                idx_threads_user (0001_initial.sql:861) on threads(user_id);
--                0012 recreated the same index under a new name (its header
--                assumed the table shipped without one). Redundant index on
--                one of the two hottest write tables. QW-3,
--                docs/features/database_roadmap.md Phase 1. Verified
--                2026-07-01: no code references either index by name.
-- depends-on:    0012_threads_user_id_index.notx.sql
-- expected:      < 1s. DROP INDEX CONCURRENTLY waits out in-flight queries.
-- locks:         non-blocking — CONCURRENTLY doesn't block reads/writes on
--                threads. idx_threads_user (0001) keeps serving user_id
--                lookups.
-- transactional: NO. DROP INDEX CONCURRENTLY can't run inside a transaction
--                block; hence the .notx.sql suffix. One statement per file —
--                the runner executes the file as a single simple-query
--                message, which would wrap multiple statements in an
--                implicit transaction.

DROP INDEX CONCURRENTLY IF EXISTS idx_threads_user_id;
