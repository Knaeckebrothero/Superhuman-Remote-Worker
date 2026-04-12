# Phase 4 — Main Cloud Settings UI

Phase 4 of the [main cloud abstraction](../features/main_cloud_abstraction.md) added an admin UI in the cockpit that lets operators edit the active main-cloud backend's configuration without a pod restart. This runbook covers what it looks like, what it can and cannot do, and how to recover from a bad config.

## 1. Where to find it

Log in to the cockpit as a user with the `admin` realm role. Navigate to **Settings** — the page the user already opens for their API keys and preferences. Scroll past **MCP Tokens** and **Codex Proxy** to the new **Cloud Storage (Main Cloud)** section. The section is only rendered when `currentUser().is_admin === true`.

The UI shows:

- **Status row** — active backend id, initialization state, refresh button.
- **Backend selector** — switch between `opencloud` and `nextcloud`. Hitting Save flips the active backend on this replica immediately and broadcasts to other replicas via `pg_notify`.
- **Non-secret form fields** — conditional on the selected backend. OpenCloud shows base URL, public URL, Keycloak issuer, Keycloak client id, admin role claim value, default Space quota. Nextcloud shows base URL, public URL, admin user, agent user.
- **Credentials ref** — an optional pointer like `env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET` that tells the loader which env var to read for secret fields. Leave empty to use the legacy per-backend env var (`OPENCLOUD_KEYCLOAK_CLIENT_SECRET` etc.).
- **Secret provenance** — a read-only table listing each secret field, the env var it resolves to, and whether that env var is set. Values are never displayed.
- **Buttons** — Test, Save + Reload, Reset to env defaults.

## 2. What Phase 4 *does*

