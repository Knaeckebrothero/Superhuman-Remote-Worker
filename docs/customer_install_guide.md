# Superhuman Remote Worker — Prototype Install Guide

**Audience:** customer ops / platform team and the SRW deployment engineer
running the install on-site.

**Scope:** **prototype / showcase deployment.** Everything bundled inside the
cluster (Keycloak, Gitea, OpenCloud, Postgres, pgvector, MongoDB). The only
customer-side integration is the network edge: DNS + reverse-proxy with TLS
termination. Federation to the customer's IdP, integration with managed
databases, MS365 cloud, mailcow SMTP, etc. are **Phase 2** items, not part
of this guide.

**Chart:** `oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker`
**Latest version at time of writing:** `0.0.19`
**Source repository:** <https://github.com/knaeckebrothero/Superhuman-Remote-Worker> (private — request access)

For shapes outside this prototype scope (BYO IdP, managed DBs, air-gapped),
see the chart README (`helm show readme oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker --version 0.0.19`).

---

## 1. What gets deployed

| Component | Purpose | This install |
|---|---|---|
| `orchestrator` | FastAPI control plane (REST API, job dispatch, MCP integration) | always on |
| `agent` | LangGraph job executor (worker + persistent session pools) | always on |
| `cockpit` | Angular web UI | always on |
| `workspace` | Per-job isolated PVC + SSH workspace pods | always on |
| `keycloak` | OIDC provider — users created in its admin UI | **bundled** |
| `gitea` | Git server for agent code workspaces | **bundled** |
| `opencloud` | Cloud storage backend | **bundled** |
| `postgres` | Application database (jobs, users, projects) | **bundled** |
| `postgres-vector` | pgvector for embeddings, citations, memories | **bundled** |
| `mongodb` | Audit trail | **bundled** |
| `neo4j` | Project knowledge graph | **off** (can enable later) |
| `mcp` | MCP server for Claude Code integration | **off** (can enable later) |
| `pgadmin`, `mongo-express`, `dozzle` | Admin UIs | off |

---

## 2. What the customer provides

### 2.1 Kubernetes

- **k3s 1.28+** on a single node, or a larger cluster. k3s is recommended
  for prototypes — it ships with Traefik ingress and a default
  local-path StorageClass, both of which the chart uses out of the box.
- **Minimum resources (prototype):** 8 vCPU / 16 GiB RAM / 100 GiB disk.
  Comfortable for 1–3 concurrent agents. Scale up for more.
- A default `StorageClass` (k3s `local-path` is fine).
- Outbound internet from the cluster to GHCR (`ghcr.io`) for image pulls.

### 2.2 DNS

Records pointing at the customer's **edge reverse proxy** IP. One per
hostname the chart exposes:

```
srw.example.com           # cockpit (root)
api.srw.example.com       # orchestrator REST + BFF
auth.srw.example.com      # bundled Keycloak
git.srw.example.com       # bundled Gitea
cloud.srw.example.com     # bundled OpenCloud
```

Use a wildcard `*.srw.example.com` if the customer prefers — easier to
maintain.

If a subdomain is already in use in the parent zone, override it
individually under `global.hostnames` in the values file (see §5).

### 2.3 Edge reverse proxy — TLS termination

TLS is terminated **at the customer's edge proxy** (in this engagement:
Zoraxy). The k3s ingress runs HTTP-only inside the cluster.

Per route, the customer configures on their proxy:

| Setting | Value |
|---|---|
| Public hostname | one of the five above |
| Upstream / backend | `http://<k3s-ingress-ip>:80` (or the NodePort, e.g. `:32080`) |
| TLS | enable with the customer's preferred ACME setup (Let's Encrypt, ZeroSSL, internal CA) |
| Pass `Host` header | **yes** — required for the cluster Ingress to route by hostname |
| WebSocket / SSE | enabled (Zoraxy enables both automatically) |
| HTTP/2 | enabled |

This is the **only customer-side integration** this install requires.
Cluster-side `cert-manager` is not used.

### 2.4 LLM provider keys

