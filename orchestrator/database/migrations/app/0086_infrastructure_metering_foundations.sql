-- migration:     0086_infrastructure_metering_foundations.sql
-- description:   Slice 0 foundations for typed infrastructure allocation
--                metering. Adds the app-owned inventory, interval, publication
--                outbox, daily read model, bootstrap gates, and immutable
--                canonical/cloud rate-version tables. All runtime flags remain
--                inert; this migration does not publish usage.
-- depends-on:    0085_datasources_auto_attach_owner_idx.notx.sql
-- expected:      < 2s. New empty tables/indexes plus trusted btree_gist setup.
-- locks:         Brief catalog locks and locks on newly-created tables only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Equality operators for UUID/text columns in the effective-range and
-- lifecycle exclusion constraints below. btree_gist is a trusted PostgreSQL
-- contrib extension and is available in the supported PG15/16 images.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- -------------------------------------------------------------------------
-- Immutable canonical ledger rates. Unlike legacy usage_rates wildcards, one
-- row is an exact typed selector and no two versions may cover the same instant.
-- -------------------------------------------------------------------------
CREATE TABLE usage_rates_v2 (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cost_domain           TEXT NOT NULL,
    measurement_basis     TEXT NOT NULL,
    category              TEXT NOT NULL,
    resource_class        TEXT NOT NULL,
    resource              TEXT NOT NULL,
    unit                  TEXT NOT NULL,
    effective_from        TIMESTAMPTZ NOT NULL,
    effective_to          TIMESTAMPTZ,
    usd_per_unit          NUMERIC(38, 18) NOT NULL,
    source                TEXT NOT NULL,
    source_version        TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT usage_rates_v2_nonempty_selector_check CHECK (
        cost_domain <> '' AND measurement_basis <> '' AND category <> ''
        AND resource_class <> '' AND resource <> '' AND resource <> '*'
        AND unit <> '' AND source <> '' AND source_version <> ''
    ),
    CONSTRAINT usage_rates_v2_rate_nonnegative_check CHECK (
        usd_per_unit >= 0
        AND usd_per_unit NOT IN (
            'NaN'::NUMERIC, 'Infinity'::NUMERIC, '-Infinity'::NUMERIC
        )
    ),
    CONSTRAINT usage_rates_v2_effective_range_check
        CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT usage_rates_v2_no_overlap EXCLUDE USING gist (
        cost_domain WITH =,
        measurement_basis WITH =,
        category WITH =,
        resource_class WITH =,
        resource WITH =,
        unit WITH =,
        tstzrange(effective_from, effective_to, '[)') WITH &&
    )
);

CREATE INDEX usage_rates_v2_lookup_idx
    ON usage_rates_v2 (
        cost_domain, measurement_basis, category, resource_class,
        resource, unit, effective_from DESC
    );

