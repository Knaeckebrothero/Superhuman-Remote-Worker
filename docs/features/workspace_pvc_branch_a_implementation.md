# Branch (a) — PVC-backed workspace pods: implementation record

## Status (2026-06-30)

**Phases 0, 1, 2, 3a, and 3b are BUILT, unit/k3d-E2E-verified, and committed on
`develop`** (latest `a9485f21`; Phase 3b code is `868bfd32`). The PVC feature is functionally complete for
single-node/local-path and prod/Longhorn alike: a job workspace gets a PVC named
by its UUID, files survive a pod crash (reattach by name), a crashed job
**auto-resumes** on the reattached volume from its Postgres checkpoint (reached
at a stable headless-Service DNS address), the PVC is reclaimed when the job goes
terminal (with a backstop sweep so it cannot leak), and a per-class
`ResourceQuota` caps fleet storage with a fail-closed clear-error. **What remains:
homelab validation of 3b** (the real stuck-attach can't be triggered on
single-node k3d) **and the prod flip.** A live finding reframed 3b — the workspace
class is single-replica, so node-loss recovery is a *discard → fresh volume →
Gitea/checkpoint resume* fallback, not the originally-planned detach-wait. Flag is
default-off in-chart (zero behavior change on merge); **ON in k3d dev and on the
homelab soak** (ns `superhuman-remote-worker`, enabled in `a9485f21`).

| Phase | Scope | State |
|---|---|---|
| **0 — the flip** | PVC-back job workspaces (flag-gated, jobs-only) | ✅ done + k3d-verified |
| **1 — GC discipline** | terminal delete + give_up gating + backstop `reap_orphans` | ✅ done + k3d-verified |
| **2 — crash-recovery reattach** | workspace-lost job re-dispatches → reattaches PVC → agent resumes on the files | ✅ **COMPLETE + full-E2E-verified (2026-06-29)** — G1+G2 (wedge eliminated, bounded fail-loud, working-tree preserved) + **Option 1 stable-DNS Service** (commit `7fb9e9e2`) fixing the ephemeral-IP churn. **Full real-job E2E PASSED** (job `b4025433`): killed a running job's pod → re-dispatched via pod arm (`vm.requested` null) → new pod reattached PVC + same DNS → agent resumed from checkpoint, sentinel survived, job back to `processing` not failed. Minor non-blocking follow-up: ~2.7 min reconnect-loop delay before recovery triggers → `workspace_reattach_ephemeral_ip_reconnect_churn.md`. See §Phase 2 |
| **3a — capacity guard** | per-class `ResourceQuota` on the ephemeral storage class (caps total storage **and** PVC count) + fail-closed clear-error on 403 | ✅ **done + k3d-verified (2026-06-30)** — **universal** (every substrate); helm-configurable, default off. See §Phase 3a |
| **3b — node-loss fallback** | single-replica reattach-wedge → discard PVC → fresh volume → Gitea/checkpoint resume (extended reattach wait + kill-switch; triple-gated discard) | ✅ **BUILT + committed (`868bfd32`, 2026-06-30)** — live finding: `longhorn-ephemeral` is single-replica, so it's a fresh-volume fallback, **not** detach-wait. Homelab PVC soak now **ON** (`a9485f21`); real node-loss validation pending (single-node k3d can't trigger the stuck-attach — runbook `tests/workspace_pvc_node_loss_validation.md`). See §Phase 3b |
| **rollout** | flag default-off in-chart; **ON in k3d dev + homelab soak**; soak → prod flip | 🔄 **homelab soak in progress** (`a9485f21`) |

**Decision: LOCKED — Branch (a), scoped to job (worker/loop) pods for v1.** This
is the chosen fork of [`workspace_pvc_backed_migration.md`](workspace_pvc_backed_migration.md).
Sessions and VMs (branch b) are explicit non-goals for v1 (§Non-goals).

