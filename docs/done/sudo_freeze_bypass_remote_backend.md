# Sudo Freeze/VM-Upgrade Bypass on Remote Backends

**Date:** 2026-04-12
**Severity:** High — core safety/escalation mechanism is non-functional in production
**Status:** Open
**Discovered via:** Test job `6b0f93bb` where agent ran `sudo -n true`, got exit code 127, and completed without triggering the VM-upgrade freeze flow.

## Summary

The sudo freeze mechanism (intercept `sudo` commands, freeze the job, offer the operator a VM upgrade) does not work on remote SSH workspaces. Since all production agents use remote workspaces, this feature is **dead code in production**. Three independent failures combine to disable it.

## What Should Happen

```
Agent calls: run_command(command="sudo apt-get install -y libxml2-dev")
  -> ShellManager._check_blocked() detects "sudo", returns SUDO_FREEZE_SENTINEL
  -> shell_tools._check_sudo_freeze() triggers context.request_freeze({
       freeze_type: "vm_upgrade_required",
       command: "sudo apt-get install -y libxml2-dev"
     })
  -> Job pauses, operator gets SSE notification
  -> Operator calls POST /api/jobs/{id}/upgrade-to-vm
  -> Orchestrator provisions VM, re-dispatches job with sudo access
```

## What Actually Happens

```
Agent calls: run_command(command="sudo -n true")
  -> ShellManager.run_sync() sees _use_backend=True
  -> Delegates to RemoteBackend.shell_run() immediately (line 642)
  -> RemoteBackend._check_blocked() only checks blocked_commands list
  -> "sudo" is not in blocked_commands -> returns None
  -> Command sent to remote workspace via SSH
  -> sudo binary not installed in container -> exit code 127
  -> _check_sudo_freeze() sees normal output (not sentinel) -> returns None
  -> Agent continues, writes a report about exit code 127, completes normally
```

## Three Independent Failures

### Failure 1: Early-Return Backend Delegation Skips Safety Checks

**File:** `src/tools/shell/shell_manager.py`

`run_sync()` (line 639) and `send()` (line 439) both follow this pattern:

```python
def run_sync(self, command, ...):
    if self._use_backend:
        return self._backend.shell_run(command, ...)  # <- exits here
    # Safety check — never reached when _use_backend is True
    blocked = self._check_blocked(command)
    if blocked:
        return blocked
```

When a remote backend is active (`_use_backend=True`), the method returns before reaching ShellManager's own `_check_blocked()` which contains the sudo intercept logic.

**Affected methods:**

| Method | Line | Skips |
|--------|------|-------|
| `run_sync()` | 639-647 | `_check_blocked()` at line 649 |
| `send()` | 439-440 | `_check_blocked()` at line 443 |

### Failure 2: RemoteBackend Lacks Sudo Intercept Logic

**File:** `src/core/backends/remote.py`

RemoteBackend has its own `_check_blocked()` (line 670) but it only checks the `blocked_commands` set (`reboot, shutdown, poweroff, halt, init`). It has no concept of `sudo_action` or `SUDO_FREEZE_SENTINEL`:

```python
# RemoteBackend._check_blocked (line 670) — incomplete
def _check_blocked(self, command: str) -> Optional[str]:
    if not self._blocked_commands:
        return None
    first_word = command.strip().split()[0] if command.strip() else ""
    if first_word in self._blocked_commands:
        return f"Command blocked: '{first_word}' is not allowed. ..."
    return None  # sudo passes through silently
```

Compare to ShellManager._check_blocked (line 1039) which has the full sudo intercept:

```python
# ShellManager._check_blocked (line 1039) — complete
if first_word == "sudo":
    if self.sudo_action == "allow":
        return None
    elif self.sudo_action == "freeze":
        return SUDO_FREEZE_SENTINEL  # <- missing from RemoteBackend
    else:
        return "Command blocked: 'sudo' is not available..."
```

RemoteBackend's `__init__` (line 105) doesn't accept a `sudo_action` parameter at all, and RemoteBackend is instantiated in `agent.py` (line 1057) without one.