-- -------------------------------------------------------------------------
-- Immutable public-cloud scenario versions. The legacy cards remain readable
-- during compatibility; these tables are the typed/non-linear successor.
-- -------------------------------------------------------------------------
CREATE TABLE usage_rate_card_versions_v2 (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    card_id                  TEXT NOT NULL
        REFERENCES usage_rate_cards(id) ON DELETE RESTRICT,
    provider                 TEXT NOT NULL,
    target_service           TEXT NOT NULL,
    target_region            TEXT NOT NULL,
    currency                 TEXT NOT NULL,
    pricing_basis            TEXT NOT NULL,
    calculator               TEXT NOT NULL,
    aggregation_scope        TEXT NOT NULL,
    shape_change_policy      TEXT NOT NULL,
    provider_effective_from  TIMESTAMPTZ NOT NULL,
    provider_effective_to    TIMESTAMPTZ,
    source_published_at      TIMESTAMPTZ,
    observed_at              TIMESTAMPTZ NOT NULL,
    source_version           TEXT NOT NULL,
    source_checksum          TEXT NOT NULL,
    component_count          INTEGER NOT NULL,
    component_manifest_hash  TEXT NOT NULL,
    applicability            JSONB NOT NULL DEFAULT '{}'::JSONB,
    calculator_config        JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT usage_rate_card_versions_v2_nonempty_check CHECK (
        provider <> '' AND target_service <> '' AND target_region <> ''
        AND source_version <> '' AND source_checksum <> ''
        AND component_count > 0
        AND component_manifest_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT usage_rate_card_versions_v2_currency_check
        CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT usage_rate_card_versions_v2_pricing_basis_check CHECK (
        pricing_basis IN ('historical-public-list', 'current-price-scenario')
    ),
    CONSTRAINT usage_rate_card_versions_v2_calculator_check CHECK (
        calculator IN (
            'linear_v1',
            'exact_flavor_v1',
            'reference_dominant_share_v1',
            'fargate_v1',
            'aci_container_group_v1',
            'block_volume_v1',
            'azure_managed_disk_v1'
        )
    ),
    CONSTRAINT usage_rate_card_versions_v2_aggregation_scope_check CHECK (
        aggregation_scope IN ('lifecycle', 'concurrency-envelope')
        AND (
            calculator = 'reference_dominant_share_v1'
            OR aggregation_scope = 'lifecycle'
        )
        AND (
            calculator <> 'reference_dominant_share_v1'
            OR aggregation_scope = 'concurrency-envelope'
        )
    ),
    CONSTRAINT usage_rate_card_versions_v2_shape_policy_check CHECK (
        shape_change_policy IN ('continue', 'restart', 'unsupported')
    ),
    CONSTRAINT usage_rate_card_versions_v2_effective_range_check CHECK (
        provider_effective_to IS NULL
        OR provider_effective_to > provider_effective_from
    ),
    CONSTRAINT usage_rate_card_versions_v2_publication_time_check CHECK (
        source_published_at IS NULL OR observed_at >= source_published_at
    ),
    CONSTRAINT usage_rate_card_versions_v2_json_check CHECK (
        jsonb_typeof(applicability) = 'object'
        AND jsonb_typeof(calculator_config) = 'object'
    )
);

CREATE INDEX usage_rate_card_versions_v2_select_idx
    ON usage_rate_card_versions_v2 (
        card_id, pricing_basis, provider_effective_from DESC
    );

CREATE TABLE usage_rate_components_v2 (
    version_id         UUID NOT NULL
        REFERENCES usage_rate_card_versions_v2(id) ON DELETE RESTRICT,
    component          TEXT NOT NULL,
    ordinal            INTEGER NOT NULL DEFAULT 0,
    source_sku         TEXT,
    source_meter       TEXT,
    billing_unit       TEXT NOT NULL,
    unit_size          NUMERIC(38, 18) NOT NULL,
    unit_price         NUMERIC(38, 18) NOT NULL,
    tier_min           NUMERIC(38, 18),
    tier_max           NUMERIC(38, 18),
    included_quantity  NUMERIC(38, 18),
    source_metadata    JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (version_id, component, ordinal),
    CONSTRAINT usage_rate_components_v2_shape_check CHECK (
        component <> '' AND billing_unit <> '' AND ordinal >= 0
        AND unit_size > 0 AND unit_price >= 0
        AND unit_size NOT IN (
            'NaN'::NUMERIC, 'Infinity'::NUMERIC, '-Infinity'::NUMERIC
        )
        AND unit_price NOT IN (
            'NaN'::NUMERIC, 'Infinity'::NUMERIC, '-Infinity'::NUMERIC
        )
        AND (tier_min IS NULL OR tier_min >= 0)
        AND (tier_max IS NULL OR tier_max > 0)
        AND (tier_min IS NULL OR tier_max IS NULL OR tier_max > tier_min)
        AND (included_quantity IS NULL OR included_quantity >= 0)
        AND (tier_min IS NULL OR tier_min NOT IN (
            'NaN'::NUMERIC, 'Infinity'::NUMERIC, '-Infinity'::NUMERIC
        ))
        AND (tier_max IS NULL OR tier_max NOT IN (
            'NaN'::NUMERIC, 'Infinity'::NUMERIC, '-Infinity'::NUMERIC
        ))
        AND (included_quantity IS NULL OR included_quantity NOT IN (
            'NaN'::NUMERIC, 'Infinity'::NUMERIC, '-Infinity'::NUMERIC
        ))
        AND jsonb_typeof(source_metadata) = 'object'
    )
);

-- -------------------------------------------------------------------------
-- Inventory scopes and effective coverage epochs. Current health belongs to an
-- epoch so enabling a new source cannot rewrite historical coverage claims.
-- -------------------------------------------------------------------------
CREATE TABLE resource_inventory_scopes (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    collector_id     TEXT NOT NULL,
    source_cluster   TEXT NOT NULL,
    api_resource     TEXT NOT NULL,
    namespace        TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT resource_inventory_scopes_id_cluster_uq
        UNIQUE (id, source_cluster),
    CONSTRAINT resource_inventory_scopes_nonempty_check CHECK (
        collector_id <> '' AND source_cluster <> '' AND api_resource <> ''
        AND (namespace IS NULL OR namespace <> '')
    )
);

CREATE UNIQUE INDEX resource_inventory_scopes_identity_uq
    ON resource_inventory_scopes (
        collector_id, source_cluster, api_resource, namespace
    ) NULLS NOT DISTINCT;

CREATE TABLE resource_inventory_scope_epochs (
    id                         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scope_id                   UUID NOT NULL
        REFERENCES resource_inventory_scopes(id) ON DELETE RESTRICT,
    epoch_number               BIGINT NOT NULL,
    reliable_from              TIMESTAMPTZ,
    required_for_rollup        BOOLEAN NOT NULL DEFAULT FALSE,
    required_from              TIMESTAMPTZ,
    retired_at                 TIMESTAMPTZ,
    coverage_mode              TEXT NOT NULL,
    capture_epoch              UUID,
    last_attempt_at            TIMESTAMPTZ,
    last_complete_at           TIMESTAMPTZ,
    last_complete_snapshot_id  UUID,
    last_resource_version      TEXT,
    controller_epoch           TEXT,
    last_sequence              BIGINT,
    leader_generation          BIGINT NOT NULL DEFAULT 0,
    continuous_since           TIMESTAMPTZ,
    complete_through           TIMESTAMPTZ,
    snapshot_health            TEXT NOT NULL DEFAULT 'initializing',
    continuity_health          TEXT NOT NULL DEFAULT 'initializing',
    item_health                TEXT NOT NULL DEFAULT 'initializing',
    backend_health             TEXT NOT NULL DEFAULT 'initializing',
    publication_health         TEXT NOT NULL DEFAULT 'initializing',
    consecutive_failures       INTEGER NOT NULL DEFAULT 0,
    last_item_count            INTEGER,
    sanitized_error            JSONB,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT resource_inventory_scope_epochs_id_scope_uq
        UNIQUE (id, scope_id),
    UNIQUE (scope_id, epoch_number),
    CONSTRAINT resource_inventory_scope_epochs_number_check
        CHECK (epoch_number > 0),
    CONSTRAINT resource_inventory_scope_epochs_requirement_check CHECK (
        (required_for_rollup AND required_from IS NOT NULL
            AND reliable_from IS NOT NULL AND required_from >= reliable_from)
        OR (NOT required_for_rollup AND required_from IS NULL)
    ),
    CONSTRAINT resource_inventory_scope_epochs_midnight_check CHECK (
        required_from IS NULL
        OR required_from = date_trunc('day', required_from, 'UTC')
    ),
    CONSTRAINT resource_inventory_scope_epochs_retirement_check CHECK (
        retired_at IS NULL
        OR ((reliable_from IS NULL OR retired_at >= reliable_from)
            AND (required_from IS NULL OR retired_at > required_from)
            AND (continuous_since IS NULL OR retired_at >= continuous_since)
            AND (complete_through IS NULL OR retired_at >= complete_through))
    ),
    CONSTRAINT resource_inventory_scope_epochs_health_check CHECK (
        coverage_mode <> ''
        AND snapshot_health <> '' AND continuity_health <> ''
        AND item_health <> '' AND backend_health <> ''
        AND publication_health <> ''
        AND leader_generation >= 0 AND consecutive_failures >= 0
        AND (last_sequence IS NULL OR last_sequence >= 0)
        AND (last_item_count IS NULL OR last_item_count >= 0)
        AND (sanitized_error IS NULL OR jsonb_typeof(sanitized_error) = 'object')
    )
);

CREATE UNIQUE INDEX resource_inventory_scope_epochs_active_uq
    ON resource_inventory_scope_epochs (scope_id)
    WHERE retired_at IS NULL;

CREATE INDEX resource_inventory_scope_epochs_rollup_idx
    ON resource_inventory_scope_epochs (required_from, retired_at)
    WHERE required_for_rollup = TRUE;

CREATE TABLE resource_inventory_snapshots (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scope_epoch_id           UUID NOT NULL,
    inventory_scope_id       UUID NOT NULL,
    collection_started_at    TIMESTAMPTZ NOT NULL,
    collection_completed_at  TIMESTAMPTZ NOT NULL,
    received_at              TIMESTAMPTZ NOT NULL,
    source_snapshot_at       TIMESTAMPTZ,
    complete                 BOOLEAN NOT NULL,
    leader_generation        BIGINT NOT NULL,
    resource_version         TEXT,
    controller_epoch         TEXT,
    sequence                 BIGINT,
    item_count               INTEGER NOT NULL,
    item_digest              TEXT,
    fatal_errors             JSONB NOT NULL DEFAULT '[]'::JSONB,
    item_errors              JSONB NOT NULL DEFAULT '[]'::JSONB,
    manifest_state           TEXT NOT NULL DEFAULT 'staging',
    sealed_at                TIMESTAMPTZ,
    items_expired_at         TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT resource_inventory_snapshots_id_epoch_uq
        UNIQUE (id, scope_epoch_id),
    CONSTRAINT resource_inventory_snapshots_id_scope_uq
        UNIQUE (id, inventory_scope_id),
    CONSTRAINT resource_inventory_snapshots_epoch_scope_fkey
        FOREIGN KEY (scope_epoch_id, inventory_scope_id)
        REFERENCES resource_inventory_scope_epochs(id, scope_id)
        ON DELETE RESTRICT,
    CONSTRAINT resource_inventory_snapshots_time_check CHECK (
        collection_completed_at >= collection_started_at
        AND received_at >= collection_completed_at
        AND (sealed_at IS NULL OR sealed_at >= received_at)
    ),
    CONSTRAINT resource_inventory_snapshots_shape_check CHECK (
        leader_generation >= 0 AND item_count >= 0
        AND (sequence IS NULL OR sequence >= 0)
        AND (item_digest IS NULL OR item_digest ~ '^[0-9a-f]{64}$')
        AND jsonb_typeof(fatal_errors) = 'array'
        AND jsonb_typeof(item_errors) = 'array'
        AND (NOT complete OR jsonb_array_length(fatal_errors) = 0)
        AND (manifest_state <> 'sealed' OR NOT complete
             OR item_digest IS NOT NULL)
    ),
    CONSTRAINT resource_inventory_snapshots_manifest_state_check CHECK (
        (manifest_state = 'staging'
            AND sealed_at IS NULL AND items_expired_at IS NULL)
        OR (manifest_state = 'sealed'
            AND sealed_at IS NOT NULL AND items_expired_at IS NULL)
        OR (manifest_state = 'items-expired'
            AND sealed_at IS NOT NULL AND items_expired_at IS NOT NULL
            AND items_expired_at >= sealed_at)
    )
);

CREATE UNIQUE INDEX resource_inventory_snapshots_controller_seq_uq
    ON resource_inventory_snapshots (
        scope_epoch_id, controller_epoch, sequence
    )
    WHERE controller_epoch IS NOT NULL AND sequence IS NOT NULL;

CREATE INDEX resource_inventory_snapshots_scope_time_idx
    ON resource_inventory_snapshots (
        scope_epoch_id, collection_completed_at DESC
    );

ALTER TABLE resource_inventory_scope_epochs
    ADD CONSTRAINT resource_inventory_scope_epochs_last_snapshot_fkey
    FOREIGN KEY (last_complete_snapshot_id, id)
    REFERENCES resource_inventory_snapshots(id, scope_epoch_id)
    ON DELETE RESTRICT;

CREATE TABLE resource_inventory_snapshot_items (
    snapshot_id         UUID NOT NULL
        REFERENCES resource_inventory_snapshots(id) ON DELETE RESTRICT,
    source_kind         TEXT NOT NULL,
    source_uid          TEXT NOT NULL,
    revision_hash       TEXT,
    normalized_item     JSONB NOT NULL,
    valid_for_metering  BOOLEAN NOT NULL,
    item_error          JSONB,

    PRIMARY KEY (snapshot_id, source_kind, source_uid),
    CONSTRAINT resource_inventory_snapshot_items_shape_check CHECK (
        source_kind <> '' AND source_uid <> ''
        AND (revision_hash IS NULL OR revision_hash ~ '^[0-9a-f]{64}$')
        AND jsonb_typeof(normalized_item) = 'object'
        AND (item_error IS NULL OR jsonb_typeof(item_error) = 'object')
        AND ((valid_for_metering AND revision_hash IS NOT NULL
                AND item_error IS NULL)
             OR (NOT valid_for_metering AND item_error IS NOT NULL))
    )
);

CREATE TABLE resource_inventory_coverage_gaps (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scope_epoch_id      UUID NOT NULL
        REFERENCES resource_inventory_scope_epochs(id) ON DELETE RESTRICT,
    gap_start           TIMESTAMPTZ NOT NULL,
    gap_end             TIMESTAMPTZ,
    reason              TEXT NOT NULL,
    resolution          TEXT NOT NULL DEFAULT 'unresolved',
    resolution_details  JSONB NOT NULL DEFAULT '{}'::JSONB,
    resolved_at         TIMESTAMPTZ,
    resolved_by         UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT resource_inventory_coverage_gaps_range_check
        CHECK (gap_end IS NULL OR gap_end > gap_start),
    CONSTRAINT resource_inventory_coverage_gaps_resolution_check CHECK (
        reason <> ''
        AND resolution IN ('unresolved', 'backfilled', 'waived')
        AND jsonb_typeof(resolution_details) = 'object'
        AND ((resolution = 'unresolved'
                AND resolved_at IS NULL AND resolved_by IS NULL)
             OR (resolution = 'backfilled' AND resolved_at IS NOT NULL)
             OR (resolution = 'waived'
                AND resolved_at IS NOT NULL AND resolved_by IS NOT NULL))
    )
);

CREATE UNIQUE INDEX resource_inventory_coverage_gaps_open_uq
    ON resource_inventory_coverage_gaps (scope_epoch_id, gap_start, reason)
    WHERE resolution = 'unresolved';

CREATE INDEX resource_inventory_coverage_gaps_range_idx
    ON resource_inventory_coverage_gaps (scope_epoch_id, gap_start, gap_end);

-- Fencing/cutover state is a real singleton, not a convention around LIMIT 1.
CREATE TABLE infra_metering_control (
    singleton          BOOLEAN PRIMARY KEY DEFAULT TRUE,
    leader_generation  BIGINT NOT NULL DEFAULT 0,
    cutover_state      TEXT NOT NULL DEFAULT 'disabled',
    cutover_at         TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT infra_metering_control_singleton_check CHECK (singleton),
    CONSTRAINT infra_metering_control_generation_check
        CHECK (leader_generation >= 0),
    CONSTRAINT infra_metering_control_cutover_check CHECK (
        (cutover_state = 'disabled' AND cutover_at IS NULL)
        OR (cutover_state IN ('preparing', 'active') AND cutover_at IS NOT NULL)
    )
);

INSERT INTO infra_metering_control (
    singleton, leader_generation, cutover_state, cutover_at
) VALUES (TRUE, 0, 'disabled', NULL)
ON CONFLICT (singleton) DO NOTHING;

-- -------------------------------------------------------------------------
-- Mutable lifecycle heads and immutable-at-capacity interval revisions.
-- -------------------------------------------------------------------------
CREATE TABLE resource_lifecycle_heads (
    source_lifecycle_id  UUID PRIMARY KEY,
    latest_revision_no   BIGINT NOT NULL DEFAULT 0,
    current_interval_id  UUID,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT resource_lifecycle_heads_revision_check
        CHECK (latest_revision_no >= 0)
);

CREATE TABLE resource_intervals (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    inventory_scope_id       UUID NOT NULL,
    source_cluster           TEXT NOT NULL,
    source_kind              TEXT NOT NULL,
    source_uid               TEXT NOT NULL,
    source_api_version       TEXT NOT NULL,
    source_resource_version  TEXT,
    source_lifecycle_id      UUID NOT NULL
        REFERENCES resource_lifecycle_heads(source_lifecycle_id)
        ON DELETE RESTRICT,
    revision_no              BIGINT NOT NULL,
    source_revision          TEXT NOT NULL,
    namespace                TEXT,
    name                     TEXT NOT NULL,
    category                 TEXT NOT NULL,
    resource                 TEXT NOT NULL,
    measurement_basis        TEXT NOT NULL,
    cost_domain              TEXT NOT NULL,
    resource_class           TEXT NOT NULL,
    attribution_scope        TEXT NOT NULL,
    owner_kind               TEXT,
    owner_id                 TEXT,
    user_id                  UUID,
    project_id               UUID,
    attribution_source       TEXT NOT NULL,
    attribution_quality      TEXT NOT NULL,
    backing_resource_uid     TEXT,
    lifecycle_confidence     TEXT NOT NULL,
    cpu_millicores           BIGINT,
    memory_bytes             BIGINT,
    storage_bytes            BIGINT,
    capacity_source          TEXT NOT NULL,
    capacity_quality         TEXT NOT NULL,
    measurement_algorithm    TEXT NOT NULL,
    started_at               TIMESTAMPTZ NOT NULL,
    start_time_source        TEXT NOT NULL,
    start_uncertainty_us     BIGINT NOT NULL,
    ended_at                 TIMESTAMPTZ,
    end_time_source          TEXT,
    end_uncertainty_us       BIGINT,
    last_seen_at             TIMESTAMPTZ NOT NULL,
    last_confirmed_at        TIMESTAMPTZ NOT NULL,
    last_seen_snapshot_id    UUID,
    materialized_through     TIMESTAMPTZ NOT NULL,
    end_reason               TEXT,
    details                  JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT resource_intervals_id_lifecycle_uq
        UNIQUE (id, source_lifecycle_id),
    CONSTRAINT resource_intervals_id_revision_uq
        UNIQUE (id, source_revision),
    CONSTRAINT resource_intervals_lifecycle_revision_uq
        UNIQUE (source_lifecycle_id, revision_no),
    CONSTRAINT resource_intervals_inventory_scope_cluster_fkey
        FOREIGN KEY (inventory_scope_id, source_cluster)
        REFERENCES resource_inventory_scopes(id, source_cluster)
        ON DELETE RESTRICT,
    CONSTRAINT resource_intervals_last_seen_snapshot_fkey
        FOREIGN KEY (last_seen_snapshot_id, inventory_scope_id)
        REFERENCES resource_inventory_snapshots(id, inventory_scope_id)
        ON DELETE RESTRICT,
    CONSTRAINT resource_intervals_identity_check CHECK (
        source_cluster <> '' AND source_uid <> '' AND source_api_version <> ''
        AND name <> '' AND resource <> '' AND resource_class <> ''
        AND attribution_source <> '' AND attribution_quality <> ''
        AND lifecycle_confidence <> '' AND capacity_source <> ''
        AND capacity_quality <> '' AND measurement_algorithm <> ''
        AND start_time_source <> ''
        AND source_revision ~ '^[0-9a-f]{64}$'
        AND (namespace IS NULL OR namespace <> '')
    ),
    CONSTRAINT resource_intervals_dimension_check CHECK (
        source_kind IN ('pod', 'vmi', 'pvc', 'volume')
        AND category IN ('compute', 'storage')
        AND measurement_basis IN (
            'scheduler-request', 'guest-provisioned',
            'claim-requested', 'volume-provisioned'
        )
        AND cost_domain IN (
            'workload-allocation', 'physical-asset', 'idle', 'overhead'
        )
        AND attribution_scope IN ('customer', 'shared-platform', 'unknown')
        AND (owner_kind IS NULL OR owner_kind IN (
            'job', 'thread', 'platform', 'unknown'
        ))
        AND ((source_kind = 'pod' AND category = 'compute'
                AND measurement_basis = 'scheduler-request'
                AND resource_class = 'kubernetes-pod'
                AND cost_domain = 'workload-allocation')
             OR (source_kind = 'vmi' AND category = 'compute'
                AND measurement_basis = 'guest-provisioned'
                AND resource_class = 'virtual-machine'
                AND cost_domain = 'workload-allocation')
             OR (source_kind = 'pvc' AND category = 'storage'
                AND measurement_basis = 'claim-requested'
                AND resource_class = 'persistent-volume-claim'
                AND cost_domain = 'workload-allocation')
             OR (source_kind = 'volume' AND category = 'storage'
                AND measurement_basis = 'volume-provisioned'
                AND resource_class = 'persistent-volume'
                AND cost_domain = 'physical-asset'))
    ),
    CONSTRAINT resource_intervals_attribution_check CHECK (
        (attribution_scope = 'customer'
            AND owner_kind IS NOT NULL
            AND owner_kind IN ('job', 'thread')
            AND owner_id IS NOT NULL AND owner_id <> ''
            AND user_id IS NOT NULL
            AND attribution_quality IN ('exact', 'derived'))
        OR (attribution_scope = 'shared-platform'
            AND user_id IS NULL AND project_id IS NULL
            AND attribution_quality IN ('exact', 'derived'))
        OR (attribution_scope = 'unknown'
            AND user_id IS NULL AND project_id IS NULL
            AND attribution_quality IN ('ambiguous', 'unknown'))
    ),
    CONSTRAINT resource_intervals_quality_check CHECK (
        attribution_quality IN (
            'exact', 'derived', 'ambiguous', 'unknown', 'invalid'
        )
        AND capacity_quality IN (
            'exact', 'derived', 'conservative',
            'resize-status-unavailable', 'unsupported', 'unknown', 'invalid'
        )
        AND attribution_quality <> 'invalid'
        AND capacity_quality NOT IN ('unsupported', 'unknown', 'invalid')
        AND lifecycle_confidence IN (
            'backend-confirmed',
            'kubernetes-visible',
            'backend-unverified'
        )
    ),
    CONSTRAINT resource_intervals_capacity_check CHECK (
        revision_no > 0
        AND (cpu_millicores IS NULL OR cpu_millicores >= 0)
        AND (memory_bytes IS NULL OR memory_bytes >= 0)
        AND (storage_bytes IS NULL OR storage_bytes >= 0)
        AND ((category = 'compute' AND cpu_millicores IS NOT NULL
                AND memory_bytes IS NOT NULL AND storage_bytes IS NULL)
             OR (category = 'storage' AND storage_bytes IS NOT NULL
                AND cpu_millicores IS NULL AND memory_bytes IS NULL))
    ),
    CONSTRAINT resource_intervals_time_check CHECK (
        start_uncertainty_us >= 0
        AND (end_uncertainty_us IS NULL OR end_uncertainty_us >= 0)
        AND (ended_at IS NULL OR ended_at >= started_at)
    ),
    CONSTRAINT resource_intervals_cursor_check CHECK (
        last_seen_at >= started_at
        AND last_confirmed_at >= started_at
        AND materialized_through >= started_at
        AND materialized_through <= COALESCE(ended_at, last_confirmed_at)
    ),
    CONSTRAINT resource_intervals_end_metadata_check CHECK (
        (ended_at IS NULL AND end_time_source IS NULL
            AND end_uncertainty_us IS NULL AND end_reason IS NULL)
        OR (ended_at IS NOT NULL AND end_time_source IS NOT NULL
            AND end_uncertainty_us IS NOT NULL AND end_reason IS NOT NULL)
    ),
    CONSTRAINT resource_intervals_details_check
        CHECK (jsonb_typeof(details) = 'object'),
    CONSTRAINT resource_intervals_lifecycle_no_overlap EXCLUDE USING gist (
        source_lifecycle_id WITH =,
        tstzrange(started_at, ended_at, '[)') WITH &&
    )
);

CREATE UNIQUE INDEX resource_intervals_open_uq
    ON resource_intervals (source_cluster, source_kind, source_uid)
    WHERE ended_at IS NULL;

CREATE UNIQUE INDEX resource_intervals_open_lifecycle_uq
    ON resource_intervals (source_lifecycle_id)
    WHERE ended_at IS NULL;

CREATE INDEX resource_intervals_materializer_idx
    ON resource_intervals (materialized_through, last_confirmed_at)
    WHERE materialized_through < COALESCE(ended_at, last_confirmed_at);

CREATE INDEX resource_intervals_user_time_idx
    ON resource_intervals (user_id, started_at, ended_at)
    WHERE user_id IS NOT NULL;

CREATE INDEX resource_intervals_project_time_idx
    ON resource_intervals (project_id, started_at, ended_at)
    WHERE project_id IS NOT NULL;

ALTER TABLE resource_lifecycle_heads
    ADD CONSTRAINT resource_lifecycle_heads_current_interval_fkey
    FOREIGN KEY (current_interval_id, source_lifecycle_id)
    REFERENCES resource_intervals(id, source_lifecycle_id)
    ON DELETE RESTRICT;

-- -------------------------------------------------------------------------
-- Frozen app-side publication outbox. One normalized child row equals one
-- expected audit usage row and carries its independent hash/rate reference.
-- -------------------------------------------------------------------------
CREATE TABLE resource_publication_plans (
    id                             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_interval_id             UUID NOT NULL,
    source_revision                TEXT NOT NULL,
    plan_kind                      TEXT NOT NULL,
    plan_revision                  BIGINT NOT NULL DEFAULT 0,
    advances_cursor                BOOLEAN NOT NULL,
    previous_materialized_through  TIMESTAMPTZ,
    correction_group_id            UUID,
    period_start                   TIMESTAMPTZ NOT NULL,
    period_end                     TIMESTAMPTZ NOT NULL,
    expected_event_count           INTEGER NOT NULL,
    payload_schema_version         INTEGER NOT NULL,
    hash_algorithm                 TEXT NOT NULL DEFAULT 'sha256',
    event_set_hash                 TEXT NOT NULL,
    rate_selection_hash            TEXT NOT NULL,
    creator_generation             BIGINT NOT NULL,
    state                          TEXT NOT NULL DEFAULT 'planned',
    attempt_count                  INTEGER NOT NULL DEFAULT 0,
    last_attempt_at                TIMESTAMPTZ,
    sanitized_error                JSONB,
    published_at                   TIMESTAMPTZ,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (
        source_interval_id, period_start, period_end,
        plan_kind, plan_revision
    ),
    CONSTRAINT resource_publication_plans_id_kind_start_uq
        UNIQUE (id, plan_kind, period_start),
    CONSTRAINT resource_publication_plans_interval_revision_fkey
        FOREIGN KEY (source_interval_id, source_revision)
        REFERENCES resource_intervals(id, source_revision)
        ON DELETE RESTRICT,
    CONSTRAINT resource_publication_plans_kind_check CHECK (
        plan_kind IN ('usage', 'late-usage', 'correction')
        AND ((plan_kind IN ('usage', 'late-usage')
                AND plan_revision = 0 AND advances_cursor
                AND previous_materialized_through IS NOT NULL
                AND correction_group_id IS NULL)
             OR (plan_kind = 'correction' AND plan_revision > 0
                AND NOT advances_cursor
                AND previous_materialized_through IS NULL
                AND correction_group_id = id))
    ),
    CONSTRAINT resource_publication_plans_period_check CHECK (
        period_end > period_start
        AND period_end <= date_trunc('day', period_start, 'UTC')
            + INTERVAL '1 day'
        AND (NOT advances_cursor
             OR previous_materialized_through = period_start)
    ),
    CONSTRAINT resource_publication_plans_payload_check CHECK (
        expected_event_count > 0 AND payload_schema_version > 0
        AND hash_algorithm = 'sha256'
        AND source_revision ~ '^[0-9a-f]{64}$'
        AND event_set_hash ~ '^[0-9a-f]{64}$'
        AND rate_selection_hash ~ '^[0-9a-f]{64}$'
        AND creator_generation > 0 AND attempt_count >= 0
        AND ((attempt_count = 0 AND last_attempt_at IS NULL)
             OR (attempt_count > 0 AND last_attempt_at IS NOT NULL))
        AND (last_attempt_at IS NULL OR last_attempt_at >= created_at)
        AND (sanitized_error IS NULL
             OR jsonb_typeof(sanitized_error) = 'object')
    ),
    CONSTRAINT resource_publication_plans_state_check CHECK (
        state IN ('planned', 'published', 'conflict')
        AND ((state = 'published' AND published_at IS NOT NULL
                AND published_at >= created_at)
             OR (state <> 'published' AND published_at IS NULL))
    )
);

CREATE INDEX resource_publication_plans_pending_idx
    ON resource_publication_plans (created_at, id)
    WHERE state = 'planned';

CREATE INDEX resource_publication_plans_interval_idx
    ON resource_publication_plans (source_interval_id, period_start);

CREATE TABLE resource_publication_plan_events (
    plan_id                    UUID NOT NULL,
    ordinal                    INTEGER NOT NULL,
    source                     TEXT NOT NULL,
    source_id                  TEXT NOT NULL,
    unit                       TEXT NOT NULL,
    ts                         TIMESTAMPTZ NOT NULL,
    event_kind                 TEXT NOT NULL,
    canonical_rate_version_id  UUID REFERENCES usage_rates_v2(id)
        ON DELETE RESTRICT,
    row_hash                   TEXT NOT NULL,
    event_payload              JSONB NOT NULL,

    PRIMARY KEY (plan_id, ordinal),
    UNIQUE (source, source_id, unit, ts),
    CONSTRAINT resource_publication_plan_events_plan_kind_time_fkey
        FOREIGN KEY (plan_id, event_kind, ts)
        REFERENCES resource_publication_plans(id, plan_kind, period_start)
        ON DELETE RESTRICT,
    CONSTRAINT resource_publication_plan_events_shape_check CHECK (
        ordinal >= 0 AND source_id <> '' AND unit <> ''
        AND event_kind IN ('usage', 'late-usage', 'correction')
        AND ((event_kind IN ('usage', 'late-usage')
                AND source = 'infra-allocation-v2')
             OR (event_kind = 'correction'
                AND source = 'infra-allocation-correction-v2'))
        AND row_hash ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(event_payload) = 'object'
    )
);

-- -------------------------------------------------------------------------
-- Day sealing, typed daily read model, and bootstrap gate. usage_daily_v2 is a
-- companion; legacy usage_daily and its conflict target remain untouched.
-- -------------------------------------------------------------------------
CREATE TABLE infra_usage_day_state (
    day                DATE PRIMARY KEY,
    state              TEXT NOT NULL DEFAULT 'open',
    coverage_status    TEXT,
    coverage_revision  TEXT,
    unknown_ranges     JSONB NOT NULL DEFAULT '[]'::JSONB,
    sealed_at          TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT infra_usage_day_state_shape_check CHECK (
        state IN ('open', 'sealing', 'sealed')
        AND (coverage_status IS NULL
             OR coverage_status IN ('complete', 'partial'))
        AND (coverage_status IS NULL) = (coverage_revision IS NULL)
        AND (coverage_revision IS NULL OR coverage_revision <> '')
        AND jsonb_typeof(unknown_ranges) = 'array'
        AND ((state = 'sealed' AND coverage_status IS NOT NULL
                AND coverage_revision IS NOT NULL AND sealed_at IS NOT NULL)
             OR (state <> 'sealed' AND sealed_at IS NULL))
    )
);

CREATE TABLE usage_daily_v2 (
    day                    DATE NOT NULL,
    user_id                UUID,
    project_id             UUID,
    category               TEXT NOT NULL,
    resource               TEXT NOT NULL,
    unit                   TEXT NOT NULL,
    measurement_basis      TEXT NOT NULL,
    resource_class         TEXT NOT NULL,
    attribution_scope      TEXT NOT NULL,
    cost_domain            TEXT NOT NULL,
    measurement_algorithm  TEXT NOT NULL,
    quantity               NUMERIC(38, 18) NOT NULL,
    cost_usd               NUMERIC(38, 18),
    priced_quantity        NUMERIC(38, 18) NOT NULL,
    unpriced_quantity      NUMERIC(38, 18) NOT NULL,
    priced_events          BIGINT NOT NULL,
    unpriced_events        BIGINT NOT NULL,
    events                 BIGINT NOT NULL,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT usage_daily_v2_dimension_check CHECK (
        category <> '' AND resource <> '' AND unit <> ''
        AND measurement_basis <> '' AND resource_class <> ''
        AND attribution_scope IN ('customer', 'shared-platform', 'unknown')
        AND cost_domain <> '' AND measurement_algorithm <> ''
        AND ((attribution_scope = 'customer'
                AND (user_id IS NOT NULL OR project_id IS NOT NULL))
             OR (attribution_scope IN ('shared-platform', 'unknown')
                AND user_id IS NULL AND project_id IS NULL))
    ),
    CONSTRAINT usage_daily_v2_coverage_check CHECK (
        priced_events >= 0 AND unpriced_events >= 0 AND events >= 0
        AND events = priced_events + unpriced_events
        AND quantity = priced_quantity + unpriced_quantity
        AND quantity NOT IN (
            'NaN'::NUMERIC, 'Infinity'::NUMERIC, '-Infinity'::NUMERIC
        )
        AND priced_quantity NOT IN (
            'NaN'::NUMERIC, 'Infinity'::NUMERIC, '-Infinity'::NUMERIC
        )
        AND unpriced_quantity NOT IN (
            'NaN'::NUMERIC, 'Infinity'::NUMERIC, '-Infinity'::NUMERIC
        )
        AND (cost_usd IS NULL OR cost_usd NOT IN (
            'NaN'::NUMERIC, 'Infinity'::NUMERIC, '-Infinity'::NUMERIC
        ))
        AND ((priced_events = 0 AND cost_usd IS NULL)
             OR (priced_events > 0 AND cost_usd IS NOT NULL))
    )
);

CREATE UNIQUE INDEX usage_daily_v2_dims_uq
    ON usage_daily_v2 (
        day, user_id, project_id, category, resource, unit,
        measurement_basis, resource_class, attribution_scope, cost_domain,
        measurement_algorithm
    ) NULLS NOT DISTINCT;

CREATE INDEX usage_daily_v2_user_day_idx
    ON usage_daily_v2 (user_id, day) WHERE user_id IS NOT NULL;

CREATE INDEX usage_daily_v2_project_day_idx
    ON usage_daily_v2 (project_id, day) WHERE project_id IS NOT NULL;

CREATE TABLE usage_rollup_day_state (
    day                     DATE PRIMARY KEY,
    applied_audit_revision  BIGINT NOT NULL,
    coverage_status         TEXT NOT NULL,
    unknown_ranges          JSONB NOT NULL DEFAULT '[]'::JSONB,
    rolled_at               TIMESTAMPTZ NOT NULL,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT usage_rollup_day_state_shape_check CHECK (
        applied_audit_revision > 0
        AND coverage_status IN ('complete', 'partial', 'unavailable')
        AND jsonb_typeof(unknown_ranges) = 'array'
    )
);

CREATE TABLE usage_rollup_v2_bootstrap_state (
    singleton               BOOLEAN PRIMARY KEY DEFAULT TRUE,
    status                  TEXT NOT NULL DEFAULT 'pending',
    seeded_through_day      DATE,
    reconciled_through_day  DATE,
    started_at              TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ,
    sanitized_error         JSONB,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT usage_rollup_v2_bootstrap_singleton_check CHECK (singleton),
    CONSTRAINT usage_rollup_v2_bootstrap_shape_check CHECK (
        status IN ('pending', 'running', 'reconciling', 'complete', 'error')
        AND (sanitized_error IS NULL
             OR jsonb_typeof(sanitized_error) = 'object')
        AND (reconciled_through_day IS NULL
             OR (seeded_through_day IS NOT NULL
                 AND reconciled_through_day <= seeded_through_day))
        AND (
            (status = 'pending'
                AND started_at IS NULL
                AND seeded_through_day IS NULL
                AND reconciled_through_day IS NULL
                AND completed_at IS NULL)
            OR (status = 'running'
                AND started_at IS NOT NULL
                AND seeded_through_day IS NULL
                AND reconciled_through_day IS NULL
                AND completed_at IS NULL)
            OR (status = 'reconciling'
                AND started_at IS NOT NULL
                AND seeded_through_day IS NOT NULL
                AND completed_at IS NULL)
            OR (status = 'complete'
                AND started_at IS NOT NULL
                AND seeded_through_day IS NOT NULL
                AND reconciled_through_day = seeded_through_day
                AND completed_at IS NOT NULL
                AND completed_at >= started_at
                AND sanitized_error IS NULL)
            OR (status = 'error' AND completed_at IS NULL)
        )
    )
);

INSERT INTO usage_rollup_v2_bootstrap_state (singleton, status)
VALUES (TRUE, 'pending')
ON CONFLICT (singleton) DO NOTHING;

INSERT INTO rollup_state (name, last_closed_day)
VALUES ('usage_daily_v2', NULL)
ON CONFLICT (name) DO NOTHING;

-- Slice 0 has no late-publication workflow that can atomically dirty audit and
-- revise a seal, so a seal is fail-closed and frozen. A later slice may replace
-- this guard only alongside that reviewed cross-database revision protocol.
CREATE FUNCTION protect_infra_usage_day_state_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'open' THEN
            RAISE EXCEPTION
                'infrastructure usage day state must begin open'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'infrastructure usage day state cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'sealed' THEN
        RAISE EXCEPTION
            'sealed infrastructure usage days are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.day <> OLD.day
       OR NEW.updated_at < OLD.updated_at
       OR (OLD.state = 'open' AND NEW.state NOT IN ('open', 'sealing'))
       OR (OLD.state = 'sealing' AND NEW.state NOT IN ('sealing', 'sealed'))
    THEN
        RAISE EXCEPTION
            'infrastructure usage day state advances open to sealing to sealed'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER infra_usage_day_state_one_way_seal
BEFORE INSERT OR UPDATE OR DELETE ON infra_usage_day_state
FOR EACH ROW EXECUTE FUNCTION protect_infra_usage_day_state_mutation();

-- Snapshot rows are staged while bounded item batches arrive, then sealed once.
-- The seal is the only metadata finalization point and verifies the declared
-- count. A separate one-way state enables item expiry after the seven-day
-- minimum without pretending that the immutable snapshot metadata disappeared.
CREATE FUNCTION protect_resource_inventory_snapshot_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    actual_count BIGINT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.manifest_state = 'staging' THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'inventory snapshots must begin in the staging state'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.manifest_state = 'staging'
       AND NEW.manifest_state = 'sealed'
       AND NEW.sealed_at IS NOT NULL
       AND NEW.items_expired_at IS NULL
       AND NEW.id = OLD.id
       AND NEW.scope_epoch_id = OLD.scope_epoch_id
       AND NEW.inventory_scope_id = OLD.inventory_scope_id
       AND NEW.collection_started_at = OLD.collection_started_at
       AND NEW.created_at = OLD.created_at THEN
        SELECT count(*)
        INTO actual_count
        FROM public.resource_inventory_snapshot_items
        WHERE snapshot_id = NEW.id;

        IF actual_count <> NEW.item_count THEN
            RAISE EXCEPTION
                'snapshot % declares % items but has %',
                NEW.id, NEW.item_count, actual_count
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.manifest_state = 'sealed'
       AND NEW.manifest_state = 'items-expired'
       AND NEW.items_expired_at IS NOT NULL
       AND NEW.items_expired_at <= statement_timestamp()
       AND OLD.collection_completed_at
           <= statement_timestamp() - INTERVAL '7 days'
       AND (to_jsonb(NEW) - 'manifest_state' - 'items_expired_at')
           = (to_jsonb(OLD) - 'manifest_state' - 'items_expired_at') THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'snapshot metadata may only be finalized once or mark sealed items expired'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER resource_inventory_snapshots_seal_only
BEFORE INSERT OR UPDATE ON resource_inventory_snapshots
FOR EACH ROW
EXECUTE FUNCTION protect_resource_inventory_snapshot_mutation();

CREATE FUNCTION protect_resource_inventory_snapshot_item_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    old_state TEXT;
    new_state TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT manifest_state INTO new_state
        FROM public.resource_inventory_snapshots
        WHERE id = NEW.snapshot_id
        FOR UPDATE;
        IF new_state = 'staging' THEN
            RETURN NEW;
        END IF;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.snapshot_id <> OLD.snapshot_id THEN
            RAISE EXCEPTION
                'snapshot items cannot move between manifests'
                USING ERRCODE = '55000';
        END IF;
        SELECT manifest_state INTO old_state
        FROM public.resource_inventory_snapshots
        WHERE id = OLD.snapshot_id
        FOR UPDATE;
        SELECT manifest_state INTO new_state
        FROM public.resource_inventory_snapshots
        WHERE id = NEW.snapshot_id
        FOR UPDATE;
        IF old_state = 'staging' AND new_state = 'staging' THEN
            RETURN NEW;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        SELECT manifest_state INTO old_state
        FROM public.resource_inventory_snapshots
        WHERE id = OLD.snapshot_id
        FOR UPDATE;
        IF old_state = 'items-expired' THEN
            RETURN OLD;
        END IF;
    END IF;

    RAISE EXCEPTION
        'sealed inventory snapshot items are immutable'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER resource_inventory_snapshot_items_staging_only
BEFORE INSERT OR UPDATE OR DELETE ON resource_inventory_snapshot_items
FOR EACH ROW
EXECUTE FUNCTION protect_resource_inventory_snapshot_item_mutation();

CREATE FUNCTION validate_inventory_epoch_last_complete_snapshot()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.last_complete_snapshot_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM public.resource_inventory_snapshots snapshot
           WHERE snapshot.id = NEW.last_complete_snapshot_id
             AND snapshot.scope_epoch_id = NEW.id
             AND snapshot.complete = TRUE
             AND snapshot.manifest_state IN ('sealed', 'items-expired')
       ) THEN
        RAISE EXCEPTION
            'last_complete_snapshot_id must reference a sealed complete snapshot in this epoch'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_inventory_scope_epochs_complete_snapshot
