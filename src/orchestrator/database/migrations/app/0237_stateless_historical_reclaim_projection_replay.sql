-- migration:     0237_stateless_historical_reclaim_projection_replay.sql
-- description:   Replay the current settled permanent workspace projection
--                after completed cleanup receipts for prior runtime UIDs.
-- depends-on:    0236_workspace_cleanup_capture_location.sql
-- expected:      < 5s. Function definitions only; no row or receipt backfill.
-- locks:         Existing owner -> queue -> cleanup intent replay authority.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone = 'UTC';

CREATE OR REPLACE FUNCTION public.stateless_terminal_reclaim_projection_is_authorized(
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
       AND runtime_incarnation::TEXT = requested_runtime
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
    -- Historical receipts append proof for older exact UIDs without replacing
    -- the current terminal workspace projection. A retry may cross those rows
    -- only after their complete terminal proof has settled under this same
    -- permanent owner/queue generation. Pending, preserved, stale and unrelated
    -- newer intents still fence replay.
    IF EXISTS (
        SELECT 1
          FROM public.managed_repository_workspace_cleanup_intents AS newer
         WHERE newer.owner_kind = 'thread' AND newer.owner_id = requested_owner
           AND newer.scope = 'workspace_container'
           AND newer.intent_generation > intent.intent_generation
           AND (
               newer.intent_source = 'historical'
               AND newer.runtime_incarnation::TEXT <> requested_runtime
               AND newer.thread_runtime_generation = owner_row.runtime_generation
               AND newer.terminal_queue_token = queue_row.lease_token
               AND newer.resource_policy = 'terminal_reclaim'
               AND newer.reclaim_shared_resources
               AND newer.target_disposition = 'deleted'
               AND newer.result_kind = 'settled'
               AND newer.phase = 'settled'
               AND newer.cleanup_completed_at IS NOT NULL
               AND newer.settled_at IS NOT NULL
               AND newer.projection_transaction_id IS NOT NULL
               AND newer.capture_complete
               AND newer.resources_captured_at IS NOT NULL
               AND newer.pod_uid = newer.runtime_incarnation
               AND EXISTS (
                   SELECT 1 FROM public.managed_repository_process_zero_receipts AS receipt
                    WHERE receipt.owner_kind = 'thread'
                      AND receipt.owner_id = requested_owner
                      AND receipt.scope = 'workspace_container'
                      AND receipt.provisioner = 'k8s'
                      AND receipt.runtime_incarnation = newer.runtime_incarnation::TEXT
               )
           ) IS NOT TRUE
    ) THEN
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

CREATE OR REPLACE FUNCTION public.restore_settled_thread_workspace_cleanup_projection(
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
                AND newer.runtime_incarnation::TEXT=requested_runtime
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

COMMIT;
