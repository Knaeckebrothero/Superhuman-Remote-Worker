---
tags:
  - issue
  - fix-spec
  - sessions
  - vm
  - workspace-lifecycle
  - snapshots
---

# Issue — workspace suspension infers tier from metadata *presence*, so VM sessions never suspend

**Status:** Found 2026-07-27 while fixing Defect 1 of
`docs/issues/session_vm_backend_never_attaches.md`. **FIXED 2026-07-28** (thread
paths only — see "Job path" below). Unit-verified; live gate owed. Work on
`develop`.

**One line:** `workspace_suspension.py` decides "is this pod-tier or VM-tier?" by
asking whether `metadata.workspace_container` exists — but `_setup_gitea` writes
that key for **every** thread including VM ones, so a VM session is read as
pod-tier, and `suspend_thread_workspace` bails out before doing anything.

## Root cause

`workspace_container` is overloaded. It carries two unrelated things:

- **Git coordinates** (`git_remote_url`, `repo_name`) — written by `_setup_gitea`
  at thread create for *every* tier, VM included.
- **Pod state** (`status`, `pod_ip`, `pod_name`) — only meaningful for
  container-backed tiers.

Suspension treats presence of the key as proof of the second. For a VM thread the
key is present but holds only the first, so every presence check answers "pod".

## What actually breaks

### 1. VM session suspend is dead (primary)

`suspend_thread_workspace` (`orchestrator/services/workspace_suspension.py:464`):

```python
ws_ctx = metadata.get("workspace_container", {})   # {git_remote_url, repo_name} — truthy
vm_ctx = metadata.get("vm", {})                    # {status: ready, ssh_host: …}

ws_status = ws_ctx.get("status") if ws_ctx else vm_ctx.get("status")
...
if ws_status != "ready":
    return False        # ← every VM session exits here
```

`ws_ctx` is truthy, so the VM's real status is never consulted; `ws_status` is
`None` and the method returns `False`. **A VM-tier session can never be
suspended** — not by idle-suspend, not by any caller.

Note `ssh_host` one line above resolves *correctly*
(`ws_ctx.get("pod_ip") or ws_ctx.get("host") or vm_ctx.get("ssh_host")`), which is
why this reads as working code.

### 2. Snapshot manifests are mislabelled

`source_type = "vm" if vm_ctx and not ws_ctx else "pod"` (lines 186 and 488)
resolves to `"pod"` for VM threads. It is persisted into the snapshot manifest
(`snapshot_service.py:540`) and read back on restore
(`ide_session.py:195`, `:254`, `:448`). Latent while (1) blocks suspend outright,
but it is wrong the moment (1) is fixed — so both must be fixed together.

### 3. Status markers are written to the wrong key

`if ws_ctx: … elif vm_ctx: …` at lines 481/487, 526/530, 562/566, 606/611 routes
`suspending`/`restoring`/rollback-to-`ready` markers onto `workspace_container`
for VM threads. Transient and lower-impact than (1) and (2), but it is what
leaves a suspended VM thread with a stale `workspace_container.status =
"suspending"`.

Teardown (line 542) and restore (line 617) both branch on `vm_ctx` *correctly* —
the bug is confined to the tier-selection expressions above them.

## Interaction with the Defect 1 fix — read this first

Before Defect 1 was fixed, VM sessions were silently running on a leaked sandbox
container. That container had `status: ready`, so suspend *did* proceed — it
snapshotted the container as `source_type="pod"` and then, at teardown, deleted
the VM (line 542 branches on `vm_ctx`). Wrong, but it ran.

With Defect 1 fixed there is no container, so `ws_ctx.get("status")` is `None` and
suspend now bails deterministically. **Net effect: an idle VM session's VM is no
longer idle-suspended and will run until the session ends.** That is a resource
consequence of the Defect 1 fix that this issue is the fix for. It is not a
regression in the tier the user gets — the session is now on the correct tier —
but it should be fixed before VM sessions are used in anger.