BEFORE INSERT OR UPDATE OF last_complete_snapshot_id
ON resource_inventory_scope_epochs
FOR EACH ROW
EXECUTE FUNCTION validate_inventory_epoch_last_complete_snapshot();

-- Scope identity is exact, including NULL for a reviewed cluster-scoped
-- collector. A normal composite FK cannot compare NULL namespaces under MATCH
-- SIMPLE, so retain the FK for deletion/cluster integrity and close the nullable
-- namespace hole with a small indexed trigger check.
CREATE FUNCTION validate_resource_interval_scope_identity()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.resource_inventory_scopes scope
        WHERE scope.id = NEW.inventory_scope_id
          AND scope.source_cluster = NEW.source_cluster
          AND scope.namespace IS NOT DISTINCT FROM NEW.namespace
    ) THEN
        RAISE EXCEPTION
            'interval inventory scope does not match cluster/namespace identity'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.last_seen_snapshot_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM public.resource_inventory_snapshots snapshot
           WHERE snapshot.id = NEW.last_seen_snapshot_id
             AND snapshot.inventory_scope_id = NEW.inventory_scope_id
             AND snapshot.complete = TRUE
             AND snapshot.manifest_state IN ('sealed', 'items-expired')
       ) THEN
        RAISE EXCEPTION
            'last_seen_snapshot_id must reference a sealed complete snapshot in the interval scope'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_intervals_scope_identity
