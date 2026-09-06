-- migration:     0105_storage_source_activation.sql
-- description:   Per-source storage activation and exact multi-scope proof.
-- depends-on:    0104_agent_metering_lock_order.sql
-- expected:      < 30s. New tables are small; the bounded backfill scans only
--                active required storage scopes and open storage intervals.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on storage activation,
--                inventory scope/epoch, interval, and shadow tables.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- The original per-basis row remains the global kill switch.  A source must
-- additionally cross its own forward-only boundary; activating one cluster or
-- collector can therefore never activate another one accidentally.
CREATE TABLE storage_metering_source_activations (
    measurement_basis TEXT NOT NULL
        REFERENCES storage_metering_activation(measurement_basis)
        ON DELETE RESTRICT,
    collector_id       TEXT NOT NULL,
    source_cluster     TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'disabled',
    activated_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),

    PRIMARY KEY (measurement_basis, collector_id, source_cluster),
    CONSTRAINT storage_metering_source_activations_identity_check CHECK (
        measurement_basis IN ('claim-requested', 'volume-provisioned')
        AND collector_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
        AND source_cluster ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$'
    ),
    CONSTRAINT storage_metering_source_activations_state_check CHECK (
        (state IN ('disabled', 'shadow') AND activated_at IS NULL)
        OR (state = 'active' AND activated_at IS NOT NULL
            AND activated_at = date_trunc('day', activated_at, 'UTC'))
    )
);

-- Freeze the legacy authority and its requirement heads before deriving the
-- backfill. The short lock timeout makes deployment retry instead of silently
-- taking a mixed before/after view behind an in-flight activation or recovery.
LOCK TABLE storage_metering_activation,
           resource_inventory_scopes,
           resource_inventory_scope_epochs
    IN SHARE ROW EXCLUSIVE MODE;

-- Repeat the stable source identity in the FK so a referenced scope cannot be
-- moved to another collector/cluster after it becomes an activation input.
ALTER TABLE resource_inventory_scopes
    ADD CONSTRAINT resource_inventory_scopes_source_identity_uq
    UNIQUE (id, collector_id, source_cluster);

-- This is an exact, immutable set.  Requirements may be inserted only while
-- their source row is disabled; entering shadow freezes the set forever.
-- Quantity scopes authorize interval creation. Attribution scopes are equally
-- required for completeness but can never authorize a quantity interval.
CREATE TABLE storage_metering_source_requirements (
    measurement_basis  TEXT NOT NULL,
    collector_id        TEXT NOT NULL,
    source_cluster      TEXT NOT NULL,
    inventory_scope_id  UUID NOT NULL,
    requirement_role    TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),

    PRIMARY KEY (
        measurement_basis, collector_id, source_cluster,
        inventory_scope_id, requirement_role
    ),
    CONSTRAINT storage_metering_source_requirements_activation_fkey
        FOREIGN KEY (measurement_basis, collector_id, source_cluster)
        REFERENCES storage_metering_source_activations (
            measurement_basis, collector_id, source_cluster
        ) ON DELETE RESTRICT,
    CONSTRAINT storage_metering_source_requirements_scope_fkey
        FOREIGN KEY (inventory_scope_id, collector_id, source_cluster)
        REFERENCES resource_inventory_scopes (
            id, collector_id, source_cluster
        ) ON DELETE RESTRICT,
    CONSTRAINT storage_metering_source_requirements_role_check CHECK (
        requirement_role IN ('quantity', 'attribution')
    )
);

CREATE INDEX storage_metering_source_requirements_scope_idx
    ON storage_metering_source_requirements (
        inventory_scope_id, measurement_basis, requirement_role
    );

-- Backfill only the legacy primary authority. The old activation helper could
-- promote only kubernetes-pods scopes, so treating any other collector as
-- implicitly active would recreate the cross-source inheritance defect.
INSERT INTO storage_metering_source_activations (
    measurement_basis, collector_id, source_cluster,
    state, activated_at
)
SELECT
    activation.measurement_basis,
    scope.collector_id,
    scope.source_cluster,
    'active',
    GREATEST(activation.activated_at, max(epoch.required_from))
