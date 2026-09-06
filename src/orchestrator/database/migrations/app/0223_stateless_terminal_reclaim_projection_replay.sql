-- migration:     0223_stateless_terminal_reclaim_projection_replay.sql
-- description:   Restore an exact settled permanent workspace UID after soft
--                End, including old already-settled null projections. Receipts
--                remain immutable; no trigger is disabled and no rows backfilled.
-- depends-on:    0222_main_cloud_instance_pairing.sql
-- expected:      < 5s. Function definitions only; recovery is per-owner on retry.
-- locks:         Function-definition locks. Runtime replay locks owner -> queue
--                -> cleanup intent and changes only the terminal projection.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone = 'UTC';

CREATE FUNCTION public.stateless_terminal_reclaim_projection_is_authorized(
    requested_owner UUID, requested_runtime TEXT,
    old_state JSONB, new_state JSONB
)
RETURNS BOOLEAN LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    owner_row RECORD;
    queue_row RECORD;
    intent RECORD;
    marker JSONB;
    workspace JSONB;
    expected_state JSONB;
BEGIN
    SELECT * INTO owner_row FROM public.threads
     WHERE id = requested_owner FOR UPDATE;
    IF NOT FOUND OR owner_row.execution_lane <> 'stateless'
       OR owner_row.status::TEXT <> 'ended'
       OR owner_row.metadata IS DISTINCT FROM old_state THEN
        RETURN FALSE;
    END IF;
    workspace := old_state -> 'workspace_container';
    IF (jsonb_typeof(workspace) = 'object'
        AND workspace ->> 'provisioner' = 'k8s'
        AND workspace ->> 'status' IN ('deleted', 'released', 'retiring_process_zero')
        AND requested_runtime ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
        AND (workspace ->> '_runtime_incarnation' IS NULL
             OR workspace ->> '_runtime_incarnation' = requested_runtime)) IS NOT TRUE THEN
        RETURN FALSE;
    END IF;
    expected_state := jsonb_set(old_state, '{workspace_container}',
        workspace || jsonb_build_object('status', 'deleted', 'pod_ip', NULL::TEXT,
            'pod_name', NULL::TEXT, '_runtime_incarnation', requested_runtime));
    IF new_state IS DISTINCT FROM expected_state THEN
        RETURN FALSE;
    END IF;
    IF old_state ? '_stateless_workspace_retirement_pending' THEN
        IF old_state -> '_stateless_workspace_retirement_pending'
               IS DISTINCT FROM 'true'::JSONB
           OR old_state ? '_stateless_workspace_retirement_settled' THEN
            RETURN FALSE;
        END IF;
        marker := old_state -> '_stateless_claim_retirement';
    ELSE
        IF old_state ? '_stateless_claim_retirement' THEN
            RETURN FALSE;
        END IF;
        marker := old_state -> '_stateless_workspace_retirement_settled';
        IF marker -> 'cleanup_complete' IS DISTINCT FROM 'true'::JSONB THEN
            RETURN FALSE;
        END IF;
    END IF;
    IF (jsonb_typeof(marker) = 'object'
        AND marker -> 'permanent' = 'true'::JSONB
        AND marker ->> 'runtime_incarnation' = requested_runtime
        AND jsonb_typeof(marker -> 'terminal_token') = 'number'
        AND marker ->> 'terminal_token' ~ '^[1-9][0-9]*$') IS NOT TRUE THEN
        RETURN FALSE;
    END IF;
    SELECT * INTO queue_row FROM public.run_queue
     WHERE unit_id = requested_owner FOR UPDATE;
    IF NOT FOUND OR queue_row.unit_kind <> 'session_turn'
       OR queue_row.state <> 'done' OR queue_row.leased_by IS NOT NULL
       OR marker -> 'terminal_token' IS DISTINCT FROM to_jsonb(queue_row.lease_token) THEN
        RETURN FALSE;
    END IF;
    SELECT * INTO intent
      FROM public.managed_repository_workspace_cleanup_intents
     WHERE owner_kind = 'thread' AND owner_id = requested_owner
       AND scope = 'workspace_container'
     ORDER BY intent_generation DESC LIMIT 1 FOR SHARE;
    IF NOT FOUND OR (
        intent.runtime_incarnation::TEXT = requested_runtime
        AND intent.thread_runtime_generation = owner_row.runtime_generation
        AND intent.terminal_queue_token = queue_row.lease_token
        AND intent.resource_policy = 'terminal_reclaim'
        AND intent.reclaim_shared_resources
        AND intent.target_disposition = 'deleted'
        AND intent.result_kind = 'settled'
        AND intent.cleanup_completed_at IS NOT NULL
        AND intent.settled_at IS NOT NULL
        AND intent.capture_complete AND intent.resources_captured_at IS NOT NULL
        AND intent.pod_uid = intent.runtime_incarnation) IS NOT TRUE THEN
        RETURN FALSE;
    END IF;
    IF EXISTS (SELECT 1 FROM public.managed_repository_workspace_creation_reservations
        WHERE owner_kind='thread' AND owner_id=requested_owner
          AND scope='workspace_container' AND settled_at IS NULL) THEN
        RETURN FALSE;
    END IF;
    RETURN EXISTS (SELECT 1 FROM public.managed_repository_process_zero_receipts
        WHERE owner_kind='thread' AND owner_id=requested_owner
          AND scope='workspace_container' AND provisioner='k8s'
          AND runtime_incarnation=requested_runtime);