BEFORE INSERT OR UPDATE OF inventory_scope_id, source_cluster, namespace,
    last_seen_snapshot_id
ON resource_intervals
FOR EACH ROW EXECUTE FUNCTION validate_resource_interval_scope_identity();

-- Event-affecting interval identity, dimensions, capacity, attribution, and
-- provenance are immutable within a revision. Liveness and publication cursors
-- may move only forward; closure metadata is a one-way transition. A closed
-- interval still permits its materialization cursor to catch up to ended_at.
CREATE FUNCTION protect_resource_interval_revision_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'resource interval revisions are retained and cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF (to_jsonb(NEW)
            - 'ended_at' - 'end_time_source' - 'end_uncertainty_us'
            - 'end_reason' - 'last_seen_at' - 'last_confirmed_at'
            - 'last_seen_snapshot_id' - 'materialized_through' - 'updated_at')
       <> (to_jsonb(OLD)
            - 'ended_at' - 'end_time_source' - 'end_uncertainty_us'
            - 'end_reason' - 'last_seen_at' - 'last_confirmed_at'
            - 'last_seen_snapshot_id' - 'materialized_through' - 'updated_at') THEN
        RAISE EXCEPTION
            'event-affecting interval revision fields are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.last_seen_at < OLD.last_seen_at
       OR NEW.last_confirmed_at < OLD.last_confirmed_at
       OR NEW.materialized_through < OLD.materialized_through
       OR NEW.updated_at < OLD.updated_at
       OR (OLD.last_seen_snapshot_id IS NOT NULL
           AND NEW.last_seen_snapshot_id IS NULL) THEN
        RAISE EXCEPTION
            'interval liveness and materialization cursors are monotonic'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.ended_at IS NOT NULL THEN
        IF NEW.ended_at IS DISTINCT FROM OLD.ended_at
           OR NEW.end_time_source IS DISTINCT FROM OLD.end_time_source
           OR NEW.end_uncertainty_us IS DISTINCT FROM OLD.end_uncertainty_us
           OR NEW.end_reason IS DISTINCT FROM OLD.end_reason
           OR NEW.last_seen_at IS DISTINCT FROM OLD.last_seen_at
           OR NEW.last_confirmed_at IS DISTINCT FROM OLD.last_confirmed_at
           OR NEW.last_seen_snapshot_id IS DISTINCT FROM OLD.last_seen_snapshot_id
        THEN
            RAISE EXCEPTION
                'closed interval evidence and end metadata are immutable'
                USING ERRCODE = '55000';
        END IF;
    ELSIF NEW.ended_at IS NULL THEN
        IF NEW.end_time_source IS NOT NULL
           OR NEW.end_uncertainty_us IS NOT NULL
           OR NEW.end_reason IS NOT NULL THEN
            RAISE EXCEPTION
                'open intervals cannot carry end metadata'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_intervals_immutable_revision