FROM storage_metering_activation AS activation
JOIN resource_inventory_scopes AS scope
  ON scope.collector_id = 'kubernetes-pods'
 AND scope.api_resource = 'core/v1/persistentvolumeclaims'
JOIN resource_inventory_scope_epochs AS epoch
  ON epoch.scope_id = scope.id
 AND epoch.retired_at IS NULL
 AND epoch.required_for_rollup
WHERE activation.measurement_basis = 'claim-requested'
  AND activation.state = 'active'
GROUP BY activation.measurement_basis, activation.activated_at,
         scope.collector_id, scope.source_cluster;

INSERT INTO storage_metering_source_requirements (
    measurement_basis, collector_id, source_cluster,
    inventory_scope_id, requirement_role
)
SELECT
    source_activation.measurement_basis,
    source_activation.collector_id,
    source_activation.source_cluster,
    scope.id,
    'quantity'
FROM storage_metering_source_activations AS source_activation
JOIN resource_inventory_scopes AS scope
  ON scope.collector_id = source_activation.collector_id
 AND scope.source_cluster = source_activation.source_cluster
 AND scope.api_resource = 'core/v1/persistentvolumeclaims'
JOIN resource_inventory_scope_epochs AS epoch
  ON epoch.scope_id = scope.id
 AND epoch.retired_at IS NULL
 AND epoch.required_for_rollup
WHERE source_activation.measurement_basis = 'claim-requested';

-- A physical-volume source is backfilled only when the same collector/cluster
-- already has a claim source. Its boundary cannot precede either the global PV
-- boundary, the required PV coverage, or the matching claim source boundary.
INSERT INTO storage_metering_source_activations (
    measurement_basis, collector_id, source_cluster,
    state, activated_at
)
SELECT
    activation.measurement_basis,
    scope.collector_id,
    scope.source_cluster,
    'active',
    GREATEST(
        activation.activated_at,
        claim_activation.activated_at,
        max(epoch.required_from)
    )
FROM storage_metering_activation AS activation
JOIN resource_inventory_scopes AS scope
  ON scope.collector_id = 'kubernetes-pods'
 AND scope.api_resource = 'core/v1/persistentvolumes'
JOIN resource_inventory_scope_epochs AS epoch
  ON epoch.scope_id = scope.id
 AND epoch.retired_at IS NULL
 AND epoch.required_for_rollup
JOIN storage_metering_source_activations AS claim_activation
  ON claim_activation.measurement_basis = 'claim-requested'
 AND claim_activation.collector_id = scope.collector_id
 AND claim_activation.source_cluster = scope.source_cluster
 AND claim_activation.state = 'active'
WHERE activation.measurement_basis = 'volume-provisioned'
  AND activation.state = 'active'
GROUP BY activation.measurement_basis, activation.activated_at,
         scope.collector_id, scope.source_cluster,
         claim_activation.activated_at;

INSERT INTO storage_metering_source_requirements (
    measurement_basis, collector_id, source_cluster,
    inventory_scope_id, requirement_role
)
SELECT
    source_activation.measurement_basis,
    source_activation.collector_id,
    source_activation.source_cluster,
    scope.id,
    'quantity'
FROM storage_metering_source_activations AS source_activation
JOIN resource_inventory_scopes AS scope
  ON scope.collector_id = source_activation.collector_id
 AND scope.source_cluster = source_activation.source_cluster
 AND scope.api_resource = 'core/v1/persistentvolumes'
JOIN resource_inventory_scope_epochs AS epoch
  ON epoch.scope_id = scope.id
 AND epoch.retired_at IS NULL
 AND epoch.required_for_rollup
