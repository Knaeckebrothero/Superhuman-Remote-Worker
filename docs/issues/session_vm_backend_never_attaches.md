---
tags:
  - issue
  - fix-spec
  - sessions
  - vm
  - workspace-lifecycle
---

# Issue — sessions created on the VM backend silently run on a sandbox container

**Status:** Diagnosed 2026-07-26 from live dev thread
`77b3d3e6-6dfc-422e-a8ec-a5848cb8febc`. Work on `develop`.

- **Defect 1 — FIXED** (2026-07-27, uncommitted): `VM_BACKENDS` arm in
  `ensure_session_workspace`. Unit-verified; **dev live gate not yet run.**
- **Defect 2 — open.**
- **Defect 3 — split out**, root cause unknown:
  `docs/issues/vm_guest_boots_to_emergency_shell.md`.
- **Defect 4 — open.**
- Sibling bug found while fixing 1, filed separately:
  `docs/issues/workspace_suspension_infers_tier_from_metadata_presence.md`.

**One line:** Selecting **VM (QEMU)** at session creation provisions a VM *and* a
sandbox container; the sandbox wins the readiness race by ~3.5 minutes, the
session silently binds to it, and the VM — whose guest never finished booting —
is left running and orphaned.

The user-visible symptom is a session that starts suspiciously fast and still
offers **"Sandbox container / Upgrade to VM"** in Session Settings. That label is
correct: `settings-pane.component.ts` renders the live tier from
`chat.workspaceTier()`, which is what the agent actually swapped in. There is no
UI bug here — the UI is the only honest component in the chain.

This directly contradicts `docs/features/session_create_on_vm.md`, whose create
path was implemented correctly; the regression lives in a *second* provisioning
path that the design did not account for.

## Incident timeline (dev, 2026-07-26)

Thread created with
`config_override.workspace = {"backend": "vm"}`, `session_base`, `gpt-5.6-sol`.

| Time (UTC) | Event | Source |
|---|---|---|
| 14:31:41.79 | `Published vm.lifecycle.create.srw-dev for thread 77b3d3e6…` | orchestrator `v4m25` |
| 14:31:42.05 | Headscale preauth key created for the VM | dev vm-controller |
| 14:31:42.10 | `VM created: agent-vm-77b3d3e6…` | dev vm-controller |
| 14:31:42.10 | `vm.lifecycle.status … created` — logged as **thread** on `v4m25`, as **job** on `g69f4` | both replicas |
| 14:31:43.08 | `POST /api/sessions/{tid}/prepare 202` (request_id `54e19d5366d7`) | orchestrator |
| 14:31:43.33 | **`Workspace container created: ws-thread-77b3d3e6-6df`** — same request_id | orchestrator |
| 14:31:51.45 | `Workspace container ready … @ 10.42.2.32` | orchestrator |
| 14:31:58.29 | `GET /connection 200` — session live on the **sandbox**, 17 s after create | orchestrator |
| 14:35:14 | QEMU process starts (3.5 min after create; DataVolume clone) | virt-launcher |
| ~14:36:45 | Guest gives up on `/dev/disk/by-label/BOOT` → **Emergency Mode** | guest serial console |

End state, 45 min later: session healthy on the container; `agent-vm-77b3d3e6…`
still `Running`/`Ready` in KubeVirt, guest sitting at an emergency shell,
consuming 8 vCPU / 16 GiB; `threads.metadata.vm.status` still `"created"` with no
`ssh_host`; the VM never appeared in Headscale (the three concurrent *job* VMs
did, at `100.64.0.104/106/113`).

## Defect 1 — `/prepare` provisions a sandbox container for VM sessions

**This is the decisive one.** Everything else is downstream.

`create_thread` (`orchestrator/main.py:20734-20781`) correctly forks on
`vm_session` and *skips* container provisioning, exactly as the design specifies
("exclude `vm` from the sandbox-container branch"). But the very next step in the
boot sequence undoes it:

`POST /api/sessions/{id}/prepare` → `_do_prepare`
(`orchestrator/routers/sessions.py:244`) fires
`ensure_session_workspace(thread_id, …)` unconditionally whenever the thread has
no `agent_id` — i.e. on every cold start, including the first one.

`ensure_session_workspace` (`orchestrator/services/session_provisioner.py:58-75`)
skips only lite backends:

```python
backend = _thread_backend(thread)
if backend in LITE_BACKENDS:      # frozenset({"virtual", "none"}) — factory.py:24
    ...
    return None
return await ensure_workspace(WorkspaceOwner.session(thread_id), …)
```

