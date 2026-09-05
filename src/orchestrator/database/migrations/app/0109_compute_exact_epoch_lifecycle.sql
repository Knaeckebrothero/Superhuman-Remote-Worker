-- migration:     0109_compute_exact_epoch_lifecycle.sql
-- description:   Keep every Slice 3 interval mutation inside the exact
--                class-specific inventory epoch frozen at activation.
-- depends-on:    0108_compute_exact_epoch_authority.sql
-- expected:      < 5s while Slice 3 compute publication remains disabled.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on compute authority,
--                inventory epochs, and resource intervals.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

LOCK TABLE resource_inventory_scope_epochs,
           compute_metering_activation,
           compute_metering_scope_requirements,
           resource_intervals
    IN SHARE ROW EXCLUSIVE MODE;

-- 0108 admitted only INSERTs and therefore left an existing open interval
-- mutable after its exact epoch retired. Replace that narrow check with one
-- lifecycle predicate used by INSERT, confirmation, closure, and publication
-- cursor updates. A retired epoch may be drained only up to its retirement
-- boundary; it can never authorize a successor epoch or later liveness.
DROP TRIGGER resource_intervals_compute_exact_epoch_guard
    ON resource_intervals;
DROP FUNCTION enforce_resource_interval_compute_exact_epoch_authority();
DROP TRIGGER resource_intervals_compute_scope_epoch_guard
    ON resource_intervals;
DROP FUNCTION enforce_resource_interval_compute_scope_epoch();
DROP TRIGGER resource_intervals_compute_activation_guard
    ON resource_intervals;
DROP FUNCTION enforce_resource_interval_compute_activation();

CREATE FUNCTION enforce_resource_interval_compute_exact_epoch_lifecycle()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    required_key         TEXT;
    activation_state     TEXT;
    activation_boundary  TIMESTAMPTZ;
    requirement_boundary TIMESTAMPTZ;
    epoch_required       BOOLEAN;
    epoch_boundary       TIMESTAMPTZ;
    epoch_retired_at     TIMESTAMPTZ;
    effective_boundary   TIMESTAMPTZ;
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

    -- Snapshot/WATCH ingestion already owns the context epoch before invoking
    -- a reconciler. Lock the exact authority epoch first and activation second;
    -- the scheduler pre-locks the same epoch set before changing activation.
    SELECT requirement.required_from,
           epoch.required_for_rollup, epoch.required_from, epoch.retired_at
    INTO requirement_boundary, epoch_required, epoch_boundary,
         epoch_retired_at
    FROM public.compute_metering_scope_requirements AS requirement
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = requirement.inventory_scope_id
     AND scope.collector_id = requirement.collector_id
     AND scope.source_cluster = requirement.source_cluster
    JOIN public.resource_inventory_scope_epochs AS epoch
      ON epoch.id = requirement.inventory_scope_epoch_id
     AND epoch.scope_id = scope.id
    WHERE requirement.activation_key = required_key
      AND requirement.inventory_scope_id = NEW.inventory_scope_id
      AND scope.source_cluster = NEW.source_cluster
    FOR SHARE OF epoch;

    SELECT activation.state, activation.activated_at
    INTO activation_state, activation_boundary
    FROM public.compute_metering_activation AS activation
    WHERE activation.activation_key = required_key
    FOR SHARE;

    effective_boundary := GREATEST(
        activation_boundary,
        requirement_boundary,
        epoch_boundary
    );
    IF activation_state IS DISTINCT FROM 'active'
       OR epoch_required IS DISTINCT FROM TRUE
       OR effective_boundary IS NULL
       OR statement_timestamp() < effective_boundary
       OR NEW.started_at < effective_boundary THEN
        RAISE EXCEPTION
            'compute product class % lacks its exact promoted epoch authority',
            required_key
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF epoch_retired_at IS NOT NULL THEN
            RAISE EXCEPTION
                'compute product class % exact epoch is retired', required_key
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    -- Epoch retirement is an authorization ceiling, not proof that the
    -- workload ended. Keep an open historical interval frozen if no lifecycle
    -- event closed it, but admit a later cursor drain or explicit closure only
    -- when every mutable timestamp remains at/before the exact retirement.
    IF epoch_retired_at IS NOT NULL
       AND (
           NEW.last_seen_at > epoch_retired_at
           OR NEW.last_confirmed_at > epoch_retired_at
           OR NEW.materialized_through > epoch_retired_at
           OR (NEW.ended_at IS NOT NULL
               AND NEW.ended_at > epoch_retired_at)
       ) THEN
        RAISE EXCEPTION
            'compute product class % mutation exceeds exact epoch retirement',
            required_key
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_intervals_compute_exact_epoch_lifecycle_guard
BEFORE INSERT OR UPDATE ON resource_intervals
FOR EACH ROW
EXECUTE FUNCTION enforce_resource_interval_compute_exact_epoch_lifecycle();

COMMENT ON FUNCTION enforce_resource_interval_compute_exact_epoch_lifecycle() IS
    'Fail-closed Slice 3 interval lifecycle guard: exact per-class epoch authority is required and a retired epoch is a hard timestamp ceiling.';

COMMIT;