WHERE source_activation.measurement_basis = 'volume-provisioned';

INSERT INTO storage_metering_source_requirements (
    measurement_basis, collector_id, source_cluster,
    inventory_scope_id, requirement_role
)
SELECT
    volume_activation.measurement_basis,
    volume_activation.collector_id,
    volume_activation.source_cluster,
    claim_requirement.inventory_scope_id,
    'attribution'
FROM storage_metering_source_activations AS volume_activation
JOIN storage_metering_source_requirements AS claim_requirement
  ON claim_requirement.measurement_basis = 'claim-requested'
 AND claim_requirement.collector_id = volume_activation.collector_id
 AND claim_requirement.source_cluster = volume_activation.source_cluster
 AND claim_requirement.requirement_role = 'quantity'
WHERE volume_activation.measurement_basis = 'volume-provisioned';

CREATE FUNCTION protect_storage_metering_source_requirement()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    activation_state TEXT;
    api_resource     TEXT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'storage source requirements are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT activation.state, scope.api_resource
    INTO activation_state, api_resource
    FROM public.storage_metering_source_activations AS activation
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = NEW.inventory_scope_id
     AND scope.collector_id = activation.collector_id
     AND scope.source_cluster = activation.source_cluster
    WHERE activation.measurement_basis = NEW.measurement_basis
      AND activation.collector_id = NEW.collector_id
      AND activation.source_cluster = NEW.source_cluster
    FOR SHARE OF activation;

    IF activation_state IS DISTINCT FROM 'disabled' THEN
        RAISE EXCEPTION
            'storage source requirements can be added only while disabled'
            USING ERRCODE = '55000';
    END IF;

    IF NOT (
        (NEW.measurement_basis = 'claim-requested'
            AND NEW.requirement_role = 'quantity'
            AND api_resource = 'core/v1/persistentvolumeclaims')
        OR (NEW.measurement_basis = 'volume-provisioned'
            AND NEW.requirement_role = 'quantity'
            AND api_resource = 'core/v1/persistentvolumes')
        OR (NEW.measurement_basis = 'volume-provisioned'
            AND NEW.requirement_role = 'attribution'
            AND api_resource = 'core/v1/persistentvolumeclaims')
    ) THEN
        RAISE EXCEPTION 'storage source requirement role/resource mismatch'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER storage_metering_source_requirements_immutable
BEFORE INSERT OR UPDATE OR DELETE ON storage_metering_source_requirements
FOR EACH ROW EXECUTE FUNCTION protect_storage_metering_source_requirement();

