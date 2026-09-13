-- migration:     0247_stateless_none_workspace_outcome.sql
-- description:   Distinguish a settled backend=none retention outcome from
--                provisioner-owned workspace authority.
-- depends-on:    0246_vm_ide_heartbeat_projection.sql
-- expected:      < 1s. Adds one immutable classifier and replaces one trigger
--                function from its catalog definition; no row scan or rewrite.
-- locks:         Function-catalog lock only; installed row triggers remain.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- A backend=none session has no workspace process, endpoint, lease, binding,
-- or external resource to retire.  Soft End nevertheless records the generic
-- volume-retention outcome used by Resume/UI reporting.  Recognize only that
-- exact settled shape: unknown fields, non-boolean outcomes, a backing/runtime
-- identity, or any live retirement/claim marker remain authority-bearing and
-- continue through the process-zero refusal below.
CREATE OR REPLACE FUNCTION public.stateless_none_workspace_outcome_is_authority_free(
    requested_state JSONB
)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
AS $function$
    SELECT COALESCE(
        jsonb_typeof(requested_state) = 'object'
        AND requested_state #>> '{config_override,workspace,backend}' = 'none'
        AND COALESCE(requested_state->'_workspace_binding', '{}'::JSONB)
            = '{}'::JSONB
        AND jsonb_typeof(requested_state->'workspace_container') = 'object'
        AND requested_state->'workspace_container' ? 'volume_reclaimed'
        AND jsonb_typeof(
            requested_state->'workspace_container'->'volume_reclaimed'
        ) = 'boolean'
        AND (requested_state->'workspace_container')
            - ARRAY['status', 'volume_reclaimed'] = '{}'::JSONB
        AND (
            NOT (requested_state->'workspace_container' ? 'status')
            OR jsonb_typeof(
                requested_state->'workspace_container'->'status'
            ) = 'string'
        )
        AND jsonb_typeof(
            requested_state->'_stateless_workspace_retirement_settled'
        ) = 'object'
        AND (requested_state->'_stateless_workspace_retirement_settled')
            - ARRAY[
                'terminal_token',
                'cleanup_complete',
                'permanent',
                'backing_id',
                'runtime_incarnation',
                'snapshot_restore_required',
                'workspace_absence_proven'
            ] = '{}'::JSONB
        AND jsonb_typeof(
            requested_state
                #> '{_stateless_workspace_retirement_settled,terminal_token}'
        ) = 'number'
        AND requested_state
                #>> '{_stateless_workspace_retirement_settled,terminal_token}'
            ~ '^(0|[1-9][0-9]*)$'
        AND requested_state
                #> '{_stateless_workspace_retirement_settled,cleanup_complete}'
            = 'true'::JSONB
        AND jsonb_typeof(
            requested_state
                #> '{_stateless_workspace_retirement_settled,permanent}'
        ) = 'boolean'
        AND requested_state
                #> '{_stateless_workspace_retirement_settled,backing_id}'
            = 'null'::JSONB
        AND requested_state
                #> '{_stateless_workspace_retirement_settled,runtime_incarnation}'
            = 'null'::JSONB
        AND requested_state
                #> '{_stateless_workspace_retirement_settled,snapshot_restore_required}'
            = 'false'::JSONB
        AND requested_state
                #> '{_stateless_workspace_retirement_settled,workspace_absence_proven}'
            = 'false'::JSONB
        AND NOT requested_state ?| ARRAY[
            '_stateless_workspace_retirement_pending',
            '_stateless_claim_retirement',
            '_stateless_claim_losses',
            '_stateless_claim_loss_hold',
            '_stateless_active_claim'
        ],
        FALSE
    );
$function$;

DO $migration$
DECLARE
    definition TEXT;
    old_fragment TEXT := $old$    old_workspace := COALESCE(old_state->'workspace_container', '{}'::JSONB);
    new_workspace := COALESCE(new_state->'workspace_container', '{}'::JSONB);$old$;
    new_fragment TEXT := $new$    old_workspace := COALESCE(old_state->'workspace_container', '{}'::JSONB);
    new_workspace := COALESCE(new_state->'workspace_container', '{}'::JSONB);

    IF source_kind = 'thread'
       AND OLD.status::TEXT = 'ended'
       AND public.stateless_none_workspace_outcome_is_authority_free(old_state)
    THEN
        old_workspace := '{}'::JSONB;
    END IF;$new$;
BEGIN
    definition := pg_get_functiondef(
        'public.enforce_managed_repository_process_zero_transition()'::regprocedure
    );
    IF strpos(definition, new_fragment) > 0 THEN
        RETURN;
    END IF;
    IF strpos(definition, old_fragment) = 0 THEN
        RAISE EXCEPTION
            'Unexpected enforce_managed_repository_process_zero_transition definition';
    END IF;
    EXECUTE replace(definition, old_fragment, new_fragment);
END;
$migration$;

DO $migration$
DECLARE
    definition TEXT;
    old_fragment TEXT := $old$        IF source_kind = 'thread' AND scope_name = 'ide' THEN
            CONTINUE;
        END IF;$old$;
    new_fragment TEXT := $new$        IF source_kind = 'thread' AND scope_name = 'ide' THEN
            CONTINUE;
        END IF;
        IF source_kind = 'thread'
           AND scope_name = 'workspace_container'
           AND public.stateless_none_workspace_outcome_is_authority_free(
               source_state
           )
        THEN
            CONTINUE;
        END IF;$new$;
BEGIN
    definition := pg_get_functiondef(
        'public.prevent_workspace_owner_delete_before_cleanup()'::regprocedure
    );
    IF strpos(definition, new_fragment) > 0 THEN
        RETURN;
    END IF;
    IF strpos(definition, old_fragment) = 0 THEN
        RAISE EXCEPTION
            'Unexpected prevent_workspace_owner_delete_before_cleanup definition';
    END IF;
    EXECUTE replace(definition, old_fragment, new_fragment);
END;
$migration$;

COMMIT;
