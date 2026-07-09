---
tags:
  - issue
  - investigation
  - jobs
  - workspace-lifecycle
  - vm-backend
  - golden-image
  - loop
  - dispatcher
---

# Investigation — a cold golden-image import (triggered by any `agent-vm-base` image bump) fails every in-flight VM job of the current loop iteration

**Status: INVESTIGATION — findings confirmed live (2026-07-09, `main` cluster);
fixes proposed below, not yet built.** Surfaced while watching the first VM
provisioning round after the "VMs not reachable" fix
([`vm_ssh_readiness_probe_unroutable_from_orchestrator.md`](vm_ssh_readiness_probe_unroutable_from_orchestrator.md))
rolled out. That fix shipped a **new `agent-vm-base` image (`sha-e375179`)**, which
forced the vm-controller to import a **fresh golden DataVolume** — a ~30-minute cold
import. The two loop jobs dispatched into that window (`c40a4ebf`, `427dbc57`) both
**failed**; the next iteration (`012c72cb`) booted cleanly once the golden was warm.

**One-line:** the vm-controller waits `VM_GOLDEN_POLL_TIMEOUT=900s` for a golden import,
but the orchestrator dispatcher recycles a not-yet-`ready` VM after only
`VM_PROVISION_TIMEOUT_S=600s` — and a cold golden import actually takes ~30 min. The
budgets are mutually misaligned and none of them account for cold-import time, so **any
VM job dispatched during a golden refresh burns all 3 provision attempts and fails.**

**Severity: iteration-burner, self-healing (NOT a loop-killer).** Unlike the SSH-readiness
wedge, the loop advances: `VM_PARK_EXHAUSTED`/`VM_PARKED` correctly fail the jobs (F3 from
the SSH doc, working as intended) and the next iteration provisions fast against the warm
golden. Blast radius is **one iteration's worth of in-flight VM jobs per `agent-vm-base`
image bump**, plus two secondary defects (a 409 re-provision race and a ~5-min orphan-reap
delay). It recurs on **every** agent-vm-base image rollout and will hit `srw-prod-private`
the same way.

**Related (read alongside):**
[`vm_ssh_readiness_probe_unroutable_from_orchestrator.md`](vm_ssh_readiness_probe_unroutable_from_orchestrator.md)
— its F3 (park → fail the job) is what let the loop advance here; its no-tailnet-route root
cause is why Finding C's reaper snapshot fails. ·
[`../done/deleted_job_orphans_workspace_pod.md`](../done/deleted_job_orphans_workspace_pod.md)
— the VM orphan-reap backstop (`vm_manager.reap_orphans`) being live-verified; this incident
is its first real exercise (Finding C). ·
[`../features/vm_golden_image_boot_acceleration.md`](../features/vm_golden_image_boot_acceleration.md)
— the golden-image design whose cold-import window this exposes.

---

## The incident (confirmed)

Main cluster (`superhuman-remote-worker` ns), RSI loop for the "Hotel Rheinland ERP"
project (MiniMax), iteration 22. Both jobs `config_override.workspace.backend="vm"`:

- `c40a4ebf-5929-4ead-91ee-249bd854d46e` — iter 22 · SCHOLAR
- `427dbc57-203d-413e-872c-bfde7ebe38f3` — iter 22 · PRODUCT-QA

