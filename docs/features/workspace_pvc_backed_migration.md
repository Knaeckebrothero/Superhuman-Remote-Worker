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
and carries no "keep disk"/"reattach disk" capability. Whether the *external*
controller (KubeVirt/Harvester/other) can persist disks is unknown from this
repo.

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

Out of this repo's direct reach; requires:
- Controller-side: "keep disk on VM delete" + "attach existing disk on VM
  create" (feasible iff the controller is KubeVirt/Harvester-style with
  DataVolume/PVC-backed VMs; **verify the controller tech first**).
- Orchestrator-side: extend the NATS `vm.lifecycle.create` payload with a
  disk-identity / reattach request; stop treating every create as fresh-disk.
- Mirror the pod-PVC GC discipline so VM disks don't leak (the unmanaged
  controller makes orphan cleanup harder, not easier).

This is the branch that delivers the user's original goal (durable VMs). It is
the largest and riskiest, and its first task is **discovery** (what does the VM
controller actually support).

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
- `helm/values.yaml` — `workspace.storageClass` / `storageSize`
