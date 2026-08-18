-- migration:     0041_project_loop_workspace_backend.sql
-- description:   Add project_loops.workspace_backend — an optional per-loop
--                workspace tier override that mirrors the existing per-loop
--                `model` override. When set, create_loop_job injects
--                config_override.workspace.backend into EVERY job the loop
--                spawns, so a loop can run all of its roles on, e.g., a `vm`
--                (root + sudo) instead of the default sandbox container.
--                NULL = unchanged behaviour (each job defaults to sandbox).
--                See orchestrator/services/project_loops.py (create_loop_job)
--                and docs/features/project_self_improvement_loop.md.
-- depends-on:    0040_sudo_request_reply_subject_unique.sql
-- expected:      < 100ms. One nullable column on a tiny table (no default, no
--                rewrite) + a CHECK validated against an all-NULL column.
-- locks:         Brief AccessExclusive on project_loops for the ADD COLUMN /
--                ADD CONSTRAINT, covered by lock_timeout. Tiny table.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE project_loops
    ADD COLUMN IF NOT EXISTS workspace_backend TEXT;

-- Idempotent constraint (re-run safe): drop then add. Mirrors the validated
-- tier set the dispatcher understands. `vm` is the headline case here; lite
-- tiers (virtual/none) are accepted for completeness but conflict with a
-- repository datasource at dispatch, which loops usually attach.
ALTER TABLE project_loops
    DROP CONSTRAINT IF EXISTS project_loop_workspace_backend_valid;
ALTER TABLE project_loops
    ADD CONSTRAINT project_loop_workspace_backend_valid
        CHECK (workspace_backend IS NULL
               OR workspace_backend IN ('sandbox', 'vm', 'virtual', 'none'));

COMMENT ON COLUMN project_loops.workspace_backend IS
    'Optional per-loop workspace tier override. NULL = each spawned job uses '
    'the default (sandbox). When set, create_loop_job injects '
    'config_override.workspace.backend for every job — e.g. ''vm'' gives every '
    'role a root VM. Mirrors the per-loop model override.';

COMMIT;