> **REVERSED for sessions (2026-08-04) — the v1 jobs-only scope no longer holds.**
> `WORKSPACE_PVC_ENABLED` now PVC-backs **sessions as well as jobs**. A session
> gets **two** claims: `pvc-ws-thread-<tid[:12]>` for its workspace pod
> (`container_provisioner`, the `owner.kind == "job"` gate removed) and
> `pvc-agent-s-<tid[:12]>` for its agent pod (`agent_provisioner`, same flag).
> Job agent pods stay emptyDir. The reclaim rule is deliberately **asymmetric**:
> a job PVC dies at terminal status, a session's dies **only when the `threads`
> row is hard-deleted** — an `ended` thread is resumable, so `end_thread`
> reclaims the volume only with `permanent=true`. The orphan reaper covers both.
> The reason sessions turned out to need this *more* than jobs, not less: a
> session's pod is idle-reaped while its thread stays resumable, so emptyDir was
> destroying the working tree of state a user could still reopen. This is a
> **code-behavior** note — it says nothing about which clusters have the flag on.
> Non-goals, the §Phase 0 gate line, and the mixed-fleet verification note below
> are annotated accordingly; VMs (branch b) remain a non-goal.

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
- ~~Sessions stay emptyDir (gated on `owner.kind == "job"`); one-line lift later.~~
  **Lifted 2026-08-04:** the `owner.kind == "job"` gate is gone — `_pvc_name_for`
  picks the prefix (`pvc-workspace` for jobs, `pvc-ws-thread` for sessions) and
  both kinds are PVC-backed under the same flag. Session *agent* pods get their
  own claim in `agent_provisioner` (`pvc-agent-s-<tid[:12]>`), also fail-closed.

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
  fail-closed before pod on PVC error. *(2026-08-04: the session-skip case was
  inverted when sessions were PVC-backed — sessions now assert a
  `pvc-ws-thread-*` claim, not its absence.)*
- `tests/test_lifecycle_workspace_manager.py` → `TestDeleteTerminalPvc` (5),
  `TestGiveUpTerminal` (1), `TestReapOrphans` (7, incl. the DB-error-≠-gone
  safety case).
- `tests/test_lifecycle_reconciler_reap.py` → 3 hook tests (called+recorded /
  absent-skipped / failure-tolerant).
- Fixed 2 pre-existing exact-stats-dict assertions for the new `orphans_reaped`
  key (`test_lifecycle_skeleton.py`, `test_lifecycle_agent_manager.py`).

### Files touched — Phase 0 + 1 (committed on `develop`)

> Phase 2 (Option 1) files: `container_provisioner.py` (headless Service) +
> `main.py` (dispatch/resume host injection), commit `7fb9e9e2`. Phase 3a files:
> `workspace-resourcequota.yaml` + `values.yaml` + `container_provisioner.py`
> 403 branch, commit `45eee2b1`. Phase 3b files: `container_provisioner.py`
> (extended reattach wait + `_pod_volume_attach_failing` + triple-gated fresh
> fallback) + `configmap.yaml`/`deployment.yaml`/`values.yaml` knobs, commit
> `868bfd32`; homelab soak enable (`values-experimental.yaml`) + node-loss runbook
> (`tests/workspace_pvc_node_loss_validation.md`), commit `a9485f21`.
> See §Phase 2 / §Phase 3a / §Phase 3b.

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
   kinds; zero PVCs leaked at the end. ✅ *(Historical: as of 2026-08-04 a
   session under the flag DOES get PVCs — two of them. The mixed-fleet property
   this checked still holds, but it is now "PVC and emptyDir pods coexist
   correctly", not "sessions are never PVC-backed".)*

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

## Phase 2 — crash-recovery reattach (COMPLETE + full-E2E-verified 2026-06-29)

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

**G1 + G2 + Option 1 are implemented, deployed to k3d, and E2E-tested — Phase 2
is COMPLETE.** G1+G2 eliminated the recovery wedge and preserved workspace data;
the remaining ephemeral-IP reconnect race was then fixed by **Option 1 (a stable
headless Service)**, and a full real-job E2E now **auto-resumes** end-to-end. The
record below is kept as the implementation history: the first E2E (job
`d65d93d3`) reached PARTIAL (data preserved, but fail-loud) and surfaced the
reconnect race; Option 1 closed it, proven by a second E2E (job `b4025433`).

| Aspect | Result |
|---|---|
| G1 routing — pod jobs recover via PVC reattach, never the VM arm | ✅ verified (`vm.requested` null every cycle) |
| G1 bounded cap → fail-loud (no forever-wedge) | ✅ verified (terminal `failed` at cap, clean `freeze_data`) |
| PVC survives pod kill + reattaches by name | ✅ verified (same PV across every recreate) |
| Working tree preserved (untracked sentinel survives) | ✅ verified (sentinel intact across recreates) |
| **Agent reconnects → G2 preserve branch runs → job resumes** | ✅ via **Option 1** stable headless Service (E2E `b4025433` resumed from checkpoint; the first E2E `d65d93d3` hit the now-fixed ephemeral-IP race) |

**Bug found + fixed by the E2E (would have shipped blind):** G1 first relied on
`delete_workspace` to set `workspace_container.status="deleted"`, but its
404/"already deleted" branch — the *always-taken* path, since the pod is already
gone — does **not** set the status. The stale `status="ready"` made
`_job_needs_sandbox` return False (`main.py:3157`), so the resume reused the dead
pod IP in an infinite loop (`recovery_attempts` 1/3 → 2/3 → 3/3 on the same dead
IP). **Fixed:** the handler now explicitly merges
`{"status":"deleted","pod_ip":None}` so the dispatcher recreates + reattaches.

**The blocker (FIXED via Option 1):** the recreate gave the pod a **new ephemeral
IP** each cycle (`.79 → .88 → .93 → .95` observed); the resuming agent dialed a
**stale IP** from a prior cycle → couldn't connect → reported
`workspace_unavailable` → another recovery → another new IP → churned to the cap
→ **fail-loud**. G2's preserve branch never got exercised (no agent ever
SSH-connected to the reattached pod). Filed + **resolved**:
[`workspace_reattach_ephemeral_ip_reconnect_churn.md`](../issues/workspace_reattach_ephemeral_ip_reconnect_churn.md).
**Fix (committed `7fb9e9e2`):** each PVC-backed workspace now gets a **stable
headless Service** (`workspace-<id>.<ns>.svc:30022`) the agent dials instead of
the ephemeral pod IP; the dispatch **and** resume paths inject this DNS `host`
(resume previously injected no container host at all — the real root cause).
Recreate → same address → the agent reconnects, G2's preserve branch runs, the
job resumes. **Verified:** full real-job E2E (job `b4025433`) auto-resumed from
the Postgres checkpoint with the un-pushed sentinel intact, job back to
`processing`, recovery cap untouched.

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
`api/persistent_session.py:346/421` — **sessions opted into PVCs on 2026-08-04,
so that mirror is now live work, not a conditional**). **Risk:** changes resume
behavior for *all* jobs, so it must land + be
tested with G1 in place via the real recovery E2E — not blind.

