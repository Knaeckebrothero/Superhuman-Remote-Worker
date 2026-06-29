# Branch (a) — PVC-backed workspace pods: implementation plan

## Status

**Decision: LOCKED — Branch (a) chosen, scoped to job (worker/loop) pods for v1.**
This is the concrete implementation plan for the "pod PVC" fork of
[`workspace_pvc_backed_migration.md`](workspace_pvc_backed_migration.md). That
doc keeps all three forks open; this one commits to (a) and specifies the exact
wiring. Sessions and VMs (branch b) are explicit non-goals for v1 (see §10).

**One-line summary:** give each job workspace a PVC named after its job UUID,
mount it at `/home/agent-host`, reattach it by name on any pod recreate, and
delete it when the job reaches a terminal state. The Postgres LangGraph
checkpoint (cross-pod, already live) carries the reasoning state; the PVC carries
the files; on a crash the new pod reattaches the volume and resumes the
checkpoint — coherent by construction, no snapshot/restore dance.

## The mental-model correction that shapes the whole plan

The fear that "a volume randomly attaches to the wrong workspace" is **not a real
failure mode here, and never was.** Workspace pods are **standalone single pods**
created by the orchestrator (`restartPolicy: Never`), not ReplicaSet/Deployment
members. The PVC name is **deterministic and owner-keyed**:

- `WorkspaceOwner.pod_name` = `workspace-<job-uuid[:12]>` (`workspace_lifecycle.py:32-34`)
- `delete_workspace_pvc` already names PVCs `pvc-workspace-<id[:12]>` /
  `pvc-ws-thread-<id[:12]>` (`container_provisioner.py:356-358`)

The name **is** the binding. A fresh job gets a fresh UUID → a fresh PVC. A
recreated pod for the *same* job recomputes the *same* name → reattaches the
*same* volume. Two different jobs can never collide because they never share a
UUID. Cross-attach is impossible by construction; no "guard against wrong attach"
needs building.

**The actual problem that got PVCs removed in `c182aefb` (Workspace
Simplification) was orphan cleanup leaks** — PVCs/PVs surviving teardown. So the
guard we must build is not "prevent wrong attach," it is **"guarantee the PVC
dies when the job dies."** That reframes the entire effort around *GC discipline*,
not *attach safety*. Everything in §Phase 1 below is that guard.

## What already exists (dormant) — reuse, don't rebuild

The earlier passes left the PVC machinery in the tree, gated off. Verified
present in current `develop`:

| Capability | Location | State |
|---|---|---|
| RWO PVC create, idempotent 409-reuse, `labels` arg, `longhorn-ephemeral` (Delete-reclaim) | `container_provisioner._create_pvc` `:603-648` | ✅ built |
| PVC delete, idempotent 404 | `container_provisioner._delete_pvc` `:650-668` | ✅ built |
| Deterministic owner→PVC-name delete | `container_provisioner.delete_workspace_pvc` `:348-359` | ✅ built |
| Pod manifest PVC-vs-emptyDir branch on `pvc_name` | `container_provisioner._build_pod_manifest` `:1087-1096` | ✅ built |
| Graceful teardown = snapshot + delete pod + **delete PVC** | `container_provisioner.release_workspace` `:361-406` | ✅ built, wired into main.py terminal paths `:1457/:3514/:3549` |
| Reaper reads each pod's **actual** volume mode | `workspace_manager._pod_volume_is_ephemeral` `:66-80`, surfaced as `volume_ephemeral` in `list_instances` `:134` | ✅ built — **mixed fleet reconciles correctly** |
| Reaper terminal-action predicate ("PVC → recreate-keep-PVC") | `workspace_manager.is_state_ephemeral` `:259-267` | ✅ built |
| Reaper `give_up` PVC arm = delete pod + recreate against same PVC (reattach) | `workspace_manager.give_up` `:329-351` | ✅ built, "minimally activated" |
| Drift recovery: `ready` + dead pod → recreate | `workspace_lifecycle.ensure_workspace` `:108-118` + `container_provisioner.workspace_pod_live` `:450` | ✅ built — **becomes reattach-on-recreate for free** |
| Helm storage-class plumbing | `helm/templates/configmap.yaml:88` ← `workspace.ephemeralStorageClass`; `_storage_class` default `longhorn-ephemeral` `:76-77` | ✅ built |

