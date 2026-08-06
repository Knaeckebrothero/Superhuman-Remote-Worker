---
tags:
  - issue
  - fix-spec
  - regression
  - jobs
  - workspace-lifecycle
  - vm-backend
  - loop
  - dispatcher
---

# Investigation — VM SSH-readiness gate probes from a vantage with no tailnet route → 100% of VM jobs wedge in `created`


**Closed by the 2026-08-06 doc-truth sweep (batch #3):** Shipped `ccff70e9` the day of filing (the Status line's 'uncommitted' went stale) — F1-F4 verified at HEAD; tests/test_nats_bridge.py 134/134 green.

**Status: IMPLEMENTED (2026-07-09, uncommitted on `develop`) — root cause confirmed
live, fix chosen and built per this doc.** F1 (evidence swap in `nats_bridge`, TCP probe
deleted), F2 (daemon `ssh_ready` self-report + re-register-on-flip), F3
(`_fail_vm_parked_job` on both PARK_EXHAUSTED and the VM_PARKED healing branch), F4
(tailnet-skip guards: `_seed_vm_ide_config`, `capture_vm_snapshot`,
`stream_extract_snapshot`; shared `is_tailnet_addr`/`orchestrator_can_reach` in
`ssh_helpers`, env escape hatch `ORCHESTRATOR_HAS_TAILNET_ROUTE=true`). 552 tests green
across affected + adjacent suites, ruff clean. Remaining: deploy → live smoke
(§Acceptance criteria) → unwedge `e0f06bc3` (self-heals via F3 on first dispatcher tick).
This is a **regression introduced by the Part-1 gate** of
[`../done/vm_ready_signal_precedes_ssh_reachability.md`](../done/vm_ready_signal_precedes_ssh_reachability.md)
(commit `96c33654`, first deployed to the main cluster as `sha-69a4da6` at
2026-07-08 19:04Z). That doc's §B2 claim — "the orchestrator reaches `100.64.x` via a
stable, shared node/subnet route" — is **false and was never true**; this doc corrects it
and replaces the orchestrator-vantage probe with evidence from vantages that exist.

**One-line:** the SSH-readiness probe that gates `context.vm.status="ready"` runs `ssh`
from the **orchestrator container**, which is not a tailnet node and has **no route to
`100.64.0.0/10` at all** — so the gate can never pass. Every VM-backend job now boots
3 healthy VMs (~10 min each, all torn down), exhausts `VM_PROVISION_MAX_ATTEMPTS`, and
parks **forever in `status='created'` with `error_message` NULL** — an invisible wedge
that stalled the RSI loop at iteration 20 for ~11 h.

**Severity: loop-killer** (unlike the prior incident, which self-healed). 100% of
VM-backend jobs on the main cluster fail since the 19:04Z rollout; the same fate awaits
`srw-prod-private` whenever this code reaches `main`. Each wedge also burns 3 VM boots
against the shared VM cluster and holds the loop's `current_job` non-terminal, which the
loop cannot survive (it tolerates `failed`, not `created`-forever).

**Related (read these first):**
[`../done/vm_ready_signal_precedes_ssh_reachability.md`](../done/vm_ready_signal_precedes_ssh_reachability.md)
— authored the gate this doc corrects; its Part 2 (agent-vantage connect budget) is
**unaffected and remains required**. ·
[`agent_fast_freeze_on_dead_workspace.md`](agent_fast_freeze_on_dead_workspace.md)
(the classifier Part 2 tunes) ·
[`snapshot_restore_dead_for_jobs.md`](snapshot_restore_dead_for_jobs.md) /
[`ide_settings_sweeper_probes_stale_workspace_endpoints.md`](ide_settings_sweeper_probes_stale_workspace_endpoints.md)
(orchestrator-vantage SSH consumers — §Root cause C shows they have *always* silently
failed for VM targets).

---

## The incident (confirmed)

Main cluster (`superhuman-remote-worker` ns), RSI loop for project `68137e29`
("Hotel Rheinland ERP", MiniMax). Loop iteration-20 DEVELOPER job
`e0f06bc3-fdca-45ff-bf11-f4406d19003c` (created 2026-07-08 19:19:29Z,
`config_override.workspace.backend="vm"`).

**Trigger correlation is exact.** The probe commit `96c33654` landed 11:22Z; the prior
deploy (`sha-2c656c8`, 10:20Z) predates it; `sha-69a4da6` (19:03Z commit `b5afccc5`,
pods restarted **19:04Z**) is the first image carrying the gate. Loop iterations 12–19
ran VM jobs successfully all day **before** 19:04 — none of their `context.vm` rows have
any probe fields (they never met the gate). The first VM job probed after the rollout is
the one that wedged, 15 minutes later.

Three provisioning attempts, all identical (orchestrator + vm-controller logs):

| Attempt | VM created | Daemon registered (NATS) | SSH probes | VM deleted |
|---|---|---|---|---|
| 1 | 19:19:42 | (within window) | all timeout | 19:29:43 |
| 2 | 19:30:12 | (within window) | all timeout | 19:40:13 |
| 3 | 19:40:42 | 19:44:17 (~3.5 min boot) | **41 attempts, all `Connection timed out`**, gave up 19:54:27 | 19:50:43 |

Attempt 3's VM was demonstrably **healthy**: registered on the tailnet
(headscale node id 7714, IP `100.64.23.180`), management daemon heartbeating over NATS
until teardown (`last_heartbeat` 19:50:18). Only the orchestrator→tailnet path was dead.
After attempt 3: `context.vm = {status: "failed", error: "provisioning exhausted after 3
attempts (never reached 'ready')"}`, and the dispatcher logs
`Dispatcher: job e0f06bc3… parked — VM provisioning failed …; clear context.vm to retry`
(`main.py:3964` VM_PARKED branch) **every 30 s, forever**. The job row: `status='created'`,
`assigned_agent_id` NULL, `freeze_data` NULL, `error_message` NULL — invisible in every
job list that surfaces errors.

Note: "clear `context.vm` to retry" (the log's own advice) does **not** recover — it
re-runs 3 healthy boots against a gate with a 0% pass rate and re-parks.

---

## Root cause (confirmed live)

### A — The probe vantage does not exist

`nats_bridge._probe_vm_ssh_ready` (`orchestrator/services/nats_bridge.py:592`) shells
`ssh` via `wait_for_agent_ssh` (`orchestrator/services/ssh_helpers.py:90`) **from the
orchestrator container**. Verified live from that container (2026-07-09):

- TCP to two **currently online** tailnet nodes (`100.64.23.182:22` — a live agent
  sidecar; `100.64.4.105:3901` — garage1) → **timed out**.
- TCP to the LAN (`10.0.50.104:22`) → connects. So egress works; `100.64.0.0/10`
  specifically black-holes (packets leave via the default route and die — which is why
  the prior doc's "timeout ⇒ shared route exists" inference was wrong).
- `headscale nodes list`: the only tailnet members are garage nodes/proxies and the
  **per-agent-pod tailscale sidecars**. **No k8s node, no subnet router, no
  orchestrator.** There is no path, and no member that could provide one.

The gate therefore has a **0% pass rate by construction** on this topology. It was
unverifiable everywhere it was developed (k3d/dev have no tailnet VM path) and shipped
with its core network assumption unasserted.

### B — The park leaves the job in a state nothing will ever advance

`VM_PARK_EXHAUSTED` (`main.py:3882`) writes the error into `context.vm` **only** — the
job keeps `status='created'`, `error_message` NULL. `VM_PARKED` (`main.py:3964`) then
skips it every tick. Contrast the no-provisioner branch directly below (`main.py:3905`
VM_PROVISION → "VM provisioner is not available"), which correctly calls
`update_job_status(status="failed", error_message=…)`. The park path violates the
liveness invariant the loop depends on: **no dispatcher decision may leave a job in
`created` with nothing scheduled to change its state.** The loop provably survives
`failed` iterations (iter-10 failed → iter-11 ran); it cannot survive this.

### C — Corollary: orchestrator-vantage SSH to VM targets has *always* silently failed

The ide-settings seed (`nats_bridge.py:681` → `services.ide_settings.
seed_ide_config_for_user`) and the pre-release snapshot (`services/snapshot_service.py`)
SSH from the orchestrator to the VM's tailnet IP — they have been timing out, non-fatally
and quietly, for **every** VM-backed job since the VM backend existed. This also means
the iter-10 investigation's "three independent SSH consumers timed out" evidence
double-counted two consumers that can never succeed; the only valid-vantage failure in
that incident was the agent clone itself.

### D — Bonus hole: "daemon registered" doesn't even guarantee a tailnet IP

The daemon waits max 60 s for tailscale and then **registers with the QEMU NAT fallback
IP anyway** (`docker/agent-vm-base/files/management-daemon.py:446`), re-registering later
when tailscale comes up (`:366-382`). Pre-gate code would set `ready` and inject an
unroutable NAT IP into the job in that window. Any replacement evidence must close this
too.

---

## Chosen fix

**Principle: readiness evidence must come from a vantage that actually has the path.**
Three real vantages exist — the VM itself (can prove local sshd + tailnet IP), the
vm-controller (pod-network NAT path), and the agent (the true consumer path, exists only
post-dispatch = Part 2). The orchestrator is not a vantage; its role is **authority**:
consume evidence over NATS, run the (kept, unmodified) bounded-retry state machine in
`dispatch_guards`, decide dispatch. Advisory-mode probing was considered and **rejected**
— a probe that provably cannot succeed, warning on every VM forever, is dead code that
trains operators to ignore warnings.

### F1 — Swap the gate's evidence source: daemon-reported readiness over NATS (orchestrator; ships first)

`_on_daemon_register` stops spawning the TCP probe (`_start_vm_ssh_probe` /
`_probe_vm_ssh_ready` path deleted) and promotes on payload evidence instead:

- Payload carries `ssh_ready: true` → promote to `ready` immediately (keep the
  `ssh_registration_id` conditional-merge machinery — it stays load-bearing against
  stale-incarnation races).
- Payload carries `ssh_ready: false` → write `ssh_pending`; promote when a re-register /
  heartbeat flips it true.
- **Legacy payload (no `ssh_ready` field — old golden image):** `ssh_host` in
  `100.64.0.0/10` → promote (pre-gate behavior, now guarded by Part 2's agent budget);
  `ssh_host` outside it (the §D NAT fallback) → `ssh_pending` until the daemon's
  existing IP-change re-register (`management-daemon.py:366-382`) supplies a tailnet IP.
  This closes §D **without** requiring a golden-image rollout, and means F1 alone
  restores VM jobs on day one with no image coupling.

Everything else in the Part-1 state machine is kept as-is: `ssh_pending` non-dispatchable,
`dispatch_guards.vm_provisioning_decision` WAIT/RECYCLE/PARK budget, leader-gating,
`_fresh_provision_ctx` clearing probe/registration state on reprovision.

### F2 — Daemon self-report (golden image; ships second, tightens the gate)

`management-daemon.py`: before registering — and re-checked in the IP-update/heartbeat
loop, re-registering on flips — set `ssh_ready` = (local sshd accepts on `127.0.0.1:22`)
AND (held IP is a tailnet `100.64.x` address). The daemon cannot observe peer netmap
propagation; that residual (measured O(seconds) — fresh sidecar reaches Running+DERP in
~3 s) is exactly what Part 2's agent-vantage budget covers. Build via `docker/agent-vm-base`.

### F3 — Park must fail the job (orchestrator; ships with F1)

- `VM_PARK_EXHAUSTED` (`main.py:3882`): in addition to the `context.vm` merge, call
  `update_job_status(status="failed", error_message="VM provisioning exhausted after N
  attempts (never reached 'ready'): <last vm error>")` — mirroring the no-provisioner
  branch. Visible in cockpit/API; the loop's existing failure handling advances the
  iteration.
- `VM_PARKED` (`main.py:3964`): if the job is still non-terminal, apply the same
  fail-with-error (healing branch — covers controller-callback races **and retroactively
  unwedges `e0f06bc3` on the first dispatcher tick after deploy**).
- Manual-retry affordance changes accordingly: a failed VM job is retried by clearing
  `context.vm` *and* resubmitting/re-queuing — document in the log message.

### F4 — Stop silently failing orchestrator-vantage SSH consumers (ships with F1)

`_seed_vm_ide_config` → `seed_ide_config_for_user` and the snapshot capture/restore
paths (`snapshot_service`, `ssh_helpers.stream_extract_snapshot`) must **explicitly skip
VM/tailnet targets with one visible log line** ("orchestrator has no tailnet route — IDE
seed/snapshot not supported on VM backend") instead of timing out quietly. This is the
honest current state; making these features real on VMs is a separate decision (§F5).

### F5 — Out of scope, recorded: orchestrator tailnet membership + boot canary

If orchestrator-vantage SSH to VMs is ever actually wanted (VM-backed sessions,
snapshots), give the orchestrator pod a tailscale sidecar (agent pattern,
`agent_provisioner.py:1283`) — and then **assert the path at startup** (TCP canary to a
known tailnet peer, refuse loudly on failure). General guardrail from this incident: any
component that assumes a network path must assert it at boot, not discover it on the
first job.

**Unchanged:** Part 2 (agent-vantage first-connect budget — it is the only check on the
path that actually clones), the F29 no-git-init-fallback hard-fail, container backend
retry behavior.

---

## Rollout / sequencing

1. **PR 1 (orchestrator-only): F1 + F3 + F4.** No image coupling — legacy golden images
   immediately work again via the tailnet-IP compat rule. Deploys via the normal develop
   pipeline; on the first dispatcher tick, F3's healing branch fails `e0f06bc3` and the
   loop advances. (If the loop is wanted sooner, manually `UPDATE jobs SET
   status='failed', error_message='VM provisioning exhausted (orchestrator probe
   unroutable — see docs/issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md)'
   WHERE id='e0f06bc3-…'`.)
2. **PR 2 (golden image): F2.** Rolls at its own pace; nats_bridge already consumes
   `ssh_ready` from PR 1, so there is no flag-day between image and orchestrator versions.
3. **`srw-prod-private`:** the gate reaches `main` only together with this fix (same
   branch), so prod never sees the wedge.

## Test plan

- `tests/test_nats_bridge.py`:
  - legacy register (no `ssh_ready`) with `100.64.x` host → promoted `ready`, **no probe
    task spawned**; with NAT-fallback host → `ssh_pending`, not promoted; follow-up
    re-register with tailnet IP promotes the same registration.
  - `ssh_ready: false` → `ssh_pending`; flip to `true` via re-register/heartbeat →
    promoted exactly once (idempotent); stale `ssh_registration_id` → not promoted.
  - leader-gating and `_fresh_provision_ctx` reset behavior unchanged.
- dispatcher: `VM_PARK_EXHAUSTED` → job `failed` + `error_message` set;
  `VM_PARKED` with non-terminal job → healed to `failed`; terminal job → untouched.
- `tests/test_dispatch_guards.py`: semantics unchanged (pure-logic module untouched).
- `tests/test_management_daemon.py`: `ssh_ready` true only when sshd listens AND IP is
  tailnet; re-register fires on readiness flip; NAT-fallback register carries
  `ssh_ready: false`.
- IDE seed / snapshot: VM-backend target → skipped with log, no SSH subprocess spawned.
- Keep untouched+green: Part 2 budget tests (`tests/test_workspace_backends.py`), F29
  (`tests/test_workspace_git.py`).

## Acceptance criteria

- No orchestrator-originated SSH/TCP to `100.64.0.0/10` remains anywhere in the VM job
  lifecycle path (`wait_for_agent_ssh` no longer referenced from `nats_bridge`).
- Live smoke on the main cluster: next loop VM job dispatches within seconds of daemon
  register (legacy image) and completes; `context.vm` shows the evidence used.
- A genuinely-dead-VM simulation (or the next real one) ends as a **`failed` job with a
  visible `error_message` within ~3×`VM_PROVISION_TIMEOUT_S`**, and the loop advances
  past it.
- `e0f06bc3` unwedged (healed by F3 or manually) and loop `7ef1bfcf` progressing.
- Correction banner added to
  `docs/done/vm_ready_signal_precedes_ssh_reachability.md` (§B2 claim + Part-1 placement
  superseded by this doc).
