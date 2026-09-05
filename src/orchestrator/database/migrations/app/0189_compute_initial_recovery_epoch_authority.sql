-- migration:     0189_compute_initial_recovery_epoch_authority.sql
-- description:   Permit initial Slice 3 compute authority on a recovered
--                current epoch only when its coverage reaches the boundary.
-- depends-on:    0188_pre_registration_delete_sandbox_zero.sql
-- expected:      < 10s. Replaces one trigger function without rewriting data.
-- locks:         Brief SHARE ROW EXCLUSIVE lock on compute epoch authorities.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

LOCK TABLE public.compute_metering_epoch_authorities
    IN SHARE ROW EXCLUSIVE MODE;

CREATE OR REPLACE FUNCTION public.protect_compute_metering_epoch_authority()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    activation_state       TEXT;
    activation_boundary    TIMESTAMPTZ;
    request_key            TEXT;
    request_kind           TEXT;
    request_collector      TEXT;
    request_cluster        TEXT;
    request_promoted_at    TIMESTAMPTZ;
    current_generation     BIGINT;
    scope_resource         TEXT;
    scope_namespace        TEXT;
    epoch_recovery_from    UUID;
    epoch_retired_at       TIMESTAMPTZ;
    epoch_reliable_from    TIMESTAMPTZ;
    epoch_continuous_since TIMESTAMPTZ;
    epoch_continuity_health TEXT;
    previous_epoch_id      UUID;
    previous_sequence      BIGINT;
    previous_retired_at    TIMESTAMPTZ;
    snapshot_item_count    BIGINT;
    snapshot_generation    BIGINT;
    snapshot_is_proof      BOOLEAN;
    shadow_count           BIGINT;
    missing_shadow_count   BIGINT;
    orphan_shadow_count    BIGINT;
    lineage_reaches_prior  BOOLEAN;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'compute epoch authorities are append-only'
            USING ERRCODE = '55000';
    END IF;

    SELECT epoch.recovery_from_epoch_id, epoch.retired_at,
           epoch.reliable_from, epoch.continuous_since,
           epoch.continuity_health,
           scope.api_resource, scope.namespace
    INTO epoch_recovery_from, epoch_retired_at,
         epoch_reliable_from, epoch_continuous_since,
         epoch_continuity_health,
         scope_resource, scope_namespace
    FROM public.resource_inventory_scope_epochs AS epoch
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = epoch.scope_id
     AND scope.collector_id = NEW.collector_id
     AND scope.source_cluster = NEW.source_cluster
    WHERE epoch.id = NEW.inventory_scope_epoch_id
      AND epoch.scope_id = NEW.inventory_scope_id
    FOR SHARE OF epoch;

    SELECT activation.state, activation.activated_at
    INTO activation_state, activation_boundary
    FROM public.compute_metering_activation AS activation
    WHERE activation.activation_key = NEW.activation_key
    FOR SHARE;

    SELECT request.activation_key, request.request_kind,
           request.collector_id, request.source_cluster, request.promoted_at
    INTO request_key, request_kind, request_collector, request_cluster,
         request_promoted_at
    FROM public.compute_metering_epoch_promotion_requests AS request
    WHERE request.id = NEW.promotion_request_id
    FOR SHARE;

    SELECT control.leader_generation
    INTO current_generation
    FROM public.infra_metering_control AS control
    WHERE control.singleton = TRUE
    FOR SHARE;

    IF epoch_retired_at IS NOT NULL
       OR request_key IS DISTINCT FROM NEW.activation_key
       OR request_collector IS DISTINCT FROM NEW.collector_id
       OR request_cluster IS DISTINCT FROM NEW.source_cluster
       OR scope_namespace IS NULL
       OR NOT (
            (NEW.activation_key IN ('agent_pod', 'ide_workspace_pod')
             AND NEW.collector_id = 'kubernetes-pods'
             AND scope_resource = 'core/v1/pods')
            OR (NEW.activation_key = 'workspace_vm'
                AND NEW.collector_id = 'kubevirt-vmis'
                AND scope_resource =
                    'kubevirt.io/v1/virtualmachineinstances')
       ) THEN
        RAISE EXCEPTION 'compute epoch authority identity is invalid'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.authority_sequence = 1 THEN
        IF activation_state IS DISTINCT FROM 'shadow'
           OR request_kind IS DISTINCT FROM 'initial-activation'
           OR (
                epoch_recovery_from IS NOT NULL
                AND (
                    epoch_continuity_health IS DISTINCT FROM 'healthy'
                    OR epoch_reliable_from IS NULL
                    OR epoch_reliable_from > NEW.effective_from
                    OR epoch_continuous_since IS NULL
                    OR epoch_continuous_since > NEW.effective_from
                    OR EXISTS (
                        SELECT 1
                        FROM public.resource_inventory_coverage_gaps AS gap
                        WHERE gap.scope_epoch_id =
                            NEW.inventory_scope_epoch_id
                          AND gap.resolution = 'unresolved'
                          AND gap.reason NOT LIKE
                              'compute-authority-awaiting-confirmation:%'
                    )
                )
           )
           OR NOT EXISTS (
                SELECT 1
                FROM public.compute_metering_scope_requirements AS requirement
                WHERE requirement.activation_key = NEW.activation_key
                  AND requirement.collector_id = NEW.collector_id
                  AND requirement.source_cluster = NEW.source_cluster
                  AND requirement.inventory_scope_id = NEW.inventory_scope_id
                  AND requirement.inventory_scope_epoch_id =
                      NEW.inventory_scope_epoch_id
                  AND requirement.required_from = NEW.effective_from
           ) THEN
            RAISE EXCEPTION 'initial compute epoch authority is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSE
        SELECT prior.inventory_scope_epoch_id, prior.authority_sequence,
               epoch.retired_at
        INTO previous_epoch_id, previous_sequence, previous_retired_at
        FROM public.compute_metering_epoch_authorities AS prior
        JOIN public.resource_inventory_scope_epochs AS epoch
          ON epoch.id = prior.inventory_scope_epoch_id
         AND epoch.scope_id = prior.inventory_scope_id
        WHERE prior.id = NEW.previous_authority_id
          AND prior.activation_key = NEW.activation_key
          AND prior.inventory_scope_id = NEW.inventory_scope_id
        FOR SHARE OF epoch;

        WITH RECURSIVE lineage AS (
            SELECT epoch.id, epoch.scope_id, epoch.recovery_from_epoch_id,
                   ARRAY[epoch.id]::UUID[] AS path, 1 AS depth
            FROM public.resource_inventory_scope_epochs AS epoch
            WHERE epoch.id = NEW.inventory_scope_epoch_id
              AND epoch.scope_id = NEW.inventory_scope_id
            UNION ALL
            SELECT predecessor.id, predecessor.scope_id,
                   predecessor.recovery_from_epoch_id,
                   lineage.path || predecessor.id,
                   lineage.depth + 1
            FROM lineage
            JOIN public.resource_inventory_scope_epochs AS predecessor
              ON predecessor.id = lineage.recovery_from_epoch_id
             AND predecessor.scope_id = NEW.inventory_scope_id
            WHERE lineage.depth < 10000
              AND NOT predecessor.id = ANY(lineage.path)
        )
        SELECT EXISTS (
            SELECT 1 FROM lineage WHERE id = previous_epoch_id
        ) INTO lineage_reaches_prior;

        IF activation_state IS DISTINCT FROM 'active'
           OR activation_boundary IS NULL
           OR statement_timestamp() < activation_boundary
           OR request_kind IS DISTINCT FROM 'recovery-rollover'
           OR previous_sequence IS NULL
           OR NEW.authority_sequence <> previous_sequence + 1
           OR epoch_recovery_from IS DISTINCT FROM NEW.predecessor_epoch_id
           OR previous_retired_at IS NULL
           OR previous_retired_at > NEW.effective_from
           OR lineage_reaches_prior IS DISTINCT FROM TRUE
           OR NEW.effective_from IS DISTINCT FROM request_promoted_at THEN
            RAISE EXCEPTION 'compute recovery epoch lineage is invalid'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    SELECT snapshot.item_count, snapshot.leader_generation,
           snapshot.complete
           AND snapshot.manifest_state = 'sealed'
           AND epoch.last_complete_snapshot_id = snapshot.id
    INTO snapshot_item_count, snapshot_generation, snapshot_is_proof
    FROM public.resource_inventory_snapshots AS snapshot
    JOIN public.resource_inventory_scope_epochs AS epoch
      ON epoch.id = snapshot.scope_epoch_id
     AND epoch.scope_id = snapshot.inventory_scope_id
    WHERE snapshot.id = NEW.proof_snapshot_id
      AND snapshot.scope_epoch_id = NEW.inventory_scope_epoch_id
      AND snapshot.inventory_scope_id = NEW.inventory_scope_id
    FOR SHARE OF epoch;

    SELECT count(*)
    INTO shadow_count
    FROM public.compute_shadow_observations AS observation
    WHERE observation.snapshot_id = NEW.proof_snapshot_id
      AND observation.inventory_scope_id = NEW.inventory_scope_id
      AND observation.activation_key = NEW.activation_key;

    SELECT count(*)
    INTO missing_shadow_count
    FROM public.resource_inventory_snapshot_items AS item
    WHERE item.snapshot_id = NEW.proof_snapshot_id
      AND NOT EXISTS (
          SELECT 1
          FROM public.compute_shadow_observations AS observation
          WHERE observation.snapshot_id = item.snapshot_id
            AND observation.activation_key = NEW.activation_key
            AND observation.source_kind = item.source_kind
            AND observation.source_uid = item.source_uid
      );

    SELECT count(*)
    INTO orphan_shadow_count
    FROM public.compute_shadow_observations AS observation
    WHERE observation.snapshot_id = NEW.proof_snapshot_id
      AND observation.activation_key = NEW.activation_key
      AND NOT EXISTS (
          SELECT 1
          FROM public.resource_inventory_snapshot_items AS item
          WHERE item.snapshot_id = observation.snapshot_id
            AND item.source_kind = observation.source_kind
            AND item.source_uid = observation.source_uid
      );

    IF snapshot_is_proof IS DISTINCT FROM TRUE
       OR snapshot_generation IS DISTINCT FROM NEW.proof_generation
       OR current_generation IS DISTINCT FROM NEW.proof_generation
       OR shadow_count IS DISTINCT FROM snapshot_item_count
       OR missing_shadow_count <> 0
       OR orphan_shadow_count <> 0 THEN
        RAISE EXCEPTION
            'compute epoch authority requires an exact item-for-item proof'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION public.protect_compute_metering_epoch_authority() IS
    'Fail-closed exact-epoch compute authority guard. Initial recovery epochs '
    'must prove healthy continuous and reliable coverage through the boundary.';

COMMIT;
