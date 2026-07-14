# Bundled Garage object store — design

> ## ✅ STATUS: COMPLETE — implemented on `develop` (unpushed) + live-verified on k3d, 2026-07-14
>
> Bundled single-node Garage (11 SDD tasks + 2 live-E2E fixes + final-review wave),
> plus the item-3 startup warning (either-seam) + opt-in `OBJECT_STORE_REQUIRED`
> fail-closed flag, plus the local-dev MinIO→Garage swap. Live-verified on k3d:
> bootstrap idempotent, S3 round-trip, bucket-scoping, agent-rclone (provider
> "Other") round-trip. Origin issue:
> `docs/done/s3_object_store_bundled_fallback.md`.

**Date**: 2026-07-12
**Status**: Approved — ready for implementation plan
**Issue**: `docs/done/s3_object_store_bundled_fallback.md` (OPEN → in-progress)
**Scope**: Helm chart only. No orchestrator/agent code changes.

## Problem

An S3-compatible object store began as an optional dependency and has quietly
become **load-bearing** for the platform. Three consumers now hard-depend on it:

1. **Virtual workspace tier** (`VIRTUAL_WORKSPACE_S3_*`) — and this is the
   sharpest edge, because the instant-landing feature made `virtual` the
   *default* session backend. A store-less install fails at dispatch/attach
   with `LiteWorkspaceConfigError`.
2. **Workspace snapshots** (`S3_*` / `SNAPSHOT_S3_*`) — suspend/resume, IDE
   sessions, VM lifecycle. No store → no suspend/restore.
3. **Main-cloud object storage** (`OBJECTSTORE_S3_*`, OpenCloud/Nextcloud) —
   prod-private runs against an external bucket. **Out of scope here.**

Today the chart deploys **zero** store. Prod-private brings its own external
MinIO; local k3d has MinIO parity tooling; a fresh self-host install with none
of these **silently loses** virtual sessions, snapshots, and suspension — each
failing at a different, late point.

The **decision of record** (issue doc): the platform may *assume* an
S3-compatible store exists. Rather than per-feature fallback/gating logic
(explicitly rejected), make that assumption safe **once, at the deployment
layer**, by shipping a chart-bundled store.

## Decisions (locked in brainstorming)

- **Bundle Garage in-chart** — a single-node Garage deployment, opt-in, marked
  not-for-production. (Not: wiring to an external Garage; not: MinIO.)
  Garage keeps the self-host fallback on the same S3 technology the homelab DR
  hub is standardizing on — one technology to operate, one rclone quirk-profile.
- **Back two consumers**: snapshots (`srw-snapshots`) and virtual workspace
  (`srw-workspaces`). Leave the user-facing cloud (OpenCloud) on its own config.
- **Approach A**: Garage `StatefulSet` + idempotent bootstrap `Job` +
  **declarative pre-set scoped keys** (no random-key generation, no write-back
  to Secrets) + auto-wiring helpers. Mirrors the existing `postgres.yaml`
  bundled-stateful pattern and the `keycloak/bootstrap-job.yaml` pattern.
- **Startup warning** (issue-doc proposal #3 — a loud orchestrator-startup
  warning when *no* store is configured at all) is **tracked separately** as an
  orchestrator-side fast-follow. This PR stays pure-chart (YAML only).

## Non-goals

- Backing main-cloud object storage (`OBJECTSTORE_S3_*` / OpenCloud).
- Per-feature fallback/gating logic (rejected in the instant-landing design).
- Multi-node / HA / erasure-coded Garage. Single replica, PVC-bound.
- Production suitability. External S3 remains the recommended production path.
- The orchestrator-startup no-store warning (separate fast-follow).

## Design

### 1. Placement & values contract

New `templates/objectstore/` directory (parallel to `databases/`, `nats/`,
`keycloak/`). New top-level `garage:` block in `values.yaml`, named by concrete
tech (like `neo4j:` / `nats:` / `opencloud:`), disabled by default:

