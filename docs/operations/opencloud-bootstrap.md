# OpenCloud Main-Cloud Bootstrap Runbook

Operational steps to stand up a fresh OpenCloud instance and point the orchestrator's `OpenCloudBackend` at it. Bundled Compose deployments do most of this automatically; external / BYO installs need to replicate the steps in §2 and §3 by hand.

> **Scope.** Phase 2 shipped the adapter and a Compose profile (`docker-compose.yaml` under `profiles: ["opencloud"]`). Phase 3 will flip the default so greenfield installs don't need to pass `MAIN_CLOUD_BACKEND=opencloud` explicitly — until then it's opt-in.

## 1. Required OpenCloud server state

Before the orchestrator can authenticate and operate, the instance must have:

1. **`PROXY_USER_OIDC_CLAIM=sub` set on day 0.** OpenCloud's proxy historically defaulted to `preferred_username`, which is *not* stable for a given end-user under OIDC §5.7. Switching the claim after any user has logged in orphans every Space, group, and share that user owns (tracked upstream as [`owncloud/ocis#6664`](https://github.com/owncloud/ocis/issues/6664)). The Compose service sets this env var in `docker-compose.yaml`; the `setup-opencloud.sh` pre-start hook logs a warning if it's ever missing.

2. **OIDC issuer wired to Keycloak.** Set `OC_OIDC_ISSUER` + `WEB_OIDC_METADATA_URL` to the Keycloak realm discovery URL:
   ```
   OC_OIDC_ISSUER=http://keycloak:8080/realms/srw
   WEB_OIDC_METADATA_URL=http://keycloak:8080/realms/srw/.well-known/openid-configuration
   ```
   The Compose service already sets both. On K8s, use the in-cluster Service DNS name.

3. **`OC_INSECURE=true` for local dev without TLS.** Required because the bundled Keycloak runs on plain HTTP; OpenCloud refuses OIDC over HTTP unless this flag is set.

4. **The `opencloud-admin` group exists in Keycloak** and the OpenCloud proxy is configured to map it to the internal admin role via `PROXY_ROLE_ASSIGNMENT_OIDC_CLAIM=groups`. This is what gives service-account tokens admin privileges on the LibreGraph API — without it, every `ensure_project_folder` / `ensure_group` call 403s.

## 2. Keycloak service-account setup

The adapter authenticates via Keycloak's `client_credentials` grant (NOT OpenCloud's internal `OCIS_SERVICE_ACCOUNT_*` mechanism, which is a private gRPC contract). You need two Keycloak clients and one group:

| Object | Kind | Purpose |
|---|---|---|
| `opencloud-web` | Public OIDC client | Browser login for end users on the OpenCloud Web UI |
| `opencloud-orchestrator` | Confidential service-account client | Orchestrator → LibreGraph admin calls |
| `opencloudAdmin` | Realm group | Membership grants the orchestrator admin privileges in OpenCloud. Name is baked into the OpenCloud proxy binary — must match exactly. |

`docker/keycloak/realm-export.json` pre-registers both clients, the `opencloudAdmin` group, and the service-account-to-group binding. On a fresh Compose stack, Keycloak imports all of this on first boot and no manual setup is needed — just:

```bash
podman-compose down -v     # if Keycloak's DB already has a stale realm
podman-compose up -d
```

### Drift repair on a persisted Keycloak DB

If you're running against a Keycloak DB that already imported an older realm (missing clients, wrong group name, etc.), run the idempotent seeder:

```bash
./docker/keycloak/setup-opencloud-client.sh
```