(The loop's stage shape is `scholar ∥ product-qa → critic`, so these two run in parallel.)

The controller was redeployed with `DEFAULT_VM_IMAGE=…agent-vm-base:sha-e375179` and, at
`09:48:35Z`, created golden DataVolume `agent-vm-golden-9ca967a4ca08`
(`source: docker://ghcr.io/knaeckebrothero/superhuman-remote-worker-agent-vm-base:sha-e375179`)
and began a cold import. The two prior goldens (`e98330dd0d47` 2d old, `58a046a8c6f3` 7d old)
were `Succeeded` but are for **older digests** — the new image needs the new golden.

### Timeline (orchestrator + vm-controller + CDI logs, all `2026-07-09` UTC)

| Time | Event |
|---|---|
| 09:48:35 | Controller (new image) creates golden DV `9ca967…`, begins cold import (~30 min) |
| 09:49:53 / 09:50:01 | iter-22 SCHOLAR / PRODUCT-QA jobs created (`backend=vm`), dispatched into the cold-golden window |
| ~09:50 → 10:10 | Provision **attempts 1 & 2**: controller `_ensure_golden` blocks on the still-importing golden; orchestrator recycles each at the 600 s budget |
| 10:03:38 / 10:05:05 | Controller: `golden agent-vm-golden-9ca967a4ca08 did not reach Succeeded within 900s` (×2); `golden pre-warm did not complete (non-fatal)` → **creates the VM anyway** (falls back toward per-VM registry import) |
| 10:05:05 | VM status `created` — stuck; golden still importing (~68 %) |
| 10:10:46 | Orchestrator: `job … VM stuck in 'created'/'provisioning' for 600s (> 600s budget) — recycling (attempt 2/3)` → deletes both VMs (`main.py:4166`) |
| 10:11:16 | Re-provision **attempt 3/3** for both (`main.py:4131`) |
| ~10:18–10:19 | Golden `9ca967…` reaches `Succeeded / 100 %` |
| 10:17:56 | Controller reports VM `created` then `failed` for both jobs (**cause to confirm** — see Finding B) |
| 10:18:16 | Orchestrator re-provision hits **`409 AlreadyExists`** (`virtualmachines.kubevirt.io "agent-vm-…" already exists`) → `VM_PARKED` → **fails both jobs** (`main.py:4152`), ~3 min *before* the attempt-3 budget deadline |
| 10:18:46 | **Loop advances**: iter-23 CRITIC `012c72cb-…` auto-provisioned, attempt 1/3 (`main.py:4131`) |
| 10:24:22 | `012c72cb` VM `Running`/`READY=True`; agent `6cdbced8` accepts (`POST /job/start → 202`); job → `processing` |
| 10:25:00 / 10:25:05 | iter-22 jobs recorded `failed` in DB |
| 10:25 → 10:30 | Reaper detects 2 orphan VMs each tick (`listed:3, reap_attempts:2`) but graceful pre-delete snapshot keeps failing |
| 10:30:51 / 10:30:56 | Reaper `force-deleted dirty unreachable instance … state not captured (snapshot attempts exhausted)` → `reap_forced:2`; **orphans gone** (`reconciler.py:287`) |

---

## Root cause (confirmed live)

### A — Provisioning budgets are mutually misaligned and cold-import-unaware (primary)

Three independent timers govern a VM boot, and they are ordered exactly wrong for a cold
golden:

- Orchestrator per-attempt recycle: `VM_PROVISION_TIMEOUT_S=600` (× `VM_PROVISION_MAX_ATTEMPTS=3`).
- Controller golden wait: `VM_GOLDEN_POLL_TIMEOUT=900` (`vm/controller/controller.py:86`).
- Actual cold golden import on `local-path` for a 20 Gi image: **~30 min** (observed
  09:48:35 → ~10:19, DV progress 0 → 100 % with 1 CDI restart).

So `600s (orch recycle) < 900s (controller golden wait) < ~1800s (real import)`. The
orchestrator deletes the VM before the controller even finishes *waiting* for the golden,
and the golden isn't done anyway. Every attempt is structurally doomed while the import is
in flight; after 3 the job fails. Nothing in the dispatch path asks "is this job's golden
still importing?" — if it did, the correct action is **WAIT, not RECYCLE**.

Contributing: when `_ensure_golden` times out at 900 s the controller logs
`golden pre-warm did not complete (non-fatal)` and **creates the VM anyway**
(`controller.py:243-256`, `golden_name=None` → manifest keeps the registry source), which
kicks off a *second* slow path (per-VM registry import) that also can't finish inside 600 s.

### B — 409 `AlreadyExists` re-provision race on a transient controller `failed`

At 10:17:56 the controller flipped the attempt-3 VMs to `failed` while the VMIs were in
fact still coming up (they later reached `Running`). The dispatcher reacted to
`status="failed"` by re-provisioning (`VM_PROVISION`), and `create_vm` collided with the
still-present VM object → **`409 AlreadyExists`** surfaced to the orchestrator as a hard
error → `VM_PARKED` → job failed (`main.py:4152`). The controller treats a create-409 as a
lock internally (`controller.py:487-519`), but this 409 propagated to the orchestrator
instead of being reconciled. Net effect: the job failed via a *race* ~3 min before its
budget, rather than via the clean `VM_PARK_EXHAUSTED` timeout path. **The gap is
delete-before-create on re-provision** (or: treat a transient controller `failed` as
`RECYCLE` — tear down then recreate — not as "VM absent, create fresh").

### C — Orphan VMs are reaped, but only after a ~5-min snapshot-timeout stall

Because the jobs failed (Finding B) *while* their attempt-3 VMs were still booting, both
VMIs reached `Running` with no owning job — orphans. The backstop reaper
(`vm_manager.reap_orphans`, `reconciler.py:238`) **detected** them correctly every tick
(`listed:3, reap_attempts:2`, sparing the one legit VM) but could not delete them for ~5
min: it first tries to **capture VM state (snapshot) before deleting**, and that snapshot
runs from the orchestrator, which **has no tailnet route to the VM** (the same root cause
as the SSH doc). Only after `snapshot attempts exhausted` did it `force-delete`
(`reconciler.py:287`, `reap_forced:2`). So this is a **latency + wasted-work** problem, not
a leak — but it's the same no-route coupling the SSH doc's F4 was meant to close, now
showing up in the reaper's pre-delete snapshot path. A VM the orchestrator provably cannot
reach should skip the snapshot and force-delete immediately (or after a short bound).

---

## What worked (do not "fix")

- **The loop self-healed.** iter-23 CRITIC provisioned, booted on the warm golden in ~5
  min, and is `processing`. The system tolerates losing one iteration's VM jobs.
- **F3 (park → fail the job)** from the SSH doc did its job: both iter-22 jobs went
  terminal with the loop advancing — no `created`-forever wedge.
- **The orphan reaper eventually cleaned up** (force-delete after snapshot exhaustion). The
  backstop from `deleted_job_orphans_workspace_pod.md` is live and functional; Finding C is
  a tuning issue on top of it.

---

## Proposed fixes (menu — not yet built)

1. **Golden-aware provisioning (addresses A, highest value).** Before recycling a
   not-`ready` VM, have the dispatcher check whether the job's golden DV is still
   `ImportInProgress` and **WAIT** instead of RECYCLE (surface golden import state from the
   controller over NATS / `GET /vms`). Equivalently: make the orchestrator per-attempt
   budget ≥ the controller golden budget, and make both ≥ a realistic cold-import bound.
2. **Pre-warm gate on image rollout (also A).** Hold loop VM dispatch (or pause new VM
   jobs) until the new golden is `Succeeded` after an `agent-vm-base` bump — the pre-warm
   already exists (`golden pre-warm …`) but is non-blocking, so the loop dispatches straight
   into the cold window. Gate on it, or block the first VM create until golden ready rather
   than falling back to the (also-slow) per-VM registry import.
3. **Delete-before-create / transient-failed handling (B).** On re-provision, delete any
   existing VM object first, or map a controller `failed` on an in-flight VM to `RECYCLE`
   rather than a fresh `VM_PROVISION` that races into `409 AlreadyExists`.
4. **Skip pre-delete snapshot for unreachable VMs (C).** When the orchestrator has no route
   to the VM (reuse the SSH doc's `orchestrator_can_reach`/`is_tailnet_addr` /
   `ORCHESTRATOR_HAS_TAILNET_ROUTE` guard), the reaper should force-delete immediately
   instead of burning ~5 min on snapshot attempts that cannot succeed.

## Acceptance criteria

- A VM job dispatched while its golden DV is `ImportInProgress` **waits** (does not consume
  provision attempts) and dispatches within seconds of the golden reaching `Succeeded` —
  demonstrated on the next `agent-vm-base` image bump.
- No `409 AlreadyExists` appears in the dispatcher VM path; a re-provision after a
  controller-reported `failed` tears down the old VM object first.
- An orphaned VM the orchestrator cannot reach is force-deleted within one reconciler tick
  (no multi-minute `snapshot attempts exhausted` stall).
- Documented expectation: an `agent-vm-base` image rollout costs at most a brief pause of
  new VM dispatch (golden pre-warm), **not** a failed loop iteration.
</content>
</invoke>