BEFORE UPDATE OR DELETE ON resource_intervals
FOR EACH ROW EXECUTE FUNCTION protect_resource_interval_revision_mutation();

-- A committed plan is a complete frozen event manifest. Parent + children may
-- be built in one transaction; every later append is rejected by the same
-- deferred count/ordinal check. State and retry diagnostics are the only
-- mutable parent fields, and published/conflict are terminal states.
CREATE FUNCTION validate_resource_publication_plan_manifest()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_plan_id UUID;
    expected_count INTEGER;
    actual_count BIGINT;
    minimum_ordinal INTEGER;
    maximum_ordinal INTEGER;
BEGIN
    IF TG_TABLE_NAME = 'resource_publication_plans' THEN
        target_plan_id := NEW.id;
    ELSE
        target_plan_id := NEW.plan_id;
    END IF;

    SELECT plan.expected_event_count
    INTO expected_count
    FROM public.resource_publication_plans plan
    WHERE plan.id = target_plan_id;

    SELECT count(*), min(event.ordinal), max(event.ordinal)
    INTO actual_count, minimum_ordinal, maximum_ordinal
    FROM public.resource_publication_plan_events event
    WHERE event.plan_id = target_plan_id;

    IF expected_count IS NULL
       OR actual_count <> expected_count
       OR minimum_ordinal <> 0
       OR maximum_ordinal <> expected_count - 1 THEN
        RAISE EXCEPTION
            'publication plan % declares % contiguous events but has count %, ordinals %..%',
            target_plan_id, expected_count, actual_count,
            minimum_ordinal, maximum_ordinal
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION protect_resource_publication_plan_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    actual_count BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.state = 'published' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'only published plans may enter the reviewed retention cleanup path'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state <> 'planned' THEN
        RAISE EXCEPTION
            'published and conflict publication plans are terminal'
            USING ERRCODE = '55000';
    END IF;

    IF (to_jsonb(NEW)
            - 'state' - 'attempt_count' - 'last_attempt_at'
            - 'sanitized_error' - 'published_at')
       <> (to_jsonb(OLD)
            - 'state' - 'attempt_count' - 'last_attempt_at'
            - 'sanitized_error' - 'published_at') THEN
        RAISE EXCEPTION
            'publication plan intent and hashes are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.attempt_count < OLD.attempt_count
       OR (OLD.last_attempt_at IS NOT NULL AND NEW.last_attempt_at IS NULL)
       OR (OLD.last_attempt_at IS NOT NULL
           AND NEW.last_attempt_at < OLD.last_attempt_at)
       OR (NEW.attempt_count > OLD.attempt_count
           AND NEW.last_attempt_at IS NULL)
       OR (NEW.attempt_count = OLD.attempt_count
           AND NEW.last_attempt_at IS DISTINCT FROM OLD.last_attempt_at) THEN
        RAISE EXCEPTION
            'publication attempt state must advance monotonically'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.state = 'published' THEN
        SELECT count(*)
        INTO actual_count
        FROM public.resource_publication_plan_events event
        WHERE event.plan_id = NEW.id;
        IF actual_count <> NEW.expected_event_count THEN
            RAISE EXCEPTION
                'publication plan % cannot publish an incomplete manifest', NEW.id
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE FUNCTION protect_resource_publication_plan_event_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    parent_state TEXT;
    target_plan_id UUID;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'publication plan events are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'DELETE' THEN
        target_plan_id := OLD.plan_id;
    ELSE
        target_plan_id := NEW.plan_id;
    END IF;

    SELECT plan.state
    INTO parent_state
    FROM public.resource_publication_plans plan
    WHERE plan.id = target_plan_id
    FOR UPDATE;

    IF TG_OP = 'INSERT' AND parent_state = 'planned' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' AND parent_state = 'published' THEN
        RETURN OLD;
    END IF;

    RAISE EXCEPTION
        'plan events may be inserted while planned and deleted only for published retention cleanup'
        USING ERRCODE = '55000';