- **Edit non-secret config in place.** URLs, usernames, client ids, quotas, admin role names — all editable via the UI.
- **Switch backends without a restart.** Pick `nextcloud` in the selector, save, and the active backend flips on every replica within one NOTIFY round-trip.
- **Validate + test before persisting.** The Test button builds a backend from the proposed overlay, calls `ensure_initialized` + `health_check`, tears it down. Returns latency + detail or a failure reason.
- **Persist with a provenance audit trail.** Every save records `updated_at` and `updated_by` (the admin's user id) on the `system_settings.main_cloud` row.
- **Hot-reload across replicas.** The PUT handler fires `NOTIFY main_cloud_config_changed`. Every orchestrator replica holds a LISTEN task that re-reads the overlay and swaps its local backend.
- **Preserve old-backend access after a switch.** `MainCloudRouter.replace_active` demotes the old backend into the `_legacy` cache when the id changes, so projects and threads created on the old backend keep working.

## 3. What Phase 4 does *not* do

- **Does not rotate secrets.** Passwords and client secrets stay in Vault / ESO / `.env`. The UI shows which env var each secret reads from and whether it's set; to change a secret, rotate it in your secret store and either restart the orchestrator or hit `POST /api/admin/system-settings/main_cloud/reload` to pick up the new value on this replica.
- **Does not migrate data.** Changing the active backend does not copy user files from the old backend to the new one. The Phase 2 "non-destructive switching" rules still apply: new projects land on the new backend, old projects stay where they were.
- **Does not run on auth tokens.** The endpoints are admin-only (`_require_admin`) and reject every non-admin request with 403. Audit log entries surface as orchestrator info-level logs under `POST /api/admin/system-settings/main_cloud`.
- **Does not persist across config wipes.** If an operator drops `system_settings.main_cloud` manually (via psql or the `DELETE` endpoint), the active backend rebuilds from env vars only. This is the intended "reset to defaults" behaviour.

## 4. REST API reference

All endpoints require the admin role. Base path: `/api/admin/system-settings/main_cloud`.

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/main_cloud` | — | `{effective, overlay, secrets, allowed_backends}` |
| `PUT` | `/main_cloud` | `{value, credentials_ref}` | `{status, backend_id, reloaded}` |
| `POST` | `/main_cloud/test` | `{value, credentials_ref}` | `{ok, detail, latency_ms}` |
| `POST` | `/main_cloud/reload` | — | `{status, backend_id}` |
| `DELETE` | `/main_cloud` | — | `{status, existed, backend_id?}` |

**PUT body shape:**

```json
{
  "value": {
    "backend_id": "opencloud",
    "base_url": "http://opencloud:9200",
    "public_url": "http://cloud.example.com",
    "keycloak_issuer": "http://keycloak:8080/realms/srw",
    "keycloak_client_id": "opencloud-orchestrator",
    "admin_role_claim_value": "opencloud-admin",
    "default_quota_bytes": 10737418240
  },
  "credentials_ref": "env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET"
}
```

Secret fields are silently dropped from `value` by the server-side sanitizer — the orchestrator never persists secrets in `system_settings.value`. The `credentials_ref` pointer is optional: if absent, the loader uses the legacy per-backend env var (`OPENCLOUD_KEYCLOAK_CLIENT_SECRET`, `NEXTCLOUD_ADMIN_PASSWORD`, etc.) for secret lookups.

## 5. Recovery: I saved a bad config and now the orchestrator is broken

Three recovery paths depending on how broken it is:

### Local replica only (PUT returned 500)

The PUT handler synchronously reloads after persisting. If the new backend fails to initialize, the handler returns `500` and the local replica is left on the previous active backend. The overlay IS still persisted though — so the next PUT will hit the same bad state. Fix by either:

1. **Reverting via the UI**: open the Cloud Storage section, fix the bad field, Save again.
2. **Dropping the overlay**: hit the `DELETE` endpoint (or click "Reset to env defaults" in the UI). The orchestrator rebuilds from env vars only.

### A notification was fired but a replica silently diverged

One replica's LISTEN task dropped the notification (network blip, reconnecting, whatever). Hit the per-replica reload endpoint:

```bash
curl -X POST http://<replica>:8085/api/admin/system-settings/main_cloud/reload \
     -H "Authorization: Bearer $ADMIN_TOKEN"
```

This is not broadcast — it only forces the replica you're pointing at. For a multi-pod fleet, `kubectl rollout restart deploy/orchestrator` picks up the overlay from the DB at boot time and is strictly simpler.

### The overlay itself is corrupt and I can't even open the UI

Drop the row directly via psql:

```sql
DELETE FROM system_settings WHERE key = 'main_cloud';
```

Then restart the orchestrator. The env-var-only path takes over and the UI becomes editable again.

## 6. Running the LISTEN task in dev

The LISTEN task is wired into `orchestrator/main.py::lifespan()` and starts automatically with the rest of the orchestrator. If the task dies (e.g. DB connection dropped), you'll see a warning log like:

```
Main cloud config LISTEN loop: <error> — reconnecting in 2.0s
```

The task reconnects with exponential backoff (1s → 30s cap). During the reconnect window, PUTs on this replica still apply locally via the synchronous reload path — the LISTEN task is only needed to pick up PUTs issued against *other* replicas.

For test fixtures that need deterministic behaviour, the task is disabled by the existing `tests/conftest.py` setup (it stubs out `postgres_db._pool`). If you write a test that exercises the LISTEN path directly, construct a `MainCloudRouter` in-process and call `reload_from_db` directly rather than firing NOTIFYs.

## 7. Where secrets live

Phase 4 deliberately did not move secrets into the database. The justification:

- **Vault/ESO/.env already own secret rotation.** Duplicating that flow in the orchestrator's DB means two places to rotate, two places to leak, and two places to audit.
- **Postgres `system_settings.value` is unencrypted JSONB.** Storing a Keycloak client secret there and then logging `SELECT * FROM system_settings` would leak it.
- **The cockpit's admin UI would have to mask + transmit secrets.** HTTPS + Bearer tokens handle this fine in production, but dev/homelab setups running plain HTTP inside a podman network would regularly leak secrets to browser tabs, tcpdumps, and developer terminal history.

The cost of keeping secrets in env vars is that rotation still requires a pod restart (or a manual `/api/admin/system-settings/main_cloud/reload` call after the env has been updated out of band). For Phase 4 that's the right tradeoff — non-secret config changes are frequent, secret rotations are rare.
