-- migration:     0220_stateless_subagent_recovery_events.sql
-- description:   Admit generation-stable subagent recovery evidence as a
--                stateless session server event.
-- depends-on:    0219_threads_job_parent_tool_call_unique.notx.sql
-- expected:      Function replacement only; no row rewrite.
-- locks:         Brief function catalog lock only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.thread_input_deliveries
    ADD COLUMN supersedes_input_seq BIGINT
        CHECK (supersedes_input_seq IS NULL OR supersedes_input_seq > 0);

COMMENT ON COLUMN public.thread_input_deliveries.supersedes_input_seq IS
    'For a foreground-child recovery event, the exact abandoned parent input '
    'sequence replaced by this evidence/continuation turn. NULL otherwise.';

CREATE OR REPLACE FUNCTION public.require_input_delivery_lane_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_lane TEXT;
    message_role TEXT;
BEGIN
    SELECT thread.execution_lane
      INTO current_lane
      FROM public.threads AS thread
     WHERE thread.id = NEW.thread_id
     FOR NO KEY UPDATE;

    IF current_lane IS NULL OR NEW.execution_lane IS DISTINCT FROM current_lane THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'input_delivery_lane_mismatch',
            MESSAGE = 'Input delivery lane does not match its owning thread',
            HINT = 'Retry from a lane-aware input-delivery writer.';
    END IF;

    IF current_lane = 'stateless' THEN
        SELECT message.role
          INTO message_role
          FROM public.thread_messages AS message
         WHERE message.id = NEW.message_id
           AND message.thread_id = NEW.thread_id;
        IF message_role IS DISTINCT FROM 'event'
           OR NEW.source NOT IN ('officer_wake', 'subagent') THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'stateless_input_delivery_event_only',
                MESSAGE = 'Stateless durable input authority is reserved for server events';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

COMMIT;
