# Start a persistent session directly on a VM backend

## Problem

The New Session form (cockpit Advanced → Workspace → Backend) offers a **VM
(QEMU)** option to any user who may use VMs (`is_admin || can_use_vm`), and the
accordion already emits the correct payload
(`config_override.workspace = {backend: "vm", vm: {cpu_cores, memory}}`). But
the server rejects it: `create_thread` runs the request through
`_validated_session_workspace_override`, which raises **HTTP 400** for
`backend == "vm"` with _"VM workspaces can't be selected at session creation;
start on the Virtual tier and upgrade."_

That denial was written when session-create genuinely had no VM-provisioner
wiring, and VM was reachable only by the sandbox/lite → VM **upgrade** path. It
has since become a miscommunication with the UI: the option is offered, the user
selects it, and gets _"The request couldn't be completed."_ (the observed
`POST /api/persistent/threads → 400`).

## What already exists (reused, not rebuilt)

The session **→ VM upgrade** path (`agent_upgrade_thread_to_vm`) already proves
every hard piece end to end:

- **Provisioner**: `vm_provisioner.create_thread_vm(thread_id, …)` routes to
  `threads.metadata.vm` via NATS / HTTP / direct-k8s.
- **Agent attach**: the agent's `_poll_workspace_ready` (persistent_app.py)
  checks `vm_status` first, and on `vm_status == "ready"` injects
  `{backend: "vm", remote: {host, port, username: "agent-host", key_path,
  workspace_path}}` and hot-swaps its backend.
- **Workspace endpoint**: `agent_get_thread_workspace` already surfaces
  `vm_status / vm_ssh_host / vm_ssh_port / vm_name` from `metadata.vm`.
- **Reaping / idle-suspend**: thread-scoped VM lifecycle (`release_thread_vm`,
  `delete_thread_vm`) already covers `metadata.vm`.

So the whole job is on the **create** path plus the readiness **budgets**.

## The gaps and the fix

### 1. Server accepts VM at create (orchestrator/main.py)

- `_validated_session_workspace_override` — stop 400-rejecting `vm`; validate
  against a new `SESSION_CREATE_WORKSPACE_BACKENDS = SESSION_WORKSPACE_BACKENDS +
  ("vm",)`. VM stays **out** of `SESSION_WORKSPACE_BACKENDS` so it is never an
  *implicit* or *saved* default (cost): a VM session must be an explicit
  per-session choice. Unknown backends still 400. Sizing
  (`workspace.vm.{cpu_cores,memory}`) carries through the existing merge.
- `create_thread` — when the resolved backend is `vm`, add the operator gate
  `_check_vm_permission(user, job_needs_vm=True)` (global `vm_workspaces`
  kill-switch + per-user `can_use_vm`; admins bypass) on top of the
  `vm_workspace` PDP grant that `_enforce_session_create_grants` already runs,
  and fast-fail **503** when `vm_provisioner` is unavailable — mirroring the
  upgrade path exactly.
- `create_thread` provisioning fork — exclude `vm` from the sandbox-container
  branch and add a VM branch that **synchronously** sets
  `metadata.vm.status = "provisioning"` (so the agent's attach poll observes a
  VM in flight instead of bailing "no workspace"), then fires
  `create_thread_vm(...)` fire-and-forget with the requested sizing. The agent
  pod is still provisioned by the unchanged `provision_or_assign` path — it runs
  the LangGraph agent and SSHes into the VM.

### 2. Three nested readiness budgets sized for a cold VM boot

A cold KubeVirt VM pays a ~2.8 GB CDI import + guest boot — minutes, far past
the sandbox defaults. The signal chain is: **agent attach-poll** → **server
`wait_for_ready`** → **client `/connection` poll**, each of which must outlast
the boot, from innermost out:

| Layer | Where | Was | VM budget |
|-------|-------|-----|-----------|
| Agent attach poll | `persistent_app._poll_workspace_ready` | 120 s | `VM_UPGRADE_POLL_TIMEOUT` (900 s), self-extended when `vm_status` is in flight |
| Server ready wait | `provision_or_assign` + `_do_prepare` `wait_for_ready` | `WS_READY_TIMEOUT_S` 180 s | `VM_WS_READY_TIMEOUT_S` 960 s when backend == vm |
| Client connection poll | cockpit `_pollConnectionUntilReady` | 180 s | ~1020 s when the session is VM-backed |

Budgets are nested (agent 900 < server 960 < client 1020) so the innermost
times out first with the truthful reason, never the outer layer with a generic
one. The agent poll self-extends the moment it sees `vm_status ∈
{provisioning, created}`, so it needs no caller signal.

**Ingress is not a fourth budget.** The create `POST` returns in seconds; the
long wait is surfaced by the always-on `/api/notifications/events` SSE (which
sends a `: keepalive` every 30 s — under the ~60 s ingress `proxy-read-timeout`,
so it never idle-504s through a multi-minute boot) and by the `/connection`
poll (short 1–2 s-backoff requests, each well under the timeout). No
`proxy-read-timeout` change is required. The `504 on sessions:1` in the original
report was a secondary artifact of the failed 400 create, not a structural
blocker.

### 3. Cockpit "VM is booting" UX

`session.lifecycle` events gain a `backend` tag; the cockpit renders the
`booting` step as **"Booting VM (this can take a few minutes)"** for VM sessions
and extends its own `/connection` poll budget. The VM option itself already
shows — no gate change.

## Non-goals / known limits (v1)

- **Cold golden-image import** (~30 min, only right after an `agent-vm-base`
  image bump) still exceeds the 960 s budget; such a session fails cleanly and
  the next attempt (warm golden) succeeds — same behavior as VM *jobs*
  (`docs/done/golden_image_cold_import_fails_inflight_vm_jobs.md`); the
  job-side `waiting_golden` park is not extended to sessions here.
- **Resume of a suspended VM session**: the server budget reads the thread's
  stored backend so `_do_prepare` waits correctly; the client poll only knows
  it is a VM session once it has seen the create body or a `backend=vm`
  lifecycle event. Create (the feature) is fully covered; a longer resume-poll
  budget can follow if needed.

## Verification

- **Local (k3d, no VM provisioner)**: unit tests (validation accepts vm /
  rejects unknown; ready-timeout selection; agent poll self-extend) + manual —
  a VM session create now returns a clean **503 "VM provisioning is not
  available"** instead of the old 400, and the UI shows the VM option.
- **Dev (`main`) cluster**: create a VM session → VM boots → session attaches
  over SSH → usable; end it → VM reaped.
