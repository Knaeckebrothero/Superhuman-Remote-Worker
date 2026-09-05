-- migration:     0198_non_pinned_workspace_lifecycle_authority.sql
-- description:   Persist exact non-pinned workspace creation, restore, and
--                cleanup generations with crash-safe external-effect receipts.
-- depends-on:    0197_non_pinned_workspace_process_zero.sql
-- expected:      < 5s. Empty ledgers, functions, and row triggers only; no
--                historical scan or owner-row rewrite.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on jobs and threads for
--                trigger installation.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE SEQUENCE public.managed_repository_workspace_cleanup_generation_seq;
CREATE SEQUENCE public.managed_repository_workspace_cleanup_claim_seq;
CREATE SEQUENCE public.managed_repository_workspace_creation_generation_seq;
CREATE SEQUENCE public.managed_repository_workspace_creation_claim_seq;
CREATE SEQUENCE public.managed_repository_workspace_restore_work_claim_seq;
CREATE TABLE public.managed_repository_workspace_cleanup_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_kind TEXT NOT NULL,
    owner_id UUID NOT NULL,
    thread_runtime_generation UUID,
    scope TEXT NOT NULL,
    runtime_incarnation UUID NOT NULL,
    intent_generation BIGINT NOT NULL DEFAULT nextval(
        'public.managed_repository_workspace_cleanup_generation_seq'
    ),
    intent_source TEXT NOT NULL DEFAULT 'current',
    admission_source TEXT NOT NULL DEFAULT 'explicit',
    target_disposition TEXT NOT NULL,
    resource_policy TEXT NOT NULL DEFAULT 'preserve',
    reclaim_shared_resources BOOLEAN NOT NULL DEFAULT FALSE,
    lifecycle_fingerprint JSONB NOT NULL DEFAULT '{}'::JSONB,
    terminal_queue_token BIGINT,
    pod_uid UUID NOT NULL,
    seed_configmap_uid UUID,
    pvc_uid UUID,
    service_uid UUID,
    capture_complete BOOLEAN NOT NULL DEFAULT FALSE,
    resources_captured_at TIMESTAMPTZ,
    suspended_at TIMESTAMPTZ,
    snapshot_restore_required BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    eligible_after TIMESTAMPTZ NOT NULL DEFAULT now(),
    phase TEXT NOT NULL DEFAULT 'prepared',
    claim_token BIGINT NOT NULL DEFAULT 0,
    claimed_by TEXT,
    claim_expires_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleanup_completed_at TIMESTAMPTZ,
    terminal_admission_transaction_id BIGINT,
    projection_transaction_id BIGINT,
    settled_at TIMESTAMPTZ,
    result_kind TEXT,
    CONSTRAINT managed_repository_workspace_cleanup_identity_unique
        UNIQUE (
            owner_kind, owner_id, scope, runtime_incarnation,
            target_disposition, resource_policy
        ),
    CONSTRAINT managed_repository_workspace_cleanup_generation_unique
        UNIQUE (intent_generation),
    CONSTRAINT managed_repository_workspace_cleanup_owner_kind_check
        CHECK (owner_kind IN ('job', 'thread')),
    CONSTRAINT managed_repository_workspace_cleanup_thread_generation_shape
        CHECK (
            (owner_kind = 'job' AND thread_runtime_generation IS NULL)
            OR (owner_kind = 'thread' AND thread_runtime_generation IS NOT NULL)
        ),
    CONSTRAINT managed_repository_workspace_cleanup_scope_check
        CHECK (scope IN ('workspace_container', 'ide')),
    CONSTRAINT managed_repository_workspace_cleanup_source_check
        CHECK (intent_source IN ('current', 'historical', 'orphan')),
    CONSTRAINT managed_repository_workspace_cleanup_admission_source_check
        CHECK (admission_source IN ('automatic', 'explicit')),
    CONSTRAINT managed_repository_workspace_cleanup_policy_check
        CHECK (
            resource_policy IN ('preserve', 'terminal_reclaim')
            AND (
                (scope = 'workspace_container' AND reclaim_shared_resources =
                    (resource_policy = 'terminal_reclaim'))
                OR (scope = 'ide' AND reclaim_shared_resources IS FALSE)
            )
        ),
    CONSTRAINT managed_repository_workspace_cleanup_phase_check
        CHECK (phase IN (
            'prepared', 'captured', 'process_zero', 'cleaned',
            'settled', 'superseded', 'ambiguous'
        )),
    CONSTRAINT managed_repository_workspace_cleanup_target_check
        CHECK (
            (
                target_disposition = 'ambiguous'
                AND resource_policy = 'preserve'
                AND phase = 'ambiguous'
                AND capture_complete IS FALSE
                AND seed_configmap_uid IS NULL
                AND pvc_uid IS NULL
                AND service_uid IS NULL
                AND suspended_at IS NULL
                AND snapshot_restore_required IS FALSE
            )
            OR (
                scope = 'workspace_container'
                AND target_disposition IN ('deleted', 'suspended')
                AND (
                    target_disposition <> 'suspended'
                    OR resource_policy = 'preserve'
                )
                AND (
                    resource_policy <> 'terminal_reclaim'
                    OR target_disposition = 'deleted'
                )
            )
            OR (
                scope = 'ide'
                AND owner_kind = 'job'
                AND target_disposition IN ('expired', 'deleted')
                AND (
                    resource_policy = 'preserve'
                    OR (resource_policy = 'terminal_reclaim'
                        AND target_disposition = 'deleted')
                )
                AND suspended_at IS NULL
                AND snapshot_restore_required IS FALSE
                AND pvc_uid IS NULL
                AND service_uid IS NULL
            )
        ),
    CONSTRAINT managed_repository_workspace_cleanup_resource_shape_check
        CHECK (
            pod_uid = runtime_incarnation
            AND (target_disposition = 'suspended' OR suspended_at IS NULL)
            AND capture_complete = (resources_captured_at IS NOT NULL)
            AND (
                capture_complete
                OR (
                    seed_configmap_uid IS NULL
                    AND pvc_uid IS NULL
                    AND service_uid IS NULL
                )
            )
        ),
    CONSTRAINT managed_repository_workspace_cleanup_claim_shape_check
        CHECK (
            attempts >= 0 AND claim_token >= 0
            AND (
                (claimed_by IS NULL AND claim_expires_at IS NULL)
                OR (
                    claimed_by IS NOT NULL
                    AND claim_expires_at IS NOT NULL
                    AND claim_token > 0
                )
            )
        ),
    CONSTRAINT managed_repository_workspace_cleanup_result_check
        CHECK (result_kind IS NULL OR result_kind IN ('settled', 'superseded')),
    CONSTRAINT managed_repository_workspace_cleanup_terminal_admission_check
        CHECK (
            terminal_admission_transaction_id IS NULL
            OR (
                intent_source = 'current'
                AND admission_source = 'explicit'
                AND target_disposition = 'deleted'
                AND lifecycle_fingerprint ->> 'admitted_by' =
                    'terminal_owner_transition'
            )
        ),
    CONSTRAINT managed_repository_workspace_cleanup_settlement_shape_check
        CHECK (
            (
                result_kind IS NULL
                AND cleanup_completed_at IS NULL
                AND projection_transaction_id IS NULL
                AND settled_at IS NULL
            )
            OR (
                result_kind = 'settled'
                AND cleanup_completed_at IS NOT NULL
                AND projection_transaction_id IS NOT NULL
                AND settled_at IS NOT NULL
                AND capture_complete
                AND phase = 'settled'
            )
            OR (
                result_kind = 'superseded'
                AND cleanup_completed_at IS NOT NULL
                AND projection_transaction_id IS NULL
                AND settled_at IS NOT NULL
                AND capture_complete
                AND resource_policy = 'preserve'
                AND phase = 'superseded'
            )
        )
);

CREATE UNIQUE INDEX managed_repository_workspace_cleanup_one_active
    ON public.managed_repository_workspace_cleanup_intents (
        owner_kind, owner_id, scope
    ) WHERE settled_at IS NULL;

