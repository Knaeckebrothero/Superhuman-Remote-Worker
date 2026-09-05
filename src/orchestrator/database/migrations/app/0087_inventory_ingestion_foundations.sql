-- migration:     0087_inventory_ingestion_foundations.sql
-- description:   Slice 1 app-DB ingestion foundations: one-time collector
--                tickets, database-enforced generation fencing, scope-oriented
--                reconciliation lookup, and immutable workspace shadow
--                comparison diagnostics, plus bounded diagnostic retention.
--                No audit publication or cutover.
-- depends-on:    0086_infrastructure_metering_foundations.sql
-- expected:      < 2s while Slice 0 tables are dark/empty. The interval index
--                is proportional if operators populated resource_intervals
--                before enabling Slice 1.
-- locks:         Brief ACCESS EXCLUSIVE locks on the dark Slice 0 tables while
--                adding metadata/triggers; SHARE lock while building the new
--                open-interval lookup index.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '10min';
SET LOCAL timezone                            = 'UTC';

-- Only a digest of the bearer nonce is retained. A ticket is issued for one
-- exact scope epoch and leader generation, binds to one snapshot, then is
-- consumed in the same transaction that seals/reconciles that snapshot.
CREATE TABLE resource_inventory_ingest_tickets (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    nonce_hash         TEXT NOT NULL UNIQUE,
    scope_epoch_id     UUID NOT NULL
        REFERENCES resource_inventory_scope_epochs(id) ON DELETE RESTRICT,
    leader_generation  BIGINT NOT NULL,
    request_digest     TEXT NOT NULL,
    max_snapshot_items INTEGER NOT NULL,
    max_snapshot_bytes BIGINT NOT NULL,
    staged_bytes       BIGINT NOT NULL DEFAULT 0,
    expires_at         TIMESTAMPTZ NOT NULL,
    bound_snapshot_id  UUID,
    bound_at           TIMESTAMPTZ,
    consumed_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT resource_inventory_ingest_tickets_hash_check CHECK (
        nonce_hash ~ '^[0-9a-f]{64}$'
        AND request_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT resource_inventory_ingest_tickets_generation_check
        CHECK (
            leader_generation > 0
            AND max_snapshot_items > 0
            AND max_snapshot_bytes > 0
            AND staged_bytes >= 0
            AND staged_bytes <= max_snapshot_bytes
        ),
    CONSTRAINT resource_inventory_ingest_tickets_time_check CHECK (
        expires_at > created_at
        AND (bound_at IS NULL OR bound_at >= created_at)
        AND (consumed_at IS NULL OR consumed_at >= bound_at)
        AND (consumed_at IS NULL OR consumed_at <= expires_at)
    ),
    CONSTRAINT resource_inventory_ingest_tickets_state_check CHECK (
        (bound_snapshot_id IS NULL AND bound_at IS NULL
            AND consumed_at IS NULL)
        OR (bound_snapshot_id IS NOT NULL AND bound_at IS NOT NULL)
    ),
    CONSTRAINT resource_inventory_ingest_tickets_id_scope_uq
        UNIQUE (id, scope_epoch_id)
);

CREATE INDEX resource_inventory_ingest_tickets_expiry_idx
    ON resource_inventory_ingest_tickets (expires_at, id)
    WHERE consumed_at IS NULL;

-- Recovery after an expired WATCH starts a new active epoch while preserving
-- the predecessor's unresolved historical gap.  A successful recovery LIST
-- promotes the new epoch and closes only the gap range, never its resolution.
ALTER TABLE resource_inventory_scope_epochs
    ADD COLUMN recovery_from_epoch_id UUID
        REFERENCES resource_inventory_scope_epochs(id) ON DELETE RESTRICT,
    ADD COLUMN require_after_recovery BOOLEAN NOT NULL DEFAULT FALSE,
    ADD CONSTRAINT resource_inventory_scope_epochs_recovery_uq
        UNIQUE (recovery_from_epoch_id),
    ADD CONSTRAINT resource_inventory_scope_epochs_recovery_shape_check CHECK (
        (recovery_from_epoch_id IS NULL AND NOT require_after_recovery)
        OR (recovery_from_epoch_id IS NOT NULL
            AND recovery_from_epoch_id <> id)
    );

-- HMAC request nonces and bearer grant hashes solve different replay problems.
-- Every authenticated HTTP request claims one (collector, nonce) here in the
-- same transaction as its side effects.  Rows live through the configured
-- replay window and are then removed only by a bounded cleanup operation.
CREATE TABLE resource_inventory_transport_nonces (
    collector_id       TEXT NOT NULL,
    request_nonce      UUID NOT NULL,
    request_kind       TEXT NOT NULL,
    request_digest     TEXT NOT NULL,
    scope_epoch_id     UUID NOT NULL
        REFERENCES resource_inventory_scope_epochs(id) ON DELETE RESTRICT,
    leader_generation  BIGINT NOT NULL,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (collector_id, request_nonce),
    CONSTRAINT resource_inventory_transport_nonces_identity_check CHECK (
        collector_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
        AND request_kind ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
        AND request_digest ~ '^[0-9a-f]{64}$'
        AND leader_generation > 0
    ),
    CONSTRAINT resource_inventory_transport_nonces_time_check
        CHECK (expires_at > received_at)
);

CREATE INDEX resource_inventory_transport_nonces_expiry_idx
    ON resource_inventory_transport_nonces (expires_at, collector_id, request_nonce);

-- WATCH is deliberately not represented as a sequence of partial LIST
-- snapshots.  One bounded session grant may commit several strictly ordered
-- events, while each event has its own immutable idempotency key and request
-- digest.  The bearer token is retained only as a SHA-256 hash.
CREATE TABLE resource_inventory_watch_sessions (
    id                         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    nonce_hash                 TEXT NOT NULL UNIQUE,
    scope_epoch_id             UUID NOT NULL
        REFERENCES resource_inventory_scope_epochs(id) ON DELETE RESTRICT,
    leader_generation          BIGINT NOT NULL,
    request_digest             TEXT NOT NULL,
    starting_resource_version  TEXT NOT NULL,
    last_resource_version      TEXT NOT NULL,
    max_events                 INTEGER NOT NULL,
    max_bytes                  BIGINT NOT NULL,
    committed_events           INTEGER NOT NULL DEFAULT 0,
    committed_bytes            BIGINT NOT NULL DEFAULT 0,
    expires_at                 TIMESTAMPTZ NOT NULL,
    termination_reason         TEXT,
    consumed_at                TIMESTAMPTZ,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT resource_inventory_watch_sessions_id_epoch_uq
        UNIQUE (id, scope_epoch_id),
    CONSTRAINT resource_inventory_watch_sessions_hash_check CHECK (
        nonce_hash ~ '^[0-9a-f]{64}$'
        AND request_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT resource_inventory_watch_sessions_cursor_check CHECK (
        starting_resource_version <> ''
        AND starting_resource_version <> '0'
        AND last_resource_version <> ''
        AND last_resource_version <> '0'
    ),
    CONSTRAINT resource_inventory_watch_sessions_bounds_check CHECK (
        leader_generation > 0
        AND max_events > 0
        AND max_bytes > 0
        AND committed_events >= 0
        AND committed_events <= max_events
        AND committed_bytes >= 0
        AND committed_bytes <= max_bytes
    ),
    CONSTRAINT resource_inventory_watch_sessions_time_check CHECK (
        expires_at > created_at
        AND (consumed_at IS NULL OR consumed_at >= created_at)
        AND (consumed_at IS NULL OR consumed_at <= expires_at)
    ),
    CONSTRAINT resource_inventory_watch_sessions_state_check CHECK (
        (consumed_at IS NULL AND termination_reason IS NULL)
        OR (consumed_at IS NOT NULL AND termination_reason IN (
            'completed', 'limit-reached', 'history-lost'
        ))
    )
);

CREATE INDEX resource_inventory_watch_sessions_live_idx
    ON resource_inventory_watch_sessions (expires_at, id)
    WHERE consumed_at IS NULL;

-- Snapshot admission checks this partial set while holding the scope epoch.
-- Keep the abandoned-WATCH recovery barrier bounded as terminal history grows.
CREATE INDEX resource_inventory_watch_sessions_live_scope_idx
    ON resource_inventory_watch_sessions (scope_epoch_id, created_at, id)
    WHERE consumed_at IS NULL;

ALTER TABLE resource_inventory_snapshots
    ADD COLUMN ingest_ticket_id UUID,
    ADD COLUMN reconciliation_summary JSONB;

-- Collector collection times are remote evidence and may lead the app DB by
-- the explicitly bounded skew enforced by InventoryStore.  received_at is the
-- authoritative PostgreSQL receipt clock and therefore is not ordered against
-- a remote clock by a database CHECK.
ALTER TABLE resource_inventory_snapshots
    DROP CONSTRAINT resource_inventory_snapshots_time_check,
    ADD CONSTRAINT resource_inventory_snapshots_time_check CHECK (
        collection_completed_at >= collection_started_at
        AND (sealed_at IS NULL OR sealed_at >= received_at)
    );

-- A crashed LIST may leave a bound manifest in staging forever. Preserve its
-- immutable metadata while making the abandoned payload explicitly eligible
-- for bounded removal; never disguise that manifest as a completed LIST.
ALTER TABLE resource_inventory_snapshots
    DROP CONSTRAINT resource_inventory_snapshots_manifest_state_check,
    ADD CONSTRAINT resource_inventory_snapshots_manifest_state_check CHECK (
        (manifest_state = 'staging'
            AND sealed_at IS NULL AND items_expired_at IS NULL)
        OR (manifest_state = 'sealed'
            AND sealed_at IS NOT NULL AND items_expired_at IS NULL)
        OR (manifest_state = 'items-expired'
            AND sealed_at IS NOT NULL AND items_expired_at IS NOT NULL
            AND items_expired_at >= sealed_at)
        OR (manifest_state = 'staging-expired'
            AND NOT complete
            AND sealed_at IS NULL AND items_expired_at IS NOT NULL
            AND items_expired_at >= created_at)
    );

ALTER TABLE resource_inventory_snapshots
    ADD CONSTRAINT resource_inventory_snapshots_ingest_ticket_uq
        UNIQUE (ingest_ticket_id),
    ADD CONSTRAINT resource_inventory_snapshots_ingest_ticket_fkey
        FOREIGN KEY (ingest_ticket_id, scope_epoch_id)
        REFERENCES resource_inventory_ingest_tickets(id, scope_epoch_id)
        ON DELETE RESTRICT,
    ADD CONSTRAINT resource_inventory_snapshots_reconciliation_summary_check
        CHECK (
            (ingest_ticket_id IS NULL AND reconciliation_summary IS NULL)
            OR (manifest_state IN ('staging', 'staging-expired')
                AND reconciliation_summary IS NULL)
            OR (manifest_state IN ('sealed', 'items-expired')
                AND jsonb_typeof(reconciliation_summary) = 'object')
        );

ALTER TABLE resource_inventory_ingest_tickets
    ADD CONSTRAINT resource_inventory_ingest_tickets_snapshot_fkey
    FOREIGN KEY (bound_snapshot_id, scope_epoch_id)
    REFERENCES resource_inventory_snapshots(id, scope_epoch_id)
    ON DELETE RESTRICT;

-- Complete-list absence is a scope scan. The identity suffix also supports the
-- positive-presence join without materializing a 50k-UID array in Python.
CREATE INDEX resource_intervals_open_scope_identity_idx
    ON resource_intervals (inventory_scope_id, source_kind, source_uid)
    WHERE ended_at IS NULL;

-- Retention candidates are selected globally and oldest-first. Partial indexes
-- keep each SKIP LOCKED batch bounded without scanning retained authority.
CREATE INDEX resource_inventory_snapshots_sealed_retention_idx
    ON resource_inventory_snapshots (sealed_at, id)
    WHERE manifest_state = 'sealed';

CREATE INDEX resource_inventory_snapshots_staging_retention_idx
    ON resource_inventory_snapshots (created_at, id)
    WHERE manifest_state = 'staging';

CREATE INDEX resource_inventory_watch_sessions_retention_idx
    ON resource_inventory_watch_sessions (
        (COALESCE(consumed_at, expires_at)), id
    );

-- Object-by-object shadow evidence is deliberately structured: no free-form
-- error text can accidentally retain customer or Kubernetes object payloads.
CREATE TABLE resource_inventory_shadow_comparisons (
    id                         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    snapshot_id                UUID NOT NULL,
    inventory_scope_id         UUID NOT NULL,
    source_uid                 TEXT NOT NULL,
    owner_kind                 TEXT,
    owner_id                   UUID,
    owner_trusted              BOOLEAN NOT NULL DEFAULT FALSE,
    legacy_interval_id         BIGINT,
    legacy_cpu_millicores      BIGINT,
    legacy_memory_bytes        BIGINT,
    legacy_started_at          TIMESTAMPTZ,
    observed_cpu_millicores    BIGINT,
    observed_memory_bytes      BIGINT,
    observed_started_at        TIMESTAMPTZ,
    observed_start_time_source TEXT,
    observed_start_uncertainty_us BIGINT,
    start_delta_us             BIGINT,
    status                     TEXT NOT NULL,
    reason_code                TEXT NOT NULL,
    explained                  BOOLEAN NOT NULL DEFAULT FALSE,
    comparison_at              TIMESTAMPTZ NOT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT resource_inventory_shadow_comparisons_snapshot_fkey
        FOREIGN KEY (snapshot_id, inventory_scope_id)
        REFERENCES resource_inventory_snapshots(id, inventory_scope_id)
        ON DELETE RESTRICT,
    CONSTRAINT resource_inventory_shadow_comparisons_snapshot_uid_uq
        UNIQUE (snapshot_id, source_uid),
    CONSTRAINT resource_inventory_shadow_comparisons_uid_check
        CHECK (source_uid <> ''),
    CONSTRAINT resource_inventory_shadow_comparisons_owner_check CHECK (
        (owner_trusted AND owner_kind IN ('job', 'thread')
            AND owner_id IS NOT NULL)
        OR (NOT owner_trusted AND owner_kind IS NULL AND owner_id IS NULL)
    ),
    CONSTRAINT resource_inventory_shadow_comparisons_capacity_check CHECK (
        ((legacy_interval_id IS NULL
                AND legacy_cpu_millicores IS NULL
                AND legacy_memory_bytes IS NULL)
         OR (legacy_interval_id IS NOT NULL
                AND legacy_cpu_millicores IS NOT NULL
                AND legacy_memory_bytes IS NOT NULL
                AND legacy_cpu_millicores >= 0
                AND legacy_memory_bytes >= 0))
        AND (observed_cpu_millicores IS NULL
             OR observed_cpu_millicores >= 0)
        AND (observed_memory_bytes IS NULL
             OR observed_memory_bytes >= 0)
    ),
    CONSTRAINT resource_inventory_shadow_comparisons_lifetime_check CHECK (
        ((observed_started_at IS NULL
                AND observed_start_time_source IS NULL
                AND observed_start_uncertainty_us IS NULL)
         OR (observed_started_at IS NOT NULL
                AND observed_start_time_source IS NOT NULL
                AND observed_start_time_source <> ''
                AND observed_start_uncertainty_us IS NOT NULL
                AND observed_start_uncertainty_us >= 0))
        AND (start_delta_us IS NULL
             OR (legacy_started_at IS NOT NULL
                 AND observed_started_at IS NOT NULL
                 AND start_delta_us = (
                    extract(epoch FROM (
                        observed_started_at - legacy_started_at
                    )) * 1000000
                 )::BIGINT))
    ),
    CONSTRAINT resource_inventory_shadow_comparisons_status_check CHECK (
        status IN (
            'matched', 'capacity-mismatch', 'owner-mismatch',
            'legacy-missing', 'invalid-observation', 'not-applicable',
            'lifetime-mismatch'
        )
        AND reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
        AND (status <> 'matched' OR start_delta_us IS NULL
             OR start_delta_us = 0)
        AND (status <> 'lifetime-mismatch'
             OR (NOT explained
                 AND reason_code IN ('start-semantics', 'start-evidence-missing')
                 AND (start_delta_us IS NULL OR start_delta_us <> 0)))
    )
);

CREATE INDEX resource_inventory_shadow_comparisons_unresolved_idx
    ON resource_inventory_shadow_comparisons (
        inventory_scope_id, comparison_at DESC, snapshot_id
    )
    WHERE explained = FALSE;

CREATE INDEX resource_inventory_shadow_comparisons_latest_idx
    ON resource_inventory_shadow_comparisons (
        inventory_scope_id, source_uid, comparison_at DESC
    );

CREATE INDEX resource_inventory_shadow_comparisons_retention_idx
    ON resource_inventory_shadow_comparisons (comparison_at, id);

-- A committed WATCH row is the durable receipt for exactly one cursor CAS.
-- It contains only the normalized allowlist and structured diagnostics, never
-- a raw Kubernetes object or a free-form error message.
CREATE TABLE resource_inventory_watch_events (
    watch_session_id       UUID NOT NULL,
    id                     UUID NOT NULL,
    scope_epoch_id         UUID NOT NULL,
    ordinal                INTEGER NOT NULL,
    request_digest         TEXT NOT NULL,
    event_type             TEXT NOT NULL,
    expected_resource_version TEXT NOT NULL,
    resource_version       TEXT,
    source_kind            TEXT,
    source_uid             TEXT,
    revision_hash          TEXT,
    normalized_item        JSONB,
    valid_for_metering     BOOLEAN,
    item_error             JSONB,
    mutation_action        TEXT NOT NULL,
    affected_interval_id   UUID
        REFERENCES resource_intervals(id) ON DELETE RESTRICT,
    coverage_gap_id        UUID
        REFERENCES resource_inventory_coverage_gaps(id) ON DELETE RESTRICT,
    event_bytes            BIGINT NOT NULL,
    collector_observed_at  TIMESTAMPTZ,
    received_at            TIMESTAMPTZ NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (watch_session_id, id),
    CONSTRAINT resource_inventory_watch_events_session_fkey
        FOREIGN KEY (watch_session_id, scope_epoch_id)
        REFERENCES resource_inventory_watch_sessions(id, scope_epoch_id)
        ON DELETE RESTRICT,
    CONSTRAINT resource_inventory_watch_events_session_ordinal_uq
        UNIQUE (watch_session_id, ordinal),
    CONSTRAINT resource_inventory_watch_events_digest_check
        CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT resource_inventory_watch_events_common_check CHECK (
        ordinal > 0
        AND (event_bytes > 0
             OR (event_type = 'history-lost' AND event_bytes = 0))
        AND expected_resource_version <> ''
        AND expected_resource_version <> '0'
        AND (resource_version IS NULL
             OR (resource_version <> '' AND resource_version <> '0'))
        AND (revision_hash IS NULL
             OR revision_hash ~ '^[0-9a-f]{64}$')
        AND (normalized_item IS NULL
             OR jsonb_typeof(normalized_item) = 'object')
        AND (item_error IS NULL OR jsonb_typeof(item_error) = 'object')
    ),
    CONSTRAINT resource_inventory_watch_events_shape_check CHECK (
        (event_type IN ('added', 'modified')
            AND resource_version IS NOT NULL
            AND source_kind IN ('pod', 'vmi', 'pvc', 'volume')
            AND source_uid IS NOT NULL AND source_uid <> ''
            AND normalized_item IS NOT NULL
            AND valid_for_metering IS NOT NULL
            AND ((valid_for_metering AND revision_hash IS NOT NULL
                    AND item_error IS NULL
                    AND mutation_action IN (
                        'confirm', 'open', 'revise', 'not-applicable',
                        'close', 'already-absent'
                    ))
                 OR (NOT valid_for_metering AND item_error IS NOT NULL
                    AND mutation_action IN (
                        'presence-invalid', 'close', 'already-absent'
                    )))
            AND coverage_gap_id IS NULL)
        OR (event_type = 'deleted'
            AND resource_version IS NOT NULL
            AND source_kind IN ('pod', 'vmi', 'pvc', 'volume')
            AND source_uid IS NOT NULL AND source_uid <> ''
            AND revision_hash IS NULL AND normalized_item IS NULL
            AND valid_for_metering IS NULL AND item_error IS NULL
            AND mutation_action IN ('close', 'already-absent')
            AND coverage_gap_id IS NULL)
        OR (event_type = 'bookmark'
            AND resource_version IS NOT NULL
            AND source_kind IS NULL AND source_uid IS NULL
            AND revision_hash IS NULL AND normalized_item IS NULL
            AND valid_for_metering IS NULL AND item_error IS NULL
            AND mutation_action = 'bookmark'
            AND affected_interval_id IS NULL AND coverage_gap_id IS NULL)
        OR (event_type = 'history-lost'
            AND resource_version IS NULL
            AND source_kind IS NULL AND source_uid IS NULL
            AND revision_hash IS NULL AND normalized_item IS NULL
            AND valid_for_metering IS NULL AND item_error IS NULL
            AND mutation_action = 'history-gap'
            AND affected_interval_id IS NULL AND coverage_gap_id IS NOT NULL)
    ),
    CONSTRAINT resource_inventory_watch_events_interval_action_check CHECK (
        (mutation_action IN ('confirm', 'open', 'revise', 'close')
            AND affected_interval_id IS NOT NULL)
        OR (mutation_action = 'presence-invalid')
        OR (mutation_action IN (
                'not-applicable', 'already-absent', 'bookmark', 'history-gap'
            )
            AND affected_interval_id IS NULL)
    )
);

CREATE INDEX resource_inventory_watch_events_scope_uid_idx
    ON resource_inventory_watch_events (
        scope_epoch_id, source_kind, source_uid, received_at DESC
    )
    WHERE source_uid IS NOT NULL;

CREATE INDEX resource_inventory_watch_events_gap_idx
    ON resource_inventory_watch_events (coverage_gap_id)
    WHERE coverage_gap_id IS NOT NULL;

-- Stable, conservative accounting for cumulative normalized staging.  The
-- fixed allowance covers tuple/JSON framing; raw HTTP bodies have an
-- independent route-level streaming cap.
CREATE FUNCTION resource_inventory_snapshot_item_size_bytes(
    source_kind TEXT,
    source_uid TEXT,
    revision_hash TEXT,
    normalized_item JSONB,
    item_error JSONB
)
RETURNS BIGINT
LANGUAGE SQL
IMMUTABLE
SET search_path = pg_catalog
AS $$
    SELECT 64::BIGINT
         + octet_length(source_kind)::BIGINT
         + octet_length(source_uid)::BIGINT
         + COALESCE(octet_length(revision_hash), 0)::BIGINT
         + pg_column_size(normalized_item)::BIGINT
         + COALESCE(pg_column_size(item_error), 0)::BIGINT
$$;

-- Extend Slice 0's one-way manifest lifecycle with a distinct abandonment
-- terminal. The hard floors live in PostgreSQL as well as the service so an
-- accidental short retention value cannot erase evidence early.
CREATE OR REPLACE FUNCTION protect_resource_inventory_snapshot_mutation()
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
       AND OLD.sealed_at <= statement_timestamp() - INTERVAL '7 days'
       AND (to_jsonb(NEW) - 'manifest_state' - 'items_expired_at')
           = (to_jsonb(OLD) - 'manifest_state' - 'items_expired_at') THEN
        RETURN NEW;
    END IF;

    IF OLD.manifest_state = 'staging'
       AND NEW.manifest_state = 'staging-expired'
       AND NOT OLD.complete
       AND NOT NEW.complete
       AND NEW.sealed_at IS NULL
       AND NEW.items_expired_at IS NOT NULL
       AND NEW.items_expired_at <= statement_timestamp()
       AND OLD.created_at <= statement_timestamp() - INTERVAL '24 hours'
       AND (
            OLD.ingest_ticket_id IS NULL
            OR EXISTS (
                SELECT 1
                FROM public.resource_inventory_ingest_tickets ticket
                WHERE ticket.id = OLD.ingest_ticket_id
                  AND ticket.scope_epoch_id = OLD.scope_epoch_id
                  AND ticket.bound_snapshot_id = OLD.id
                  AND ticket.expires_at <= statement_timestamp()
            )
       )
       AND (to_jsonb(NEW) - 'manifest_state' - 'items_expired_at')
           = (to_jsonb(OLD) - 'manifest_state' - 'items_expired_at') THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'snapshot metadata may only be finalized once or enter an expiry terminal'
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION protect_resource_inventory_snapshot_item_mutation()
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
        IF old_state IN ('items-expired', 'staging-expired') THEN
            RETURN OLD;
        END IF;
    END IF;

    RAISE EXCEPTION
        'inventory snapshot items are immutable outside an expiry terminal'
        USING ERRCODE = '55000';
END;
$$;

-- The database generation never moves backwards or disappears. Service-local
-- deactivation therefore cannot make a delayed old leader current again.
CREATE FUNCTION protect_infra_metering_generation_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'infra metering control cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.singleton IS DISTINCT FROM OLD.singleton
       OR NEW.leader_generation < OLD.leader_generation
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'infra metering generation is monotonic'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER infra_metering_control_monotonic_generation
BEFORE UPDATE OR DELETE ON infra_metering_control
FOR EACH ROW EXECUTE FUNCTION protect_infra_metering_generation_mutation();

CREATE FUNCTION protect_resource_inventory_transport_nonce_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    current_generation BIGINT;
    epoch_retired_at TIMESTAMPTZ;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'inventory transport nonce claims are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.expires_at > statement_timestamp() THEN
            RAISE EXCEPTION 'live inventory transport nonce cannot be deleted'
                USING ERRCODE = '55000';
        END IF;
        RETURN OLD;
    END IF;

    SELECT leader_generation INTO current_generation
    FROM public.infra_metering_control WHERE singleton = TRUE;
    SELECT retired_at INTO epoch_retired_at
    FROM public.resource_inventory_scope_epochs WHERE id = NEW.scope_epoch_id;
    IF current_generation IS NULL
       OR epoch_retired_at IS NOT NULL
       OR NEW.leader_generation <> current_generation
       OR NEW.received_at > statement_timestamp()
       OR NEW.expires_at <= statement_timestamp() THEN
        RAISE EXCEPTION 'inventory transport nonce generation/scope fence failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_inventory_transport_nonces_immutable
BEFORE INSERT OR UPDATE OR DELETE ON resource_inventory_transport_nonces
FOR EACH ROW
EXECUTE FUNCTION protect_resource_inventory_transport_nonce_mutation();

-- Snapshot INSERT and the staging->sealed transition both require a live,
-- unconsumed ticket for the active scope epoch/current generation. The ticket
-- identity and stamped generation cannot be swapped during finalization.
CREATE FUNCTION enforce_resource_inventory_snapshot_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    current_generation BIGINT;
    epoch_retired_at TIMESTAMPTZ;
    ticket_generation BIGINT;
    ticket_expires_at TIMESTAMPTZ;
    ticket_bound_snapshot_id UUID;
    ticket_consumed_at TIMESTAMPTZ;
    ticket_max_items INTEGER;
    ticket_max_bytes BIGINT;
    ticket_staged_bytes BIGINT;
    actual_staged_bytes BIGINT;
BEGIN
    IF TG_OP = 'UPDATE'
       AND (
            (OLD.manifest_state = 'sealed'
                AND NEW.manifest_state = 'items-expired')
            OR (OLD.manifest_state = 'staging'
                AND NEW.manifest_state = 'staging-expired')
       ) THEN
        RETURN NEW;
    END IF;

    SELECT leader_generation INTO current_generation
    FROM public.infra_metering_control
    WHERE singleton = TRUE;

    SELECT retired_at INTO epoch_retired_at
    FROM public.resource_inventory_scope_epochs
    WHERE id = NEW.scope_epoch_id;

    SELECT leader_generation, expires_at, bound_snapshot_id, consumed_at,
           max_snapshot_items, max_snapshot_bytes, staged_bytes
    INTO ticket_generation, ticket_expires_at,
         ticket_bound_snapshot_id, ticket_consumed_at,
         ticket_max_items, ticket_max_bytes, ticket_staged_bytes
    FROM public.resource_inventory_ingest_tickets
    WHERE id = NEW.ingest_ticket_id
      AND scope_epoch_id = NEW.scope_epoch_id;

    IF current_generation IS NULL
       OR epoch_retired_at IS NOT NULL
       OR NEW.ingest_ticket_id IS NULL
       OR ticket_generation IS NULL
       OR NEW.leader_generation <> current_generation
       OR ticket_generation <> current_generation
       OR ticket_consumed_at IS NOT NULL
       OR ticket_expires_at <= statement_timestamp()
       OR (ticket_bound_snapshot_id IS NOT NULL
           AND ticket_bound_snapshot_id <> NEW.id) THEN
        RAISE EXCEPTION
            'snapshot ingestion ticket/generation/scope epoch fence failed'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'UPDATE'
       AND OLD.manifest_state = 'staging'
       AND NEW.manifest_state = 'sealed' THEN
        SELECT COALESCE(sum(
            public.resource_inventory_snapshot_item_size_bytes(
                item.source_kind, item.source_uid, item.revision_hash,
                item.normalized_item, item.item_error
            )
        ), 0)
        INTO actual_staged_bytes
        FROM public.resource_inventory_snapshot_items item
        WHERE item.snapshot_id = NEW.id;

        IF NEW.leader_generation IS DISTINCT FROM OLD.leader_generation
            OR NEW.ingest_ticket_id IS DISTINCT FROM OLD.ingest_ticket_id
            OR ticket_bound_snapshot_id IS DISTINCT FROM NEW.id
            OR jsonb_typeof(NEW.reconciliation_summary)
                IS DISTINCT FROM 'object'
            OR NEW.item_count > ticket_max_items
            OR ticket_staged_bytes <> actual_staged_bytes
            OR actual_staged_bytes > ticket_max_bytes THEN
            RAISE EXCEPTION 'snapshot fence identity/bounds failed at seal'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_inventory_snapshots_generation_fence
BEFORE INSERT OR UPDATE ON resource_inventory_snapshots
FOR EACH ROW EXECUTE FUNCTION enforce_resource_inventory_snapshot_fence();

-- Staged item batches are also fenced. This prevents a delayed old leader from
-- filling a manifest after a new generation has taken the lease.
CREATE FUNCTION enforce_resource_inventory_item_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_snapshot_id UUID;
    fence_ok BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    target_snapshot_id := NEW.snapshot_id;

    SELECT TRUE INTO fence_ok
    FROM public.resource_inventory_snapshots snapshot
    JOIN public.resource_inventory_scope_epochs epoch
      ON epoch.id = snapshot.scope_epoch_id
    JOIN public.resource_inventory_ingest_tickets ticket
      ON ticket.id = snapshot.ingest_ticket_id
     AND ticket.scope_epoch_id = snapshot.scope_epoch_id
    JOIN public.infra_metering_control control ON control.singleton = TRUE
    WHERE snapshot.id = target_snapshot_id
      AND snapshot.manifest_state = 'staging'
      AND epoch.retired_at IS NULL
      AND snapshot.leader_generation = control.leader_generation
      AND ticket.leader_generation = control.leader_generation
      AND ticket.bound_snapshot_id = snapshot.id
      AND ticket.consumed_at IS NULL
      AND ticket.expires_at > statement_timestamp();

    IF fence_ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'snapshot item ingestion fence failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_inventory_snapshot_items_generation_fence
BEFORE INSERT OR UPDATE ON resource_inventory_snapshot_items
FOR EACH ROW EXECUTE FUNCTION enforce_resource_inventory_item_fence();

-- Ticket rows are immutable except for the two one-way transitions:
-- unbound -> bound snapshot -> consumed by that same snapshot.
CREATE FUNCTION protect_resource_inventory_ingest_ticket_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    current_generation BIGINT;
    epoch_retired_at TIMESTAMPTZ;
    snapshot_ticket_id UUID;
    actual_staged_bytes BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.bound_snapshot_id IS NULL
           AND OLD.bound_at IS NULL
           AND OLD.consumed_at IS NULL
           AND OLD.expires_at <= statement_timestamp() THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'only expired unbound inventory ingest tickets may be deleted'
            USING ERRCODE = '55000';
    END IF;

    SELECT leader_generation INTO current_generation
    FROM public.infra_metering_control WHERE singleton = TRUE;
    SELECT retired_at INTO epoch_retired_at
    FROM public.resource_inventory_scope_epochs WHERE id = NEW.scope_epoch_id;

    IF current_generation IS NULL
       OR epoch_retired_at IS NOT NULL
       OR NEW.leader_generation <> current_generation THEN
        RAISE EXCEPTION 'inventory ingest ticket generation/scope fence failed'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.bound_snapshot_id IS NOT NULL
           OR NEW.bound_at IS NOT NULL
           OR NEW.consumed_at IS NOT NULL
           OR NEW.staged_bytes <> 0
           OR NEW.expires_at <= statement_timestamp() THEN
            RAISE EXCEPTION 'new inventory ingest ticket must be live and unbound'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF (to_jsonb(NEW)
            - 'bound_snapshot_id' - 'bound_at' - 'consumed_at' - 'staged_bytes')
       <> (to_jsonb(OLD)
            - 'bound_snapshot_id' - 'bound_at' - 'consumed_at' - 'staged_bytes') THEN
        RAISE EXCEPTION 'inventory ingest ticket request identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.bound_snapshot_id IS NULL
       AND OLD.bound_at IS NULL
       AND OLD.consumed_at IS NULL
       AND NEW.bound_snapshot_id IS NOT NULL
       AND NEW.bound_at IS NOT NULL
       AND NEW.consumed_at IS NULL
       AND NEW.staged_bytes = OLD.staged_bytes
       AND NEW.expires_at > statement_timestamp() THEN
        SELECT ingest_ticket_id INTO snapshot_ticket_id
        FROM public.resource_inventory_snapshots
        WHERE id = NEW.bound_snapshot_id
          AND scope_epoch_id = NEW.scope_epoch_id;
        IF snapshot_ticket_id = NEW.id THEN
            RETURN NEW;
        END IF;
    END IF;

    IF OLD.bound_snapshot_id IS NOT NULL
       AND NEW.bound_snapshot_id = OLD.bound_snapshot_id
       AND NEW.bound_at = OLD.bound_at
       AND OLD.consumed_at IS NULL
       AND NEW.consumed_at IS NULL
       AND NEW.staged_bytes >= OLD.staged_bytes
       AND NEW.staged_bytes <= NEW.max_snapshot_bytes
       AND NEW.expires_at > statement_timestamp() THEN
        SELECT COALESCE(sum(
            public.resource_inventory_snapshot_item_size_bytes(
                item.source_kind, item.source_uid, item.revision_hash,
                item.normalized_item, item.item_error
            )
        ), 0)
        INTO actual_staged_bytes
        FROM public.resource_inventory_snapshot_items item
        WHERE item.snapshot_id = NEW.bound_snapshot_id;
        IF actual_staged_bytes = NEW.staged_bytes THEN
            RETURN NEW;
        END IF;
    END IF;

    IF OLD.bound_snapshot_id IS NOT NULL
       AND NEW.bound_snapshot_id = OLD.bound_snapshot_id
       AND NEW.bound_at = OLD.bound_at
       AND NEW.staged_bytes = OLD.staged_bytes
       AND OLD.consumed_at IS NULL
       AND NEW.consumed_at IS NOT NULL
       AND NEW.consumed_at <= NEW.expires_at
       AND NEW.expires_at > statement_timestamp() THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'inventory ingest ticket transition is invalid'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER resource_inventory_ingest_tickets_one_way
BEFORE INSERT OR UPDATE OR DELETE ON resource_inventory_ingest_tickets
FOR EACH ROW EXECUTE FUNCTION protect_resource_inventory_ingest_ticket_mutation();

-- Session grants are immutable apart from an exactly-one-event advancement or
-- explicit completion.  The matching immutable event must exist before the
-- cursor/counter transition, making a cursor-only ACK impossible through this
-- contract.
CREATE FUNCTION protect_resource_inventory_watch_session_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    current_generation BIGINT;
    epoch_retired_at TIMESTAMPTZ;
    epoch_resource_version TEXT;
    committed_event public.resource_inventory_watch_events%ROWTYPE;
    hit_limit BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF (OLD.consumed_at IS NOT NULL
                OR OLD.expires_at <= statement_timestamp())
           AND COALESCE(OLD.consumed_at, OLD.expires_at)
                <= statement_timestamp() - INTERVAL '7 days'
           AND NOT EXISTS (
                SELECT 1
                FROM public.resource_inventory_watch_events event
                WHERE event.watch_session_id = OLD.id
           ) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'watch sessions require a seven-day terminal floor and no events'
            USING ERRCODE = '55000';
    END IF;

    SELECT leader_generation INTO current_generation
    FROM public.infra_metering_control WHERE singleton = TRUE;
    SELECT retired_at, last_resource_version
    INTO epoch_retired_at, epoch_resource_version
    FROM public.resource_inventory_scope_epochs
    WHERE id = NEW.scope_epoch_id;

    IF current_generation IS NULL
       OR epoch_retired_at IS NOT NULL
       OR NEW.leader_generation <> current_generation THEN
        RAISE EXCEPTION 'inventory watch session generation/scope fence failed'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.starting_resource_version IS DISTINCT FROM epoch_resource_version
           OR NEW.last_resource_version IS DISTINCT FROM epoch_resource_version
           OR NEW.committed_events <> 0
           OR NEW.committed_bytes <> 0
           OR NEW.consumed_at IS NOT NULL
           OR NEW.termination_reason IS NOT NULL
           OR NEW.expires_at <= statement_timestamp() THEN
            RAISE EXCEPTION 'new watch session must bind the committed cursor'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.consumed_at IS NOT NULL
       OR (to_jsonb(NEW)
            - 'last_resource_version' - 'committed_events'
            - 'committed_bytes' - 'termination_reason'
            - 'consumed_at' - 'updated_at')
          <> (to_jsonb(OLD)
            - 'last_resource_version' - 'committed_events'
            - 'committed_bytes' - 'termination_reason'
            - 'consumed_at' - 'updated_at')
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'inventory watch session identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    -- A clean shutdown consumes the grant without inventing an event/cursor.
    IF NEW.committed_events = OLD.committed_events
       AND NEW.committed_bytes = OLD.committed_bytes
       AND NEW.last_resource_version = OLD.last_resource_version
       AND NEW.termination_reason = 'completed'
       AND NEW.consumed_at IS NOT NULL
       AND NEW.consumed_at <= NEW.expires_at
       AND NEW.expires_at > statement_timestamp() THEN
        RETURN NEW;
    END IF;

    SELECT event.* INTO committed_event
    FROM public.resource_inventory_watch_events event
    WHERE event.watch_session_id = NEW.id
      AND event.ordinal = NEW.committed_events;

    hit_limit := NEW.committed_events = NEW.max_events
                 OR NEW.committed_bytes = NEW.max_bytes;
    IF committed_event.id IS NOT NULL
       AND NEW.expires_at > statement_timestamp()
       AND NEW.committed_events = OLD.committed_events + 1
       AND NEW.committed_bytes = OLD.committed_bytes
                                     + committed_event.event_bytes
       AND committed_event.expected_resource_version
             = OLD.last_resource_version
       AND NEW.last_resource_version = COALESCE(
            committed_event.resource_version, OLD.last_resource_version
       )
       AND (
            (committed_event.event_type = 'history-lost'
                AND NEW.last_resource_version = OLD.last_resource_version
                AND NEW.termination_reason = 'history-lost'
                AND NEW.consumed_at IS NOT NULL)
            OR (committed_event.event_type <> 'history-lost'
                AND hit_limit
                AND NEW.termination_reason = 'limit-reached'
                AND NEW.consumed_at IS NOT NULL)
            OR (committed_event.event_type <> 'history-lost'
                AND NOT hit_limit
                AND NEW.termination_reason IS NULL
                AND NEW.consumed_at IS NULL)
       )
       AND (NEW.consumed_at IS NULL OR NEW.consumed_at <= NEW.expires_at) THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'inventory watch session transition is invalid'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER resource_inventory_watch_sessions_one_way
BEFORE INSERT OR UPDATE OR DELETE ON resource_inventory_watch_sessions
FOR EACH ROW EXECUTE FUNCTION protect_resource_inventory_watch_session_mutation();

-- Event rows are immutable and must describe the exact next cursor CAS.  For
-- object events, the corresponding interval postcondition must already hold in
-- this transaction.  This lets application-specific interval construction run
-- as a typed hook while preventing a cursor ACK without its mutation.
CREATE FUNCTION protect_resource_inventory_watch_event_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    session_row public.resource_inventory_watch_sessions%ROWTYPE;
    current_generation BIGINT;
    epoch_scope_id UUID;
    epoch_retired_at TIMESTAMPTZ;
    epoch_resource_version TEXT;
    postcondition_ok BOOLEAN;
    deletion_allowed BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT (session.consumed_at IS NOT NULL
                    OR session.expires_at <= statement_timestamp())
               AND COALESCE(session.consumed_at, session.expires_at)
                    <= statement_timestamp() - INTERVAL '7 days'
        INTO deletion_allowed
        FROM public.resource_inventory_watch_sessions session
        WHERE session.id = OLD.watch_session_id
          AND session.scope_epoch_id = OLD.scope_epoch_id
        FOR SHARE;
        IF deletion_allowed IS TRUE THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'watch events require a seven-day terminal session floor'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'inventory watch event rows are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT * INTO session_row
    FROM public.resource_inventory_watch_sessions
    WHERE id = NEW.watch_session_id
      AND scope_epoch_id = NEW.scope_epoch_id
    FOR UPDATE;
    SELECT leader_generation INTO current_generation
    FROM public.infra_metering_control WHERE singleton = TRUE;
    SELECT scope_id, retired_at, last_resource_version
    INTO epoch_scope_id, epoch_retired_at, epoch_resource_version
    FROM public.resource_inventory_scope_epochs
    WHERE id = NEW.scope_epoch_id;

    IF session_row.id IS NULL
       OR session_row.consumed_at IS NOT NULL
       OR session_row.expires_at <= statement_timestamp()
       OR epoch_retired_at IS NOT NULL
       OR current_generation IS NULL
       OR session_row.leader_generation <> current_generation
       OR NEW.ordinal <> session_row.committed_events + 1
       OR NEW.expected_resource_version
            IS DISTINCT FROM session_row.last_resource_version
       OR NEW.expected_resource_version
            IS DISTINCT FROM epoch_resource_version
       OR NEW.event_bytes > session_row.max_bytes
                              - session_row.committed_bytes
       OR NEW.received_at > statement_timestamp() THEN
        RAISE EXCEPTION 'inventory watch event cursor/session fence failed'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.mutation_action IN ('confirm', 'open', 'revise') THEN
        SELECT TRUE INTO postcondition_ok
        FROM public.resource_intervals interval
        WHERE interval.id = NEW.affected_interval_id
          AND interval.inventory_scope_id = epoch_scope_id
          AND interval.source_kind = NEW.source_kind
          AND interval.source_uid = NEW.source_uid
          AND interval.source_revision = NEW.revision_hash
          AND interval.ended_at IS NULL
          AND interval.last_seen_at >= NEW.received_at
          AND interval.last_confirmed_at >= NEW.received_at;
    ELSIF NEW.mutation_action = 'presence-invalid'
          AND NEW.affected_interval_id IS NOT NULL THEN
        SELECT TRUE INTO postcondition_ok
        FROM public.resource_intervals interval
        WHERE interval.id = NEW.affected_interval_id
          AND interval.inventory_scope_id = epoch_scope_id
          AND interval.source_kind = NEW.source_kind
          AND interval.source_uid = NEW.source_uid
          AND interval.ended_at IS NULL
          AND interval.last_seen_at >= NEW.received_at;
    ELSIF NEW.mutation_action = 'close' THEN
        SELECT TRUE INTO postcondition_ok
        FROM public.resource_intervals interval
        WHERE interval.id = NEW.affected_interval_id
          AND interval.inventory_scope_id = epoch_scope_id
          AND interval.source_kind = NEW.source_kind
          AND interval.source_uid = NEW.source_uid
          AND interval.ended_at = NEW.received_at;
    ELSIF NEW.mutation_action IN ('already-absent', 'not-applicable') THEN
        SELECT NOT EXISTS (
            SELECT 1 FROM public.resource_intervals interval
            WHERE interval.inventory_scope_id = epoch_scope_id
              AND interval.source_kind = NEW.source_kind
              AND interval.source_uid = NEW.source_uid
              AND interval.ended_at IS NULL
        ) INTO postcondition_ok;
    ELSIF NEW.mutation_action = 'history-gap' THEN
        SELECT TRUE INTO postcondition_ok
        FROM public.resource_inventory_coverage_gaps gap
        WHERE gap.id = NEW.coverage_gap_id
          AND gap.scope_epoch_id = NEW.scope_epoch_id
          AND gap.resolution = 'unresolved'
          AND gap.gap_start <= NEW.received_at;
    ELSE
        -- BOOKMARK and an invalid item without an existing interval have no
        -- object mutation, by design.
        postcondition_ok := TRUE;
    END IF;

    IF postcondition_ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'inventory watch event interval/gap postcondition failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_inventory_watch_events_immutable
BEFORE INSERT OR UPDATE OR DELETE ON resource_inventory_watch_events
FOR EACH ROW EXECUTE FUNCTION protect_resource_inventory_watch_event_mutation();

-- Shadow rows can only be inserted while their snapshot is staging. Afterwards
-- correction means a new snapshot comparison; deletion is permitted only after
-- both the diagnostic floor and the parent manifest's expiry terminal.
CREATE FUNCTION protect_resource_inventory_shadow_comparison_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    snapshot_is_staging BOOLEAN;
    snapshot_state TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT snapshot.manifest_state
        INTO snapshot_state
        FROM public.resource_inventory_snapshots snapshot
        WHERE snapshot.id = OLD.snapshot_id
          AND snapshot.inventory_scope_id = OLD.inventory_scope_id
        FOR SHARE;
        IF snapshot_state IN ('items-expired', 'staging-expired')
           AND OLD.comparison_at
                <= statement_timestamp() - INTERVAL '7 days'
           AND OLD.created_at
                <= statement_timestamp() - INTERVAL '7 days' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'shadow comparisons require an expired manifest and seven-day floor'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'inventory shadow comparison rows are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT snapshot.manifest_state = 'staging'
    INTO snapshot_is_staging
    FROM public.resource_inventory_snapshots snapshot
    JOIN public.resource_inventory_ingest_tickets ticket
      ON ticket.id = snapshot.ingest_ticket_id
    JOIN public.infra_metering_control control ON control.singleton = TRUE
    WHERE snapshot.id = NEW.snapshot_id
      AND snapshot.inventory_scope_id = NEW.inventory_scope_id
      AND snapshot.leader_generation = control.leader_generation
      AND ticket.bound_snapshot_id = snapshot.id
      AND ticket.consumed_at IS NULL
      AND ticket.expires_at > statement_timestamp();

    IF snapshot_is_staging IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'shadow comparison snapshot fence failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_inventory_shadow_comparisons_immutable
BEFORE INSERT OR UPDATE OR DELETE ON resource_inventory_shadow_comparisons
FOR EACH ROW
EXECUTE FUNCTION protect_resource_inventory_shadow_comparison_mutation();

COMMENT ON TABLE resource_inventory_ingest_tickets IS
    'Hashed one-time collector tickets bound to one scope epoch, generation, request digest, and snapshot.';
COMMENT ON TABLE resource_inventory_transport_nonces IS
    'Immutable HMAC request nonce claims retained through a replay window; expired rows are removed in bounded batches.';
COMMENT ON TABLE resource_inventory_watch_sessions IS
    'Hashed bounded WATCH grants bound to one scope epoch, leader generation, starting cursor, event count, bytes, and expiry; terminal diagnostics may be pruned child-first after the hard floor.';
COMMENT ON TABLE resource_inventory_watch_events IS
    'One-event receipts coupling a UID mutation, BOOKMARK, or history gap to one opaque cursor CAS; immutable until their terminal session passes retention.';
COMMENT ON TABLE resource_inventory_shadow_comparisons IS
    'Per-snapshot workspace shadow comparisons, immutable until the manifest and diagnostic horizons expire; reason_code contains no free-form customer data.';

COMMIT;
