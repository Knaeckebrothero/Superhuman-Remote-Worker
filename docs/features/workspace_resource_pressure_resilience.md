# Workspace resource-pressure resilience

## Status

Designed 2026-07-13. Slice 1 (legibility) in progress. Work on `develop`.

Motivating incident: job `73e68890` ("Build frontend design mockups") ran ~2.4h
of real work (Playwright + a local HTTP server + heavy KB/memory writes),
then its **workspace pod died mid-run**. The failure surfaced as an opaque
`SSH ... Key-exchange timed out` and the job ended `failed` with `freeze_data`
NULL — 11h wall-clock gone, and a resume would have re-run straight into the
same death. Not a loop job (loop jobs get a VM by habit); a browser-class task
that landed on a default `4Gi` pod.

## Problem

A workspace pod has a fixed memory limit (default request `1Gi` / limit
**`4Gi`**, `container_provisioner.create_workspace`). A bursty browser/build
workload (Chromium + dev server + rebuilds + a model firing parallel tool
calls) spikes past the limit; the kernel answers with a hard OOM-kill (or the
kubelet evicts under node pressure). Three things then fail **independently** —
conflating them is what makes this feel unfixable:

1. **The burst kills the pod** (prevention gap).
2. **The death is illegible** — the orchestrator deletes the dead pod
   (`main.py:12680`) recording only the downstream SSH error, never reading the
   pod's `OOMKilled`/`Evicted` termination reason. A memory kill masquerades as
   a network blip.
3. **There is no recourse** — resume re-dispatches at the *same* size into the
   same grave; the agent can't ask for more, and the user has no one-click
   "give it more / move it to a VM".

## Decided constraints (portability first)

We ship a Helm chart onto other people's clusters, so nothing here may depend on
cluster-operator or node-level configuration:

- **No metrics-server dependency.** It's an add-on, not core k8s, not
  guaranteed present. Per-job utilization is read from the workspace's **own
  cgroup files over the SSH channel the agent already holds** —
  `/sys/fs/cgroup/memory.current`, `memory.max`, `memory.peak` (cgroup v2 gives
  a true high-water mark for free), and `cpu.stat`. Portable, add-on-free, and
  a real peak instead of sampled instants.
- **No node cgroup `memory.high` / swap dependency.** Soft-limiting (throttle +
  page instead of kill — the "Windows lags instead of killing" behavior) is the
  *ideal* mechanism, but it's kubelet/node-level (MemoryQoS feature gate, node
  swap) and un-shippable from a workload chart. It survives only as an **optional
  ops note** for operators who own their nodes (e.g. the homelab). Never a
  product dependency.

## Escalation ladder (decided)

`default pod → 2× memory pod (auto, once) → agent-requested VM (user-approved) → fail`

- On a **resource kill** (OOMKilled / Evicted), recovery re-dispatches the job
  at **2× memory** — one auto-step only (no unbounded pod growth that would
  destabilize a node) — and injects a note into the resumed agent's context:
  *"your workspace was OOM-killed last run; you now have 2× the memory — be
  economical, or request a VM."*
- If it OOMs **again** at 2×, stop auto-growing. The agent may issue a **VM
  request stating the cores + RAM it needs**, routed through the existing sudo
  approval gate so the **user stays the approver**. VMs have far more headroom.
- `fail` only when the VM request is declined or the ladder is exhausted —
  and by then the failure is *legible* (says "out of memory") and *actionable*.

Failing is an acceptable terminal; failing *illegibly with no recourse* is not.

## Architecture & reuse surfaces

### Layer 1 — Legibility (foundation; everything else keys off it)

