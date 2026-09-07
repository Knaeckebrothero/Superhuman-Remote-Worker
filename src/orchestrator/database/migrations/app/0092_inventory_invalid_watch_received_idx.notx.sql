-- migration:     0092_inventory_invalid_watch_received_idx.notx.sql
-- description:   Online invalid-presence WATCH lookup for generation-fenced
--                infrastructure usage day sealing.
-- depends-on:    0091_inventory_complete_received_idx.notx.sql
-- expected:      Online build; duration depends on retained inventory WATCH events.
-- locks:         SHARE UPDATE EXCLUSIVE; normal reads and writes continue.
-- transactional: no (single CREATE INDEX CONCURRENTLY statement)
--
-- Recovery: an interrupted concurrent build can leave an INVALID index. Drop
-- resource_inventory_watch_events_invalid_received_idx CONCURRENTLY, then rerun.

CREATE INDEX CONCURRENTLY IF NOT EXISTS
    resource_inventory_watch_events_invalid_received_idx
    ON resource_inventory_watch_events (scope_epoch_id, received_at, id)
    WHERE valid_for_metering IS FALSE
      AND mutation_action = 'presence-invalid';