CREATE INDEX managed_repository_workspace_cleanup_pending_page
    ON public.managed_repository_workspace_cleanup_intents (
        next_attempt_at, intent_generation, owner_kind, owner_id
    ) WHERE settled_at IS NULL AND phase <> 'ambiguous';

COMMENT ON TABLE public.managed_repository_workspace_cleanup_intents IS
    'Restart-safe exact Kubernetes cleanup authority persisted before external deletion and settled atomically with the exact owner projection.';

CREATE TABLE public.managed_repository_workspace_creation_reservations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_kind TEXT NOT NULL,
    owner_id UUID NOT NULL,
    thread_runtime_generation UUID,
    scope TEXT NOT NULL,
    reservation_generation BIGINT NOT NULL DEFAULT nextval(
        'public.managed_repository_workspace_creation_generation_seq'
    ),
    claim_token BIGINT NOT NULL DEFAULT nextval(
        'public.managed_repository_workspace_creation_claim_seq'
    ),
    claimed_by TEXT NOT NULL,
    operation_kind TEXT NOT NULL DEFAULT 'create',
    lifecycle_fingerprint JSONB NOT NULL DEFAULT '{}'::JSONB,
    desired_manifest_digest TEXT NOT NULL,
    external_effects JSONB NOT NULL DEFAULT '{}'::JSONB,
    runtime_incarnation UUID,
    pod_uid UUID,
    seed_configmap_uid UUID,
    pvc_uid UUID,
    service_uid UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    phase TEXT NOT NULL DEFAULT 'reserved',
    external_mutation_started_at TIMESTAMPTZ,
    cancel_requested_at TIMESTAMPTZ,
    cancel_claim_projection_transaction_id BIGINT,
    cancel_target_disposition TEXT,
    cancel_resource_policy TEXT,
    cancel_suspended_at TIMESTAMPTZ,
    cancel_snapshot_restore_required BOOLEAN,
    attempts INTEGER NOT NULL DEFAULT 1,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancel_cleanup_completed_at TIMESTAMPTZ,
    cancel_projection_transaction_id BIGINT,
    settled_at TIMESTAMPTZ,
    result_kind TEXT,
    restore_work_claim_token BIGINT NOT NULL DEFAULT 0,
    restore_work_claimed_by TEXT,
    restore_work_claim_expires_at TIMESTAMPTZ,
    restore_work_next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    restore_work_completed_at TIMESTAMPTZ,
    restore_work_result_kind TEXT,
    restore_work_projection_transaction_id BIGINT,
    CONSTRAINT managed_repository_workspace_creation_generation_unique
        UNIQUE (reservation_generation),
    CONSTRAINT managed_repository_workspace_creation_claim_unique
        UNIQUE (claim_token),
    CONSTRAINT managed_repository_workspace_creation_owner_kind_check
        CHECK (owner_kind IN ('job', 'thread')),
    CONSTRAINT managed_repository_workspace_creation_thread_generation_shape
        CHECK (
            (owner_kind = 'job' AND thread_runtime_generation IS NULL)
            OR (owner_kind = 'thread' AND thread_runtime_generation IS NOT NULL)
        ),
    CONSTRAINT managed_repository_workspace_creation_scope_check
        CHECK (scope IN ('workspace_container', 'ide')),
    CONSTRAINT managed_repository_workspace_creation_operation_check
        CHECK (operation_kind IN ('create', 'restore', 'reattach', 'adopt')),
    CONSTRAINT managed_repository_workspace_creation_manifest_digest_check
        CHECK (desired_manifest_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT managed_repository_workspace_creation_effects_check
        CHECK (jsonb_typeof(external_effects) = 'object'),
    CONSTRAINT managed_repository_workspace_creation_phase_check
        CHECK (phase IN (
            'reserved', 'mutating', 'runtime_bound',
            'settled', 'aborted', 'ambiguous'
        )),
    CONSTRAINT managed_repository_workspace_creation_expiry_check
        CHECK (expires_at > created_at AND attempts > 0),
    CONSTRAINT managed_repository_workspace_creation_runtime_shape_check
        CHECK (
            (runtime_incarnation IS NULL AND pod_uid IS NULL)
            OR (runtime_incarnation IS NOT NULL AND pod_uid = runtime_incarnation)
        ),
    CONSTRAINT managed_repository_workspace_creation_cancel_shape_check
        CHECK (
            (
                cancel_requested_at IS NULL
                AND cancel_claim_projection_transaction_id IS NULL
                AND cancel_target_disposition IS NULL
                AND cancel_resource_policy IS NULL
                AND cancel_suspended_at IS NULL
                AND cancel_snapshot_restore_required IS NULL
            )
            OR (
                cancel_requested_at IS NOT NULL
                AND cancel_claim_projection_transaction_id IS NOT NULL
                AND cancel_target_disposition IS NOT NULL
                AND cancel_resource_policy IN ('preserve', 'terminal_reclaim')
                AND cancel_snapshot_restore_required IS NOT NULL
                AND (
                    cancel_target_disposition = 'suspended'
                    OR cancel_suspended_at IS NULL
                )
            )
        ),
    CONSTRAINT managed_repository_workspace_creation_result_check
        CHECK (
            (
                settled_at IS NULL
                AND result_kind IS NULL
                AND phase IN ('reserved', 'mutating', 'runtime_bound', 'ambiguous')
            )
            OR (
                settled_at IS NOT NULL
                AND result_kind IN ('settled', 'aborted')
                AND phase = result_kind
            )
        ),
    CONSTRAINT managed_repository_workspace_creation_cleanup_shape_check
        CHECK (
            (
                cancel_cleanup_completed_at IS NULL
                AND cancel_projection_transaction_id IS NULL
            )
            OR (
                cancel_cleanup_completed_at IS NOT NULL
                AND cancel_projection_transaction_id IS NOT NULL
                AND cancel_requested_at IS NOT NULL
                AND settled_at IS NOT NULL
                AND result_kind = 'aborted'
                AND phase = 'aborted'
            )
        ),
    CONSTRAINT managed_repository_workspace_restore_work_shape_check
        CHECK (
            restore_work_claim_token >= 0
            AND (
                (
                    restore_work_claimed_by IS NULL
                    AND restore_work_claim_expires_at IS NULL
                )
                OR (
                    restore_work_claimed_by IS NOT NULL
                    AND restore_work_claim_expires_at IS NOT NULL
                    AND restore_work_claim_token > 0
                )
            )
            AND (
                restore_work_result_kind IS NULL
                AND restore_work_projection_transaction_id IS NULL
                OR (
                    operation_kind = 'restore'
                    AND result_kind = 'settled'
                    AND restore_work_completed_at IS NOT NULL
                    AND restore_work_projection_transaction_id IS NOT NULL
                    AND (
                        (
                            scope = 'ide'
                            AND restore_work_result_kind IN ('active', 'failed')
                        )
                        OR (
                            scope = 'workspace_container'
                            AND restore_work_result_kind IN ('ready', 'failed')
                        )
                    )
                )
            )
            AND (
                restore_work_completed_at IS NULL
                OR restore_work_result_kind IS NOT NULL
            )
        )
);

CREATE UNIQUE INDEX managed_repository_workspace_creation_one_active
    ON public.managed_repository_workspace_creation_reservations (
        owner_kind, owner_id, scope
    ) WHERE settled_at IS NULL;

CREATE UNIQUE INDEX managed_repository_workspace_restore_work_claim_unique
    ON public.managed_repository_workspace_creation_reservations (
        restore_work_claim_token
    ) WHERE restore_work_claim_token > 0;

COMMENT ON TABLE public.managed_repository_workspace_creation_reservations IS
    'Exact owner/scope reservation committed before the first Kubernetes workspace creation side effect.';

