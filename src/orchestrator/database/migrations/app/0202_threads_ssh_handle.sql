-- migration:     0202_threads_ssh_handle.sql
-- description:   Short public handle addressing a session over SSH.
-- depends-on:    0201_user_ssh_keys.sql,
--                0185_thread_runtime_generation_retirement.sql
-- expected:     One nullable column on threads, plus firing any deferred
--               reciprocity-fence events an earlier migration in the same
--               transactional pass left queued. No row rewrites; existing rows
--               are backfilled lazily by the application on first read.
-- locks:        Brief ACCESS EXCLUSIVE on threads to attach the column.
--               Retried, because threads is hot and carries several triggers.
--               Also fires 0185's threads_agent_reciprocity_fence early and
--               restores its declared timing; see the note below.
-- transactional: yes
-- rollout:      Inert until the ssh-gateway ships.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Drain 0185's deferred reciprocity fence before touching the table.
--
-- 0191_stateless_input_deliveries.sql runs `UPDATE public.threads` while
-- 0185's threads_agent_reciprocity_fence is installed as DEFERRABLE INITIALLY
-- DEFERRED, so that UPDATE leaves AFTER-row events queued until COMMIT.  The
-- runner applies EVERY transactional migration inside ONE transaction
-- (run_migrations' `async with conn.transaction()` in migrate.py, under the
-- advisory lock), so on any upgrade whose unapplied span still contains 0191
-- -- i.e. any install older than 0191 that has rows in threads -- those events
-- are still pending when this file runs, and Postgres refuses
--     ALTER TABLE ... because it has pending trigger events
-- aborting the whole transactional pass and hard-failing boot.  A fresh
-- database is immune only because the UPDATE matches no rows, which is why the
-- schema snapshot never caught it.
--
-- Firing this one constraint early is equivalent to letting it fire at COMMIT:
-- nothing between here and COMMIT writes threads or agents again.  Scope it to
-- the single constraint rather than SET CONSTRAINTS ALL, which would also
-- defer officer_ticket_claim_job_integrity (DEFERRABLE INITIALLY IMMEDIATE)
-- for every migration appended after this one.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_constraint
         WHERE conname      = 'threads_agent_reciprocity_fence'
           AND connamespace = 'public'::regnamespace
           AND conrelid     = 'public.threads'::regclass
           AND condeferrable
    ) THEN
        EXECUTE 'SET CONSTRAINTS public.threads_agent_reciprocity_fence IMMEDIATE';
    END IF;
END $$;

DO $$
DECLARE
    attempt int := 0;
BEGIN
    LOOP
        BEGIN
            ALTER TABLE public.threads ADD COLUMN IF NOT EXISTS ssh_handle text;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            attempt := attempt + 1;
            IF attempt >= 10 THEN
                RAISE;
            END IF;
            PERFORM pg_sleep(1);
        END;
    END LOOP;
END $$;

-- Put the fence back the way 0185 declared it, so a migration appended after
-- this one still sees INITIALLY DEFERRED timing.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_constraint
         WHERE conname      = 'threads_agent_reciprocity_fence'
           AND connamespace = 'public'::regnamespace
           AND conrelid     = 'public.threads'::regclass
           AND condeferrable
    ) THEN
        EXECUTE 'SET CONSTRAINTS public.threads_agent_reciprocity_fence DEFERRED';
    END IF;
END $$;

COMMIT;
