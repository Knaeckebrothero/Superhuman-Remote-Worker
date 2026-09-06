-- migration:     0060_canvas_events_epoch_comment.sql
-- description:   Document the fail-closed persistent-event runtime generation
--                introduced with the Dynamic Canvas state foundation.
-- depends-on:    0059_docker_workspace_leases.sql
-- expected:      < 1s (catalog comment only)
-- locks:         Brief catalog-row lock only
-- transactional: yes
-- ============================================================================

COMMENT ON COLUMN threads.events_epoch IS
    'Current event-log runtime generation. The agent allocates a new epoch on every DB-backed runtime attach; older client cursors trigger authoritative re-sync.';
