# Branch (a) — PVC-backed workspace pods: implementation record

## Status (2026-06-29)

**Phase 0 + Phase 1 are BUILT, unit-tested, and k3d-E2E-verified. Uncommitted on
`develop`.** This delivers the *durability + self-cleanup* half: a job workspace
gets a PVC named by its UUID, files survive a pod crash (reattach by name), and
the PVC is reclaimed when the job goes terminal — with a backstop sweep so it
cannot leak. Verified end-to-end against the live orchestrator on k3d.

| Phase | Scope | State |
|---|---|---|
| **0 — the flip** | PVC-back job workspaces (flag-gated, jobs-only) | ✅ done + k3d-verified |
| **1 — GC discipline** | terminal delete + give_up gating + backstop `reap_orphans` | ✅ done + k3d-verified |
| **2 — crash-recovery reattach** | workspace-lost job re-dispatches → reattaches PVC → agent resumes on the files | ✅ **COMPLETE + full-E2E-verified (2026-06-29)** — G1+G2 (wedge eliminated, bounded fail-loud, working-tree preserved) + **Option 1 stable-DNS Service** (commit `7fb9e9e2`) fixing the ephemeral-IP churn. **Full real-job E2E PASSED** (job `b4025433`): killed a running job's pod → re-dispatched via pod arm (`vm.requested` null) → new pod reattached PVC + same DNS → agent resumed from checkpoint, sentinel survived, job back to `processing` not failed. Minor non-blocking follow-up: ~2.7 min reconnect-loop delay before recovery triggers → `workspace_reattach_ephemeral_ip_reconnect_churn.md`. See §Phase 2 |
| **3a — capacity guard** | per-class `ResourceQuota` on the ephemeral storage class (caps total storage **and** PVC count) + fail-closed clear-error on 403 | ✅ **done + k3d-verified (2026-06-30)** — **universal** (every substrate); helm-configurable, default off. See §Phase 3a |
| **3b — dead-node hardening** | RWO dead-node detach-wait + S3 fallback | ⏳ pending — **Longhorn/multi-node only**; does NOT reproduce on single-node local-path. Validate on homelab, not this k3d. See §Phase 3b |
| **rollout** | flag default-off in-chart; **ON in k3d dev**; dev-soak → prod flip | ⏳ pending |

**Decision: LOCKED — Branch (a), scoped to job (worker/loop) pods for v1.** This
is the chosen fork of [`workspace_pvc_backed_migration.md`](workspace_pvc_backed_migration.md).
Sessions and VMs (branch b) are explicit non-goals for v1 (§Non-goals).

**One-line model:** PVC named by job UUID, mounted at `/home/agent-host`,
reattached by name on any recreate, deleted when the job is terminal. The
Postgres LangGraph checkpoint (cross-pod, already live) carries reasoning state;
the PVC carries the files; on a crash the new pod reattaches the volume and
resumes the checkpoint — coherent by construction, no snapshot/restore dance.

---

## Implementation record — what was actually built (Phase 0 + 1)

### Phase 0 — the flip (create path)

- **Flags** (`container_provisioner.py:88-89`): `WORKSPACE_PVC_ENABLED`
  (default `False`, via the existing `_env_flag` helper) + `WORKSPACE_PVC_SIZE`
  (default `10Gi`).
- **`create_workspace`** (`container_provisioner.py:222-248`): when
  `_pvc_enabled and owner.kind == "job"`, compute `pvc-workspace-<id[:12]>`,
  `_create_pvc(...)` with owner label `{srw/job-id: <uuid>}`, **fail-closed**
  (PVC create fails → set `status=failed`, return False), then pass `pvc_name`
  into `_build_pod_manifest` (PVC-vs-emptyDir branch already existed,
  `:1129`). **Created BEFORE the seed ConfigMap** so a PVC failure leaves
  nothing to clean up (the prerequisite resource).
- Sessions stay emptyDir (gated on `owner.kind == "job"`); one-line lift later.

### Phase 1 — GC discipline (the leak guard)

