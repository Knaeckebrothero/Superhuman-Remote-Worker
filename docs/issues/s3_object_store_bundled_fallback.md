# S3 object store is a near-hard requirement — ship a bundled fallback

**Status**: IN PROGRESS (2026-07-12) — chart-bundled Garage store.
Spec: `docs/superpowers/specs/2026-07-12-bundled-garage-object-store-design.md`.
Plan: `docs/superpowers/plans/2026-07-12-bundled-garage-object-store.md`.

**Decision of record**: platform features may **assume an S3-compatible
object store is present**. Self-host installs without an external store are
served by a future chart-bundled option, not by per-feature fallback logic.

## Problem

An S3-compatible object store started as an optional dependency and has
quietly become load-bearing. Consumers today:

- **Virtual workspace tier** (`virtualWorkspace.rclone` /
  `VIRTUAL_WORKSPACE_S3_*`, `helm/values.yaml:1341+`) — the instant/lite
  session backend stores all workspace files in a per-thread S3 prefix.
  Empty `rclone.type` disables the tier; a `virtual` job/session then fails
  at dispatch/attach (`LiteWorkspaceConfigError`). With the instant-landing
  feature (`docs/features/instant_landing_session.md`) making virtual the
  *default* session backend, a store-less install has a broken
  out-of-the-box experience.
- **Workspace snapshots** (`SNAPSHOT_S3_*`) — workspace suspension/resume,
  IDE sessions, VM lifecycle (S3→VM extract), container/VM provisioners
  (`orchestrator/services/workspace_suspension.py`, `snapshot_service.py`,
  `ide_session.py`, `lifecycle/*`). No store → no suspend/restore paths.
- **Main-cloud object storage** (`cloud.objectStore` / `OBJECTSTORE_S3_*`,
  `helm/values.yaml:859+`) — the prod-private OpenCloud layout runs against
  an external bucket.

The chart itself deploys **no** store (`helm/values.yaml:63` — the MinIO
host value is a cockpit deep-link only). Prod-private brings its own MinIO
(`minio.minio.svc`); local k3d has MinIO parity tooling. A fresh self-host
install with none of these silently loses virtual sessions, snapshots, and
suspension — each failing at a different, late point.

## Proposal

1. Add an **opt-in bundled S3 store** to the Helm chart — a small
   single-node Garage or MinIO deployment + PVC, wired automatically into
   `virtualWorkspace.rclone`, `SNAPSHOT_S3_*`, and (optionally)
   `cloud.objectStore` when enabled and no external endpoint is set.
2. Mark it clearly **not for production** (values comment + NOTES.txt
   warning on install): single replica, no erasure/replication, PVC-bound;
   external S3 remains the recommended path. Garage is the leaner candidate
   (single small binary, low idle RAM); MinIO is the familiar one — decide
   at implementation time.
3. Install-time visibility: when no store is configured at all (bundled off,
   external unset), surface one loud warning at orchestrator startup listing
   the features that will be degraded, instead of today's per-feature late
   failures.

**Current scope:** Item 1 (bundled Garage backing snapshots + virtual tier)
IMPLEMENTED + live-verified on k3d (chart-only). Item 2 guardrails shipped with
it. Item 3 (orchestrator no-store startup warning) IMPLEMENTED —
`_object_store_startup_warning` in `orchestrator/main.py` emits one loud
`logger.warning` from the lifespan when `S3_ENDPOINT` is empty and the virtual
tier has no durable `s3` store (unit-tested in
`tests/test_lite_workspace_dispatch.py`). All three proposal items now
addressed; unpushed on develop.

## Non-goals

- Per-feature fallback/gating logic in the cockpit or per-endpoint
  capability flags — explicitly rejected in the instant-landing design
  discussion (2026-07-11/12). The platform assumes the store exists; making
  that assumption safe is *this* issue, solved once at the deployment layer.
