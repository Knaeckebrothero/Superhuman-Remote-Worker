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

### 1.1 Component licensing

The SRW components (`orchestrator`, `agent`, `cockpit`, `mcp`, workspace tooling)
are under the project license — see [`LICENSE.txt`](../LICENSE.txt) — and the
third-party libraries bundled *inside* those images are inventoried in
[`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md).

The bundled **services** (`keycloak`, `gitea`, `opencloud`, `postgres`,
`postgres-vector`, `mongodb`, `neo4j`) are **not redistributed by us**: the chart
pins official upstream images that the customer's cluster pulls directly from
their public registries, so each arrives under its own upstream OSS license (e.g.
Neo4j Community Edition under **GPLv3** if you enable it). We reference those
images; we don't convey them.

> ⚠️ This holds only while the cluster pulls from the public registries. If you
> mirror, re-tag, or ship those images yourself (e.g. an **air-gapped** install —
> outside this prototype's scope, see the note at the top), you become a
> distributor of that component and take on its distribution terms (source offer,
> notices). Plan that with counsel before the first air-gapped delivery.

---

## 2. What the customer provides

### 2.1 A host for the cluster

- **Ubuntu 22.04+ LTS** (24.04 also fine) on a physical machine or VM.
- **Minimum (prototype):** 8 vCPU / 16 GiB RAM / 100 GiB disk. Comfortable
  for 1–3 concurrent agents. Scale up for heavier demos.
- **Network:** static IP reachable from the customer's edge reverse proxy
  on TCP/80, and outbound internet for image pulls (`ghcr.io`).
- **SSH access** for the SRW deployment engineer.

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
individually under `global.hostnames` in the values file (see §6).

### 2.3 Edge reverse proxy — TLS termination

TLS is terminated **at the customer's edge proxy** (in this engagement:
Zoraxy). The k3s ingress runs HTTP-only inside the cluster.

```
browser ─HTTPS─▶ Zoraxy ─HTTP─▶ k3s Traefik ─HTTP─▶ pods
                 (TLS terminated here,
                  ACME cert via lego)
```

Per route, the customer configures on their proxy:

| Setting | Value |
|---|---|
| Public hostname | one of the five above |
| Upstream / backend | `http://<k3s-node-ip>:80` |
| TLS | enable with the customer's preferred ACME setup (Let's Encrypt, ZeroSSL, internal CA) |
| Pass `Host` header | **yes** — required for the cluster Ingress to route by hostname |
| WebSocket / SSE | enabled (Zoraxy enables both automatically) |
| HTTP/2 | enabled |

This is the **only customer-side integration** this install requires.
Cluster-side `cert-manager` is not used — see §3.3 for why.

### 2.4 LLM provider accounts

At least one provider account. Customer-billed, customer-owned. **These are
NOT placed in the Kubernetes Secret** — they're configured post-install via
the cockpit Admin → Providers UI, which stores them encrypted (using
`APP_ENCRYPTION_KEY`) in the database.

Common providers: OpenAI, Anthropic, Groq, Google Gemini, OpenRouter,
Tavily (web search). Need at least one chat model + ideally one embedding
model.

---

## 3. Cluster bootstrap (Ubuntu → k3s → helm)

Run on the Ubuntu host the customer provided. All commands below are run
via SSH on that node — `kubectl` and `helm` execute locally there.

### 3.1 System prep

```bash
# Update + a couple of utilities the chart and install need
sudo apt update && sudo apt -y upgrade
sudo apt -y install curl ca-certificates iptables jq

# Make sure the hostname resolves locally (k3s needs this)
hostnamectl set-hostname srw-host       # whatever the customer calls it
echo "127.0.1.1 $(hostname)" | sudo tee -a /etc/hosts

# Confirm a swap-free setup is fine — k3s handles swap, but high I/O on
# swap will hurt the databases. Recommended: leave swap off for prod-ish
# workloads.
free -h
```

### 3.2 Install k3s

```bash
# One-line install. k3s ships Traefik + local-path StorageClass +
# metrics-server + CoreDNS + klipper-lb as a single binary, so this is
# everything you need for a working single-node cluster.
curl -sfL https://get.k3s.io | sh -

# k3s starts automatically as a systemd service:
sudo systemctl status k3s --no-pager

# Confirm node is Ready
sudo k3s kubectl get nodes
#   → STATUS=Ready

# Confirm built-ins are up
sudo k3s kubectl -n kube-system get pods
#   → traefik, local-path-provisioner, metrics-server, coredns all Running

# Confirm the default StorageClass
sudo k3s kubectl get storageclass
#   → `local-path (default)`
```