At least one provider account. Customer-billed, customer-owned. **These are
NOT placed in the Kubernetes Secret** — they're configured post-install via
the cockpit Admin → Providers UI, which stores them encrypted (using
`APP_ENCRYPTION_KEY`) in the database.

Common providers: OpenAI, Anthropic, Groq, Google Gemini, OpenRouter,
Tavily (web search). Need at least one chat model + ideally one embedding
model.

---

## 3. Pre-flight checklist

Run through these on the cluster **before** `helm install`.

```bash
# Kubernetes version
kubectl version --short
#   → server ≥ v1.28

# Default storage class exists
kubectl get storageclass
#   → one entry has (default) annotation (k3s ships `local-path` (default))

# Traefik ingress controller (k3s default)
kubectl -n kube-system get pods | grep traefik
#   → Running

# DNS records resolve to the edge proxy
dig +short srw.example.com
dig +short api.srw.example.com
dig +short auth.srw.example.com
dig +short git.srw.example.com
dig +short cloud.srw.example.com
#   → all return the edge proxy IP

# Edge proxy can reach k3s
# (from the proxy host)
curl -sI http://<k3s-node-ip>/
#   → 404 from Traefik is fine — it means HTTP works, just no Host match yet
```

---

## 4. Secret creation

The chart references a Kubernetes Secret named `srw-secrets` for cluster
credentials. **LLM provider keys are NOT in this Secret** — those go into
the database via the Admin UI after install (see §9).

### 4.1 Generate the encryption key

```bash
openssl rand -base64 32
#   → save to the customer's password manager IMMEDIATELY.
#     If lost, every stored credential in the DB becomes unrecoverable.
```

### 4.2 Author `srw.env`

```env
# --- Always required ---
APP_ENCRYPTION_KEY=<paste from 4.1>

# --- Bundled Postgres ---
POSTGRES_USER=srw
POSTGRES_PASSWORD=<random 32-char string>
VECTOR_POSTGRES_USER=srw
VECTOR_POSTGRES_PASSWORD=<random 32-char string>

# --- Bundled Keycloak ---
KEYCLOAK_ADMIN_USER=admin
KEYCLOAK_ADMIN_PASSWORD=<random 32-char string>
KC_DB_PASSWORD=<random 32-char string>
KC_REALM_ADMIN_PASSWORD=<random 32-char string>

# --- Bundled Gitea ---
GITEA_ADMIN_USER=srw-admin
GITEA_ADMIN_PASSWORD=<random 32-char string>
GITEA_OIDC_CLIENT_SECRET=<random 32-char string>   # Gitea ↔ bundled Keycloak

# --- Bundled OpenCloud ---
OPENCLOUD_KEYCLOAK_CLIENT_SECRET=<random 32-char string>   # OpenCloud ↔ bundled Keycloak
CLOUD_SERVICE_USER=srw-agent
CLOUD_SERVICE_PASSWORD=<random 32-char string>
```

> The chart auto-provisions the bundled Keycloak's realm, the Gitea OAuth
> source, the OpenCloud OIDC client, and the agent's account on OpenCloud —
> all using the secrets above. No manual OIDC client setup required.

### 4.3 Apply

```bash
kubectl create namespace srw
kubectl -n srw create secret generic srw-secrets --from-env-file=./srw.env

# Verify keys present
kubectl -n srw get secret srw-secrets -o jsonpath='{.data}' | \
  python3 -c "import sys, json; print('\n'.join(sorted(json.load(sys.stdin).keys())))"
```

Delete `srw.env` from disk once the Secret is applied. The
`APP_ENCRYPTION_KEY` belongs in the customer's password manager.

---

## 5. Values file

