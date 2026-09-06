-- migration:     0219_threads_job_parent_tool_call_unique.notx.sql
-- description:   Make one worker delegate_agent tool call name at most one
--                durable child, including across a lost background-create
--                response or concurrent retry.
-- depends-on:    0218_threads_job_parent_tool_call_dedupe.sql
-- expected:      One concurrent partial UNIQUE index build. Session children
--                and children without a tool-call id create no entry.
-- locks:         SHARE UPDATE EXCLUSIVE only (CONCURRENTLY).
-- transactional: no
-- rollout:       Deliberately no IF NOT EXISTS: an invalid shell from a failed
--                concurrent build must be repaired before the migration ledger
--                is marked successful.

-- squawk-ignore prefer-robust-stmts
CREATE UNIQUE INDEX CONCURRENTLY idx_threads_job_parent_tool_call
    ON public.threads (parent_job_id, parent_tool_call_id)
    WHERE kind = 'subagent'
      AND parent_job_id IS NOT NULL
      AND parent_thread_id IS NULL
      AND parent_tool_call_id IS NOT NULL;