- **1a. Inline terminal delete** (`workspace_manager.py:441` `delete()`,
  reclaim at `:467-471`): after deleting the pod, if `_is_terminal(inst)` **and**
  not `volume_ephemeral`, call `delete_workspace_pvc(owner)`. Logs
  `Terminal workspace PVC reclaimed for <kind> <id>`. Idle/suspend reaps keep the
  PVC (reattach next dispatch); emptyDir reaps skip it. Pod-delete failure
  short-circuits before touching the volume.
- **1b. `give_up` gating** (`workspace_manager.py:329`, gate at `:349`): only
  recreate-and-reattach when **not terminal** and PVC-backed, so it can't race
  1a's terminal delete.
- **1c. Backstop `reap_orphans`** (`workspace_manager.py:478`, log `:548`): lists
  PVCs by `srw.io/component=agent-workspace`, filters to name `pvc-workspace-*`
  with an `srw/job-id` label, and deletes any whose job is **terminal or gone**
  **and** has no live pod. Uses a **3-way DB lookup** (`SELECT status ... WHERE
  id=$1::uuid`): row present → use status; no row → genuinely gone (reap); query
  raised / malformed-uuid label → unknown (skip, never reap). Wired as a
  once-per-tick `getattr(manager, "reap_orphans", None)` hook in
  `reconciler.tick()` (`reconciler.py:238-241`) with an `orphans_reaped` stat
  (`:127`). Emits a WARN with the orphan count when it reaps (alerting hook).

### Helm

- `configmap.yaml:89-90` — emits `WORKSPACE_PVC_ENABLED` / `WORKSPACE_PVC_SIZE`.
- `values.yaml:961-963` — `workspace.pvcEnabled: false` + `pvcSize: "10Gi"`
  (plus the pre-existing `ephemeralStorageClass`, `:953`).
- **`orchestrator/deployment.yaml:244-253`** — maps `WORKSPACE_PVC_ENABLED` /
  `WORKSPACE_PVC_SIZE` from the configmap to env. **This was NOT in the original
  plan and is essential** — see Gotcha #1.

### Tests (all green: 294 lifecycle tests, ruff clean)

- `tests/test_container_provisioner.py` → `TestCreateWorkspacePvc` (4): PVC
  create+mount for jobs; emptyDir when disabled; session-skip (v1 scope);
  fail-closed before pod on PVC error.
- `tests/test_lifecycle_workspace_manager.py` → `TestDeleteTerminalPvc` (5),
  `TestGiveUpTerminal` (1), `TestReapOrphans` (7, incl. the DB-error-≠-gone
  safety case).
- `tests/test_lifecycle_reconciler_reap.py` → 3 hook tests (called+recorded /
  absent-skipped / failure-tolerant).
- Fixed 2 pre-existing exact-stats-dict assertions for the new `orphans_reaped`
  key (`test_lifecycle_skeleton.py`, `test_lifecycle_agent_manager.py`).

### Files touched (uncommitted on `develop`)

```
orchestrator/services/container_provisioner.py        flags + create_workspace flip + docstring
orchestrator/services/lifecycle/workspace_manager.py  delete() terminal GC + give_up gate + reap_orphans()
orchestrator/services/lifecycle/reconciler.py         once-per-tick orphan-sweep hook + stat
helm/templates/configmap.yaml                         WORKSPACE_PVC_ENABLED/SIZE keys
helm/templates/orchestrator/deployment.yaml           map those keys → orchestrator env  (Gotcha #1)
helm/values.yaml                                       workspace.pvcEnabled / pvcSize
tests/test_container_provisioner.py                   TestCreateWorkspacePvc
tests/test_lifecycle_workspace_manager.py             TestDeleteTerminalPvc / GiveUpTerminal / ReapOrphans
tests/test_lifecycle_reconciler_reap.py               orphan-sweep hook tests
tests/test_lifecycle_skeleton.py, test_lifecycle_agent_manager.py   orphans_reaped stat-dict fix
deployment/values-local.yaml                           (gitignored) ephemeralStorageClass: local-path + pvcEnabled: true
```

---

## k3d E2E results — all 5 passed (live orchestrator, 2026-06-29)

