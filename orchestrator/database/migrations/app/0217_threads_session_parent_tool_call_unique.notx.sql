-- migration:     0217_threads_session_parent_tool_call_unique.notx.sql
-- description:   Make one session delegate_agent tool call name at most one
--                durable child, including across a lost create response or
--                concurrent stateless-turn retries.
-- depends-on:    0216_threads_subagent_parent_shape_validate.notx.sql
-- expected:      One concurrent partial UNIQUE index build. Worker children
--                and children without a tool-call id create no entry.
-- locks:         SHARE UPDATE EXCLUSIVE only (CONCURRENTLY).
-- transactional: no
-- rollout:       Deliberately no IF NOT EXISTS: an invalid shell from a failed
--                concurrent build must be repaired before the migration ledger
--                is marked successful. Any duplicate U5 keys are unsafe
--                ambiguous executions and must be reconciled, never silently
--                selected by creation order.

-- squawk-ignore prefer-robust-stmts
CREATE UNIQUE INDEX CONCURRENTLY idx_threads_session_parent_tool_call
    ON public.threads (parent_thread_id, parent_tool_call_id)
    WHERE kind = 'subagent'
      AND parent_job_id IS NULL
      AND parent_thread_id IS NOT NULL
      AND parent_tool_call_id IS NOT NULL;
