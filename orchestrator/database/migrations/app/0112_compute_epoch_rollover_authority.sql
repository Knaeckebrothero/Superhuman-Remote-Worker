-- migration:     0112_compute_epoch_rollover_authority.sql
-- description:   Append-only exact-epoch authority for audited Slice 3
--                recovery promotion and direct interval-to-epoch binding.
-- depends-on:    0109_compute_exact_epoch_lifecycle.sql,
--                0111_thread_messages_live_index.notx.sql
-- expected:      < 10s while Slice 3 compute publication remains disabled.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on inventory epochs,
--                compute activation/authority, intervals, and lifecycle heads.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

LOCK TABLE resource_inventory_scope_epochs,
           compute_metering_activation,
           compute_metering_scope_requirements,
           resource_intervals,
           resource_lifecycle_heads
    IN SHARE ROW EXCLUSIVE MODE;

-- No released configuration can activate Slice 3. Refuse to guess historical
-- epoch authority if an operator bypassed the dark-launch gates before this
-- migration; an invented backfill could turn shadow history into billable
-- usage or silently bridge a WATCH gap.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.compute_metering_scope_requirements
    ) OR EXISTS (
        SELECT 1
        FROM public.compute_metering_activation
        WHERE state = 'active'
    ) OR EXISTS (
        SELECT 1
        FROM public.resource_intervals AS interval
        WHERE (interval.source_kind = 'pod'
               AND interval.category = 'compute'
               AND interval.resource = 'agent_pod')
           OR (interval.source_kind = 'pod'
               AND interval.category = 'compute'
               AND interval.resource = 'workspace_pod'
               AND interval.details->>'product_class' = 'ide-session')
           OR (interval.source_kind = 'vmi'
               AND interval.category = 'compute'
               AND interval.resource = 'workspace_vm')
    ) THEN
        RAISE EXCEPTION
            'cannot safely backfill append-only compute epoch authority'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

-- A recovery LIST may finish before the next generic day boundary and then be
-- superseded by another 410. Such an empty future-required epoch is valid and
-- must not make a second recovery impossible. Its uncertainty remains in the
-- predecessor gap rows and in the compute authority discontinuity.
ALTER TABLE resource_inventory_scope_epochs
    DROP CONSTRAINT resource_inventory_scope_epochs_retirement_check,
    ADD CONSTRAINT resource_inventory_scope_epochs_retirement_check CHECK (
        retired_at IS NULL
        OR ((reliable_from IS NULL OR retired_at >= reliable_from)
            AND (continuous_since IS NULL OR retired_at >= continuous_since)
            AND (complete_through IS NULL OR retired_at >= complete_through))
    );

CREATE TABLE compute_metering_epoch_promotion_requests (
    id                UUID PRIMARY KEY,
    activation_key    TEXT NOT NULL
        REFERENCES compute_metering_activation(activation_key)
        ON DELETE RESTRICT,
    request_kind      TEXT NOT NULL,
    collector_id      TEXT NOT NULL,
    source_cluster    TEXT NOT NULL,
    request_digest    TEXT NOT NULL,
    actor_id          UUID NOT NULL,
    audit_reason      TEXT NOT NULL,
    promoted_at       TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),

    CONSTRAINT compute_epoch_promotion_requests_kind_check CHECK (
        request_kind IN ('initial-activation', 'recovery-rollover')
    ),
    CONSTRAINT compute_epoch_promotion_requests_identity_check CHECK (
        collector_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
        AND source_cluster ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$'
        AND request_digest ~ '^[0-9a-f]{64}$'
        AND length(btrim(audit_reason)) BETWEEN 1 AND 2048
        AND promoted_at = created_at
    )
);

CREATE INDEX compute_epoch_promotion_requests_activation_idx
    ON compute_metering_epoch_promotion_requests (
        activation_key, promoted_at DESC, id
    );