Driven through the running orchestrator (confirmed `reap_orphans` present,
`pvc_enabled=True`); the GC paths were exercised by the **actual reconciler loop**
on the leader replica, not mocks.

1. **Create + mount** — PVC `Bound` (10Gi RWO local-path), pod Running, mounts
   `/home/agent-host` as `persistentVolumeClaim` (not emptyDir), `srw/job-id`
   label stamped. ✅
2. **Reattach survives pod kill (keystone)** — wrote a sentinel → force-deleted
   the pod → recreated → sentinel survived on the new pod. ✅
3. **Terminal GC** — direct teardown reclaimed pod+PVC+PV (no orphan). Live
   reconciler then proved it: log `Terminal workspace PVC reclaimed for job …
   (workspace_manager.py:471)` + `PVC deleted` + pod/PVC/PV gone. ✅
4. **Backstop `reap_orphans`** — orphan PVC (no pod, no job row) swept by the live
   reaper: `Orphan workspace PVC reaped … status=gone` + tick
   `{… 'orphans_reaped': 1}`. ✅
5. **Mixed fleet** — the pre-existing emptyDir session pod was untouched (still
   emptyDir, no PVC ever created — v1 jobs-only honored); reaper handled both
   kinds; zero PVCs leaked at the end. ✅

---

## Gotchas discovered during k3d verification (read before prod)

1. **Orchestrator env is `configMapKeyRef`, not `envFrom`.** New configmap keys
   do **not** reach the orchestrator unless explicitly mapped in
   `orchestrator/deployment.yaml`. The orchestrator runs *both* the dispatcher's
   `create_workspace` and the lifecycle reaper, so it must see
   `WORKSPACE_PVC_ENABLED`. (Same trap as the cross-pod-checkpointer flag.) Fixed
   here; **carry this pattern to any future workspace flag.**
2. **Storage class must exist on the target cluster.** Default is
   `longhorn-ephemeral` (Delete-reclaim) for prod. k3d ships only `local-path`,
   so `deployment/values-local.yaml` sets `workspace.ephemeralStorageClass:
   local-path`. A missing class → PVCs hang `Pending` forever (no error).
3. **`tilt trigger srw` does a full helm uninstall/reinstall + image rebuild**
   (~6 min). Data PVCs survive (`helm.sh/resource-policy: keep`), so it's
   recoverable, but for a **config-only** change prefer patching the configmap +
   `kubectl set env deploy/srw-orchestrator --from=configmap/srw-config
   --keys=…` over triggering the whole helm resource.
4. **local-path = hostPath bind-mount → no RWO detach dance.** Reattach on a
   single node is instant. The dead-node stale-`VolumeAttachment` problem is a
   **Longhorn/networked-storage** concern → that's exactly Phase 3, and it does
   not reproduce on k3d.
5. **PVC delete is not instant (~40s on local-path).** The `pvc-protection`
   finalizer waits for full pod termination + the provisioner cleans the hostpath.
   The backstop is idempotent (404-safe), so a slow delete is harmless — but
   don't assert "gone" within a tight window.
6. **`reap_orphans` only acts on valid-UUID labels.** The `::uuid` cast makes a
   malformed `srw/job-id` label safely skipped (never reaped); a valid UUID with
   no `jobs` row is treated as "gone" → reaped. All real PVCs carry a valid UUID.
7. **Reconciler runs on the leader only.** With 2 orchestrator replicas, only the
   leader ticks the reconciler / runs `reap_orphans` — grep the leader's logs
   when verifying.

---

## Phase 2 — crash-recovery reattach (IMPLEMENTED + E2E'd 2026-06-29 — PARTIAL)

**Goal:** a job whose workspace pod dies mid-run re-dispatches, recreates the
pod, **reattaches its PVC**, and resumes from the Postgres checkpoint with its
files intact — no data loss, no manual intervention.

> **CORRECTION (2026-06-29, from a code-level resume-path investigation):** an
> earlier draft of this section claimed Phase 2 was "verification only / nothing
> in code." **That was wrong.** The PVC substrate (Phase 0/1) is necessary but
> **not sufficient** — with the resume path as it stands today, the agent
> **actively wipes the reattached PVC** on resume (trace in G2 below). Phase 2
> needs **two** changes, *both* in the recovery / agent-resume path, and **both
> are untestable until the wedge-fix (G1) lands**. The PVC half is the foundation
> they sit on; it does not deliver job durability on its own.

