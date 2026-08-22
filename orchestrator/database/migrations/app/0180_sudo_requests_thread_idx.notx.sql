-- migration:     0180_sudo_requests_thread_idx.notx.sql
-- description:   Index thread-scoped sudo requests for the per-thread listing
--                and the expiry sweep (thread_id, requested_at DESC), mirroring
--                the existing job_id access path.
-- depends-on:    0179_sudo_requests_entity_check.sql
-- expected:      < 1s on a small table; CONCURRENTLY so it never blocks the
--                sudo gate's inserts.
-- locks:         none beyond the SHARE UPDATE EXCLUSIVE that CONCURRENTLY takes.
-- transactional: NO (CREATE INDEX CONCURRENTLY cannot run inside one). ONE
--                statement only: the runner sends the file as a single simple
--                query, and Postgres wraps a multi-statement query in an
--                implicit transaction.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sudo_requests_thread
    ON public.sudo_approval_requests (thread_id, requested_at DESC);
