-- migration:     0208_threads_subagent_validate.sql
-- description:   Validate the kind CHECK and the two parent foreign keys
--                0206 installed NOT VALID on threads.
-- depends-on:    0206_threads_subagent_kind.sql
-- expected:      < 1s. Three scans of threads (tens to low thousands of rows).
--                Every pre-0206 row is a session with NULL parents and
--                kind='session' from the column default, so nothing can
--                fail validation; a failure therefore reports a child row
--                written between 0206 and this file against a job or thread
--                that no longer exists, and must be reconciled, never
--                silently widened.
-- locks:         SHARE UPDATE EXCLUSIVE on threads plus ROW SHARE on jobs
--                for the foreign-key scan; ordinary reads and writes continue
--                while PostgreSQL validates. No deferred-fence drain: 0202
--                fires 0185's reciprocity fence on any span that reaches back
--                past 0191, and no migration from 0202 to here writes threads
--                rows, so this ALTER can never meet pending trigger events.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.threads
    VALIDATE CONSTRAINT threads_kind_check,
    VALIDATE CONSTRAINT threads_parent_job_id_fkey,
    VALIDATE CONSTRAINT threads_parent_thread_id_fkey;

COMMIT;