END;
$$;

CREATE FUNCTION public.restore_settled_thread_workspace_cleanup_projection(
    requested_owner UUID, requested_runtime TEXT, requested_intent_generation BIGINT
)
RETURNS BOOLEAN LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    state JSONB;
    next_state JSONB;
BEGIN
    SELECT metadata INTO state FROM public.threads
     WHERE id=requested_owner FOR UPDATE;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    next_state := jsonb_set(state, '{workspace_container}',
        (state -> 'workspace_container') || jsonb_build_object(
            'status', 'deleted', 'pod_ip', NULL::TEXT, 'pod_name', NULL::TEXT,
            '_runtime_incarnation', requested_runtime));
    IF public.stateless_terminal_reclaim_projection_is_authorized(
        requested_owner, requested_runtime, state, next_state) IS NOT TRUE THEN
        RETURN FALSE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.managed_repository_workspace_cleanup_intents AS intent
        WHERE owner_kind='thread' AND owner_id=requested_owner
          AND scope='workspace_container' AND runtime_incarnation::TEXT=requested_runtime
          AND intent_generation=requested_intent_generation AND result_kind='settled'
          AND resource_policy='terminal_reclaim'
          AND NOT EXISTS (SELECT 1 FROM public.managed_repository_workspace_cleanup_intents AS newer
              WHERE newer.owner_kind='thread' AND newer.owner_id=requested_owner
                AND newer.scope='workspace_container'
                AND newer.intent_generation > intent.intent_generation)) THEN
        RETURN FALSE;
    END IF;
    IF state IS DISTINCT FROM next_state THEN
        UPDATE public.threads SET metadata=next_state, last_activity=now()
         WHERE id=requested_owner;
    END IF;
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION public.prevent_retired_workspace_runtime_rebinding()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    source_kind TEXT;
    source_id UUID;
    old_state JSONB;
    new_state JSONB;
    scope_name TEXT;
    state_key TEXT;
    old_runtime TEXT;
    new_runtime TEXT;
    old_status TEXT;
    new_status TEXT;
    new_reservation TEXT;
    new_claim_token TEXT;
    old_runtime_state JSONB;
    new_runtime_state JSONB;
    old_envelope JSONB;
    new_envelope JSONB;
    old_identity_envelope JSONB;
    new_identity_envelope JSONB;
    creation_authorized BOOLEAN;
    uidless_creation_authorized BOOLEAN;
    cleanup_projection_authorized BOOLEAN;
    restore_projection_authorized BOOLEAN;
    cancelled_creation_projection_authorized BOOLEAN;
    cancel_claim_projection_authorized BOOLEAN;
    adoption_reversal_authorized BOOLEAN;
    terminal_cancel_projection_authorized BOOLEAN;
    safe_retirement_projection BOOLEAN;
    managed_k8s_envelope BOOLEAN;
    uidless_k8s_candidate BOOLEAN;
    initial_uidless_precreate BOOLEAN;
    uidless_precreate_progress BOOLEAN;
    matching_pending BOOLEAN;
    owner_pending BOOLEAN;
    owner_unsettled_receipt BOOLEAN;
    has_receipt BOOLEAN;
    old_settled BOOLEAN;
    new_settled BOOLEAN;
