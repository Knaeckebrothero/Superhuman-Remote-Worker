# Superhuman Remote Worker — Helm Chart

A self-improving AI agent system: Orchestrator (FastAPI) coordinates jobs,
Agents (LangGraph) execute them in isolated workspaces, Cockpit (Angular)
provides the web UI. Backed by PostgreSQL, pgvector, MongoDB, and (optionally)
Neo4j.

This chart deploys the full stack to a Kubernetes cluster. Internal databases
and supporting services can each be replaced with externally managed
equivalents (managed Postgres, an external OIDC provider, an existing git
server, etc.) — see [Production install](#production-install-bring-your-own).

- **Chart:** `oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker`
- **Source:** <https://github.com/knaeckebrothero/Superhuman-Remote-Worker>
- **License:** see [LICENSE](https://github.com/knaeckebrothero/Superhuman-Remote-Worker/blob/main/LICENSE) — you must accept the terms to install (`license.acceptTerms: true`).

---

## What gets deployed

| Component | Purpose | Toggle |
|---|---|---|
| `orchestrator` | FastAPI control plane (REST API, job dispatch, MCP) | always on |
| `agent` | LangGraph job executor (worker + persistent session pools) | always on |
| `cockpit` | Angular web UI | always on |
| `mcp` | MCP server for Claude Code / AI platform integration | `mcp.enabled` |
| `workspace` | Per-job isolated PVC + SSH workspace pods | always on |
| `databases.postgres` | Application database (jobs, users, projects) | internal or external |
| `databases.vector` | pgvector for embeddings, citations, memories | internal or external |
| `databases.keycloak` | Dedicated Postgres for the bundled Keycloak (only relevant when `keycloak.internal: true`) | internal or external |
| `databases.mongodb` | Audit trail (optional but recommended) | internal or external |
| `databases.neo4j` | Project knowledge graph (optional). `edition: community` (default) or `enterprise` (set `acceptLicense` to `"yes"` for Startup Program / commercial, or `"eval"` for non-production). | internal or external |
| `keycloak` | OIDC provider | internal or external |
| `gitea` | Git server for agent code workspaces | internal or external |
| `opencloud` / `nextcloud` | Cloud storage backend | one or external |
| `pgadmin`, `mongoExpress`, `dozzle` | Admin UIs (off by default) | `*.enabled` |
| `reloader` | Watches Secret/ConfigMap changes, triggers rolling restarts | `reloader.enabled` |
| `vmController` | KubeVirt VM lifecycle controller (HTTP or NATS transport) | `vmController.enabled` |

---

## Prerequisites

- **Kubernetes** 1.28+ with at least 8 vCPU / 16 GiB RAM available for the namespace
- **Ingress controller** — `nginx`, `traefik`, or another. The chart defaults to `traefik` (override via `ingress.className`)
- **cert-manager** with a `ClusterIssuer` for TLS (set `ingress.tls.issuerName`)
- **DNS** — wildcard or per-subdomain records pointing at your ingress LB for `*.<global.domain>` (`api`, `auth`, `git`, `cloud`, `mcp`)
- **Helm** 3.12+
- **A `dockerconfigjson` pull secret** if you're pulling private GHCR images. Public images need no credentials.

Optional but recommended:
- **External Secrets Operator** + a backing store (Vault, AWS Secrets Manager, etc.) — see `externalSecrets.*`
- **An OIDC IdP** if you don't want the bundled Keycloak (Azure AD, Google Workspace, Okta, etc.)
- **Managed databases** for production (any standard Postgres 14+ for app + pgvector, MongoDB 6+, Neo4j 5+)

---

## Quick start (evaluation, single command)

For a self-contained evaluation install with everything in-cluster (Postgres,
MongoDB, Neo4j, Keycloak, Gitea, OpenCloud all bundled):

```bash
helm install srw oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker \
  --version 0.0.1 \
  --namespace srw --create-namespace \
  --set license.acceptTerms=true \
  --set global.domain=srw.example.com \
  --set ingress.tls.issuerName=letsencrypt-prod \
  --set secrets.create=true
```

`secrets.create=true` makes the chart auto-generate an `APP_ENCRYPTION_KEY`
and create internal credentials. **This mode is for evaluation only** — see
[Secrets](#secrets) for production options.

After install, follow the printed `NOTES.txt` to back up the encryption key.

---

## Production install (bring your own)

For pilot and customer deployments, you bring your own external services and
manage your own secrets. Start by extracting the example values:

```bash
helm pull oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker \
  --version 0.0.1 --untar
cp superhuman-remote-worker/values.example.yaml my-values.yaml
$EDITOR my-values.yaml
```

Edit at minimum:
- `license.acceptTerms` → `true`
- `global.domain` → your base hostname
- `secrets.existingSecret` → name of a Secret you create yourself (see below)
- `databases.*.externalUrl` → connection strings for managed Postgres, vector, MongoDB
- `keycloak.externalIssuerUrl` → your IdP issuer URL
- `gitea.internal: false` + `*.externalUrl` → your git server URLs
- `cloud.externalBackend` + `cloud.externalUrl` → your cloud storage endpoint
- `ingress.className` and `ingress.tls.issuerName` → your cluster's ingress + cert-manager issuer

### Per-component hostname overrides

By default every subservice ingress is `<subdomain>.<global.domain>` (`api`,
`auth`, `git`, `cloud`, `mcp`, `neo4j`, …). When one of those flat names is
already in use in the parent zone — e.g. you already run a Gitea at
`git.example.com` and want SRW's bundled Gitea on a separate host — override
just that hostname:

```yaml
global:
  domain: srw.example.com
  hostnames:
    git: git-srw.example.com   # takes precedence over the default git.srw.example.com
```

The override propagates to the Ingress host + TLS SAN, the `GITEA_URL`
ConfigMap entry, the cockpit deep-link, and the Keycloak gitea-client
redirect URI. TLS Secret names stay tied to the release name, so two
SRW installs in the same cluster never collide on cert storage. See
`global.hostnames` in `values.yaml` for the full key list (cockpit, api,
auth, git, cloud, mcp, neo4j, neo4jBolt, pgadmin, mongo, dozzle, headscale,
minio).

Pre-create the secret with all required keys (see [Secret schema](#secret-schema)):

```bash
kubectl create namespace srw
kubectl -n srw create secret generic srw-secrets \
  --from-env-file=./srw.env
```

Install:

```bash
helm install srw oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker \
  --version 0.0.1 \
  --namespace srw \
  -f my-values.yaml
```

---

## Secrets

The chart supports three mutually exclusive modes. Pick one.

### Mode 1 — External Secrets Operator (recommended for production)

Reads secrets from Vault (or another ESO-supported backend) and projects them
into a K8s Secret. Survives chart upgrades, rotation, and cluster rebuilds.

```yaml
externalSecrets:
  enabled: true
  refreshInterval: "1h"
  secretStoreRef: "vault-backend"
  secretStoreKind: "ClusterSecretStore"
  vaultPath: "secret/data/srw/srw-secrets"
  vmSshKeyVaultPath: "secret/data/srw/vm-ssh-key"  # optional, only if using VM workspace backend
```

The Vault payload at `vaultPath` must contain every key listed in
[Secret schema](#secret-schema) (lowercased — ESO uppercases them on
projection). Missing `app_encryption_key` will block orchestrator startup.

### Mode 2 — Pre-existing K8s Secret (typical for customer-managed installs)

You create the Secret out of band (Sealed Secrets, kubectl, your CD pipeline,
SOPS, whatever). The chart just references it.

```yaml
secrets:
  existingSecret: srw-secrets
```

### Mode 3 — Chart-created Secret (evaluation / dev only)

The chart generates `APP_ENCRYPTION_KEY` if absent, preserves it across
upgrades via `lookup`, and inlines any keys you provide in `secrets.values`.
**Do not use in production** — values end up in `helm get values` output.

```yaml
secrets:
  create: true
  values:
    OPENAI_API_KEY: "sk-..."
    POSTGRES_PASSWORD: "..."
```

### Secret schema

These keys are referenced by chart templates. Not all are required for every
deployment — what you need depends on which optional components you enable.

**Always required:**
- `APP_ENCRYPTION_KEY` — base64-encoded 32-byte key. Encrypts user/system API
  keys and LLM endpoint credentials at rest. **If lost, all stored credentials
  become unrecoverable.** Back up immediately after install.

**Database credentials** — discrete user + password keys only. The chart
composes the DSN at runtime from these + ConfigMap-provided host/port/db,
so `/`, `@`, `=`, and `+` in passwords are URL-quoted automatically. Don't
ship a bundled `DATABASE_URL` / `VECTOR_DB_URL` Vault
key — both layouts coexist (the app falls back to the URL if user+password
aren't set), but the URL form is legacy and a footgun under `urlsplit`.
- `POSTGRES_USER`, `POSTGRES_PASSWORD`
- `VECTOR_POSTGRES_USER`, `VECTOR_POSTGRES_PASSWORD` — pgvector has its
  own superuser password (separate from the main Postgres) so a credential
  leak on one instance doesn't compromise the other. Citations, embeddings,
  and memories all live in this instance (`srw_vector`); the citation engine
  is a native SRW subsystem on the vector pool, **not** a separate role or
  database (the former `srw_citations` / `citation_engine` DB was retired in
  the citation-engine native integration — see
  `docs/features/citation_engine_integration.md`).
- `NEO4J_USERNAME`, `NEO4J_PASSWORD` — both live in Vault (mirroring
  the `POSTGRES_USER` / `VECTOR_POSTGRES_USER`
  pattern, so all DB credentials sit in one place). Community edition
  expects `NEO4J_USERNAME=neo4j`; enterprise can use a different value.
  The Neo4j server image's `NEO4J_AUTH` is composed at pod-start from
  these two; don't ship `NEO4J_AUTH` as a separate Vault key — it's
  dead. **Don't include `/` in the password** — the Neo4j image splits
  `NEO4J_AUTH` on the first `/`, so a slash mis-parses server-side and
  the Bolt port comes up unauthenticated. URL-safe base64 (or any
  alphabet without `/`) avoids it.

For external-mode databases (`internal: false`), the host/port/db come
from `databases.<which>.externalHost/externalPort/externalDb` in values;
only the credentials live in Vault.

**OIDC / SSO** (when Keycloak or external IdP enabled):
- `KEYCLOAK_ADMIN_USER`, `KEYCLOAK_ADMIN_PASSWORD` (internal Keycloak only)
- `KC_DB_PASSWORD` (internal Keycloak only) — used as the Postgres superuser password on the dedicated `srw-keycloakdb` StatefulSet *and* as the connection password the Keycloak pod presents. When pointing the bundled Keycloak at an external Postgres (`databases.keycloak.internal: false`), the same value is sent over the wire — pre-provision a `keycloak` role with this password on your managed instance.
- `KC_REALM_ADMIN_PASSWORD`
- `MCP_OIDC_CLIENT_SECRET` (if `mcp.enabled`)
- `GITEA_OIDC_CLIENT_SECRET`, `NEXTCLOUD_OIDC_CLIENT_SECRET`, `OPENCLOUD_KEYCLOAK_CLIENT_SECRET`, `PGADMIN_OIDC_CLIENT_SECRET` (per enabled component)

**Git, cloud, admin credentials:**
- `GITEA_ADMIN_USER`, `GITEA_ADMIN_PASSWORD` (internal Gitea only)
- `NEXTCLOUD_ADMIN_USER`, `NEXTCLOUD_ADMIN_PASSWORD`, `NEXTCLOUD_AGENT_USER`, `NEXTCLOUD_AGENT_PASSWORD` (internal Nextcloud only)
- `CLOUD_SERVICE_USER`, `CLOUD_SERVICE_PASSWORD` (the agent's account on whichever cloud backend is active)

**LLM provider keys** (any combination, depending on which providers you use):
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `UNPAYWALL_EMAIL`

**Optional integrations:**
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` (when `email.enabled`)
- `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD` (when email polling enabled)
- `NTFY_URL`, `NTFY_TOPIC`, `NTFY_TOKEN` (push notifications)
- `DISCORD_WEBHOOK_URL`, `SLACK_WEBHOOK_URL` (chat notifications)
- `TAILSCALE_AUTH_KEY` (when `agent.tailscale.enabled`)
- `CODEX_MANAGEMENT_KEY` (when `codexProxy.enabled`)
- `MCP_INTERNAL_KEY` (when `mcp.enabled`)
- `DEFAULT_DS_WEBDAV_*` (auto-configure a default WebDAV datasource for new users)

A skeleton `srw.env` to feed into `kubectl create secret generic ... --from-env-file=`:

```env
APP_ENCRYPTION_KEY=<base64-encoded 32-byte key>
POSTGRES_USER=srw
POSTGRES_PASSWORD=changeme
VECTOR_POSTGRES_USER=srw
VECTOR_POSTGRES_PASSWORD=changeme
KC_REALM_ADMIN_PASSWORD=changeme
OPENAI_API_KEY=sk-...
CLOUD_SERVICE_USER=agent
CLOUD_SERVICE_PASSWORD=changeme
```

Generate `APP_ENCRYPTION_KEY` with: `openssl rand -base64 32`

---

## Same-cluster VMs (optional)

By default the chart does not deploy any VM infrastructure — VMs live on a
separate `vm` cluster managed by the Fleet bundles in `deployment-vms/`,
and the orchestrator reaches them via NATS. To run KubeVirt VMs in the same
cluster as the orchestrator, enable the bundled VM controller:

```yaml
vmController:
  enabled: true
  transport: http       # http | nats | both
  namespace: agent-vms
  vmStorageClass: longhorn
  vmDiskSize: 30Gi
  vmSshPublicKey: "ssh-ed25519 AAAA... agent@srw"
```

Prerequisites the chart does **not** install (the cluster operator must
provide these before enabling the toggle):

- **KubeVirt** operator + `KubeVirt` CR (cluster-scoped)
- **CDI** operator (the bundled VM template uses `DataVolume`)
- **Nodes with hardware virtualization** (`vmx`/`svm`) and the relevant
  KubeVirt feature gates enabled

The `transport` choice trades off features:

- `http` (default for same-cluster) — orchestrator → controller over a
  ClusterIP Service. Carries lifecycle only (create / delete / query).
  Does **not** support in-VM daemon events (register, heartbeat, freeze,
  resume) because those use NATS subjects. Use this when you only need
  workspace VMs and your jobs don't pause/resume.
- `nats` — same path as the cross-cluster bundle, but with the controller
  co-located. Requires `nats.url` set to a reachable NATS server. Full
  feature set.
- `both` — controller listens on both transports. Useful while migrating
  or when only some clients have been moved to HTTP.

When `vmController.enabled=false`, none of these resources render and the
orchestrator's behavior is identical to today (NATS / direct K8s / docker
selection).

### Network isolation

Same-cluster VMs are covered by the **same NetworkPolicy** as workspace
containers (`workspace.networkPolicy.enabled`, default `true`). KubeVirt
propagates labels from `VMI.spec.template.metadata.labels` to the
virt-launcher pod, so a single `podSelector` on
`srw.io/component: agent-workspace` matches both pod and VM workspaces.
Disabling the flag removes the policy for both — there is no separate
VM-only toggle.

The unified policy enforces:

- **Ingress**: SSH/CDP only from the agent, IDE only from the orchestrator
  / Traefik.
- **Egress**: DNS, TCP 22/80/443, Tailscale (UDP/41641 + UDP/3478), in-cluster
  database services. No general internet beyond HTTP/S + SSH (SSH is needed
  for git clone of SSH-auth repository datasources).
- **VM↔VM and container↔container lateral isolation**: the policy does not
  list the workspace label as an allowed source.

**CNI requirement.** NetworkPolicy on KubeVirt VMs is only enforced by CNIs
that implement the standard policy resource on virt-launcher pods:

| CNI                | NetworkPolicy enforced? |
|--------------------|-------------------------|
| Calico             | yes                     |
| Cilium             | yes                     |
| OVN-Kubernetes     | yes                     |
| Antrea             | yes                     |
| **Flannel**        | **no** (any policy)     |
| Kube-OVN           | partial (KubeVirt bugs) |

Operators on Flannel without a policy add-on see the resource applied with
no effect — the YAML is accepted but does nothing. Verify your CNI before
relying on the isolation guarantee.

A note on Tailscale: VMs run their own `tailscaled` inside the guest, and
KubeVirt's masquerade networking NATs the VM's NIC through virt-launcher's
veth. Pod-level NetworkPolicy therefore only sees the encrypted WireGuard
envelope (UDP/41641 + DERP/443), not the in-VM traffic. Egress restriction
on virt-launcher pods is meaningful for boot-time + tunnel handshake
traffic and meaningless for everything inside the tunnel — Headscale ACLs
are the right layer for tailnet-source filtering. See
`docs/features/workspace_network_policy_unification.md` for details.

## Post-install verification

```bash
# Wait for pods to be ready
kubectl -n srw rollout status deploy/srw-orchestrator
kubectl -n srw rollout status deploy/srw-cockpit

# Tail orchestrator logs
kubectl -n srw logs -l app.kubernetes.io/component=orchestrator -f

# Back up the encryption key — losing it is unrecoverable
kubectl -n srw get secret srw-secrets \
  -o jsonpath='{.data.APP_ENCRYPTION_KEY}' | base64 -d
```

Then visit `https://<global.domain>` for the Cockpit UI. The default
realm administrator credentials (when using internal Keycloak) are
`admin` / value of `KC_REALM_ADMIN_PASSWORD`.

---

## Upgrade

```bash
helm upgrade srw oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker \
  --version <new-version> \
  -n srw -f my-values.yaml
```

The chart preserves `APP_ENCRYPTION_KEY` across upgrades when `secrets.create=true`
via `lookup`. ESO mode reads from Vault on each refresh interval.

---

## Uninstall

```bash
helm uninstall srw -n srw

# PVCs are NOT deleted automatically — keep them, or remove explicitly:
kubectl -n srw delete pvc -l app.kubernetes.io/instance=srw
kubectl delete namespace srw
```

---

## Configuration reference

The full configurable surface is documented inline in `values.yaml`
(every option has a `# --` comment):

```bash
helm show values oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker \
  --version 0.0.1
```

A reference customer overlay is shipped as `values.example.yaml` inside the
chart tarball.

---

## Support

- **Issues:** <https://github.com/knaeckebrothero/Superhuman-Remote-Worker/issues>
- **License:** [LICENSE](https://github.com/knaeckebrothero/Superhuman-Remote-Worker/blob/main/LICENSE)