END;
$$;

CREATE CONSTRAINT TRIGGER resource_publication_plans_manifest_complete
AFTER INSERT ON resource_publication_plans
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_resource_publication_plan_manifest();

CREATE CONSTRAINT TRIGGER resource_publication_plan_events_manifest_complete
AFTER INSERT ON resource_publication_plan_events
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_resource_publication_plan_manifest();

CREATE TRIGGER resource_publication_plans_frozen_intent
BEFORE UPDATE OR DELETE ON resource_publication_plans
FOR EACH ROW EXECUTE FUNCTION protect_resource_publication_plan_mutation();

CREATE TRIGGER resource_publication_plan_events_frozen
BEFORE INSERT OR UPDATE OR DELETE ON resource_publication_plan_events
FOR EACH ROW EXECUTE FUNCTION protect_resource_publication_plan_event_mutation();

-- Rate terms are immutable. The sole permitted update is closing a previously
-- open effective range once; a successor can then be inserted in the same
-- transaction without weakening the no-overlap exclusion constraint.
CREATE FUNCTION protect_usage_rates_v2_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.effective_to IS NULL
       AND NEW.effective_to IS NOT NULL
       AND (to_jsonb(NEW) - 'effective_to')
           = (to_jsonb(OLD) - 'effective_to') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        'usage_rates_v2 terms are immutable; close once and insert a successor'
        USING ERRCODE = '55000';
