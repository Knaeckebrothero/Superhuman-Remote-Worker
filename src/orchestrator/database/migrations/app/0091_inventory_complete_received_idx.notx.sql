-- migration:     0091_inventory_complete_received_idx.notx.sql
-- description:   Online complete-manifest time lookup for generation-fenced
--                infrastructure usage day sealing.
-- depends-on:    0090_infrastructure_interval_overlap_idx.notx.sql
-- expected:      Online build; duration depends on retained inventory snapshots.
-- locks:         SHARE UPDATE EXCLUSIVE; normal reads and writes continue.
-- transactional: no (single CREATE INDEX CONCURRENTLY statement)
--
-- Recovery: an interrupted concurrent build can leave an INVALID index. Drop
-- resource_inventory_snapshots_complete_received_idx CONCURRENTLY, then rerun.

CREATE INDEX CONCURRENTLY IF NOT EXISTS
    resource_inventory_snapshots_complete_received_idx
    ON resource_inventory_snapshots (scope_epoch_id, received_at, id)
    WHERE complete IS TRUE
      AND manifest_state IN ('sealed', 'items-expired');
