-- Working heartbeats used to create timestamp-only workspace projections on
-- virtual/none Jobs. A timestamp is liveness metadata, not resource authority.
-- Preserve every gate for actual runtime fields and effectful reservations.

DO $migration$
DECLARE
    definition TEXT;
    old_fragment TEXT := $old$       AND old_workspace - 'status' <> '{}'::JSONB$old$;
    new_fragment TEXT := $new$       AND old_workspace - 'status' <> '{}'::JSONB
       -- A legacy Job heartbeat can leave only a timestamp. It never named
       -- compute, credentials, an endpoint or a provisioner to retire.
       AND NOT (
           source_kind = 'job'
           AND old_workspace - 'last_activity' = '{}'::JSONB
           AND jsonb_typeof(old_workspace -> 'last_activity') = 'string'
       )$new$;
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

DO $migration$
DECLARE
    definition TEXT;
    old_fragment TEXT := $old$        runtime_uid := runtime_state ->> '_runtime_incarnation';
        IF runtime_uid IS NULL THEN$old$;
    new_fragment TEXT := $new$        -- Only the legacy heartbeat's exact timestamp-only Job placeholder
        -- is metadata. Resource fields and all creation/cleanup receipts keep
        -- their existing authority requirements.
        IF source_kind = 'job' AND scope_name = 'workspace_container'
           AND runtime_state - 'last_activity' = '{}'::JSONB
           AND jsonb_typeof(runtime_state -> 'last_activity') = 'string' THEN
            CONTINUE;
        END IF;
        runtime_uid := runtime_state ->> '_runtime_incarnation';
        IF runtime_uid IS NULL THEN$new$;
BEGIN
    definition := pg_get_functiondef('public.prevent_workspace_owner_delete_before_cleanup()'::regprocedure);
    IF strpos(definition, new_fragment) > 0 THEN
        RETURN;
    END IF;
    IF strpos(definition, old_fragment) = 0 THEN
        RAISE EXCEPTION 'Unexpected prevent_workspace_owner_delete_before_cleanup definition';
    END IF;
    EXECUTE replace(definition, old_fragment, new_fragment);
END;
$migration$;
