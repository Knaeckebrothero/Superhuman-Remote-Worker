---
tags:
  - issue
  - sessions
  - vm-backend
  - workspace-lifecycle
  - snapshots
  - data-loss
---

# Issue — VM workspace state can never be captured from the orchestrator: suspend fails loudly, reap fails silently and logs success

**Status:** Found 2026-07-28 during the live gate of `6d66f7c4`; scope widened
2026-07-29 after review pushback. **Confirmed live and at code level, not fixed.
Decision required** — see Options.

**One line:** Capturing a workspace snapshot means SSHing to it from the
**orchestrator**, but a VM workspace only has a tailnet address
(`100.64.0.0/10`) and the orchestrator is not a tailnet node — so
`capture_vm_snapshot` refuses for every VM. The suspend path surfaces that as a
failure; **the reap path ignores the return value, logs "VM snapshot captured",
and deletes the VM anyway.**

**Severity: silent data loss.** Every VM session end discards unpushed workspace
state while reporting success. Bounded by session git-versioning (commits every
turn, pushes every 5th tool-turn), so what is lost is unpushed commits — not
everything, but not nothing, and nobody is told.

## Root cause (one), consequences (two)

`SnapshotService.capture_vm_snapshot` (`snapshot_service.py:389-421`) opens with
the F4 tailnet guard from
`vm_ssh_readiness_probe_unroutable_from_orchestrator.md`:

```python
if not orchestrator_can_reach(ssh_host):
    logger.info("Skipping snapshot capture for %s %s (%s:%d): orchestrator "
                "has no route to tailnet targets", ...)
    await self._set_snapshot_context(job_id, {
        "status": "capture_skipped",
        "error": "unroutable tailnet target from orchestrator"}, ...)
    return False          # ← returns, does NOT raise
```

The guard is correct — an orchestrator-vantage SSH to `100.64.x` black-holes
rather than failing fast. Structurally: **agent pods run a `tailscale` sidecar,
the orchestrator does not** (`containers: agent tailscale` vs
`containers: orchestrator`).

### Consequence A — suspend fails loudly (visible, no data loss)

`suspend_thread_workspace` checks the return, logs
`"Snapshot capture failed … — keeping workspace alive"`, and returns False. The
VM keeps running. Live proof (thread `a9299e55-810b-48d0-b08d-fabe045aa131`,
VM at `100.64.1.6`, via internal `POST /api/agents/threads/{id}/suspend`):

```
Skipping snapshot capture for threads a9299e55… (100.64.1.6:22):
  orchestrator has no route to tailnet targets      snapshot_service.py:395
Snapshot capture failed for thread a9299e55… — keeping workspace alive
                                                   workspace_suspension.py:565
→ {"suspended":false,"status":"active","reason":"snapshot_failed"}
```

### Consequence B — reap loses the workspace and claims it didn't (the serious one)

`vm_provisioner.release_thread_vm` (`vm_provisioner.py:441-496`) is documented
*"Snapshot a thread VM to S3, then delete it"* with *"snapshot failure is
non-fatal"*. It only guards against **exceptions**:

```python
try:
    await self._snapshot_service.capture_vm_snapshot(...)   # returns False — ignored
    logger.info("VM snapshot captured for thread %s before release", thread_id)
except Exception:
    logger.exception("VM snapshot failed for thread %s — deleting anyway", ...)

return await self.delete_thread_vm(thread_id)
```

Because the guard returns rather than raises, the `except` never fires: the
success line is logged, the VM is deleted, and the snapshot does not exist. The
only honest trace is `snapshot.status = "capture_skipped"` on the thread context,
which nothing surfaces to the user or to `list_jobs`-style views.

This runs on **every** VM session end — verified twice on dev (threads
`6e9f7aad`, `a9299e55` both went to `vm.status='deleted'` seconds after ending).

## Scope note — this is not only sessions

`release_workspace`/`release_thread_vm` and `capture_vm_snapshot` are shared with
the job path. Any VM-backed **job** reap takes the same silent path. The job-side
sibling doc `snapshot_restore_dead_for_jobs.md` says snapshots are "captured for
jobs … but never restored"; for **VM**-backed jobs they are not even captured.
That doc's claim that restore "runs only for sessions/threads" should be read as
**container** sessions only.

## Options

1. ~~**Reap without snapshotting for VM tier.**~~ **REJECTED.** Proposed in the
   first draft of this doc on the grounds that it "matches what explicit end
   already does". Review pushback was correct: reap is *designed* to snapshot
   first, so this would codify the data loss rather than fix it. The fact that
   the current reap already loses state is a second instance of the bug, not a
   baseline to copy.
2. **VM-side push (recommended).** The in-VM management daemon captures and
   uploads its own snapshot to S3, triggered over NATS. The VM already speaks
   NATS and needs **no inbound route**, which is the whole difficulty here.
   Fixes both consequences and is the only option that makes suspend/resume
   actually work. Costs: golden-image + daemon change, version skew with running
   VMs (old images would need to keep failing loudly, not silently).
3. **Give the orchestrator a tailnet route.** The escape hatch already exists
   (`ORCHESTRATOR_HAS_TAILNET_ROUTE=true`); it needs a `tailscale` sidecar on the
   orchestrator deployment. Cheapest path to a working capture, but widens the
   orchestrator's reach to every agent VM — a blast radius the readiness incident
   deliberately narrowed by *removing* orchestrator-vantage SSH.
4. **Tailnet-capable helper pod per capture.** Correct but the most moving parts:
   the agent pod is normally gone by reap/idle time, so this means spawning a
   short-lived job purely to snapshot.

### Do this first regardless of the option chosen

**Stop the false success log.** `release_thread_vm` (and its job-side twin) must
check `capture_vm_snapshot`'s return value and say what actually happened:

```
VM snapshot SKIPPED for thread <id> (unroutable tailnet target) —
deleting anyway; unpushed workspace state will be lost.
```

One-line change, no behaviour change, and it converts a misleading log into an
accurate one. Until the capture itself is fixed, this is the difference between
"we lose state and say so" and "we lose state and claim we didn't".

Consider also surfacing `snapshot.status == "capture_skipped"` somewhere a user
can see, since that is currently the only record.

## Acceptance criteria

- Reap logs the truth about whether a snapshot was captured.
- A VM session's workspace snapshot exists in S3 after an idle suspend or an
  explicit end, with `source_type="vm"`, **or** it is documented as unsupported
  and the user is told at session-create time rather than discovering it later.
- Whichever option is chosen, `snapshot_restore_dead_for_jobs.md` is updated in
  step — its "sessions work" claim is only true for container sessions.

## Related

- `docs/issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md` — why the
  orchestrator has no tailnet route; source of the F4 skip guards.
- `docs/issues/snapshot_restore_dead_for_jobs.md` — job-side sibling; its scope
  is narrowed by this doc.
- `docs/issues/workspace_suspension_infers_tier_from_metadata_presence.md` — the
  first of the two gates in front of suspend, fixed in `6d66f7c4`.