### 3.3 Why no cert-manager

Zoraxy at the edge already does ACME (via go-lego — supports Let's Encrypt,
ZeroSSL, Buypass, Google CA, plus DNS-01 for ~100 providers). The cluster
Ingress runs HTTP-only (`ingress.tls.enabled: false` in §6) and pods are
unreachable from outside the proxy. Adding cert-manager would mean **two
cert systems with no integration between them** — Zoraxy can't consume
cluster-issued certs automatically, and the cluster doesn't need certs.
Dead weight on a prototype.

If a later phase needs per-service mTLS (e.g. encrypting the orchestrator's
Postgres connection), we install cert-manager then for that specific use
case.

### 3.4 kubectl + helm setup for the install user

```bash
# Make `kubectl` work without sudo for the install user
sudo install -m 0644 /etc/rancher/k3s/k3s.yaml ~/.kube/config 2>/dev/null \
  || (mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown $(id -u):$(id -g) ~/.kube/config)
export KUBECONFIG=~/.kube/config
echo "export KUBECONFIG=~/.kube/config" >> ~/.bashrc

kubectl get nodes      # should work without sudo now

# Install helm 3.12+
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
helm version           # → v3.12.x or newer
```

### 3.5 Firewall

k3s itself does not need any ports open to the outside world for the
install — `kubectl` and `helm` run locally on the node. The only inbound
traffic the cluster receives in this install is from the customer's edge
proxy.

If `ufw` is active on the host:

```bash
sudo ufw status
# If inactive — nothing to do.
# If active — allow inbound HTTP from the edge proxy host:
sudo ufw allow from <zoraxy-host-ip> to any port 80 proto tcp
sudo ufw reload
```

Ports the customer should **not** expose publicly:

| Port | Purpose | Public exposure |
|---|---|---|
| `6443/tcp` | k3s API server | localhost only (install runs on-node) |
| `80/tcp` | cluster Ingress (Traefik) | only from the edge proxy |
| `443/tcp` | unused on the node — Zoraxy terminates TLS | not needed on the cluster host |
| `8472/udp` | k3s flannel VXLAN | internal — single-node anyway |
| `10250/tcp` | kubelet | internal — single-node anyway |

---

## 4. Pre-flight checklist

Before `helm install`, confirm the bootstrap landed cleanly:

```bash
# K8s version
kubectl version
#   → server ≥ v1.28

# Default storage class
kubectl get storageclass
#   → `local-path (default)`

# Traefik ingress controller (k3s built-in)
kubectl -n kube-system get pods | grep traefik
#   → Running

# DNS records resolve to the edge proxy
for h in srw api.srw auth.srw git.srw cloud.srw; do
  echo -n "$h.example.com → "; dig +short $h.example.com
done
#   → all return the edge proxy IP

# Edge proxy can reach the cluster
# (from the proxy host)
curl -sI http://<k3s-node-ip>/
#   → 404 from Traefik is fine — it means HTTP works, just no Host match yet

# helm is installed
helm version    # → v3.12+
```

---

## 5. Secret creation

The chart references a Kubernetes Secret named `srw-secrets` for cluster
credentials. **LLM provider keys are NOT in this Secret** — those go into
the database via the Admin UI after install (see §10).

### 5.1 Generate the encryption key

```bash
openssl rand -base64 32
#   → save to the customer's password manager IMMEDIATELY.
#     If lost, every stored credential in the DB becomes unrecoverable.
```

### 5.2 Author `srw.env`

```env
# --- Always required ---
APP_ENCRYPTION_KEY=<paste from 5.1>

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

# --- Stubs: keys the chart's pods reference unconditionally ------------------
# The chart's pod templates read these keys via `secretKeyRef` without
# `optional: true` and without an enclosing `{{ if .Values.X.enabled }}`
# guard. The keys must therefore EXIST in the Secret or pods fail to start
# with `couldn't find key X in Secret srw/srw-secrets`. Empty values are
# valid — the underlying features stay off because the corresponding
# `*.enabled` knobs in §6 are off. This is a chart gap; expect these to be
# either gated or marked `optional: true` in a future chart release.

