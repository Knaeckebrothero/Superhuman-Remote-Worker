-- VM heartbeats previously created untyped IDE activity placeholders. Repair
-- only their exact telemetry shape on controller-authenticated VM generations.
-- No runtime endpoint, unknown field or IDE lifecycle authority is reclassified.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '15min';

CREATE OR REPLACE FUNCTION public.vm_ide_heartbeat_cleanup_is_authorized(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    old_state JSONB,
    new_state JSONB
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
AS $function$
    SELECT COALESCE(
      requested_owner_kind IN ('job', 'thread')
      AND requested_owner_id IS NOT NULL
      AND new_state = old_state - 'ide_session'
      AND jsonb_typeof(old_state -> 'ide_session') = 'object'
      AND (old_state -> 'ide_session')
            - ARRAY['status', 'code_server_connections', 'last_activity'] = '{}'::JSONB
      AND old_state -> 'ide_session' ->> 'status' IN ('active', 'idle')
      AND jsonb_typeof(old_state -> 'ide_session' -> 'code_server_connections') = 'number'
      AND old_state -> 'ide_session' ->> 'code_server_connections' ~ '^(0|[1-9][0-9]*)$'
      AND (NOT (old_state -> 'ide_session' ? 'last_activity')
           OR jsonb_typeof(old_state -> 'ide_session' -> 'last_activity') = 'string')
      AND old_state -> 'vm' -> 'identity_authenticated' = 'true'::JSONB
      AND old_state -> 'vm' ->> 'provision_generation'
            ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
      AND old_state -> 'vm' ->> 'identity_provision_generation'
            = old_state -> 'vm' ->> 'provision_generation'
      AND old_state -> 'vm' ->> 'vm_uid'
            ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
      AND NOT EXISTS (
          SELECT 1 FROM public.managed_repository_workspace_creation_reservations AS reservation
          WHERE reservation.owner_kind = requested_owner_kind
            AND reservation.owner_id = requested_owner_id AND reservation.scope = 'ide'
      )
      AND NOT EXISTS (
          SELECT 1 FROM public.managed_repository_workspace_cleanup_intents AS intent
          WHERE intent.owner_kind = requested_owner_kind
            AND intent.owner_id = requested_owner_id AND intent.scope = 'ide'
      )
      AND NOT EXISTS (
          SELECT 1 FROM public.managed_repository_process_zero_receipts AS receipt
          WHERE receipt.owner_kind = requested_owner_kind
            AND receipt.owner_id = requested_owner_id AND receipt.scope = 'ide'
      )
    , FALSE);
$function$;

-- Permit only removal of the exact telemetry placeholder. The process-zero, creation,
-- cleanup and owner-delete fences still govern all actual runtime transitions.
DO $migration$
DECLARE
    definition TEXT;
    old_fragment TEXT := $old$           AND NOT terminal_cancel_projection_authorized THEN$old$;
    new_fragment TEXT := $new$           AND NOT terminal_cancel_projection_authorized
           AND NOT (
               scope_name = 'ide'
               AND public.vm_ide_heartbeat_cleanup_is_authorized(
                   source_kind, source_id, old_state, new_state
               )
           ) THEN$new$;
BEGIN
    definition := pg_get_functiondef('public.prevent_retired_workspace_runtime_rebinding()'::regprocedure);
    IF strpos(definition, new_fragment) > 0 THEN
        RETURN;
    END IF;
    IF strpos(definition, old_fragment) = 0 THEN
        RAISE EXCEPTION 'Unexpected prevent_retired_workspace_runtime_rebinding definition';
    END IF;
    EXECUTE replace(definition, old_fragment, new_fragment);
END;
$migration$;

DO $migration$
DECLARE
    definition TEXT;
    old_fragment TEXT := $old$        new_ide := COALESCE(new_state->'ide_session', '{}'::JSONB);$old$;
    new_fragment TEXT := $new$        new_ide := COALESCE(new_state->'ide_session', '{}'::JSONB);
        IF public.vm_ide_heartbeat_cleanup_is_authorized(
            source_kind, source_id, old_state, new_state
        ) THEN
            -- No endpoint/process was named by this exact legacy placeholder.
            -- The VM itself still requires its independent process-zero receipt.
            old_ide := '{}'::JSONB;
        END IF;$new$;
BEGIN
    definition := pg_get_functiondef('public.enforce_managed_repository_process_zero_transition()'::regprocedure);
    IF strpos(definition, new_fragment) > 0 THEN
        RETURN;
    END IF;
    IF strpos(definition, old_fragment) = 0 THEN
        RAISE EXCEPTION 'Unexpected enforce_managed_repository_process_zero_transition definition';
    END IF;
    EXECUTE replace(definition, old_fragment, new_fragment);
END;
$migration$;

WITH owners AS (
    SELECT 'job'::TEXT AS owner_kind, id, context AS state FROM public.jobs
    UNION ALL
    SELECT 'thread'::TEXT AS owner_kind, id, metadata AS state FROM public.threads
), eligible AS (
    SELECT owner_kind, id, state FROM owners
    WHERE public.vm_ide_heartbeat_cleanup_is_authorized(
        owner_kind, id, state, state - 'ide_session'
    )
), repaired_jobs AS (
    UPDATE public.jobs AS job
       SET context = job.context - 'ide_session'
      FROM eligible
     WHERE eligible.owner_kind = 'job' AND job.id = eligible.id
       AND job.context = eligible.state
    RETURNING job.id
)
UPDATE public.threads AS thread
   SET metadata = thread.metadata - 'ide_session'
  FROM eligible
 WHERE eligible.owner_kind = 'thread' AND thread.id = eligible.id
   AND thread.metadata = eligible.state;

COMMIT;
