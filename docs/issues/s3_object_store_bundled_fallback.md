# S3 object store is a near-hard requirement — ship a bundled fallback

**Status**: RESOLVED (2026-07-14) — all 3 proposal items shipped (unpushed on
develop, live-verified on k3d). Kept in issues/ (not archived to done/) because
the orchestrator startup warning + chart NOTES/values cite this exact path.
Spec: `docs/superpowers/specs/2026-07-12-bundled-garage-object-store-design.md`.
Plan: `docs/superpowers/plans/2026-07-12-bundled-garage-object-store.md`.

**Decision of record**: platform features may **assume an S3-compatible
object store is present**. Self-host installs without an external store are
served by the chart-bundled Garage option, not by per-feature fallback logic.

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

Before this work, the chart deployed **no** store. Prod-private supplied an
external S3-compatible service, while local k3d carried separate parity
tooling. A fresh self-host install with neither silently lost virtual sessions,
snapshots, and suspension — each failing at a different, late point.

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
`logger.warning` from the lifespan when EITHER seam is unconfigured —
`S3_ENDPOINT` empty (snapshots/suspend/IDE/VM-extract) or the virtual tier
lacking a durable `s3` store (`memory` warns but flags non-durable). The two
almost always point at the same store, so a half-config is usually a mistake,
and virtual being the default session backend means a snapshots-only config
silently breaks the default UX; silent only when both seams have a durable
store (unit-tested in `tests/test_lite_workspace_dispatch.py`). All three
proposal items are addressed.

The 2026-07-13 local rollout also closed the chart-created credential lifecycle:
Garage keys render under `stringData`, `MCP_INTERNAL_KEY` is emitted, S3 IDs and
secrets are validated/preserved as atomic pairs, and malformed/orphan-prone
states fail closed. The bootstrap verifies an existing key's secret before
granting it and removes stale same-name chart-managed keys. Garage watches
Secret changes through Reloader. Chart-managed Secrets survive Tilt's
uninstall/reinstall Force Update and are reclaimed with `--take-ownership`.
A live force cycle preserved the Secret UID and full data digest and left
exactly one managed key for each bundled bucket; no Secret value was printed.

## Non-goals

- Per-feature fallback/gating logic in the cockpit or per-endpoint
  capability flags — explicitly rejected in the instant-landing design
  discussion (2026-07-11/12). The platform assumes the store exists; making
  that assumption safe is *this* issue, solved once at the deployment layer.