# Email — orchestrator + Keycloak read SMTP_USER/SMTP_PASSWORD unconditionally
SMTP_USER=
SMTP_PASSWORD=
IMAP_USER=
IMAP_PASSWORD=
MAIL_DOMAIN=

# pgadmin OIDC — Keycloak realm bootstrap references it even when pgadmin.enabled=false
PGADMIN_OIDC_CLIENT_SECRET=

# Nextcloud — orchestrator references these keys (we run OpenCloud, not Nextcloud)
NEXTCLOUD_ADMIN_USER=
NEXTCLOUD_ADMIN_PASSWORD=
NEXTCLOUD_AGENT_USER=
NEXTCLOUD_AGENT_PASSWORD=
NEXTCLOUD_OIDC_CLIENT_SECRET=
NEXTCLOUD_S3_ACCESS_KEY=
NEXTCLOUD_S3_SECRET_KEY=

# Object storage — orchestrator references these (S3 backend off in this install)
S3_ACCESS_KEY=
S3_SECRET_KEY=

# WebDAV default datasource — orchestrator references these
DEFAULT_DS_WEBDAV_USERNAME=
DEFAULT_DS_WEBDAV_PASSWORD=

# Notifications — orchestrator references all of these
SLACK_WEBHOOK_URL=
DISCORD_WEBHOOK_URL=
NTFY_URL=
NTFY_TOPIC=
NTFY_TOKEN=

# MCP — orchestrator + Keycloak reference these (mcp.enabled=false in this install)
MCP_OIDC_CLIENT_SECRET=
MCP_INTERNAL_KEY=

# Codex proxy — orchestrator references (codexProxy.enabled=false)
CODEX_MANAGEMENT_KEY=

# Keycloak bootstrap Job — realm-init container reads these
KC_ADMIN_USER=
KC_ADMIN_PASSWORD=
KC_CLIENT_SECRET=
```

> The chart auto-provisions the bundled Keycloak's realm, the Gitea OAuth
> source, the OpenCloud OIDC client, and the agent's account on OpenCloud —
> all using the secrets above. No manual OIDC client setup required.

### 5.3 Apply

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

## 6. Values file

```yaml
# my-values.yaml
license:
  acceptTerms: true

# Pin resource names to `srw-*` (matches the verification commands in §8).
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

## 7. Install

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

## 8. Post-install verification

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

## 9. Edge proxy route configuration

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

## 10. First-user setup + LLM providers

### Create the first user in Keycloak

1. Open `https://auth.srw.example.com/admin/` in a browser.
2. Sign in: `admin` / `KEYCLOAK_ADMIN_PASSWORD` from §5.2.
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

## 11. Upgrade

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

## 12. Rollback

```bash
helm history srw -n srw
helm rollback srw <REVISION> -n srw
```

Database migrations are **not** auto-reverted. The schema is
forward-compatible by design (additive migrations only). For a destructive
upgrade you need a snapshot taken before the upgrade.

---

## 13. Uninstall

```bash
helm uninstall srw -n srw

# PVCs are intentionally retained — keep them, or delete:
kubectl -n srw delete pvc -l app.kubernetes.io/instance=srw

# Drop the namespace:
kubectl delete namespace srw
```

To fully tear down the cluster too (only if abandoning the host):

```bash
sudo /usr/local/bin/k3s-uninstall.sh
```

---

## 14. Secret schema (this install)

The table below uses three values in the **Required** column:

- **value** — key must be present **and non-empty**. Pods fail closed if the
  value is missing.
- **key-only** — key must **exist in the Secret** but the value can be an
  empty string. The chart's pod templates read these via `secretKeyRef`
  without `optional: true`, so kubelet rejects pod startup if the key is
  absent. The feature itself stays off as long as the matching `*.enabled`
  knob in §6 is off. (Chart gap; tracked for a future release.)
- **omittable** — chart marks the `secretKeyRef` `optional: true`. Safe to
  omit the key entirely.