CREATE FUNCTION protect_storage_metering_source_activation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    quantity_count     BIGINT;
    attribution_count  BIGINT;
    global_state       TEXT;
    global_boundary    TIMESTAMPTZ;
    claim_state        TEXT;
    claim_boundary     TIMESTAMPTZ;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'storage source activation rows cannot be deleted'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'disabled' OR NEW.activated_at IS NOT NULL THEN
            RAISE EXCEPTION 'storage source activation rows must begin disabled'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.measurement_basis IS DISTINCT FROM OLD.measurement_basis
       OR NEW.collector_id IS DISTINCT FROM OLD.collector_id
       OR NEW.source_cluster IS DISTINCT FROM OLD.source_cluster
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'storage source activation identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'disabled' AND NEW.state = 'shadow'
       AND NEW.activated_at IS NULL THEN
        SELECT
            count(*) FILTER (WHERE requirement_role = 'quantity'),
            count(*) FILTER (WHERE requirement_role = 'attribution')
        INTO quantity_count, attribution_count
        FROM public.storage_metering_source_requirements AS requirement
        WHERE requirement.measurement_basis = OLD.measurement_basis
          AND requirement.collector_id = OLD.collector_id
          AND requirement.source_cluster = OLD.source_cluster;

        IF quantity_count = 0
           OR (OLD.measurement_basis = 'claim-requested'
               AND attribution_count <> 0)
           OR (OLD.measurement_basis = 'volume-provisioned'
               AND attribution_count = 0) THEN
            RAISE EXCEPTION
                'storage source activation requires an exact scope set'
                USING ERRCODE = '55000';
        END IF;
        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF OLD.state = 'shadow' AND NEW.state = 'active'
       AND NEW.activated_at IS NOT NULL
       AND NEW.activated_at = date_trunc('day', NEW.activated_at, 'UTC')
       AND NEW.activated_at > statement_timestamp() THEN
        SELECT activation.state, activation.activated_at
        INTO global_state, global_boundary
        FROM public.storage_metering_activation AS activation
        WHERE activation.measurement_basis = OLD.measurement_basis
        FOR SHARE;
        IF global_state IS DISTINCT FROM 'active'
           OR global_boundary IS NULL
           OR global_boundary > NEW.activated_at THEN
            RAISE EXCEPTION
                'global storage basis must have an equal or earlier activation boundary'
                USING ERRCODE = '55000';
        END IF;

        IF OLD.measurement_basis = 'volume-provisioned' THEN
            SELECT activation.state, activation.activated_at
            INTO claim_state, claim_boundary
            FROM public.storage_metering_source_activations AS activation
            WHERE activation.measurement_basis = 'claim-requested'
              AND activation.collector_id = OLD.collector_id
              AND activation.source_cluster = OLD.source_cluster
            FOR SHARE;
            IF claim_state IS DISTINCT FROM 'active'
               OR claim_boundary IS NULL
               OR claim_boundary > NEW.activated_at THEN
                RAISE EXCEPTION
                    'matching claim source must activate before volume source'
                    USING ERRCODE = '55000';
            END IF;

            IF EXISTS (
                (SELECT requirement.inventory_scope_id
                 FROM public.storage_metering_source_requirements AS requirement
                 WHERE requirement.measurement_basis = 'volume-provisioned'
                   AND requirement.collector_id = OLD.collector_id
                   AND requirement.source_cluster = OLD.source_cluster
                   AND requirement.requirement_role = 'attribution')
                EXCEPT
                (SELECT requirement.inventory_scope_id
                 FROM public.storage_metering_source_requirements AS requirement
                 WHERE requirement.measurement_basis = 'claim-requested'
                   AND requirement.collector_id = OLD.collector_id
                   AND requirement.source_cluster = OLD.source_cluster
                   AND requirement.requirement_role = 'quantity')
            ) OR EXISTS (
                (SELECT requirement.inventory_scope_id
                 FROM public.storage_metering_source_requirements AS requirement
                 WHERE requirement.measurement_basis = 'claim-requested'
                   AND requirement.collector_id = OLD.collector_id
                   AND requirement.source_cluster = OLD.source_cluster
                   AND requirement.requirement_role = 'quantity')
                EXCEPT
                (SELECT requirement.inventory_scope_id
                 FROM public.storage_metering_source_requirements AS requirement
                 WHERE requirement.measurement_basis = 'volume-provisioned'
                   AND requirement.collector_id = OLD.collector_id
                   AND requirement.source_cluster = OLD.source_cluster
                   AND requirement.requirement_role = 'attribution')
            ) THEN
                RAISE EXCEPTION
                    'volume attribution requirements must exactly match claim quantity requirements'
                    USING ERRCODE = '55000';
            END IF;
        END IF;

        -- The runtime proves freshness and item identity before promotion.
        -- This trigger independently requires that every frozen input was
        -- promoted in the same transaction (or at an earlier boundary).
        PERFORM epoch.id
        FROM public.storage_metering_source_requirements AS requirement
        JOIN public.resource_inventory_scope_epochs AS epoch
          ON epoch.scope_id = requirement.inventory_scope_id
         AND epoch.retired_at IS NULL
        WHERE requirement.measurement_basis = OLD.measurement_basis
          AND requirement.collector_id = OLD.collector_id
          AND requirement.source_cluster = OLD.source_cluster
        ORDER BY requirement.requirement_role,
                 requirement.inventory_scope_id
        FOR SHARE OF epoch;

        IF EXISTS (
            SELECT 1
            FROM public.storage_metering_source_requirements AS requirement
            LEFT JOIN public.resource_inventory_scope_epochs AS epoch
              ON epoch.scope_id = requirement.inventory_scope_id
             AND epoch.retired_at IS NULL
            WHERE requirement.measurement_basis = OLD.measurement_basis
              AND requirement.collector_id = OLD.collector_id
              AND requirement.source_cluster = OLD.source_cluster
              AND (epoch.id IS NULL
                   OR NOT epoch.required_for_rollup
                   OR epoch.required_from IS NULL
                   OR epoch.required_from > NEW.activated_at)
        ) THEN
            RAISE EXCEPTION
                'storage source activation requires every exact scope to be promoted'
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
        'storage source activation permits only disabled -> shadow -> future active'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER storage_metering_source_activations_one_way
