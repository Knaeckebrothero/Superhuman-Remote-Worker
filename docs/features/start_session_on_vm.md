# Start a session directly on a VM workspace

**Status:** Proposed (plan — not yet built)
**Date:** 2026-06-26
**Related:** `project_workspace_tier_upgrade.md`, `project_session_create_backend_dropped.md`,
`docs/features/no_workspace_agent_mode.md`,
`docs/features/configurable_default_workspace_tier.md` (separable companion — default tier vs. VM-as-pick)

## Problem

You cannot create an interactive session (persistent thread) directly on a VM
workspace. Selecting **VM (QEMU)** in the New Session "Backend" dropdown returns
an immediate HTTP 400 and no thread row is ever created.

### Live evidence (dev, `--context main`, ns `superhuman-remote-worker`, 2026-06-26)

```
19:58:29  POST /api/persistent/threads  400 (9ms)
19:59:06  POST /api/persistent/threads  400 (4ms)
```

- 4–9 ms ⇒ pure validation; nothing reached provisioning, NATS, or the VM cluster.
- No `threads` row was written (newest row predates the attempt by a day) — the
  400 fires *before* the DB insert.
- **Not an HA artifact and not a permissions artifact.** Both replicas run this
  validation identically; the rejection is deterministic and precedes the grant
  check. For an admin, `canUseVm()` (`is_admin || can_use_vm`) is already true, so
  the self-granted `can_use_vm` capability changed nothing.

### Root cause

`_validated_session_workspace_override()` (`orchestrator/main.py:2742`) hard-rejects
`backend == "vm"` at session-create:

```python
if backend == "vm":
    raise HTTPException(status_code=400, detail=(
        "VM workspaces can't be selected at session creation; start the "
        "session on the Virtual tier and upgrade it to VM."))
```

This is a **guard rail over a wiring gap**, not a fundamental limitation:

- **Worker jobs already start directly on a VM.** The dispatcher
  (`main.py:3945-3994`) checks VM permission, calls `vm_provisioner.create_vm(...)`
  with `cpu_cores`/`memory` from the override, waits for the VM to register, then
  dispatches. The session create/prepare path simply never learned the same branch.
- The thread-scoped primitive **already exists**: `vm_provisioner.create_thread_vm()`
  (`vm_provisioner.py:740`), used today by the suspend/resume path
  (`workspace_suspension.py:622`) and the sandbox→VM upgrade.

It was scoped to "upgrade-only" in v1 for two honest reasons:

1. **Latency vs. the ready budget.** A sandbox container is ready in ~8 s (observed
   in the live logs); a VM cold-boots in *minutes* (`VM_UPGRADE_POLL_TIMEOUT=900`).
   The session attach polls `wsReadyTimeoutSeconds: 180` — a VM blows past it. The
   upgrade path sidesteps this because the session is already interactive on a
   container when the VM provisions in the background.
2. **Duplicated wiring** the upgrade flow already had.

## Current architecture (the three relevant paths)

| Concern | Where | Notes |
|---|---|---|
| Create validation | `main.py:2721` `_validated_session_workspace_override` | rejects `vm` (400) |
| Create grant PEP | `main.py:~3130` `_enforce_session_create_grants` → `_check_vm_permission` (`main.py:3151`) | **already** validates `workspace.backend` against grants + the `vm_workspaces.enabled` kill-switch once VM is unblocked |
| Create provision fork | `main.py:15154-15211` | branches: lite (`virtual`/`none`, no pod) → k8s container → docker → manual. **No VM branch.** |
| VM provisioner | `vm_provisioner.py:740` `create_thread_vm` | routes via NATS/HTTP/direct-K8s; writes `threads.metadata.vm` |
| VM register callback | `nats_bridge.py:448` `_on_daemon_register` | daemon reports Tailscale IP → `status=ready, ssh_host, ssh_port` |
| Upgrade endpoint (reference) | `main.py:14422` `POST /api/agents/threads/{id}/upgrade-to-vm` | grant → idempotency guard → `create_thread_vm` |
| Agent upgrade driver (reference) | `persistent_app.py:4710` `_handle_workspace_upgrade` | request → `_poll_vm_ready` (900 s) → build `RemoteBackend(sudo="allow")` → seed → swap → retool → reopen sudo → persist tier; emits `workspace_upgrade.{started,progress,complete,failed}` |
| Cockpit New Session (live) | `views/session-create/session-create.component.ts:402` `createSession()` | → `agent-settings` → `advanced-accordion.getOverrides()` (`:1211-1229`) builds `workspace.backend` + `workspace.vm.{cpu_cores,memory}`; POSTs `/api/persistent/threads`. **Already sends VM correctly.** No create-vs-runtime `@Input` exists. |