```yaml
# my-values.yaml
license:
  acceptTerms: true

# Pin resource names to `srw-*` (matches the verification commands in §7).
fullnameOverride: srw

global:
  domain: srw.example.com
  # imagePullSecrets:
  #   - name: ghcr-pull-secret   # only if any of the SRW images are private

  # Override a subdomain if the flat default conflicts in the parent zone:
  # hostnames:
  #   git: git-srw.example.com

secrets:
  existingSecret: srw-secrets

# --- All identity via bundled Keycloak ---
keycloak:
  enabled: true
  internal: true
  realm: srw

# --- Bundled Git ---
gitea:
  enabled: true
  internal: true

# --- Bundled cloud storage (OpenCloud) ---
opencloud:
  enabled: true
nextcloud:
  enabled: false
cloud:
  externalBackend: ""   # empty = use the bundled cloud above

# --- Bundled databases ---
databases:
  postgres:
    enabled: true
    internal: true
  vector:
    enabled: true
    internal: true
  mongodb:
    enabled: true
    internal: true
  neo4j:
    enabled: false   # turn on later if knowledge-graph features are demoed

# --- Required agent config + small pool for a prototype ---
agent:
  config: defaults
  pool:
    minAgents: "1"
    maxAgents: "3"
    reservedSessionSlots: "1"
    reservedJobSlots: "1"

# --- Ingress — HTTP only; TLS terminated at the customer's edge proxy ---
ingress:
  enabled: true
  className: traefik
  tls:
    enabled: false

# --- Auth cookies — emit Secure flag (browser sees HTTPS via the edge proxy) ---
auth:
  bff:
    cookieSecure: "1"
    cookieSamesite: "lax"
    cookieDomain: "auto"

# --- Off by default; enable when the matching capability is in scope ---
mcp:
  enabled: false
headscale:
  enabled: false
pgadmin:
  enabled: false
mongoExpress:
  enabled: false
dozzle:
  enabled: false
codexProxy:
  enabled: false

logLevel: INFO
```

---

## 6. Install

```bash
helm install srw \
  oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker \
  --version 0.0.19 \
  --namespace srw \
  -f my-values.yaml
```

First install takes 3–6 minutes (database init + migrations + Keycloak
realm bootstrap + Gitea bootstrap + OpenCloud OIDC client setup).

```bash
kubectl -n srw get pods -w
```

---

## 7. Post-install verification

```bash
kubectl -n srw rollout status deploy/srw-orchestrator --timeout=300s
kubectl -n srw rollout status deploy/srw-cockpit --timeout=180s
kubectl -n srw rollout status statefulset/srw-postgres --timeout=180s
kubectl -n srw rollout status statefulset/srw-postgres-vector --timeout=180s
kubectl -n srw rollout status statefulset/srw-keycloak --timeout=300s

# No pods in CrashLoopBackOff
kubectl -n srw get pods | grep -v Running | grep -v Completed
#   → empty result is good

# Orchestrator health
kubectl -n srw exec deploy/srw-orchestrator -- curl -sf http://localhost:8085/health

# Migrations applied cleanly
kubectl -n srw logs deploy/srw-orchestrator | grep -i "migration"
#   → "Migrations applied: NN" with no errors

# Encryption key loaded
kubectl -n srw logs deploy/srw-orchestrator | grep -iE "encryption|app_encryption"
#   → no "missing" / "invalid"
```

---

## 8. Edge proxy route configuration

(Customer-side. Configure these after the cluster install completes.)

### Find the cluster ingress IP

```bash
kubectl -n kube-system get svc traefik
#   → note the EXTERNAL-IP (LoadBalancer) or the NodePort under PORT(S)
```

On single-node k3s with `klipper-lb` (default), `EXTERNAL-IP` is the node's
own IP. If the edge proxy is on a different host, that IP must be reachable
from it on port 80.

### Configure one route per hostname

In the edge proxy admin UI (Zoraxy: *Add Proxy Rule*), create five entries:

| Public hostname | Upstream | Notes |
|---|---|---|
| `srw.example.com` | `http://<k3s-ip>:80` | Cockpit web UI |
| `api.srw.example.com` | `http://<k3s-ip>:80` | Orchestrator REST + BFF |
| `auth.srw.example.com` | `http://<k3s-ip>:80` | Bundled Keycloak |
| `git.srw.example.com` | `http://<k3s-ip>:80` | Bundled Gitea |
| `cloud.srw.example.com` | `http://<k3s-ip>:80` | Bundled OpenCloud |

For each:

- **TLS:** issue/attach a cert. Zoraxy's built-in ACME (via go-lego)
  supports Let's Encrypt, ZeroSSL, Buypass, Google CA, plus DNS-01 for
  most providers. The customer chooses.