**The single reason no job is PVC-backed today:** `create_workspace` calls
`_build_pod_manifest` **without** `pvc_name` (`:214-224`, comment `:212`
"emptyDir by default"). That one omission forces every pod to emptyDir, which
leaves every other dormant branch above unexercised.

## What's missing — the deltas to build

1. **The flip:** `create_workspace` must, when enabled for a job, compute the
   deterministic PVC name, create-or-reuse the PVC, and pass `pvc_name` into the
   manifest. (Phase 0)
2. **Terminal GC (the leak guard):** the **reconciler reap path** deletes the pod
   but not the PVC. `WorkspaceInstanceManager.delete()` (`:437-451`) →
   `delete_workspace` (`:299-346`) leaves the volume. A terminal reap of a
   PVC-backed job orphans the PVC. (Phase 1)
3. **Backstop PVC reaper:** an age/ownership sweep that deletes PVCs whose bound
   job is terminal-or-gone, for the cases the inline delete missed (pod already
   gone, delete failed, orchestrator restart mid-teardown). (Phase 1)
4. **Recovery coupling:** the `workspace_unavailable` handler must stop poisoning
   pod jobs into the VM arm so the recovered pod actually reaches the reattach
   path. This is the **Tier-1 fix already specified** in
   [`loop_job_workspace_lost_wedged_in_recovery.md`](../issues/loop_job_workspace_lost_wedged_in_recovery.md)
   — a hard prerequisite, not new work owned here. (Phase 2, depends on Tier-1)
5. **RWO dead-node robustness:** reattach needs the volume to detach from the old
   (possibly dead) node first. Add a bounded detach-wait + S3-restore fallback on
   a healthy node. (Phase 3)
6. **Capacity guard:** a `ResourceQuota` ceiling so a PVC leak or runaway can't
   exhaust Longhorn. (Phase 3)

## The flag

Follow the existing `os.environ` convention in `ContainerProvisioner.__init__`
(alongside `_storage_class` at `:76`):

```python
# Branch (a): PVC-backed job workspaces. Default off → emptyDir (today's
# behavior). Scoped to jobs in v1; sessions rehydrate from Postgres and stay
# emptyDir until a follow-on.
self._pvc_enabled: bool = _env_bool("WORKSPACE_PVC_ENABLED", default=False)
self._pvc_size: str = os.environ.get("WORKSPACE_PVC_SIZE", "10Gi")
```

Helm: add `workspace.pvcEnabled: false` + `workspace.pvcSize: "10Gi"` to
`values.yaml` (next to `ephemeralStorageClass` at `:953`), emit
`WORKSPACE_PVC_ENABLED` / `WORKSPACE_PVC_SIZE` in `configmap.yaml` (next to
`WORKSPACE_STORAGE_CLASS` at `:88`). `_storage_class` stays `longhorn-ephemeral`
(Delete reclaim) — **do not** point per-workspace PVCs at a Retain class; Delete
reclaim is what prevents the orphan-PV class even if a PVC delete is missed.

Scope decision: v1 gates on `owner.kind == "job"`. Sessions keep emptyDir (their
"brain" is in Postgres; marginal benefit is files-only and lower-urgency). The
gate is one `and owner.kind == "job"` — trivial to lift to sessions later behind
the same flag.

## Phase 0 — The flip (create path) · ~30 lines · the whole user-visible win

In `container_provisioner.create_workspace` (`:172-297`), between the seed-config
block (`:208-210`) and the `_build_pod_manifest` call (`:214`):

```python
pvc_name = None
if self._pvc_enabled and owner.kind == "job":
    pvc_name = f"pvc-workspace-{owner.id[:12]}"   # same key delete_workspace_pvc uses
    ok = await self._create_pvc(
        pvc_name,
        size=self._pvc_size,
        labels={owner.label_key: owner.id, "srw.io/component": "agent-workspace"},
    )
    if not ok:
        await self._set_context(owner, {"status": "failed", "error": "PVC create failed"})
        return False
```

