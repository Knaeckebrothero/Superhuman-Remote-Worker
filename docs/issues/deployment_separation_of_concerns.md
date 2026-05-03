# Deployment Separation of Concerns — 2026-05-02

Audit of the Helm chart (`helm/`) and the Compose stack (`docker-compose.yaml`)
for shared databases, shared credentials, missing auth, and missing network
isolation between services.

The findings below are ordered by payoff. Issue A is the headline (what the
audit was triggered by); B–E are smaller but cheap to fix at the same time.

---

## Current data plane (helm chart, default values)

| Service              | Backend  | Instance         | Auth                  | Notes                                     |
|----------------------|----------|------------------|-----------------------|-------------------------------------------|
| Orchestrator (app)   | Postgres | `srw-postgres`   | user `srw`, db `srw`  | Shares instance with Keycloak             |
| Keycloak             | Postgres | `srw-postgres`   | user `keycloak`, db `keycloak` | **Co-tenant on orchestrator's Postgres** |
| Vector store         | pgvector | `srw-pgvector`   | user `srw`, db `srw_vector`    | Dedicated instance                        |
| Audit trail          | MongoDB  | `srw-mongodb`    | **none**              | Dedicated instance, unauthenticated       |
| Knowledge graph      | Neo4j    | `srw-neo4j`      | `NEO4J_AUTH` secret   | Dedicated, but defined as a `Deployment`  |
| Gitea                | SQLite   | `srw-gitea` PVC  | n/a                   | Self-contained                            |
| Nextcloud            | SQLite   | `srw-nextcloud`  | n/a                   | Self-contained                            |
| OpenCloud            | internal | `srw-opencloud`  | n/a                   | Self-contained (uses internal stores + S3) |
| MCP / Cockpit / etc. | stateless| —                | —                     | No persistence                            |

---

## Issue A: Keycloak co-tenants on the orchestrator's Postgres — RESOLVED 2026-05-02

**Status**: Fixed on `develop`. Keycloak now runs against a dedicated
`srw-keycloakdb` StatefulSet (helm) / `postgres-keycloak` service (compose).
The init ConfigMap, `KC_DB_PASSWORD` env on the main postgres, and the
`postStart` lifecycle hook were all removed; `init_sso_dbs.sh` and the
`ensure_sso_databases()` / `reset_sso_databases()` helpers in
`orchestrator/init.py` were deleted. New chart toggle
`databases.keycloak.internal` + `externalUrl` supports pointing the bundled
Keycloak at a managed Postgres. Verified with `helm lint` and three template
renders (internal / external-IdP / external-Postgres). Existing test
deployment to be wiped and re-provisioned from scratch — no migration.

**Severity**: High

**Symptom**: A single Postgres StatefulSet (`srw-postgres`) hosts both the
orchestrator's `srw` database and Keycloak's `keycloak` database. Separation
is logical only — same pod, same PVC, same backup window, same blast radius
on a CVE.

**Evidence**:
- `helm/templates/services/keycloak.yaml:585-588`:
  ```yaml
  - name: KC_DB_URL
    value: "jdbc:postgresql://{{ include "srw.fullname" . }}-postgres:5432/keycloak"
  - name: KC_DB_USERNAME
    value: "keycloak"
  ```
- `helm/templates/databases/postgres.yaml:91-95` injects `KC_DB_PASSWORD` into
  the postgres pod so its `postStart` hook can `CREATE ROLE keycloak`. The
  orchestrator's database pod ends up needing to know Keycloak's secret —
  the wrong direction of trust.
- `orchestrator/database/init_sso_dbs.sh` — script bolted into postgres
  initdb to provision SSO databases.

**Why it's wrong**:
- Compromise of the Postgres pod exposes both the orchestrator's and the
  IdP's data simultaneously.
- Restoring orchestrator state from backup also restores Keycloak state
  (and vice versa) — they cannot be backed up or rolled back independently.
- Keycloak's resource ceiling fights with orchestrator workload spikes.
- The `keycloak` role-creation hook runs on every postgres start; this is
  awkward operational coupling.

**Fix**: Split Keycloak onto its own Postgres StatefulSet
(`srw-keycloak-postgres`).
- New StatefulSet + PVC + Service in `helm/templates/keycloak/postgres.yaml`
  (or under `databases/`, named for keycloak).
- Drop the `init_sso_dbs.sh` ConfigMap, the `KC_DB_PASSWORD` env on the main
  postgres, and the `postStart` lifecycle hook from `databases/postgres.yaml`.
- Point `KC_DB_URL` at the new instance.
- Migration: existing data lives in `srw-postgres.keycloak`. Pre-cutover,
  `pg_dump --dbname=keycloak` from old → `pg_restore` into new, then flip
  `KC_DB_URL`. Document this in `helm/README.md`.
- Mirror the change in `docker-compose.yaml` (add a second postgres service
  for Keycloak) so dev parity holds.

---

## Issue B: pgvector and main Postgres share `POSTGRES_PASSWORD` — RESOLVED 2026-05-02

