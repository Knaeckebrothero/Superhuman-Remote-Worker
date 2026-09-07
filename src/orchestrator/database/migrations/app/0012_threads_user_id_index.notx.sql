-- migration:     0012_threads_user_id_index.notx.sql
-- description:   Index threads(user_id) so the per-user threads visibility
--                clause added by F2-F7 doesn't fall back to seq-scan on a
--                growing table.
--
--                Background: F1 of the multi-tenancy work (see
--                docs/multi_tenancy.md) centralises visibility queries in
--                orchestrator/security/access.py. The threads table is the
--                main per-user resource and ships without an index on
--                user_id — list_threads and SSE filter currently rely on
--                in-memory filtering, which becomes a hotspot once we close
--                the F1+ leaks and start gating by user_id everywhere.
-- depends-on:    0001_initial.sql
-- expected:      < 2s on dev (table is small). On a populated production
--                table this is concurrent and won't block writes.
-- locks:         ShareUpdateExclusiveLock — CREATE INDEX CONCURRENTLY
--                doesn't block reads or writes against threads.
-- transactional: NO. CONCURRENTLY can't run inside a transaction block;
--                hence the .notx.sql suffix the runner recognises.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_threads_user_id
    ON threads(user_id);
