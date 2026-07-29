---
tags:
  - design
  - reconciliation
  - vm-lifecycle
  - workspace-lifecycle
  - persistence
  - snapshots
---

# Reconciliation — who owns "a VM workspace survives teardown"?

**Written 2026-07-29** after `docs/issues/vm_workspace_snapshot_unreachable_from_orchestrator.md`
found that VM workspace snapshots are never captured. Two existing designs both
claim part of this ground and **each assumes the other's part already works**.
Neither does. This doc reconciles them and recommends an order.

**Decision made 2026-07-29: the persistent rootdisk owns VM workspace survival,
sessions included.** The open capacity question below was answered — 2 TB SSD,
disks are ephemeral, and VM concurrency runs out of RAM long before disk — so
nothing forces sessions back onto delegated capture. Recommendation 1 is
implemented (`vm_persistent_rootdisk.md` Phases 0-2 + the session extension,
flag-gated OFF, live gate owed). Recommendation 2 remains unbuilt and is now
correctly off the critical path.

Recommendation 3's networking probe is **DONE — and it resolves in favour of
the design's own architecture.** See "Networking probe: answered" below.

## The two designs

| | `vm_snapshots_and_ide.md` | `vm_persistent_rootdisk.md` |
|---|---|---|
| Status | Partially implemented (Ph. 3-4 mostly done, Ph. 1 misbuilt — below) | **PROPOSED, not started** (2026-07-02) |
| Model | Tar the environment → S3; restore into a fresh VM | Rootdisk becomes a standalone DataVolume that **survives VM deletion** and reattaches by name |
| Solves | IDE for terminal/old jobs, archive, cross-VM portability | Crash recovery, resume-with-files, fast recreate (skips the ~3m27s clone) |
| Cost | Capture + restore pipeline, S3 storage, freeze window | 20 Gi per kept disk, GC discipline, tailnet identity wrinkle |

## Where each is wrong about the status quo

**`vm_snapshots_and_ide.md` — architecture and task list disagree, and the task
list won.**

Its architecture delegates capture to the VM Controller over NATS
(`vm.snapshot.capture`, lines 501/526), with orchestrator-side SSH explicitly
scoped to *"direct K8s mode (same-cluster without NATS)"* (line 506). But its
Phase 1 checklist says *"Add SSH tar capture to VM teardown flow in
`vm_provisioner.py`"* (line 541) — the orchestrator-side path. That is what got
built; `vm.snapshot.capture` exists nowhere in the codebase.

**SRW dev and prod are cross-cluster NATS deployments** (orchestrator on `main`,
VMs on `vm`, `vm.lifecycle.*` over a NATS leaf). So the one topology the
orchestrator-side path was scoped for is not the one it runs in. It cannot reach
`100.64.0.0/10`, declines every VM capture, and — until `ed26ebfa` — logged
success anyway.

**`vm_persistent_rootdisk.md` — two false premises, both about snapshots.**

- D5: *"S3 SSH-tar snapshots remain for what they're good at: pause/resume, idle
  suspension, terminal archive, and the reconciler's snapshot-before-reap. This
  feature removes their (never-working) role as a crash net, nothing else."*
  The doc is right that they never worked as a crash net, and wrong that they
  work at the graceful points — for **VM** targets they have never worked
  anywhere.
- Deferred/non-goals: *"Session/thread VMs: same mechanics would work
  (`release_thread_vm`), but **sessions already have working suspend/restore**."*
  They do not. VM-tier session suspend was blocked first by a tier misread
  (fixed, `6d66f7c4`) and now by exactly the unroutable capture above.

Both premises pointed the same way: "snapshots have the graceful paths covered,
so this feature only needs to cover crashes." With those premises corrected, the
persistent rootdisk covers materially more than its own doc claims.

## They are complementary, not competing

The overlap is narrower than it looks:

- **Rootdisk owns "the same job/session comes back to its files"** — crash
  recovery, resume, idle-suspend, pause. The disk simply persists; no capture,
  no restore, no freeze window, and it works for VMs already running.
- **Snapshots own "the files outlive the disk"** — IDE on a *terminal* job whose
  rootdisk was purged, long-term archive, and portability to a different VM.
  A persistent rootdisk cannot serve these: terminal purge (D4) deletes it by
  design, and that GC is load-bearing at 20 Gi apiece.