### Implementation + E2E result (2026-06-29)

**G1 + G2 are implemented, deployed to k3d, and E2E-tested.** Net result: **the
wedge is eliminated and workspace data is preserved, but auto-resume does NOT
yet complete** — a deeper ephemeral-IP reconnect race (filed separately) makes
recovery fail-loud after the cap instead of resuming.

| Aspect | Result |
|---|---|
| G1 routing — pod jobs recover via PVC reattach, never the VM arm | ✅ verified (`vm.requested` null every cycle) |
| G1 bounded cap → fail-loud (no forever-wedge) | ✅ verified (terminal `failed` at cap, clean `freeze_data`) |
| PVC survives pod kill + reattaches by name | ✅ verified (same PV across every recreate) |
| Working tree preserved (untracked sentinel survives) | ✅ verified (sentinel intact across recreates) |
| **Agent reconnects → G2 preserve branch runs → job resumes** | ❌ **blocked** — see "the blocker" below |

**Bug found + fixed by the E2E (would have shipped blind):** G1 first relied on
`delete_workspace` to set `workspace_container.status="deleted"`, but its
404/"already deleted" branch — the *always-taken* path, since the pod is already
gone — does **not** set the status. The stale `status="ready"` made
`_job_needs_sandbox` return False (`main.py:3157`), so the resume reused the dead
pod IP in an infinite loop (`recovery_attempts` 1/3 → 2/3 → 3/3 on the same dead
IP). **Fixed:** the handler now explicitly merges
`{"status":"deleted","pod_ip":None}` so the dispatcher recreates + reattaches.

**The blocker (new issue):** the recreate gives the pod a **new ephemeral IP**
each cycle (`.79 → .88 → .93 → .95` observed); the resuming agent dials a
**stale IP** from a prior cycle → can't connect → reports `workspace_unavailable`
→ another recovery → another new IP → churns to the cap → **fail-loud**. G2's
preserve branch never gets exercised (no agent ever SSH-connects to the
reattached pod). Filed:
[`workspace_reattach_ephemeral_ip_reconnect_churn.md`](../issues/workspace_reattach_ephemeral_ip_reconnect_churn.md).
**Net today:** "wedge forever" → "fail cleanly in ~5 min with the PVC data
intact" (a strict improvement), but not seamless resume. The proper fix is a
**stable workspace address** (headless Service / DNS so recreate → same address).

**E2E record:** k3d, fresh scholar job `d65d93d3`, `WORKSPACE_PVC_ENABLED=true`,
`CHECKPOINTER_BACKEND=postgres`, agent image with G2 (`tilt-fb1597…`). Planted an
untracked `E2E_SENTINEL.txt`, force-deleted the workspace pod mid-run → G1 logged
`pod recovery attempt 1/3 (PVC reattach)`, PVC stayed `Bound`, new pods reattached
the same PV, the sentinel survived every cycle; the agent dialed stale `.88` while
the pod was at `.95` → cap → `failed` with `recovery exhausted after 3 attempts`.
(An earlier run on the developer→scholar subjob first revealed the stale-`ready`
loop bug, which was fixed before this run.)

### G1 — control-flow wedge-fix (IMPLEMENTED — `main.py:10118`)

The `workspace_unavailable` handler previously
**unconditionally stamped `ctx["vm"]={requested:True,recovering:True}` on pod-backed
jobs** → routed them into the VM arm → **wedged in `paused` forever**, never
reaching `ensure_workspace`/the pod arm where a recreate (and thus reattach)
happens. Full trace in
[`loop_job_workspace_lost_wedged_in_recovery.md`](../issues/loop_job_workspace_lost_wedged_in_recovery.md).
**Shipped fix:** branch on backend (`_job_needs_vm` on the original job); pod jobs
record a bounded `recovery_attempts`, **explicitly invalidate the stale container
(`status="deleted"`, `pod_ip=None`)**, delete the dead pod (best-effort), and
re-dispatch through the pod arm; at the cap they **fail loud** (`update_job_status`
`failed` + `freeze_data`). The re-dispatch is a `resume` (the job is paused →
checkpoint resume), **not** a fresh dispatch (which would hit `initialize()`'s
`rm -rf`, see G2).