It verifies both clients exist (creating them if they don't), ensures the `opencloudAdmin` group exists, and adds the service-account user to the group. Runs against the live `srw-keycloak` container via `kcadm.sh`.

### Alternative: manual via the Keycloak admin UI

1. Log in to Keycloak at `http://localhost:8180` as `admin` / `admin`.
2. Switch to the `srw` realm.
3. Go to **Clients → opencloud-orchestrator → Service account roles**.
4. Click **Groups** tab → **Join Group** → select `opencloudAdmin`.
5. Save.

## 3. Orchestrator env vars

Paste the Keycloak client secret into the orchestrator's environment. For Compose dev, this goes in `.env`:

```bash
MAIN_CLOUD_BACKEND=opencloud
OPENCLOUD_URL=http://opencloud:9200
OPENCLOUD_PUBLIC_URL=http://localhost:9200
OPENCLOUD_KEYCLOAK_ISSUER=http://keycloak:8080/realms/srw
OPENCLOUD_KEYCLOAK_CLIENT_ID=opencloud-orchestrator
OPENCLOUD_KEYCLOAK_CLIENT_SECRET=opencloud-orchestrator-local-secret
OPENCLOUD_ADMIN_ROLE_CLAIM_VALUE=opencloud-admin
# Optional:
# OPENCLOUD_DEFAULT_QUOTA_BYTES=10737418240   # 10 GB per project
```

For Kubernetes, these land as secrets via Vault/ESO; refer to `deployment/19-opencloud.yaml` once it lands (not in Phase 2; Phase 3 adds the manifest as part of the default flip).

Restart the orchestrator so the new config is picked up:

```bash
podman-compose restart orchestrator
# or: kubectl rollout restart deploy/orchestrator
```

## 4. Verifying the bootstrap

Two smoke checks, parallel to the Nextcloud runbook.

### Healthcheck

```bash
curl -sf http://<orchestrator>/health/cloud
```

Expected: `{"ok": true, "backend": "opencloud", "latency_ms": <small>}`. If `ok: false`, check the orchestrator logs for `OpenCloud backend init failed` — common causes:

- **Keycloak unreachable** → `connection error` → OPENCLOUD_KEYCLOAK_ISSUER points at wrong host.
- **401 from Keycloak token endpoint** → wrong `OPENCLOUD_KEYCLOAK_CLIENT_SECRET` or the service-account user isn't in the `opencloud-admin` group yet.
- **LibreGraph 401** → token is valid but the proxy isn't mapping the `groups` claim to the admin role. Check `PROXY_ROLE_ASSIGNMENT_OIDC_CLAIM=groups` is set on the opencloud container.
- **404 on `/graph/v1.0/drives`** → the instance is running an old LibreGraph version; upgrade to ≥ v1.0.8.

### Project creation end-to-end

```bash
curl -X POST http://<orchestrator>/api/projects \
  -H 'Content-Type: application/json' \
  -d '{"name": "OpenCloud Bootstrap Test", "user_id": "<user-uuid>"}'
```

Expected: the response includes a `main_cloud_folder_handle`. Inside OpenCloud the Space named "OpenCloud Bootstrap Test" should be visible to the new project group. Confirm via the Web UI or the LibreGraph API:

```bash
curl -H "Authorization: Bearer $(token)" http://<opencloud>/graph/v1.0/drives
```

## 5. OIDC caveats

1. **Day-0 `sub` claim is non-negotiable.** The adapter's `ensure_initialized` cannot protect against a mid-deployment claim change; the ops hook only warns. If you discover `preferred_username` was used in error, the recovery is (a) back up data via WebDAV, (b) wipe the OpenCloud volume, (c) reprovision with `sub`, (d) restore. There is no in-place fix.

2. **First-login race on project-member sync.** Adding a user to a project immediately after their first OIDC login may fail if the OpenCloud LDAP-IDM has not yet materialized the user record. The adapter retries `resolve_user_identity` via `/graph/v1.0/users` `$search`, but if the user doesn't exist yet, the call returns `None` and the group-add is skipped. Access is granted on the user's second login via Keycloak's `groups` claim. Same UX as Nextcloud's OIDC path.

3. **Group naming.** LibreGraph's group display names are free-form strings. The adapter persists the orchestrator's `GroupId` (typically `project-<uuid>`) as the `displayName`. The backend-side UUID is resolved on every call via `$search=` + client-side exact match — never cache it on the orchestrator side, because LibreGraph has been known to assign a new UUID after certain re-provisioning flows.

## 6. Known OpenCloud / LibreGraph quirks

These affect the adapter regardless of how it is configured. Phase 2 handles them in code; recovery is an ops task when it ever comes up.

- **Disable-then-purge delete.** `DELETE /graph/v1.0/drives/{id}` on an enabled Space returns 400. You have to send `DELETE` with no headers first (disables), then `DELETE` with `Purge: T` (removes). This is not documented upstream; the adapter implements the two-step pattern automatically in `delete_project_folder`.

- **Composite drive id encoding.** Drive ids look like `<storageProviderId>!<spaceId>$<namespace>`. The `!` and `$` delimiters are allowed by the spec but many HTTP clients skip percent-encoding them, so the adapter explicitly `quote()`s every drive id before interpolation (see [`opencloud-eu/web#1795`](https://github.com/opencloud-eu/web/issues/1795)).

- **`$filter=mail eq '...'` is not supported** on `/graph/v1.0/users`. Only `memberOf/any(...)` and `appRoleAssignments/any(...)` are honored. The adapter uses `$search="..."` + client-side exact match instead.

- **Role UUIDs are per-deployment opaque.** LibreGraph returns roles by UUID and the spec explicitly says clients MUST treat the values as opaque. The adapter resolves display names ("Space Editor", "Space Viewer") to UUIDs via `/graph/v1beta1/roleManagement/permissions/roleDefinitions` at startup and caches them.

- **Sharing lives under `/graph/v1beta1/`.** Despite the beta prefix, the maintainers have stated the shape will be promoted to v1.0 additively. Treat v1beta1 as required; there is no v1.0 sharing API to fall back to.

- **`@libre.graph.recipient.type` annotation is required on invites.** Without it, the server assumes `user` and silently drops group invites. The adapter always sets the annotation explicitly.

## 7. Rotating the service-account secret

If the `opencloud-orchestrator` client secret leaks or needs rotation:

1. **Keycloak UI**: Clients → opencloud-orchestrator → Credentials → Regenerate.
2. **Or via kcadm**:
   ```bash
   podman exec srw-keycloak /opt/keycloak/bin/kcadm.sh \
       update "clients/<uuid>" -r srw -s "secret=<new-secret>"
   ```
3. Update `OPENCLOUD_KEYCLOAK_CLIENT_SECRET` in the backing secret store.
4. Rolling restart the orchestrator so the new value is read.

There is **no hot reload** in Phase 2 — the backend reads env vars once at process start. The cached access token will continue to work until its `expires_in` elapses (~5 min by default), so operators have a grace window during which the old secret can still authenticate to Keycloak but the next refresh will use the new secret.