- **Pass `Host` header:** **required** — Traefik routes by Host. Zoraxy's
  default behavior preserves it.
- **WebSocket:** enabled by default in Zoraxy (`websocketproxy`). The
  cockpit's persistent-chat / session-stream features depend on this.
- **SSE:** plain HTTP streaming; works automatically.
- **Skip upstream TLS verify:** not applicable — upstream is HTTP.

### Verify

Browse to `https://srw.example.com`. You should be redirected to
`https://auth.srw.example.com/realms/srw/protocol/openid-connect/auth?…`
(bundled Keycloak's login page), with a valid TLS cert chain everywhere.

---

## 9. First-user setup + LLM providers

### Create the first user in Keycloak

1. Open `https://auth.srw.example.com/admin/` in a browser.
2. Sign in: `admin` / `KEYCLOAK_ADMIN_PASSWORD` from §4.2.
3. Top-left realm dropdown → switch from `master` to `srw`.
4. **Users → Add user.** Fill in `Email`, `First/Last name`, set
   `Email verified` ON.
5. **Credentials** tab → *Set password*. Untick `Temporary` if you don't
   want a forced reset on first login.
6. **Role mapping** tab → assign `admin` and `user` realm roles.

### Promote the user in SRW's database (one-time)

The first login creates a `users` row with `is_approved=false`,
`is_admin=false`. Flip both:

```bash
kubectl -n srw exec sts/srw-postgres -- \
  psql -U srw -d srw -c \
    "UPDATE users SET is_approved=true, is_admin=true
     WHERE email='first.user@customer.example.com';"
```

(Subsequent users created from inside SRW's Admin UI inherit roles from
Keycloak claims and don't need this step.)

### Log in + configure LLM providers

1. Browse `https://srw.example.com` and sign in.
2. Sidebar → **Admin → Providers** → add at least one chat-model provider
   and one embedding-model provider. Pick a default model.

The system is now usable.

---

## 10. Upgrade

```bash
helm upgrade srw \
  oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker \
  --version <new-version> \
  -n srw -f my-values.yaml
```

`APP_ENCRYPTION_KEY` is preserved (lookup-based). Database migrations
apply on orchestrator startup (advisory-lock + checksum-tracked; see
`docs/db_migration.md`).

When only ConfigMap-projected values change (no chart/image bump), force
the affected pods to re-roll:

```bash
helm upgrade srw … -f my-values.yaml
kubectl -n srw rollout restart deploy/srw-cockpit deploy/srw-orchestrator
```

---

## 11. Rollback

```bash
helm history srw -n srw
helm rollback srw <REVISION> -n srw
```

Database migrations are **not** auto-reverted. The schema is
forward-compatible by design (additive migrations only). For a destructive
upgrade you need a snapshot taken before the upgrade.

---

## 12. Uninstall

```bash
helm uninstall srw -n srw

# PVCs are intentionally retained — keep them, or delete:
kubectl -n srw delete pvc -l app.kubernetes.io/instance=srw

# Drop the namespace:
kubectl delete namespace srw
```

---

## 13. Secret schema (this install)

| Key | Required | Notes |
|---|---|---|
| `APP_ENCRYPTION_KEY` | always | base64 32 bytes. Loss = unrecoverable encrypted credentials |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | always | bundled Postgres |
| `VECTOR_POSTGRES_USER` / `VECTOR_POSTGRES_PASSWORD` | always | bundled pgvector |
| `KEYCLOAK_ADMIN_USER` / `KEYCLOAK_ADMIN_PASSWORD` | always | bundled Keycloak admin login |
| `KC_DB_PASSWORD` | always | Keycloak's dedicated Postgres |
| `KC_REALM_ADMIN_PASSWORD` | always | realm-level admin (for kcadm bootstrap) |
| `GITEA_ADMIN_USER` / `GITEA_ADMIN_PASSWORD` | always | bundled Gitea admin login |
| `GITEA_OIDC_CLIENT_SECRET` | always | Gitea ↔ Keycloak OAuth |
| `OPENCLOUD_KEYCLOAK_CLIENT_SECRET` | always | OpenCloud ↔ Keycloak OAuth |
| `CLOUD_SERVICE_USER` / `CLOUD_SERVICE_PASSWORD` | always | agent's account on OpenCloud |
| `MCP_OIDC_CLIENT_SECRET` | only if `mcp.enabled=true` | |
| `NEO4J_USERNAME` / `NEO4J_PASSWORD` | only if `neo4j.enabled=true` | no `/` in the password |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | only if email features enabled | trivial to wire in if mailcow is reachable |
| `NTFY_URL` / `NTFY_TOPIC` / `NTFY_TOKEN` | optional | push notifications |
| `DISCORD_WEBHOOK_URL` / `SLACK_WEBHOOK_URL` | optional | chat notifications |

**Not in the Secret** (configured post-install via Admin UI, stored
encrypted in DB): `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`,
`GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`, embedding model
endpoints.

---

## Appendix — Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `orchestrator` `CrashLoopBackOff`, logs say `APP_ENCRYPTION_KEY` missing/invalid | Secret not created or key absent | Re-create per §4 |
| Cockpit loads but login bounces / "Invalid redirect_uri" | Edge proxy not passing the `Host` header, or DNS for `auth.<domain>` not pointing at the edge proxy | Verify §2.2 and §8 |
| `502 Bad Gateway` from edge proxy on every host | k3s ingress IP changed or NodePort moved | Re-check `kubectl -n kube-system get svc traefik`, update upstream in proxy |
| WebSocket fails (chat shows "disconnected") | Edge proxy not forwarding `Upgrade: websocket` | Confirm WebSocket toggle is on for the host (Zoraxy enables by default) |
| Cockpit shows "no LLM providers configured" | Expected on first login | Configure in §9 Step 3 |
| Jobs stuck in `created` | Agent pool size = 0 or agents `Offline` | `kubectl -n srw get pods -l app.kubernetes.io/component=agent`; agents heartbeat every 5s, go offline after 3min |
| OpenCloud login fails | `OPENCLOUD_KEYCLOAK_CLIENT_SECRET` mismatch, or the in-realm OIDC client wasn't created | `kubectl -n srw logs job/srw-keycloak-bootstrap` (the realm bootstrap Job's output) |
| Migration fails on upgrade | Crashed orchestrator left the advisory lock held | See `docs/db_migration.md` § "Troubleshooting" |

---

## What's NOT in this install (Phase 2)

These are deliberately deferred to a follow-up phase once the prototype
has been evaluated:

- **IdP federation** to the customer's UCS LDAP (or any external OIDC).
  Adds AD-group → SRW-role mapping, SSO for existing employees. ~1–2 weeks
  of chart work for LDAP federation, or ~1 day if the customer's UCS
  Keycloak app is reachable and we point at it as external OIDC.
- **Managed databases** — Postgres / pgvector / MongoDB moved to managed
  instances. Chart already supports it (`internal: false` + external
  connection details). Day's work + cutover.
- **Customer cloud storage** — point at the customer's existing
  Nextcloud / OpenCloud / MS365. Chart supports `cloud.externalBackend`.
  MS365 support is on the SRW roadmap (1–2 months).
- **mailcow SMTP** — wire the customer's existing SMTP into the
  `SMTP_*` Secret keys (§13) and turn on the relevant email features.
- **n8n / osTicket / Teams** — out-of-cluster integrations via the SRW
  REST API; no chart changes needed.
- **GPU node for local LLM** — if the customer wants on-prem inference for
  patient-record / PHI workflows, add a GPU node to the cluster and
  configure the LLM endpoint in Admin → Providers. PHI routing rules
  ("never send to external providers") would be defined per-provider.
- **GDPR / audit hardening** — MongoDB audit trail is already on in this
  install; extending it (retention, export, PHI-aware log scrubbing) is
  the Phase 2 conversation.

---

## Support

- **Issues:** <https://github.com/knaeckebrothero/Superhuman-Remote-Worker/issues> (private; request access)
- **Chart README** (full configurable surface): `helm show readme oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker --version 0.0.19`
