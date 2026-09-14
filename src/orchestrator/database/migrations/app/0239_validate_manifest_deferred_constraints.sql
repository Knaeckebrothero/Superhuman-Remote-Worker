-- migration:     0239_validate_manifest_deferred_constraints.sql
-- description:   Validate the constraints 0234 and 0236 added NOT VALID.
-- depends-on:    0238_workspace_activity_projection.sql
-- expected:      < 5s. Every scanned column arrives all-NULL from its own
--                chain, so validation confirms a state already guaranteed.
-- locks:         SHARE UPDATE EXCLUSIVE per table; no writes are blocked.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone = 'UTC';

-- 0234 added manifest_resource_id to these pre-existing tables as an all-NULL
-- column, so no row can reference a resource that does not exist.
ALTER TABLE experts VALIDATE CONSTRAINT experts_manifest_resource_id_fkey;
ALTER TABLE projects VALIDATE CONSTRAINT projects_manifest_resource_id_fkey;

-- 0235 relaxed owner_id on two tables 0234 had just created, so each retirement
-- check scans only the zero rows those brand-new tables hold.
ALTER TABLE srw_workspace_instances
    VALIDATE CONSTRAINT srw_workspace_instances_retired_owner_check;
ALTER TABLE srw_resource_secrets
    VALIDATE CONSTRAINT srw_resource_secrets_retired_owner_check;

-- 0236 added resource_location as an all-NULL column, and the shape check
-- admits NULL unconditionally, so every existing capture already satisfies it.
ALTER TABLE public.managed_repository_workspace_cleanup_intents
    VALIDATE CONSTRAINT workspace_cleanup_resource_location_shape;

COMMIT;
