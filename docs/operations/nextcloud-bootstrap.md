# Nextcloud Main-Cloud Bootstrap Runbook

Operational steps to bring a fresh Nextcloud instance to the point where `NextcloudBackend` (the main-cloud adapter in `orchestrator/services/cloud/nextcloud.py`) can initialize successfully. This is a one-time ops task per deployment.

> **Scope.** Covers bare metal / Compose / Kubernetes equally. For the Fleet-managed K8s install, the config pieces here land as secrets via Vault/ESO; nothing is patched manually on the cluster. Refer to `deployment/19-nextcloud.yaml` for the manifest.

## 1. Required Nextcloud server state

Before the orchestrator can talk to Nextcloud, the instance must have:

1. **`groupfolders` app installed and enabled.** The adapter's `ensure_initialized()` probes `/index.php/apps/groupfolders/folders` and falls back to "cloud provisioning disabled" if it gets a 404. Install via:
   ```bash
   php occ app:install groupfolders
   php occ app:enable  groupfolders
   ```
   (or via the web UI → Apps → Files → Group folders.)

2. **An `srw-agents` group.** The adapter grants every project folder access to this group in addition to the per-project group so the agent service account can read/write regardless of which project's folder it's looking at.
   ```bash
   php occ group:add srw-agents
   ```

3. **A service-account user** (default username `agent-service`). This user owns the session-folders hierarchy and issues the OCS share calls when a persistent thread is started.
   ```bash
   php occ user:add --display-name "SRW Agent Service" agent-service
   php occ group:adduser srw-agents agent-service
   ```

4. **OCS API access enabled.** On hardened installs check `config/config.php` for `"ocs.provisioning_api": true` (the default).

The `docker/nextcloud/setup-nextcloud.sh` init hook in this repo does all four for Compose deployments. On Kubernetes, `deployment/19-nextcloud.yaml` runs the same script as a `before-starting` job.

## 2. The app-password chicken-and-egg

The adapter's service account **must use an app password**, not the admin password. Rationale:

- The admin password is a credential-exposure risk and breaks on MFA.
- App passwords are per-integration, individually revocable, and bypass 2FA legitimately.

**There is no API-only flow to create an app password for a user other than yourself.** Nextcloud's `/ocs/v2.php/core/getapppassword` endpoint returns a password for the *currently authenticated* user; there is no admin endpoint to mint one on behalf of `agent-service`. This is not a bug in the adapter — it is how Nextcloud's security model works.

The one-time ops step is therefore:

1. Log in to the Nextcloud web UI as `agent-service`. Initial password is whatever seed value the setup script used (e.g. `agent-service-dev` on bare Compose).
2. Navigate to **Settings → Security → Devices & sessions → Create new app password**.
3. Name the password `orchestrator` (or similar); copy the generated value.
4. Paste the value into whatever secret backs `NEXTCLOUD_AGENT_PASSWORD`:
   - **Compose dev**: `.env` file at repo root. Restart the orchestrator.
   - **Kubernetes**: Vault secret consumed by ESO; `kubectl rollout restart deploy/orchestrator`.
5. Rotate the seed password on the `agent-service` account so the original seed cannot be used. This is ops hygiene, not a technical requirement.

**After this step**, the orchestrator's `NextcloudBackend` is fully bootstrapped and does not need any more manual intervention.

## 3. Verifying the bootstrap

Two quick smoke checks:

### Healthcheck

```bash
curl -sf http://<orchestrator>/health/cloud
```

Expected: `{"ok": true, "backend": "nextcloud", "latency_ms": <small>}`. If `ok: false`, check the orchestrator logs for `Nextcloud backend init failed`.

### Project creation end-to-end

```bash
curl -X POST http://<orchestrator>/api/projects \
  -H 'Content-Type: application/json' \
  -d '{"name": "Bootstrap Test", "user_id": "<some-user-uuid>"}'
```

Expected: the response includes a `main_cloud_folder_handle`. Inside Nextcloud the Group Folder named "Bootstrap Test" should be visible to the new project group and to `srw-agents`.

If this fails, look for `Failed to provision main-cloud folder for project` in the orchestrator logs. The most common causes are:

- `groupfolders` app not installed or not enabled → 404 on `/index.php/apps/groupfolders/folders`.
- `agent-service` not in the `srw-agents` group → grant step silently fails.
- Wrong app password in `NEXTCLOUD_AGENT_PASSWORD` → 401 on the MKCOL step of `ensure_session_folder` (session folders fail, project folders still work because they authenticate as admin).

## 4. OIDC caveats

When Nextcloud is configured as a Keycloak OIDC relying party (`user_oidc` app), the NC username for each user is not necessarily the email. Two symptoms to watch for:

1. **Personal WebDAV datasources point at the wrong path.** `POST /api/users` builds a WebDAV URL using the user's email as the NC username. If Nextcloud stores a UUID as the username instead, the URL is `/remote.php/dav/files/<email>/` which 404s. **Workaround for now**: configure `user_oidc` with `unique_uid=0` and `mapping-uid=email` so email == username. A proper fix switches the adapter to `resolve_user_identity()` + `get_user_home()` before building the URL — tracked as an inherited Phase 1 bug; see §5.1 of `docs/features/main_cloud_abstraction.md`.

2. **Project-member sync races OIDC login.** Adding a user to a project immediately after their first OIDC login may fail if Nextcloud has not yet created the user row. The adapter retries `resolve_user_identity` via `/users/details?search=` but if the user does not exist on the Nextcloud side at all, the call returns `None` and the group-add is skipped. The member's access is granted the next time they log in via Nextcloud's OIDC group claim — no manual intervention needed, but the UX is "membership takes effect on second login."

## 5. Known Nextcloud server bugs

These affect the adapter regardless of how it is configured. Phase 1.5 detects them; recovery is an ops task.

- **`groupfolders#4127`** — a corrupted `root_id` crashes the Group Folders app entirely. `ensure_initialized()` now parses the groupfolders endpoint response explicitly; if it's non-JSON the orchestrator logs a recovery hint and disables cloud provisioning for that process. **Fix**: inspect `oc_filecache` / `oc_group_folders` for orphaned entries. There is no clean `occ` command — manual SQL.

- **`server#57445`** — newly-added group members don't immediately see existing group folders. The adapter works around this by delete-and-re-add of the group share on the folder (`refresh_project_folder_access`). No ops action required; the workaround is automatic.

- **`server#44782`** — creating a share with specific permissions silently drops the permission bits. The adapter creates with the default (full) permission set and accepts that; if a deployment needs scoped shares, file an issue and we'll add a verification GET after the POST.

- **`/share API` rate limit (NC 30.0.10+)** — 20 new shares per 10 minutes per user. The adapter's client-side leaky bucket (15 events / 10 min in `retry.py`) keeps requests under this ceiling; you should never see a 429 from the orchestrator. If you do, the bucket size is wrong for your traffic profile — tune `_SHARE_RATE_LIMIT_EVENTS` in `orchestrator/services/cloud/nextcloud.py`.

## 6. What to do when you have to rotate `agent-service`

If the app password leaks or needs rotation:

1. Log in as `agent-service`, go to **Settings → Security → Devices & sessions**, revoke the old app password, create a new one.
2. Update the secret backing `NEXTCLOUD_AGENT_PASSWORD`.
3. Rolling restart the orchestrator so the new value is picked up by `NextcloudBackend.__init__`.

There is **no hot-reload** of credentials in Phase 1.5 — the backend reads env vars once at process start. Phase 4 (cockpit settings UI) will wire a `pg_notify` channel for in-place config updates; until then rotation always requires a restart.