…then pass `pvc_name=pvc_name` into the existing `_build_pod_manifest(...)` call
(`:214-224`). The manifest branch at `:1087-1096` already does the rest.

**Why this is nearly the entire feature:** with the PVC named by job UUID and
`_create_pvc` idempotent (409-reuse), **every recreate reattaches automatically**:
- `ensure_workspace` drift path (`ready` + dead pod → `_create`, `:116-117`) →
  `create_workspace` → same `pvc_name` → 409 reuse → **files reattach**.
- Blank `_create` path (`deleted`/`None` status, `:94-96`) → same.
- Reaper `give_up` PVC arm (`:347-349`) → `create_workspace(owner)` → same.

No new resume logic is needed for the happy path — deterministic naming + 409
reuse *is* the resume logic. Stamp the owner label on the PVC (above) so the
backstop reaper (Phase 1) and any human can resolve ownership; it doubles as a
belt-and-suspenders identity check before any reattach.

## Phase 1 — GC discipline (the actual guard) · the leak fix

### 1a. Inline terminal delete

`WorkspaceInstanceManager.delete()` (`workspace_manager.py:437-451`) must also
delete the PVC **when the bound work is terminal** — and only then:

```python
async def delete(self, inst, grace_s):
    ...
    owner = WorkspaceOwner.session(bound) if "srw/thread-id" in labels else WorkspaceOwner.job(bound)
    await self._provisioner.delete_workspace(owner)
    if self._is_terminal(inst) and not inst.metadata.get("volume_ephemeral", True):
        await self._provisioner.delete_workspace_pvc(owner)   # idempotent 404
```

Gating on `_is_terminal` (`:220-229`, = completed/failed/cancelled job, ended
thread) is the crux:
- **Terminal reap** → delete pod **and** PVC (work is done; reclaim storage).
- **Idle/suspend reap** (paused/pending_review/reviewing) → delete pod, **keep
  PVC** (the job will re-dispatch and reattach).
- **`give_up` reattach** → keep PVC (it recreates against it).

### 1b. Fix the `give_up` × terminal interaction

`give_up` (`:329-351`) currently recreates a pod for any non-ephemeral instance.
For a *terminal* instance that's pointless (and would race 1a's PVC delete). Gate
the recreate on non-terminal:

```python
await self.delete(inst, grace_s)          # 1a deletes the PVC if terminal
if not self._is_terminal(inst) and not inst.metadata.get("volume_ephemeral", True):
    await self._provisioner.create_workspace(owner)   # idle recovery only — reattach
```

### 1c. Backstop PVC reaper

A periodic sweep (extend the existing lifecycle reconciler tick, or a sibling to
the workspace idle sweeper) that lists PVCs by label and deletes orphans:

```
list PVCs where srw.io/component=agent-workspace
for each: owner = label srw/job-id  → fetch job
          delete PVC if: job row gone, OR job status in {completed, failed, cancelled}
                         AND no live pod named workspace-<id[:12]>
```

This catches: pod deleted before the inline path ran, inline delete failed, or
the orchestrator restarted mid-teardown. With `reclaimPolicy=Delete` the
underlying PV (and Longhorn replicas) vanish with the PVC. Log a WARN with the
orphan count each sweep so log-based alerting can fire if GC ever regresses.

**Net of Phase 1:** a PVC exists exactly as long as its job is non-terminal, with
two independent reclaimers (inline + backstop) and Delete-reclaim as the floor.
That is the leak guard the 2026-04 simplification asked for, made explicit.

## Phase 2 — Wire PVC reattach into crash recovery (depends on Tier-1)

A PVC alone does **not** fix the recovery *wedge* — that's a control-flow bug.
Today the `workspace_unavailable` handler (`main.py:10118-10161`) stamps
`vm.requested=True` on a **pod** job, routing it through the VM arm so it never
reaches `ensure_workspace` / the pod reattach path (full trace in
[`loop_job_workspace_lost_wedged_in_recovery.md`](../issues/loop_job_workspace_lost_wedged_in_recovery.md)).

