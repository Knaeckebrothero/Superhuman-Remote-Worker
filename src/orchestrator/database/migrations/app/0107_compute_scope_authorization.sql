-- migration:     0107_compute_scope_authorization.sql
-- description:   Bind every activated compute class to its own immutable
--                inventory-scope set and forward boundary.
-- depends-on:    0106_compute_scope_epoch_guard.sql
-- expected:      < 5s while Slice 3 compute classes remain disabled/shadow.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on compute activation,
--                inventory scope/epoch, and interval tables.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

LOCK TABLE compute_metering_activation,
           resource_inventory_scopes,
           resource_inventory_scope_epochs,
           resource_intervals
    IN SHARE ROW EXCLUSIVE MODE;

-- 0103 did not persist the exact scope set used by its shadow proof. Guessing
-- that set for an already-active class could authorize a namespace promoted by
-- another class, because agent and IDE Pods share inventory epochs. This
-- feature is still dark, so fail the upgrade instead of inventing authority.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.compute_metering_activation
        WHERE state = 'active'
    ) THEN
        RAISE EXCEPTION
            'cannot safely backfill exact scopes for an active compute class'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

-- Rows are inserted only while their class is shadow and become immutable at
-- activation. Repeating collector/cluster in the FK freezes the source
-- identity of an authorized scope as well as its UUID.
CREATE TABLE compute_metering_scope_requirements (
    activation_key      TEXT NOT NULL,
    collector_id        TEXT NOT NULL,
    source_cluster      TEXT NOT NULL,
    inventory_scope_id  UUID NOT NULL,
    required_from       TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),

    PRIMARY KEY (activation_key, inventory_scope_id),
    CONSTRAINT compute_metering_scope_requirements_activation_fkey
        FOREIGN KEY (activation_key)
        REFERENCES compute_metering_activation(activation_key)
        ON DELETE RESTRICT,
    CONSTRAINT compute_metering_scope_requirements_scope_fkey
        FOREIGN KEY (inventory_scope_id, collector_id, source_cluster)
        REFERENCES resource_inventory_scopes (
            id, collector_id, source_cluster
        ) ON DELETE RESTRICT,
    CONSTRAINT compute_metering_scope_requirements_boundary_check CHECK (
        required_from = date_trunc('day', required_from, 'UTC')
    )
);

CREATE INDEX compute_metering_scope_requirements_scope_idx
    ON compute_metering_scope_requirements (
        inventory_scope_id, activation_key, required_from
    );

CREATE FUNCTION protect_compute_metering_scope_requirement()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    activation_state TEXT;
    scope_resource   TEXT;
    scope_namespace  TEXT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'compute scope requirements are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT activation.state, scope.api_resource, scope.namespace
    INTO activation_state, scope_resource, scope_namespace
    FROM public.compute_metering_activation AS activation
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = NEW.inventory_scope_id
     AND scope.collector_id = NEW.collector_id
     AND scope.source_cluster = NEW.source_cluster
    WHERE activation.activation_key = NEW.activation_key
    FOR SHARE OF activation;

    IF activation_state IS DISTINCT FROM 'shadow' THEN
        RAISE EXCEPTION
            'compute scope requirements can be added only while shadow'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.required_from <= statement_timestamp() THEN
        RAISE EXCEPTION 'compute scope requirement boundary must be future'
            USING ERRCODE = '55000';
    END IF;
    IF NOT (
        (NEW.activation_key IN ('agent_pod', 'ide_workspace_pod')
         AND NEW.collector_id = 'kubernetes-pods'
         AND scope_resource = 'core/v1/pods'
         AND scope_namespace IS NOT NULL)
        OR (NEW.activation_key = 'workspace_vm'
            AND NEW.collector_id = 'kubevirt-vmis'
            AND scope_resource =
                'kubevirt.io/v1/virtualmachineinstances'
            AND scope_namespace IS NOT NULL)
    ) THEN
        RAISE EXCEPTION 'compute scope requirement does not match its class'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER compute_metering_scope_requirements_immutable
BEFORE INSERT OR UPDATE OR DELETE ON compute_metering_scope_requirements
FOR EACH ROW EXECUTE FUNCTION protect_compute_metering_scope_requirement();

-- Supersede the function behind the 0103 trigger. Scheduling must insert the
-- complete per-class scope set and promote every exact current epoch in the
-- same transaction before the activation row can become active.
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
        IF requirement_count = 0 THEN
            RAISE EXCEPTION
                'compute activation requires an exact scope set'
                USING ERRCODE = '55000';
        END IF;

        PERFORM epoch.id
        FROM public.compute_metering_scope_requirements AS requirement
        JOIN public.resource_inventory_scope_epochs AS epoch
          ON epoch.scope_id = requirement.inventory_scope_id
         AND epoch.retired_at IS NULL
        WHERE requirement.activation_key = OLD.activation_key
        ORDER BY requirement.inventory_scope_id
        FOR SHARE OF epoch;

        IF EXISTS (
            SELECT 1
            FROM public.compute_metering_scope_requirements AS requirement
            LEFT JOIN public.resource_inventory_scope_epochs AS epoch
              ON epoch.scope_id = requirement.inventory_scope_id
             AND epoch.retired_at IS NULL
            WHERE requirement.activation_key = OLD.activation_key
              AND (
                  requirement.required_from IS DISTINCT FROM NEW.activated_at
                  OR epoch.id IS NULL
                  OR NOT epoch.required_for_rollup
                  OR epoch.required_from IS NULL
                  OR epoch.required_from > requirement.required_from
              )
        ) THEN
            RAISE EXCEPTION
                'compute activation requires every exact scope to be promoted'
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

-- Bind the interval to both its per-class authorization and the exact active
-- epoch. An IDE promotion can no longer authorize agent_pod (or vice versa)
-- merely because the two product classes share a Kubernetes Pod scope.
CREATE FUNCTION enforce_resource_interval_compute_scope_authority()
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

    SELECT activation.state, activation.activated_at,
           requirement.required_from,
           epoch.required_for_rollup, epoch.required_from
    INTO activation_state, activation_boundary, requirement_boundary,
         epoch_required, epoch_boundary
    FROM public.compute_metering_scope_requirements AS requirement
    JOIN public.compute_metering_activation AS activation
      ON activation.activation_key = requirement.activation_key
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = requirement.inventory_scope_id
     AND scope.collector_id = requirement.collector_id
     AND scope.source_cluster = requirement.source_cluster
    JOIN public.resource_inventory_scope_epochs AS epoch
      ON epoch.scope_id = scope.id
     AND epoch.retired_at IS NULL
    WHERE requirement.activation_key = required_key
      AND requirement.inventory_scope_id = NEW.inventory_scope_id
    FOR SHARE OF activation, epoch;

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
            'compute product class % lacks exact current scope authority',
            required_key
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_intervals_compute_scope_authority_guard
BEFORE INSERT ON resource_intervals
FOR EACH ROW EXECUTE FUNCTION enforce_resource_interval_compute_scope_authority();

COMMENT ON TABLE compute_metering_scope_requirements IS
    'Immutable exact inventory scopes and per-class boundary proven when one Slice 3 compute class is activated.';

COMMIT;