END;
$$;

CREATE FUNCTION protect_usage_rate_card_version_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.provider_effective_to IS NULL
       AND NEW.provider_effective_to IS NOT NULL
       AND (to_jsonb(NEW) - 'provider_effective_to')
           = (to_jsonb(OLD) - 'provider_effective_to') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        'usage rate-card version terms are immutable; close once and insert a successor'
        USING ERRCODE = '55000';
END;
$$;

CREATE FUNCTION reject_usage_rate_component_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION
        'usage rate-card components are immutable; insert a successor version'
        USING ERRCODE = '55000';
END;
$$;

-- A version declares its complete ordered component manifest before any row is
-- visible. Deferred checks let parent + children be inserted in one transaction,
-- reject an incomplete commit, and reject any later append without a mutable
-- publication flag or race-prone application convention.
CREATE FUNCTION validate_usage_rate_card_component_count()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_version_id UUID;
    expected_count INTEGER;
    actual_count BIGINT;
BEGIN
    IF TG_TABLE_NAME = 'usage_rate_card_versions_v2' THEN
        target_version_id := NEW.id;
    ELSE
        target_version_id := NEW.version_id;
    END IF;

    SELECT component_count
    INTO expected_count
    FROM public.usage_rate_card_versions_v2
    WHERE id = target_version_id;

    SELECT count(*)
    INTO actual_count
    FROM public.usage_rate_components_v2
    WHERE version_id = target_version_id;

    IF expected_count IS NULL OR actual_count <> expected_count THEN
        RAISE EXCEPTION
            'rate-card version % declares % components but has %',
            target_version_id, expected_count, actual_count
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER usage_rate_card_versions_v2_manifest_complete
AFTER INSERT ON usage_rate_card_versions_v2
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_usage_rate_card_component_count();