```yaml
# =============================================================================
# Bundled object store (Garage) — NOT FOR PRODUCTION
# =============================================================================
# Single-node Garage S3 for self-host installs with no external object store.
# When enabled AND the matching external endpoint is unset, the chart auto-wires
# this store into snapshots (S3_*) and the virtual workspace tier
# (VIRTUAL_WORKSPACE_S3_*). Single replica, PVC-bound, NO replication/erasure —
# external S3 remains the recommended production path.
garage:
  enabled: false
  image: dxflrs/garage:v1.0.1        # pinned
  replicas: 1                         # single-node layout — DO NOT raise
  storageClass: ""
  dataStorageSize: "20Gi"             # backs both buckets
  metaStorageSize: "1Gi"              # Garage metadata (subdir of the data PVC)
  buckets:
    snapshots: "srw-snapshots"
    workspaces: "srw-workspaces"
  bootstrap:
    image: ""                         # shell+curl image for the admin-API Job
    capacity: "20G"                   # single-node layout capacity
    resources: {}
  resources:
    requests: { memory: "256Mi", cpu: "100m" }
    limits:   { memory: "1Gi",   cpu: "1000m" }
```

### 2. Kubernetes resources

Mirrors `postgres.yaml` (bundled stateful) and `keycloak/bootstrap-job.yaml`
(post-install/upgrade bootstrap). All gated on `.Values.garage.enabled`.

- **PVC** (`helm.sh/resource-policy: keep`) — one volume mounted at
  `/var/lib/garage`, with `meta_dir` / `data_dir` as subdirs. A single-node
  fallback does not need meta on separate fast storage.
- **StatefulSet** `replicas: 1` — Garage container. Ports: **3900** (S3 API),
  **3901** (RPC), **3903** (admin). Readiness via the admin `/health` endpoint;
  liveness TCP on 3901. `GARAGE_RPC_SECRET` and `GARAGE_ADMIN_TOKEN` injected
  from the app Secret via env (kept out of the ConfigMap).
- **ConfigMap** — `garage.toml`: `replication_factor: 1` (single node),
  `metadata_dir` / `data_dir`, `[s3_api]` bind + region, `[admin]` bind.
  Secrets come from env, not the file.
- **Service** — ClusterIP `<release>-garage`, ports 3900 (S3) + 3903 (admin).
- **Bootstrap Job** — Helm hook `post-install,post-upgrade`, delete policy
  `before-hook-creation,hook-succeeded`.

  **Image constraint (important):** the `dxflrs/garage` image is shell-less
  (static binary, built `FROM scratch`), so we **cannot** exec the `garage` CLI
  in a shell script — neither in a Job nor via `kubectl exec` into the pod.
  The Job therefore runs a small **shell+curl image** and drives Garage's
  **admin HTTP API** end-to-end (all layout/bucket/key operations exist there):

  1. Poll `GET /health` (admin port) until the node is up.
  2. Read the node id from `GET /v1/status`.
  3. If layout is unassigned: stage a single-node role (`POST /v1/layout`
     with the node id + `bootstrap.capacity`) and apply it
     (`POST /v1/layout/apply`).
  4. Create each bucket (`POST /v1/bucket`) — ignore already-exists.
  5. **Import** each pre-set key (`POST /v1/key/import` with the id+secret
     from env) — ignore already-exists.
  6. Grant RW (`POST /v1/bucket/allow`) for each key on its bucket.

  Every step is check-or-ignore-conflict, so the Job is **idempotent** across
  `helm upgrade`. The Job needs only `GARAGE_ADMIN_TOKEN` + the four S3 cred
  values + the Garage service host — no RPC secret / node-host discovery for a
  CLI, because everything goes through the admin API.

  The `bootstrap.image` must ship a POSIX shell + `curl`; parse the node id and
  bucket id from JSON responses without requiring `jq` (use `grep`/`sed`) so the
  image stays airgap-friendly and unpinned-network-free. Pick a pinned image at
  implementation time.

### 3. Credentials & auto-wiring

**No random-key generation.** The bootstrap Job imports keys whose IDs/secrets
**already live in the app Secret**, so nothing is written *back* into a Secret
(GitOps/ESO-clean). It reuses the **exact Secret keys the consumers already
read** — this PR adds no new env-var contract for the orchestrator/agent:

