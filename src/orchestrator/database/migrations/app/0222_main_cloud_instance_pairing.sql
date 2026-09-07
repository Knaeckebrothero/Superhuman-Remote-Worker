-- migration:     0222_main_cloud_instance_pairing.sql
-- description:   Refuse new rows that stamp a main-cloud provider without its
--                backend-instance authority. NOT VALID on purpose: pre-0186
--                rows still violate this until the operator-attested backfill
--                runs, and they must keep loading meanwhile.
-- depends-on:    0221_threads_parent_tool_call_comment.sql
-- expected:      < 1s. NOT VALID skips the existing-row scan entirely.
-- locks:         Brief ACCESS EXCLUSIVE on projects and threads (catalog only).
-- transactional: yes

-- Why this exists
-- ---------------
-- 0186 added main_cloud_backend_instance_id nullable with NOT VALID foreign
-- keys and no backfill, so "provider stamped, instance absent" became a
-- representable state. MainCloudRouter.for_project/for_thread fail closed on
-- it — correctly, since acting on a guessed installation is unrecoverable —
-- which meant every pre-0186 project row 500'd its own detail endpoint.
--
-- Nothing prevented that state from being created again. postgres.py
-- update_project() is a generic kwargs updater that skips None values, so a
-- caller passing main_cloud_backend with a None instance id would silently
-- mint a fresh unusable row. The thread writer is safer only by accident (it
-- coerces UUID(backend_instance_id), which raises on None). A constraint is
-- the only thing that can hold this invariant across a kwargs bag.
--
-- NOT VALID is the point, not a shortcut: it enforces on every INSERT and
-- UPDATE from now on while leaving the historical rows readable. Validating
-- it is a separate migration that may only run once the backfill has stamped
-- them (see knowledge-base/knowledge/features/protected_session_lifecycle_and_mount_readiness.md
-- — the backfill needs a live installation proof, which SQL cannot obtain).

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.projects
    DROP CONSTRAINT IF EXISTS projects_main_cloud_instance_pairing,
    ADD CONSTRAINT projects_main_cloud_instance_pairing CHECK (
        main_cloud_backend IS NULL
        OR main_cloud_backend_instance_id IS NOT NULL
    ) NOT VALID;

ALTER TABLE public.threads
    DROP CONSTRAINT IF EXISTS threads_main_cloud_instance_pairing,
    ADD CONSTRAINT threads_main_cloud_instance_pairing CHECK (
        main_cloud_backend IS NULL
        OR main_cloud_backend_instance_id IS NOT NULL
    ) NOT VALID;

COMMENT ON CONSTRAINT projects_main_cloud_instance_pairing ON public.projects IS
    'A project that names a main-cloud provider must also record which '
    'installation of it holds the project folder. Deliberately NOT VALID: '
    'pre-0186 rows violate it until the operator-attested backfill stamps '
    'them, and they must stay readable in the meantime.';

COMMENT ON CONSTRAINT threads_main_cloud_instance_pairing ON public.threads IS
    'A thread that names a main-cloud provider must also record which '
    'installation of it holds the session folder. See the projects sibling.';

COMMIT;