### G2 — backend-aware resume detection (the gate that makes reattach *usable*)

Even once G1 re-dispatches a pod job through the pod arm, the **agent's resume
gates are keyed on the agent-pod-LOCAL path, not the remote workspace**, so they
don't see the reattached PVC and end up clobbering it:

- `WorkspaceManager.path` is a local `pathlib.Path` (`core/workspace.py:243-245`);
  `.path.exists()` checks the **harness-pod-local** fs. The reattached PVC lives
  on the **remote** workspace pod, reached via `backend.exists()` / `backend.root`
  (`core/backends/remote.py:459/160`).
- Resume flow in `agent.py`:
  - `:1836` `if resume and not path.exists() and git_remote_url:` → for a remote
    backend `path.exists()` is **False**, so this **pod-handoff clone from Gitea
    fires** — but `git clone` into the non-empty reattached dir **fails** → falls
    through (`:1868`).
  - `:1873` `if resume and path.exists():` (the **non-destructive** branch that
    reuses existing files) → **never fires** for a remote backend (local path
    False).
  - `:1916` fresh `initialize()` → `core/workspace.py:295/313`
    **`rm -rf {backend.root}/*`** on the **remote** workspace → **the reattached
    working tree is wiped** and replaced with the last-pushed Gitea state.

  Net: reattach is **defeated** — the agent discards the volume it just got back.

**The fix:** make the resume detection **backend-aware** — gate on
`backend.exists(<marker>)` (remote), not `path.exists()` (local). When the
reattached remote workspace is present, take the **non-destructive resume branch**
(`:1873`-style: re-init the git-manager handle + todo manager, ensure remote +
branch, **no `rm -rf`, no clone**). This is the *inverse* of what the issue doc's
**Tier-2 Option B** proposed ("force a fresh clone into the blanked box"): under
PVC reattach (Option C) we **detect and preserve** the workspace instead of
re-cloning it. Touches `agent.py:1835-1920` (and mirror the same gate in
`api/persistent_session.py:346/421` only if/when sessions opt into PVCs — v1
non-goal). **Risk:** changes resume behavior for *all* jobs, so it must land + be
tested with G1 in place via the real recovery E2E — not blind.

### Verification — RAN 2026-06-29 (PARTIAL pass)

- E2E (test-plan step 7) was executed on k3d (see "Implementation + E2E result"
  above). **Passed:** pod-arm routing (not VM), recreate, PVC reattach (same PV),
  un-pushed working tree preserved (sentinel survived), bounded fail-loud.
  **Did not pass:** the agent never reconnects to the recreated pod (stale
  ephemeral IP), so it never reaches G2's preserve branch and the job fail-louds
  instead of resuming → `workspace_reattach_ephemeral_ip_reconnect_churn.md`.
