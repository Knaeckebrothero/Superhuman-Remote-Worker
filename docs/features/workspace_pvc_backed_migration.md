# Workspace Storage Durability (PVC / VM-disk / snapshot)

## Status

Design brief — **substantially revised 2026-06-10** after verifying the live
architecture against code and the dev cluster. The original framing ("migrate
workspace pods to PVCs") was too narrow and rested on a wrong assumption (that
persistent threads already had per-workspace PVCs while jobs didn't). This
revision corrects the architecture, sharpens the *actual* problem, and lays out
the three-way fork for fixing it.

**No implementation direction is locked.** The strategic choice — pod PVCs vs.
VM controller-side disks vs. hardening the snapshot path — is still open. The
verified pod-PVC design from the earlier pass is preserved below as *one branch*,
not the decided plan.

It is the deferred follow-up carved out of the workspace-reaper work
([`docs/superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md`](../superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md))
and builds on the ephemeral-workspace snapshot model
([`ephemeral_workspaces.md`](ephemeral_workspaces.md)).

**Where we are (2026-06-10):** architecture verified and corrected; the fork is
open. The user's stated goal is **durable VMs** → branch (b). The immediate next
action is *discovery*, not design: confirm which VM backend the deployment uses
and whether its disk survives `delete_vm` today (the direct-K8s path is already
KubeVirt/CDI-DataVolume-backed — a strong lead; see branch (b)).

## TL;DR of the correction

- There is **no per-workspace PersistentVolume in the active path** — not for
  jobs, not for sessions. Workspace storage is `emptyDir` (container backend) or
  a fresh per-create disk (VM backend), plus an S3 snapshot.
- The `pvc-persistent-*` PVC that looked like "sessions have PVCs" is
  **vestigial** — a dormant *fallback* provisioner left over from before session
  state moved to Postgres. It is not exercised on current clusters.
- The durable session state (conversation, plan, context) lives **in Postgres**
  and survives any pod/VM churn. The only thing actually lost on a teardown that
  can't snapshot is the **working files** in `/home/agent-host`.
- **VMs use the same ephemeral + S3-snapshot model as pods.** A durable VM disk
  is a controller-side capability the orchestrator does not use today — not an
  impossibility, just unbuilt.

## Verified architecture (2026-06-10)

### Two pods per agent

Every agent run is a **harness pod** (runs the LangGraph process) plus an
**execution target** it reaches over SSH. They are distinct:

| Backend | Harness (LangGraph) | Execution target (the "workspace") | Both durable? |
|---|---|---|---|
| Container — **job** | `srw-agent-j-<id>` pod, emptyDir `/workspace` (`agent_provisioner`) | `workspace-<id>` pod, **emptyDir** `/home/agent-host` (`container_provisioner`) | ❌ emptyDir |
| Container — **session** | `srw-agent-s-<id>` pod, emptyDir `/workspace` (`agent_provisioner`) | `ws-thread-<id>` pod, **emptyDir** `/home/agent-host` (`container_provisioner`) | ❌ emptyDir |
| **VM** — job or session | `srw-agent-*-<id>` pod, emptyDir `/workspace` | the VM, **fresh disk** per create, SSH `:22` | ❌ fresh disk |

The execution pod is created by the *same* `container_provisioner.create_workspace()`
for jobs and sessions — it has no idea who's driving it. Its volume is `emptyDir`
in both cases (`container_provisioner.py` `_build_pod_manifest`, the
`pvc_name`-gated branch defaults to emptyDir at ~line 1080).

### Why the `pvc-persistent-*` PVC is vestigial

`persistent_provisioner.py` creates a `persistent-<id>` harness pod backed by
`pvc-persistent-<id>` (PVC at `/workspace`, lines 166–187). But it is a
**fallback**: the dispatch/resume paths try `agent_provisioner` first and only
fall back to `persistent_provisioner` when `agent_provisioner.is_available` is
False (`main.py:1927`, `1968`, `12526`). The dev cluster runs `agent_provisioner`
(the live `srw-agent-j-*` pods prove it) and has **zero** `persistent-*` pods and
**zero** `pvc-persistent-*` PVCs. So that PVC path is dead here — a remnant of an
earlier design, not current behavior.

### State & durability layers (what survives what)

| Layer | Where it lives | Durable across pod/VM teardown? |
|---|---|---|
| Conversation / thread / context (the "brain") | **Postgres** (orchestrator) | ✅ yes — independent of pod/VM |
| LangGraph checkpoint (`AsyncSqliteSaver`, `workspace/checkpoints/<id>.db`) | harness pod `/workspace` (emptyDir) | ❌ no — but it's a **live-process cache**, not the resume source anymore |
| Working files (repos, edits, shell state) | execution pod `/home/agent-host` | ❌ only via **S3 snapshot** (SSH `tar`+`zstd`) |

Two consequences worth internalizing:

- The SQLite checkpoint was the *original* reason sessions had a PVC (persist
  graph state across harness restarts). Once the durable thread state moved to
  **Postgres**, the checkpoint was demoted to an in-process cache and the
  active path correctly dropped the PVC. Sessions on emptyDir harness pods
  resume fine because they rehydrate from Postgres (`persistent_session.py:171`,
  `get_thread()`), not from the `.db`.
- The S3 snapshot tars **`/home/agent-host`** on the *execution* pod
  (`snapshot_service.py:~285`) — it does **not** include the harness pod's
  checkpoint. So the snapshot protects working files, nothing else.

### VMs

VM workspaces follow the identical model: `create_vm` → fresh disk; `delete_vm`
→ destroyed; state preserved only by the same S3 snapshot-over-SSH. The reaper's
VM `give_up` is force-delete with **no disk reattach** by design
(`vm_manager.py` give_up: "VMs have no volume-reattach … disks live behind the
external VM controller"). The orchestrator sends clean create/delete over NATS
and carries no "keep disk"/"reattach disk" capability. **But the disk substrate
is partly known:** the direct-K8s VM path provisions a **KubeVirt CDI
DataVolume** (`vm_provisioner.py:~242`; `VM_STORAGE_CLASS` default `local-path`,
`VM_DISK_SIZE` 20Gi) — so VM disks *are* PVC-backed, just on a node-local,
Delete-reclaim class today. The NATS-bridge path delegates to the external
controller, whose tech is unverified. So persistent VM disks are likely a
reclaim + reattach **policy** problem on an existing CDI/DataVolume base, not a
from-scratch capability (see branch (b)).

## Why emptyDir replaced PVC (recovered — the original decision #1)

PVCs were not an oversight to "switch back on" — they were deliberately removed
in commit `c182aefb` (2026-04-08), the **Workspace Simplification** project
([`workspace_simplification.md`](workspace_simplification.md)). The reasons, from
that doc's own emptyDir-vs-PVC comparison and bug list:

- **Cleanup burden / leaks:** "PVCs … persist across pod restarts, requiring
  explicit cleanup that **frequently fails or is incomplete**" — i.e. the PV/PVC
  leak class. This is exactly what branch (a) must *not* reintroduce (hence
  Delete-reclaim + a backstop PVC-reaper).
- **Provisioning:** PVC binding adds seconds–minutes and a startup failure mode
  (`"PVC creation failed" → return False`); emptyDir is instant.
- **Scheduling:** RWO pins the pod to a node (generic-storage framing — Longhorn
  relaxes this; see branch (a)).
- **Philosophy:** "the container IS the isolation boundary"; matches CI/CD
  runners (GitHub ARC, GitLab/Tekton K8s executors). State persistence was
  *intentionally* moved to S3 snapshot + Gitea push + Nextcloud sync.

The same doc blessed one narrow exception: **"Reserve PVCs only for persistent
threads that must survive pod restarts."** So re-introducing PVCs re-litigates a
deliberate trade-off; the bar is clearing the cleanup-leak problem that motivated
the switch.

## The sharpened problem

Because the brain is in Postgres, **the only state at risk on teardown is the
working tree** in `/home/agent-host`. The failure mode is *shared* across all
three substrates (job pod, session pod, VM) and identical:

> If the compute is **unreachable or dead** when teardown fires, the SSH snapshot
> can't be taken, and the working files are lost on force-delete. The reaper
> already emits a data-loss WARN in exactly this case.

This narrows the blast radius from how it first looked: a force-delete loses an
uncommitted working tree, **not** the conversation, plan, or agent state. That
matters for deciding how much the fix is worth.

## The fork — three ways to fix it (OPEN decision)

| Option | Covers | Cost / where | Limit |
|---|---|---|---|
| **(a) Pod PVC** | container pods only | entirely in this repo | doesn't help VMs |
| **(b) VM controller-side disk** | VMs only | **external/unmanaged VM controller** + a NATS reattach request | biggest lift, cross-boundary, depends on controller tech |
| **(c) Harden snapshot path** | pods *and* VMs | one change in this repo | can't recover dead/unreachable compute — SSH-snapshot needs a *live* target, so it lowers frequency, not the floor |

Key trade-off: **persistent storage is two separate builds** (a for pods, b for
VMs, because they're different substrates), while **snapshot-hardening is one
build but only partial**. If the goal is specifically *durable VMs* (the original
motivation), only (b) delivers it — and it's the heaviest. If the goal is "stop
losing working trees anywhere for the least work," (c) is the one-shot.

## Branch (a): pod-PVC design — verified, if chosen

This is the design from the earlier pass, kept intact. It targets the
**execution pod** (`/home/agent-host`) for jobs and sessions.

**Already plumbed (dormant), needs flipping on:**
- `_build_pod_manifest` already branches PVC-vs-emptyDir on `pvc_name`.
- `_create_pvc` (RWO, idempotent 409-reuse) / `_delete_pvc` (idempotent 404).
- `delete_workspace_pvc(owner)` with deterministic names
  `pvc-workspace-<id>` / `pvc-ws-thread-<id>`.
- Reaper `is_state_ephemeral` + `_pod_volume_is_ephemeral` read each pod's volume
  mode → a mixed emptyDir/PVC fleet reconciles correctly during cutover.

**Net-new work:**
1. Flip `create_workspace` to PVC mode (create-or-reuse PVC, pass `pvc_name`),
   flag-gated. This also un-breaks the reaper's dormant `give_up` PVC arm for
   free (it recreates via `create_workspace`).
2. PVC GC: explicit delete on *terminal* bound-work (job completed/failed/
   cancelled; thread **deleted**, not merely *ended*) + a backstop PVC-reaper arm
   for orphans. Snapshot-confirmed before delete.
3. Resume prefers PVC-reattach, S3-restore fallback (the fast-resume win).
4. Capacity guard (ResourceQuota).
5. `give_up` attach-wait robustness (RWO detach-before-reattach; dead-node falls
   back to snapshot-restore on a healthy node via Longhorn's 2nd replica).

**Settled decisions (conditional on choosing branch a):**
- **Access mode: RWO.** Longhorn (4 nodes, 2 replicas, hard anti-affinity,
  `auto-salvage`) reattaches an RWO volume on whichever node the recreated pod
  lands. No node affinity, no RWX/share-manager overhead.
- **Reclaim: Delete + lifecycle guard + backstop reaper.** `reclaimPolicy=Delete`
  (reuse `longhorn-ephemeral`) so GC leaves no orphan PVs; safety lives in the
  tested deletion guard (terminal + snapshot-confirmed + no active pod) plus the
  retained S3 snapshot as DR. Avoids reintroducing the orphan-PV leak class.
- **Both wins:** keep S3 snapshot as the cross-node/DR layer; PVC is the fast
  same-cluster resume path. They back each other up.

## Branch (b): VM controller-side disk — sketch (if chosen)

Delivers the user's original goal (durable VMs). More tractable than "unknown
external controller" first implied — the **direct-K8s VM path already uses
KubeVirt CDI DataVolumes** (`vm_provisioner.py:~242`), so the disk is already a
PVC; it's just on `local-path` (node-local, Delete) today. The work:

- **Storage:** point the DataVolume at a Longhorn (networked, reattachable) class
  instead of `local-path`, sized via `VM_DISK_SIZE`.
- **Lifecycle:** a "keep DataVolume on `delete_vm`" + "bind existing DataVolume on
  `create_vm`" policy; extend the NATS `vm.lifecycle.create` payload with a
  disk-identity so a recreate reattaches instead of provisioning fresh.
- **GC:** mirror the pod-PVC discipline so VM DataVolumes don't leak.

**Discovery first** (before any of the above): which VM backend does the target
deployment actually use — direct-K8s (KubeVirt/CDI, the lead above) or the
NATS-bridge external controller (tech unverified)? And does the DataVolume
survive `delete_vm` today, or get GC'd with the VM? That answer sets the shape of
the whole branch.

## Branch (c): harden the snapshot path (if chosen)

Keep emptyDir / fresh-disk; make the *existing* S3 snapshot fail less often so
"snapshot impossible" stays rare. Levers: better reachability handling + retry on
the reap path; and optionally **Longhorn-native volume backup** to S3 (block
level, no SSH) for the pod substrate, which removes the "snapshot needs a live
SSH target" dependency at the storage layer. One build, helps pods and VMs.

Inherent limit: anything SSH/tar-based still can't capture **dead or unreachable**
compute — only a storage-layer backup (Longhorn for pods; CDI/controller for VMs)
escapes that, and only where the substrate supports it. So (c) lowers the
frequency of loss but, on its own, can't drive it to zero. Cheapest partial fix
and a sensible stopgap, not a full durability guarantee.

## Cluster facts (verified 2026-06-10)

- Storage classes: `longhorn` (Retain, Immediate), `longhorn-ephemeral`
  (Delete — current `WORKSPACE_STORAGE_CLASS`), `longhorn-static` (Delete).
- 4 nodes (k3s); Longhorn `default-replica-count=2`,
  `replica-soft-anti-affinity=false`, `auto-salvage=true`,
  `node-down-pod-deletion-policy=do-nothing`.
- Capacity: ~4.2 TB available across the fleet; at 10Gi × 2 replicas = 20Gi raw
  per workspace → ~210 concurrent before exhaustion. A ResourceQuota (~1 Ti)
  gives a runaway ceiling well under that.
- Helm stubs exist: `workspace.storageClass: ""`, `storageSize: "10Gi"`.
- The shared `srw-workspace` PVC (20Gi RWX) is **agent-pod local scratch**
  (`/workspace`), unrelated to per-workspace storage. No per-workspace PVCs
  exist on the cluster today.

## Out of scope / deferred

- **Automated DB-orphan enumeration** in the reconciler (`list_instances`
  surfacing rows whose pod/PVC is gone) — its own follow-up.
- Collapsing the duplicated PVC plumbing across `persistent_provisioner` and
  `container_provisioner` into a shared helper — worth doing *if* branch (a) is
  chosen, since it activates a second live copy.

## References

- [`docs/superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md`](../superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md)
  — the reaper; `is_state_ephemeral` + `give_up` PVC arm
- [`ephemeral_workspaces.md`](ephemeral_workspaces.md) — S3 snapshot/restore model
- [`workspace_simplification.md`](workspace_simplification.md) — the emptyDir switch (commit `c182aefb`) + its rationale
- `orchestrator/services/container_provisioner.py` — execution pod;
  `_build_pod_manifest` (emptyDir vs PVC), `_create_pvc`, `delete_workspace_pvc`
- `orchestrator/services/agent_provisioner.py` — active harness pod (emptyDir)
- `orchestrator/services/persistent_provisioner.py` — **vestigial** fallback
  harness pod (`pvc-persistent-*`)
- `orchestrator/services/lifecycle/workspace_manager.py` / `vm_manager.py` —
  reaper predicates; VM `give_up` (no disk reattach)
- `src/agent.py` — `AsyncSqliteSaver` checkpoint (`workspace/checkpoints/<id>.db`)
- `src/api/persistent_session.py` — session rehydrates from Postgres (`get_thread`)
- `orchestrator/services/snapshot_service.py` — snapshot tars `/home/agent-host`
- `orchestrator/services/vm_provisioner.py` — VM lifecycle; KubeVirt CDI DataVolume (`VM_STORAGE_CLASS` / `VM_DISK_SIZE`)
- `helm/values.yaml` — `workspace.storageClass` / `storageSize`