## Design

### Two approaches

**Approach A — native VM at create.** Mirror the job-dispatcher branch into the
create fork: fire `create_thread_vm()` at create, and teach the agent's *initial*
connect path to poll-then-connect to the VM (no seed, no swap — there is no source
backend). Cleanest end-state, no throwaway tier.
*Cost:* net-new agent-init branch on the sensitive attach path, **plus** decoupling
"agent ready" from "workspace ready" so the 180 s WS-ready budget doesn't trip while
the VM boots.

**Approach B (recommended v1) — "pick VM" = staged base-tier → auto-upgrade.**
Honor a VM selection at create by booting the agent on a base tier and immediately
invoking the **existing, verified** `upgrade-to-vm` flow. From the user's point of
view they pick VM and land on a VM; the staging is an implementation detail.
*Reuses 100% of the verified upgrade pipeline and its progress UX* (heartbeats to
the cockpit), and the agent is interactive immediately, which sidesteps the
ready-budget problem entirely.

- **Base tier = `sandbox`** for v1: the 2026-06-21 e2e verified exactly `sandbox→VM`
  seeding, and a fresh session's workspace is near-empty so the ~8 s throwaway pod
  + trivial seed is cheap.
- **Base tier = `virtual`** (no pod, instant) is a follow-up optimization, gated on
  confirming `seed_workspace()` supports an object-store source (today's seed is
  SSH→SSH).

