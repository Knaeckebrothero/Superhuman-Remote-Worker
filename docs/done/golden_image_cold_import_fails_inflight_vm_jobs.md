---
tags:
  - issue
  - fix-spec
  - jobs
  - workspace-lifecycle
  - vm-backend
  - golden-image
  - loop
  - dispatcher
---

# Investigation — a cold golden-image import (triggered by any `agent-vm-base` image bump) fails every in-flight VM job of the current loop iteration

**Status: RESOLVED (2026-07-12) — fix committed `5f8b6047`, pushed, and deployed
on both sides of the dev stack since 2026-07-10** (orchestrator `sha-2a71df3` on
`main`/`superhuman-remote-worker`, vm-controller `sha-f1f32eb` on `vm`/`agent-vms`;
both contain `5f8b6047`). All three findings addressed: (A)
controller `_do_create` no longer blocks on the golden import — it returns a new
`waiting_golden` status and the dispatcher polls create without consuming provision
attempts (`VM_GOLDEN_POLL`, bounded by `VM_GOLDEN_WAIT_TIMEOUT_S=2700` →
`VM_PARK_GOLDEN` fails the job with a truthful error); (B) a plain VM-create 409
AlreadyExists is idempotent success in the controller (deterministic name
`agent-vm-<job_id>` ⇒ the existing VM IS this job's VM); (C) the lifecycle reaper's
`attempts_exhausted` is instantly true for tailnet hosts the orchestrator provably
cannot reach — force-delete on the first tick instead of a ~5-min snapshot-retry
stall. 446 tests green across the five affected suites
(`test_vm_controller`, `test_dispatch_guards`, `test_vm_provisioner`,
`test_nats_bridge`, `test_lifecycle_vm_manager`), ruff clean.

**⚠️ Live-verification caveat:** as of 2026-07-12 the new `waiting_golden` path has
**never fired in production** — two golden cold imports occurred since deploy
(2026-07-10, e.g. `agent-vm-golden-33df2ec74f6a` for `sha-01bf2c9`) but no VM
create coincided with an import window (0 `deferring VM create` in controller
logs, 8 VM creates all against warm goldens). The deployed fix is
unit-verified + rollout-verified, not incident-verified. Coverage inventory and
the manual live-smoke runbook:
[`../../tests/golden_cold_import_provisioning_validation.md`](../../tests/golden_cold_import_provisioning_validation.md).

**Mechanism refinement discovered during implementation:** Findings A and B share
one root — the controller's `handle_create` *blocked up to 900 s* inside
`_ensure_golden` before creating the VM. While attempt N's handler was parked in
that wait, the orchestrator recycled and issued attempt N+1; when the golden
finally succeeded, the stale and fresh handlers raced to create the same VM and
the loser's 409 was published as `failed`. Making create non-blocking dissolves
the race *and* provides the `waiting_golden` signal the dispatcher needs — one
change fixes both. (The 10:05:05 `created` status in the timeline below was in
fact attempt 1's stale handler completing its registry fallback, and the 10:17:56
`failed` was attempt 2's stale handler hitting AlreadyExists against attempt 3's
VM.)

Surfaced while watching the first VM
provisioning round after the "VMs not reachable" fix
([`vm_ssh_readiness_probe_unroutable_from_orchestrator.md`](../issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md))
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
[`vm_ssh_readiness_probe_unroutable_from_orchestrator.md`](../issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md)
— its F3 (park → fail the job) is what let the loop advance here; its no-tailnet-route root
cause is why Finding C's reaper snapshot fails. ·
[`../done/deleted_job_orphans_workspace_pod.md`](deleted_job_orphans_workspace_pod.md)
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

## Chosen fix (BUILT 2026-07-09)

**Principle: never block a create handler on a shared import, and never spend
boot budget on a wait that isn't a boot.** The controller reports golden state
honestly; the orchestrator polls patiently on a *golden* budget and keeps the
*boot* budget for actual boots.

### F1 — Controller: non-blocking golden check + `waiting_golden` status

`_do_create` (`vm/controller/controller.py`) calls the new
`_golden_state_nowait(image)` instead of the blocking `_ensure_golden` (which
remains, but only for the `_prewarm_golden` background task):

- Golden DV `Succeeded` → `(name, None)` → clone rootdisk from it (unchanged fast
  path).
- DV importing / just-created / `Failed`-and-recreated → return
  `{"status": "waiting_golden", golden, golden_phase, golden_progress}`
  **without creating the VM** (and before minting a Headscale auth key, so polls
  don't churn keys).
- Golden DV create rejected (CDI infra error, non-409) → `(None, None)` → legacy
  registry fallback, byte-for-byte pre-golden behaviour.

### F2 — Controller: plain 409 AlreadyExists is idempotent success

In the VM-create retry loop, a 409 whose body is not "is being deleted" now logs
`idempotent create` and returns `created` instead of raising. The VM name is
deterministic (`agent-vm-<job_id>`), so an existing live VM IS this job's VM. This
is the direct fix for §B — the propagated 409 that parked both jobs.

### F3 — Orchestrator: `VM_GOLDEN_POLL` / `VM_PARK_GOLDEN` decision states

`dispatch_guards.vm_provisioning_decision` gains a `waiting_golden` branch: poll
while `now - golden_wait_started_at ≤ golden_timeout_s`
(`VM_GOLDEN_WAIT_TIMEOUT_S`, default 2700 s > observed ~30 min import), park past
it. The dispatcher (`main.py`):

- `VM_GOLDEN_POLL` → stamp `golden_wait_started_at` (once), re-issue
  `create_vm(fresh=False)` as the poll — **no `provision_attempts` increment**
  (attempts bound VM boots; polling is not a boot), no fresh-context reset (the
  golden anchor must survive), no `provisioning` status overwrite (status stays
  `waiting_golden` between controller responses), only `provisioned_at` rolls
  forward so the boot budget starts ≈ when the golden completes and the VM is
  actually built.
- `VM_PARK_GOLDEN` → fail the job with the truth: *"golden image import did not
  complete within Ns (golden <name>, last progress <p>) — VM never created"* —
  not the misleading "provisioning exhausted after N attempts".

`nats_bridge` passes `golden`/`golden_phase`/`golden_progress` through to
`context.vm`; `_fresh_provision_ctx` clears `golden_wait_started_at` on genuine
re-provisions.

### F4 — Reaper: skip futile snapshot wait for unroutable VMs (§C)

`VMInstanceManager.attempts_exhausted` returns true immediately when the
instance's `ssh_host` is a tailnet address the orchestrator provably cannot reach
(`ssh_helpers.orchestrator_can_reach`, honoring `ORCHESTRATOR_HAS_TAILNET_ROUTE`)
— the dirty-unreachable reap force-deletes on the first tick instead of waiting
out `WORKSPACE_SNAPSHOT_MAX_ATTEMPTS × tick` (~5 min) on snapshots that cannot
succeed. Missing/hostname/pod-network hosts keep the bounded-attempt behaviour.

### Rollout / version skew

No flag-day. Orchestrator (develop pipeline) and controller (VM-cluster Fleet
bundle) can roll in either order:
- **New orchestrator + old controller**: the old controller still blocks in
  `_ensure_golden` and never emits `waiting_golden` → dispatcher behaves exactly
  as today.
- **New controller + old orchestrator**: `waiting_golden` lands in `context.vm`;
  the old decision logic treats it as generic not-ready → WAIT/RECYCLE as today
  (cold-import jobs still fail after 3 attempts until the orchestrator side
  lands, but the delete/recreate churn is now harmless — no stale blocked
  handlers, no 409 race).
Full benefit requires both; ship together on the same develop push.

### Not built (recorded)

- **Pre-warm gate on image rollout**: with F1/F3, dispatching into the cold
  window is harmless (jobs wait on the golden budget instead of failing), so
  holding dispatch is no longer needed.
- **Thread VMs**: `create_thread_vm` still uses the fresh path only; a thread
  provisioned during a cold import will see `waiting_golden` in its metadata and
  time out at the session-attach layer as before (no worse than the old blocking
  behaviour — the controller now just doesn't hold its handler hostage). Wire
  thread-side polling if VM-backed sessions start mattering during image bumps.

## Test plan (written, green)

- `test_vm_controller.py` — `TestGoldenStateNowait` (Succeeded/importing/absent/
  409-racer/infra-error/Failed-recreate, never sleeps), `TestDoCreateWaitingGolden`
  (defers VM create + Headscale key, publishes telemetry; Succeeded → clone
  source; infra error → registry fallback), idempotent-409 test replaces the old
  409→failed assertion.
- `test_dispatch_guards.py` — `TestVmGoldenWaitDecision`: polls within budget,
  polls before anchor stamped, **outlives the boot budget without recycling**,
  **ignores attempt exhaustion**, parks past the golden budget.
- `test_vm_provisioner.py` — `TestGoldenPollCreate`: `fresh=False` merges only
  `provisioned_at`, passes `set_provisioning=False`; fresh default resets ctx
  (incl. `golden_wait_started_at`).
- `test_nats_bridge.py` — golden telemetry passthrough; poll skips the
  `provisioning` status overwrite but still publishes.
- `test_lifecycle_vm_manager.py` — `TestAttemptsExhausted`: counter semantics for
  routable/missing hosts, instant exhaustion for unroutable tailnet hosts,
  escape-hatch env restores counter behaviour.

## Acceptance criteria

- A VM job dispatched while its golden DV is `ImportInProgress` **waits** (does not consume
  provision attempts; `context.vm.status='waiting_golden'` with import progress visible)
  and its VM create issues within one dispatcher tick (~30 s) of the golden reaching
  `Succeeded` — demonstrated on the next `agent-vm-base` image bump.
- No `409 AlreadyExists` appears in the dispatcher VM path; a re-provision after a
  controller-reported `failed` tears down the old VM object first.
- An orphaned VM the orchestrator cannot reach is force-deleted within one reconciler tick
  (no multi-minute `snapshot attempts exhausted` stall).
- Documented expectation: an `agent-vm-base` image rollout costs at most a brief pause of
  new VM dispatch (golden pre-warm), **not** a failed loop iteration.
</content>
</invoke>