CREATE FUNCTION public.validate_non_pinned_workspace_owner_generation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    owner_lane TEXT;
    owner_generation UUID;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.owner_kind IS DISTINCT FROM OLD.owner_kind
           OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
           OR NEW.thread_runtime_generation IS DISTINCT FROM
              OLD.thread_runtime_generation THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_workspace_owner_generation_immutable',
                MESSAGE = 'Workspace lifecycle owner generation is immutable';
        END IF;
    END IF;

    IF NEW.owner_kind = 'job' THEN
        IF NEW.thread_runtime_generation IS NOT NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_job_workspace_generation_forbidden',
                MESSAGE = 'Job workspace authority cannot carry a thread generation';
        END IF;
        RETURN NEW;
    END IF;

    SELECT execution_lane, runtime_generation
      INTO owner_lane, owner_generation
      FROM public.threads
     WHERE id = NEW.owner_id;
    IF owner_lane IS DISTINCT FROM 'stateless'
       OR owner_generation IS NULL
       OR NEW.thread_runtime_generation IS DISTINCT FROM owner_generation THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_stateless_workspace_generation_required',
            MESSAGE = 'Stateless workspace authority must match the current thread runtime generation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_workspace_cleanup_validate_owner_generation
BEFORE INSERT OR UPDATE ON public.managed_repository_workspace_cleanup_intents
FOR EACH ROW
EXECUTE FUNCTION public.validate_non_pinned_workspace_owner_generation();

CREATE TRIGGER trg_workspace_creation_validate_owner_generation
BEFORE INSERT OR UPDATE ON public.managed_repository_workspace_creation_reservations
FOR EACH ROW
EXECUTE FUNCTION public.validate_non_pinned_workspace_owner_generation();

