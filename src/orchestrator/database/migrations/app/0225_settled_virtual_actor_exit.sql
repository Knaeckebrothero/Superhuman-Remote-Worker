-- A used virtual actor can exit after End closes admission but before it
-- publishes local quiescence. This separate recovery contract accepts only
-- settled work and an exact, protected, claim-free actor; it never adopts
-- unfinished child/input/control work. The caller must first finish the exact
-- Pod stop/finalizer actuator. Kubernetes absence alone is not this proof.
CREATE OR REPLACE FUNCTION public.acknowledge_settled_virtual_actor_exit(
    owner_id uuid, generation_id uuid, retirement_id uuid,
    actor_id uuid, attach_id uuid, stopped_pod_uid text
) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    owner_row public.threads%ROWTYPE;
    actor_row public.agents%ROWTYPE;
    context jsonb;
    marker jsonb;
    binding jsonb;
    receipt jsonb;
    input_count bigint;
    input_digest text;
BEGIN
    SELECT * INTO owner_row FROM public.threads
     WHERE id = owner_id FOR UPDATE;
    IF NOT FOUND OR owner_row.kind IS DISTINCT FROM 'session'
       OR owner_row.execution_lane IS DISTINCT FROM 'pinned'
       OR owner_row.runtime_generation IS DISTINCT FROM generation_id
       OR owner_row.runtime_retirement_token IS DISTINCT FROM retirement_id
       OR owner_row.runtime_retirement_authorized_at IS NULL
       OR owner_row.agent_id IS DISTINCT FROM actor_id
       OR owner_row.runtime_attach_token IS DISTINCT FROM attach_id
       OR (owner_row.control_admission_agent_id IS NOT NULL
           AND owner_row.control_admission_agent_id IS DISTINCT FROM actor_id)
       OR owner_row.runtime_authority_exposed IS DISTINCT FROM true
       OR actor_id IS NULL OR attach_id IS NULL
       OR NULLIF(stopped_pod_uid, '') IS NULL THEN
        RETURN NULL;
    END IF;
    context := owner_row.runtime_retirement_context;
    marker := context->'agent_pod';
    binding := context->'workspace_binding';
    IF context->>'generation' IS DISTINCT FROM generation_id::text
       OR context->>'agent_id' IS DISTINCT FROM actor_id::text
       OR context->>'runtime_attach_token' IS DISTINCT FROM attach_id::text
       OR context->>'workspace_backend' IS DISTINCT FROM 'virtual'
       OR context->>'settle_status' IS DISTINCT FROM 'ended'
       OR jsonb_typeof(binding) IS DISTINCT FROM 'object'
       OR binding->>'kind' IS DISTINCT FROM 'virtual'
       OR COALESCE(binding->>'backing_id', '') !~ '^rclone:[0-9a-f]{64}$'
       OR COALESCE(binding->>'generation', '') !~
          '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR binding->'ssh_host_key_fingerprint' IS DISTINCT FROM 'null'::jsonb
       OR binding - ARRAY['generation','kind','backing_id','ssh_host_key_fingerprint']
          IS DISTINCT FROM '{}'::jsonb
       OR owner_row.metadata->'_workspace_binding' IS DISTINCT FROM binding
       OR owner_row.metadata->'agent_pod' IS DISTINCT FROM marker
       OR COALESCE(owner_row.metadata->'protected_cloud', 'false'::jsonb)
          IS DISTINCT FROM 'false'::jsonb
       OR EXISTS (
           SELECT 1 FROM unnest(ARRAY[
               'workspace_container','vm','workspace_provision_intent',
               'agent_workspace_claim','agent_pod_provision_intent','protected_ro'
           ]) AS field
           WHERE COALESCE(context->field, 'null'::jsonb)
                 NOT IN ('null'::jsonb, '{}'::jsonb)
       )
       OR marker->>'pod_uid' IS DISTINCT FROM stopped_pod_uid
       OR marker->>'protection_protocol' IS DISTINCT FROM 'finalizer_v1'
       OR NULLIF(marker->>'namespace', '') IS NULL
       OR NULLIF(marker->>'pod_name', '') IS NULL
       OR context->'agent'->>'hostname' IS DISTINCT FROM marker->>'pod_name'
       OR context->'agent'->>'pod_uid' IS DISTINCT FROM stopped_pod_uid
       OR NOT EXISTS (
           SELECT 1 FROM public.thread_agent_pod_provision_intents intent
            WHERE intent.thread_id = owner_id
              AND intent.runtime_generation = generation_id
              AND intent.attempt_id::text = marker->>'provision_attempt'
              AND intent.pod_name = marker->>'pod_name'
              AND intent.pod_uid = stopped_pod_uid
              AND intent.namespace = marker->>'namespace'
              AND intent.protection_protocol = 'finalizer_v1'
              AND intent.status = 'published'
              AND intent.workspace_claim_id IS NULL
       ) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO actor_row FROM public.agents
     WHERE id = actor_id FOR SHARE;
    IF NOT FOUND OR actor_row.thread_id IS DISTINCT FROM owner_id
       OR actor_row.pod_uid IS DISTINCT FROM stopped_pod_uid
       OR actor_row.hostname IS DISTINCT FROM marker->>'pod_name'
       OR actor_row.current_job_id IS NOT NULL
       OR EXISTS (SELECT 1 FROM public.agents other
                   WHERE other.id <> actor_id
                     AND (other.thread_id = owner_id OR other.pod_uid = stopped_pod_uid))
    THEN
        RETURN NULL;
    END IF;

    -- All supported input/child/control admission locks the owner first and
    -- rejects its retirement token. These reads run after that durable fence,
    -- not before a still-open producer can commit an admission.
    IF EXISTS (SELECT 1 FROM public.thread_input_deliveries
                WHERE thread_id = owner_id AND state <> 'settled')
       OR EXISTS (SELECT 1 FROM public.threads WHERE parent_thread_id = owner_id)
       OR EXISTS (SELECT 1 FROM public.thread_control_requests
                   WHERE thread_id = owner_id AND runtime_generation = generation_id)
       OR EXISTS (SELECT 1 FROM public.thread_permission_requests
                   WHERE thread_id = owner_id AND status = 'pending')
       OR EXISTS (SELECT 1 FROM public.thread_interrupt_requests
                   WHERE thread_id = owner_id AND
                     (outcome IS NULL OR (outcome = 'applied' AND
                      NOT (COALESCE(result, '{}'::jsonb) ? 'consumed_input_seq'))))
       OR EXISTS (SELECT 1 FROM public.run_queue
                   WHERE unit_id = owner_id AND (state <> 'done' OR leased_by IS NOT NULL))
       OR EXISTS (SELECT 1 FROM public.completion_effects
                   WHERE (scope_id = owner_id OR producer_id = owner_id)
                     AND (state <> 'done' OR claimed_by IS NOT NULL))
       OR EXISTS (SELECT 1 FROM public.thread_agent_workspace_claims
                   WHERE thread_id = owner_id AND status <> 'reclaimed')
       OR EXISTS (SELECT 1 FROM public.thread_workspace_provision_intents
                   WHERE thread_id = owner_id)
       OR EXISTS (SELECT 1 FROM public.managed_repository_workspace_creation_reservations
                   WHERE owner_kind = 'thread' AND
                         managed_repository_workspace_creation_reservations.owner_id =
                         acknowledge_settled_virtual_actor_exit.owner_id)
       OR EXISTS (SELECT 1 FROM public.cloud_ro_mounts
                   WHERE thread_id = owner_id AND status IN ('engaging','active','revoking'))
    THEN
        RETURN NULL;
    END IF;

    -- owner_runtime_generation is a process UUID, not the thread generation.
    -- The current published Pod/actor join above owns these completed inputs
    -- across container process restarts inside that same Kubernetes UID.
    SELECT count(*), md5(string_agg(delivery_id::text, ',' ORDER BY delivery_id))
      INTO input_count, input_digest
      FROM public.thread_input_deliveries
     WHERE thread_id = owner_id AND owner_agent_id = actor_id
       AND owner_pod_uid = stopped_pod_uid AND state = 'settled'
       AND execution_lane = 'pinned';
    IF input_count = 0 THEN
        RETURN NULL;
    END IF;
    receipt := jsonb_build_object(
        'version', 1,
        'runtime_generation', generation_id,
        'retirement_token', retirement_id,
        'agent_id', actor_id,
        'runtime_attach_token', attach_id,
        'settle_status', 'ended',
        'quiescence_protocol', 'agent_runtime_zero_v1',
        'quiescence_actor', 'orchestrator',
        'workspace_generation', NULL,
        'workspace_runtime_incarnation', NULL,
        'recovery_protocol', 'settled_virtual_actor_exit_v1',
        'agent_pod_uid', stopped_pod_uid,
        'settled_input_count', input_count,
        'settled_input_ids_digest', input_digest
    );
    IF owner_row.runtime_retirement_local_quiescence IS NOT NULL THEN
        RETURN CASE WHEN owner_row.runtime_retirement_local_quiescence = receipt
                    THEN receipt ELSE NULL END;
    END IF;
    UPDATE public.threads SET runtime_retirement_local_quiescence = receipt
     WHERE id = owner_id;
    RETURN receipt;
END;
$$;
