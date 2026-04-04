# Deployment Roadmap — Internal Testing Release

> **Scope:** This document covers the **Kubernetes production deployment** only.
> For Docker Compose deployment (development and small deployments), see
> [`docs/docker_compose_mode.md`](docker_compose_mode.md). For the overall
> deployment strategy, see [`docs/deployment.md`](deployment.md).

**Date:** 2026-03-19 (initial), last updated 2026-03-20
**Target:** Update the existing K3s deployment to reflect current codebase, then expose to the internet.
**Domain:** `superhuman-remote-worker.com` (registered on Cloudflare 2026-03-19)

---

## Completed — Phase 1 + 2 (2026-03-19)

Phase 1 (manifest updates + internal deploy) and Phase 2 (internet exposure via Cloudflare Tunnel) were completed in a single session. The stack is live and publicly accessible.

### What was done

#### Domain & DNS

Registered `superhuman-remote-worker.com` on Cloudflare. All services use subdomains on this domain. CNAME records point to the existing Cloudflare Tunnel (deployed in `cloudflare-tunnel` namespace, 2 replicas). Tunnel routing configured via the Zero Trust dashboard.

| Service | URL | Auth |
|---------|-----|------|
| Cockpit | `https://superhuman-remote-worker.com` | Keycloak OIDC |
| API | `https://api.superhuman-remote-worker.com` | Bearer token |
| Keycloak | `https://auth.superhuman-remote-worker.com` | Admin console |
| Gitea | `https://git.superhuman-remote-worker.com` | Keycloak OIDC |
| MCP | `https://mcp.superhuman-remote-worker.com` | MCP token |
| Nextcloud | `https://cloud.superhuman-remote-worker.com` | Admin / Keycloak OIDC |
| pgAdmin | `https://pgadmin.superhuman-remote-worker.com` | Internal only |
| Mongo Express | `https://mongo.superhuman-remote-worker.com` | Internal only |
| Neo4j | `https://neo4j.superhuman-remote-worker.com` | Internal only |
| Dozzle | `https://dozzle.superhuman-remote-worker.com` | Internal only |

Admin UIs (pgAdmin, Mongo Express, Neo4j, Dozzle) have DNS records but are intended for internal access only — they should be excluded from the Cloudflare Tunnel or protected with Cloudflare Access policies.

#### Email

Created a dedicated Proton Mail account (`project@redacted.invalid`) with custom domain aliases:
- `agent@superhuman-remote-worker.com` — `AGENT_EMAIL`, IMAP reply routing inbox
- `noreply@superhuman-remote-worker.com` — `SMTP_FROM`, outbound notification sender

Both are aliases on the same inbox. A dedicated Proton Bridge instance (`deployment/25-protonmail-bridge.yaml`) runs in the SRW namespace with credentials copied from local Podman volume.

#### S3 / MinIO

Uses the **existing cluster MinIO** at `minio.minio.svc:9000` (3-node StatefulSet in `minio` namespace, 3x4TB SSDs, erasure coding). No new MinIO deployment needed.

Two buckets with separate IAM-scoped credentials:

| Bucket | Used by | Access key | Policy |
|--------|---------|------------|--------|
| `srw-snapshots` | Orchestrator (VM snapshots, IDE sessions) | `srw-snapshots` | Bucket-scoped read/write/create |
| `srw-nextcloud` | Nextcloud primary object storage | `srw-nextcloud` | Bucket-scoped read/write/create |