CREATE TABLE compute_metering_epoch_authorities (
    id                         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    activation_key             TEXT NOT NULL
        REFERENCES compute_metering_activation(activation_key)
        ON DELETE RESTRICT,
    collector_id               TEXT NOT NULL,
    source_cluster             TEXT NOT NULL,
    inventory_scope_id         UUID NOT NULL,
    inventory_scope_epoch_id   UUID NOT NULL,
    previous_authority_id      UUID,
    predecessor_epoch_id       UUID,
    authority_sequence         BIGINT NOT NULL,
    effective_from             TIMESTAMPTZ NOT NULL,
    proof_snapshot_id          UUID NOT NULL,
    proof_generation           BIGINT NOT NULL,
    promotion_request_id       UUID NOT NULL
        REFERENCES compute_metering_epoch_promotion_requests(id)
        ON DELETE RESTRICT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),

    CONSTRAINT compute_epoch_authorities_scope_fkey
        FOREIGN KEY (inventory_scope_id, collector_id, source_cluster)
        REFERENCES resource_inventory_scopes (
            id, collector_id, source_cluster
        ) ON DELETE RESTRICT,
    CONSTRAINT compute_epoch_authorities_epoch_scope_fkey
        FOREIGN KEY (inventory_scope_epoch_id, inventory_scope_id)
        REFERENCES resource_inventory_scope_epochs (id, scope_id)
        ON DELETE RESTRICT,
    CONSTRAINT compute_epoch_authorities_predecessor_scope_fkey
        FOREIGN KEY (predecessor_epoch_id, inventory_scope_id)
        REFERENCES resource_inventory_scope_epochs (id, scope_id)
        ON DELETE RESTRICT,
    CONSTRAINT compute_epoch_authorities_proof_epoch_fkey
        FOREIGN KEY (proof_snapshot_id, inventory_scope_epoch_id)
        REFERENCES resource_inventory_snapshots (id, scope_epoch_id)
        ON DELETE RESTRICT,
    CONSTRAINT compute_epoch_authorities_proof_scope_fkey
        FOREIGN KEY (proof_snapshot_id, inventory_scope_id)
        REFERENCES resource_inventory_snapshots (id, inventory_scope_id)
        ON DELETE RESTRICT,
    CONSTRAINT compute_epoch_authorities_id_scope_uq
        UNIQUE (id, activation_key, inventory_scope_id),
    CONSTRAINT compute_epoch_authorities_previous_fkey
        FOREIGN KEY (
            previous_authority_id, activation_key, inventory_scope_id
        ) REFERENCES compute_metering_epoch_authorities (
            id, activation_key, inventory_scope_id
        ) ON DELETE RESTRICT,
    CONSTRAINT compute_epoch_authorities_epoch_uq
        UNIQUE (activation_key, inventory_scope_id, inventory_scope_epoch_id),
    CONSTRAINT compute_epoch_authorities_sequence_uq
        UNIQUE (activation_key, inventory_scope_id, authority_sequence),
    CONSTRAINT compute_epoch_authorities_request_scope_uq
        UNIQUE (promotion_request_id, inventory_scope_id),
    CONSTRAINT compute_epoch_authorities_sequence_check CHECK (
        authority_sequence > 0 AND proof_generation > 0
        AND ((authority_sequence = 1
              AND previous_authority_id IS NULL
              AND predecessor_epoch_id IS NULL)
             OR (authority_sequence > 1
                 AND previous_authority_id IS NOT NULL
                 AND predecessor_epoch_id IS NOT NULL))
    )
);

CREATE INDEX compute_epoch_authorities_current_idx
    ON compute_metering_epoch_authorities (
        activation_key, inventory_scope_id, authority_sequence DESC
    );
CREATE INDEX compute_epoch_authorities_epoch_idx
    ON compute_metering_epoch_authorities (
        inventory_scope_epoch_id, activation_key, effective_from
    );

CREATE FUNCTION protect_compute_epoch_promotion_request()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'compute epoch promotion requests are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.promoted_at IS DISTINCT FROM statement_timestamp()
       OR NEW.created_at IS DISTINCT FROM statement_timestamp() THEN
        RAISE EXCEPTION 'compute epoch promotion must use the database clock'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER compute_epoch_promotion_requests_immutable
BEFORE INSERT OR UPDATE OR DELETE ON compute_metering_epoch_promotion_requests
FOR EACH ROW EXECUTE FUNCTION protect_compute_epoch_promotion_request();

