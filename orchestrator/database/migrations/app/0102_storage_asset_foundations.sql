-- migration:     0102_storage_asset_foundations.sql
-- description:   Slice 2 storage activation, opaque physical-volume identity,
--                retained-backend evidence, and non-publishable shadow data.
-- depends-on:    0101_usage_rates_v2_referenced_range_guard.sql
-- expected:      < 30s. New tables are empty; the resource_intervals trigger
--                takes a brief SHARE ROW EXCLUSIVE lock while publication is
--                independently gated.
-- locks:         Brief ACCESS EXCLUSIVE lock on resource_intervals for one
--                INSERT trigger. No audit-database or publication changes.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Storage sources have their own forward-only activation boundary because the
-- workspace cutover may already be active when storage inventory is deployed.
-- Merely collecting or classifying shadow observations can never make an old
-- observation publishable. Activation is scheduled at a future UTC midnight;
-- immutable shadow diagnostics may continue afterward for reconciliation.
CREATE TABLE storage_metering_activation (
    measurement_basis TEXT PRIMARY KEY,
    state             TEXT NOT NULL DEFAULT 'disabled',
    activated_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT storage_metering_activation_basis_check CHECK (
        measurement_basis IN ('claim-requested', 'volume-provisioned')
    ),
    CONSTRAINT storage_metering_activation_state_check CHECK (
        (state IN ('disabled', 'shadow') AND activated_at IS NULL)
        OR (state = 'active' AND activated_at IS NOT NULL
            AND activated_at = date_trunc('day', activated_at, 'UTC'))
    )
);

INSERT INTO storage_metering_activation (measurement_basis)
VALUES ('claim-requested'), ('volume-provisioned');

CREATE FUNCTION protect_storage_metering_activation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'storage activation rows cannot be deleted'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'disabled' OR NEW.activated_at IS NOT NULL THEN
            RAISE EXCEPTION 'storage activation rows must begin disabled'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.measurement_basis IS DISTINCT FROM OLD.measurement_basis
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'storage activation identity is immutable'
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
        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF NEW.state IS NOT DISTINCT FROM OLD.state
       AND NEW.activated_at IS NOT DISTINCT FROM OLD.activated_at
       AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'storage activation permits only disabled -> shadow -> future active'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER storage_metering_activation_one_way
BEFORE INSERT OR UPDATE OR DELETE ON storage_metering_activation
FOR EACH ROW EXECUTE FUNCTION protect_storage_metering_activation();