- `srw-snapshots` key ← existing snapshot credential Secret keys. **Verify at
  impl time** whether these are `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` or
  the renamed `SNAPSHOT_S3_*` (rename in flight — see memory
  `srw_virtual_workspace_s3_provisioning`). Reuse whatever the orchestrator
  reads today. Orchestrator-side.
- `srw-workspaces` key ← `VIRTUAL_WORKSPACE_S3_ACCESS_KEY_ID` /
  `VIRTUAL_WORKSPACE_S3_SECRET_ACCESS_KEY`. Travels to the agent; bucket-scoped.

Two **new** Secret keys: `GARAGE_RPC_SECRET`, `GARAGE_ADMIN_TOKEN`. Wired
through both `secret.yaml` (chart-managed `secrets.values` path) and
`external-secret.yaml` (ESO/Vault path).

**Effective-endpoint helpers** in `_helpers.tpl`: when `garage.enabled` **and**
the external endpoint is empty, default the endpoints to the in-cluster Garage
service. **External always wins** — set an endpoint and the bundle is bypassed.

- `S3_ENDPOINT` → `http://<release>-garage:3900`
- `VIRTUAL_WORKSPACE_S3_ENDPOINT` → `http://<release>-garage:3900`
- rclone `type` → `s3`, `root` → `srw-workspaces`, `provider` → `Other`
  (rclone's Garage-compatible profile; path-style). Currently defaults to
  `Minio` — the helper picks `Other` when auto-wiring to bundled Garage.

`configmap.yaml` renders these via the helpers. The render sites for the rclone
`type` / `root` (wherever `virtualWorkspace.rclone.*` currently feeds config)
must be updated to use the effective (auto-wired) values — impl must locate and
update them.

### 4. Guardrails (not-for-production)

- Loud banner comment in `values.yaml` (above the `garage:` block).
- `NOTES.txt`: when `garage.enabled`, print a WARNING block — single-node, no
  replication, not for production; for prod set `garage.enabled=false` and
  configure external S3 (`s3.endpoint`, `virtualWorkspace.s3.endpoint`).
- `values.schema.json`: extend for the `garage` block. The chart enforces its
  schema, so omitting this breaks `helm install`.

### 5. Testing

- `helm lint` + `helm template` with a new `helm/ci/` values variant enabling
  `garage` (renders + schema-validates in CI). Assert the auto-wiring helpers
  resolve endpoints to the Garage service when external is unset, and to the
  external value when it is set.
- Local k3d/tilt smoke: `garage.enabled=true`, dispatch a `virtual` session →
  files round-trip; suspend/resume a workspace → snapshot lands in
  `srw-snapshots`. (Local tilt stack is Nextcloud-only / neo4j-off — enabling
  garage there is fine.)

### 6. Files

**New**
- `helm/templates/objectstore/garage-config.yaml`
- `helm/templates/objectstore/garage-statefulset.yaml`
- `helm/templates/objectstore/garage-service.yaml`
- `helm/templates/objectstore/garage-bootstrap-job.yaml`

**Modified**
- `helm/values.yaml` — `garage:` block + banner comment
- `helm/values.schema.json` — `garage` schema
- `helm/templates/_helpers.tpl` — effective-endpoint helpers
- `helm/templates/configmap.yaml` — use helpers for `S3_ENDPOINT`,
  `VIRTUAL_WORKSPACE_S3_*`, rclone `type`/`root`/`provider` render sites
- `helm/templates/secret.yaml` — `GARAGE_RPC_SECRET`, `GARAGE_ADMIN_TOKEN`
- `helm/templates/external-secret.yaml` — map `GARAGE_*`
- `helm/templates/NOTES.txt` — warning block
- `helm/ci/*-values.yaml` — a variant with `garage.enabled=true`
- `docs/done/s3_object_store_bundled_fallback.md` — status → in-progress,
  link this spec

## Open questions for implementation

- Exact snapshot credential Secret key names (`S3_*` vs `SNAPSHOT_S3_*`).
- Pinned `bootstrap.image` with shell + curl (airgap-friendly, no runtime
  package install).
- Confirm Garage admin API version path (`/v1/...` vs `/v2/...`) for the pinned
  `dxflrs/garage:v1.0.1` image.
- rclone `provider` value that works with Garage (`Other` + path-style, to
  confirm against the agent's rclone version).
