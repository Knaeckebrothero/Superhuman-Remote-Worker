# Phase 3 — Main Cloud Default Flip to OpenCloud

Phase 3 of the [main cloud abstraction](../features/main_cloud_abstraction.md) flipped the greenfield default main-cloud backend from Nextcloud to OpenCloud. This runbook describes what changed, who is affected, and what operators need to do.

**TL;DR.** If you deploy fresh from this commit, you get OpenCloud. If you already have a Nextcloud deployment running, nothing changes — the legacy heuristic auto-routes you back to Nextcloud and all your data stays put. If you want to explicitly migrate between backends, see §4.

## 1. What changed

1. **Compose default is now OpenCloud.** `podman-compose up -d` now starts the `opencloud` service. The `nextcloud` service moved behind `profiles: ["nextcloud"]` and has to be explicitly opted into via `COMPOSE_PROFILES=nextcloud`.

2. **Orchestrator resolver order.** `load_main_cloud_config()` now resolves the backend as:
   1. `MAIN_CLOUD_BACKEND` env var — explicit override wins.
   2. `_detect_legacy_nextcloud_mode()` — if any `NEXTCLOUD_*` var is set (other than `NEXTCLOUD_PORT`), route to Nextcloud. This is the **in-place upgrade path**.
   3. Default → `opencloud`.

3. **`DEFAULT_DS_WEBDAV_*` is no longer Nextcloud-hardcoded** in any compose file. The main-cloud adapter injects per-project and per-user datasources itself; the shared "admin-visible" default datasource was a Phase 0 shim that's no longer useful. It's still wired to env vars for operators who want to keep it pointing at a legacy Nextcloud.

4. **`build_backend()` default is now `None`.** Callers can still pass an explicit backend id (used by `MainCloudRouter._legacy` for dispatch to cached adapters) but the no-arg path now reads config from the env.

## 2. Greenfield install path (new deployments)

```bash
# 1. Copy .env.example → .env and fill in secrets as usual.
cp .env.example .env
vim .env   # Set passwords; the main-cloud section can stay commented.

# 2. Bring up the full stack — OpenCloud is included by default.
podman-compose up -d

# 3. Seed the Keycloak client for the orchestrator service account.
#    (Idempotent — safe to re-run.)
./docker/keycloak/setup-opencloud-client.sh

# 4. Verify the orchestrator picked up OpenCloud.
curl -sf http://localhost:8085/health/cloud
# Expected: {"ok": true, "backend": "opencloud", ...}
```

See `docs/operations/opencloud-bootstrap.md` for the full OpenCloud setup guide (required server state, OIDC caveats, known quirks).

## 3. In-place upgrade path (existing Nextcloud deployments)

**You don't have to do anything.** The legacy heuristic auto-detects your Nextcloud setup via your existing `NEXTCLOUD_*` env vars and keeps the orchestrator pointing at Nextcloud. Your projects and persistent threads keep working with zero config changes.

What you *will* notice on your next `podman-compose up`:

- **The `nextcloud` service won't start automatically.** You need to run `COMPOSE_PROFILES=nextcloud podman-compose up -d nextcloud` or add `COMPOSE_PROFILES=nextcloud` to your shell (or `.env`, via Docker Compose conventions). The orchestrator will refuse to serve cloud operations until the Nextcloud container is reachable.

- **The opencloud service will also try to start** alongside Nextcloud on a vanilla `podman-compose up`. You have two choices:
  - Run both side by side (they use different ports and different volumes — harmless).
  - Stop OpenCloud explicitly: `podman-compose stop opencloud && podman-compose rm opencloud`.
  - Or pin your profile: set `COMPOSE_PROFILES=nextcloud` and add an explicit `podman-compose --profile nextcloud up -d` to your deploy script.

Recommended: **make the routing explicit** after upgrade. Add the following to your `.env` so future-you doesn't have to re-derive it from the heuristic:

```bash
MAIN_CLOUD_BACKEND=nextcloud
COMPOSE_PROFILES=nextcloud
```

## 4. Explicit migration between backends

