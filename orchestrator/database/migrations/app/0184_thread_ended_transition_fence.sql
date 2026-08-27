-- migration:     0184_thread_ended_transition_fence.sql
-- description:   Make ended persistent sessions terminal for mixed-version
--                writers. Only the resume-shaped ended -> created edge is
--                allowed; old agents cannot write active/awaiting/suspended.
-- depends-on:    0183_persistent_input_delivery_cancellation.sql
-- expected:      < 1s. One trigger/function; no row rewrite.
-- locks:         SHARE ROW EXCLUSIVE on threads while the trigger is created.
-- transactional: yes
-- rollout:       Safe before or after application code. Existing Resume
--                already writes created, so mixed-version owner resume and
--                rollback remain available while stale agents fail closed.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE OR REPLACE FUNCTION public.enforce_thread_ended_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'ended' AND NEW.status NOT IN ('ended', 'created') THEN
        RAISE EXCEPTION
            'ended thread % may only take the resume-shaped created edge', OLD.id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_ended_transition_fence';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS threads_ended_transition_fence ON public.threads;
CREATE TRIGGER threads_ended_transition_fence
BEFORE UPDATE OF status ON public.threads
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_ended_transition();

COMMENT ON FUNCTION public.enforce_thread_ended_transition() IS
    'Mixed-version fence: ended stays ended or takes the resume-shaped created edge.';

COMMIT;