CREATE FUNCTION public.prevent_stateless_runtime_generation_with_live_workspace()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.execution_lane = 'pinned'
       OR NEW.runtime_generation IS NOT DISTINCT FROM OLD.runtime_generation THEN
        RETURN NEW;
    END IF;
    IF OLD.execution_lane IS DISTINCT FROM 'stateless'
       OR NEW.execution_lane IS DISTINCT FROM 'stateless'
       OR EXISTS (
            SELECT 1
              FROM public.managed_repository_workspace_creation_reservations r
             WHERE r.owner_kind = 'thread' AND r.owner_id = OLD.id
               AND r.thread_runtime_generation = OLD.runtime_generation
               AND r.settled_at IS NULL
       ) OR EXISTS (
            SELECT 1
              FROM public.managed_repository_workspace_cleanup_intents i
             WHERE i.owner_kind = 'thread' AND i.owner_id = OLD.id
               AND i.thread_runtime_generation = OLD.runtime_generation
               AND i.settled_at IS NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'stateless_runtime_generation_workspace_authority_pending',
            MESSAGE = 'Stateless runtime generation cannot rotate while workspace authority is pending';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_threads_validate_workspace_runtime_generation
BEFORE UPDATE OF execution_lane, runtime_generation ON public.threads
FOR EACH ROW
EXECUTE FUNCTION public.prevent_stateless_runtime_generation_with_live_workspace();

CREATE FUNCTION public.stamp_managed_repository_cleanup_projection_transaction()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.terminal_admission_transaction_id IS NOT NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_cleanup_terminal_admission_transaction_server_owned',
                MESSAGE = 'Workspace terminal admission authority is server-owned';
        END IF;
        IF NEW.intent_source = 'current'
           AND NEW.admission_source = 'explicit'
           AND NEW.target_disposition = 'deleted'
           AND NEW.lifecycle_fingerprint ->> 'admitted_by' =
               'terminal_owner_transition' THEN
            NEW.terminal_admission_transaction_id := txid_current();
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.terminal_admission_transaction_id IS DISTINCT FROM
       OLD.terminal_admission_transaction_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_cleanup_terminal_admission_transaction_server_owned',
            MESSAGE = 'Workspace terminal admission authority is server-owned';
    END IF;
    IF OLD.terminal_admission_transaction_id IS NULL
       AND NEW.intent_source = 'current'
       AND NEW.admission_source = 'explicit'
       AND NEW.target_disposition = 'deleted'
       AND NEW.lifecycle_fingerprint ->> 'admitted_by' =
           'terminal_owner_transition' THEN
        NEW.terminal_admission_transaction_id := txid_current();
    END IF;
    IF NEW.projection_transaction_id IS DISTINCT FROM
       OLD.projection_transaction_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_cleanup_projection_transaction_server_owned',
            MESSAGE = 'Workspace cleanup projection authority is server-owned';
    END IF;
    IF OLD.result_kind IS DISTINCT FROM 'settled'
       AND NEW.result_kind = 'settled'
       AND NEW.cleanup_completed_at IS NOT NULL
       AND NEW.settled_at IS NOT NULL THEN
        NEW.projection_transaction_id := txid_current();
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_workspace_cleanup_stamp_projection_transaction
BEFORE INSERT OR UPDATE ON public.managed_repository_workspace_cleanup_intents
FOR EACH ROW
EXECUTE FUNCTION public.stamp_managed_repository_cleanup_projection_transaction();

CREATE FUNCTION public.stamp_managed_repository_restore_projection_transaction()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.restore_work_projection_transaction_id IS DISTINCT FROM
       OLD.restore_work_projection_transaction_id
       OR NEW.cancel_projection_transaction_id IS DISTINCT FROM
          OLD.cancel_projection_transaction_id
       OR NEW.cancel_claim_projection_transaction_id IS DISTINCT FROM
          OLD.cancel_claim_projection_transaction_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_reservation_projection_transaction_server_owned',
            MESSAGE = 'Workspace reservation projection authority is server-owned';
    END IF;
    IF (
           OLD.cancel_requested_at IS NULL
           AND NEW.cancel_requested_at IS NOT NULL
       ) OR (
           NEW.cancel_requested_at IS NOT NULL
           AND NEW.claim_token IS DISTINCT FROM OLD.claim_token
       ) THEN
        NEW.cancel_claim_projection_transaction_id := txid_current();
    END IF;
    IF OLD.restore_work_completed_at IS NULL
       AND NEW.restore_work_completed_at IS NOT NULL
       AND NEW.restore_work_result_kind IS NOT NULL THEN
        NEW.restore_work_projection_transaction_id := txid_current();
    END IF;
    IF OLD.cancel_cleanup_completed_at IS NULL
       AND NEW.cancel_cleanup_completed_at IS NOT NULL
       AND NEW.result_kind = 'aborted'
       AND NEW.settled_at IS NOT NULL THEN
        NEW.cancel_projection_transaction_id := txid_current();
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_workspace_restore_stamp_projection_transaction
BEFORE UPDATE ON public.managed_repository_workspace_creation_reservations
FOR EACH ROW
EXECUTE FUNCTION public.stamp_managed_repository_restore_projection_transaction();

-- Serialize the previous-release writer boundary before installing the
-- publication fences. Historical receipts are adopted only after application
-- convergence by an explicit, disabled-by-default reconciler.
LOCK TABLE public.jobs IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE public.threads IN SHARE ROW EXCLUSIVE MODE;

CREATE FUNCTION public.managed_repository_workspace_has_process_zero_receipt(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    requested_scope TEXT,
    requested_runtime TEXT
)
RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
    SELECT public.managed_repository_process_zero_receipt_exists(
        requested_owner_kind, requested_owner_id, requested_scope, 'k8s',
        requested_runtime
    ) OR (
        requested_owner_kind = 'thread'
        AND requested_scope = 'workspace_container'
        AND public.managed_repository_process_zero_receipt_exists(
            requested_owner_kind, requested_owner_id, 'stateless_workspace',
            'k8s', requested_runtime
        )
    );
$$;

CREATE FUNCTION public.managed_repository_workspace_cleanup_projection_is_settled(
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
                   AND projected_runtime IS NULL
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

CREATE FUNCTION public.managed_repository_workspace_cleanup_is_pending(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    requested_scope TEXT,
    requested_runtime TEXT
)
RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
    SELECT EXISTS (
        SELECT 1
          FROM public.managed_repository_workspace_cleanup_intents AS intent
         WHERE intent.owner_kind = requested_owner_kind
           AND intent.owner_id = requested_owner_id
           AND intent.scope = requested_scope
           AND intent.runtime_incarnation::TEXT = requested_runtime
           AND intent.settled_at IS NULL
    );
$$;

CREATE FUNCTION public.managed_repository_workspace_creation_is_authorized(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    requested_scope TEXT,
    requested_runtime TEXT,
    requested_reservation TEXT,
    requested_claim_token TEXT
)
RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
    SELECT EXISTS (
        SELECT 1
          FROM public.managed_repository_workspace_creation_reservations AS reservation
         WHERE reservation.owner_kind = requested_owner_kind
           AND reservation.owner_id = requested_owner_id
           AND reservation.scope = requested_scope
           AND reservation.id::TEXT = requested_reservation
           AND reservation.claim_token::TEXT = requested_claim_token
           AND reservation.runtime_incarnation::TEXT = requested_runtime
           AND reservation.phase = 'runtime_bound'
           AND reservation.settled_at IS NULL
           AND reservation.expires_at > now()
           AND reservation.cancel_requested_at IS NULL
    );
$$;

CREATE FUNCTION public.managed_repository_workspace_uidless_creation_is_authorized(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    requested_scope TEXT,
    requested_reservation TEXT,
    requested_claim_token TEXT
)
RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
    SELECT EXISTS (
        SELECT 1
          FROM public.managed_repository_workspace_creation_reservations AS reservation
         WHERE reservation.owner_kind = requested_owner_kind
           AND reservation.owner_id = requested_owner_id
           AND reservation.scope = requested_scope
           AND reservation.id::TEXT = requested_reservation
           AND reservation.claim_token::TEXT = requested_claim_token
           AND reservation.runtime_incarnation IS NULL
           AND reservation.phase IN ('reserved', 'mutating')
           AND reservation.settled_at IS NULL
           AND reservation.expires_at > now()
           AND reservation.cancel_requested_at IS NULL
    );
$$;

CREATE FUNCTION public.managed_repo_cancel_claim_projection_authorized_now(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    requested_scope TEXT,
    requested_runtime TEXT,
    requested_reservation TEXT,
    requested_claim_token TEXT,
    old_state JSONB,
    new_state JSONB
)
RETURNS BOOLEAN LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    state_key TEXT;
    old_runtime_state JSONB;
    new_runtime_state JSONB;
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM public.managed_repository_workspace_creation_reservations AS reservation
         WHERE reservation.owner_kind = requested_owner_kind
           AND reservation.owner_id = requested_owner_id
           AND reservation.scope = requested_scope
           AND reservation.id::TEXT = requested_reservation
           AND reservation.claim_token::TEXT = requested_claim_token
           AND reservation.runtime_incarnation::TEXT IS NOT DISTINCT FROM
               requested_runtime
           AND reservation.phase IN ('mutating', 'runtime_bound')
           AND reservation.settled_at IS NULL
           AND reservation.expires_at > now()
           AND reservation.cancel_requested_at IS NOT NULL
           AND reservation.cancel_claim_projection_transaction_id = txid_current()
    ) THEN
        RETURN FALSE;
    END IF;
    state_key := CASE WHEN requested_scope = 'ide'
        THEN 'ide_session' ELSE 'workspace_container' END;
    old_runtime_state := old_state -> state_key;
    new_runtime_state := new_state -> state_key;
    RETURN jsonb_typeof(old_runtime_state) = 'object'
        AND jsonb_typeof(new_runtime_state) = 'object'
        AND (old_state - state_key) = (new_state - state_key)
        AND old_runtime_state #>> ARRAY['_runtime_incarnation']
            IS NOT DISTINCT FROM requested_runtime
        AND old_runtime_state #>> ARRAY['_creation_reservation_id'] =
            requested_reservation
        AND new_runtime_state #>> ARRAY['_creation_reservation_id'] =
            requested_reservation
        AND old_runtime_state #>> ARRAY['_creation_claim_token'] IS DISTINCT FROM
            requested_claim_token
        AND new_runtime_state #>> ARRAY['_creation_claim_token'] =
            requested_claim_token
        AND (old_runtime_state - '_creation_claim_token') =
            (new_runtime_state - '_creation_claim_token');
END;
$$;

CREATE FUNCTION public.managed_repo_terminal_cancel_projection_authorized_now(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    requested_scope TEXT,
    requested_runtime TEXT,
    requested_reservation TEXT,
    requested_claim_token TEXT,
    old_state JSONB,
    new_state JSONB
)
RETURNS BOOLEAN LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    state_key TEXT;
    old_runtime_state JSONB;
    new_runtime_state JSONB;
BEGIN
    -- A terminal owner transition composes two otherwise-independent guarded
    -- projections in one NEW row: it rotates the exact creation claim and
    -- marks the same live runtime retiring.  Admit only that exact composition
    -- when both durable authorities were written by this transaction.
    IF NOT EXISTS (
        SELECT 1
          FROM public.managed_repository_workspace_creation_reservations AS reservation
         WHERE reservation.owner_kind = requested_owner_kind
           AND reservation.owner_id = requested_owner_id
           AND reservation.scope = requested_scope
           AND reservation.id::TEXT = requested_reservation
           AND reservation.claim_token::TEXT = requested_claim_token
           AND reservation.runtime_incarnation::TEXT IS NOT DISTINCT FROM
               requested_runtime
           AND reservation.phase IN ('mutating', 'runtime_bound')
           AND reservation.settled_at IS NULL
           AND reservation.expires_at > now()
           AND reservation.cancel_requested_at IS NOT NULL
           AND reservation.cancel_claim_projection_transaction_id = txid_current()
    ) OR NOT EXISTS (
        SELECT 1
          FROM public.managed_repository_workspace_cleanup_intents AS intent
         WHERE intent.owner_kind = requested_owner_kind
           AND intent.owner_id = requested_owner_id
           AND intent.scope = requested_scope
           AND intent.runtime_incarnation::TEXT = requested_runtime
           AND intent.intent_source = 'current'
           AND intent.admission_source = 'explicit'
           AND intent.target_disposition = 'deleted'
           AND intent.result_kind IS NULL
           AND intent.settled_at IS NULL
           AND intent.lifecycle_fingerprint ->> 'admitted_by' =
               'terminal_owner_transition'
           AND intent.terminal_admission_transaction_id = txid_current()
    ) THEN
        RETURN FALSE;
    END IF;
    state_key := CASE WHEN requested_scope = 'ide'
        THEN 'ide_session' ELSE 'workspace_container' END;
    old_runtime_state := old_state -> state_key;
    new_runtime_state := new_state -> state_key;
    RETURN jsonb_typeof(old_runtime_state) = 'object'
        AND jsonb_typeof(new_runtime_state) = 'object'
        AND (old_state - state_key) = (new_state - state_key)
        AND old_runtime_state #>> ARRAY['_runtime_incarnation']
            IS NOT DISTINCT FROM requested_runtime
        AND new_runtime_state #>> ARRAY['_runtime_incarnation']
            IS NOT DISTINCT FROM requested_runtime
        AND old_runtime_state #>> ARRAY['_creation_reservation_id'] =
            requested_reservation
        AND new_runtime_state #>> ARRAY['_creation_reservation_id'] =
            requested_reservation
        AND old_runtime_state #>> ARRAY['_creation_claim_token'] IS DISTINCT FROM
            requested_claim_token
        AND new_runtime_state #>> ARRAY['_creation_claim_token'] =
            requested_claim_token
        AND new_runtime_state ->> 'status' = 'retiring_process_zero'
        AND (
            old_runtime_state - '_creation_claim_token' - 'status'
        ) = (
            new_runtime_state - '_creation_claim_token' - 'status'
        );
END;
$$;

CREATE FUNCTION public.managed_repository_workspace_authority_envelope(
    requested_state JSONB,
    requested_scope TEXT
)
RETURNS JSONB LANGUAGE SQL IMMUTABLE AS $$
    SELECT jsonb_build_object(
        'provisioner', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'provisioner'
        ],
        'restore_type', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'restore_type'
        ],
        'status', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'status'
        ],
        '_runtime_incarnation', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            '_runtime_incarnation'
        ],
        '_creation_reservation_id', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            '_creation_reservation_id'
        ],
        '_creation_claim_token', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            '_creation_claim_token'
        ],
        'pod_name', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'pod_name'
        ],
        'container_name', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'container_name'
        ],
        'namespace', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'namespace'
        ],
        'pod_ip', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'pod_ip'
        ],
        'host', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'host'
        ],
        'port', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'port'
        ],
        'container_port', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'container_port'
        ],
        'service_name', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'service_name'
        ],
        'code_server_url', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'code_server_url'
        ],
        'backing_id', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'backing_id'
        ],
        'generation', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'generation'
        ],
        'workspace_generation', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'workspace_generation'
        ],
        'endpoint_generation', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'endpoint_generation'
        ],
        '_canvas_workspace_generation', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            '_canvas_workspace_generation'
        ],
        'ssh_host_key_fingerprint', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'ssh_host_key_fingerprint'
        ],
        'host_key_fingerprint', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'host_key_fingerprint'
        ],
        '_snapshot_restore_required', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            '_snapshot_restore_required'
        ],
        'endpoint', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'endpoint'
        ],
        'ssh', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'ssh'
        ],
        'binding', requested_state #> ARRAY[
            CASE WHEN requested_scope = 'ide'
                THEN 'ide_session' ELSE 'workspace_container' END,
            'binding'
        ],
        '_workspace_binding', CASE WHEN requested_scope = 'workspace_container'
            THEN requested_state -> '_workspace_binding' ELSE NULL END
    );
