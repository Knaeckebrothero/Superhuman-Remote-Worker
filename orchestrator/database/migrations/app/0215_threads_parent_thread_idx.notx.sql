-- migration:     0215_threads_parent_thread_idx.notx.sql
-- description:   Partial index for the U5 per-session child roster and
--                parent-tool-call replay lookup, built without blocking the
--                hot sessions table.
-- depends-on:    0214_threads_subagent_parent_shape.sql
-- expected:      One concurrent partial index build. Session roots and
--                worker-owned children carry NULL parent_thread_id and create
--                no index entry.
-- locks:         SHARE UPDATE EXCLUSIVE only (CONCURRENTLY).
-- transactional: no
-- rollout:       Deliberately no IF NOT EXISTS: an invalid shell from a failed
--                concurrent build must be repaired before the migration ledger
--                is marked successful (the same rule as 0207).

-- squawk-ignore prefer-robust-stmts
CREATE INDEX CONCURRENTLY idx_threads_parent_thread
    ON public.threads (parent_thread_id)
    WHERE parent_thread_id IS NOT NULL;