CREATE FUNCTION protect_compute_metering_epoch_authority()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    activation_state       TEXT;
    activation_boundary    TIMESTAMPTZ;
    request_key            TEXT;
    request_kind           TEXT;
    request_collector      TEXT;
    request_cluster        TEXT;
    request_promoted_at    TIMESTAMPTZ;
    current_generation     BIGINT;
    scope_resource         TEXT;
    scope_namespace        TEXT;
    epoch_recovery_from    UUID;
    epoch_retired_at       TIMESTAMPTZ;
    previous_epoch_id      UUID;
    previous_sequence      BIGINT;
    previous_retired_at    TIMESTAMPTZ;
    snapshot_item_count    BIGINT;
    snapshot_generation    BIGINT;
    snapshot_is_proof      BOOLEAN;
    shadow_count           BIGINT;
    missing_shadow_count   BIGINT;
    orphan_shadow_count    BIGINT;
    lineage_reaches_prior  BOOLEAN;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'compute epoch authorities are append-only'
            USING ERRCODE = '55000';
    END IF;

    SELECT epoch.recovery_from_epoch_id, epoch.retired_at,
           scope.api_resource, scope.namespace
    INTO epoch_recovery_from, epoch_retired_at,
         scope_resource, scope_namespace
    FROM public.resource_inventory_scope_epochs AS epoch
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = epoch.scope_id
     AND scope.collector_id = NEW.collector_id
     AND scope.source_cluster = NEW.source_cluster
    WHERE epoch.id = NEW.inventory_scope_epoch_id
      AND epoch.scope_id = NEW.inventory_scope_id
    FOR SHARE OF epoch;

    SELECT activation.state, activation.activated_at
    INTO activation_state, activation_boundary
    FROM public.compute_metering_activation AS activation
    WHERE activation.activation_key = NEW.activation_key
    FOR SHARE;

    SELECT request.activation_key, request.request_kind,
           request.collector_id, request.source_cluster, request.promoted_at
    INTO request_key, request_kind, request_collector, request_cluster,
         request_promoted_at
    FROM public.compute_metering_epoch_promotion_requests AS request
    WHERE request.id = NEW.promotion_request_id
    FOR SHARE;

    SELECT control.leader_generation
    INTO current_generation
    FROM public.infra_metering_control AS control
    WHERE control.singleton = TRUE
    FOR SHARE;

    IF epoch_retired_at IS NOT NULL
       OR request_key IS DISTINCT FROM NEW.activation_key
       OR request_collector IS DISTINCT FROM NEW.collector_id
       OR request_cluster IS DISTINCT FROM NEW.source_cluster
       OR scope_namespace IS NULL
       OR NOT (
            (NEW.activation_key IN ('agent_pod', 'ide_workspace_pod')
             AND NEW.collector_id = 'kubernetes-pods'
             AND scope_resource = 'core/v1/pods')
            OR (NEW.activation_key = 'workspace_vm'
                AND NEW.collector_id = 'kubevirt-vmis'
                AND scope_resource =
                    'kubevirt.io/v1/virtualmachineinstances')
       ) THEN
        RAISE EXCEPTION 'compute epoch authority identity is invalid'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.authority_sequence = 1 THEN
        IF activation_state IS DISTINCT FROM 'shadow'
           OR request_kind IS DISTINCT FROM 'initial-activation'
           OR epoch_recovery_from IS NOT NULL
           OR NOT EXISTS (
                SELECT 1
                FROM public.compute_metering_scope_requirements AS requirement
                WHERE requirement.activation_key = NEW.activation_key
                  AND requirement.collector_id = NEW.collector_id
                  AND requirement.source_cluster = NEW.source_cluster
                  AND requirement.inventory_scope_id = NEW.inventory_scope_id
                  AND requirement.inventory_scope_epoch_id =
                      NEW.inventory_scope_epoch_id
                  AND requirement.required_from = NEW.effective_from
           ) THEN
            RAISE EXCEPTION 'initial compute epoch authority is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSE
        SELECT prior.inventory_scope_epoch_id, prior.authority_sequence,
               epoch.retired_at
        INTO previous_epoch_id, previous_sequence, previous_retired_at
        FROM public.compute_metering_epoch_authorities AS prior
        JOIN public.resource_inventory_scope_epochs AS epoch
          ON epoch.id = prior.inventory_scope_epoch_id
         AND epoch.scope_id = prior.inventory_scope_id
        WHERE prior.id = NEW.previous_authority_id
          AND prior.activation_key = NEW.activation_key
          AND prior.inventory_scope_id = NEW.inventory_scope_id
        FOR SHARE OF epoch;

        WITH RECURSIVE lineage AS (
            SELECT epoch.id, epoch.scope_id, epoch.recovery_from_epoch_id,
                   ARRAY[epoch.id]::UUID[] AS path, 1 AS depth
            FROM public.resource_inventory_scope_epochs AS epoch
            WHERE epoch.id = NEW.inventory_scope_epoch_id
              AND epoch.scope_id = NEW.inventory_scope_id
            UNION ALL
            SELECT predecessor.id, predecessor.scope_id,
                   predecessor.recovery_from_epoch_id,
                   lineage.path || predecessor.id,
                   lineage.depth + 1
            FROM lineage
            JOIN public.resource_inventory_scope_epochs AS predecessor
              ON predecessor.id = lineage.recovery_from_epoch_id
             AND predecessor.scope_id = NEW.inventory_scope_id
            WHERE lineage.depth < 10000
              AND NOT predecessor.id = ANY(lineage.path)
        )
        SELECT EXISTS (
            SELECT 1 FROM lineage WHERE id = previous_epoch_id
        ) INTO lineage_reaches_prior;

        IF activation_state IS DISTINCT FROM 'active'
           OR activation_boundary IS NULL
           OR statement_timestamp() < activation_boundary
           OR request_kind IS DISTINCT FROM 'recovery-rollover'
           OR previous_sequence IS NULL
           OR NEW.authority_sequence <> previous_sequence + 1
           OR epoch_recovery_from IS DISTINCT FROM NEW.predecessor_epoch_id
           OR previous_retired_at IS NULL
           OR previous_retired_at > NEW.effective_from
           OR lineage_reaches_prior IS DISTINCT FROM TRUE
           OR NEW.effective_from IS DISTINCT FROM request_promoted_at THEN
            RAISE EXCEPTION 'compute recovery epoch lineage is invalid'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    SELECT snapshot.item_count, snapshot.leader_generation,
           snapshot.complete
           AND snapshot.manifest_state = 'sealed'
           AND epoch.last_complete_snapshot_id = snapshot.id
    INTO snapshot_item_count, snapshot_generation, snapshot_is_proof
    FROM public.resource_inventory_snapshots AS snapshot
    JOIN public.resource_inventory_scope_epochs AS epoch
      ON epoch.id = snapshot.scope_epoch_id
     AND epoch.scope_id = snapshot.inventory_scope_id
    WHERE snapshot.id = NEW.proof_snapshot_id
      AND snapshot.scope_epoch_id = NEW.inventory_scope_epoch_id
      AND snapshot.inventory_scope_id = NEW.inventory_scope_id
    FOR SHARE OF epoch;

    SELECT count(*)
    INTO shadow_count
    FROM public.compute_shadow_observations AS observation
    WHERE observation.snapshot_id = NEW.proof_snapshot_id
      AND observation.inventory_scope_id = NEW.inventory_scope_id
      AND observation.activation_key = NEW.activation_key;

    SELECT count(*)
    INTO missing_shadow_count
    FROM public.resource_inventory_snapshot_items AS item
    WHERE item.snapshot_id = NEW.proof_snapshot_id
      AND NOT EXISTS (
          SELECT 1
          FROM public.compute_shadow_observations AS observation
          WHERE observation.snapshot_id = item.snapshot_id
            AND observation.activation_key = NEW.activation_key
            AND observation.source_kind = item.source_kind
            AND observation.source_uid = item.source_uid
      );

    SELECT count(*)
    INTO orphan_shadow_count
    FROM public.compute_shadow_observations AS observation
    WHERE observation.snapshot_id = NEW.proof_snapshot_id
      AND observation.activation_key = NEW.activation_key
      AND NOT EXISTS (
          SELECT 1
          FROM public.resource_inventory_snapshot_items AS item
          WHERE item.snapshot_id = observation.snapshot_id
            AND item.source_kind = observation.source_kind
            AND item.source_uid = observation.source_uid
      );

    IF snapshot_is_proof IS DISTINCT FROM TRUE
       OR snapshot_generation IS DISTINCT FROM NEW.proof_generation
       OR current_generation IS DISTINCT FROM NEW.proof_generation
       OR shadow_count IS DISTINCT FROM snapshot_item_count
       OR missing_shadow_count <> 0
       OR orphan_shadow_count <> 0 THEN
        RAISE EXCEPTION
            'compute epoch authority requires an exact item-for-item proof'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER compute_metering_epoch_authorities_immutable