CREATE CONSTRAINT TRIGGER usage_rate_components_v2_manifest_complete
AFTER INSERT ON usage_rate_components_v2
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_usage_rate_card_component_count();

CREATE TRIGGER usage_rates_v2_immutable
BEFORE UPDATE OR DELETE ON usage_rates_v2
FOR EACH ROW EXECUTE FUNCTION protect_usage_rates_v2_mutation();

CREATE TRIGGER usage_rate_card_versions_v2_immutable
BEFORE UPDATE OR DELETE ON usage_rate_card_versions_v2
FOR EACH ROW EXECUTE FUNCTION protect_usage_rate_card_version_mutation();

CREATE TRIGGER usage_rate_components_v2_immutable
BEFORE UPDATE OR DELETE ON usage_rate_components_v2
FOR EACH ROW EXECUTE FUNCTION reject_usage_rate_component_mutation();

COMMENT ON TABLE resource_inventory_scopes IS
    'Stable metering inventory scope identity; effective requirements live in scope epochs.';
COMMENT ON TABLE resource_intervals IS
    'App-owned allocation interval revisions; immutable capacity/dimensions with mutable liveness/materialization cursor.';
COMMENT ON TABLE resource_publication_plans IS
    'Irrevocable app-side outbox plans frozen before cross-database audit publication.';
COMMENT ON TABLE usage_daily_v2 IS
    'Typed UTC daily usage read model with explicit priced/unpriced coverage; rebuilt from immutable audit events.';
COMMENT ON TABLE usage_rates_v2 IS
    'Immutable exact-selector canonical USD ledger rates; absence means unpriced.';
COMMENT ON TABLE usage_rate_card_versions_v2 IS
    'Immutable public-cloud comparison card versions and calculator applicability.';

COMMIT;