**Status**: Fixed on `develop`. `helm/templates/databases/postgres-vector.yaml`
now reads `POSTGRES_PASSWORD` from secret key `VECTOR_POSTGRES_PASSWORD`
(separate from the main Postgres' `POSTGRES_PASSWORD` key). README's "Secret
schema" section and the skeleton `srw.env` were updated. Compose files
already used `${VECTOR_POSTGRES_PASSWORD:-srw_password}` and `.env.example`
already declared the variable, so no compose-side changes were needed. ESO
picks up the new key automatically via `dataFrom: extract`. Verified with
`helm lint` and a full render showing three independent password keys
(`POSTGRES_PASSWORD` → main, `VECTOR_POSTGRES_PASSWORD` → pgvector,
`KC_DB_PASSWORD` → keycloakdb). Operator action on the next deploy:
populate `VECTOR_POSTGRES_PASSWORD` in Vault (or the chart-managed Secret)
distinct from `POSTGRES_PASSWORD` to realize the separation in practice.

**Severity**: Medium

**Symptom**: Both Postgres instances pull the same secret key for their
superuser password. Different usernames (`srw` vs the value of
`VECTOR_POSTGRES_USER`), same password value.

**Evidence**:
- `helm/templates/databases/postgres.yaml:81-85` — main postgres reads
  `POSTGRES_PASSWORD` from key `POSTGRES_PASSWORD`.
- `helm/templates/databases/postgres-vector.yaml:58-62` — pgvector reads
  `POSTGRES_PASSWORD` from the same key `POSTGRES_PASSWORD`.
- Compose already supports separation via `VECTOR_POSTGRES_PASSWORD`
  (`docker-compose.yaml:57`); helm flattened it.

**Why it's wrong**: Compromise of one instance's credentials hands the
attacker the other.

**Fix**:
- Add `VECTOR_POSTGRES_PASSWORD` to the secret schema (chart README +
  `external-secret.yaml` + `secret.yaml`).
- Update `postgres-vector.yaml` to read `POSTGRES_PASSWORD` from key
  `VECTOR_POSTGRES_PASSWORD`.
- Update `srw.vectorDbUrl` helper (or wherever `VECTOR_DB_URL` is built) to
  use the new key.
- Document the new secret key in `values.example.yaml` and the README's
  "Secret schema" section.

---

## Issue C: MongoDB runs with authentication disabled

**Severity**: High (for production), Medium (for homelab)

**Symptom**: The audit-trail MongoDB accepts unauthenticated connections.
Any pod in the namespace that can resolve `srw-mongodb` can read and write
audit logs, chat history, and LLM request records.

**Evidence**:
- `helm/templates/databases/mongodb.yaml:44-46`:
  ```yaml
  env:
    - name: MONGO_INITDB_DATABASE
      value: "srw_logs"
  ```
  No `MONGO_INITDB_ROOT_USERNAME` / `MONGO_INITDB_ROOT_PASSWORD`. No `--auth`
  flag on the container command.
- `docker-compose.yaml:74-83` has the same gap.

**Why it's wrong**: This is the system's audit trail. Tampering or unauthorized
reads here defeat the purpose of having audit logs.

**Fix**:
- Add `MONGODB_USERNAME` / `MONGODB_PASSWORD` to the secret schema.
- Pass them as `MONGO_INITDB_ROOT_USERNAME` / `MONGO_INITDB_ROOT_PASSWORD`
  on first init.
- Add `command: ["mongod", "--auth", "--bind_ip_all"]` to enforce auth on
  every start (initdb env only takes effect on first boot).
- Update the `srw.mongodbUrl` helper to embed credentials in the URL
  (`mongodb://user:pass@srw-mongodb:27017/srw_logs?authSource=admin`).
- Move `MONGODB_URL` from the ConfigMap to the Secret (it now contains
  credentials).
- Update healthcheck to use the credentials, or keep `db.adminCommand('ping')`
  if it works pre-auth (verify).

---

## Issue D: Neo4j is a `Deployment`, not a `StatefulSet` — RESOLVED 2026-05-02

**Status**: Fixed on `develop`. `helm/templates/databases/neo4j.yaml` now
declares `kind: StatefulSet` with `serviceName: srw-neo4j`; the `strategy:
Recreate` block was removed (StatefulSet has its own update strategy
defaulting to `RollingUpdate`, which is fine at `replicas: 1`). Kept the
existing standalone `srw-neo4j-data` PVC reference rather than switching to
`volumeClaimTemplates` so the existing volume binds across the conversion
with no data movement — matches the pgvector layout. Neo4j Community
Edition is single-instance only; that's exactly why StatefulSet is the
right primitive here — it makes the "two pods racing for the same RWO PVC
on a replica bump" footgun unreachable (StatefulSet creates a PVC per
ordinal, so a bump just fails to bind instead of corrupting the database).
Verified with `helm lint` (passes) and `helm template` (renders
`kind: StatefulSet` for `srw-superhuman-remote-worker-neo4j`). Operator
action on the next deploy: existing Deployment is replaced; the bound PVC
is preserved so the graph state survives.