BEFORE INSERT OR UPDATE OR DELETE ON compute_metering_epoch_authorities
FOR EACH ROW EXECUTE FUNCTION protect_compute_metering_epoch_authority();

ALTER TABLE resource_intervals
    ADD COLUMN compute_scope_epoch_id UUID,
    ADD CONSTRAINT resource_intervals_compute_scope_epoch_fkey
        FOREIGN KEY (compute_scope_epoch_id, inventory_scope_id)
        REFERENCES resource_inventory_scope_epochs (id, scope_id)
        ON DELETE RESTRICT,
    ADD CONSTRAINT resource_intervals_compute_scope_epoch_shape_check CHECK (
        (((source_kind = 'pod' AND category = 'compute'
           AND resource = 'agent_pod')
          OR (source_kind = 'pod' AND category = 'compute'
              AND resource = 'workspace_pod'
              AND details->>'product_class' = 'ide-session')
          OR (source_kind = 'vmi' AND category = 'compute'
              AND resource = 'workspace_vm'))
         AND compute_scope_epoch_id IS NOT NULL)
        OR
        (NOT ((source_kind = 'pod' AND category = 'compute'
               AND resource = 'agent_pod')
              OR (source_kind = 'pod' AND category = 'compute'
                  AND resource = 'workspace_pod'
                  AND details->>'product_class' = 'ide-session')
              OR (source_kind = 'vmi' AND category = 'compute'
                  AND resource = 'workspace_vm'))
         AND compute_scope_epoch_id IS NULL)
    );

