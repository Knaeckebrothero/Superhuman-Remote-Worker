-- migration:     0210_thread_terminal_reclaim_projection.sql
-- description:   Keep a permanently reclaimed stateless thread's immutable
--                Kubernetes runtime UID in its terminal projection until the
--                owner row is deleted. Soft End remains resumable and clears
--                the UID. Repair the short-lived 0198 projection shape only
--                when an exact settled terminal-reclaim intent, process-zero
--                receipt, permanent retirement marker, and queue token all
--                agree on the same runtime.
-- depends-on:    0209_expert_persona_identity_backfill.sql
-- expected:      < 5s at current scale. One exact-authority ledger scan and
--                primary-key join into threads; normally updates zero rows.
-- locks:         Function-definition locks plus a brief ACCESS EXCLUSIVE lock
--                on threads while one named user trigger is disabled for the
--                guarded compatibility repair. Matched rows take row locks.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE OR REPLACE FUNCTION public.managed_repository_workspace_cleanup_projection_is_settled(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    requested_scope TEXT,
    requested_runtime TEXT,
    projected_runtime TEXT,
    projected_status TEXT
)
RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
    SELECT EXISTS (
        SELECT 1
          FROM public.managed_repository_workspace_cleanup_intents AS intent
         WHERE intent.owner_kind = requested_owner_kind
           AND intent.owner_id = requested_owner_id
           AND intent.scope = requested_scope
           AND intent.runtime_incarnation::TEXT = requested_runtime
           AND intent.result_kind = 'settled'
           AND intent.cleanup_completed_at IS NOT NULL
           AND intent.target_disposition = projected_status
           AND (
               (
                   requested_scope = 'workspace_container'
                   AND requested_owner_kind = 'thread'
                   AND projected_status = 'deleted'
                   AND (
                       (
                           intent.resource_policy = 'terminal_reclaim'
                           AND projected_runtime = requested_runtime
                       )
                       OR (
                           intent.resource_policy = 'preserve'
                           AND projected_runtime IS NULL
                       )
                   )
               )
               OR (
                   NOT (
                       requested_scope = 'workspace_container'
                       AND requested_owner_kind = 'thread'
                       AND projected_status = 'deleted'
                   )
                   AND projected_runtime = requested_runtime
               )
           )
    );
$$;

CREATE OR REPLACE FUNCTION public.managed_repo_workspace_cleanup_projection_authorized_now(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    requested_scope TEXT,
    requested_runtime TEXT,
    old_state JSONB,
    new_state JSONB
)
RETURNS BOOLEAN LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    intent RECORD;
    state_key TEXT;
    old_runtime_state JSONB;
    new_runtime_state JSONB;
    expected_runtime_state JSONB;
BEGIN
    SELECT * INTO intent
      FROM public.managed_repository_workspace_cleanup_intents
     WHERE owner_kind = requested_owner_kind
       AND owner_id = requested_owner_id
       AND scope = requested_scope
       AND runtime_incarnation::TEXT = requested_runtime
       AND result_kind = 'settled'
       AND cleanup_completed_at IS NOT NULL
       AND settled_at IS NOT NULL
       AND projection_transaction_id = txid_current()
     ORDER BY intent_generation DESC
     LIMIT 1;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    state_key := CASE WHEN requested_scope = 'ide'
        THEN 'ide_session' ELSE 'workspace_container' END;
    old_runtime_state := old_state -> state_key;
    new_runtime_state := new_state -> state_key;
    IF jsonb_typeof(old_runtime_state) <> 'object'
       OR jsonb_typeof(new_runtime_state) <> 'object'
       OR (old_state - state_key) IS DISTINCT FROM (new_state - state_key) THEN
        RETURN FALSE;
    END IF;

    expected_runtime_state := old_runtime_state || jsonb_build_object(
        'status', intent.target_disposition,
        'pod_ip', NULL::TEXT
    );
    IF requested_scope = 'workspace_container' THEN
        expected_runtime_state := expected_runtime_state || jsonb_build_object(
            'pod_name', NULL::TEXT
        );
        IF intent.target_disposition = 'suspended' THEN
            expected_runtime_state := expected_runtime_state || jsonb_build_object(
                '_snapshot_restore_required', intent.snapshot_restore_required
            );
            IF intent.suspended_at IS NOT NULL THEN
                IF jsonb_typeof(new_runtime_state -> 'suspended_at') <> 'string' THEN
                    RETURN FALSE;
                END IF;
                expected_runtime_state := expected_runtime_state || jsonb_build_object(
                    'suspended_at', new_runtime_state -> 'suspended_at'
                );
            END IF;
        END IF;
        IF requested_owner_kind = 'thread'
           AND intent.target_disposition = 'deleted'
           AND intent.resource_policy = 'preserve' THEN
            expected_runtime_state := expected_runtime_state || jsonb_build_object(
                '_runtime_incarnation', NULL::TEXT
            );
        END IF;
        RETURN new_runtime_state = expected_runtime_state;
    END IF;

    IF jsonb_typeof(new_runtime_state -> 'stopped_at') <> 'string' THEN
        RETURN FALSE;
    END IF;
    expected_runtime_state := expected_runtime_state || jsonb_build_object(
        'code_server_url', NULL::TEXT,
        'stopped_at', new_runtime_state -> 'stopped_at'
    );
    RETURN new_runtime_state = expected_runtime_state;