Note `vm_snapshots_and_ide.md` Phase 5 already anticipates the rootdisk work
("Migrate VMs from ephemeral containerDisk to persistent volumes", "use KubeVirt
`VirtualMachineSnapshot` CRD"). The persistent-rootdisk doc *is* that phase,
written up separately and in more detail. They were never really rivals — the
sequencing just never got recorded.

## Recommendation

**1. Persistent rootdisk first, and extend it to sessions.**

It is the cheaper fix for the data loss we actually hit, and it is already
designed to Phase-plan detail with a live verification harness. Critically, the
session extension its own doc defers as a *"one-line lift later"* is no longer
optional — it was deferred on the false premise that session suspend works.
With the disk persisting, VM session suspend stops needing a snapshot at all:
keep the disk, delete the VM, reattach on resume.

This also removes the freeze-and-tar window from the hot path, and makes
recovery *faster* than a fresh start (no golden clone).

**2. Delegated snapshot capture second, scoped to what only it can do.**

Build `vm.snapshot.capture` as the architecture already specifies — VM Controller
tars and uploads from inside the VM cluster — but scope it to terminal archive
and IDE-on-old-jobs, not to crash/suspend/resume. Smaller feature, clearer
justification, and it stops being on the critical path for data loss.

**Do not build my earlier suggestion** (in-VM daemon pushes its own snapshot).
It needs a golden-image change and carries version skew with running VMs; the
controller-side path the design already specifies needs neither.

**3. Verify one networking assumption before either.**

VMIs carry pod IPs on the VM cluster's pod network and the controller runs in
that same cluster. If KubeVirt's masquerade binding forwards port 22, the
controller can capture over the pod network with **no tailnet involvement at
all** — which is what "tar + upload from agent node" assumes. If it cannot,
delegated capture needs the controller on the tailnet, which is a different size
of job.

### Networking probe: ANSWERED 2026-07-29 — it can

From inside the running vm-controller pod on the dev VM cluster, against a live
VMI's pod IP:

```
10.42.225.106:22 REACHABLE  banner=b'SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.1'
```

KubeVirt's masquerade binding does forward port 22, and the controller reaches
it directly. **Delegated capture needs no tailnet and no orchestrator
involvement** — it is a contained change inside the VM cluster, exactly as
`vm_snapshots_and_ide.md` assumed when it wrote "tar + upload from agent node".
That removes the one thing that could have made the delegated-capture slice
expensive.

## What to do with the docs

- `vm_persistent_rootdisk.md` — correct the two false premises; promote the
  session extension from Deferred to in-scope, citing the suspend issue.
- `vm_snapshots_and_ide.md` — add a status section recording that Phase 1's
  capture was built orchestrator-side against an architecture that specifies
  controller-side, and is therefore dead for VM targets in every deployed
  topology; re-scope the feature away from crash/suspend/resume.
- `vm_workspace_snapshot_unreachable_from_orchestrator.md` — keeps the
  diagnosis; its Options list is superseded by this doc's ordering.

## Open question for the decision — ANSWERED

Is the 20 Gi-per-kept-disk cost acceptable for *sessions* on the current VM
cluster? **Yes.** 2 TB SSD, the volumes are ephemeral anyway, and concurrent VMs
run out of compute/RAM well before disk. So the ordering above stands, and two
things the design proposed for capacity pressure were deliberately not built:

- **The `ResourceQuota` capacity guard (D4).** Not the binding constraint.
- **The controller's orphan backstop is shipped OFF** rather than the proposed
  72h-on. It has no DB, so it cannot tell a leaked disk from the workspace of a
  session suspended over a long weekend — an on-by-default destructive sweep
  buys little here and can eat a live session. The knob exists for a deployment
  where the trade differs.

The kept-disk sweep that *did* ship covers **jobs only**, for the same class of
reason: a thread's terminal status is `ended`, which is also exactly the state a
suspended-but-resumable session sits in.

## Live gate owed

Everything is flag-gated OFF (`vmController.persistentRootdisk.enabled`), so
nothing changes until it is flipped. Order matters: **controller first, then
orchestrator** — the orchestrator's copy of the flag decides whether VM session
suspend may proceed without a snapshot, and turning that on against a controller
that still cascade-deletes disks would suspend sessions into nothing. The
reverse order is harmless.

1. Flip the controller flag; confirm a fresh VM job creates a standalone
   `agent-vm-<id>-rootdisk` DataVolume and the VM manifest has no
   `dataVolumeTemplates`.
2. Write a sentinel file, delete the VM object (mimicking the crash arm) →
   the job recovers, the controller logs `rootdisk reattach`, no clone runs,
   the sentinel is still there, and recovery is measurably faster than a fresh
   start.
3. Cancel it → VM, DV and PVC all gone; the golden is untouched.
4. Flip the orchestrator flag; suspend a VM session and resume it — the VM is
   deleted, the DV is not, and the workspace comes back with no S3 extract.

## Live-gate run log

**2026-07-29, attempt 1 — controller flag on, FAILED at step 1, rolled back.**

Flipping `vmController.persistentRootdisk.enabled: true` on the dev vm-cluster
(`f4fd08e2`) broke every VM create at the CDI admission webhook:

```
DataVolume "agent-vm-a43bfb73-...-rootdisk" is invalid:
  spec.source.pvc.namespace: Required value
```

A **templated** DataVolume may omit `spec.source.pvc.namespace` — CDI defaults
it from the owning VM. A **standalone** one may not. `_apply_clone_source`
omits it deliberately (same namespace → no cross-namespace clone RBAC) and that
reasoning still holds; the value simply has to be *stated* once the disk is
lifted out of the VM. Fixed in `daec8704`, normalised in `_ensure_rootdisk` so
the templated path stays byte-identical with the flag off.

**Why no unit test caught it, and what was done instead.** The controller's k8s
client is a `MagicMock` that accepts any body, so manifest *shape* is only ever
validated by a real API server. 149 controller tests passed against an invalid
manifest. `TestRootdiskCloneSourceNamespace` now pins the shape the API demands
— the next best thing to an integration test, and the general lesson for
anything else built against this mock.

**The flag was rolled back rather than left on while CI built** (`61ce7dbe`): a
real `developer` job was processing on a VM at the time, and a crash recovery in
that window would have tried to create a VM, hit the 422, and failed live work.
The dispatcher itself behaved correctly — it treats a 422 as fatal and fails the
job fast instead of looping (`Dispatcher: job ... VM parked ... — failing job`).

Cost of the window: one throwaway probe job. Nothing else affected.
