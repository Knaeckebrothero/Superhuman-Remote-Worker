-- migration:     0093_infrastructure_workspace_cutover.sql
-- description:   Durable, crash-resumable workspace-Pod metering cutover.
--                Makes the disabled -> preparing -> active barrier one-way,
--                prevents legacy workspace opens after the barrier, freezes
--                strict legacy drain intent, and binds typed rollups to the
--                exact infrastructure coverage revision they consumed.
-- depends-on:    0092_inventory_invalid_watch_received_idx.notx.sql
-- expected:      < 5s while cutover is disabled. Additive columns/tables plus
--                brief trigger replacement locks on metering state tables.
-- locks:         Brief ACCESS EXCLUSIVE locks on infra_metering_control,
--                infra_usage_day_state, usage_rollup_day_state,
--                workspace_intervals, and resource_intervals. Deploy with all
--                infrastructure publication/cutover gates disabled.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- A cutover request is deliberately richer than a boolean feature flag. The
-- request identity, actor, reason, and barrier become immutable together. The
-- phase makes the cross-database legacy drain restartable without choosing a
-- second barrier after a crash.
ALTER TABLE infra_metering_control
    ADD COLUMN cutover_phase TEXT NOT NULL DEFAULT 'disabled',
    ADD COLUMN cutover_request_id UUID,
    ADD COLUMN cutover_actor_id UUID,
    ADD COLUMN cutover_reason TEXT,
    ADD COLUMN cutover_requested_at TIMESTAMPTZ,
    ADD COLUMN barrier_committed_at TIMESTAMPTZ,
    ADD COLUMN legacy_drained_at TIMESTAMPTZ,
    ADD COLUMN activated_at TIMESTAMPTZ,
    ADD COLUMN cutover_error JSONB,
    ADD CONSTRAINT infra_metering_control_cutover_request_uq
        UNIQUE (cutover_request_id),
    ADD CONSTRAINT infra_metering_control_cutover_phase_check CHECK (
        (cutover_state = 'disabled'
            AND cutover_phase = 'disabled'
            AND cutover_at IS NULL
            AND cutover_request_id IS NULL
            AND cutover_actor_id IS NULL
            AND cutover_reason IS NULL
            AND cutover_requested_at IS NULL
            AND barrier_committed_at IS NULL
            AND legacy_drained_at IS NULL
            AND activated_at IS NULL
            AND cutover_error IS NULL)
        OR
        (cutover_state = 'preparing'
            AND cutover_phase IN ('legacy-draining', 'ready-to-activate')
            AND cutover_at IS NOT NULL
            AND cutover_request_id IS NOT NULL
            AND cutover_actor_id IS NOT NULL
            AND cutover_reason IS NOT NULL
            AND cutover_reason = btrim(cutover_reason)
            AND char_length(cutover_reason) BETWEEN 1 AND 1024
            AND cutover_requested_at IS NOT NULL
            AND barrier_committed_at IS NOT NULL
            AND barrier_committed_at >= cutover_requested_at
            AND (cutover_phase <> 'ready-to-activate'
                OR legacy_drained_at IS NOT NULL)
            AND activated_at IS NULL)
        OR
        (cutover_state = 'active'
            AND cutover_phase = 'active'
            AND cutover_at IS NOT NULL
            AND cutover_request_id IS NOT NULL
            AND cutover_actor_id IS NOT NULL
            AND cutover_reason IS NOT NULL
            AND cutover_reason = btrim(cutover_reason)
            AND char_length(cutover_reason) BETWEEN 1 AND 1024
            AND cutover_requested_at IS NOT NULL
            AND barrier_committed_at IS NOT NULL
            AND legacy_drained_at IS NOT NULL
            AND activated_at IS NOT NULL
            AND activated_at >= legacy_drained_at)
    ),
    ADD CONSTRAINT infra_metering_control_cutover_error_check CHECK (
        cutover_error IS NULL OR jsonb_typeof(cutover_error) = 'object'
    );