### Failure 3: agent.py Forces `sudo_action="allow"` for Remote Backends

**File:** `src/agent.py` (lines 1655-1657)

```python
sudo_action = shell_config.get("sudo_action", "freeze")
if use_remote_shell:
    sudo_action = "allow"  # unconditionally overrides config
```

Even if Failure 1 were fixed (safety check runs before delegation), this override would make `_check_blocked()` return `None` for sudo commands.

This is then passed to ShellManager at line 1669:
```python
shell_manager = ShellManager(
    ...
    sudo_action=sudo_action,  # "allow" when remote
)
```

**Note:** `persistent_session.py` (line 519) does NOT have this override — it passes through the config value correctly. But it's still affected by Failures 1 and 2.

## Evidence: Job 6b0f93bb Audit Trail

**Job:** `6b0f93bb-c9e9-40de-bcb8-646a10108167` ("Test 43532523")
**Agent host:** Kubernetes pod (`srw-agent-j-*`), remote SSH workspace
**Models:** `codex/gpt-5.4` (strategic), `codex/gpt-5.3-codex-spark` (tactical)

### Timeline

| Audit # | Time | Event |
|---------|------|-------|
| 96 | 17:51:13 | Agent enters Tactical Phase 1: "Benign sudo execution and report verification" |
| 109-116 | 17:51:28-38 | Agent reads tool docs (`run_command.md`, `write_file.md`), completes prep todo |
| **120** | **17:51:41** | **`run_command(command="sudo -n true", timeout=600, tail=80)` -> exit code 127, no output** |
| 127 | 17:51:47 | Agent writes `output/sudo_attempt_report.md` (1,905 bytes) |
| 131-162 | 17:51:49-52:10 | Agent revises report, verifies content programmatically |
| 183-184 | 17:52:26 | Phase 1 complete, agent continues to strategic review |
| 279 | 17:58:32 | `job_complete` called — normal completion freeze (NOT vm_upgrade_required) |
| 296 | 17:59:58 | Final state: `freeze_type: job_complete`, confidence 90% |

### Key observations
- Tool status at entry 120: `[ok]` — `run_command` treated it as a normal successful execution
- Zero error entries in the full 296-entry audit trail (except one benign `workspace.md` not found)
- Agent correctly interpreted exit code 127 as "sudo binary not present" and marked VM-upgrade observation as "inconclusive"
- No `SUDO_FREEZE_SENTINEL` was ever returned, no `vm_upgrade_required` freeze was triggered

## Scope of Impact

- **All job-mode agents** (`agent.py`): triple-blocked by failures 1 + 2 + 3
- **All persistent session agents** (`persistent_session.py`): blocked by failures 1 + 2 (no failure 3)
- **Local tmux execution**: would work correctly — but this path is never used in production
- **Tests**: Only test sudo freeze via `send()` on local ShellManager (test_shell_manager.py:468), no remote backend coverage

## File Reference

| Component | File | Lines |
|-----------|------|-------|
| ShellManager early-return (run_sync) | `src/tools/shell/shell_manager.py` | 639-647 |
| ShellManager early-return (send) | `src/tools/shell/shell_manager.py` | 439-440 |
| ShellManager._check_blocked (complete) | `src/tools/shell/shell_manager.py` | 1039-1068 |
| SUDO_FREEZE_SENTINEL constant | `src/tools/shell/shell_manager.py` | 83 |
| RemoteBackend._check_blocked (incomplete) | `src/core/backends/remote.py` | 670-680 |
| RemoteBackend.__init__ (no sudo_action) | `src/core/backends/remote.py` | 105-120 |
| RemoteBackend.shell_run calls _check_blocked | `src/core/backends/remote.py` | 727-729 |
| RemoteBackend.shell_send calls _check_blocked | `src/core/backends/remote.py` | 941-943 |
| agent.py forces sudo_action="allow" | `src/agent.py` | 1655-1657 |
| agent.py ShellManager construction | `src/agent.py` | 1659-1670 |
| agent.py RemoteBackend construction | `src/agent.py` | 1057-1076 |
| persistent_session.py (no override) | `src/api/persistent_session.py` | 506-535 |
| Freeze detection in shell_tools | `src/tools/shell/shell_tools.py` | 128-148 |
| run_command tool calls run_sync | `src/tools/shell/shell_tools.py` | 276 |
| Config default: sudo_action: freeze | `config/defaults.yaml` | 318 |
| Test: send sudo freeze (local only) | `tests/test_shell_manager.py` | 468-471 |