`vm` is not in `LITE_BACKENDS`, so a VM-tier thread falls straight through to
sandbox-container provisioning. The `/prepare` handler *does* know the thread's
backend — it reads `_thread_workspace_backend(thread)` about 40 lines later to
size the readiness budget — it simply never consults it before reconciling the
workspace.

**Fix:** teach `ensure_session_workspace` that `vm` owns its own workspace. It
must not create, restore, or drift-probe a sandbox container for a VM-tier
thread. Options, in preference order:

1. Add a `vm` arm to `ensure_session_workspace` that returns `None` (VM lifecycle
   is owned by `vm_provisioner` / `metadata.vm`, not `workspace_container`).
   Centralizing it here fixes `/prepare`, resume, and the periodic sweep in one
   place — the same reason `LITE_BACKENDS` is handled here rather than at each
   call site.
2. Do **not** gate at the `/prepare` call site only; resume and reconcile reach
   the same function and would drift apart.

**Cleanup note:** deleting the leaked pod by hand is not sufficient and not
durable. The periodic `reconcile_session_workspaces` sweep is narrower than it
first looks — `list_threads_needing_workspace`
(`orchestrator/database/postgres.py:5071`) requires
`metadata->'workspace_container' IS NOT NULL` and a non-progressing status, so it
will not spontaneously create a container for a thread that never had one. The
re-creation risk is the **drift probe inside `ensure_workspace`**, which
deliberately recreates a `'ready'`-but-dead pod on the next prepare/resume. So a
VM thread that has already been polluted with a `workspace_container` entry will
regrow the pod on its next cold start until Defect 1 is fixed.

## Defect 2 — the readiness poll lets a ready container beat a booting VM

`_poll_workspace_ready` (`src/api/persistent_app.py:6288-6388`) checks VM
readiness *first*, which reads like correct precedence but is not:

```python
if not _vm_budget_applied and vm_status in ("provisioning", "created"):
    deadline = start + max(timeout, vm_timeout)   # 900 s — extends, does not gate
    ...
if vm_status == "ready" and ws.get("vm_ssh_host"):
    return {"backend": "vm", …}

status = ws.get("status", "none")
if status == "ready" and ws.get("pod_ip"):
    return {"backend": "sandbox", …}             # ← same iteration, wins
```

Within a single poll iteration, a not-yet-ready VM falls through to the container
branch and returns `backend: "sandbox"`. The VM cold-boot budget extension only
moves the *deadline*; it never expresses "this thread is VM-tier, a container is
not an acceptable answer."

The race is not close and never will be: the container was ready in 8 s; QEMU for
the VM had not even started until 3.5 min in, because the DataVolume clone runs
first. Any VM session that also has a container will bind to the container,
100% of the time.

This is defence-in-depth behind Defect 1, and it matters on its own: it is the
layer that decides *which tier the agent actually runs on*, and today that
decision can silently contradict the tier the user paid for and the operator
gated.

**Fix:** make the poll tier-aware. When the thread's resolved backend is `vm`,
`vm_status == "ready"` is the only acceptable success; a ready container must be
ignored (and ideally logged loudly as a provisioning leak), and the poll must
time out on the VM budget with a truthful VM reason rather than silently
downgrading. The thread's backend is already available to this code path via the
workspace payload's `config_override`.

**Sequencing warning:** fixing Defects 1+2 *without* Defect 3 converts today's
"fast session on the wrong tier" into "session hangs ~15 min, then fails." That
is a more honest failure, but it is a visible regression for anyone currently
relying on VM sessions accidentally working as sandboxes. Land 3 first, or land
all three together.

## Defect 3 — the VM guest boots to an emergency shell → split out

**Moved to `docs/issues/vm_guest_boots_to_emergency_shell.md`. Root cause not
determined.**

Summary for context here: the VM that *was* created could never have been
attached to. Its guest failed `local-fs.target` on a missing
`/dev/disk/by-label/BOOT` and dropped to an emergency shell before cloud-init
ran, so `tailscale up` and `management-daemon.service` never started, no
`agent.vm.*.register` was ever published, and `threads.metadata.vm` stayed at
`{"status": "created"}` with no `ssh_host`. KubeVirt reported the VM
`Running`/`Ready` throughout — those conditions describe the VMI process, not the
guest.

It is split out because it is a KubeVirt/golden-image problem with an open root
cause, while Defects 1, 2 and 4 are orchestrator-side and independently fixable.
Two consequences carry back here:

- **Defect 2's sequencing warning stands.** With Defect 3 open, fixing 1+2 turns
  "fast session on the wrong tier" into "session waits out the VM budget, then
  fails" — the intended honest failure, but a visible change.
- **The never-registered-VM reaper proposed in that doc is the missing guard**
  that would have made this incident self-reporting instead of a silent 45-minute
  resource leak.

## Defect 4 (latent, not triggered here) — thread↔job VM routing is process-local under HA

Found while tracing Defect 3; it did not cause this incident, but it will cause a
similar one.

`nats_bridge._thread_vm_ids` (`orchestrator/services/nats_bridge.py:85`) is an
**in-process `set`**, populated only on the replica that publishes
`vm.lifecycle.create` (line 255). It is the sole discriminator for routing VM
callbacks to `threads.metadata.vm` versus `jobs.context.vm` (lines 502/506,
577, 719/724).

Dev runs **2 orchestrator replicas**. The subscriptions are plain fan-out (no
queue group), so both replicas see every event and disagree about what it is —
visible verbatim in the incident logs at 14:31:42:

```
v4m25:  VM lifecycle status for thread 77b3d3e6…: created
g69f4:  VM lifecycle status for job    77b3d3e6…: created
```

For `vm.lifecycle.status` this is harmless: the publishing replica routes
correctly and the other one writes to a non-existent job row. But
`_on_daemon_register` — the handler that supplies `ssh_host` and flips the thread
to `ready` — is **leader-gated** (line 539). So the write only happens if the
publishing replica *is also* the leader:

| Publisher | Leader | Outcome |
|---|---|---|
| replica A | replica A | correct (this incident, by luck — `v4m25` was both) |
| replica A | replica B | leader has no entry in `_thread_vm_ids` → treats the thread UUID as a job → writes `ssh_host` into `jobs.context.vm` for a row that does not exist → **thread stays `created` forever** |

That is a coin flip per VM session. Any orchestrator restart also drops the set
entirely, orphaning every in-flight VM session it had published.

**Fix:** stop inferring entity type from process memory. The entity kind is
already durable — resolve it per event (thread row vs job row lookup, or an
`entity_type` field echoed by the controller in the payload it already carries),
and drop `_thread_vm_ids`. Worth confirming whether the leader gate on
`_on_daemon_register` is still load-bearing once routing is durable.

## Scope

- **Defect 1:** small and well-contained — one arm in
  `ensure_session_workspace`, plus tests asserting a `vm`-tier thread provisions
  no container via prepare, resume, or reconcile.
- **Defect 2:** small — tier-aware success condition + truthful timeout in
  `_poll_workspace_ready`; test the "ready container + booting VM" matrix
  explicitly, since that is the exact case that regressed.
- **Defect 3:** split out to `docs/issues/vm_guest_boots_to_emergency_shell.md`
  — root cause unknown, investigation first. The never-registered-VM reaper
  proposed there is independently scoped and small.
- **Defect 4:** medium — touches VM callback routing for both jobs and threads;
  needs care not to regress the working job path.

## Verification

Local k3d cannot exercise any of this (no VM provisioner — VM session create
returns a clean 503 there), so the gate is the dev `main` cluster:

1. Create a session with **VM (QEMU)**. Assert **no** `ws-thread-*` pod is
   created for that thread.
2. Assert the session does not go live until `metadata.vm.status = "ready"`, and
   that the agent's swapped backend reports tier `vm` (Session Settings shows
   the VM tier, **not** "Upgrade to VM").
3. Assert the guest reaches multi-user and registers — Headscale shows
   `vm-<thread-id>` online with a `100.64.0.x` address.
4. Kill/stall a guest deliberately and assert the never-registered reaper marks
   the thread's VM failed and surfaces a truthful error instead of hanging.
5. End the session; assert the VM is reaped.

## Related

- `docs/features/session_create_on_vm.md` — the design this violates; its create
  path is correct, its verification step appears never to have been run live.
- `docs/issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md` — why
  readiness evidence must come from the in-VM daemon, which is precisely the
  signal Defect 3 destroys.
- `docs/issues/vm_upgrade_pause_workspace_reaped_before_approval.md` — sibling
  VM-lifecycle race.
- The sandbox→VM **upgrade** path (`_handle_workspace_upgrade`,
  `src/api/persistent_app.py:6430`) is a different code path and is not affected
  by Defects 1-2 — but it provisions from the same golden image, so Defect 3
  would break it too.
