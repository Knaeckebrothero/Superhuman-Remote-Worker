-- migration:     0194_managed_repository_orphan_process_zero.sql
-- description:   Make an orphan process-zero receipt a permanent owner-UUID
--                tombstone so a deleted workspace owner cannot be resurrected.
-- depends-on:    0193_notifications_cutover.sql
-- expected:      < 1s. Two insert-only triggers; no row scan or rewrite.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on jobs and threads for
--                trigger installation.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE OR REPLACE FUNCTION public.prevent_process_zero_owner_resurrection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    source_kind TEXT;
BEGIN
    source_kind := CASE WHEN TG_TABLE_NAME = 'jobs' THEN 'job' ELSE 'thread' END;
    IF EXISTS (
        SELECT 1
          FROM public.managed_repository_process_zero_receipts AS receipt
         WHERE receipt.owner_kind = source_kind
           AND receipt.owner_id = NEW.id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_process_zero_owner_resurrection',
            MESSAGE = 'A process-zero workspace owner UUID cannot be reused';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_jobs_process_zero_owner_resurrection
    ON public.jobs;
CREATE TRIGGER trg_jobs_process_zero_owner_resurrection
BEFORE INSERT ON public.jobs
FOR EACH ROW EXECUTE FUNCTION public.prevent_process_zero_owner_resurrection();

DROP TRIGGER IF EXISTS trg_threads_process_zero_owner_resurrection
    ON public.threads;
CREATE TRIGGER trg_threads_process_zero_owner_resurrection
BEFORE INSERT ON public.threads
FOR EACH ROW EXECUTE FUNCTION public.prevent_process_zero_owner_resurrection();

COMMIT;