CREATE INDEX resource_intervals_compute_scope_epoch_idx
    ON resource_intervals (compute_scope_epoch_id, started_at, id)
    WHERE compute_scope_epoch_id IS NOT NULL;

DROP TRIGGER resource_intervals_compute_exact_epoch_lifecycle_guard
    ON resource_intervals;
DROP FUNCTION enforce_resource_interval_compute_exact_epoch_lifecycle();

CREATE FUNCTION enforce_resource_interval_compute_epoch_authority()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    required_key         TEXT;
    activation_state     TEXT;
    activation_boundary  TIMESTAMPTZ;
    epoch_retired_at     TIMESTAMPTZ;
    authority_boundary   TIMESTAMPTZ;
BEGIN
    IF NEW.source_kind = 'pod'
       AND NEW.category = 'compute'
       AND NEW.resource = 'agent_pod' THEN
        required_key := 'agent_pod';
    ELSIF NEW.source_kind = 'pod'
          AND NEW.category = 'compute'
          AND NEW.resource = 'workspace_pod'
          AND NEW.details->>'product_class' = 'ide-session' THEN
        required_key := 'ide_workspace_pod';
    ELSIF NEW.source_kind = 'vmi'
          AND NEW.category = 'compute'
          AND NEW.resource = 'workspace_vm' THEN
        required_key := 'workspace_vm';
    ELSE
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE'
       AND NEW.compute_scope_epoch_id IS DISTINCT FROM OLD.compute_scope_epoch_id THEN
        RAISE EXCEPTION 'compute interval epoch binding is immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT epoch.retired_at
    INTO epoch_retired_at
    FROM public.resource_inventory_scope_epochs AS epoch
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = epoch.scope_id
    WHERE epoch.id = NEW.compute_scope_epoch_id
      AND epoch.scope_id = NEW.inventory_scope_id
      AND scope.source_cluster = NEW.source_cluster
    FOR SHARE OF epoch;

    SELECT activation.state, activation.activated_at
    INTO activation_state, activation_boundary
    FROM public.compute_metering_activation AS activation
    WHERE activation.activation_key = required_key
    FOR SHARE;

    SELECT authority.effective_from
    INTO authority_boundary
    FROM public.compute_metering_epoch_authorities AS authority
    WHERE authority.activation_key = required_key
      AND authority.inventory_scope_id = NEW.inventory_scope_id
      AND authority.inventory_scope_epoch_id = NEW.compute_scope_epoch_id;

    IF activation_state IS DISTINCT FROM 'active'
       OR activation_boundary IS NULL
       OR statement_timestamp() < activation_boundary
       OR authority_boundary IS NULL
       OR statement_timestamp() < authority_boundary
       OR NEW.started_at < GREATEST(activation_boundary, authority_boundary) THEN
        RAISE EXCEPTION
            'compute product class % lacks bound exact epoch authority',
            required_key
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' AND epoch_retired_at IS NOT NULL THEN
        RAISE EXCEPTION 'compute interval exact epoch is retired'
            USING ERRCODE = '55000';
    END IF;
    IF epoch_retired_at IS NOT NULL
       AND (NEW.last_seen_at > epoch_retired_at
            OR NEW.last_confirmed_at > epoch_retired_at
            OR NEW.materialized_through > epoch_retired_at
            OR (NEW.ended_at IS NOT NULL
                AND NEW.ended_at > epoch_retired_at)) THEN
        RAISE EXCEPTION 'compute interval mutation exceeds epoch retirement'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_intervals_compute_epoch_authority_guard