$$;

CREATE FUNCTION public.managed_repo_workspace_cleanup_projection_authorized_now(
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
           AND intent.target_disposition = 'deleted' THEN
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

CREATE FUNCTION public.managed_repo_workspace_restore_projection_authorized_now(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    requested_scope TEXT,
    requested_runtime TEXT,
    new_reservation TEXT,
    new_claim_token TEXT,
    old_state JSONB,
    new_state JSONB
)
RETURNS BOOLEAN LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    reservation RECORD;
    state_key TEXT;
    old_runtime_state JSONB;
    new_runtime_state JSONB;
    old_normalized JSONB;
    new_normalized JSONB;
BEGIN
    SELECT * INTO reservation
      FROM public.managed_repository_workspace_creation_reservations
     WHERE owner_kind = requested_owner_kind
       AND owner_id = requested_owner_id
       AND scope = requested_scope
       AND id::TEXT = new_reservation
       AND claim_token::TEXT = new_claim_token
       AND runtime_incarnation::TEXT = requested_runtime
       AND operation_kind = 'restore'
       AND result_kind = 'settled'
       AND settled_at IS NOT NULL
       AND restore_work_completed_at IS NOT NULL
       AND restore_work_result_kind IS NOT NULL
       AND restore_work_projection_transaction_id = txid_current();
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    state_key := CASE WHEN requested_scope = 'ide'
        THEN 'ide_session' ELSE 'workspace_container' END;
    old_runtime_state := old_state -> state_key;
    new_runtime_state := new_state -> state_key;
    IF jsonb_typeof(old_runtime_state) <> 'object'
       OR jsonb_typeof(new_runtime_state) <> 'object'
       OR (old_state - state_key) IS DISTINCT FROM (new_state - state_key)
       OR new_runtime_state #>> ARRAY['_runtime_incarnation']
            IS DISTINCT FROM requested_runtime
       OR new_runtime_state #>> ARRAY['_creation_reservation_id']
            IS DISTINCT FROM new_reservation
       OR new_runtime_state #>> ARRAY['_creation_claim_token']
            IS DISTINCT FROM new_claim_token THEN
        RETURN FALSE;
    END IF;

    IF requested_scope = 'workspace_container' THEN
        IF reservation.restore_work_result_kind = 'ready' THEN
            old_normalized := old_runtime_state - ARRAY[
                'status', 'error', 'restored_at', '_snapshot_restore_required'
            ];
            new_normalized := new_runtime_state - ARRAY[
                'status', 'error', 'restored_at', '_snapshot_restore_required'
            ];
            RETURN old_normalized = new_normalized
                AND new_runtime_state ->> 'status' = 'ready'
                AND new_runtime_state -> '_snapshot_restore_required' =
                    'false'::JSONB
                AND jsonb_typeof(new_runtime_state -> 'restored_at') = 'string';
        END IF;
        old_normalized := old_runtime_state - ARRAY['status', 'error'];
        new_normalized := new_runtime_state - ARRAY['status', 'error'];
        RETURN reservation.restore_work_result_kind = 'failed'
            AND old_normalized = new_normalized
            AND new_runtime_state ->> 'status' = 'failed'
            AND jsonb_typeof(new_runtime_state -> 'error') = 'string'
            AND length(new_runtime_state ->> 'error') > 0;
    END IF;

    IF reservation.restore_work_result_kind = 'active' THEN
        old_normalized := old_runtime_state - ARRAY[
            'status', 'error', 'code_server_url', 'restore_type', 'last_activity'
        ];
        new_normalized := new_runtime_state - ARRAY[
            'status', 'error', 'code_server_url', 'restore_type', 'last_activity'
        ];
        RETURN old_normalized = new_normalized
            AND new_runtime_state ->> 'status' = 'active'
            AND new_runtime_state ->> 'restore_type' = 'k8s_container'
            AND jsonb_typeof(new_runtime_state -> 'code_server_url') = 'string'
            AND length(new_runtime_state ->> 'code_server_url') > 0
            AND jsonb_typeof(new_runtime_state -> 'last_activity') = 'string';
    END IF;
    old_normalized := old_runtime_state - ARRAY[
        'status', 'error', 'code_server_url'
    ];
    new_normalized := new_runtime_state - ARRAY[
        'status', 'error', 'code_server_url'
    ];
    RETURN reservation.restore_work_result_kind = 'failed'
        AND old_normalized = new_normalized
        AND new_runtime_state ->> 'status' = 'failed'
        AND new_runtime_state -> 'code_server_url' = 'null'::JSONB
        AND jsonb_typeof(new_runtime_state -> 'error') = 'string'
        AND length(new_runtime_state ->> 'error') > 0;
END;
$$;

CREATE FUNCTION public.managed_repo_cancelled_creation_projection_authorized_now(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    requested_scope TEXT,
    old_state JSONB,
    new_state JSONB
)
RETURNS BOOLEAN LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    reservation RECORD;
    state_key TEXT;
    old_runtime_state JSONB;
    new_runtime_state JSONB;
    expected_runtime_state JSONB;
BEGIN
    SELECT * INTO reservation
      FROM public.managed_repository_workspace_creation_reservations
     WHERE owner_kind = requested_owner_kind
       AND owner_id = requested_owner_id
       AND scope = requested_scope
       AND runtime_incarnation IS NULL
       AND pod_uid IS NULL
       AND result_kind = 'aborted'
       AND phase = 'aborted'
       AND settled_at IS NOT NULL
       AND cancel_requested_at IS NOT NULL
       AND cancel_cleanup_completed_at IS NOT NULL
       AND cancel_projection_transaction_id = txid_current()
     ORDER BY reservation_generation DESC
     LIMIT 1;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    state_key := CASE WHEN requested_scope = 'ide'
        THEN 'ide_session' ELSE 'workspace_container' END;
    old_runtime_state := COALESCE(old_state -> state_key, '{}'::JSONB);
    new_runtime_state := new_state -> state_key;
    IF jsonb_typeof(old_runtime_state) <> 'object'
       OR jsonb_typeof(new_runtime_state) <> 'object'
       OR (old_state - state_key) IS DISTINCT FROM (new_state - state_key) THEN
        RETURN FALSE;
    END IF;
    expected_runtime_state := (
        old_runtime_state
        - '_creation_reservation_id'
        - '_creation_claim_token'
    ) || jsonb_build_object(
        'status', reservation.cancel_target_disposition,
        'pod_ip', NULL::TEXT
    );
    IF requested_scope = 'workspace_container' THEN
        expected_runtime_state := expected_runtime_state || jsonb_build_object(
            'pod_name', NULL::TEXT
        );
        IF reservation.cancel_target_disposition = 'suspended'
           AND reservation.cancel_suspended_at IS NOT NULL THEN
            IF jsonb_typeof(new_runtime_state -> 'suspended_at') <> 'string' THEN
                RETURN FALSE;
            END IF;
            expected_runtime_state := expected_runtime_state || jsonb_build_object(
                'suspended_at', new_runtime_state -> 'suspended_at'
            );
        END IF;
        IF requested_owner_kind = 'thread'
           AND reservation.cancel_target_disposition = 'deleted' THEN
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

CREATE FUNCTION public.prevent_retired_workspace_runtime_rebinding()
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

CREATE FUNCTION public.prevent_workspace_owner_delete_before_cleanup()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    source_kind TEXT;
    source_state JSONB;
    runtime_state JSONB;
    runtime_uid TEXT;
    scope_name TEXT;
    state_key TEXT;
    parent_id UUID;
    parent_state JSONB := '{}'::JSONB;
    parent_runtime JSONB;
    declared_inherited BOOLEAN := FALSE;
    inherited_scope BOOLEAN;
BEGIN
    IF TG_TABLE_NAME = 'threads' AND OLD.execution_lane = 'pinned' THEN
        RETURN OLD;
    END IF;
    source_kind := CASE WHEN TG_TABLE_NAME = 'jobs' THEN 'job' ELSE 'thread' END;
    source_state := CASE WHEN TG_TABLE_NAME = 'jobs'
        THEN COALESCE(to_jsonb(OLD) -> 'context', '{}'::JSONB)
        ELSE COALESCE(to_jsonb(OLD) -> 'metadata', '{}'::JSONB)
    END;
    IF source_kind = 'job' THEN
        BEGIN
            parent_id := (to_jsonb(OLD) ->> 'parent_job_id')::UUID;
        EXCEPTION WHEN invalid_text_representation THEN
            parent_id := NULL;
        END;
        declared_inherited := parent_id IS NOT NULL
            AND source_state ->> 'inherits_parent_workspace' = 'true'
            AND (
                NOT (source_state ? '_workspace_contract')
                OR source_state #>> ARRAY[
                    '_workspace_contract', 'assignment_source'
                ] = 'parent_inheritance'
            );
        IF declared_inherited THEN
            SELECT COALESCE(parent.context, '{}'::JSONB)
              INTO parent_state
              FROM public.jobs AS parent
             WHERE parent.id = parent_id;
            parent_state := COALESCE(parent_state, '{}'::JSONB);
        END IF;
    END IF;

    -- A settled creation receipt is not a retirement receipt.  Deleting its
    -- owner would orphan the exact Pod/PVC/Service authority, so every live
    -- managed projection (including historical UID-less shapes) must first
    -- reach an exact absorbing terminal-reclaim settlement.
    FOR scope_name, state_key IN
        SELECT * FROM (VALUES
            ('workspace_container'::TEXT, 'workspace_container'::TEXT),
            ('ide'::TEXT, 'ide_session'::TEXT)
        ) AS scopes(scope_name, state_key)
    LOOP
        IF source_kind = 'thread' AND scope_name = 'ide' THEN
            CONTINUE;
        END IF;
        -- An effectful creation cancelled above still owns the exact partial
        -- resource inventory and rotated reconciliation token.  Its
        -- same-generation cancellation path will capture the accepted Pod (or
        -- prove absolute pre-Pod absence) before converting to cleanup.  Only
        -- an already-settled live runtime is admitted directly here.
        --
        -- This trigger is BEFORE DELETE, so NEW is unassigned and every field
        -- reference silently reads NULL rather than raising.  The owner
        -- identity must therefore come from OLD, or this fence degrades into
        -- an always-false predicate and the in-flight creation is reported
        -- under the wrong (terminal/legacy) cleanup constraint instead of the
        -- unsettled-reservation one.
        IF EXISTS (
            SELECT 1
              FROM public.managed_repository_workspace_creation_reservations
             WHERE owner_kind = source_kind
               AND owner_id = OLD.id
               AND scope = scope_name
               AND settled_at IS NULL
        ) THEN
            CONTINUE;
        END IF;
        runtime_state := source_state -> state_key;
        IF runtime_state IS NULL
           OR jsonb_typeof(runtime_state) <> 'object'
           OR runtime_state = '{}'::JSONB THEN
            CONTINUE;
        END IF;
        parent_runtime := parent_state -> state_key;
        inherited_scope := declared_inherited
            AND jsonb_typeof(parent_runtime) = 'object'
            AND (
                (
                    scope_name = 'workspace_container'
                    AND runtime_state ->> 'provisioner'
                        = parent_runtime ->> 'provisioner'
                    AND (
                        (
                            runtime_state ->> '_runtime_incarnation' IS NOT NULL
                            AND runtime_state ->> '_runtime_incarnation'
                                = parent_runtime ->> '_runtime_incarnation'
                        )
                        OR (
                            runtime_state ->> '_runtime_incarnation' IS NULL
                            AND runtime_state = parent_runtime
                        )
                        OR public.managed_repository_process_zero_receipt_exists(
                            'job', parent_id, 'workspace_container', 'k8s',
                            runtime_state ->> '_runtime_incarnation'
                        )
                    )
                )
                OR (
                    scope_name = 'ide'
                    AND runtime_state ->> '_runtime_incarnation' IS NOT NULL
                    AND (
                        runtime_state ->> '_runtime_incarnation'
                            = parent_runtime ->> '_runtime_incarnation'
                        OR public.managed_repository_process_zero_receipt_exists(
                            'job', parent_id, 'ide', 'k8s',
                            runtime_state ->> '_runtime_incarnation'
                        )
                    )
                )
            );
        IF inherited_scope THEN
            CONTINUE;
        END IF;
        IF (scope_name = 'workspace_container'
                AND runtime_state ->> 'provisioner' <> 'k8s')
           OR (scope_name = 'ide'
                AND runtime_state ->> 'restore_type' <> 'k8s_container') THEN
            CONTINUE;
        END IF;
        runtime_uid := runtime_state ->> '_runtime_incarnation';
        IF runtime_uid IS NULL THEN
            -- A caller-authored terminal-looking projection is not evidence
            -- that the API server never accepted an earlier Pod/PVC/Service
            -- mutation.  UID-less historical state has no exact authority to
            -- bind a terminal receipt, so raw owner deletion must fail closed
            -- for every non-empty managed projection, including failed,
            -- deleted, and expired shapes.
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_legacy_workspace_cleanup_required_before_owner_delete',
                MESSAGE = 'A legacy Kubernetes projection cannot be deleted without exact cleanup authority';
        END IF;
        IF runtime_uid !~* '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
           OR NOT EXISTS (
                SELECT 1
                  FROM public.managed_repository_workspace_cleanup_intents AS intent
                 WHERE intent.owner_kind = source_kind
                   AND intent.owner_id = OLD.id
                   AND intent.scope = scope_name
                   AND intent.runtime_incarnation::TEXT = runtime_uid
                   AND intent.resource_policy = 'terminal_reclaim'
                   AND intent.target_disposition = 'deleted'
                   AND intent.result_kind = 'settled'
                   AND intent.cleanup_completed_at IS NOT NULL
                   AND intent.settled_at IS NOT NULL
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_terminal_workspace_cleanup_required_before_owner_delete',
                MESSAGE = 'A live Kubernetes projection requires exact terminal reclaim before owner deletion';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1
          FROM public.managed_repository_workspace_cleanup_intents AS intent
         WHERE intent.owner_kind = source_kind
           AND intent.owner_id = OLD.id
           AND intent.settled_at IS NULL
    ) OR EXISTS (
        SELECT 1
          FROM public.managed_repository_workspace_creation_reservations AS reservation
         WHERE reservation.owner_kind = source_kind
           AND reservation.owner_id = OLD.id
           AND reservation.settled_at IS NULL
    ) OR EXISTS (
        SELECT 1
          FROM public.managed_repository_process_zero_receipts AS receipt
         WHERE receipt.owner_kind = source_kind
           AND receipt.owner_id = OLD.id
           AND receipt.provisioner = 'k8s'
           AND receipt.scope IN (
               'workspace_container', 'stateless_workspace', 'ide'
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.managed_repository_workspace_cleanup_intents AS intent
                WHERE intent.owner_kind = source_kind
                  AND intent.owner_id = OLD.id
                  AND intent.scope = CASE
                      WHEN receipt.scope = 'stateless_workspace'
                      THEN 'workspace_container'
                      ELSE receipt.scope
                  END
                  AND intent.runtime_incarnation::TEXT = receipt.runtime_incarnation
                  AND intent.resource_policy = 'terminal_reclaim'
                  AND intent.target_disposition = 'deleted'
                  AND intent.result_kind = 'settled'
                  AND intent.cleanup_completed_at IS NOT NULL
                  AND intent.settled_at IS NOT NULL
           )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_workspace_cleanup_required_before_owner_delete',
            MESSAGE = 'Exact Kubernetes workspace cleanup must settle before owner deletion';
    END IF;
    RETURN OLD;
END;
$$;

CREATE FUNCTION public.cancel_workspace_creation_on_terminal_owner_transition()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    source_kind TEXT;
    source_state JSONB;
    state_key TEXT;
    reservation RECORD;
    rotated_token BIGINT;
    thread_terminal_token BIGINT;
    locked_queue_token BIGINT;
    thread_terminal_reclaim BOOLEAN := FALSE;
    desired_resource_policy TEXT;
    scope_name TEXT;
    runtime_state JSONB;
    runtime_uid TEXT;
    inserted_cleanup UUID;
BEGIN
    IF NEW.status IS NOT DISTINCT FROM OLD.status OR NOT (
        (TG_TABLE_NAME = 'jobs' AND NEW.status::TEXT IN (
            'completed', 'failed', 'cancelled'
        ))
        OR (TG_TABLE_NAME = 'threads' AND NEW.status::TEXT = 'ended')
    ) THEN
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'threads' AND NEW.execution_lane = 'pinned' THEN
        RETURN NEW;
    END IF;

    source_kind := CASE WHEN TG_TABLE_NAME = 'jobs' THEN 'job' ELSE 'thread' END;
    source_state := CASE WHEN TG_TABLE_NAME = 'jobs'
        THEN COALESCE(to_jsonb(NEW) -> 'context', '{}'::JSONB)
        ELSE COALESCE(to_jsonb(NEW) -> 'metadata', '{}'::JSONB)
    END;

    -- Do not wait while the row-update already owns the owner lock: an
    -- external creator holds the matching session lock and must reacquire the
    -- owner row to persist its exact observed UID, so blocking here would
    -- deadlock.  A terminal writer instead acquires the same domain with a
    -- non-blocking transaction lock or fails atomically and retries after the
    -- bounded, joined Kubernetes mutation completes.  All token rotation and
    -- cancellation below therefore occur under the shared owner/scope guard.
    IF NOT pg_try_advisory_xact_lock(hashtextextended(
        'workspace_runtime_mutation:' || source_kind || ':'
            || NEW.id::TEXT || ':workspace_container', 0
    )) THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'Workspace mutation is still in progress';
    END IF;
    IF source_kind = 'job' AND NOT pg_try_advisory_xact_lock(hashtextextended(
        'workspace_runtime_mutation:' || source_kind || ':'
            || NEW.id::TEXT || ':ide', 0
    )) THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'IDE mutation is still in progress';
    END IF;
    IF source_kind = 'thread'
       AND (source_state #>> ARRAY[
           '_stateless_claim_retirement', 'permanent'
       ]) = 'true' THEN
        BEGIN
            thread_terminal_token := (
                source_state #>> ARRAY[
                    '_stateless_claim_retirement', 'terminal_token'
                ]
            )::BIGINT;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            thread_terminal_token := NULL;
        END;
        IF thread_terminal_token IS NOT NULL AND thread_terminal_token > 0 THEN
            SELECT lease_token
              INTO locked_queue_token
              FROM public.run_queue
             WHERE unit_id = NEW.id
               AND unit_kind = 'session_turn'
               AND state = 'done'
               AND lease_token = thread_terminal_token
               AND leased_by IS NULL
             FOR UPDATE;
            thread_terminal_reclaim := locked_queue_token IS NOT NULL;
        END IF;
    END IF;

    -- A terminal owner cannot admit or continue post-create restore work.
    -- Clearing the lease under the owner-row lock makes the running claimant's
    -- next renewal fail; the settled creation receipt remains as exact-B
    -- evidence for subsequent cleanup.
    UPDATE public.managed_repository_workspace_creation_reservations
       SET restore_work_claimed_by = NULL,
           restore_work_claim_expires_at = NULL,
           restore_work_next_attempt_at = now()
     WHERE owner_kind = source_kind
       AND owner_id = NEW.id
       AND operation_kind = 'restore'
       AND result_kind = 'settled'
       AND restore_work_completed_at IS NULL;

    FOR reservation IN
        SELECT *
          FROM public.managed_repository_workspace_creation_reservations
         WHERE owner_kind = source_kind
           AND owner_id = NEW.id
           AND settled_at IS NULL
         ORDER BY scope
         FOR UPDATE
    LOOP
        desired_resource_policy := CASE
            WHEN reservation.scope = 'workspace_container'
                 AND (
                     source_kind = 'job'
                     OR (source_kind = 'thread' AND thread_terminal_reclaim)
                 )
            THEN 'terminal_reclaim'
            ELSE 'preserve'
        END;
        IF reservation.phase = 'reserved'
           AND reservation.external_mutation_started_at IS NULL THEN
            UPDATE public.managed_repository_workspace_creation_reservations
               SET cancel_requested_at = now(),
                   cancel_target_disposition = CASE
                       WHEN reservation.scope = 'ide' THEN 'deleted'
                       ELSE 'deleted'
                   END,
                   cancel_resource_policy = desired_resource_policy,
                   cancel_snapshot_restore_required = FALSE,
                   settled_at = now(),
                   phase = 'aborted',
                   result_kind = 'aborted'
             WHERE id = reservation.id;
            CONTINUE;
        END IF;

        rotated_token := nextval(
            'public.managed_repository_workspace_creation_claim_seq'
        );
        UPDATE public.managed_repository_workspace_creation_reservations
           SET cancel_requested_at = COALESCE(cancel_requested_at, now()),
               cancel_target_disposition = 'deleted',
               cancel_resource_policy = desired_resource_policy,
               cancel_suspended_at = NULL,
               cancel_snapshot_restore_required = FALSE,
               claimed_by = 'terminal-owner-transition',
               claim_token = rotated_token,
               expires_at = GREATEST(
                   now() + INTERVAL '1 millisecond',
                   created_at + INTERVAL '1 millisecond'
               ),
               attempts = attempts + 1,
               next_attempt_at = now()
         WHERE id = reservation.id;

        state_key := CASE WHEN reservation.scope = 'ide'
            THEN 'ide_session' ELSE 'workspace_container' END;
        IF reservation.runtime_incarnation IS NOT NULL
           AND source_state #>> ARRAY[
               state_key, '_runtime_incarnation'
           ] = reservation.runtime_incarnation::TEXT
           AND source_state #>> ARRAY[
               state_key, '_creation_reservation_id'
           ] = reservation.id::TEXT
           AND source_state #>> ARRAY[
               state_key, '_creation_claim_token'
           ] = reservation.claim_token::TEXT THEN
            source_state := jsonb_set(
                source_state,
                ARRAY[state_key, '_creation_claim_token'],
                to_jsonb(rotated_token::TEXT),
                FALSE
            );
        END IF;
    END LOOP;

    -- Class-A terminal state is also the durable cleanup admission point for
    -- a runtime whose creation generation already settled.  The trigger does
    -- not claim external work; it freezes the exact UID and leaves resource
    -- capture/deletion to the guarded reconciler.
    FOR scope_name, state_key IN
        SELECT * FROM (VALUES
            ('workspace_container'::TEXT, 'workspace_container'::TEXT),
            ('ide'::TEXT, 'ide_session'::TEXT)
        ) AS scopes(scope_name, state_key)
    LOOP
        IF source_kind = 'thread' AND scope_name = 'ide' THEN
            CONTINUE;
        END IF;
        runtime_state := source_state -> state_key;
        IF jsonb_typeof(runtime_state) <> 'object'
           OR (scope_name = 'workspace_container'
               AND runtime_state ->> 'provisioner' <> 'k8s')
           OR (scope_name = 'ide'
               AND runtime_state ->> 'restore_type' <> 'k8s_container') THEN
            CONTINUE;
        END IF;
        runtime_uid := runtime_state ->> '_runtime_incarnation';
        IF runtime_uid IS NULL
           OR runtime_uid !~* '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN
            CONTINUE;
        END IF;
        desired_resource_policy := CASE
            WHEN source_kind = 'job' THEN 'terminal_reclaim'
            WHEN scope_name = 'workspace_container' AND thread_terminal_reclaim
                THEN 'terminal_reclaim'
            ELSE 'preserve'
        END;
        inserted_cleanup := NULL;

        -- A terminal Class-A transition must not lose to a cleanup generation
        -- admitted just before it took the owner lock.  Promote that exact
        -- live-runtime generation in place; the one-active-per-scope index
        -- then remains the serialization backstop.  Ambiguous discovery is
        -- now exact because the owner projection supplies the immutable UID.
        UPDATE public.managed_repository_workspace_cleanup_intents
           SET intent_source = 'current',
               admission_source = 'explicit',
               target_disposition = 'deleted',
               resource_policy = desired_resource_policy,
               reclaim_shared_resources = (
                   scope_name = 'workspace_container'
                   AND desired_resource_policy = 'terminal_reclaim'
               ),
               lifecycle_fingerprint = lifecycle_fingerprint
                   || jsonb_build_object(
                       'owner_status', NEW.status::TEXT,
                       'runtime_status', COALESCE(
                           runtime_state ->> 'status', ''
                       ),
                       'admitted_by', 'terminal_owner_transition'
                   ),
               terminal_queue_token = CASE
                   WHEN source_kind = 'thread' THEN locked_queue_token
                   ELSE 0
               END,
               suspended_at = NULL,
               snapshot_restore_required = FALSE,
               phase = CASE WHEN phase = 'ambiguous'
                   THEN 'prepared' ELSE phase END,
               next_attempt_at = now()
         WHERE owner_kind = source_kind
           AND owner_id = NEW.id
           AND scope = scope_name
           AND runtime_incarnation::TEXT = runtime_uid
           AND settled_at IS NULL
        RETURNING id INTO inserted_cleanup;

        IF inserted_cleanup IS NULL THEN
        INSERT INTO public.managed_repository_workspace_cleanup_intents (
            owner_kind, owner_id, thread_runtime_generation,
            scope, runtime_incarnation,
            intent_source, admission_source, target_disposition,
            resource_policy, reclaim_shared_resources,
            lifecycle_fingerprint, terminal_queue_token, pod_uid,
            capture_complete, snapshot_restore_required, phase
        ) VALUES (
            source_kind, NEW.id,
            CASE WHEN source_kind = 'thread'
                 THEN (to_jsonb(NEW) ->> 'runtime_generation')::UUID
                 ELSE NULL END,
            scope_name, runtime_uid::UUID,
            'current', 'explicit', 'deleted', desired_resource_policy,
            (scope_name = 'workspace_container'
                AND desired_resource_policy = 'terminal_reclaim'),
            jsonb_build_object(
                'owner_status', NEW.status::TEXT,
                'runtime_status', COALESCE(runtime_state ->> 'status', ''),
                'admitted_by', 'terminal_owner_transition'
            ),
            CASE WHEN source_kind = 'thread' THEN locked_queue_token ELSE 0 END,
            runtime_uid::UUID, FALSE, FALSE, 'prepared'
        ) ON CONFLICT (
            owner_kind, owner_id, scope, runtime_incarnation,
            target_disposition, resource_policy
        ) DO NOTHING
        RETURNING id INTO inserted_cleanup;
        END IF;

        IF EXISTS (
            SELECT 1
              FROM public.managed_repository_workspace_cleanup_intents AS intent
             WHERE intent.owner_kind = source_kind
               AND intent.owner_id = NEW.id
               AND intent.scope = scope_name
               AND intent.runtime_incarnation::TEXT = runtime_uid
               AND intent.result_kind IS NULL
        ) THEN
            runtime_state := jsonb_set(
                runtime_state, ARRAY['status'],
                to_jsonb('retiring_process_zero'::TEXT), TRUE
            );
            source_state := jsonb_set(
                source_state, ARRAY[state_key], runtime_state, TRUE
            );
        END IF;
    END LOOP;

    IF TG_TABLE_NAME = 'jobs' THEN
        NEW := jsonb_populate_record(
            NEW, jsonb_build_object('context', source_state)
        );
    ELSE
        NEW := jsonb_populate_record(
            NEW, jsonb_build_object('metadata', source_state)
        );
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_jobs_block_retired_workspace_runtime_rebind
    ON public.jobs;
