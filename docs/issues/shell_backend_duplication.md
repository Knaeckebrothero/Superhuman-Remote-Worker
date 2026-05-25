---
tags:
  - shell-tools
  - tech-debt
  - architecture
  - testing
  - isolation
related:
  - "[[shell_stall_detection_false_positive]]"
---

# Two redundant shell-execution implementations (local libtmux vs remote SSH), kept in sync by hand

**Reported**: 2026-05-25
**Status**: Open — tech debt; design options below. Partial mitigation landed on branch `fix/shell-stall-detection` (the *pure* logic is now shared; the imperative loops are still duplicated).
**Severity**: Medium. Two consequences: (1) **maintainability/correctness** — the same logic lives in two places and drifts; this already caused one bug to exist (and need fixing) in both copies, see [[shell_stall_detection_false_positive]]. (2) A latent **isolation footgun** — the local path can execute agent-authored commands on the *agent pod* if a caller-side guard is ever bypassed (see "Related footgun" below).

## Summary

Synchronous shell execution for the agent is implemented **twice**:

| | Local path | Remote path |
|---|---|---|
| Class / method | `ShellManager.run_sync` | `RemoteBackend.shell_run` |
| File | `src/tools/shell/shell_manager.py` | `src/core/backends/remote.py` |
| Transport | **libtmux** objects in-process (`pane.send_keys`, `pane.capture_pane`) | **SSH** running the `tmux` CLI on a remote host |
| Runs on | the agent process's own pod | the isolated workspace (sandbox container pod / VM) |
| Used in | **tests + local dev only** | **production** (persistent sessions & jobs always delegate here — see [[persistent_session_shell_runs_on_workspace_pod]]) |
| CI-testable? | **Yes** (real tmux, no VM needed) | **No** (needs SSH + a provisioned workspace) |

The two methods are ~200 lines each and implement the same algorithm: build a sentinel-suffixed command, send it, poll the captured buffer for the sentinel + exit code, detect interactive prompts, apply the no-change ("still running") soft timeout, enforce the colliding-command guard, restore cwd, and extract stdout (incl. the multi-line heredoc `start_marker` path). Each backend also has its own copy of `_detect_interactive_prompt`, `_detect_blocked_tab`, `_capture_terminal_state`, `_send_and_wait`, and its own tab struct (`ShellTab` vs `_RemoteTab`).

## Why it persists (the root cause: a testability asymmetry)

This is the crux. The **production** path (`remote.py`) cannot be exercised in CI without a VM + sshd, so it has effectively **no integration coverage**. The **local libtmux** path *can* run against a real tmux in CI with no extra infra, so the shell test suite is built entirely on it:

- `tests/test_shell_manager.py` and `tests/test_run_command.py` construct `ShellManager(...)` with **no backend** and drive real tmux. These are the only tests that exercise the sentinel/poll/stall/colliding logic end-to-end.
- `tests/test_shell_stall_logic.py` covers the *transport-agnostic* helper (`compute_no_change_state`) without tmux.
- `tests/test_managers_git.py` uses a **mocked** backend (parsing only).

So the local path survives largely as the **testable reference implementation**. We keep two implementations because deleting the local one would leave the shell engine with no CI-testable path at all — and the remaining (prod) path is the harder one to test. Historically the local libtmux `ShellManager` was also the *original* executor; `RemoteBackend` was layered on later for isolation, and the algorithm was copy-adapted rather than abstracted.

## Evidence of the cost

The [[shell_stall_detection_false_positive]] bug (5s/last-20-lines stall heuristic mislabeled as "interactive input") existed **identically in both files** and had to be fixed in lockstep in both. Any future change to the poll loop, sentinel parsing, prompt detection, or output extraction carries the same drift risk — and the prod copy's changes can't be verified by CI.

## What's already shared vs still duplicated (after the stall fix)

The stall fix extracted the **transport-agnostic** pieces into `shell_manager.py`, imported by `remote.py`:

- Shared now: `NO_CHANGE_TIMEOUT_SECONDS`, `HARD_TIMEOUT_CAP_SECONDS`, `NONINTERACTIVE_ENV_EXPORT`, the `STILL_RUNNING` / `COLLIDING_COMMAND` / `INTERACTIVE_PROMPT` templates, `INTERACTIVE_PROMPT_PATTERNS`, `build_sentinel_command`, `compute_no_change_state`, `prompt_is_ready`.
- Still duplicated: the **imperative poll loop** itself, sentinel scanning + exit-code parse, multi-line/`start_marker` output extraction, the pre-flight + colliding-guard wiring, `_detect_interactive_prompt` / `_detect_blocked_tab` / `_capture_terminal_state`, `_send_and_wait`, env-injection sites, and the per-tab state structs.

The reason the *loops* couldn't be shared is that they're coupled to different transports — libtmux `Pane` objects vs `tmux` CLI invoked over SSH. Sharing them requires abstracting that transport.

## Related footgun: the local path is reachable in production

Independent of the duplication, the local path is a latent isolation risk:

- Both `ShellManager` construction sites do `backend=ws_backend if use_remote_shell else None` and still build a *local* manager if `tmux` is on `PATH` (`agent.py` ~:1693, `persistent_session.py` ~:544). `ShellManager(backend=None)` runs libtmux **in-process with no refusal**.
- The agent pod image **ships tmux** (`docker/Dockerfile.agent:116`), so the natural "no tmux ⇒ shell disabled" fail-safe does **not** protect the agent pod.
- Today this is prevented only by **caller-side preconditions** (`agent.py:1070` and `persistent_session.py:233` hard-require `sandbox`/`vm`; backend connect fails closed). The guarantee lives in the callers, not in `ShellManager`. A future entrypoint/refactor/bug that builds a `ShellManager` without that check would silently execute agent-authored commands on the agent pod — which holds the workspace SSH key (`/run/secrets/vm-ssh-key`), model API keys, and cluster credentials.

## Options

1. **Gate the local path fail-closed (small, do first).** Add `allow_local_shell: bool = False` to `ShellManager`; raise if `backend is None` and not explicitly allowed (tests/dev set the flag or an env var like `SRW_ALLOW_LOCAL_SHELL=1`). Optionally drop `tmux` from `docker/Dockerfile.agent`. This makes "never on the agent pod" a code-enforced invariant and removes the footgun, without touching the duplication.

2. **Unify into one engine + swappable transport (larger, the real fix).** Extract a single poll/sentinel/stall/extract engine that drives a thin `ShellTransport` interface (`send_keys`, `capture`, `signal`, `is_ready`). Provide two transports: a libtmux transport (in-process, used by tests/dev) and an SSH+tmux transport (prod). "Local" then stops being a parallel *implementation* and becomes just a *test transport* for the single shared engine — eliminating the duplication and giving the prod path real CI coverage (tests run the engine over the libtmux transport; the SSH transport is a thin, separately-tested adapter).

3. **Make the prod path testable directly (complement to #2).** Run `RemoteBackend` against a localhost `sshd` in CI so the SSH path itself gets integration coverage. Heavier CI setup; pairs well with #2's thin adapter.

Note: simply *deleting* the local path is **not** advisable on its own — it would remove all CI-testable coverage of the shell engine, leaving only the untested prod path.

## Recommendation

- **Now:** Option 1 (gate + Dockerfile) — cheap, kills the isolation footgun immediately, no test loss.
- **Next:** Option 2 (unify + transport), optionally with Option 3. This is what genuinely retires the "local path" as a parallel production implementation and removes the drift that produced [[shell_stall_detection_false_positive]].

## Affected files

- `src/tools/shell/shell_manager.py` — local `run_sync` + libtmux helpers + tab struct
- `src/core/backends/remote.py` — `RemoteBackend.shell_run` + SSH helpers + tab struct
- `src/core/workspace_backend.py` — `WorkspaceBackend` abstract (would host the transport seam)
- `src/agent.py` (~:1070, ~:1693), `src/api/persistent_session.py` (~:233, ~:552) — construction sites / guards
- `docker/Dockerfile.agent` (~:116) — tmux in the agent image
- `tests/test_shell_manager.py`, `tests/test_run_command.py` — local-tmux integration tests (would target the engine-over-libtmux-transport)

## Acceptance criteria

- One implementation of the poll/sentinel/stall/colliding/extract algorithm; backends differ only in a thin transport adapter.
- The production (SSH) path has automated coverage (engine tests over a fake/local transport, and/or localhost-sshd integration).
- `ShellManager` cannot execute locally on the agent pod without an explicit, non-default opt-in; the agent image does not depend on the local path being available.