BEFORE INSERT OR UPDATE OR DELETE ON storage_metering_source_activations
FOR EACH ROW EXECUTE FUNCTION protect_storage_metering_source_activation();

-- Fence guard replacement with writers so no transaction crosses the commit
-- using a mixture of the legacy global-only and new per-source predicates.
LOCK TABLE resource_intervals, storage_shadow_observations
    IN SHARE ROW EXCLUSIVE MODE;

CREATE OR REPLACE FUNCTION enforce_resource_interval_storage_activation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    global_state     TEXT;
    global_boundary  TIMESTAMPTZ;
    source_state     TEXT;
    source_boundary  TIMESTAMPTZ;
    effective_boundary TIMESTAMPTZ;
BEGIN
    IF NEW.measurement_basis NOT IN (
        'claim-requested', 'volume-provisioned'
    ) THEN
        RETURN NEW;
    END IF;

    SELECT global_activation.state, global_activation.activated_at,
           source_activation.state, source_activation.activated_at
    INTO global_state, global_boundary, source_state, source_boundary
    FROM public.resource_inventory_scopes AS scope
    JOIN public.storage_metering_source_requirements AS requirement
      ON requirement.inventory_scope_id = scope.id
     AND requirement.measurement_basis = NEW.measurement_basis
     AND requirement.collector_id = scope.collector_id
     AND requirement.source_cluster = scope.source_cluster
     AND requirement.requirement_role = 'quantity'
    JOIN public.storage_metering_source_activations AS source_activation
      ON source_activation.measurement_basis = requirement.measurement_basis
     AND source_activation.collector_id = requirement.collector_id
     AND source_activation.source_cluster = requirement.source_cluster
    JOIN public.storage_metering_activation AS global_activation
      ON global_activation.measurement_basis = requirement.measurement_basis
    WHERE scope.id = NEW.inventory_scope_id
    FOR SHARE OF source_activation, global_activation;

    effective_boundary := GREATEST(global_boundary, source_boundary);
    IF global_state IS DISTINCT FROM 'active'
       OR source_state IS DISTINCT FROM 'active'
       OR effective_boundary IS NULL
       OR statement_timestamp() < effective_boundary
       OR NEW.started_at < effective_boundary THEN
        RAISE EXCEPTION
            'storage interval source is not active at its clamped boundary'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION protect_storage_shadow_observation_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    snapshot_state TEXT;
    global_state   TEXT;
    source_state   TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT snapshot.manifest_state INTO snapshot_state
        FROM public.resource_inventory_snapshots AS snapshot
        WHERE snapshot.id = OLD.snapshot_id
          AND snapshot.inventory_scope_id = OLD.inventory_scope_id
        FOR SHARE;
        IF snapshot_state IN ('items-expired', 'staging-expired')
           AND OLD.observed_at <= statement_timestamp() - INTERVAL '7 days'
           AND OLD.created_at <= statement_timestamp() - INTERVAL '7 days' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'storage shadow deletion requires an expired manifest and seven-day floor'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'storage shadow observations are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT snapshot.manifest_state,
           global_activation.state, source_activation.state
    INTO snapshot_state, global_state, source_state
    FROM public.resource_inventory_snapshots AS snapshot
    JOIN public.resource_inventory_ingest_tickets AS ticket
      ON ticket.id = snapshot.ingest_ticket_id
    JOIN public.infra_metering_control AS control ON control.singleton = TRUE
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = snapshot.inventory_scope_id
    JOIN public.storage_metering_source_requirements AS requirement
      ON requirement.inventory_scope_id = scope.id
     AND requirement.measurement_basis = NEW.measurement_basis
     AND requirement.collector_id = scope.collector_id
     AND requirement.source_cluster = scope.source_cluster
     AND requirement.requirement_role = 'quantity'
    JOIN public.storage_metering_source_activations AS source_activation
      ON source_activation.measurement_basis = requirement.measurement_basis
     AND source_activation.collector_id = requirement.collector_id
     AND source_activation.source_cluster = requirement.source_cluster
    JOIN public.storage_metering_activation AS global_activation
      ON global_activation.measurement_basis = requirement.measurement_basis
    WHERE snapshot.id = NEW.snapshot_id
      AND snapshot.inventory_scope_id = NEW.inventory_scope_id
      AND snapshot.leader_generation = control.leader_generation
      AND ticket.bound_snapshot_id = snapshot.id
      AND ticket.consumed_at IS NULL
      AND ticket.expires_at > statement_timestamp()
    FOR SHARE OF source_activation, global_activation;

    IF snapshot_state IS DISTINCT FROM 'staging'
       OR global_state NOT IN ('shadow', 'active')
       OR source_state NOT IN ('shadow', 'active') THEN
        RAISE EXCEPTION 'storage shadow observation source fence failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