Bundled in the same pass: a `databases.neo4j.edition` toggle
(`community` default, `enterprise` opt-in) plus `acceptLicense` (`"yes"`
for Startup Program / commercial, `"eval"` for non-production). The
template hard-fails at render time if `edition: enterprise` is set
without `acceptLicense`. Image auto-selects between `neo4j:5-community`
and `neo4j:5-enterprise`; the existing `image` field still pins a
specific tag when set. README "Components" table and
`values.example.yaml` updated. Verified by rendering all three paths
(community default, enterprise+missing-license fails, enterprise+eval
injects `NEO4J_ACCEPT_LICENSE_AGREEMENT`).

**Severity**: Low (consistency / latent foot-gun)

**Symptom**: The other three databases are StatefulSets. Neo4j is a
Deployment with `strategy: Recreate`.

**Evidence**:
- `helm/templates/databases/neo4j.yaml:47-56`:
  ```yaml
  apiVersion: apps/v1
  kind: Deployment
  ...
  spec:
    replicas: 1
    strategy:
      type: Recreate
  ```

**Why it's wrong**: Works at `replicas: 1`, but if anyone bumps replicas the
Deployment will schedule two pods racing for the same `ReadWriteOnce` PVC.
StatefulSet is the correct primitive for stateful workloads and matches the
rest of the data tier.

**Fix**: Convert to `StatefulSet` with `serviceName: srw-neo4j` and
`volumeClaimTemplates` (or keep the existing standalone PVC and reference
it like the current Deployment does — same shape as `postgres.yaml`).
Cosmetic but worth bundling with the others.

---

## Issue E: No NetworkPolicies on the data tier — RESOLVED 2026-05-02

**Status**: Fixed on `develop`. New
`helm/templates/databases/network-policies.yaml` adds one ingress
NetworkPolicy per internal database StatefulSet, each gated on the same
`enabled && internal` condition as the database itself:
- `srw-postgres` ← orchestrator, agent, llm-seed Job, pgadmin (when
  `pgadmin.enabled`)
- `srw-pgvector` ← orchestrator, agent
- `srw-mongodb` ← orchestrator, agent, mongo-express (when
  `mongoExpress.enabled`)
- `srw-neo4j` ← orchestrator, agent (HTTP 7474 + Bolt 7688)
- `srw-keycloakdb` ← keycloak only (renders only when
  `keycloak.internal && databases.keycloak.internal`)

Selectors use the chart's existing `srw.componentSelectorLabels` helper, so
they stay in sync if components are ever renamed. Only `policyTypes:
[Ingress]` is set — egress is left unrestricted (DBs don't initiate
meaningful outbound traffic; locking it down would just complicate future
backup/replication work). Kubelet probes bypass NetworkPolicy entirely, so
no allowance was needed for liveness/readiness. Verified by rendering
default, `pgadmin/mongoExpress on`, and `keycloak.internal=false` paths —
admin-UI selectors appear when toggled, keycloakdb policy disappears with
external IdP. `helm lint` clean. Operator action on the next deploy: none
beyond confirming the cluster has a CNI that enforces NetworkPolicy
(Cilium, Calico, etc. — most do; Flannel does not).

**Severity**: Medium

**Symptom**: Postgres, pgvector, MongoDB, and Neo4j all accept connections
from any pod in the `superhuman-remote-worker` namespace. There is a
`workspace-network-policy.yaml` for workspace pods, but nothing equivalent
for the databases.

**Evidence**:
- `helm/templates/workspace-network-policy.yaml` exists.
- `helm/templates/databases/` contains zero NetworkPolicy resources.
- A workspace pod that gets compromised (the most exposed surface in the
  system — agents run untrusted code there) can reach every database
  service directly.

**Why it's wrong**: Workspace pods only need to talk to the orchestrator
and (for some flows) Gitea/cloud storage. They have no business reaching
Postgres or Mongo directly. Lack of an explicit allow-list means any new
pod added to the namespace inherits full database access.

**Fix**: One NetworkPolicy per database, ingress restricted to the pods
that legitimately need it:
- Postgres → orchestrator (and, after Issue A, Keycloak). Migration job is
  short-lived; allow by label.
- pgvector → orchestrator only.
- MongoDB → orchestrator, mongo-express (when enabled).
- Neo4j → orchestrator, agent (read-only data sources).
- Keycloak's new Postgres → keycloak only.

Use `podSelector` matching `componentLabels` from `_helpers.tpl`.

---

## Out of scope for this issue

- Postgres HA / replication — current scale doesn't justify it; revisit when
  it does.
- Moving secrets to per-component Vault paths — `srw-secrets` is currently
  a single shared blob; finer-grained ESO mappings are a separate cleanup.
- Backup/restore strategy — touched on in Issue A (independent recovery)
  but the broader backup design lives elsewhere.

---

## Suggested PR plan

1. **PR 1** — Issue A (Keycloak Postgres split). Has a data migration step;
   keep it on its own. Update `helm/`, `docker-compose.yaml`, README,
   `values.example.yaml`.
2. **PR 2** — Issues B + C + D bundled. All chart-only, no migration risk
   beyond a one-time secret-rotation for B/C.
3. **PR 3** — Issue E (NetworkPolicies). Independent of the others; safe
   to merge last so policies can be authored against the final topology.
