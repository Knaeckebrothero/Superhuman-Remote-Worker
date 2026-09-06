-- migration:     0013_thread_mounts.sql
-- description:   Canonical table for what cloud surfaces are attached to a
--                persistent thread. Replaces threads.metadata.project_ids as
--                source of truth (the JSONB key is deprecated by the same
--                rollout; readers/writers in orchestrator are migrated to
--                consume this table instead). Forms the foundation for
--                Phase 1 of the cloud-mirror workspace model
--                (docs/features/cloud_collaboration_model.md §9).
--
--                Each row describes one mount: where in the agent's workspace
--                the surface appears (target_path), what kind of surface it
--                is (mount_kind), how to resolve it back to a cloud handle
--                (source_kind, source_ref, webdav_url, ...), and which
--                backend to talk to.
--
--                The runtime project_ids: list[str] contract for downstream
--                consumers (RAG scoping, tool context, knowledge tools)
--                remains; it is now *derived* from the project-kind rows of
--                this table at payload-build time rather than stored in
--                JSONB. visible_project_ids (multi-tenancy access control)
--                is a separate concept and is not affected by this table.
--
--                Phase 1 inserts 'project' rows only. 'project_default' is
--                introduced in Phase 2 (user-home mount at workspace root).
--                'repo' is reserved for Phase 3 (multi-surface mounting).
-- depends-on:    0001_initial.sql
-- expected:      < 1s on dev DB. New table; no data movement.
-- locks:         AccessExclusiveLock on the new table only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS thread_mounts (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    thread_id    UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,

    -- What the agent sees this mount as.
    mount_kind   VARCHAR(32) NOT NULL,

    -- Path within the agent workspace, relative, no leading slash.
    -- Examples: 'projects/alpha', '' (root, for project_default), 'repos/foo'.
    target_path  TEXT        NOT NULL,

    -- How to resolve the cloud-side source.
    source_kind  VARCHAR(32) NOT NULL,
    source_ref   UUID,              -- projects.id when source_kind='project_folder'; NULL for user_home
    backend_id   TEXT,              -- which MainCloudBackend instance to use (e.g. 'opencloud')
    cloud_handle TEXT,              -- backend-specific handle (e.g. OpenCloud space/file id)
    webdav_url   TEXT,              -- absolute WebDAV URL to the mount root

    created_at   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT valid_mount_kind  CHECK (mount_kind  IN ('project', 'project_default', 'repo')),
    CONSTRAINT valid_source_kind CHECK (source_kind IN ('project_folder', 'user_home', 'repo')),

    -- One mount per workspace path on a thread.
    CONSTRAINT uq_thread_mount_path UNIQUE (thread_id, target_path)
);

CREATE INDEX IF NOT EXISTS idx_thread_mounts_thread     ON thread_mounts(thread_id);
CREATE INDEX IF NOT EXISTS idx_thread_mounts_source_ref ON thread_mounts(source_ref) WHERE source_ref IS NOT NULL;

COMMENT ON TABLE thread_mounts IS
    'Canonical record of which cloud surfaces are attached to each persistent '
    'thread. Source of truth for project attachment; deprecates '
    'threads.metadata.project_ids.';

COMMENT ON COLUMN thread_mounts.target_path IS
    'Workspace-relative path where this mount appears to the agent. '
    'Empty string means the mount lives at the workspace root '
    '(used by project_default in Phase 2). No leading slash.';

COMMENT ON COLUMN thread_mounts.source_ref IS
    'projects.id when source_kind=''project_folder''. NULL for ''user_home'' '
    '(resolved via the thread''s user_id at payload-build time).';

COMMIT;