BEFORE INSERT OR UPDATE ON resource_intervals
FOR EACH ROW EXECUTE FUNCTION enforce_resource_interval_compute_epoch_authority();

-- Retiring an authorized epoch closes every still-open interval at the exact
-- database retirement instant and releases its lifecycle head. This is a
-- coverage fence, not evidence that the workload stopped; a successor begins
-- only after a fresh proof and explicit promotion.
CREATE FUNCTION close_compute_intervals_at_epoch_retirement()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF OLD.retired_at IS NULL AND NEW.retired_at IS NOT NULL THEN
        PERFORM activation.activation_key
        FROM public.compute_metering_epoch_authorities AS authority
        JOIN public.compute_metering_activation AS activation
          ON activation.activation_key = authority.activation_key
        WHERE authority.inventory_scope_epoch_id = OLD.id
        ORDER BY activation.activation_key
        FOR SHARE OF activation;

        IF EXISTS (
            SELECT 1
            FROM public.resource_intervals AS interval
            WHERE interval.compute_scope_epoch_id = OLD.id
              AND interval.ended_at IS NULL
              AND (interval.started_at > NEW.retired_at
                   OR interval.last_seen_at > NEW.retired_at
                   OR interval.last_confirmed_at > NEW.retired_at
                   OR interval.materialized_through > NEW.retired_at)
        ) THEN
            RAISE EXCEPTION
                'compute interval evidence leads epoch retirement clock'
                USING ERRCODE = '55000';
        END IF;

        UPDATE public.resource_intervals AS interval
        SET ended_at = NEW.retired_at,
            end_time_source = 'inventory-epoch-retired',
            end_uncertainty_us = 0,
            end_reason = 'inventory-epoch-retired',
            updated_at = statement_timestamp()
        WHERE interval.compute_scope_epoch_id = OLD.id
          AND interval.ended_at IS NULL;

        UPDATE public.resource_lifecycle_heads AS head
        SET current_interval_id = NULL,
            updated_at = statement_timestamp()
        WHERE head.current_interval_id IN (
            SELECT interval.id
            FROM public.resource_intervals AS interval
            WHERE interval.compute_scope_epoch_id = OLD.id
              AND interval.ended_at = NEW.retired_at
              AND interval.end_reason = 'inventory-epoch-retired'
        );
    ELSIF OLD.retired_at IS NOT NULL
          AND NEW.retired_at IS DISTINCT FROM OLD.retired_at THEN
        RAISE EXCEPTION 'inventory epoch retirement is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_inventory_epochs_compute_retirement
BEFORE UPDATE OF retired_at ON resource_inventory_scope_epochs
FOR EACH ROW EXECUTE FUNCTION close_compute_intervals_at_epoch_retirement();