CREATE TRIGGER trg_jobs_require_workspace_creation_reservation_on_insert
BEFORE INSERT ON public.jobs FOR EACH ROW
EXECUTE FUNCTION public.prevent_retired_workspace_runtime_rebinding();

DROP TRIGGER IF EXISTS trg_threads_block_retired_workspace_runtime_rebind
    ON public.threads;
CREATE TRIGGER trg_threads_require_workspace_creation_reservation_on_insert
BEFORE INSERT ON public.threads FOR EACH ROW
EXECUTE FUNCTION public.prevent_retired_workspace_runtime_rebinding();

CREATE TRIGGER trg_jobs_cancel_workspace_creation_on_terminal_status
BEFORE UPDATE OF status ON public.jobs FOR EACH ROW
EXECUTE FUNCTION public.cancel_workspace_creation_on_terminal_owner_transition();

CREATE TRIGGER trg_threads_cancel_workspace_creation_on_terminal_status
BEFORE UPDATE OF status ON public.threads FOR EACH ROW
EXECUTE FUNCTION public.cancel_workspace_creation_on_terminal_owner_transition();

-- PostgreSQL orders same-event triggers by name.  The final validator must run
-- after terminal cancellation, because that earlier trigger rotates the exact
-- creation token in NEW while holding the reservation row.
CREATE TRIGGER trg_jobs_d_validate_workspace_authority_envelope
BEFORE UPDATE OF context, status ON public.jobs FOR EACH ROW
EXECUTE FUNCTION public.prevent_retired_workspace_runtime_rebinding();

CREATE TRIGGER trg_threads_d_validate_workspace_authority_envelope
BEFORE UPDATE OF metadata, status ON public.threads FOR EACH ROW
EXECUTE FUNCTION public.prevent_retired_workspace_runtime_rebinding();

-- owner deletion first proves the stronger terminal-reclaim authority; the
-- older trigger remains an independent exact process-zero backstop.
CREATE TRIGGER trg_jobs_c_require_workspace_cleanup_before_delete
BEFORE DELETE ON public.jobs FOR EACH ROW
EXECUTE FUNCTION public.prevent_workspace_owner_delete_before_cleanup();

CREATE TRIGGER trg_threads_c_require_workspace_cleanup_before_delete
BEFORE DELETE ON public.threads FOR EACH ROW
EXECUTE FUNCTION public.prevent_workspace_owner_delete_before_cleanup();

COMMIT;
