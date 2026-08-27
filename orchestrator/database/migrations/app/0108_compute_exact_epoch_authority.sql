-- migration:     0108_compute_exact_epoch_authority.sql
-- description:   Freeze compute class authority to the exact inventory epoch
--                proven at activation, not only its reusable scope identity.
-- depends-on:    0107_compute_scope_authorization.sql
-- expected:      < 5s while Slice 3 compute classes remain disabled/shadow.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on compute requirement,
--                activation, inventory epoch, and interval tables.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

LOCK TABLE compute_metering_activation,
           compute_metering_scope_requirements,
           resource_inventory_scope_epochs,
           resource_intervals
    IN SHARE ROW EXCLUSIVE MODE;

-- No released runtime can write 0107 requirements yet. Refuse to infer which
-- epoch an unexpected row proved: reusing the current epoch would recreate the
-- exact rollover inheritance this migration closes.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.compute_metering_scope_requirements
    ) OR EXISTS (
        SELECT 1
        FROM public.compute_metering_activation
        WHERE state = 'active'
    ) THEN
        RAISE EXCEPTION
            'cannot safely backfill exact compute scope epoch authority'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

ALTER TABLE compute_metering_scope_requirements
    ADD COLUMN inventory_scope_epoch_id UUID NOT NULL,
    ADD CONSTRAINT compute_metering_scope_requirements_epoch_scope_fkey
        FOREIGN KEY (inventory_scope_epoch_id, inventory_scope_id)
        REFERENCES resource_inventory_scope_epochs (id, scope_id)
        ON DELETE RESTRICT,
    ADD CONSTRAINT compute_metering_scope_requirements_epoch_uq
        UNIQUE (activation_key, inventory_scope_epoch_id);

CREATE INDEX compute_metering_scope_requirements_epoch_idx
    ON compute_metering_scope_requirements (
        inventory_scope_epoch_id, activation_key, required_from
    );

CREATE OR REPLACE FUNCTION protect_compute_metering_scope_requirement()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    activation_state TEXT;
    scope_resource   TEXT;
    scope_namespace  TEXT;
    epoch_is_current BOOLEAN;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'compute scope requirements are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT activation.state, scope.api_resource, scope.namespace,
           epoch.retired_at IS NULL
    INTO activation_state, scope_resource, scope_namespace, epoch_is_current
    FROM public.compute_metering_activation AS activation
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = NEW.inventory_scope_id
     AND scope.collector_id = NEW.collector_id
     AND scope.source_cluster = NEW.source_cluster
    JOIN public.resource_inventory_scope_epochs AS epoch
      ON epoch.id = NEW.inventory_scope_epoch_id
     AND epoch.scope_id = scope.id
    WHERE activation.activation_key = NEW.activation_key
    FOR SHARE OF activation, epoch;

    IF activation_state IS DISTINCT FROM 'shadow' THEN
        RAISE EXCEPTION
            'compute scope requirements can be added only while shadow'
            USING ERRCODE = '55000';
    END IF;
    IF epoch_is_current IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION
            'compute scope requirement must name the exact current epoch'
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
                'compute activation requires an exact scope epoch set'
                USING ERRCODE = '55000';
        END IF;

        PERFORM epoch.id
        FROM public.compute_metering_scope_requirements AS requirement
        JOIN public.resource_inventory_scope_epochs AS epoch
          ON epoch.id = requirement.inventory_scope_epoch_id
         AND epoch.scope_id = requirement.inventory_scope_id
        WHERE requirement.activation_key = OLD.activation_key
        ORDER BY requirement.inventory_scope_epoch_id
        FOR SHARE OF epoch;

        IF EXISTS (
            SELECT 1
            FROM public.compute_metering_scope_requirements AS requirement
            LEFT JOIN public.resource_inventory_scope_epochs AS epoch
              ON epoch.id = requirement.inventory_scope_epoch_id
             AND epoch.scope_id = requirement.inventory_scope_id
            WHERE requirement.activation_key = OLD.activation_key
              AND (
                  requirement.required_from IS DISTINCT FROM NEW.activated_at
                  OR epoch.id IS NULL
                  OR epoch.retired_at IS NOT NULL
                  OR NOT epoch.required_for_rollup
                  OR epoch.required_from IS NULL
                  OR epoch.required_from > requirement.required_from
              )
        ) THEN
            RAISE EXCEPTION
                'compute activation requires every exact epoch to be promoted'
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

DROP TRIGGER resource_intervals_compute_scope_authority_guard
    ON resource_intervals;
DROP FUNCTION enforce_resource_interval_compute_scope_authority();

CREATE FUNCTION enforce_resource_interval_compute_exact_epoch_authority()
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
      ON epoch.id = requirement.inventory_scope_epoch_id
     AND epoch.scope_id = scope.id
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
            'compute product class % lacks exact current epoch authority',
            required_key
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER resource_intervals_compute_exact_epoch_guard
BEFORE INSERT ON resource_intervals
FOR EACH ROW
EXECUTE FUNCTION enforce_resource_interval_compute_exact_epoch_authority();

COMMENT ON COLUMN compute_metering_scope_requirements.inventory_scope_epoch_id IS
    'Exact immutable inventory epoch whose class-specific shadow proof authorized this scope; epoch rollover fails closed in v1.';

COMMIT;