The abstraction supports "non-destructive switching": you can change the active backend without invalidating data that older projects and threads created on the previous backend. The `main_cloud_backend` column on `projects` and `threads` records which backend each row was created against, and `MainCloudRouter.for_project` / `for_thread` dispatch reads/updates/deletes to the right instance. Creates always use the active backend.

### Migrate Nextcloud → OpenCloud

1. Stand up OpenCloud alongside your existing Nextcloud:
   ```bash
   podman-compose up -d opencloud
   ./docker/keycloak/setup-opencloud-client.sh
   ```
2. Flip the orchestrator:
   ```bash
   # In .env:
   MAIN_CLOUD_BACKEND=opencloud
   OPENCLOUD_URL=http://opencloud:9200
   OPENCLOUD_PUBLIC_URL=http://localhost:9200
   OPENCLOUD_KEYCLOAK_ISSUER=http://keycloak:8080/realms/srw
   OPENCLOUD_KEYCLOAK_CLIENT_SECRET=...
   # (Leave the NEXTCLOUD_* vars in place — the router needs them to
   #  reach the legacy backend for old projects.)
   ```
3. Restart the orchestrator:
   ```bash
   podman-compose restart orchestrator
   ```
4. Verify: `curl -sf http://localhost:8085/health/cloud` should return `{"ok": true, "backend": "opencloud", ...}`. Existing projects still appear; their file operations transparently route through the cached `NextcloudBackend` instance.
5. New projects and persistent threads go to OpenCloud. Nextcloud stays running as a "legacy data store" — you can shut it down only after you've migrated (or decided to orphan) every project with `main_cloud_backend = 'nextcloud'`.

### Migrate OpenCloud → Nextcloud

Same flow in reverse:
```bash
COMPOSE_PROFILES=nextcloud podman-compose up -d nextcloud
# Then in .env:
MAIN_CLOUD_BACKEND=nextcloud
```
Then restart the orchestrator.

### Bulk data migration

Out of scope for Phase 3. An explicit `migrate-project` CLI is tracked as a separate workstream (see §10 of the abstraction design doc). For now, migration means "new projects land on the new backend, old projects stay where they were."

## 5. Rollback

If Phase 3 breaks your deployment and you need to revert:

1. Roll back the orchestrator image to the pre-Phase-3 build.
2. Restart the compose stack — the old orchestrator will continue to call `NextcloudBackend()` directly from `main.py:137` as it did before.
3. No DB rollback needed: the schema is backward-compatible. The new `main_cloud_*` columns and the legacy `nextcloud_folder_id` / `nc_session_folder` / `nc_share_id` columns coexist; the old orchestrator only reads the legacy columns, and the new orchestrator double-writes to both.

If you need to rescue a mis-provisioned OpenCloud Space created by Phase 3, use the LibreGraph API directly:
```bash
TOKEN=$(curl -sf -d grant_type=client_credentials -d client_id=opencloud-orchestrator \
    -d client_secret=... http://keycloak:8080/realms/srw/protocol/openid-connect/token \
    | jq -r .access_token)
# List drives:
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:9200/graph/v1.0/drives | jq
# Delete a drive (disable-then-purge):
curl -sf -XDELETE -H "Authorization: Bearer $TOKEN" http://localhost:9200/graph/v1.0/drives/<id>
curl -sf -XDELETE -H "Authorization: Bearer $TOKEN" -H "Purge: T" http://localhost:9200/graph/v1.0/drives/<id>
```

## 6. Verification checklist

After any Phase 3-related deploy, confirm:

- [ ] `curl -sf http://localhost:8085/health/cloud` returns `{"ok": true, "backend": "<expected>"}`.
- [ ] `podman-compose ps | grep -E '(nextcloud|opencloud)'` shows the expected services running.
- [ ] A newly-created project has a non-null `main_cloud_folder_handle` in the `projects` table.
- [ ] The project folder is visible in the chosen backend's web UI (OpenCloud Spaces list, or Nextcloud Group Folders).
- [ ] The orchestrator logs do NOT contain `Nextcloud backend init failed` or `OpenCloud backend init failed`.

If any of these fail, check the orchestrator logs first — every cloud operation emits a structured log line via the `@instrument_backend_op` decorator, and failures include the backend id, op name, and error kind.
