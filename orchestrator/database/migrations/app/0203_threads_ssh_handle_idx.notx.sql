-- migration:     0203_threads_ssh_handle_idx.notx.sql
-- description:   Unique index backing threads.ssh_handle, built concurrently.
-- depends-on:    0202_threads_ssh_handle.sql
-- expected:     One index build over threads with no exclusive lock. Partial,
--               so it holds only rows that actually carry a handle: backfill
--               is lazy, so nearly every existing row stays NULL for a long
--               time, and a full index would add an entry per thread row plus
--               write amplification on a hot table for nothing.
-- locks:        SHARE UPDATE EXCLUSIVE only (CONCURRENTLY).
-- transactional: no
-- rollout:      Deliberately NOT "IF NOT EXISTS", following 0132's runbook:
--               IF NOT EXISTS reports success against an INVALID same-name
--               shell left by a failed concurrent build, which would record
--               0203 as applied while uniqueness went unenforced. It buys
--               nothing on the success path either — the runner re-reads
--               schema_migrations and skips an applied .notx migration. So a
--               failed build must be repaired explicitly:
--                   SELECT i.indisvalid, i.indisready
--                     FROM pg_index AS i
--                     JOIN pg_class AS c ON c.oid = i.indexrelid
--                    WHERE c.relname = 'idx_threads_ssh_handle';
--               DROP INDEX CONCURRENTLY the invalid shell, repair the dirty
--               ledger row, then re-run. A duplicate handle is not cosmetic:
--               the handle is how SSH addresses a session, so an unenforced
--               index routes a connection to the wrong workspace.
--               This index IS the uniqueness enforcement; there is no
--               follow-up constraint migration to adopt it.

CREATE UNIQUE INDEX CONCURRENTLY idx_threads_ssh_handle
    ON public.threads (ssh_handle)
    WHERE ssh_handle IS NOT NULL;