CREATE OR REPLACE FUNCTION protect_infra_metering_cutover_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'infra metering cutover control cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.cutover_request_id IS NOT NULL AND (
        NEW.cutover_request_id IS DISTINCT FROM OLD.cutover_request_id
        OR NEW.cutover_actor_id IS DISTINCT FROM OLD.cutover_actor_id
        OR NEW.cutover_reason IS DISTINCT FROM OLD.cutover_reason
        OR NEW.cutover_requested_at IS DISTINCT FROM OLD.cutover_requested_at
        OR NEW.barrier_committed_at IS DISTINCT FROM OLD.barrier_committed_at
        OR NEW.cutover_at IS DISTINCT FROM OLD.cutover_at
    ) THEN
        RAISE EXCEPTION 'infrastructure metering cutover identity/barrier is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.cutover_state = 'disabled' THEN
        IF NEW.cutover_state = 'disabled' THEN
            IF NEW.cutover_phase <> 'disabled' THEN
                RAISE EXCEPTION 'disabled metering cutover phase is immutable'
                    USING ERRCODE = '55000';
            END IF;
        ELSIF NEW.cutover_state = 'preparing' THEN
            IF NEW.cutover_phase <> 'legacy-draining'
               OR NEW.cutover_request_id IS NULL
               OR NEW.cutover_at IS NULL THEN
                RAISE EXCEPTION 'cutover must enter preparing at legacy-draining'
                    USING ERRCODE = '55000';
            END IF;
        ELSE
            RAISE EXCEPTION 'cutover advances disabled to preparing only'
                USING ERRCODE = '55000';
        END IF;
    ELSIF OLD.cutover_state = 'preparing' THEN
        IF NEW.cutover_state = 'preparing' THEN
            IF NOT (
                NEW.cutover_phase = OLD.cutover_phase
                OR (OLD.cutover_phase = 'legacy-draining'
                    AND NEW.cutover_phase = 'ready-to-activate')
            ) THEN
                RAISE EXCEPTION 'preparing cutover phase cannot move backwards'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.legacy_drained_at IS NOT NULL
               AND NEW.legacy_drained_at IS DISTINCT FROM OLD.legacy_drained_at THEN
                RAISE EXCEPTION 'legacy drain completion is immutable'
                    USING ERRCODE = '55000';
            END IF;
        ELSIF NEW.cutover_state = 'active' THEN
            IF OLD.cutover_phase <> 'ready-to-activate'
               OR NEW.cutover_phase <> 'active'
               OR NEW.activated_at IS NULL THEN
                RAISE EXCEPTION 'cutover activates only after durable legacy drain'
                    USING ERRCODE = '55000';
            END IF;
        ELSE
            RAISE EXCEPTION 'preparing cutover cannot be disabled or replaced'
                USING ERRCODE = '55000';
        END IF;
    ELSE
        IF NEW.cutover_state IS DISTINCT FROM OLD.cutover_state
           OR NEW.cutover_phase IS DISTINCT FROM OLD.cutover_phase
           OR NEW.legacy_drained_at IS DISTINCT FROM OLD.legacy_drained_at
           OR NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
            RAISE EXCEPTION 'active infrastructure metering cutover is irreversible'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER infra_metering_control_cutover_one_way
BEFORE UPDATE OR DELETE ON infra_metering_control
FOR EACH ROW EXECUTE FUNCTION protect_infra_metering_cutover_mutation();

-- Old replicas continue calling workspace_metering.open_interval during a
-- rolling deployment. Taking a share lock on the singleton makes every legacy
-- open serialize with the barrier's FOR UPDATE lock. An insert that wins first
-- is included and closed at T; one that loses observes preparing and fails.
CREATE FUNCTION enforce_legacy_workspace_cutover_barrier()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    current_state TEXT;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.ended_at IS NOT NULL THEN
        RETURN NEW;
    END IF;

    SELECT cutover_state INTO current_state
    FROM public.infra_metering_control
    WHERE singleton = TRUE
    FOR SHARE;

    IF current_state IS NULL OR current_state <> 'disabled' THEN
        RAISE EXCEPTION 'legacy workspace opens are disabled by metering cutover'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER workspace_intervals_cutover_open_barrier
BEFORE INSERT OR UPDATE OF ended_at ON workspace_intervals
FOR EACH ROW EXECUTE FUNCTION enforce_legacy_workspace_cutover_barrier();

-- Resource reconciliation is allowed on both sides of the barrier, but its
-- interval/head mutations must serialize with the atomic shadow split. After
-- the barrier commits a waiting observation naturally opens/revises at or after
-- T and cannot sneak an unmatched pre-T interval into the frozen set.
CREATE FUNCTION serialize_resource_interval_with_cutover()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    control_exists BOOLEAN;
BEGIN
    SELECT TRUE INTO control_exists
    FROM public.infra_metering_control
    WHERE singleton = TRUE
    FOR SHARE;
    IF control_exists IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'infra metering control row is missing'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_intervals_cutover_serialization
BEFORE INSERT OR UPDATE ON resource_intervals
FOR EACH ROW EXECUTE FUNCTION serialize_resource_interval_with_cutover();