## Fix as implemented (2026-07-28)

New module helper `_thread_is_vm_tier(metadata, ws_ctx, vm_ctx)` reads the
resolved tier from `metadata.config_override.workspace.backend` via
`is_vm_backend` (`src/core/backends/factory`). Legacy rows with no materialized
tier fall back to pod **state** (`ws_ctx["status"]`) rather than mere presence, so
a VM-upgraded thread still reads correctly. Both `suspend_thread_workspace` and
`restore_thread_workspace` resolve `is_vm` once and key every branch off it:
the status markers, the `ws_status` read, `source_type`, and the teardown/
provision fork.

**A third instance of the same bug turned up during the fix.** `_resolve_ssh_port`
also branched on `if ws_ctx:` — so a VM thread was handed the **pod** port 30022
instead of 22, which would have broken the snapshot SSH the moment suspend
started working. It now takes an explicit `is_vm` argument; the thread callers
pass it, job callers omit it and keep the presence behaviour.

**The idle sweeper needed no change.** `check_idle_threads`' SQL already selected
vm-tier rows (`metadata->'vm'->>'status' = 'ready'`). The blockage was entirely
inside `suspend_thread_workspace`, which bailed on every one it was handed.

### Job path: deliberately unchanged

Investigated and left alone. A job's `context.workspace_container` is written
**only** by container-provisioning paths — its `git_remote_url` goes to the
context root via `merge_job_context` (`services/job_provisioning.py`), not into
`workspace_container`. So for jobs, presence really does imply pod-tier and the
existing checks are correct. Do not "fix" them by analogy.

### Tests

`tests/test_workspace_suspension.py::TestThreadTierIsExplicit` — vm-tier thread
actually suspends; snapshot labelled `source_type="vm"`; markers land on
`metadata.vm` and not on the git-only `workspace_container`; container-tier
thread unchanged; and an end-to-end `check_idle_threads` sweep that suspends an
idle VM thread (the leak itself).

## Proposal (as designed, for reference)

Single-source the tier instead of inferring it. `session_provisioner._thread_backend`
already reads `metadata.config_override.workspace.backend`, and
`src/core/backends/factory` now exports `VM_BACKENDS` / `is_vm_backend` (added by
the Defect 1 fix) for exactly this.

- Replace the four `if ws_ctx: … elif vm_ctx:` tier selections and both
  `source_type` expressions with an explicit tier read.
- Replace `ws_status = ws_ctx.get("status") if ws_ctx else vm_ctx.get("status")`
  with a tier-driven read: VM tier reads `vm_ctx["status"]`, container tiers read
  `ws_ctx["status"]`.
- Leave `ssh_host` resolution alone — its `or` chain is already correct for both
  tiers.

**Decide separately for the job path.** Lines 176-190 use the same pattern with
`job_id`. Jobs do not go through `_setup_gitea`, so `context.workspace_container`
may be a reliable pod-tier signal there — this needs its own check before being
changed. Do not assume the session diagnosis transfers.

## Scope

Medium. Confined to one file for sessions, but it changes suspend/restore
behaviour on a path shared with jobs, so the job-side decision above gates how
much moves. Tests should cover: VM-tier thread suspends via the VM branch with
`source_type="vm"`; container-tier thread unchanged; a VM thread carrying a
git-only `workspace_container` is not read as pod-tier.

## Verification

Unit-testable (`tests/test_workspace_suspension.py`,
`tests/test_workspace_suspension_port.py`). Live gate on dev: idle-suspend a VM
session, assert the VM is deleted and the snapshot manifest records
`source_type="vm"`, then resume and assert the VM is recreated and the snapshot
extracted.

## Related

- `docs/issues/session_vm_backend_never_attaches.md` — Defect 1, whose fix exposes
  this. Same root pattern: tier inferred from metadata shape rather than asked for.
- `docs/issues/vm_guest_boots_to_emergency_shell.md` — the open guest-boot bug;
  until it is resolved, a live VM-session suspend/resume gate cannot be run.