-- Abort rather than strand a pre-existing live lifecycle behind the stricter
-- guard. A valid legacy primary lifecycle was backfilled above; any remaining
-- row needs explicit operator reconciliation instead of inferred authority.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.resource_intervals AS interval
        JOIN public.resource_inventory_scopes AS scope
          ON scope.id = interval.inventory_scope_id
        JOIN public.storage_metering_activation AS global_activation
          ON global_activation.measurement_basis = interval.measurement_basis
        LEFT JOIN public.storage_metering_source_requirements AS requirement
          ON requirement.inventory_scope_id = interval.inventory_scope_id
         AND requirement.measurement_basis = interval.measurement_basis
         AND requirement.collector_id = scope.collector_id
         AND requirement.source_cluster = scope.source_cluster
         AND requirement.requirement_role = 'quantity'
        LEFT JOIN public.storage_metering_source_activations AS source_activation
          ON source_activation.measurement_basis = requirement.measurement_basis
         AND source_activation.collector_id = requirement.collector_id
         AND source_activation.source_cluster = requirement.source_cluster
        WHERE interval.measurement_basis IN (
            'claim-requested', 'volume-provisioned'
        )
          AND interval.ended_at IS NULL
          AND (
              global_activation.state <> 'active'
              OR requirement.inventory_scope_id IS NULL
              OR source_activation.state <> 'active'
              OR interval.started_at < GREATEST(
                  global_activation.activated_at,
                  source_activation.activated_at
              )
          )
    ) THEN
        RAISE EXCEPTION
            'open legacy storage interval lacks a safe source activation backfill'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

COMMENT ON TABLE storage_metering_source_activations IS
    'Forward-only storage boundary for one measurement basis, collector, and source cluster; the per-basis activation remains the global master.';
COMMENT ON TABLE storage_metering_source_requirements IS
    'Immutable exact quantity/attribution inventory-scope set frozen when a storage source enters shadow.';

COMMIT;