-- One immutable app-side plan freezes the exact two legacy workspace audit
-- rows (CPU and RAM) before audit I/O. It is intentionally separate from the
-- typed resource_publication_plans table: the latter has UUID interval FKs and
-- typed-infrastructure-only payload checks.
CREATE TABLE legacy_workspace_cutover_plans (
    id                     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_interval_id  BIGINT NOT NULL UNIQUE
        REFERENCES workspace_intervals(id) ON DELETE RESTRICT,
    cutover_request_id     UUID NOT NULL,
    expected_event_count   INTEGER NOT NULL DEFAULT 2,
    payload_schema_version INTEGER NOT NULL DEFAULT 1,
    hash_algorithm         TEXT NOT NULL DEFAULT 'sha256',
    event_set_hash         TEXT NOT NULL,
    creator_generation     BIGINT NOT NULL,
    state                  TEXT NOT NULL DEFAULT 'planned',
    attempt_count          INTEGER NOT NULL DEFAULT 0,
    last_attempt_generation BIGINT,
    last_attempt_at        TIMESTAMPTZ,
    sanitized_error        JSONB,
    published_at           TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT legacy_workspace_cutover_plans_shape_check CHECK (
        expected_event_count = 2
        AND payload_schema_version = 1
        AND hash_algorithm = 'sha256'
        AND event_set_hash ~ '^[0-9a-f]{64}$'
        AND creator_generation > 0
        AND state IN ('planned', 'published', 'conflict')
        AND attempt_count >= 0
        AND ((attempt_count = 0
                AND last_attempt_generation IS NULL
                AND last_attempt_at IS NULL)
             OR (attempt_count > 0
                AND last_attempt_generation IS NOT NULL
                AND last_attempt_generation > 0
                AND last_attempt_at IS NOT NULL))
        AND (sanitized_error IS NULL
            OR jsonb_typeof(sanitized_error) = 'object')
        AND ((state = 'published' AND published_at IS NOT NULL)
             OR (state <> 'published' AND published_at IS NULL))
    )
);

CREATE INDEX legacy_workspace_cutover_plans_pending_idx
    ON legacy_workspace_cutover_plans (created_at, id)
    WHERE state = 'planned';

CREATE TABLE legacy_workspace_cutover_plan_events (
    plan_id       UUID NOT NULL
        REFERENCES legacy_workspace_cutover_plans(id) ON DELETE RESTRICT,
    ordinal       INTEGER NOT NULL,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    unit          TEXT NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    row_hash      TEXT NOT NULL,
    event_payload JSONB NOT NULL,

    PRIMARY KEY (plan_id, ordinal),
    UNIQUE (source, source_id, unit, ts),
    CONSTRAINT legacy_workspace_cutover_plan_events_shape_check CHECK (
        ordinal IN (0, 1)
        AND source = 'orchestrator'
        AND source_id <> ''
        AND unit IN ('vcpu-hour', 'gib-hour')
        AND row_hash ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(event_payload) = 'object'
    )
);

CREATE FUNCTION protect_legacy_workspace_cutover_plan_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'legacy workspace cutover plans cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF (to_jsonb(NEW)
            - 'state' - 'attempt_count' - 'last_attempt_generation'
            - 'last_attempt_at' - 'sanitized_error' - 'published_at')
       <> (to_jsonb(OLD)
            - 'state' - 'attempt_count' - 'last_attempt_generation'
            - 'last_attempt_at' - 'sanitized_error' - 'published_at') THEN
        RAISE EXCEPTION 'legacy workspace cutover plan intent is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state <> 'planned'
       OR NEW.state NOT IN ('planned', 'published', 'conflict')
       OR NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'legacy workspace cutover plan terminal/retry state is invalid'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER legacy_workspace_cutover_plans_frozen
BEFORE UPDATE OR DELETE ON legacy_workspace_cutover_plans
FOR EACH ROW
EXECUTE FUNCTION protect_legacy_workspace_cutover_plan_mutation();

CREATE FUNCTION protect_legacy_workspace_cutover_plan_event_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    parent_state TEXT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'legacy workspace cutover plan events are immutable'
            USING ERRCODE = '55000';
    END IF;
    SELECT state INTO parent_state
    FROM public.legacy_workspace_cutover_plans
    WHERE id = NEW.plan_id
    FOR SHARE;
    IF parent_state IS DISTINCT FROM 'planned' THEN
        RAISE EXCEPTION 'legacy workspace cutover plan no longer accepts events'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER legacy_workspace_cutover_plan_events_frozen
BEFORE INSERT OR UPDATE OR DELETE ON legacy_workspace_cutover_plan_events
FOR EACH ROW
EXECUTE FUNCTION protect_legacy_workspace_cutover_plan_event_mutation();

CREATE FUNCTION validate_legacy_workspace_cutover_plan_manifest()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_plan UUID;
    expected_count INTEGER;
    actual_count INTEGER;
    min_ordinal INTEGER;
    max_ordinal INTEGER;
