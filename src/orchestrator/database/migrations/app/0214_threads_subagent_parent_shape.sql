-- migration:     0214_threads_subagent_parent_shape.sql
-- description:   Make the U3/U5 child-parent shape explicit: a session is a
--                root row, while a subagent belongs to exactly one worker job
--                or one parent session. Existing U3 rows that accidentally
--                copied a diagnostic parent_thread_id beside parent_job_id are
--                unambiguously worker children and are repaired to the sole
--                job parent before validation.
-- depends-on:    0208_threads_subagent_validate.sql
-- expected:      One catalog-only NOT VALID CHECK add, then a narrow repair
--                of contradictory pre-U5 worker children. Validation is split
--                into the runner-barrier 0216 .notx pass so the historical
--                scan does not run while this transaction retains the ADD
--                CONSTRAINT lock.
-- locks:         Brief ACCESS EXCLUSIVE on threads for ADD CONSTRAINT; the
--                bounded repair takes ordinary row locks first.
-- transactional: yes
-- rollout:       U5 is the first writer of a thread-only parent. Before this
--                migration every legitimate subagent has parent_job_id, so a
--                row carrying both parents can only be the old worker shape.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_constraint
         WHERE conname = 'threads_parent_shape_check'
           AND conrelid = 'public.threads'::regclass
    ) THEN
        ALTER TABLE public.threads
            ADD CONSTRAINT threads_parent_shape_check CHECK (
                (
                    kind = 'session'
                    AND parent_job_id IS NULL
                    AND parent_thread_id IS NULL
                )
                OR
                (
                    kind = 'subagent'
                    AND num_nonnulls(parent_job_id, parent_thread_id) = 1
                )
            ) NOT VALID;
    END IF;
END $$;

-- Add first, repair second. Updating threads before ALTER TABLE can queue the
-- deferred 0185 reciprocity constraint trigger and make PostgreSQL reject the
-- ALTER with "pending trigger events". NOT VALID still enforces the new shape
-- on this repair UPDATE, which moves every touched row toward validity.
UPDATE public.threads
   SET parent_thread_id = NULL
 WHERE kind = 'subagent'
   AND parent_job_id IS NOT NULL
   AND parent_thread_id IS NOT NULL;

COMMIT;
