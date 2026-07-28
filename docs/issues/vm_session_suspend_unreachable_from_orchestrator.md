---
tags:
  - issue
  - sessions
  - vm-backend
  - workspace-lifecycle
  - snapshots
---

# Issue — VM-tier sessions can never idle-suspend: the snapshot needs a tailnet route the orchestrator does not have

**Status:** Found 2026-07-28 during the live gate of `6d66f7c4` (the suspension
tier fix). **Confirmed live, not fixed. Decision required** — see Options.

**One line:** Suspending a session snapshots its workspace over SSH from the
**orchestrator**, but a VM workspace only has a tailnet address (`100.64.0.0/10`)
and the orchestrator is not a tailnet node — so `capture_vm_snapshot` refuses by
design, `suspend_thread_workspace` returns False, and the VM keeps running.

## Evidence (live, dev)

Thread `a9299e55-810b-48d0-b08d-fabe045aa131`, VM ready at `100.64.1.6`, invoked
via the internal `POST /api/agents/threads/{id}/suspend`:

```
Skipping snapshot capture for threads a9299e55… (100.64.1.6:22):
  orchestrator has no route to tailnet targets      snapshot_service.py:395
Snapshot capture failed for thread a9299e55… — keeping workspace alive
                                                   workspace_suspension.py:565
→ {"suspended":false,"status":"active","reason":"snapshot_failed"}
```

This is not a bug in the refusal — it is the F4 tailnet-skip guard from
`vm_ssh_readiness_probe_unroutable_from_orchestrator.md`, which exists because an
orchestrator-vantage SSH attempt black-holes rather than failing fast. The
comment at `snapshot_service.py:390-398` states it plainly: *"snapshots are not
supported on the VM backend."*

Structural confirmation: the **agent** pod runs a `tailscale` sidecar
(`containers: agent tailscale`); the **orchestrator** pod does not
(`containers: orchestrator`).

## Relationship to the tier fix (`6d66f7c4`)

That fix was necessary and is working — it is what let the suspend get *far
enough* to hit this. Before it, `suspend_thread_workspace` bailed at
`ws_status != "ready"` (tier misread) and never attempted a snapshot at all. The
live gate showed it now resolving `is_vm=True`, using the VM's ssh_host on port
22, and reaching `capture_vm_snapshot`.

So there were **two gates in series**. `6d66f7c4` opened the first. This is the
second, and it is architectural rather than a coding error.

**Correction to `6d66f7c4`'s framing:** that commit was described as fixing the
"VM suspend leak". It does not — an idle VM session's VM still runs. What changed
is that suspend now fails *visibly* instead of silently misreading the tier, and
the tier/`source_type`/SSH-port bugs are genuinely fixed.

## How large is the leak, really

**Narrower than it first appears.** An explicit session end already deletes the
VM — verified twice on dev (threads `6e9f7aad` and `a9299e55` both went to
`vm.status='deleted'` within seconds of ending). `check_idle_threads` only selects
threads that are **already `ended`** with a still-`ready` VM, i.e. cases where the
end-path delete did not happen: an orphaned/crashed session, or a failed delete.

So this is not "every VM session leaks", it is "a VM whose session ended
abnormally is never reclaimed". Real, but bounded — and worth sizing before
spending much on it.

## Options

1. **Reap instead of suspend for VM tier (smallest).** On the idle path, delete
   the VM without snapshotting. This makes the idle path match what an explicit
   end already does, so it introduces no behaviour that does not already exist.
   Costs: no resume-from-suspend for VM sessions (there is none today anyway),
   and loss of workspace state not yet pushed to the thread's Gitea repo — bounded
   by the session git-versioning cadence, which commits every turn but pushes
   every 5th tool-turn.
2. **VM-side push (most principled).** Have the in-VM management daemon capture
   and upload its own snapshot to S3, triggered over NATS — the VM already speaks
   NATS, and nothing needs an inbound route. Costs: golden-image + daemon change,
   and version skew with already-running VMs.
3. **Give the orchestrator a tailnet route.** The escape hatch already exists
   (`ORCHESTRATOR_HAS_TAILNET_ROUTE=true`, F4 in the readiness doc); it would need
   a `tailscale` sidecar on the orchestrator deployment. Costs: widens the
   orchestrator's network reach to every agent VM. The readiness doc deliberately
   removed orchestrator-vantage SSH rather than add this.
4. **Delegate to a tailnet-capable helper pod.** Correct, but the agent pod is
   normally gone by the time the idle sweep runs (the thread is `ended`), so this
   means spawning a short-lived job purely to snapshot — the most moving parts for
   the least-used path.

**Recommendation: (1) now, (2) if VM sessions become load-bearing.** The exposure
is a narrow orphan case on an admin-gated tier; (1) removes the resource burn with
a change that matches existing behaviour, while (2) is the only option that makes
suspend/resume genuinely work and is worth doing when someone actually needs to
resume a VM session. (3) buys the same outcome as (2) by widening a blast radius
the codebase spent an incident narrowing.

## Related

- `docs/issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md` — why the
  orchestrator has no tailnet route and where the F4 skip guards came from.
- `docs/issues/snapshot_restore_dead_for_jobs.md` — the job-side sibling. It notes
  restore "runs only for sessions/threads"; this doc narrows that further: it runs
  only for **container** sessions. VM sessions were never covered.
- `docs/issues/workspace_suspension_infers_tier_from_metadata_presence.md` — the
  first of the two gates, fixed in `6d66f7c4`.
