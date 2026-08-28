-- migration:     0203_threads_ssh_handle_idx.notx.sql
-- description:   Unique index backing threads.ssh_handle, built concurrently.
-- depends-on:    0202_threads_ssh_handle.sql
-- expected:     One index build over threads with no exclusive lock.
-- locks:        SHARE UPDATE EXCLUSIVE only (CONCURRENTLY).
-- transactional: no
-- rollout:      If this leaves an INVALID index behind after a failure, drop it
--               and re-run. The index is the uniqueness enforcement; there
--               is no follow-up constraint migration to adopt it.

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_threads_ssh_handle
    ON public.threads (ssh_handle);
