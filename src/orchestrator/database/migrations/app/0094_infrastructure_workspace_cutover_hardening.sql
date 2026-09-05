-- migration:     0094_infrastructure_workspace_cutover_hardening.sql
-- description:   Harden the applied 0093 cutover contract: exact request
--                shape, partial-day source activation, deadlock-free barriers,
--                bounded LIST start semantics, and monotonic seal degradation.
-- depends-on:    0092z_infrastructure_day_sequence_backfill_prep.sql,
--                0093_infrastructure_workspace_cutover.sql
-- expected:      < 5s while cutover/publication gates are disabled.
-- locks:         Brief ACCESS EXCLUSIVE trigger/constraint replacement locks
--                on metering control, inventory, interval, and day tables.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE infra_metering_control
    DROP CONSTRAINT infra_metering_control_cutover_phase_check,
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
            AND cutover_reason !~ '[[:cntrl:]]'
            AND cutover_requested_at IS NOT NULL
            AND barrier_committed_at IS NOT NULL
            AND cutover_requested_at = cutover_at
            AND barrier_committed_at = cutover_at
            AND ((cutover_phase = 'legacy-draining'
                    AND legacy_drained_at IS NULL)
                OR (cutover_phase = 'ready-to-activate'
                    AND legacy_drained_at IS NOT NULL
                    AND legacy_drained_at >= cutover_at))
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
            AND cutover_reason !~ '[[:cntrl:]]'
            AND cutover_requested_at IS NOT NULL
            AND barrier_committed_at IS NOT NULL
            AND cutover_requested_at = cutover_at
            AND barrier_committed_at = cutover_at
            AND legacy_drained_at IS NOT NULL
            AND legacy_drained_at >= cutover_at
            AND activated_at IS NOT NULL
            AND activated_at >= legacy_drained_at)
    );

-- Ordinary source activation starts at UTC midnight. The initial irreversible
-- handoff is the sole partial-day exception and must equal its durable barrier.
ALTER TABLE resource_inventory_scope_epochs
    DROP CONSTRAINT resource_inventory_scope_epochs_midnight_check;

CREATE FUNCTION enforce_inventory_epoch_required_boundary()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    durable_state TEXT;
    durable_cutover TIMESTAMPTZ;
BEGIN
    SELECT cutover_state, cutover_at
    INTO durable_state, durable_cutover
    FROM public.infra_metering_control
    WHERE singleton = TRUE
    FOR SHARE;

    IF TG_OP = 'UPDATE'
       AND OLD.required_for_rollup
       AND OLD.required_from IS NOT NULL
       AND OLD.required_from = durable_cutover
       AND (NEW.required_for_rollup IS DISTINCT FROM OLD.required_for_rollup
            OR NEW.required_from IS DISTINCT FROM OLD.required_from) THEN
        RAISE EXCEPTION 'initial cutover inventory boundary is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.required_from IS NULL
       OR NEW.required_from = date_trunc('day', NEW.required_from, 'UTC') THEN
        RETURN NEW;
    END IF;

    IF durable_state NOT IN ('preparing', 'active')
       OR durable_cutover IS NULL
       OR NEW.required_from IS DISTINCT FROM durable_cutover THEN
        RAISE EXCEPTION
            'inventory requirement must begin at UTC midnight or durable cutover'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_inventory_scope_epochs_boundary_insert
BEFORE INSERT ON resource_inventory_scope_epochs
FOR EACH ROW EXECUTE FUNCTION enforce_inventory_epoch_required_boundary();

CREATE TRIGGER resource_inventory_scope_epochs_boundary_update
BEFORE UPDATE OF required_for_rollup, required_from
ON resource_inventory_scope_epochs
FOR EACH ROW EXECUTE FUNCTION enforce_inventory_epoch_required_boundary();

-- Only INSERT creates a new legacy open. UPDATE paths never acquire the control
-- lock after their row lock, which avoids inverting the cutover lock order.
DROP TRIGGER workspace_intervals_cutover_open_barrier ON workspace_intervals;

CREATE OR REPLACE FUNCTION enforce_legacy_workspace_cutover_barrier()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    current_state TEXT;
BEGIN
    IF TG_OP = 'INSERT' AND NEW.ended_at IS NOT NULL THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF OLD.ended_at IS NOT NULL THEN
            IF NEW.ended_at IS DISTINCT FROM OLD.ended_at THEN
                RAISE EXCEPTION 'closed legacy workspace end is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END IF;
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
BEFORE INSERT OR UPDATE ON workspace_intervals
FOR EACH ROW EXECUTE FUNCTION enforce_legacy_workspace_cutover_barrier();

-- Statement-level locking obtains control SHARE before UPDATE/INSERT takes any
-- resource row locks. Cutover takes control UPDATE first, so both paths now use
-- one lock order instead of relying on PostgreSQL deadlock victim selection.
DROP TRIGGER resource_intervals_cutover_serialization ON resource_intervals;
DROP FUNCTION serialize_resource_interval_with_cutover();

CREATE FUNCTION serialize_resource_interval_statement_with_cutover()
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
    RETURN NULL;
END;
$$;

CREATE TRIGGER resource_intervals_cutover_serialization
BEFORE INSERT OR UPDATE ON resource_intervals
FOR EACH STATEMENT
EXECUTE FUNCTION serialize_resource_interval_statement_with_cutover();

-- A receipt-bounded initial LIST start is explained only inside its persisted
-- uncertainty. All other lifetime mismatches remain unresolved and blocking.
ALTER TABLE resource_inventory_shadow_comparisons
    DROP CONSTRAINT resource_inventory_shadow_comparisons_status_check,
    ADD CONSTRAINT resource_inventory_shadow_comparisons_status_check CHECK (
        status IN (
            'matched', 'capacity-mismatch', 'owner-mismatch',
            'legacy-missing', 'invalid-observation', 'not-applicable',
            'lifetime-mismatch'
        )
        AND reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
        AND (status <> 'matched' OR start_delta_us IS NULL
             OR start_delta_us = 0)
        AND (status <> 'lifetime-mismatch' OR (
            (NOT explained
                AND reason_code IN ('start-semantics', 'start-evidence-missing')
                AND (start_delta_us IS NULL OR start_delta_us <> 0))
            OR
            (explained
                AND reason_code = 'bounded-start-semantics'
                AND start_delta_us > 0
                AND observed_start_time_source = 'app-db-received'
                AND observed_start_uncertainty_us IS NOT NULL
                AND start_delta_us <= observed_start_uncertainty_us)
        ))
    );

-- The deferred trigger fires on two record shapes. Resolve the target through
-- JSON so PL/pgSQL never dereferences a field absent from one of those shapes.
CREATE OR REPLACE FUNCTION validate_legacy_workspace_cutover_plan_manifest()
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
    target_plan := COALESCE(
        (to_jsonb(NEW) ->> 'id')::UUID,
        (to_jsonb(NEW) ->> 'plan_id')::UUID
    );
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

-- Replace the 0092z one-time backfill guard with the permanent monotonic
-- coverage-revision state machine.
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
           OR EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.unknown_ranges) AS item(value)
                GROUP BY item.value
                HAVING count(*) > 1
           )
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

COMMIT;