-- Storage tier classification is a trusted operator-owned registry, never a
-- collector label or raw StorageClass parameter. Exact nullable selectors are
-- ordinary values rather than wildcards. Rules are append-only so rolling
-- orchestrator replicas resolve the same observed PV identically.
CREATE TABLE infrastructure_storage_resource_mappings (
    source_cluster      TEXT NOT NULL,
    storage_class_name  TEXT,
    csi_driver          TEXT,
    volume_mode         TEXT NOT NULL,
    resource            TEXT NOT NULL,
    mapping_version     TEXT NOT NULL,
    rule_fingerprint    CHAR(64) PRIMARY KEY,
    registered_at       TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),

    CONSTRAINT infrastructure_storage_resource_mappings_selector_uq
        UNIQUE NULLS NOT DISTINCT (
            source_cluster, storage_class_name, csi_driver, volume_mode
        ),
    CONSTRAINT infrastructure_storage_resource_mappings_selector_check CHECK (
        source_cluster ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$'
        AND (storage_class_name IS NULL OR (
            storage_class_name ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$'
            AND length(storage_class_name) <= 253
            AND storage_class_name NOT IN ('unknown', 'unmapped', 'any')
        ))
        AND (csi_driver IS NULL OR (
            csi_driver ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$'
            AND length(csi_driver) <= 253
            AND csi_driver NOT IN ('unknown', 'unmapped', 'any')
        ))
        AND volume_mode IN ('filesystem', 'block')
    ),
    CONSTRAINT infrastructure_storage_resource_mappings_output_check CHECK (
        resource ~ '^block_volume_[a-z0-9_]+$'
        AND length(resource) <= 128
        AND mapping_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'
        AND rule_fingerprint ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX infrastructure_storage_resource_mappings_resource_idx
    ON infrastructure_storage_resource_mappings (resource, source_cluster);

CREATE FUNCTION protect_infrastructure_storage_resource_mapping()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'storage resource mappings are append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER infrastructure_storage_resource_mappings_append_only
BEFORE UPDATE OR DELETE ON infrastructure_storage_resource_mappings
FOR EACH ROW EXECUTE FUNCTION protect_infrastructure_storage_resource_mapping();

-- This is the database backstop against shadow-history back-billing. A storage
-- reconciler may create its first interval only after the scheduled boundary,
-- and it must clamp any older Kubernetes timestamp to that boundary.
CREATE FUNCTION enforce_resource_interval_storage_activation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    activation_state TEXT;
    activation_time  TIMESTAMPTZ;
BEGIN
    IF NEW.measurement_basis NOT IN (
        'claim-requested', 'volume-provisioned'
    ) THEN
        RETURN NEW;
    END IF;

    SELECT activation.state, activation.activated_at
    INTO activation_state, activation_time
    FROM public.storage_metering_activation AS activation
    WHERE activation.measurement_basis = NEW.measurement_basis
    FOR SHARE;

    IF activation_state IS DISTINCT FROM 'active'
       OR activation_time IS NULL
       OR statement_timestamp() < activation_time
       OR NEW.started_at < activation_time THEN
        RAISE EXCEPTION
            'storage interval basis % is not active at its clamped boundary',
            NEW.measurement_basis
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_intervals_storage_activation_guard
BEFORE INSERT ON resource_intervals
FOR EACH ROW EXECUTE FUNCTION enforce_resource_interval_storage_activation();

-- Only the fingerprint/version of the dedicated HMAC key is persisted. This
-- v1 singleton deliberately rejects silent key rotation: changing it would
-- make the same CSI backend appear to be a second billable asset.
CREATE TABLE storage_identity_key_state (
    singleton       BOOLEAN PRIMARY KEY DEFAULT TRUE,
    key_version     TEXT NOT NULL UNIQUE,
    key_fingerprint TEXT NOT NULL UNIQUE,
    algorithm       TEXT NOT NULL DEFAULT 'hmac-sha256-v1',
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT storage_identity_key_state_singleton_check CHECK (singleton),
    CONSTRAINT storage_identity_key_state_shape_check CHECK (
        key_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'
        AND key_fingerprint ~ '^[0-9a-f]{64}$'
        AND algorithm = 'hmac-sha256-v1'
    )
);

CREATE FUNCTION protect_storage_identity_key_state()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'storage identity key state is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER storage_identity_key_state_immutable
BEFORE UPDATE OR DELETE ON storage_identity_key_state
FOR EACH ROW EXECUTE FUNCTION protect_storage_identity_key_state();

CREATE TABLE storage_volume_assets (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_cluster           TEXT NOT NULL,
    asset_digest             TEXT NOT NULL,
    identity_key_version     TEXT NOT NULL
        REFERENCES storage_identity_key_state(key_version) ON DELETE RESTRICT,
    identity_scheme          TEXT NOT NULL,
    csi_driver               TEXT,
    source_lifecycle_id      UUID NOT NULL UNIQUE
        REFERENCES resource_lifecycle_heads(source_lifecycle_id)
        ON DELETE RESTRICT,
    lifecycle_state          TEXT NOT NULL DEFAULT 'visible',
    first_observed_at        TIMESTAMPTZ NOT NULL,
    last_observed_at         TIMESTAMPTZ NOT NULL,
    backend_unverified_at    TIMESTAMPTZ,
    destroyed_at             TIMESTAMPTZ,
    destruction_assertion_id UUID UNIQUE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT storage_volume_assets_identity_uq
        UNIQUE (source_cluster, asset_digest),
    CONSTRAINT storage_volume_assets_identity_check CHECK (
        source_cluster <> '' AND length(source_cluster) <= 255
        AND asset_digest ~ '^[0-9a-f]{64}$'
        AND ((identity_scheme = 'csi-hmac-sha256-v1'
                AND csi_driver ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$')
             OR (identity_scheme = 'pv-uid-v1' AND csi_driver IS NULL))
    ),
    CONSTRAINT storage_volume_assets_time_check CHECK (
        last_observed_at >= first_observed_at
        AND (backend_unverified_at IS NULL
             OR backend_unverified_at >= first_observed_at)
        AND (destroyed_at IS NULL OR destroyed_at >= first_observed_at)
    ),
    CONSTRAINT storage_volume_assets_state_check CHECK (
        (lifecycle_state = 'visible'
            AND backend_unverified_at IS NULL
            AND destroyed_at IS NULL
            AND destruction_assertion_id IS NULL)
        OR (lifecycle_state = 'backend-unverified'
            AND backend_unverified_at IS NOT NULL
            AND destroyed_at IS NULL
            AND destruction_assertion_id IS NULL)
        OR (lifecycle_state = 'destroyed'
            AND destroyed_at IS NOT NULL
            AND destruction_assertion_id IS NOT NULL)
    )
);

CREATE INDEX storage_volume_assets_state_idx
    ON storage_volume_assets (lifecycle_state, source_cluster, id);

CREATE TABLE storage_volume_incarnations (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    asset_id                 UUID NOT NULL
        REFERENCES storage_volume_assets(id) ON DELETE RESTRICT,
    inventory_scope_id       UUID NOT NULL,
    source_cluster           TEXT NOT NULL,
    pv_uid                   TEXT NOT NULL,
    pv_name                  TEXT NOT NULL,
    storage_class_name       TEXT,
    reclaim_policy           TEXT NOT NULL,
    backend_deletion_finalizer_observed BOOLEAN NOT NULL DEFAULT FALSE,
    volume_mode              TEXT NOT NULL,
    capacity_bytes           BIGINT NOT NULL,
    bound_claim_uid          TEXT,
    source_resource_version  TEXT,
    first_observed_at        TIMESTAMPTZ NOT NULL,
    last_observed_at         TIMESTAMPTZ NOT NULL,
    detached_at              TIMESTAMPTZ,
    detach_reason            TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT storage_volume_incarnations_scope_cluster_fkey
        FOREIGN KEY (inventory_scope_id, source_cluster)
        REFERENCES resource_inventory_scopes(id, source_cluster)
        ON DELETE RESTRICT,
    CONSTRAINT storage_volume_incarnations_pv_uid_uq
        UNIQUE (source_cluster, pv_uid),
    CONSTRAINT storage_volume_incarnations_identity_check CHECK (
        source_cluster <> '' AND pv_uid <> '' AND pv_name <> ''
        AND length(pv_uid) <= 256 AND length(pv_name) <= 253
        AND (storage_class_name IS NULL
             OR (storage_class_name <> '' AND length(storage_class_name) <= 253))
        AND (bound_claim_uid IS NULL
             OR (bound_claim_uid <> '' AND length(bound_claim_uid) <= 256))
        AND (source_resource_version IS NULL
             OR (source_resource_version <> ''
                 AND length(source_resource_version) <= 255))
    ),
    CONSTRAINT storage_volume_incarnations_shape_check CHECK (
        reclaim_policy IN ('delete', 'retain', 'recycle', 'unknown')
        AND volume_mode IN ('filesystem', 'block', 'unknown')
        AND capacity_bytes >= 0
        AND last_observed_at >= first_observed_at
        AND ((detached_at IS NULL AND detach_reason IS NULL)
             OR (detached_at IS NOT NULL AND detach_reason IN (
                    'pv-deleted', 'reimported', 'backend-destroyed'
                ) AND detached_at >= last_observed_at))
    ),
    CONSTRAINT storage_volume_incarnations_no_overlap EXCLUDE USING gist (
        asset_id WITH =,
        tstzrange(first_observed_at, detached_at, '[)') WITH &&
    )
);

CREATE UNIQUE INDEX storage_volume_incarnations_active_asset_uq
    ON storage_volume_incarnations (asset_id)
    WHERE detached_at IS NULL;

CREATE INDEX storage_volume_incarnations_asset_time_idx
    ON storage_volume_incarnations (
        asset_id, first_observed_at, detached_at, id
    );

CREATE TABLE storage_asset_coverage_gaps (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    asset_id                UUID NOT NULL
        REFERENCES storage_volume_assets(id) ON DELETE RESTRICT,
    scope_epoch_id          UUID NOT NULL
        REFERENCES resource_inventory_scope_epochs(id) ON DELETE RESTRICT,
    gap_start               TIMESTAMPTZ NOT NULL,
    gap_end                 TIMESTAMPTZ,
    reason_code             TEXT NOT NULL,
    resolution              TEXT NOT NULL DEFAULT 'unresolved',
    resolution_assertion_id UUID,
    resolved_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT storage_asset_coverage_gaps_reason_check CHECK (
        reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
    ),
    CONSTRAINT storage_asset_coverage_gaps_range_check CHECK (
        gap_end IS NULL OR gap_end >= gap_start
    ),
    CONSTRAINT storage_asset_coverage_gaps_resolution_check CHECK (
        (resolution = 'unresolved' AND gap_end IS NULL
            AND resolution_assertion_id IS NULL AND resolved_at IS NULL)
        OR (resolution = 'reobserved' AND gap_end IS NOT NULL
            AND resolution_assertion_id IS NULL AND resolved_at IS NOT NULL)
        OR (resolution = 'destroyed-confirmed' AND gap_end IS NOT NULL
            AND resolution_assertion_id IS NOT NULL AND resolved_at IS NOT NULL)
    ),
    CONSTRAINT storage_asset_coverage_gaps_no_overlap EXCLUDE USING gist (
        asset_id WITH =,
        tstzrange(gap_start, gap_end, '[)') WITH &&
    )
);

CREATE UNIQUE INDEX storage_asset_coverage_gaps_open_uq
    ON storage_asset_coverage_gaps (asset_id)
    WHERE resolution = 'unresolved';

CREATE INDEX storage_asset_coverage_gaps_range_idx
    ON storage_asset_coverage_gaps (asset_id, gap_start, gap_end, id);

CREATE TABLE storage_backend_assertions (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    idempotency_key   UUID NOT NULL,
    asset_id          UUID NOT NULL
        REFERENCES storage_volume_assets(id) ON DELETE RESTRICT,
    assertion_kind    TEXT NOT NULL DEFAULT 'backend-destroyed',
    request_hash      TEXT NOT NULL,
    effective_at      TIMESTAMPTZ NOT NULL,
    evidence_kind     TEXT NOT NULL,
    evidence_digest   TEXT NOT NULL,
    actor_kind        TEXT NOT NULL,
    actor_id          UUID,
    reason_code       TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT storage_backend_assertions_idempotency_uq
        UNIQUE (idempotency_key),
    CONSTRAINT storage_backend_assertions_asset_uq UNIQUE (asset_id),
    CONSTRAINT storage_backend_assertions_shape_check CHECK (
        assertion_kind = 'backend-destroyed'
        AND request_hash ~ '^[0-9a-f]{64}$'
        AND evidence_digest ~ '^[0-9a-f]{64}$'
        AND evidence_kind IN (
            'csi-confirmed', 'provider-confirmed',
            'delete-finalizer-confirmed', 'operator-attested'
        )
        AND ((actor_kind = 'user' AND actor_id IS NOT NULL)
             OR (actor_kind = 'service' AND actor_id IS NULL))
        AND reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
    )
);

ALTER TABLE storage_volume_assets
    ADD CONSTRAINT storage_volume_assets_destruction_assertion_fkey
    FOREIGN KEY (destruction_assertion_id)
    REFERENCES storage_backend_assertions(id) ON DELETE RESTRICT;

ALTER TABLE storage_asset_coverage_gaps
    ADD CONSTRAINT storage_asset_coverage_gaps_assertion_fkey
    FOREIGN KEY (resolution_assertion_id)
    REFERENCES storage_backend_assertions(id) ON DELETE RESTRICT;

-- Storage shadow rows are structured diagnostics, not usage intervals. There
-- is intentionally no resource_interval_id, lifecycle ID, publication-plan
-- FK, or path that a materializer can scan.
CREATE TABLE storage_shadow_observations (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    snapshot_id          UUID NOT NULL,
    inventory_scope_id   UUID NOT NULL,
    source_kind          TEXT NOT NULL,
    source_uid           TEXT NOT NULL,
    measurement_basis    TEXT NOT NULL,
    asset_id             UUID REFERENCES storage_volume_assets(id)
        ON DELETE RESTRICT,
    storage_bytes        BIGINT,
    resource             TEXT NOT NULL,
    mapping_version      TEXT,
    mapping_fingerprint  CHAR(64),
    attribution_scope    TEXT NOT NULL,
    owner_kind           TEXT,
    owner_id             TEXT,
    disposition          TEXT NOT NULL,
    reason_code          TEXT NOT NULL,
    observed_at          TIMESTAMPTZ NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT storage_shadow_observations_snapshot_fkey
        FOREIGN KEY (snapshot_id, inventory_scope_id)
        REFERENCES resource_inventory_snapshots(id, inventory_scope_id)
        ON DELETE RESTRICT,
    CONSTRAINT storage_shadow_observations_snapshot_identity_uq
        UNIQUE (snapshot_id, source_kind, source_uid),
    CONSTRAINT storage_shadow_observations_identity_check CHECK (
        source_uid <> '' AND length(source_uid) <= 256
        AND resource <> '' AND length(resource) <= 255
        AND reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
        AND ((mapping_version IS NULL AND mapping_fingerprint IS NULL)
             OR (source_kind = 'volume'
                AND resource ~ '^block_volume_[a-z0-9_]+$'
                AND mapping_version
                    ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'
                AND mapping_fingerprint ~ '^[0-9a-f]{64}$'))
        AND ((source_kind = 'pvc'
                AND measurement_basis = 'claim-requested'
                AND asset_id IS NULL)
             OR (source_kind = 'volume'
                AND measurement_basis = 'volume-provisioned'))
    ),
    CONSTRAINT storage_shadow_observations_shape_check CHECK (
        attribution_scope IN ('customer', 'shared-platform', 'unknown')
        AND ((attribution_scope = 'customer'
                AND owner_kind IN ('job', 'thread')
                AND owner_id IS NOT NULL AND owner_id <> '')
             OR (attribution_scope = 'shared-platform'
                AND owner_kind = 'platform' AND owner_id IS NULL)
             OR (attribution_scope = 'unknown'
                AND owner_kind IS NULL AND owner_id IS NULL))
        AND disposition IN (
            'eligible-unpriced', 'not-applicable', 'invalid',
            'identity-ambiguous', 'backend-unverified'
        )
        AND (storage_bytes IS NULL OR storage_bytes >= 0)
        AND (disposition <> 'eligible-unpriced' OR storage_bytes IS NOT NULL)
        AND (source_kind <> 'volume'
             OR disposition IN ('invalid', 'identity-ambiguous')
             OR asset_id IS NOT NULL)
    )
);

CREATE INDEX storage_shadow_observations_latest_idx
    ON storage_shadow_observations (
        inventory_scope_id, source_kind, source_uid, observed_at DESC
    );

CREATE INDEX storage_shadow_observations_retention_idx
    ON storage_shadow_observations (observed_at, id);

-- Mutable asset heads retain immutable identity and one-way terminal state.
CREATE FUNCTION protect_storage_volume_asset_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    transition_ok BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'storage volume assets cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.source_cluster IS DISTINCT FROM OLD.source_cluster
       OR NEW.asset_digest IS DISTINCT FROM OLD.asset_digest
       OR NEW.identity_key_version IS DISTINCT FROM OLD.identity_key_version
       OR NEW.identity_scheme IS DISTINCT FROM OLD.identity_scheme
       OR NEW.csi_driver IS DISTINCT FROM OLD.csi_driver
       OR NEW.source_lifecycle_id IS DISTINCT FROM OLD.source_lifecycle_id
       OR NEW.first_observed_at IS DISTINCT FROM OLD.first_observed_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'storage volume asset identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.last_observed_at < OLD.last_observed_at
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'storage volume asset cursors are monotonic'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.lifecycle_state = 'destroyed' THEN
        IF NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'destroyed storage volume assets are immutable'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.lifecycle_state = 'backend-unverified' THEN
        SELECT TRUE INTO transition_ok
        FROM public.storage_asset_coverage_gaps AS gap
        WHERE gap.asset_id = OLD.id AND gap.resolution = 'unresolved';
    ELSIF NEW.lifecycle_state = 'visible'
          AND OLD.lifecycle_state = 'backend-unverified' THEN
        SELECT TRUE INTO transition_ok
        FROM public.storage_asset_coverage_gaps AS gap
        WHERE gap.asset_id = OLD.id AND gap.resolution = 'reobserved'
        ORDER BY gap.gap_end DESC LIMIT 1;
    ELSIF NEW.lifecycle_state = 'destroyed' THEN
        SELECT TRUE INTO transition_ok
        FROM public.storage_backend_assertions AS assertion
        WHERE assertion.id = NEW.destruction_assertion_id
          AND assertion.asset_id = OLD.id
          AND assertion.effective_at = NEW.destroyed_at;
    ELSE
        transition_ok := NEW.lifecycle_state = OLD.lifecycle_state;
    END IF;

    IF transition_ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'storage volume asset transition lacks durable evidence'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER storage_volume_assets_lifecycle_guard
BEFORE UPDATE OR DELETE ON storage_volume_assets
FOR EACH ROW EXECUTE FUNCTION protect_storage_volume_asset_mutation();

CREATE FUNCTION protect_storage_volume_incarnation_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'storage volume incarnations cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.asset_id IS DISTINCT FROM OLD.asset_id
       OR NEW.inventory_scope_id IS DISTINCT FROM OLD.inventory_scope_id
       OR NEW.source_cluster IS DISTINCT FROM OLD.source_cluster
       OR NEW.pv_uid IS DISTINCT FROM OLD.pv_uid
       OR NEW.pv_name IS DISTINCT FROM OLD.pv_name
       OR NEW.first_observed_at IS DISTINCT FROM OLD.first_observed_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'storage volume incarnation identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.last_observed_at < OLD.last_observed_at
       OR NEW.updated_at < OLD.updated_at
       OR (OLD.backend_deletion_finalizer_observed
           AND NOT NEW.backend_deletion_finalizer_observed)
       OR (OLD.detached_at IS NOT NULL AND NEW IS DISTINCT FROM OLD)
       OR (OLD.detached_at IS NULL AND NEW.detached_at IS NOT NULL
           AND NEW.detached_at < OLD.last_observed_at) THEN
        RAISE EXCEPTION 'storage volume incarnation lifecycle is not monotonic'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER storage_volume_incarnations_lifecycle_guard
BEFORE UPDATE OR DELETE ON storage_volume_incarnations
FOR EACH ROW EXECUTE FUNCTION protect_storage_volume_incarnation_mutation();

-- Opening a gap atomically moves the asset out of confirmed accrual. A later
-- re-observation may close it and restore visibility; destruction is owned by
-- the append-only assertion trigger below.
CREATE FUNCTION protect_storage_asset_coverage_gap_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    asset_state TEXT;
    lifecycle_id UUID;
    first_seen  TIMESTAMPTZ;
    last_seen   TIMESTAMPTZ;
    evidence_ok BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'storage asset coverage gaps cannot be deleted'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'INSERT' THEN
        SELECT asset.lifecycle_state, asset.first_observed_at,
               asset.last_observed_at
        INTO asset_state, first_seen, last_seen
        FROM public.storage_volume_assets AS asset
        WHERE asset.id = NEW.asset_id
        FOR UPDATE;
        IF asset_state IS NULL OR asset_state = 'destroyed'
           OR NEW.gap_start < first_seen
           OR NEW.gap_start < last_seen
           OR NEW.resolution <> 'unresolved' THEN
            RAISE EXCEPTION 'storage asset gap cannot open for this lifecycle'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.resolution <> 'unresolved'
       OR NEW.id IS DISTINCT FROM OLD.id
       OR NEW.asset_id IS DISTINCT FROM OLD.asset_id
       OR NEW.scope_epoch_id IS DISTINCT FROM OLD.scope_epoch_id
       OR NEW.gap_start IS DISTINCT FROM OLD.gap_start
       OR NEW.reason_code IS DISTINCT FROM OLD.reason_code
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.resolution NOT IN ('reobserved', 'destroyed-confirmed')
       OR NEW.gap_end IS NULL
       OR NEW.resolved_at IS NULL
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'storage asset gap resolution is immutable or invalid'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.resolution = 'destroyed-confirmed' THEN
        SELECT TRUE INTO evidence_ok
        FROM public.storage_backend_assertions AS assertion
        WHERE assertion.id = NEW.resolution_assertion_id
          AND assertion.asset_id = OLD.asset_id
          AND assertion.effective_at = NEW.gap_end;
        IF evidence_ok IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'storage asset gap destruction evidence is invalid'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER storage_asset_coverage_gaps_lifecycle_guard
BEFORE INSERT OR UPDATE OR DELETE ON storage_asset_coverage_gaps
FOR EACH ROW EXECUTE FUNCTION protect_storage_asset_coverage_gap_mutation();

CREATE FUNCTION transition_storage_asset_for_gap()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE public.storage_volume_assets
        SET lifecycle_state = 'backend-unverified',
            backend_unverified_at = NEW.gap_start,
            updated_at = statement_timestamp()
        WHERE id = NEW.asset_id;
    ELSIF NEW.resolution = 'reobserved' THEN
        UPDATE public.storage_volume_assets
        SET lifecycle_state = 'visible',
            backend_unverified_at = NULL,
            last_observed_at = GREATEST(last_observed_at, NEW.gap_end),
            updated_at = statement_timestamp()
        WHERE id = NEW.asset_id;
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER storage_asset_coverage_gaps_transition
AFTER INSERT OR UPDATE OF resolution ON storage_asset_coverage_gaps
FOR EACH ROW EXECUTE FUNCTION transition_storage_asset_for_gap();

-- The assertion trigger validates exact idempotency replays before PostgreSQL
-- resolves ON CONFLICT, then owns both the gap closure and terminal asset state.
CREATE FUNCTION protect_storage_backend_assertion_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    prior       public.storage_backend_assertions%ROWTYPE;
    asset_state TEXT;
    lifecycle_id UUID;
    first_seen  TIMESTAMPTZ;
    last_seen   TIMESTAMPTZ;
    open_start  TIMESTAMPTZ;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'storage backend assertions are append-only'
            USING ERRCODE = '55000';
    END IF;

    SELECT * INTO prior
    FROM public.storage_backend_assertions AS assertion
    WHERE assertion.idempotency_key = NEW.idempotency_key;
    IF FOUND THEN
        IF prior.asset_id IS DISTINCT FROM NEW.asset_id
           OR prior.assertion_kind IS DISTINCT FROM NEW.assertion_kind
           OR prior.request_hash IS DISTINCT FROM NEW.request_hash
           OR prior.effective_at IS DISTINCT FROM NEW.effective_at
           OR prior.evidence_kind IS DISTINCT FROM NEW.evidence_kind
           OR prior.evidence_digest IS DISTINCT FROM NEW.evidence_digest
           OR prior.actor_kind IS DISTINCT FROM NEW.actor_kind
           OR prior.actor_id IS DISTINCT FROM NEW.actor_id
           OR prior.reason_code IS DISTINCT FROM NEW.reason_code THEN
            RAISE EXCEPTION
                'storage assertion idempotency replay changed immutable intent'
                USING ERRCODE = '23505';
        END IF;
        RETURN NEW;
    END IF;

    SELECT asset.lifecycle_state, asset.source_lifecycle_id,
           asset.first_observed_at, asset.last_observed_at
    INTO asset_state, lifecycle_id, first_seen, last_seen
    FROM public.storage_volume_assets AS asset
    WHERE asset.id = NEW.asset_id
    FOR UPDATE;
    IF asset_state IS NULL OR asset_state <> 'backend-unverified'
       OR NEW.effective_at < first_seen
       OR NEW.effective_at < last_seen
       OR NEW.effective_at > statement_timestamp() THEN
        RAISE EXCEPTION 'storage destruction assertion is outside the lifecycle'
            USING ERRCODE = '55000';
    END IF;

    SELECT gap.gap_start INTO open_start
    FROM public.storage_asset_coverage_gaps AS gap
    WHERE gap.asset_id = NEW.asset_id AND gap.resolution = 'unresolved'
    FOR UPDATE;
    IF open_start IS NULL THEN
        RAISE EXCEPTION 'storage destruction requires an unresolved backend gap'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.effective_at < open_start THEN
        RAISE EXCEPTION 'storage destruction predates the backend-unknown gap'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.storage_volume_incarnations AS incarnation
        WHERE incarnation.asset_id = NEW.asset_id
          AND incarnation.detached_at IS NULL
    ) OR EXISTS (
        SELECT 1 FROM public.resource_intervals AS interval
        WHERE interval.source_lifecycle_id = lifecycle_id
          AND interval.ended_at IS NULL
    ) THEN
        RAISE EXCEPTION 'storage destruction requires a detached closed lifecycle'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER storage_backend_assertions_append_only
BEFORE INSERT OR UPDATE OR DELETE ON storage_backend_assertions
FOR EACH ROW EXECUTE FUNCTION protect_storage_backend_assertion_mutation();

CREATE FUNCTION transition_storage_asset_for_destruction()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    UPDATE public.storage_asset_coverage_gaps
    SET gap_end = NEW.effective_at,
        resolution = 'destroyed-confirmed',
        resolution_assertion_id = NEW.id,
        resolved_at = statement_timestamp(),
        updated_at = statement_timestamp()
    WHERE asset_id = NEW.asset_id AND resolution = 'unresolved';

    UPDATE public.storage_volume_incarnations
    SET detached_at = GREATEST(last_observed_at, NEW.effective_at),
        detach_reason = 'backend-destroyed',
        updated_at = statement_timestamp()
    WHERE asset_id = NEW.asset_id AND detached_at IS NULL;

    UPDATE public.storage_volume_assets
    SET lifecycle_state = 'destroyed',
        destroyed_at = NEW.effective_at,
        destruction_assertion_id = NEW.id,
        updated_at = statement_timestamp()
    WHERE id = NEW.asset_id;
    RETURN NULL;
END;
$$;

CREATE TRIGGER storage_backend_assertions_transition
AFTER INSERT ON storage_backend_assertions
FOR EACH ROW EXECUTE FUNCTION transition_storage_asset_for_destruction();

CREATE FUNCTION protect_storage_shadow_observation_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    snapshot_state   TEXT;
    activation_state TEXT;
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

    SELECT snapshot.manifest_state
    INTO snapshot_state
    FROM public.resource_inventory_snapshots AS snapshot
    JOIN public.resource_inventory_ingest_tickets AS ticket
      ON ticket.id = snapshot.ingest_ticket_id
    JOIN public.infra_metering_control AS control ON control.singleton = TRUE
    WHERE snapshot.id = NEW.snapshot_id
      AND snapshot.inventory_scope_id = NEW.inventory_scope_id
      AND snapshot.leader_generation = control.leader_generation
      AND ticket.bound_snapshot_id = snapshot.id
      AND ticket.consumed_at IS NULL
      AND ticket.expires_at > statement_timestamp();

    SELECT activation.state
    INTO activation_state
    FROM public.storage_metering_activation AS activation
    WHERE activation.measurement_basis = NEW.measurement_basis
    FOR SHARE;

    IF snapshot_state IS DISTINCT FROM 'staging'
       OR activation_state NOT IN ('shadow', 'active') THEN
        RAISE EXCEPTION 'storage shadow observation fence failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER storage_shadow_observations_immutable
BEFORE INSERT OR UPDATE OR DELETE ON storage_shadow_observations
FOR EACH ROW EXECUTE FUNCTION protect_storage_shadow_observation_mutation();

COMMENT ON TABLE storage_metering_activation IS
    'Forward-only per-basis shadow/activation boundary; storage intervals are database-clamped to activated_at.';
COMMENT ON TABLE storage_identity_key_state IS
    'Fingerprint and version of the dedicated stable volume-identity HMAC key; key material and raw CSI handles are never persisted.';
COMMENT ON TABLE storage_volume_assets IS
    'Physical storage lifecycle keyed only by an opaque HMAC digest; PV UID belongs to an incarnation, not the billable asset.';
COMMENT ON TABLE storage_volume_incarnations IS
    'Kubernetes PV incarnations attached to one durable physical-volume asset; contains no CSI handle or attributes.';
COMMENT ON TABLE storage_asset_coverage_gaps IS
    'Per-asset backend-unverified ranges, separate from collector-wide coverage gaps.';
COMMENT ON TABLE storage_backend_assertions IS
    'Append-only idempotent evidence that a physical backend was destroyed.';
COMMENT ON TABLE storage_shadow_observations IS
    'Non-publishable storage classifications with no interval or publication-plan relationship.';

COMMIT;
