-- migration:     0106_compute_scope_epoch_guard.sql
-- description:   Fail closed compute interval creation when its exact current
--                inventory scope epoch has not crossed a rollup boundary.
-- depends-on:    0105_storage_source_activation.sql
-- expected:      < 5s while Slice 3 compute publication remains disabled.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on inventory epochs and
--                resource intervals while the forward guard is installed.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Freeze the epoch heads before interval writers. A migration either observes
-- one complete before/after state or retries; it never admits an interval in
-- the gap between the audit below and trigger installation.
LOCK TABLE resource_inventory_scope_epochs,
           resource_intervals
    IN SHARE ROW EXCLUSIVE MODE;

-- The 0103 activation row fences history for a product class, but it does not
-- identify a collector scope. Without this second guard, adding a namespace or
-- replacing a scope epoch after class activation would inherit the global
-- boundary before that exact inventory epoch had proved continuity.
--
-- Scope expansion after activation is intentionally unsupported in v1. A new
-- scope (or a recovery epoch) starts non-required and therefore cannot create
-- compute intervals. A future extension must first perform a fresh shadow
-- proof and durably promote the exact current epoch at a forward boundary.
CREATE FUNCTION enforce_resource_interval_compute_scope_epoch()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    required_key       TEXT;
    expected_collector TEXT;
    expected_resource  TEXT;
    epoch_required     BOOLEAN;
    epoch_boundary     TIMESTAMPTZ;
BEGIN
    IF NEW.source_kind = 'pod'
       AND NEW.category = 'compute'
       AND NEW.resource = 'agent_pod' THEN
        required_key := 'agent_pod';
        expected_collector := 'kubernetes-pods';
        expected_resource := 'core/v1/pods';
    ELSIF NEW.source_kind = 'pod'
          AND NEW.category = 'compute'
          AND NEW.resource = 'workspace_pod'
          AND NEW.details->>'product_class' = 'ide-session' THEN
        required_key := 'ide_workspace_pod';
        expected_collector := 'kubernetes-pods';
        expected_resource := 'core/v1/pods';
    ELSIF NEW.source_kind = 'vmi'
          AND NEW.category = 'compute'
          AND NEW.resource = 'workspace_vm' THEN
        required_key := 'workspace_vm';
        expected_collector := 'kubevirt-vmis';
        expected_resource := 'kubevirt.io/v1/virtualmachineinstances';
    ELSE
        RETURN NEW;
    END IF;

    SELECT epoch.required_for_rollup, epoch.required_from
    INTO epoch_required, epoch_boundary
    FROM public.resource_inventory_scopes AS scope
    JOIN public.resource_inventory_scope_epochs AS epoch
      ON epoch.scope_id = scope.id
     AND epoch.retired_at IS NULL
    WHERE scope.id = NEW.inventory_scope_id
      AND scope.collector_id = expected_collector
      AND scope.api_resource = expected_resource
      AND scope.namespace IS NOT NULL
    FOR SHARE OF epoch;

    IF epoch_required IS DISTINCT FROM TRUE
       OR epoch_boundary IS NULL
       OR statement_timestamp() < epoch_boundary
       OR NEW.started_at < epoch_boundary THEN
        RAISE EXCEPTION
            'compute product class % lacks a promoted current inventory epoch',
            required_key
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

-- A dark upgrade should find no compute intervals. If an earlier deployment
-- nevertheless enabled one, refuse to strand an unsafe open lifecycle behind
-- the stricter guard and require explicit operator reconciliation.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.resource_intervals AS interval
        JOIN public.resource_inventory_scopes AS scope
          ON scope.id = interval.inventory_scope_id
        LEFT JOIN public.resource_inventory_scope_epochs AS epoch
          ON epoch.scope_id = scope.id
         AND epoch.retired_at IS NULL
        WHERE interval.ended_at IS NULL
          AND interval.category = 'compute'
          AND (
              (interval.source_kind = 'pod'
               AND interval.resource = 'agent_pod')
              OR (interval.source_kind = 'pod'
                  AND interval.resource = 'workspace_pod'
                  AND interval.details->>'product_class' = 'ide-session')
              OR (interval.source_kind = 'vmi'
                  AND interval.resource = 'workspace_vm')
          )
          AND (
              epoch.id IS NULL
              OR NOT epoch.required_for_rollup
              OR epoch.required_from IS NULL
              OR interval.started_at < epoch.required_from
              OR (
                  interval.source_kind = 'vmi'
                  AND (
                      scope.collector_id <> 'kubevirt-vmis'
                      OR scope.api_resource <>
                         'kubevirt.io/v1/virtualmachineinstances'
                      OR scope.namespace IS NULL
                  )
              )
              OR (
                  interval.source_kind = 'pod'
                  AND (
                      scope.collector_id <> 'kubernetes-pods'
                      OR scope.api_resource <> 'core/v1/pods'
                      OR scope.namespace IS NULL
                  )
              )
          )
    ) THEN
        RAISE EXCEPTION
            'open compute interval lacks a promoted current inventory epoch'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

CREATE TRIGGER resource_intervals_compute_scope_epoch_guard
BEFORE INSERT ON resource_intervals
FOR EACH ROW EXECUTE FUNCTION enforce_resource_interval_compute_scope_epoch();

COMMENT ON FUNCTION enforce_resource_interval_compute_scope_epoch() IS
    'Requires each new Slice 3 compute interval to use the exact active, promoted inventory scope epoch and its forward required_from boundary.';

COMMIT;
