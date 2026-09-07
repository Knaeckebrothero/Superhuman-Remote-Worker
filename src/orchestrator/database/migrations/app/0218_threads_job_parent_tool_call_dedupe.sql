-- migration:     0218_threads_job_parent_tool_call_dedupe.sql
-- description:   Canonicalize historical U3 retries before enforcing the
--                worker-parent/tool-call idempotency key. Existing lookup
--                already selected the newest row; older shadowed rows keep
--                their transcript but relinquish the replay key.
-- depends-on:    0217_threads_session_parent_tool_call_unique.notx.sql
-- expected:      Usually zero rows. At most the older rows from worker calls
--                that were re-spawned after an ambiguous create/hard kill.
-- locks:         Ordinary row locks on duplicate worker-child rows only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY parent_job_id, parent_tool_call_id
               ORDER BY created_at DESC, id DESC
           ) AS replay_rank
      FROM public.threads
     WHERE kind = 'subagent'
       AND parent_job_id IS NOT NULL
       AND parent_thread_id IS NULL
       AND parent_tool_call_id IS NOT NULL
)
UPDATE public.threads AS child
   SET parent_tool_call_id = NULL
  FROM ranked
 WHERE ranked.id = child.id
   AND ranked.replay_rank > 1;

COMMIT;
