# Workspace teardown reaper — reconciler-owned, clean/dirty-gated

## Status

Design — 2026-06-04. Approved in brainstorming; not yet implemented. Builds on
[`unified_instance_lifecycle.md`](../../features/unified_instance_lifecycle.md)
(the reconciler this work extends), and resolves the open issues
[`snapshot_capture_ssh_failure.md`](../../issues/snapshot_capture_ssh_failure.md)
and [`stuck_thread_workspace_pods.md`](../../issues/stuck_thread_workspace_pods.md).

## Problem

Workspace pods that should be torn down leak indefinitely. Five were found
stuck 17–24 days on the dev cluster (`superhuman-remote-worker`, `main`
context).

Teardown is **snapshot-then-delete**: `WorkspaceSuspensionService.suspend_*`
SSHes into the pod, `tar`s `/home/agent-host` to S3, and **only deletes the
pod if the snapshot succeeds**. On failure it reverts status to `ready` and
logs *"keeping workspace alive."* So any pod the orchestrator cannot SSH into
is **never reaped** — it re-fails every ~60s forever (one job logged
`idle 35858m`).

The five leaked pods are unreachable because they **predate the SSH port
migration** (22 → 30022; commits `2aeaf524`, `56ec68bc`, 2026-05-19): their
sshd listens on 22, but the current NetworkPolicy admits ingress only on
30022. They are pre-migration artifacts — deleting them is correct and that
exact mismatch cannot recur on new pods.

The migration is therefore **already fixed**; what remains open is the
**architectural cause**: snapshot-failure means keep-alive-*forever*. The port
was one trigger; a stale pod IP, a node power-event, or a transient SSH window
hit the same dead end. This spec fixes that class.

### Triage of contributing causes