**Prerequisite (owned by that issue's Tier-1, lands first, independent of PVCs):**
branch on backend before stamping; for pod jobs re-dispatch through the pod arm;
bounded retries → fail-loud terminal; clear `recovering` on the pod path.

**What this plan adds once Tier-1 is in:** nothing new in code — the PVC makes the
already-correct pod recovery path *non-blank*. After Tier-1, a workspace-lost pod
job re-dispatches through `ensure_workspace`, which recreates the pod, which
reattaches the PVC. This **supersedes the Tier-2 fork** in both recovery docs:

- Replaces **Option A (snapshot-restore)** — no S3 tar/extract on the hot path;
  reattach is a volume mount.
- Replaces **Option B (blank + checkpoint)** — the workspace is *not* blank; the
  files are on the PVC.

**The coherence question the snapshot path forced — dissolved.** Option A had to
reconcile "snapshot taken at phase-boundary may be older than the checkpoint."
With reattach, the disk reflects on-disk state at the instant the pod died, and
the Postgres checkpoint reflects the last completed super-step — both "as of the
crash." They are strictly *more* coherent than any periodic snapshot. Residual
(call it out, don't over-engineer): disk and PG checkpoint are two stores, so a
tool write that didn't fsync before a hard kill can lag the checkpoint by one
step — the same tolerance the agent already has mid-run (it re-reads / re-clones).
Keep the existing re-clone gates; no new coherence guard required for v1.

Keep the S3 snapshot as the **cross-node / DR** layer (see Phase 3) — PVC is the
fast same-cluster path, snapshot is the backstop. "Both wins," per the brief.

## Phase 3 — Robustness & capacity

- **RWO dead-node detach:** when the old pod's node is dead, K8s holds a stale
  `VolumeAttachment` and the new pod's mount blocks. Add a bounded wait in the
  recreate path; on timeout, fall back to S3-restore onto a healthy node (Longhorn
  `auto-salvage` + the 2nd replica means the *data* survives a node loss; only the
  *attach* needs forcing). This is the one place "just reattach" isn't literally
  instant — node-alive pod-restart is clean; node-death needs this fallback.
- **Capacity guard:** a `ResourceQuota` on `requests.storage` (~1 Ti) in the
  workspace namespace — a runaway/leak ceiling well under the ~4.2 TB fleet
  (10Gi × 2 replicas ≈ 210 concurrent before exhaustion; see brief §Cluster
  facts). Pairs with the backstop reaper.

## Identity & labels (the user's explicit "proper guard")

Deterministic naming already prevents cross-attach. The *added* identity surface
is for **GC safety and observability**, not attach safety:

- PVC carries `srw/job-id: <uuid>` + `srw.io/component: agent-workspace` (Phase 0).
- Pod already carries `srw/job-id` (`_build_workspace_labels` `:670-683`).
- Backstop reaper resolves PVC→owner→job-status via the label (Phase 1c).
- Optional belt-and-suspenders: before a `give_up` reattach, assert the PVC's
  `srw/job-id` label equals the owner id. With UUID naming this can never
  mismatch; it's a cheap invariant that turns a "can't happen" into a logged
  assertion.

## Test plan

**Unit (pytest, `tests/` — uses `FilesystemTestBackend`, mock k8s):**
- `create_workspace` flag-on + `owner.kind=job` → `_create_pvc` called, `pvc_name`
  threaded to `_build_pod_manifest`; flag-off → emptyDir, no `_create_pvc`
  (assert today's behavior preserved).
- `create_workspace` flag-on + `owner.kind=session` → emptyDir (v1 scope).
- `WorkspaceInstanceManager.delete()`: terminal + PVC-backed → `delete_workspace_pvc`
  called; idle + PVC-backed → not called; terminal + emptyDir → not called.
- `give_up`: terminal → no recreate; idle + PVC → recreate; idle + emptyDir → no
  recreate.
- backstop reaper: selects only terminal/gone-owner PVCs; skips PVCs with a live
  pod / non-terminal job.

**k3d e2e (the gate — `Plan → Develop → Verify`):**
1. Flip `workspace.pvcEnabled=true` in `values-local.yaml`, `helm upgrade`.
2. Create a job → assert `pvc-workspace-<id>` `Bound`, pod mounts it at
   `/home/agent-host`.
3. **Reattach:** write a sentinel file in `/home/agent-host`, `kubectl delete pod
   workspace-<id>` (simulate crash, node alive) → `ensure_workspace` drift-recreates
   → assert sentinel survives on the new pod.
4. **Terminal GC:** cancel/complete the job → assert PVC deleted (no orphan),
   PV/Longhorn replicas gone (Delete reclaim).
5. **Backstop:** delete a pod out-of-band + mark its job cancelled so the inline
   path is skipped → assert the backstop sweep deletes the PVC.
6. **Mixed fleet:** with one emptyDir job pod (flag was off) and one PVC job pod
   coexisting, run the reaper → assert emptyDir pod gets delete-tombstone, PVC pod
   gets reattach/GC per status (proves `_pod_volume_is_ephemeral` reconciles both).
7. **Recovery (after Tier-1):** reproduce the `19707fa1` shape — kill the workspace
   pod mid-run → assert the job re-dispatches through the pod arm (not VM),
   reattaches the PVC, resumes from checkpoint, files intact.

## Rollout

- Ship flag **default off** — zero behavior change on merge.
- Flip on in `values-local` (k3d) → run the e2e gate above.
- Flip on `develop`/dev → soak; watch orphan-PVC WARN count + Longhorn capacity.
- **Mixed fleet is safe and is the rollout mechanism** — the reaper reads each
  pod's actual volume mode, so there is no big-bang cutover. New pods are PVC,
  in-flight emptyDir pods drain naturally.
- **Rollback** = flip the flag off. New pods revert to emptyDir; existing PVC pods
  finish and GC normally. No data migration either direction.
- Prod flip only after dev soak shows zero orphan growth across a full
  create→reap→GC cycle.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Orphan PVC/PV leak (the 2026-04 regression) | Delete reclaim + inline terminal delete + backstop reaper + WARN-on-orphan alerting + ResourceQuota ceiling |
| RWO mount blocks on dead-node stale VolumeAttachment | Bounded detach-wait + S3-restore fallback on healthy node (Phase 3); S3 snapshot retained as DR |
| PVC bind latency on create (seconds) | Acceptable; Longhorn binds fast; only on first-create, not reattach. Measure in e2e step 2 |
| Disk↔checkpoint single-step skew on hard kill | Documented residual; existing re-clone/re-read gates tolerate it; strictly better than snapshot. Not a v1 blocker |
| Longhorn capacity exhaustion | ResourceQuota (~1 Ti) + backstop reaper keeps the working set bounded |

## Non-goals (v1)

- **Sessions** — stay emptyDir (brain in Postgres). One-line gate lift later.
- **VMs (branch b)** — different substrate (KubeVirt CDI DataVolume); separate
  effort. See [`workspace_pvc_backed_migration.md`](workspace_pvc_backed_migration.md) §Branch (b).
- **Removing the S3 snapshot path** — kept as cross-node/DR backstop, not removed.
- **Collapsing the duplicated PVC plumbing** in `persistent_provisioner` vs
  `container_provisioner` — only relevant once sessions opt in.

## Coupling — keep these docs in lockstep

- [`loop_job_workspace_lost_wedged_in_recovery.md`](../issues/loop_job_workspace_lost_wedged_in_recovery.md)
  — **Tier-1 is a prerequisite** (Phase 2). Record "PVC reattach" as the chosen
  Tier-2 direction there, superseding its Option A/B fork.
- [`snapshot_restore_dead_for_jobs.md`](../issues/snapshot_restore_dead_for_jobs.md)
  — same Tier-2 decision. With PVC reattach as the recovery path, the
  never-restored job snapshot becomes **DR-only**; record that its Option A/B is
  resolved by this plan (and that job snapshots stay as the cross-node backstop,
  not removed).
- [`workspace_pvc_backed_migration.md`](workspace_pvc_backed_migration.md) — the
  open-fork brief; mark Branch (a) as the decided direction pointing here.