## Fix Direction

**Approach: defense-in-depth (both layers)**

Fix at both ShellManager and RemoteBackend so no single regression can re-open the hole:

1. **ShellManager** — move the `_check_blocked()` call **before** the backend delegation branch in `run_sync()` and `send()`, so sudo is intercepted regardless of which backend runs the command.
2. **RemoteBackend** — add `sudo_action` support to `RemoteBackend.__init__` and its `_check_blocked()`, mirroring ShellManager's logic. This way even if someone calls the backend directly (bypassing ShellManager), the check still fires.

### Open question: `sudo_action="allow"` override in agent.py

The override at `agent.py:1655-1657` sets `sudo_action="allow"` whenever `use_remote_shell=True`. This is a historical artifact from when "remote" meant "VM" and "local" meant "running inside the agent pod". The project evolved through several workspace stages:

1. **Local** — agent ran commands in its own container (removed: agent could interfere with LangGraph logic/guardrails)
2. **Remote (VM)** — KubeVirt VMs with sudo gate, SSH access. "Remote" was added to mean this.
3. **Remote (container)** — lightweight SSH workspace containers (current default). Added later as the standard workspace.

The local option was deliberately removed — agents must never operate on their own filesystem. So now `backend: remote` is the only option, covering both containers and VMs. But the `sudo_action="allow"` override still treats all remote backends as VMs where the sudo gate handles everything.

**The correct behavior:**
- **Container workspace** (default) — `sudo_action: freeze` (intercept, offer VM upgrade)
- **VM workspace** (upgraded/provisioned) — `sudo_action: allow` (sudo gate handles approval via NATS)

Possible fix approaches:

- **Option A:** Remove the override entirely, always respect config. Orchestrator sets `sudo_action: allow` in the dispatch-time `config_override` when dispatching to a VM. Containers inherit the default `freeze`. Cleanest — orchestrator already knows what it provisioned.
- **Option B:** Check a workspace capability flag (e.g. `context.vm.active` or `backend.has_sudo_gate`) to decide at runtime.
- **Option C:** Add a `workspace_type` field (`"container"` vs `"vm"`) to the remote backend config injected by the orchestrator at dispatch time, and branch on that.

### Cleanup: rename `backend: remote` to meaningful labels

The `remote` label is a leftover from the local/remote era and no longer communicates anything useful — everything is remote now. Rename to distinguish workspace types by what they actually are:

| Current | New | Meaning |
|---------|-----|---------|
| `backend: remote` (container workspace) | `backend: sandbox` | Isolated SSH workspace container — no sudo, throwaway, default |
| `backend: remote` (VM workspace) | `backend: vm` | KubeVirt VM with sudo gate, persistent disk, full OS |

"Sandbox" captures the intent (isolated, disposable, restricted) and avoids confusion with "container" (since the agent pod is also a container). "VM" is self-explanatory.

This rename touches:
- `config/defaults.yaml` (`workspace.backend`)
- `config/persistent_defaults.yaml`
- `src/agent.py` (backend selection logic)
- `src/core/workspace_backend.py` (base class / type checks)
- `src/core/backends/remote.py` (class name? or just the config label)
- `orchestrator/main.py` (dispatcher, `_job_needs_vm()`, upgrade endpoint)
- `orchestrator/services/completion.py`
- CLAUDE.md (architecture docs)
- Dockerfiles / deployment manifests (if they reference the backend type)

With this rename, the `sudo_action` override in agent.py becomes a clean branch:
```python
if workspace_backend == "vm":
    sudo_action = "allow"   # sudo gate handles it
# else: respect config (default "freeze" for sandbox)
```
