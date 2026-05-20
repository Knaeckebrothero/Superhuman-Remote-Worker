-- migration:     0014_thread_mounts_target_user_sub.sql
-- description:   Adds ``target_user_sub`` to ``thread_mounts`` so user-home
--                mounts (project_default) carry the Keycloak ``sub`` of the
--                user whose Personal Space is being mounted. The agent uses
--                this to mint a user-scoped token via RFC 8693 token-exchange
--                — required because OpenCloud Personal Spaces are owned by
--                exactly one user and the service account has no WebDAV
--                access of its own.
--
--                Nullable: only ``mount_kind='project_default'`` populates it;
--                ``project`` and ``repo`` mounts use the service-account token
--                directly (they have access via group invite, no impersonation
--                needed).
-- depends-on:    0013_thread_mounts.sql
-- expected:      < 1s on dev DB. Add column on a small table; no data rewrite.
-- locks:         AccessExclusiveLock on thread_mounts; covered by lock_timeout.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE thread_mounts
    ADD COLUMN IF NOT EXISTS target_user_sub TEXT;

COMMENT ON COLUMN thread_mounts.target_user_sub IS
    'Keycloak sub of the user being impersonated for this mount. Set only '
    'for project_default mounts (user-home at workspace root); NULL for '
    'project / repo mounts that the service account can read directly. '
    'Drives RFC 8693 token-exchange in the agent''s OpenCloud sync.';

COMMIT;