- **New** `ContainerProvisioner.get_last_termination(owner)` — read the Failed
  pod's `container_statuses[].state/last_state.terminated.{reason,exit_code,
  signal}` + pod `status.reason` (`Evicted`). Mirrors the agent-pod reap
  classifier at `agent_provisioner.py:689-707`; same `read_namespaced_pod`
  shape as `workspace_status` (`container_provisioner.py:576-595`).
- **New** `completion.classify_workspace_death(term) -> (human_reason,
  is_resource_kill)` — policy helper next to `is_teardown_infra_error`.
- **Wire**: in the pod-recovery arm (`main.py:12665`), call it **before**
  `delete_workspace` (`:12680`); stamp `termination` + `death_reason` onto the
  `workspace_container` context and use `death_reason` for the job's
  `error_message` instead of the raw SSH string. Also carry it into the
  fail-loud branch (`:12592`).

### Layer 2 — Per-job utilization metrics

- The agent heartbeat already carries a `metrics` dict (`orchestrator_client.py:923`,
  loop at `:989`). Each heartbeat, sample the workspace cgroup via the existing
  `RemoteBackend` SSH (`src/core/backends/remote.py`), attach
  `{mem_current, mem_peak, mem_max, cpu_usage}`.
- Orchestrator's `/api/agents/{id}/heartbeat` handler records a per-job
  high-water mark on `job.context` (`merge_job_context`). Distinct from
  `workspace_metering.py` (bills on *requests*, not usage).
- Cockpit workspace panel: show live mem/cpu vs limit + the recorded peak.

### Layer 3 — Escalation

- **2× pod on resource-kill recovery**: read current size from context, pass
  scaled `memory`/`memory_limit` into `create_workspace` on re-dispatch; gate on
  `is_resource_kill` so only proven-hungry jobs grow. One auto-step.
- **Context note** injected on the resumed run (dispatch-time context inject).
- **Agent VM-request tool** (sudo-gated): reuse `sudo_gate.insert_vm_upgrade_request`
  (`request_type="vm_upgrade"`, `:577`) + `nats_bridge.request_vm_create(cpu_cores,
  …)` (`:219`). Cockpit "continue on a VM" action for the user side.

### Layer 4 — Cheap prior: workload-class defaults

Key the *baseline* workspace size off the expert/tooling profile: a
developer/browser expert starts bigger (or on a VM); a scholar/research expert
stays lean. Reduces how often escalation fires. The right signal is workload
class, not loop membership.

## Slices & acceptance criteria

- **S1 — Legibility.** `get_last_termination` + `classify_workspace_death`
  (unit-tested) + recovery-arm wiring. **AC:** a workspace OOM produces a job
  `error_message`/context that says "out of memory (OOMKilled)", verified on k3d
  by capping a workspace at a low memory limit and driving it OOM.
- **S2 — Metrics.** cgroup sampling in the heartbeat + per-job peak stored +
  Cockpit panel. **AC:** the job page shows mem peak vs limit; a near-OOM job
  reads ~limit.
- **S3 — 2× auto-recovery + note.** **AC:** a job OOM-killed at `4Gi`
  re-dispatches at `8Gi` with the context note; a second OOM does *not* auto-grow
  again.
- **S4 — Agent VM request + Cockpit VM action.** **AC:** agent tool opens a
  `vm_upgrade` sudo request; user approval provisions the VM and resumes.
- **S5 — Workload-class default sizing.** **AC:** a developer/browser job starts
  above `4Gi` without manual intervention.

## Out of scope

- Node `memory.high`/swap (ops note only, per constraints).
- Live chat-session workspace auto-recovery (separate design).
- Changing the `WORKSPACE_RECOVERY_MAX_ATTEMPTS` semantics beyond carrying the
  legible reason.

## Related

- `docs/issues/agent_fast_freeze_on_dead_workspace.md` (the detection path this
  builds on — implemented)
- `docs/issues/agent_workspace_pod_resource_headroom.md` (the root-cause family)
- `docs/issues/loop_job_workspace_lost_wedged_in_recovery.md` (recovery consumer)
- `docs/done/coincident_infra_error_overrides_reported_job_outcome.md`