- Coherence: disk + Postgres checkpoint are both "as of the crash"; the disk may
  lag the checkpoint by ≤1 unfsynced super-step. With G2 (preserve, don't clobber)
  this is the same tolerance the agent already has mid-run — **no new coherence
  guard for v1**. (Not yet exercised live — gated behind the reconnect fix.)

### Ownership note

G1 is squarely the recovery session's (control-flow). G2 lives in the same
agent-resume code the recovery session's Tier-2 already touches
(`agent.py:1835-1889`), so it should be **coordinated with that session** rather
than landed blind from here — both are untestable without G1, and editing the
resume path from two sessions will collide.

**This supersedes the Tier-2 fork** in both recovery docs: PVC reattach (+ G2)
replaces both *Option A (snapshot-restore)* (no S3 tar/extract on the hot path)
and *Option B (blank + checkpoint)* (the workspace is not blank). The never-restored
job S3 snapshot becomes **DR-only** (kept, not removed).

---

## Phase 3 — split by storage substrate

Phase 3 is **two independent things**, and which one an operator needs is
decided by their storage substrate — not by deployment size. Treating them as
one "prod hardening" block was the wrong framing.

| Item | Who needs it | Reproduces on this k3d? | State |
|---|---|---|---|
| **3a — capacity guard** | **everyone** (local-path, Longhorn, cloud) | ✅ yes | ✅ done + verified |
| **3b — dead-node RWO detach-wait + S3 fallback** | **only** multi-node + networked-RWO (Longhorn/Ceph/EBS) | ❌ no (single node = no failover target, hostPath = no detach dance) | ⏳ pending → homelab |

### Phase 3a — capacity guard (DONE + k3d-verified 2026-06-30)

A namespaced **`ResourceQuota` keyed on the *ephemeral* workspace storage
class** that caps **both** the total storage and the count of concurrent
workspace PVCs of that class. When the cap is hit, `_create_pvc` gets a **403**,
fails closed (no pod, no emptyDir fallback — capacity exhaustion must never
silently drop durability), and logs a **distinct** `Workspace capacity quota
exceeded …` line so an operator/alert can tell "fleet at capacity" from a real
infra failure (which logs the generic `Failed to create PVC`).

**Why per-storage-class, not namespace-wide `requests.storage`:** a namespace
quota would conflate workspace PVCs with the platform's own ~15 data PVCs
(Postgres, Mongo, Neo4j, Gitea, OpenCloud, …), forcing the operator to track a
moving platform baseline. A per-class quota bounds *workspace* storage in
isolation — **but only if `ephemeralStorageClass` is dedicated to workspaces.**
On prod `longhorn-ephemeral` already is. On single-node/local-path the platform
*and* workspaces share `local-path` by default, so a local-path operator who
wants the quota should create a **dedicated local-path-backed class** (same
`rancher.io/local-path` provisioner, distinct name) and point
`ephemeralStorageClass` at it. (Without a dedicated class the quota still works
but also counts platform PVCs — usually not what you want.)

**Implementation:**
- `helm/templates/workspace-resourcequota.yaml` — renders the quota only when
  `workspace.pvcEnabled && workspace.resourceQuota.enabled`; `required`-guards a
  non-empty `ephemeralStorageClass` (a quota can't key on an empty class).
- `helm/values.yaml` — `workspace.resourceQuota.{enabled:false, maxStorage:200Gi,
  maxCount:20}`. No configmap/env wiring needed — the quota is a pure apiserver
  object; the orchestrator only ever *sees* the resulting 403.
- `container_provisioner.py` `_create_pvc` — a `403` branch logs the distinct
  capacity error and returns `False` (the existing fail-closed in
  `create_workspace` then aborts before the pod). `409` (reattach) and other
  errors are unchanged.

**k3d verification (2026-06-30, non-disruptive — throwaway dedicated class, never
touched the shared orchestrator):**
- `helm template` renders correct per-class resource names
  (`<class>.storageclass.storage.k8s.io/requests.storage` + `…/persistentvolumeclaims`);
  renders nothing when disabled; errors with the `required` message when the
  class is empty.
- **Count cap (maxCount=1):** PVC #1 created and **`Pending` under
  WaitForFirstConsumer yet already `used=1`** (proves a PVC consumes quota at
  admission, *before* a pod schedules — exactly the orchestrator's create-PVC-
  then-pod order); PVC #2 → `403 Forbidden: exceeded quota …
  persistentvolumeclaims=1, used=1, limited=1`.
- **Storage cap (maxStorage=1Gi):** PVC #1 (1Gi) fills it; PVC #2 →
  `403 Forbidden: exceeded quota … requests.storage=1Gi, used=1Gi, limited=1Gi`.
- Unit: `test_pvc_quota_403_fails_closed_with_capacity_log` (403 → `False`, no
  pod, `status=failed`, capacity log fires).

**Deferred nicety (non-blocking):** the *job-facing* error is still the generic
"PVC creation failed"; only the orchestrator *log* says "capacity quota
exceeded." Threading a capacity reason into the job context would need a richer
`_create_pvc` return; left for later since the operator signal (log/alert) is
the one that matters for a capacity event and fail-closed is already safe.

### Phase 3b — dead-node RWO detach-wait + S3 fallback (pending; Longhorn/multi-node only)

Does **not** reproduce on this k3d (Gotcha #4: single node = nowhere to fail
over; `local-path` = hostPath bind-mount = no `VolumeAttachment` detach dance).
On **multi-node networked-RWO** (Longhorn/Ceph/cloud-EBS) when the old pod's
node dies, K8s holds a stale `VolumeAttachment` on the dead node and the new
pod's mount **blocks** until it's force-detached (the *data* survives via
Longhorn replicas + `auto-salvage`; only the *attach* hangs). Plan: a bounded
detach-wait in the recreate path; on timeout, fall back to S3-restore onto a
healthy node. This is **substrate-specific hardening for the prod/homelab
Longhorn config** — validate on the homelab (real multi-node Longhorn) or a
purpose-built multi-node-k3d + Longhorn rig, not this single-node k3d. Folds in
the ~2.7 min reconnect-loop follow-up
([`workspace_reattach_ephemeral_ip_reconnect_churn.md`](../issues/workspace_reattach_ephemeral_ip_reconnect_churn.md)),
since on a node death the agent-reconnect and volume-detach latencies stack.

---

## Storage substrate matrix (read before choosing a deployment)

The PVC feature (Phase 0/1/2) was proven on single-node `local-path` k3d, so a
**single-server / local-path "big server" deployment is a first-class, fully
working target today** — it does **not** need Phase 3b. What differs by
substrate is durability and capacity semantics:

| Property | `local-path` (single node / k3s) | `longhorn-ephemeral` (prod/homelab, multi-node) |
|---|---|---|
| Reattach after pod crash | ✅ instant (hostPath bind-mount) | ✅ (networked RWO) |
| Node **reboot/maintenance** | ✅ data persists on disk → pods restart → PVCs reattach → jobs auto-recover (G1) | ✅ |
| Node **permanent loss** | ❌ no replication → PVC data gone → **S3 snapshot is the only DR** | ✅ survives (replicas + `auto-salvage`); 3b forces the stale attach |
| Per-volume **size enforcement** | ⚠️ `pvcSize` is a *hint* on a hostPath dir — a runaway workspace can overfill the node disk | ✅ enforced (block device) |
| Capacity guard (3a) | ✅ works (use a dedicated local-path class to isolate) — but pair with **disk monitoring**, since the quota caps *requests*, not *bytes written* | ✅ works (dedicated class already) |
| Phase 3b (dead-node detach) | n/a (no failover target) | required |

**Takeaways for a local-path single-server operator:** put local-path on a big
fast disk; rely on the S3 snapshot as your DR (no replicas to save you); enable
3a with a dedicated workspace class **and** watch disk usage (hostPath doesn't
hard-enforce the 10Gi). Everything else already works.

---

## Rollout status

- ✅ Flag default-off in-chart (zero behavior change on merge).
- ✅ **ON in k3d dev** (`values-local.yaml`), E2E gate steps 1-6 passed.
- ⏳ Commit + push Phase 0/1 to `develop`.
- ⏳ Dev soak — watch orphan-PVC WARN count + storage capacity.
- ⏳ Prod flip — only after dev soak shows zero orphan growth across a full
  create→reap→GC cycle. **Mixed fleet is safe and is the rollout mechanism** (the
  reaper reads each pod's actual volume mode), so there is no big-bang cutover;
  rollback = flip the flag off (new pods revert to emptyDir, existing PVC pods
  finish and GC normally). No data migration either way.

---

## Test plan status

**Unit — ✅ done** (see Implementation record): create-flip on/off/session,
delete() terminal-vs-idle-vs-emptyDir, give_up gating, reap_orphans selection +
DB-error safety, reconciler hook.

**k3d E2E — steps 1-6 ✅ done** (see results above). Step 7 (Phase 2) **RAN
2026-06-29 — PARTIAL**:
7. **Recovery (Phase 2):** reproduced the `19707fa1` shape (job `d65d93d3`) — kill
   the workspace pod mid-run → ✅ re-dispatch through the pod arm (not VM), ✅ PVC
   reattach (same PV), ✅ working tree preserved (sentinel survived), ✅ bounded
   fail-loud; ❌ checkpoint resume — the agent can't reconnect (stale ephemeral IP)
   so it fail-louds instead of resuming →
   `workspace_reattach_ephemeral_ip_reconnect_churn.md`. Re-run after that fix to
   close step 7.

---

## Background — why this shape (kept for reviewers)

**Cross-attach is impossible by construction; the real risk is leaks.** Workspace
pods are standalone single pods (`restartPolicy: Never`), not ReplicaSet members,
and PVC names are deterministic + owner-keyed (`pvc-workspace-<uuid[:12]>`). The
name *is* the binding: fresh job → fresh UUID → fresh PVC; same-job recreate →
same name → reattach; two jobs never collide. The problem that got PVCs ripped
out in `c182aefb` (Workspace Simplification) was **orphan cleanup leaks** — so the
whole of Phase 1 is the "PVC dies when the job dies" guard, not attach safety.

**~90% was already plumbed, dormant.** `_create_pvc` (RWO, 409-reuse),
`_delete_pvc` (404-safe), `delete_workspace_pvc`, the `_build_pod_manifest`
PVC branch, the reaper's volume-mode read (`_pod_volume_is_ephemeral` →
`volume_ephemeral`), `is_state_ephemeral`, the `give_up` reattach arm, and the
`ensure_workspace` drift-recreate probe all pre-existed. The only reason no job
was PVC-backed was `create_workspace` never passing `pvc_name`. Phase 0 was that
one wiring; Phase 1 closed the GC gap on the reconciler reap path.

---

## Risks & mitigations

| Risk | Mitigation | Status |
|---|---|---|
| Orphan PVC/PV leak (the 2026-04 regression) | Delete-reclaim + inline terminal delete + backstop reaper + WARN-on-orphan + ResourceQuota | inline+backstop ✅; ResourceQuota = Phase 3a ✅ |
| RWO mount blocks on dead-node stale VolumeAttachment | bounded detach-wait + S3 fallback | Phase 3b (Longhorn/multi-node only) |
| PVC bind latency on create | acceptable; only first-create, not reattach | observed fine on k3d |
| Disk↔checkpoint ≤1-step skew on hard kill | documented residual; existing re-clone/re-read gates tolerate it | Phase 2 verify; no new guard for v1 |
| Capacity exhaustion | per-class ResourceQuota (caps storage+count) + fail-closed + backstop reaper | Phase 3a ✅ (k3d-verified) |
| local-path has no replication / no hard size enforcement | S3 snapshot = DR; dedicated class + disk monitoring | documented (§Storage substrate matrix) |

## Non-goals (v1)

- **Sessions** — stay emptyDir (brain in Postgres). One-line gate lift later.
- **VMs (branch b)** — different substrate (KubeVirt CDI DataVolume); separate
  effort. See [`workspace_pvc_backed_migration.md`](workspace_pvc_backed_migration.md) §Branch (b).
- **Removing the S3 snapshot path** — kept as cross-node/DR backstop.
- **Collapsing duplicated PVC plumbing** (`persistent_provisioner` vs
  `container_provisioner`) — only relevant once sessions opt in.

## Coupling — keep these docs in lockstep

- [`loop_job_workspace_lost_wedged_in_recovery.md`](../issues/loop_job_workspace_lost_wedged_in_recovery.md)
  — **wedge-fix (Tier-1) is the Phase 2 prerequisite** (separate session). Record
  "PVC reattach" as the chosen Tier-2 direction there.
- [`snapshot_restore_dead_for_jobs.md`](../issues/snapshot_restore_dead_for_jobs.md)
  — same Tier-2 decision; with PVC reattach the never-restored job snapshot
  becomes DR-only (kept, not removed).
- [`workspace_pvc_backed_migration.md`](workspace_pvc_backed_migration.md) — the
  open-fork brief; Branch (a) is the decided direction, Phase 0/1 shipped per this
  doc.