| Key | Required | Notes |
|---|---|---|
| `APP_ENCRYPTION_KEY` | value | base64 32 bytes. Loss = unrecoverable encrypted credentials |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | value | bundled Postgres |
| `VECTOR_POSTGRES_USER` / `VECTOR_POSTGRES_PASSWORD` | value | bundled pgvector |
| `KEYCLOAK_ADMIN_USER` / `KEYCLOAK_ADMIN_PASSWORD` | value | bundled Keycloak admin login |
| `KC_DB_PASSWORD` | value | Keycloak's dedicated Postgres |
| `KC_REALM_ADMIN_PASSWORD` | value | realm-level admin (for kcadm bootstrap) |
| `GITEA_ADMIN_USER` / `GITEA_ADMIN_PASSWORD` | value | bundled Gitea admin login |
| `GITEA_OIDC_CLIENT_SECRET` | value | Gitea ↔ Keycloak OAuth |
| `OPENCLOUD_KEYCLOAK_CLIENT_SECRET` | value | OpenCloud ↔ Keycloak OAuth |
| `CLOUD_SERVICE_USER` / `CLOUD_SERVICE_PASSWORD` | value | agent's account on OpenCloud |
| `SMTP_USER` / `SMTP_PASSWORD` | key-only | orchestrator + Keycloak read these regardless of email config. Set values to wire SMTP. |
| `IMAP_USER` / `IMAP_PASSWORD` | key-only | orchestrator reads these regardless of inbound-email config |
| `MAIL_DOMAIN` | key-only | orchestrator reads it regardless of email config |
| `PGADMIN_OIDC_CLIENT_SECRET` | key-only | Keycloak realm bootstrap reads it even when `pgadmin.enabled=false` |
| `NEXTCLOUD_ADMIN_USER` / `NEXTCLOUD_ADMIN_PASSWORD` / `NEXTCLOUD_AGENT_USER` / `NEXTCLOUD_AGENT_PASSWORD` / `NEXTCLOUD_OIDC_CLIENT_SECRET` / `NEXTCLOUD_S3_ACCESS_KEY` / `NEXTCLOUD_S3_SECRET_KEY` | key-only | orchestrator references these; we run OpenCloud not Nextcloud |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | key-only | orchestrator references these; S3 backend off in this install |
| `DEFAULT_DS_WEBDAV_USERNAME` / `DEFAULT_DS_WEBDAV_PASSWORD` | key-only | orchestrator references; off by default |
| `SLACK_WEBHOOK_URL` / `DISCORD_WEBHOOK_URL` / `NTFY_URL` / `NTFY_TOPIC` / `NTFY_TOKEN` | key-only | orchestrator references all of them; populate to enable the corresponding channel |
| `MCP_OIDC_CLIENT_SECRET` / `MCP_INTERNAL_KEY` | key-only | required keys for MCP wiring even when `mcp.enabled=false`. Set values when enabling MCP. |
| `CODEX_MANAGEMENT_KEY` | key-only | orchestrator references it even when `codexProxy.enabled=false` |
| `KC_ADMIN_USER` / `KC_ADMIN_PASSWORD` / `KC_CLIENT_SECRET` | key-only | Keycloak bootstrap Job reads these |
| `NEO4J_USERNAME` / `NEO4J_PASSWORD` | value (only if `neo4j.enabled=true`) | no `/` in the password — `NEO4J_AUTH` is split on the first slash |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_FROM` / `SMTP_USE_TLS` / `SMTP_TRUST_SELF_SIGNED` | omittable | sourced from values.yaml `email.smtp.*` rather than the Secret |

**Not in the Secret** (configured post-install via Admin UI, stored
encrypted in DB): `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`,
`GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`, embedding model
endpoints.

---

## Appendix — Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `orchestrator` `CrashLoopBackOff`, logs say `APP_ENCRYPTION_KEY` missing/invalid | Secret not created or key absent | Re-create per §5 |
| Cockpit loads but login bounces / "Invalid redirect_uri" | Edge proxy not passing the `Host` header, or DNS for `auth.<domain>` not pointing at the edge proxy | Verify §2.2 and §9 |
| `502 Bad Gateway` from edge proxy on every host | k3s ingress IP changed or NodePort moved | Re-check `kubectl -n kube-system get svc traefik`, update upstream in proxy |
| WebSocket fails (chat shows "disconnected") | Edge proxy not forwarding `Upgrade: websocket` | Confirm WebSocket toggle is on for the host (Zoraxy enables by default) |
| Cockpit shows "no LLM providers configured" | Expected on first login | Configure in §10 Step 3 |
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
  `SMTP_*` Secret keys (§14) and turn on the relevant email features.
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
