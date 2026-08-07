-- migration:     0114_compute_interval_epoch_shape_repair.sql
-- description:   Repair the deployed workspace-Pod exact-epoch shape CHECK
--                so SQL NULL cannot bypass compute authority classification.
-- depends-on:    0113_compute_authority_confirmation_gap.sql
-- expected:      < 10s while Slice 3 compute publication remains disabled.
-- locks:         Brief SHARE ROW EXCLUSIVE lock on resource intervals, plus
--                the ACCESS EXCLUSIVE lock required to replace its CHECK.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

LOCK TABLE resource_intervals IN SHARE ROW EXCLUSIVE MODE;

-- The deployed 0112 predicate used SQL three-valued equality for the IDE
-- product class. CHECK constraints accept NULL, so an ordinary workspace Pod
-- could otherwise carry a non-NULL compute epoch without reaching the compute
-- authority trigger. Validate the corrected shape against every existing row.
ALTER TABLE resource_intervals
    DROP CONSTRAINT resource_intervals_compute_scope_epoch_shape_check,
    ADD CONSTRAINT resource_intervals_compute_scope_epoch_shape_check CHECK (
        (((source_kind = 'pod' AND category = 'compute'
           AND resource = 'agent_pod')
          OR (source_kind = 'pod' AND category = 'compute'
              AND resource = 'workspace_pod'
              AND COALESCE(details->>'product_class', '') = 'ide-session')
          OR (source_kind = 'vmi' AND category = 'compute'
              AND resource = 'workspace_vm'))
         AND compute_scope_epoch_id IS NOT NULL)
        OR
        (NOT ((source_kind = 'pod' AND category = 'compute'
               AND resource = 'agent_pod')
              OR (source_kind = 'pod' AND category = 'compute'
                  AND resource = 'workspace_pod'
                  AND COALESCE(details->>'product_class', '') = 'ide-session')
              OR (source_kind = 'vmi' AND category = 'compute'
                  AND resource = 'workspace_vm'))
         AND compute_scope_epoch_id IS NULL)
    );

COMMENT ON CONSTRAINT resource_intervals_compute_scope_epoch_shape_check
    ON resource_intervals IS
    'Requires exact promoted compute epoch binding only for agent Pods, IDE Pods, and workspace VMIs; NULL product classes cannot bypass the shape.';

COMMIT;
