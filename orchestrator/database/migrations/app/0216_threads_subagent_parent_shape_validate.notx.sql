-- migration:     0216_threads_subagent_parent_shape_validate.notx.sql
-- description:   Validate the U5 parent-shape CHECK installed NOT VALID by
--                0214 after its deterministic worker-row repair.
-- depends-on:    0214_threads_subagent_parent_shape.sql,
--                0215_threads_parent_thread_idx.notx.sql
-- expected:      One scan of threads under SHARE UPDATE EXCLUSIVE. A failure
--                identifies a parentless child or a non-root session and must
--                be reconciled rather than silently widened.
-- locks:         SHARE UPDATE EXCLUSIVE on threads; ordinary reads and writes
--                continue while PostgreSQL validates.
-- transactional: no (runner barrier; PostgreSQL executes this one statement
--                atomically). The app runner puts every pending transactional
--                file in one outer transaction, so this must live in the
--                later .notx pass or 0214's ACCESS EXCLUSIVE lock would stay
--                held across the validation scan and NOT VALID would buy us
--                nothing. Its filename sorts after the concurrent index.

DO $$
BEGIN
    -- This local setting reaches the nested utility statement without adding
    -- a second top-level statement (a .notx file is sent as one simple query).
    PERFORM pg_catalog.set_config('lock_timeout', '2s', true);
    EXECUTE 'ALTER TABLE public.threads '
            'VALIDATE CONSTRAINT threads_parent_shape_check';
END $$;