**Recommendation:** Approach B with a sandbox base. It delivers the user-visible
outcome ("select VM → get a VM session, interactive at once, VM attaches with a
progress bar"), reuses verified code, and avoids A's two riskiest pieces. Revisit A
/ virtual-base if the throwaway container proves annoying.

### Changes for Approach B

**Orchestrator**
1. `_validated_session_workspace_override` (`main.py:2742`): stop hard-rejecting
   `vm`. Either accept `vm` (and let the grant PEP gate it), or translate it to an
   intent: persist `config_override.workspace.backend = "sandbox"` **plus** a
   `workspace.upgrade_to = "vm"` marker (and keep `workspace.vm.{cpu_cores,memory}`).
2. Grant pre-flight: `_enforce_session_create_grants` already runs `_check_vm_permission`
   — confirm it fires on the *intended* VM tier (validate against `vm`, not the staged
   `sandbox`) so a non-permitted user is rejected loud at create (422), not after a
   container boots.
3. Auto-trigger the upgrade once the agent binds: simplest is to have the agent read
   `workspace.upgrade_to` on first connect and self-invoke
   `request_thread_workspace_upgrade(thread_id, "vm")` — reusing `persistent_app.py:4710`
   verbatim. (Alternative: orchestrator fires it post-bind.)

**Agent** (`src/api/persistent_app.py`)
4. On session init, if `config_override.workspace.upgrade_to == "vm"` and the live
   backend isn't already VM, kick `_handle_workspace_upgrade(ws, "vm")` after the
   first ready. No new provisioning logic — just the trigger.

**Cockpit** — essentially none functional (the form already emits VM + cpu/memory).
5. Add a hint under the Backend select when `vm` is chosen at create:
   *"VMs take a few minutes to boot — your session starts immediately and the VM
   attaches when ready."* (`advanced-accordion.component.ts` after the lite-hint, ~:422).

### Changes unique to Approach A (if chosen instead)

- Create fork (`main.py:~15162`): add `elif _thread_needs_vm(...)` → mark
  `metadata.vm.status=pending` + `asyncio.create_task(create_thread_vm(...))`.
- Agent init: new "poll VM then connect, no seed/swap" path.
- Decouple agent-ready from workspace-ready (raise/limit `wsReadyTimeoutSeconds` for
  VM, or report agent ready before the VM SSH connect and surface a workspace-booting
  state). This is the crux of A's risk.

## Risks & prerequisites

- **R1 — HA register-routing durability hole (shared, fix first).**
  `nats_bridge.py` subscribes to `vm.lifecycle.status.{oid}` and
  `agent.vm.{oid}.*.register` **without a queue group**, and routes thread-vs-job via
  the **per-pod in-memory set** `self._thread_vm_ids` (`:441,467,483`). At the
  `replicas: 2` default (live since 2026-06-26), the non-queue fan-out *accidentally*
  protects correctness only while the **creating** pod stays alive. If that pod rolls
  during the multi-minute VM boot, the surviving pod routes "VM ready" into
  `jobs.context.vm` instead of `threads.metadata.vm`, and `_poll_vm_ready` hangs to
  timeout → the session fails. **Fix:** resolve thread-vs-job from the DB in the
  register/lifecycle handlers (does a `threads` row exist for this id?) instead of the
  in-memory set; make the handlers idempotent under double-delivery. This is a
  reliability prerequisite for VM sessions under the new HA default.
- **R2 — Verify the existing `sandbox→VM` upgrade still works under 2 replicas**
  before building on it. The 2026-06-21 e2e predates the HA rollout. Gate the whole
  feature on a green manual upgrade under `replicas: 2`.
- **R3 — VM kill-switch + grant.** `_check_vm_permission` 403s everyone (incl. admins)
  if `system_settings['vm_workspaces'].enabled == false`. Confirm it's enabled on the
  target env; surface a clear error if not.
- **R4 — Seed source (only if virtual-base is pursued).** `seed_workspace()` is
  SSH→SSH today; `virtual→VM` needs an object-store source path or it's unsupported.
- **R5 — Failure/teardown.** If VM provisioning fails or times out after create,
  surface `workspace_upgrade.failed` and reuse `abort-vm-upgrade`
  (`main.py:14479`) + `release_thread_vm` so no orphan VM leaks.

## Verification plan (k3d, then dev)

1. **Pre-flight:** confirm `vm_workspaces.enabled` and that a manual `sandbox→VM`
   upgrade succeeds under `replicas: 2` (R1/R2 gate).
2. **Unit:** `_validated_session_workspace_override` accepts VM intent;
   `_enforce_session_create_grants` rejects a non-permitted user at create (422);
   cockpit hint renders for `vm` at create.
3. **E2E:** New Session → Backend = VM (8c/16Gi) → submit. Assert: thread row
   persists; session is interactive within seconds on the base tier;
   `workspace_upgrade.progress` heartbeats render; VM registers; backend swaps to VM;
   `sudo` works as root; teardown leaves no VM/Headscale/pod/row leak.
4. **HA E2E (the headline):** during the VM boot window, delete the orchestrator pod
   that handled create; assert the session still reaches a ready VM (proves R1 fix).

## Acceptance criteria

- [ ] Selecting VM at New Session creates a session (no 400) for a permitted user;
      a non-permitted user is rejected loud at create (422), before any pod boots.
- [ ] The session is interactive within seconds; the VM attaches with visible
      progress; final backend is `vm` with working root `sudo`.
- [ ] VM provisioning reaches the correct thread context even when the creating
      orchestrator pod is rolled mid-boot (R1).
- [ ] Failure/timeout surfaces a clear error and leaves no orphaned VM.
- [ ] Existing `sandbox→VM` upgrade and non-VM session creation are unchanged.

## Out of scope (v1)

Approach A native-at-create; `virtual`-base staging; golden-image clone for faster
VM boot (tracked separately in `project_workspace_tier_upgrade.md`).