### Verification — full pass (2026-06-29, after Option 1)

- E2E (test-plan step 7) executed on k3d in two rounds. **Round 1** (job
  `d65d93d3`, pre-Option-1): pod-arm routing (not VM), recreate, PVC reattach
  (same PV), un-pushed working tree preserved (sentinel survived), bounded
  fail-loud all ✅; checkpoint **resume blocked** by the stale-ephemeral-IP race.
  **Round 2** (job `b4025433`, post-Option-1): the same mid-run pod kill now
  **auto-resumes** — the new pod reattaches the PVC + the same stable DNS, the
  agent reconnects, G2's preserve branch runs (no `rm -rf`), and the job resumes
  from the Postgres checkpoint with the sentinel intact, back to `processing`,
  recovery cap untouched. ✅
- Coherence: disk + Postgres checkpoint are both "as of the crash"; the disk may
  lag the checkpoint by ≤1 unfsynced super-step. With G2 (preserve, don't clobber)
  this is the same tolerance the agent already has mid-run — **no new coherence
  guard for v1**.

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
| **3b — single-replica node-loss → discard PVC → fresh-volume resume** (reframed from "detach-wait") | **only** multi-node + networked-RWO (Longhorn/Ceph/EBS) | ❌ no (single node = no failover target, hostPath = no detach dance) | ✅ built + committed (`868bfd32`); homelab soak ON, real node-loss validation pending |

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

### Phase 3b — single-replica node-loss fallback (BUILT + committed `868bfd32` 2026-06-30; homelab soak ON, real node-loss validation pending)

> **Live finding that reframed this (2026-06-30, on the homelab `main` cluster):**
> the workspace class `longhorn-ephemeral` is **`numberOfReplicas: 1`** (single
> replica, chosen deliberately to save space). So the original "detach-wait"
> premise — *"Longhorn replicas + auto-salvage mean the data survives a node loss;
> only the attach needs forcing"* — is **false for our config.** With one replica,
> a node loss = the volume's only replica is gone: a **transient outage**
> (reboot/maintenance, disk intact) makes it unavailable until the node returns; a
> **permanent loss** destroys the workspace data. There is no surviving replica to
> "detach-wait" for — so 3b is **not** detach-wait.

**The PVC is a fast-recovery cache, not the durability layer.** Durability is
already triple-layered: Postgres checkpoint (reasoning, replicated) + Gitea
(pushed code) + S3 snapshot (DR). The PVC exists to make the *common* failure — a
pod crash on a healthy node — reattach instantly (Phase 2). So single-replica is
the right default; node-loss recovery rides the layers we already have.

**What was built (the fallback, `container_provisioner.py` `create_workspace`):**
when a PVC **reattach** can't bring the pod ready within an extended window
(`WORKSPACE_REATTACH_READY_TIMEOUT`, default 180 s — long enough that a transient
node reboot reattaches with **no** data loss) **and** the holdup is confirmed to
be a volume-attach failure (`_pod_volume_attach_failing` — matches the K8s
`FailedAttachVolume`/`FailedMount`/Multi-Attach events, substrate-generic), the
provisioner **discards the wedged PVC and recreates a fresh empty one** under the
same deterministic name; the pod comes up and the agent **clones from Gitea +
resumes the Postgres checkpoint** onto it (the existing pre-G2 path — **no agent
or G1 edit**). Only *unpushed* working-tree files are lost (bounded; loop jobs
push often). The discard is **triple-gated** — reattach-only
(`_create_pvc` returned `"reused"`, never an initial create), volume-failure-
confirmed, and behind a kill-switch (`WORKSPACE_REATTACH_FRESH_FALLBACK`, default
on) — because it is the only data-destructive recovery path. It recurses once with
`fresh=True` (which creates a new volume, not a reattach), so it cannot re-fire.

**Why this beats detach-wait:** it's the *same* recovery a single-node /
local-path operator needs (they can't replicate either), so one path serves every
substrate. The S3 snapshot stays DR-only; reviving its (vestigial) restore path to
also recover *unpushed* files is a future enhancement, not required.

**Tests** (`tests/test_container_provisioner.py`): fresh-fallback happy path + the
three false-discard guards (volume-OK-not-discarded, initial-create-never-
discards, kill-switch-off) + the `_create_pvc` status and `_pod_volume_attach_failing`
contracts. 87 provisioner + 88 lifecycle pass; ruff + `helm lint` clean. Knobs
wired through configmap → orchestrator env → `values.yaml`
(`workspace.reattachReadyTimeout` / `workspace.freshFallback`).

**Homelab validation (deferred — needs a real node loss):** enable PVC mode in ns
`superhuman-remote-worker` (`WORKSPACE_PVC_ENABLED` is currently `false` there) and
ungracefully lose the node holding a job's workspace replica; assert the job
discards → fresh PVC → clones + resumes, and that a *fast* reboot (< timeout)
reattaches without discarding. Mechanism in hand (privileged `nsenter` pod +
self-recovering `systemd-run` timer; no SSH to the nodes). Single-node k3d cannot
trigger the real stuck-attach — the discard logic is unit-covered; the *trigger*
is homelab-only. **Runbook:**
[`tests/workspace_pvc_node_loss_validation.md`](../../tests/workspace_pvc_node_loss_validation.md)
(scenarios, the kubectl-only self-recovering node-outage mechanism, data-safety
gate, assertions). PVC mode is now **ON** on the homelab
(`values-experimental.yaml`, `workspace.pvcEnabled: true`) for this soak.

**HA alternative (deferred to a "proper deployment"):** bump `longhorn-ephemeral`
to ≥2 replicas → workspaces *survive* a single node loss (no fallback, no lost
unpushed work) at 2× workspace storage, multi-node only; then the classic
detach-wait becomes the relevant mechanism. Out of scope for v1 per the
cost/space tradeoff.

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
- ✅ **ON in k3d dev** (`values-local.yaml`), E2E gate steps 1-7 passed.
- ✅ Phase 0/1/2/3a/**3b** committed to `develop` (latest `a9485f21`; 3b code `868bfd32`).
- 🔄 **Homelab soak STARTED** (`a9485f21`) — PVC mode enabled in ns
  `superhuman-remote-worker`. Watch orphan-PVC WARN count + storage capacity, and
  run the 3b node-loss validation (runbook
  `tests/workspace_pvc_node_loss_validation.md`) — the real stuck-attach can only
  be triggered here, not on single-node k3d.
- ⏳ Prod flip — only after the homelab soak shows zero orphan growth across a full
  create→reap→GC cycle. **Mixed fleet is safe and is the rollout mechanism** (the
  reaper reads each pod's actual volume mode), so there is no big-bang cutover;
  rollback = flip the flag off (new pods revert to emptyDir, existing PVC pods
  finish and GC normally). No data migration either way.

---

## Test plan status

**Unit — ✅ done** (see Implementation record): create-flip on/off/session,
delete() terminal-vs-idle-vs-emptyDir, give_up gating, reap_orphans selection +
DB-error safety, reconciler hook.

**k3d E2E — steps 1-7 ✅ done** (see results above). Step 7 (Phase 2) **CLOSED
2026-06-29 after Option 1**:
7. **Recovery (Phase 2):** reproduced the `19707fa1` shape — kill the workspace
   pod mid-run → ✅ re-dispatch through the pod arm (not VM), ✅ PVC reattach (same
   PV), ✅ working tree preserved (sentinel survived), ✅ bounded fail-loud. Round 1
   (`d65d93d3`) exposed the stale-ephemeral-IP race; **Round 2 (`b4025433`, post-
   Option-1) ✅ auto-resumed** from the checkpoint (agent reconnects via the stable
   DNS → G2 preserve → `processing`). Step 7 closed.

**k3d E2E — Phase 3a (capacity guard) ✅ done 2026-06-30** (non-disruptive,
throwaway dedicated storage class): the rendered `ResourceQuota` enforces on
**both** count (`maxCount=1` → 2nd PVC `403`) and storage (`maxStorage=1Gi` → 2nd
1Gi PVC `403`); a WaitForFirstConsumer `Pending` PVC consumes quota at admission
(`used=1` while Pending — matches the create-PVC-then-pod order); the rejection
is the `403` `_create_pvc` catches → fail-closed + capacity log. Unit:
`test_pvc_quota_403_fails_closed_with_capacity_log`. 81 provisioner + 88
lifecycle tests pass; ruff + `helm lint` clean.

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
| Single-replica node loss wedges the RWO reattach | extended reattach wait → discard wedged PVC → fresh volume → Gitea/checkpoint resume (triple-gated + kill-switch) | Phase 3b ✅ built+committed (`868bfd32`); homelab node-loss validation pending |
| PVC bind latency on create | acceptable; only first-create, not reattach | observed fine on k3d |
| Disk↔checkpoint ≤1-step skew on hard kill | documented residual; existing re-clone/re-read gates tolerate it | Phase 2 verify; no new guard for v1 |
| Capacity exhaustion | per-class ResourceQuota (caps storage+count) + fail-closed + backstop reaper | Phase 3a ✅ (k3d-verified) |
| local-path has no replication / no hard size enforcement | S3 snapshot = DR; dedicated class + disk monitoring | documented (§Storage substrate matrix) |

## Non-goals (v1)

- ~~**Sessions** — stay emptyDir (brain in Postgres). One-line gate lift later.~~
  **No longer a non-goal (2026-08-04):** the gate was lifted. Sessions are
  PVC-backed under the same flag, two claims each (workspace pod + agent pod),
  reclaimed only on thread **deletion**. See the reversal note at the top.
- **VMs (branch b)** — different substrate (KubeVirt CDI DataVolume); separate
  effort. See [`workspace_pvc_backed_migration.md`](workspace_pvc_backed_migration.md) §Branch (b).
- **Removing the S3 snapshot path** — kept as cross-node/DR backstop.
- **Collapsing duplicated PVC plumbing** (`persistent_provisioner` vs
  `container_provisioner`) — ~~only relevant once sessions opt in~~ **now
  relevant (2026-08-04)**: three PVC-creating helpers coexist
  (`container_provisioner`, `agent_provisioner`, and the dormant
  `persistent_provisioner`, whose `_create_pvc` still omits the
  `srw.io/component=agent-workspace` label the reaper selects on). Still not
  scheduled; noted so the divergence is not mistaken for design.

## Coupling — keep these docs in lockstep

- [`loop_job_workspace_lost_wedged_in_recovery.md`](../issues/loop_job_workspace_lost_wedged_in_recovery.md)
  — **wedge-fix (Tier-1) is the Phase 2 prerequisite** (separate session). Record
  "PVC reattach" as the chosen Tier-2 direction there.
- [`snapshot_restore_dead_for_jobs.md`](../issues/snapshot_restore_dead_for_jobs.md)
  — same Tier-2 decision; with PVC reattach the never-restored job snapshot
  becomes DR-only (kept, not removed).
- [`workspace_pvc_backed_migration.md`](workspace_pvc_backed_migration.md) — the
  open-fork brief; Branch (a) is the decided direction, Phases 0/1/2/3a shipped
  per this doc.