BEGIN
    IF TG_TABLE_NAME = 'threads'
       AND to_jsonb(NEW) ->> 'execution_lane' = 'pinned' THEN
        RETURN NEW;
    END IF;
    source_kind := CASE WHEN TG_TABLE_NAME = 'jobs' THEN 'job' ELSE 'thread' END;
    source_id := NEW.id;
    old_state := CASE
        WHEN TG_OP = 'INSERT' THEN '{}'::JSONB
        WHEN TG_TABLE_NAME = 'jobs'
            THEN COALESCE(to_jsonb(OLD) -> 'context', '{}'::JSONB)
        ELSE COALESCE(to_jsonb(OLD) -> 'metadata', '{}'::JSONB)
    END;
    new_state := CASE
        WHEN TG_TABLE_NAME = 'jobs'
            THEN COALESCE(to_jsonb(NEW) -> 'context', '{}'::JSONB)
        ELSE COALESCE(to_jsonb(NEW) -> 'metadata', '{}'::JSONB)
    END;

    IF source_kind = 'thread'
       AND to_jsonb(NEW) ->> 'execution_lane' = 'stateless'
       AND jsonb_typeof(new_state #> ARRAY[
           'workspace_container', '_runtime_creation'
       ]) = 'object'
       AND new_state #>> ARRAY[
           'workspace_container', '_runtime_creation', 'generation'
       ] IS DISTINCT FROM to_jsonb(NEW) ->> 'runtime_generation' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'stateless_workspace_runtime_generation_mismatch',
            MESSAGE = 'Stateless workspace projection must match the thread runtime generation';
    END IF;

    FOREACH scope_name IN ARRAY ARRAY['workspace_container', 'ide'] LOOP
        IF scope_name = 'ide' AND source_kind <> 'job' THEN
            CONTINUE;
        END IF;
        state_key := CASE WHEN scope_name = 'ide' THEN 'ide_session'
                          ELSE 'workspace_container' END;
        old_runtime := old_state #>> ARRAY[state_key, '_runtime_incarnation'];
        new_runtime := new_state #>> ARRAY[state_key, '_runtime_incarnation'];
        old_status := old_state #>> ARRAY[state_key, 'status'];
        -- Restoring the UID of a settled permanent cleanup is a terminal
        -- projection, never a new runtime binding. All other fields and the
        -- current owner/queue/receipt tuple must agree before this exception.
        IF source_kind = 'thread' AND TG_OP = 'UPDATE'
           AND scope_name = 'workspace_container'
           AND to_jsonb(NEW) ->> 'status' = to_jsonb(OLD) ->> 'status'
           AND to_jsonb(NEW) ->> 'status' = 'ended'
           AND to_jsonb(NEW) ->> 'execution_lane' = to_jsonb(OLD) ->> 'execution_lane'
           AND to_jsonb(NEW) ->> 'runtime_generation' = to_jsonb(OLD) ->> 'runtime_generation'
           AND public.stateless_terminal_reclaim_projection_is_authorized(
               source_id, new_runtime, old_state, new_state
           ) THEN
            CONTINUE;
        END IF;
        new_status := new_state #>> ARRAY[state_key, 'status'];
        new_reservation := new_state #>> ARRAY[
            state_key, '_creation_reservation_id'
        ];
        new_claim_token := new_state #>> ARRAY[
            state_key, '_creation_claim_token'
        ];
        old_runtime_state := old_state -> state_key;
        new_runtime_state := new_state -> state_key;
        managed_k8s_envelope := old_runtime IS NOT NULL
            OR new_runtime IS NOT NULL
            OR old_state #>> ARRAY[state_key, '_creation_reservation_id']
                IS NOT NULL
            OR new_reservation IS NOT NULL
            OR (
                scope_name = 'workspace_container'
                AND (
                    old_state #>> ARRAY[state_key, 'provisioner'] = 'k8s'
                    OR new_state #>> ARRAY[state_key, 'provisioner'] = 'k8s'
                )
            )
            OR (
                scope_name = 'ide'
                AND (
                    old_state #>> ARRAY[state_key, 'restore_type'] =
                        'k8s_container'
                    OR new_state #>> ARRAY[state_key, 'restore_type'] =
                        'k8s_container'
                )
            );
        uidless_k8s_candidate := old_runtime IS NULL
            AND jsonb_typeof(old_runtime_state) = 'object'
            AND (
                (
                    scope_name = 'workspace_container'
                    AND (
                        old_runtime_state ->> 'provisioner' = 'k8s'
                        OR (
                            NOT (old_runtime_state ? 'provisioner')
                            AND NOT (old_runtime_state ? 'container_id')
                        )
                    )
                )
                OR (
                    scope_name = 'ide'
                    AND (
                        old_runtime_state ->> 'restore_type' = 'k8s_container'
                        OR (
                            NOT (old_runtime_state ? 'restore_type')
                            AND NOT (old_runtime_state ? 'container_id')
                        )
                    )
                )
            );
        old_envelope := public.managed_repository_workspace_authority_envelope(
            old_state, scope_name
        );
        new_envelope := public.managed_repository_workspace_authority_envelope(
            new_state, scope_name
        );
        old_identity_envelope := old_envelope - ARRAY[
            'status', '_runtime_incarnation', '_creation_reservation_id',
            '_creation_claim_token', '_snapshot_restore_required'
        ];
        new_identity_envelope := new_envelope - ARRAY[
            'status', '_runtime_incarnation', '_creation_reservation_id',
            '_creation_claim_token', '_snapshot_restore_required'
        ];
        creation_authorized := new_runtime IS NOT NULL AND
            public.managed_repository_workspace_creation_is_authorized(
                source_kind, source_id, scope_name, new_runtime,
                new_reservation, new_claim_token
            );
        uidless_creation_authorized := new_runtime IS NULL AND
            public.managed_repository_workspace_uidless_creation_is_authorized(
                source_kind, source_id, scope_name,
                new_reservation, new_claim_token
            );
        cleanup_projection_authorized := old_runtime IS NOT NULL AND
            public.managed_repo_workspace_cleanup_projection_authorized_now(
                source_kind, source_id, scope_name, old_runtime,
                old_state, new_state
            );
        restore_projection_authorized := new_runtime IS NOT NULL AND
            public.managed_repo_workspace_restore_projection_authorized_now(
                source_kind, source_id, scope_name, new_runtime,
                new_reservation, new_claim_token, old_state, new_state
            );
        cancelled_creation_projection_authorized :=
            public.managed_repo_cancelled_creation_projection_authorized_now(
                source_kind, source_id, scope_name, old_state, new_state
            );
        cancel_claim_projection_authorized :=
            public.managed_repo_cancel_claim_projection_authorized_now(
                source_kind, source_id, scope_name, new_runtime,
                new_reservation, new_claim_token, old_state, new_state
            );
        terminal_cancel_projection_authorized :=
            public.managed_repo_terminal_cancel_projection_authorized_now(
                source_kind, source_id, scope_name, new_runtime,
                new_reservation, new_claim_token, old_state, new_state
            );
        adoption_reversal_authorized := old_runtime IS NOT NULL
            AND new_runtime IS NULL
            AND public.managed_repo_adoption_reversal_authorized_now(
                source_kind, source_id, scope_name, old_runtime,
                old_state, new_state
            );
        safe_retirement_projection := old_runtime IS NOT NULL
            AND new_runtime = old_runtime
            AND new_status = 'retiring_process_zero'
            AND (old_envelope - 'status') = (new_envelope - 'status');
        initial_uidless_precreate := new_runtime IS NULL
            AND new_status IN ('pending', 'creating', 'restoring')
            AND (
                TG_OP = 'INSERT'
                OR old_runtime_state IS NULL
                OR old_runtime_state = '{}'::JSONB
            );
        uidless_precreate_progress := TG_OP = 'UPDATE'
            AND old_runtime IS NULL
            AND new_runtime IS NULL
            AND old_status IN ('pending', 'creating', 'restoring')
            AND new_status IN ('pending', 'creating', 'restoring')
            AND old_identity_envelope = new_identity_envelope;

        IF TG_OP = 'UPDATE'
           AND old_runtime IS NULL
           AND uidless_k8s_candidate
           AND jsonb_typeof(old_runtime_state) = 'object'
           AND old_runtime_state <> '{}'::JSONB
           AND (
               old_identity_envelope IS DISTINCT FROM new_identity_envelope
               OR (
                   old_status IN (
                       'failed', 'deleted', 'retiring_process_zero',
                       'expired', 'cleanup_pending', 'suspended'
                   )
                   AND new_status IN (
                       'pending', 'creating', 'created', 'restoring',
                       'ready', 'active', 'idle'
                   )
               )
           )
           AND NOT creation_authorized
           AND NOT uidless_creation_authorized
           AND NOT cleanup_projection_authorized
           AND NOT restore_projection_authorized
           AND NOT cancelled_creation_projection_authorized
           AND NOT cancel_claim_projection_authorized
           AND NOT terminal_cancel_projection_authorized THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = CASE WHEN scope_name = 'ide'
                    THEN 'managed_repository_uidless_ide_runtime_transition_forbidden'
                    ELSE 'managed_repository_uidless_workspace_runtime_transition_forbidden' END,
                MESSAGE = 'A non-empty UID-less Kubernetes runtime cannot be recycled without exact authority';
        END IF;

        SELECT
            EXISTS (
                SELECT 1
                  FROM public.managed_repository_workspace_cleanup_intents AS intent
                 WHERE intent.owner_kind = source_kind
                   AND intent.owner_id = source_id
                   AND intent.scope = scope_name
                   AND intent.settled_at IS NULL
            ),
            EXISTS (
                SELECT 1
                  FROM public.managed_repository_workspace_cleanup_intents AS intent
                 WHERE intent.owner_kind = source_kind
                   AND intent.owner_id = source_id
                   AND intent.scope = scope_name
                   AND intent.runtime_incarnation::TEXT = old_runtime
                   AND intent.settled_at IS NULL
            ),
            EXISTS (
                SELECT 1
                  FROM public.managed_repository_process_zero_receipts AS receipt
                 WHERE receipt.owner_kind = source_kind
                   AND receipt.owner_id = source_id
                   AND receipt.provisioner = 'k8s'
                   AND receipt.scope IN (
                       scope_name,
                       CASE WHEN scope_name = 'workspace_container'
                            AND source_kind = 'thread'
                            THEN 'stateless_workspace'
                            ELSE scope_name END
                   )
                   AND NOT EXISTS (
                       SELECT 1
                         FROM public.managed_repository_workspace_cleanup_intents AS intent
                        WHERE intent.owner_kind = source_kind
                          AND intent.owner_id = source_id
                          AND intent.scope = scope_name
                          AND intent.runtime_incarnation::TEXT =
                              receipt.runtime_incarnation
                          AND intent.result_kind IN ('settled', 'superseded')
                   )
            )
          INTO owner_pending, matching_pending, owner_unsettled_receipt;

        IF old_runtime IS NULL AND (owner_pending OR owner_unsettled_receipt)
           AND (
               new_runtime IS DISTINCT FROM old_runtime
               OR new_status IS DISTINCT FROM old_status
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = CASE WHEN scope_name = 'ide'
                    THEN 'managed_repository_ide_cleanup_in_progress'
                    ELSE 'managed_repository_workspace_cleanup_in_progress' END,
                MESSAGE = 'A Kubernetes runtime may not change before exact cleanup settlement';
        END IF;

        IF old_runtime IS NOT NULL THEN
            has_receipt := public.managed_repository_workspace_has_process_zero_receipt(
                source_kind, source_id, scope_name, old_runtime
            );
            old_settled := public.managed_repository_workspace_cleanup_projection_is_settled(
                source_kind, source_id, scope_name, old_runtime,
                old_runtime, old_status
            );
            new_settled := public.managed_repository_workspace_cleanup_projection_is_settled(
                source_kind, source_id, scope_name, old_runtime,
                new_runtime, new_status
            );

            IF (matching_pending OR (has_receipt AND NOT old_settled))
               AND (
                   new_runtime IS DISTINCT FROM old_runtime
                   OR new_status IS DISTINCT FROM old_status
               )
               AND NOT (
                   matching_pending
                   AND new_runtime = old_runtime
                   AND new_status = 'retiring_process_zero'
               )
               AND NOT new_settled THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = CASE WHEN scope_name = 'ide'
                        THEN 'managed_repository_ide_cleanup_in_progress'
                        ELSE 'managed_repository_workspace_cleanup_in_progress' END,
                    MESSAGE = 'A Kubernetes runtime may not change before exact cleanup settlement';
            END IF;

            IF has_receipt AND old_settled
               AND new_runtime = old_runtime
               AND new_status IS DISTINCT FROM old_status THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = CASE WHEN scope_name = 'ide'
                        THEN 'managed_repository_retired_ide_runtime_reactivation'
                        ELSE 'managed_repository_retired_workspace_runtime_reactivation' END,
                    MESSAGE = 'A settled retired runtime may not be reactivated';
            END IF;
        END IF;

        IF new_runtime IS DISTINCT FROM old_runtime
           AND new_runtime IS NOT NULL THEN
            IF public.managed_repository_workspace_has_process_zero_receipt(
                source_kind, source_id, scope_name, new_runtime
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = CASE WHEN scope_name = 'ide'
                        THEN 'managed_repository_retired_ide_runtime_rebind'
                        ELSE 'managed_repository_retired_workspace_runtime_rebind' END,
                    MESSAGE = 'A retired Kubernetes runtime may not be rebound';
            END IF;
            IF NOT creation_authorized THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = CASE WHEN scope_name = 'ide'
                        THEN 'managed_repository_ide_creation_reservation_required'
                        ELSE 'managed_repository_workspace_creation_reservation_required' END,
                    MESSAGE = 'A new Kubernetes runtime requires exact creation reservation authority';
            END IF;
        END IF;

        IF managed_k8s_envelope
           AND old_envelope IS DISTINCT FROM new_envelope
           AND NOT creation_authorized
           AND NOT uidless_creation_authorized
           AND NOT cleanup_projection_authorized
           AND NOT restore_projection_authorized
           AND NOT cancelled_creation_projection_authorized
           AND NOT cancel_claim_projection_authorized
           AND NOT terminal_cancel_projection_authorized
           AND NOT safe_retirement_projection
           AND NOT adoption_reversal_authorized
           AND NOT initial_uidless_precreate
           AND NOT uidless_precreate_progress THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = CASE WHEN scope_name = 'ide'
                    THEN 'managed_repository_ide_authority_envelope_immutable'
                    ELSE 'managed_repository_workspace_authority_envelope_immutable' END,
                MESSAGE = 'Kubernetes runtime authority fields require exact durable authority';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

COMMIT;
