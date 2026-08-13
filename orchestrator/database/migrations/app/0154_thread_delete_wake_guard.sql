-- migration:     0154_thread_delete_wake_guard.sql
-- description:   Settle completion wakes inside every thread DELETE, including
--                raw deletes issued by an overlapping pre-M3 orchestrator,
--                before the creator FK can null their destination.
-- depends-on:    0153_thread_permission_lease_comment.sql
-- expected:      < 1s. One trigger function plus one row trigger on threads;
--                no data scan. Each delete updates only jobs for OLD.id.
-- locks:         Brief SHARE ROW EXCLUSIVE on threads for CREATE TRIGGER;
--                ACCESS EXCLUSIVE on the new function only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE FUNCTION public.settle_job_wakes_before_thread_delete()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    UPDATE public.jobs
    SET wake_state = 'undeliverable',
        wake_claimed_at = NULL,
        updated_at = CURRENT_TIMESTAMP
    WHERE wake_on_complete
      AND created_by_thread_id = OLD.id
      AND wake_state IN ('none', 'pending', 'sending');
    RETURN OLD;
END;
$function$;

COMMENT ON FUNCTION public.settle_job_wakes_before_thread_delete() IS
    'Atomically retires open completion wakes before jobs.creator FK ON DELETE '
    'SET NULL. The trigger is the rolling-version guard for old/raw thread '
    'deletes; PostgresDB.delete_thread performs the same update explicitly.';

CREATE TRIGGER settle_job_wakes_before_thread_delete
BEFORE DELETE ON public.threads
FOR EACH ROW
EXECUTE FUNCTION public.settle_job_wakes_before_thread_delete();

COMMIT;
