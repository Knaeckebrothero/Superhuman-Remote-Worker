---
tags:
  - shell-tools
  - agent
  - terminal
  - reliability
related:
  - "[[shell_mode_model_family_default]]"
  - "[[shell_stall_detection_false_positive]]"
  - "[[shell_backend_duplication]]"
---

# `cancel_command` — a Ctrl+C / abort primitive for the stateless shell

**Reported:** 2026-07-20 (from stuck job `9b760af1-a693-4652-a50c-aca46542ab48`)
**Status:** **Implemented & shipped on `develop`** — commit `14b55d83` (`feat(shell): add cancel_command to abort stuck shell tabs`), pushed. 242 shell tests + `ruff` green.
**Component:** `src/core/backends/remote.py`, `src/core/workspace_backend.py`, `src/core/backends/subdir.py`, `src/tools/shell/shell_manager.py`, `src/tools/shell/shell_tools.py`, stateless expert `tools.shell` lists.

## Problem (root cause)

A DEVELOPER agent sat in `waiting_for_reply` for ~2 days. During a TDD RED phase it ran
`pytest …::test_ac1_register` in its single hidden `default` shell tab; the test **hung** (no
output, never emitted its completion sentinel), so the backend marked the tab busy
(`pending_sentinel` set) and every later command — **including the agent's own `ps`/recovery
probes** — collided with *"previous command still running."*

The stateless tool set (`run_command` + `shell_read`) has **no abort primitive and only one
hardcoded `default` tab**, so the agent could neither cancel, inspect, nor sidestep the hang. Its
only escape was to escalate to a human via a blocking message — which then went unanswered. The
Ctrl+C machinery already existed one layer down (`_tmux_send_keys(tab, "C-c")`; the colliding-guard
clears the busy flag once the shell returns to a prompt) but was never exposed to the stateless set.

## Fix

A zero-arg **`cancel_command`** tool (stateless set only — the persistent set's `shell_execute`
already has keys-mode C-c) that runs a short, always-terminating ladder in
`RemoteBackend.shell_cancel(tab_name="default")`:

1. Capture pane; if already at a prompt → clear stale `pending_sentinel`, "nothing to cancel".
2. Send `C-c`, re-check `prompt_is_ready` → free → clear `pending_sentinel`, "interrupted".
3. Still busy → a second `C-c`.
4. Still stuck (process ignoring SIGINT) → **reset the tab** (close + reopen), state cleared.

Supporting changes:
- `WorkspaceBackend.shell_cancel` NotImplementedError default; `SubdirBackend` override routing
  through `self._tab(name)` (so light subagents C-c their *own* tab, not the parent's).
- `ShellManager.cancel()` forwarder; `cancel_command` `@tool` + `SHELL_TOOLS_METADATA` entry;
  added to the stateless factory list and every stateless expert's `tools.shell`.
- **Discoverability** (the wedge happened because recovery was never surfaced): `run_command`
  now appends a one-line *"↳ call cancel_command to abort it"* pointer on any still-running /
  colliding / interactive-prompt result, plus a docstring update.

## Verification

TDD (Red-Green): 242 tests across `test_workspace_backends.py`, `test_subdir_backend.py`,
`test_shell_manager.py`, `test_run_command.py`; 119 sibling-backend + 206 registry/loader tests
confirm the base-class + registration changes are safe; `ruff` clean.

**Net effect:** a hung foreground command in the stateless shell is now self-recoverable — the
agent gets pointed at `cancel_command`, one call frees the tab (resetting it if SIGINT is ignored),
and no human escalation is needed.