CREATE FUNCTION protect_inventory_epoch_recovery_identity()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'inventory epochs cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.scope_id IS DISTINCT FROM OLD.scope_id
       OR NEW.epoch_number IS DISTINCT FROM OLD.epoch_number
       OR NEW.coverage_mode IS DISTINCT FROM OLD.coverage_mode
       OR NEW.capture_epoch IS DISTINCT FROM OLD.capture_epoch
       OR NEW.recovery_from_epoch_id IS DISTINCT FROM OLD.recovery_from_epoch_id
       OR NEW.require_after_recovery IS DISTINCT FROM OLD.require_after_recovery
    THEN
        RAISE EXCEPTION 'inventory epoch recovery identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_inventory_epochs_recovery_identity_immutable
BEFORE UPDATE OR DELETE ON resource_inventory_scope_epochs
FOR EACH ROW EXECUTE FUNCTION protect_inventory_epoch_recovery_identity();

-- Initial activation now also requires the append-only sequence-1 authority
-- for every frozen requirement. Direct SQL cannot bypass the audited request
-- ledger and later let an unbound interval through.
CREATE OR REPLACE FUNCTION protect_compute_metering_activation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    requirement_count BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'compute activation rows cannot be deleted'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'disabled' OR NEW.activated_at IS NOT NULL THEN
            RAISE EXCEPTION 'compute activation rows must begin disabled'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.activation_key IS DISTINCT FROM OLD.activation_key
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'compute activation identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'disabled' AND NEW.state = 'shadow'
       AND NEW.activated_at IS NULL THEN
        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF OLD.state = 'shadow' AND NEW.state = 'active'
       AND NEW.activated_at IS NOT NULL
       AND NEW.activated_at = date_trunc('day', NEW.activated_at, 'UTC')
       AND NEW.activated_at > statement_timestamp() THEN
        SELECT count(*)
        INTO requirement_count
        FROM public.compute_metering_scope_requirements AS requirement
        WHERE requirement.activation_key = OLD.activation_key;
        IF requirement_count = 0 OR EXISTS (
            SELECT 1
            FROM public.compute_metering_scope_requirements AS requirement
            LEFT JOIN public.compute_metering_epoch_authorities AS authority
              ON authority.activation_key = requirement.activation_key
             AND authority.inventory_scope_id = requirement.inventory_scope_id
             AND authority.inventory_scope_epoch_id =
                 requirement.inventory_scope_epoch_id
             AND authority.authority_sequence = 1
             AND authority.effective_from = requirement.required_from
            LEFT JOIN public.resource_inventory_scope_epochs AS epoch
              ON epoch.id = requirement.inventory_scope_epoch_id
             AND epoch.scope_id = requirement.inventory_scope_id
            WHERE requirement.activation_key = OLD.activation_key
              AND (requirement.required_from IS DISTINCT FROM NEW.activated_at
                   OR authority.id IS NULL
                   OR epoch.id IS NULL
                   OR epoch.retired_at IS NOT NULL
                   OR NOT epoch.required_for_rollup
                   OR epoch.required_from IS NULL
                   OR epoch.required_from > requirement.required_from)
        ) THEN
            RAISE EXCEPTION
                'compute activation requires audited exact epoch authority'
                USING ERRCODE = '55000';
        END IF;
        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF NEW.state IS NOT DISTINCT FROM OLD.state
       AND NEW.activated_at IS NOT DISTINCT FROM OLD.activated_at
       AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'compute activation permits only disabled -> shadow -> future active'
        USING ERRCODE = '55000';
END;
$$;

COMMENT ON TABLE compute_metering_epoch_promotion_requests IS
    'Immutable fleet-admin idempotency and audit ledger for initial and recovery compute epoch promotion.';
COMMENT ON TABLE compute_metering_epoch_authorities IS
    'Append-only per-class exact-epoch authority; effective end is the bound inventory epoch retired_at and gaps are never inherited.';
COMMENT ON COLUMN resource_intervals.compute_scope_epoch_id IS
    'Exact promoted Slice 3 inventory epoch authorizing this immutable compute interval revision; NULL for every other resource class.';

COMMIT;
