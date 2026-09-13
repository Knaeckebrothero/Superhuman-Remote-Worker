BEGIN;
-- Backend identity is server-owned. Existing sandbox reservations remain unchanged.
ALTER TABLE srw_workspace_instances
    ADD COLUMN backend_state JSONB NOT NULL DEFAULT '{}'::jsonb;
COMMIT;