BEGIN
    target_plan := CASE WHEN TG_TABLE_NAME = 'legacy_workspace_cutover_plans'
        THEN NEW.id ELSE NEW.plan_id END;
    SELECT expected_event_count INTO expected_count
    FROM public.legacy_workspace_cutover_plans WHERE id = target_plan;
    SELECT count(*), min(ordinal), max(ordinal)
    INTO actual_count, min_ordinal, max_ordinal
    FROM public.legacy_workspace_cutover_plan_events
    WHERE plan_id = target_plan;
    IF expected_count IS NULL
       OR actual_count <> expected_count
       OR min_ordinal <> 0
       OR max_ordinal <> expected_count - 1 THEN
        RAISE EXCEPTION 'legacy workspace cutover plan manifest is incomplete'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER legacy_workspace_cutover_plan_manifest_complete
AFTER INSERT OR UPDATE ON legacy_workspace_cutover_plans
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION validate_legacy_workspace_cutover_plan_manifest();

CREATE CONSTRAINT TRIGGER legacy_workspace_cutover_plan_event_manifest_complete
AFTER INSERT ON legacy_workspace_cutover_plan_events
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION validate_legacy_workspace_cutover_plan_manifest();

-- coverage_revision is content-addressed and therefore not naturally ordered.
-- coverage_sequence supplies the monotonic ordering needed by late evidence.
-- The trigger assigns sequence 1 on the existing sealer's first transition and
-- only permits a sealed proof to degrade complete -> partial (or add further
-- unknown ranges while already partial) at exactly the next sequence.
ALTER TABLE infra_usage_day_state
    ADD COLUMN coverage_sequence BIGINT NOT NULL DEFAULT 0;

UPDATE infra_usage_day_state
SET coverage_sequence = 1
WHERE state = 'sealed';

ALTER TABLE infra_usage_day_state
    ADD CONSTRAINT infra_usage_day_state_coverage_sequence_check CHECK (
        (state = 'sealed' AND coverage_sequence > 0)
        OR (state <> 'sealed' AND coverage_sequence = 0)
    );

CREATE OR REPLACE FUNCTION protect_infra_usage_day_state_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'open' OR NEW.coverage_sequence <> 0 THEN
            RAISE EXCEPTION
                'infrastructure usage day state must begin open at sequence zero'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'infrastructure usage day state cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.day IS DISTINCT FROM OLD.day
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'infrastructure usage day identity/time is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'sealed' THEN
        IF NEW.state <> 'sealed'
           OR NEW.sealed_at IS DISTINCT FROM OLD.sealed_at
           OR NEW.coverage_sequence <> OLD.coverage_sequence + 1
           OR NEW.coverage_revision IS NULL
           OR NEW.coverage_revision = ''
           OR NEW.coverage_revision IS NOT DISTINCT FROM OLD.coverage_revision
           OR NOT (OLD.unknown_ranges <@ NEW.unknown_ranges)
           OR jsonb_array_length(NEW.unknown_ranges)
                < jsonb_array_length(OLD.unknown_ranges)
           OR NOT (
                (OLD.coverage_status = 'complete'
                    AND NEW.coverage_status = 'partial'
                    AND jsonb_array_length(NEW.unknown_ranges) > 0)
                OR
                (OLD.coverage_status = 'partial'
                    AND NEW.coverage_status = 'partial'
                    AND jsonb_array_length(NEW.unknown_ranges)
                        > jsonb_array_length(OLD.unknown_ranges))
           ) THEN
            RAISE EXCEPTION
                'sealed infrastructure day may only gain fail-closed unknown ranges'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF (OLD.state = 'open' AND NEW.state NOT IN ('open', 'sealing'))
       OR (OLD.state = 'sealing' AND NEW.state NOT IN ('sealing', 'sealed')) THEN
        RAISE EXCEPTION
            'infrastructure usage day state advances open to sealing to sealed'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.state = 'sealed' THEN
        IF OLD.state <> 'sealing' OR NEW.coverage_sequence NOT IN (0, 1) THEN
            RAISE EXCEPTION 'initial infrastructure day seal is invalid'
                USING ERRCODE = '55000';
        END IF;
        NEW.coverage_sequence := 1;
    ELSIF NEW.coverage_sequence <> 0 THEN
        RAISE EXCEPTION 'unsealed infrastructure day has a coverage revision'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

-- Null remains valid for legacy-only/pre-cutover days. Every post-cutover
-- rolled day is written with the exact non-null seal revision, allowing readers
-- to reject a stale complete daily row after a fail-closed seal degradation.
ALTER TABLE usage_rollup_day_state
    ADD COLUMN infra_coverage_revision TEXT,
    ADD CONSTRAINT usage_rollup_day_state_infra_revision_check CHECK (
        infra_coverage_revision IS NULL OR infra_coverage_revision <> ''
    );

COMMENT ON COLUMN usage_rollup_day_state.infra_coverage_revision IS
    'Exact infra_usage_day_state.coverage_revision consumed by this full-day rollup; NULL only for legacy-only/pre-cutover days.';

COMMIT;