END;
$$;

-- The previous projection contract deliberately wrote JSON null here. The
-- runtime-authority trigger must remain enabled for every ordinary writer, but
-- it also correctly rejects rebinding a process-zero UID. Disable only that
-- named user trigger while this transaction repairs rows whose independent
-- durable authorities all agree. ACCESS EXCLUSIVE serializes old writers;
-- once it is released, the replacement functions above reject the old shape.
ALTER TABLE public.threads
    DISABLE TRIGGER trg_threads_d_validate_workspace_authority_envelope;

WITH exact_terminal_reclaims AS (
    SELECT DISTINCT ON (thread.id)
           thread.id,
           intent.runtime_incarnation
      FROM public.managed_repository_workspace_cleanup_intents AS intent
      JOIN public.threads AS thread
        ON thread.id = intent.owner_id
       AND intent.owner_kind = 'thread'
     WHERE intent.scope = 'workspace_container'
       AND intent.resource_policy = 'terminal_reclaim'
       AND intent.target_disposition = 'deleted'
       AND intent.result_kind = 'settled'
       AND intent.cleanup_completed_at IS NOT NULL
       AND intent.settled_at IS NOT NULL
       AND intent.terminal_queue_token IS NOT NULL
       AND thread.execution_lane = 'stateless'
       AND thread.status = 'ended'
       AND jsonb_typeof(thread.metadata -> 'workspace_container') = 'object'
       AND thread.metadata #>> '{workspace_container,provisioner}' = 'k8s'
       AND thread.metadata #>> '{workspace_container,status}' IN (
           'deleted', 'released'
       )
       AND thread.metadata #>>
           '{workspace_container,_runtime_incarnation}' IS NULL
       AND thread.metadata #> '{_stateless_workspace_retirement_pending}'
           = 'true'::JSONB
       AND jsonb_typeof(
           thread.metadata -> '_stateless_claim_retirement'
       ) = 'object'
       AND thread.metadata #> '{_stateless_claim_retirement,permanent}'
           = 'true'::JSONB
       AND thread.metadata #>>
           '{_stateless_claim_retirement,runtime_incarnation}'
           = intent.runtime_incarnation::TEXT
       AND thread.metadata #>>
           '{_stateless_claim_retirement,terminal_token}'
           = intent.terminal_queue_token::TEXT
       AND EXISTS (
           SELECT 1
             FROM public.managed_repository_process_zero_receipts AS receipt
            WHERE receipt.owner_kind = 'thread'
              AND receipt.owner_id = thread.id
              AND receipt.scope = 'workspace_container'
              AND receipt.provisioner = 'k8s'
              AND receipt.runtime_incarnation = intent.runtime_incarnation::TEXT
       )
       AND EXISTS (
           SELECT 1
             FROM public.run_queue AS queue
            WHERE queue.unit_id = thread.id
              AND queue.unit_kind = 'session_turn'
              AND queue.state = 'done'
              AND queue.lease_token = intent.terminal_queue_token
              AND queue.leased_by IS NULL
       )
     ORDER BY thread.id, intent.intent_generation DESC
)
UPDATE public.threads AS thread
   SET metadata = jsonb_set(
       thread.metadata,
       '{workspace_container,_runtime_incarnation}',
       to_jsonb(exact.runtime_incarnation::TEXT),
       true
   )
  FROM exact_terminal_reclaims AS exact
 WHERE thread.id = exact.id;

-- The guarded UPDATE can queue deferrable FK/check trigger events. PostgreSQL
-- will not ALTER a table while those events are pending, so validate them now
-- while the transaction still owns the repaired rows and all other triggers.
SET CONSTRAINTS ALL IMMEDIATE;

ALTER TABLE public.threads
    ENABLE TRIGGER trg_threads_d_validate_workspace_authority_envelope;

COMMIT;
