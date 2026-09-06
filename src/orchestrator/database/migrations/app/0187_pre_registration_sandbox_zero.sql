-- migration:     0187_pre_registration_sandbox_zero.sql
-- description:   Permit one combined orchestrator sandbox-zero receipt when
--                a never-attached pinned generation captured both an agent
--                Pod marker and a physical workspace.
-- depends-on:    0186_protected_cloud_instance_authority.sql
-- expected:      < 1s. Replace one trigger function; no row rewrite.
-- locks:         Brief function-catalog lock; no table lock or validation scan.
-- transactional: yes
-- rollout:       Backward compatible with 0185 writers. The new application
--                branch is served only after startup migrations complete.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

DO $migration$
DECLARE
    original_definition text;
    patched_definition  text;
    old_fragment text := $old$NEW.runtime_retirement_local_quiescence
                            ->>'quiescence_protocol' <> 'agent_runtime_zero_v1'
                        OR NEW.runtime_retirement_local_quiescence
                            ->>'agent_pod_name'$old$;
    new_fragment text := $new$NEW.runtime_retirement_local_quiescence
                            ->>'quiescence_protocol'
                           NOT IN ('agent_runtime_zero_v1',
                                   'sandbox_actuator_zero_v1')
                        OR NEW.runtime_retirement_local_quiescence
                            ->>'agent_pod_name'$new$;
    occurrence_count integer;
BEGIN
    SELECT pg_get_functiondef(
        'public.enforce_thread_ended_transition()'::regprocedure
    )
      INTO original_definition;

    occurrence_count := (
        length(original_definition)
        - length(replace(original_definition, old_fragment, ''))
    ) / length(old_fragment);
    IF occurrence_count <> 1 THEN
        RAISE EXCEPTION
            '0187 expected one pre-registration receipt predicate, found %',
            occurrence_count;
    END IF;

    patched_definition := replace(
        original_definition,
        old_fragment,
        new_fragment
    );
    EXECUTE patched_definition;

    SELECT pg_get_functiondef(
        'public.enforce_thread_ended_transition()'::regprocedure
    )
      INTO patched_definition;
    IF position(new_fragment IN patched_definition) = 0
       OR position(old_fragment IN patched_definition) <> 0 THEN
        RAISE EXCEPTION
            '0187 pre-registration sandbox-zero predicate did not install';
    END IF;
END;
$migration$;

COMMIT;