Both auto-create on first connection (`OBJECTSTORE_S3_AUTOCREATE` / orchestrator's `_ensure_bucket()`).

#### New manifests created

| File | Description |
|------|-------------|
| `deployment/18-keycloak.yaml` | Keycloak 26.2 with realm import, `--health-enabled=true`, health probes on port 9000, wait-for-postgres init container |
| `deployment/19-nextcloud.yaml` | Nextcloud 31-apache, PostgreSQL backend on srw-postgres, S3 primary storage on cluster MinIO, 10Gi PVC |
| `deployment/24-dozzle.yaml` | Codified from live cluster — ServiceAccount, ClusterRole, ClusterRoleBinding, Deployment, Service |
| `deployment/25-protonmail-bridge.yaml` | Proton Bridge for SRW email account, 1Gi PVC, IMAP:1143 + SMTP:1025 |

#### Updated manifests

| File | Changes |
|------|---------|
| `01-secrets.yaml` | Added: Keycloak, Nextcloud, S3 (separate keys per bucket), MCP_INTERNAL_KEY, Proton Bridge SMTP/IMAP password, notification webhooks, WebDAV, Claude Code token. Populated all API keys and VPN credentials from .env |
| `02-configmap.yaml` | Added: Keycloak SSO, CORS_ORIGINS, COCKPIT_EXTERNAL_URL, S3/MinIO (cluster MinIO endpoint), IDE sessions, SMTP/IMAP (Proton Bridge in-namespace), Ntfy, WebDAV, WORKSTATION_BASE_URL, GITEA_INTERNAL_URL. Updated env.js with new domain + Keycloak config. Removed: SESSION_TIMEOUT_HOURS. Updated VPN config with real credentials/endpoints. Updated DB connection strings with real passwords |
| `10-postgres.yaml` | Added: `srw-postgres-init` ConfigMap with `init_sso_dbs.sh` mounted into entrypoint |
| `12-gitea.yaml` | Updated ROOT_URL to `https://git.superhuman-remote-worker.com` (OIDC env vars were already present) |
| `20-orchestrator.yaml` | Added: wait-for-keycloak init container, Keycloak/CORS/S3/IDE/SMTP/IMAP/webhooks/WebDAV/GITEA_INTERNAL_URL env vars. Removed: SESSION_TIMEOUT_HOURS, stale admin vars |
| `21-agent.yaml` | Added: WORKSTATION_BASE_URL, GITEA_URL |
| `23-mcp.yaml` | Added: MCP_INTERNAL_KEY env var |
| `30-ingress.yaml` | All hostnames updated to `*.superhuman-remote-worker.com`. Added: Keycloak, Nextcloud, Dozzle ingress rules. Removed: X-CSRF-Token from CORS middleware |
| `docker/keycloak/realm-export.json` | Added production redirect URIs for all clients (cockpit, gitea, nextcloud, pgadmin) alongside localhost dev URIs |

#### Bug fix during deployment

`PyJWT[crypto]` was missing from `orchestrator/requirements.txt` — the OIDC module (`security/oidc.py`) imports `jwt` but the package was never added as a dependency. The orchestrator crashed with `ModuleNotFoundError: No module named 'jwt'`. Fixed by adding `PyJWT[crypto]>=2.8.0` to requirements and rebuilding the image.

#### Keycloak health probe fix

Keycloak 26.2 in dev mode serves health endpoints on port 9000, not 8080, and requires `--health-enabled=true` to activate them. The initial manifest had probes on port 8080 which returned 404, causing startup probe failures and pod restarts. Fixed by adding the flag and changing probe ports to 9000.

#### Deployment procedure used

1. Deleted all existing resources (deployments, statefulsets, services, ingresses, configmaps, secrets, PVCs) except `srw-protonmail-bridge-data` PVC
2. Applied infrastructure: secrets, configmap, workspace PVC, postgres (both), mongodb, neo4j, gitea, keycloak, nextcloud
3. Waited for postgres pods to be ready
4. Applied application layer: VPN sidecars, orchestrator, agents, cockpit, MCP, dozzle, proton bridge, pgadmin, mongo express
5. Applied ingress + CORS middleware
6. Fixed Keycloak health probes, reapplied
7. Restarted orchestrator/cockpit/agent/MCP after Keycloak was ready
8. Fixed PyJWT dependency, rebuilt image, restarted orchestrator
9. Configured Cloudflare Tunnel routing via Zero Trust dashboard
10. Verified all 6 public endpoints returning 200

#### Hardening session (2026-03-20)

- Changed Keycloak admin password (was `admin`/`admin`, now in `srw-secrets`)
- Implemented user registration with admin approval: Keycloak `verifyEmail: true`, default roles exclude `user`, cockpit pending-approval screen, orchestrator `require_approved_user()` on all endpoints except `/api/auth/me`
- Fixed cockpit debug sidebar links (env.js ConfigMap with correct URLs)
- Fixed IPv6 DNS bypass (MikroTik AAAA wildcard → `::1`)
- Automated Gitea OIDC: postStart lifecycle hook creates admin user + Keycloak OAuth2 source. Orchestrator `GiteaClient.ensure_oidc_configured()` as future fallback (Gitea admin auth API not available in 1.22). Fixed `ensure_initialized()` to use unauthenticated request for version check.
- Automated Nextcloud OIDC: ConfigMap `srw-nextcloud-hooks` with `before-starting` hook script. Installs `user_oidc` app, registers Keycloak provider. Non-fatal (`set +e`) so failures don't crash Nextcloud.
- Added OIDC client secrets to `01-secrets.yaml` (`GITEA_OIDC_CLIENT_SECRET`, `NEXTCLOUD_OIDC_CLIENT_SECRET`)

| File | Changes |
|------|---------|
| `01-secrets.yaml` | Added OIDC client secrets |
| `02-configmap.yaml` | Updated cockpit env.js URLs, added `srw-nextcloud-hooks` ConfigMap |
| `12-gitea.yaml` | Added wait-for-keycloak initContainer, postStart lifecycle hook (admin user + OIDC bootstrap), bootstrap env vars |
| `19-nextcloud.yaml` | Added wait-for-keycloak initContainer, OIDC env vars, oidc-hook volume mount |
| `20-orchestrator.yaml` | Added `GITEA_OIDC_CLIENT_SECRET` env var |
| `docker/keycloak/realm-export.json` | Enabled `verifyEmail`, added SMTP config for Proton Bridge |
| `orchestrator/security/auth.py` | Added `is_approved` flag, `require_approved_user()` |
| `orchestrator/main.py` | Switched to `require_approved_user()`, added OIDC setup call |
| `orchestrator/services/gitea.py` | Added `ensure_oidc_configured()`, fixed unauthenticated version check |
| `cockpit/src/app/app.ts` | Added pending-approval screen |
| `cockpit/src/app/core/services/user.service.ts` | Added `isApproved` signal |
| `cockpit/src/app/core/models/api.model.ts` | Added `is_approved` to User interface |

### Current state (2026-03-20)

**20 pods running**, all healthy, zero crash loops:

| Component | Pods | Status |
|-----------|------|--------|
| PostgreSQL (app) | 1 | Running |
| PostgreSQL (vector) | 1 | Running |
| MongoDB | 1 | Running |
| Neo4j | 1 | Running |
| Gitea | 1 | Running |
| Keycloak | 1 | Running |
| Nextcloud | 1 | Running |
| Orchestrator | 1 | Running |
| Agent | 2 | Running |
| Cockpit | 1 | Running |
| MCP | 1 | Running |
| Proton Bridge | 1 | Running |
| Dozzle | 1 | Running |
| VPN (cluster) | 1 | Running |
| VPN (research) | 1 | Running |
| VPN (workstation) | 1 | Running |
| pgAdmin | 1 | Running |
| Mongo Express | 1 | Running |

**Storage:**

| PVC | Size | Mode | Used By |
|-----|------|------|---------|
| srw-postgres-data | 10Gi | RWO | App DB |
| srw-postgres-vector-data | 10Gi | RWO | Vector DB |
| srw-mongodb-data | 5Gi | RWO | MongoDB |
| srw-neo4j-data | 10Gi | RWO | Neo4j |
| srw-gitea-data | 5Gi | RWO | Gitea |
| srw-nextcloud-data | 10Gi | RWO | Nextcloud |
| srw-protonmail-bridge-data | 1Gi | RWO | Proton Bridge credentials |
| srw-workspace | 20Gi | RWX | Orchestrator + Agents |

---

## Remaining Tasks

### Immediate (security)

- [x] **Change Keycloak admin password** — changed from default, stored in `srw-secrets`
- [x] **User registration with admin approval** — email verification enabled, new users lack `user` role until admin grants it in Keycloak. Cockpit shows "pending approval" screen for unapproved users, API returns 403 via `require_approved_user()`.
- [x] **Gitea OIDC setup** — automated via postStart lifecycle hook on Gitea container. Creates admin user + registers Keycloak OAuth2 source on every pod start. Persisted in SQLite on PVC. Legacy script `docker/keycloak/setup-gitea-oidc.sh` deprecated.
- [x] **Nextcloud OIDC setup** — automated via `before-starting` hook (ConfigMap `srw-nextcloud-hooks`). Installs `user_oidc` app + registers Keycloak provider on every container start. Legacy script `docker/keycloak/setup-nextcloud-oidc.sh` deprecated.
- [x] **Debug sidebar links fixed** — cockpit env.js ConfigMap updated with correct `*.superhuman-remote-worker.com` URLs for all admin UIs
- [x] **IPv6 DNS bypass fixed** — MikroTik static DNS AAAA record blocks Cloudflare IPv6 wildcard, forcing local clients to resolve via IPv4 to Traefik

### Testing

- [ ] Log in via Keycloak at `https://superhuman-remote-worker.com`
- [ ] Verify JIT user provisioning (Keycloak login creates local user row)
- [ ] Create a job, verify agents pick it up and execute
- [ ] Test MCP server with API token (`X-MCP-Token` auth)
- [ ] Verify VPN sidecars healthy (LLM routing works)
- [ ] Test email notifications (send a test agent message)
- [ ] Verify Nextcloud WebDAV access from agent

### Hardening (before wider rollout)

- [ ] Keycloak brute-force protection tuning (currently enabled, 5 failures / 15min lockout)
- [ ] Rate limiting on public endpoints (Cloudflare rules)
- [ ] Review `LOG_LEVEL` — currently `DEBUG`, may leak info in responses
- [ ] Protect admin UIs with Cloudflare Access or remove from tunnel
- [ ] Consider Keycloak production mode (`start` instead of `start-dev`)
- [ ] Image tagging strategy (currently `:latest`)
- [x] ~~Update MikroTik DNS~~ — A records for `*.superhuman-remote-worker.com` → `10.0.51.11` + AAAA wildcard → `::1` to block Cloudflare IPv6 bypass

### Deferred

- [ ] Configure Slack/Discord/Ntfy notification webhooks (env vars wired but empty)
- [ ] Monitoring (Prometheus/Grafana — not in current stack)
- [ ] Update `deployment/README.md` to reflect new architecture
- [ ] Update `docker-compose.yaml` and `docker-compose.local.yaml` to match K8s manifests (Keycloak, Nextcloud, updated env vars)
- [ ] Secrets management (HashiCorp Vault + Fleet)

---

## Infrastructure Notes

### Existing cluster services used (not deployed by SRW)

| Service | Namespace | Access from SRW |
|---------|-----------|-----------------|
| MinIO (S3) | `minio` | `minio.minio.svc:9000` |
| Cloudflare Tunnel | `cloudflare-tunnel` | Routes configured via Zero Trust dashboard |
| NATS | `nats` | `nats.nats.svc.cluster.local:4222` |
| cert-manager | `cert-manager` | ClusterIssuer: `cloudflare-dns-issuer` |
| Traefik | `traefik` | Ingress controller at `10.0.51.11` |
| MetalLB | `metallb-system` | BGP mode, IPs from `10.0.51.0/24` |
| Longhorn | `longhorn-system` | Default storage class |

### Cloudflare Tunnel routing

The existing tunnel in `cloudflare-tunnel` namespace (2 replicas, image `cloudflare/cloudflared:2024.1.5`) serves multiple projects. SRW routes were added via the Cloudflare Zero Trust dashboard (not the ConfigMap, due to a token refresh issue with pod restarts on the pinned image version).

Public routes (through tunnel):
- `superhuman-remote-worker.com` → `srw-cockpit.superhuman-remote-worker.svc:4000`
- `api.superhuman-remote-worker.com` → `srw-orchestrator.superhuman-remote-worker.svc:8085`
- `auth.superhuman-remote-worker.com` → `srw-keycloak.superhuman-remote-worker.svc:8080`
- `git.superhuman-remote-worker.com` → `srw-gitea.superhuman-remote-worker.svc:3000`
- `mcp.superhuman-remote-worker.com` → `srw-mcp.superhuman-remote-worker.svc:8055`
- `cloud.superhuman-remote-worker.com` → `srw-nextcloud.superhuman-remote-worker.svc:80`

API and MCP routes have SSE buffering disabled for streaming support.