| Cause | Status |
|---|---|
| 22→30022 port/netpol mismatch (why the 5 are unreachable) | ✅ Fixed — pods predate it |
| Pod-side Failed/Unknown zombies (node power-down class) | ✅ Fixed — reconciler crash-recovery (Phase 2b) ships |
| **Snapshot-failure → keep-alive-forever (no retry bound, no give-up)** | ❌ **Open — this spec** |
| **DB-orphan rows (status `ready`, pod already gone)** | ❌ Open — this spec |
| **Suspend path defaults SSH port to 22 when `port` missing** | ❌ Open (latent) — this spec |
| Bare pods, no ownerRef (K8s won't GC) | ❌ Open (defense-in-depth) — this spec |

## Goals

- A workspace whose bound work is finished, or which is reap-eligible and
  cannot be snapshotted, is **always** cleaned up — never kept alive forever.
- Cleanup is **correct for both volume modes**: emptyDir (today's default) and
  PVC-backed (a supported but not-yet-default code path).
- **No behavior change for a healthy, in-use workspace.** Force-delete fires
  only on clean, or reap-eligible-and-exhausted, instances.
- Teardown logic is **owned by the lifecycle reconciler**, the documented
  end-state, retiring the parallel `workspace_idle_sweeper` loop.

## Non-goals

- **Migrating workspaces to PVC-backed durable storage.** This spec makes the
  reaper *correct* for PVC mode (via `is_state_ephemeral`), but emptyDir stays
  the default. The migration is a separate follow-up spec (per-pod PVC
  lifecycle, PVC GC policy, RWO/RWX, restore-by-reattach tested live, helm
  defaults). Decision: **split — fix now, migrate next.**
- **Content-hash dirty detection.** Dirty is activity-based (see Design). A
  content hash was considered and rejected: it needs in-pod machinery and
  curated hash inputs, and the cheaper activity signal already works when the
  pod is unreachable. Recorded for the migration spec to revisit if needed.
- **VM workspaces.** The same reconciler hosts `VMInstanceManager`; this spec
  scopes to the container `WorkspaceInstanceManager`. VM parity is a follow-up.

## Design

### Decision flow (the reap path in `tick()`)

Today `reconciler.tick()` has two branches — crash (`not is_healthy` →
`delete(grace=0)`) and drift (→ `drain`). It has **no idle/teardown branch**;
that lives in the separate `workspace_idle_sweeper`, the loop stuck in
keep-alive-forever. Approach B moves teardown into `tick()` as a stateful reap
path, run for a healthy instance after crash/drift handling:

```
if is_stateful(mgr) and await mgr.is_reapable(inst):
    if not await mgr.is_dirty(inst):
        await mgr.delete(inst, grace_s)                  # clean → reap, no SSH
    elif await mgr.is_reachable(inst):
        ref = await mgr.snapshot(inst)
        if ref:
            await mgr.delete(inst, grace_s)              # dirty + saved → reap
        else:
            await mgr.record_attempt(inst)               # snapshot failed → retry later
    elif await mgr.attempts_exhausted(inst):
        await mgr.give_up(inst, grace_s)                 # dirty + unreachable, exhausted
    else:
        await mgr.record_attempt(inst)                   # leave alive, retry next tick
```

`give_up` branches on volume mode (see *Volume-mode branch*). The disruption
budget already caps deletes per kind per tick; reap deletes respect it.

### The predicates

All computed **orchestrator-side, without touching the pod** unless noted.

**`is_reapable(inst)`** — widens today's `is_idle`. Two families that both mean
"pod no longer needed":
- **terminal**: job ∈ {completed, failed, cancelled}, or thread = ended → reap.
  (Today's `is_idle` excludes completed jobs — that is why completed job
  `8d31111d` leaked. This closes it.)
- **suspendable-idle**: job ∈ {paused, pending_review, waiting_for_reply} past
  `WORKSPACE_IDLE_TIMEOUT` → snapshot-then-delete, restorable on resume
  (unchanged intent of the old sweeper).

**`is_dirty(inst)`** = `work_marker(inst) > marker_at_last_snapshot`.
- Threads: **`total_turns`** (verified to exist; monotonic; incremented only on
  real turns). A 0-turn thread reads clean → instant reap (3 of the 5).
- Jobs: **RESOLVED — no dirty-marker (asymmetric with threads).** `jobs` has no
  Postgres turn counter, and the only monotonic job-activity signal
  (`get_audit_count`) lives in MongoDB — a cross-store call we will not add to
  the reconciler tick. Instead: terminal jobs (completed/failed/cancelled)
  already receive a completion snapshot (`main.py:6038`), so they reap via that
  existing capture; paused/idle jobs simply attempt a snapshot when reaped
  (re-snapshotting an unchanged paused job is mildly wasteful but correct).
  `is_dirty` therefore only does real work for threads (`total_turns`); for
  jobs it returns a conservative "treat as dirty → attempt snapshot," and the
  escape hatch bounds the unreachable case. Both observed leaked job pods are
  handled correctly without a job marker.
- **NOT `last_activity`** — it is contaminated: `merge_*_workspace_context` /
  `merge_*_snapshot_context` set `last_activity = CURRENT_TIMESTAMP` as a side
  effect (postgres.py:1564, 1603, 1640, 1714), so the reaper's own bookkeeping
  (and the attempt-counter writes below) bump it. This is why the 3 threads
  reported a fixed `idle 30m` every cycle while the job reported `idle 35858m`.
- An **emptyDir crash is unconditionally clean** — the volume died with the
  pod, so there is provably nothing left to capture (see volume-mode branch).

**`is_reachable(inst)`** — cheap TCP/SSH ping on the kind-correct port,
**cached ~30s** per pod IP (single orchestrator process → in-memory dict).
Used **only** in the reap path's dirty branch to choose snapshot-vs-retry.
Deliberately **not** part of `is_healthy` (see below).

### `is_healthy` stays phase-only (correcting an earlier draft)

An earlier sketch put SSH-ping into `is_healthy` (per the vault's original
wording). Rejected: `is_healthy` failure routes to crash-recovery
`delete(grace=0)`, so "unreachable ⇒ unhealthy" would force-delete a **busy**
workspace over a transient network blip — a worse bug than the leak.
Separation:
- **`is_healthy` = pod phase only** (Failed/Unknown → delete). Already-shipped
  Phase 2b crash recovery; unchanged.
- **Reachability is probed only in the reap path**, only for already
  reapable (done/paused) instances. A busy workspace is never probed, so it can
  never be reaped over a blip.

### Volume-mode branch (`is_state_ephemeral`)

The reaper must not assume emptyDir forever — the provisioner already supports
PVC mode (`container_provisioner.py:928` PVC vs `:932` emptyDir, gated by
`pvc_name`). State recoverability differs:

- **emptyDir** (today's 5, current default): crash ⇒ state genuinely gone ⇒
  snapshot impossible ⇒ reaping the tombstone loses nothing. **Force-delete the
  pod.**
- **PVC-backed** (supported, not default): crash ⇒ **do not snapshot, do not
  destroy** — **recreate the pod against the same PVC**; the volume reattaches
  and state survives. The PVC outlives the pod; PVC deletion happens only when
  the bound work is truly terminal, not on crash.

`is_state_ephemeral(inst)` (read from the pod/owner volume spec) splits the
terminal arms:

```
unhealthy, or (dirty and unreachable and exhausted):
    if is_state_ephemeral(inst):   delete(pod)                 # nothing to save
    else:                          recreate_pod_keep_pvc(inst) # volume reattaches
```

For this spec the PVC arm is **designed-for but minimally activated** — the
branch and predicate exist and are correct, but `recreate_pod_keep_pvc` may be
a thin wrapper over existing provisioner calls; full restore-by-reattach
testing lands with the migration spec.

### Attempt counter + escape-hatch threshold

`snapshot_attempts` stored in the workspace context JSONB
(`jobs.context.workspace_container` / `threads.metadata.workspace_container`).
Incremented on each failed/unreachable attempt; reset to 0 on success.
`attempts_exhausted = snapshot_attempts >= N`. **N is Helm-configurable**,
default ~5 (~5 min at the 60s tick). On exhaustion → `give_up` (force-delete
for emptyDir; recreate for PVC) + metric + WARN.

### Snapshot marker

On snapshot success `snapshot_service` already writes `{"status":"available"}`
into the snapshot context. Add one field: **`work_marker_at_capture`** (the
turn/audit count at capture). This is the value `is_dirty` compares against.
One-line addition at each `_set_snapshot_context(..., {"status":"available"})`.

### DB-orphan handling

Rows with workspace status `ready` whose pod is already gone ("No route to
host") flow through the same hatch: gone pod ⇒ unreachable ⇒ (if emptyDir,
clean ⇒) reap; the row's workspace context is cleared. The current reconciler
lists **by pod**, so it cannot see these. `list_instances` must additionally
surface context rows whose pod is missing — the one genuinely new enumeration
path. Reaping such a row means marking its workspace context torn-down (and,
for terminal-bound PVC rows, releasing the PVC).

## Supporting changes

- **G — Metrics.** `workspace_reaped_total{reason=clean|snapshotted|forced|crash}`,
  `workspace_force_deleted_total{volume_mode}`, `workspace_snapshot_attempts`.
  `forced` is the "accepted data loss" signal and should alert.
- **H — Retire `workspace_idle_sweeper`.** Its suspend logic moves into the
  reap path. The loop thins to the session-workspace reconcile it also runs
  today (`reconcile_session_workspaces`), or that moves to the reconciler too.
  No two-reaper overlap remains.
- **I — Default-22 port fix.** Resolve SSH port by kind in one shared place
  (pod → 30022, VM → 22); remove the `ws_ctx.get("port", …, 22)` fallback
  (`workspace_suspension.py:127`). Latent-footgun removal, independent of the
  rest.
- **J — Owner-ref / TTL.** Stamp an `ownerReference` (or `srw.io/ttl`
  annotation) on workspace pods at creation so K8s GC is a backstop even if
  orchestrator logic misses. Defense-in-depth.

## One-time cleanup

Delete the 5 pre-migration pods manually (bare pods, no ownerRef, emptyDir →
no data to preserve):

```bash
kubectl --context=main -n superhuman-remote-worker delete pod \
  workspace-692f00d5-0ac workspace-8d31111d-31d \
  ws-thread-8b7f0e31-a15 ws-thread-b9b6c2be-dcc ws-thread-f6c19671-7b9
```

Caveat: job `692f00d5` is `pending_review`; its emptyDir state was never
snapshotted, so deletion discards it. Confirm the review is abandoned first.
Also clear any DB-orphan workspace contexts left behind.

## Testing

- **Unit (predicates):** `is_reapable` per status; `is_dirty` clean (0-turn /
  marker-equal) vs dirty (marker-ahead); `is_state_ephemeral` emptyDir vs PVC;
  `attempts_exhausted` boundary; `last_activity` is **not** consulted.
- **Reconciler tick (table-driven):** each branch of the decision flow reaches
  the right action; healthy in-use workspace → untouched; disruption budget
  caps reap deletes.
- **Escape hatch:** dirty + unreachable increments attempts, force-deletes at N
  (emptyDir) / recreates (PVC), emits the metric.
- **DB-orphan:** context row with missing pod is enumerated and reaped.
- **Regression:** the suspendable-idle snapshot→restore round-trip still works
  (parity with the retired sweeper).

CI (Py3.12) is the gate; local `pytest tests/` is env-noisy.

## Open questions

1. ~~Jobs work-marker for `is_dirty`~~ — **RESOLVED**: no job marker; asymmetric
   with threads (see *The predicates*).
2. **`give_up` for PVC** — exact recreate-vs-leave semantics on a *dirty,
   unreachable, PVC-backed* pod (restart may restore reachability and let a
   later tick snapshot). Likely: recreate (reattach), don't force-delete the
   PVC. Deferred to the migration spec; for this spec the PVC arm is minimally
   activated (designed-for, thin wrapper).

## References

- [`unified_instance_lifecycle.md`](../../features/unified_instance_lifecycle.md)
- [`ephemeral_workspaces.md`](../../features/ephemeral_workspaces.md)
- [`snapshot_capture_ssh_failure.md`](../../issues/snapshot_capture_ssh_failure.md)
- [`stuck_thread_workspace_pods.md`](../../issues/stuck_thread_workspace_pods.md)
- `orchestrator/services/lifecycle/{reconciler,workspace_manager,types}.py`
- `orchestrator/services/workspace_suspension.py`,
  `orchestrator/services/snapshot_service.py`,
  `orchestrator/services/container_provisioner.py`
