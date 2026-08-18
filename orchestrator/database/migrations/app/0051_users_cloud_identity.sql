-- migration:     0051_users_cloud_identity.sql
-- description:   Per-backend cloud identity cache on users. resolve_user_identity
--                against the main-cloud backend costs up to two sequential
--                user-search HTTP calls (~2.3s measured on Nextcloud OCS) and
--                its result is a stable fact (the user's cloud account id), yet
--                nothing persisted it — so GET /api/projects/{id} re-resolved it
--                on every project page open. Shape, keyed by backend id so
--                Nextcloud (prod-private) and OpenCloud (dev) coexist:
--                  {"opencloud": {"user_id": "…", "home_browser_url": "…",
--                                 "resolved_at": "…"}}
--                Positive results only — "user hasn't logged into the cloud
--                yet" is a valid transient state and is never cached here.
--                Written by services/cloud/identity.py (merge semantics via
--                Database.merge_user_cloud_identity).
--                docs/issues/project_page_open_blocks_on_cloud_heal.md part 2.
-- depends-on:    0001_initial.sql
-- expected:      < 1s (ADD COLUMN with static default, no table rewrite on PG15).
-- locks:         Brief ACCESS EXCLUSIVE on users (metadata-only change).
-- transactional: yes

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS cloud_identity jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN users.cloud_identity IS
    'Per-backend cloud identity cache: {"<backend_id>": {"user_id", "home_browser_url", "resolved_at"}}. Positive results only; maintained by services/cloud/identity.py.';
