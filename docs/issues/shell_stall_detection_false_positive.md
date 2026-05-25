---
tags:
  - shell-tools
  - bug
  - agent
  - terminal
related:
  - "[[persistent_session_empty_chunk_history_corruption]]"
  - "[[shell_backend_duplication]]"
---

# Shell tools — long commands falsely flagged "requires interactive input", trapping the agent in a `shell_read` polling loop

**Reported**: 2026-05-25
**Status**: Fixed and **E2E-validated on dev** (session `576f2c29`, 2026-05-25) — adopted OpenHands-style "still running" semantics in both shell backends (soft no-change timeout over the full buffer, honest still-running result, colliding-command guard, explicit-timeout opt-out, non-interactive env). The Issue #1 false "interactive input" misfire no longer occurs. Original analysis and fix options retained below for context.
**Severity**: High for any task that installs heavy deps. A normal `pip install` is misreported as broken, the install keeps running invisibly, and the shared `default` tab becomes unusable for the rest of the turn.

## E2E validation (dev session `576f2c29`, 2026-05-25)

Re-ran the same class of task (Stadur Süd RAG build: heavy installs + a long `python ingest.py --reset`) on dev after deploy. Confirmed working:

- **Issue #1 resolved.** Zero `Command requires interactive input` across all 198 messages. Long/quiet commands now return the honest `Exit code: -1 / --- still running --- … (this is NOT an error)`. The heavy `pip install` (torch/CUDA) that broke `c934b963` completed with `Exit code: 0`.
- **Colliding-guard works.** A second command on the busy `default` tab was rejected (`…previous command still running; your new command was NOT executed…`) instead of silently head-of-line-blocking.
- **Non-interactive env + `_send_and_wait` marker work.** Tabs init with `export PAGER=cat … PIP_PROGRESS_BAR=off …; cd …; echo __READY_…__`; no env leak into command output.
- **Both timeouts fire.** Soft (30s no-change → reported ~32s) and hard (600s cap) both returned control honestly without killing the process.

**Residual gap (not a regression, not the shell layer's bug):** the agent still fell into a long `shell_read` polling loop on a genuinely-long command — `python ingest.py --reset`, which its own generated `rag.py` ran as a CPU `HuggingFaceEmbeddings` pass over 11,890 chunks (ignoring the provided internal embedding API), so the command was effectively hung. The fix made the loop *honest* (correct "still running" + colliding-guard) but did not make the model *escalate*: it never re-ran with an explicit `timeout`, moved the work to an async/background tab, or sent C-c. This blocked the turn for minutes and is what surfaced the `persistent_ws_connecting_during_long_tool` "Connecting…" symptom.

**Follow-up fixes landed (2026-05-25, both backends + cockpit):**
- **Steer the model off the polling loop.** The shared still-running message now reports the real quiet window and frames the escalation as *prevention* (set an explicit `timeout` up front for slow/quiet work incl. data ingestion/embedding) rather than tight polling; the `run_command`/`shell_execute` docstrings teach the same. The `COLLIDING_COMMAND_TEMPLATE` is now **mode-neutral** — it no longer tells a stateless `run_command`/`shell_read` agent to "send C-c in keys mode / use a different tab" (which it can't); those options live in the persistent `shell_execute` docstring.
- **Hard-cap wording (the minor bug below).** The hard-timeout path now uses a distinct `STILL_RUNNING_HARDCAP_TEMPLATE` that says only that the time limit was reached and does **not** claim "no new output" (a redrawing progress bar can keep emitting while still hitting the cap). The soft path reports the true no-change duration.
- **Running-command card on (re)attach.** The `session.state` welcome frame now carries `running_tool` (derived from `_tool_inflight` + `archiver.inflight_tool_call(_session.messages)` — the trailing assistant tool_call with no matching `ToolMessage`), and the cockpit renders a "running command" card (`pickRunningCommandCard`, suppressed when the call is already in a visible turn). This is the only channel for the in-flight call on a cold reload mid-turn, since it isn't persisted to the DB until the turn ends — see `persistent_ws_connecting_during_long_tool`. (Full in-flight *message-tail* reconstruction on cold reload is still deferred; the card covers "what's running".)

## Incident

| | |
|---|---|
| Thread | `c934b963-f958-41f2-b59c-ccc95a0648f6` ("Building a Stadur Süd RAG Chatbot") |
| Environment | dev |
| Project / Agent | `a9eae0b7-498d-409d-9573-ad3830dcb893` / `f42b82d3-5a40-4014-a36b-27135c656017` |
| Model | `gpt-5.5` via `srw-codex-proxy:8317/v1` |
| Mode | autonomous |
| Created → Ended | 2026-05-25T06:37:31Z → 07:23:24Z (1 turn) |
| User-visible symptom | Agent repeatedly reported the terminal "requires interactive input" and burned the turn polling a `pip install` that was actually progressing fine. |

### Sequence (logical order — per-message timestamps in the stored history are corrupted, all collapsed to ~06:53:05; see related doc)

1. Agent wrote the app files (`rag.py`, `app.py`, `static/`, `README.md`, …) via file tools — fine.
2. `cd repos/Stadur-Sued-Project …` via `run_command` → **Exit 1**, relative-path `-bash: cd: repos/Stadur-Sued-Project: No such file or directory`, with a prompt/sentinel fragment leaked into stdout: `2eb158337ab1__ $?"`. (Issue #2 below.)
3. Absolute-path `ls` → worked, confirming `/home/agent-host/workspace/repos/Stadur-Sued-Project` does exist.
4. `pip install` of the deps (langchain, chromadb, **torch 2.12.0 = 532.3 MB**, CUDA wheels) via `run_command` → **`Error: Command requires interactive input. Use non-interactive alternatives (sshpass, -y flags, etc.)`** after 5s. (Issue #1 — the command was *not* interactive.)
5. Agent polled `shell_read` repeatedly; scrollback grew 801 → 803 → 817 → 868 lines (`Downloading …`, then `Installing collected packages: …`) — i.e. the install was healthy the whole time.
6. A second `run_command` (a `ps -ef | grep … && pip show …` status check) on the same `default` tab → **same false stall error** (head-of-line blocking against the still-running pip).
7. Turn fizzles; session ends.

## Issue #1 (NEW, primary) — 5s "stall = waiting for input" misfires on slow/quiet commands

The synchronous exec path declares a command to be waiting for input after **5 seconds** of no change in **only the last 20 captured lines**:

- `src/tools/shell/shell_manager.py:131` → `STALL_DETECTION_SECONDS = 5.0`
- Stall logic `hash(tuple(all_lines[-20:]))` at `src/tools/shell/shell_manager.py:815-841` (local backend)
- Identical copy at `src/core/backends/remote.py:931-954` (**remote/cluster path — the one this session used**)

A heavy `pip install` legitimately has many >5s windows with no change in the last 20 lines: a single large-file download (532MB torch), and especially the silent `Installing collected packages …` unpack/build phase. Stall detection is unconditional (only the interactive-prompt regexes at `shell_manager.py:120-128` are special-cased, and those are the *opposite* signal) — there is no allowlist or "long-running" affordance.

The tool layer then rewrites the stall into a **misleading** error at `src/tools/shell/shell_tools.py:278-286`:

```
Error: Command requires interactive input.
Use non-interactive alternatives (sshpass, -y flags, etc.).
```

`pip install -r requirements.txt` is already non-interactive, so the suggested remedy is a dead end.

**Why it cascades (the real damage):**

1. On stall the command is **not** cancelled — pip keeps running in the foreground of the `default` tab. The stall branch even sends `cd {sandbox_cwd}` keystrokes into that busy pane (`shell_manager.py:828-830`, `remote.py:940-944`).
2. `run_command` always targets the shared `default` tab (`shell_tools.py:268-270`). The next `run_command` lands on the still-busy tab → head-of-line blocking → it re-trips the same 5s stall on the unchanged pip output.
3. The agent falls back to `shell_read` polling and never makes progress on the turn.

The right tool exists — `shell_execute(is_async=True, name="<tab>")` then poll `shell_read` (`shell_tools.py:300-355`) — but nothing steers the model to it for installs, and `run_command` offers no "long-running / don't stall-detect" option.

## Issue #2 (NEW, secondary) — shell-tab cwd desync + sentinel/prompt leak

`cd repos/Stadur-Sued-Project` failed with a **relative-path** "No such file or directory" while the file tools wrote to that exact relative path successfully → the shell tab's cwd was not the file-tool workspace root at that moment. The leaked `2eb158337ab1__ $?"` fragment shows the sentinel-based exit-code capture (`build_sentinel_command`, parsing at `shell_manager.py:776-787` / `remote.py:898-906`) bleeding the prompt into stdout on a fast-failing command. Lower severity than #1 but indicates the output-extraction is fragile when a command errors before producing output.

## Fix options (pending — owner declined to implement at investigation time)

1. **Smarter detection (most robust):** hash the full *new* output since command start rather than just `[-20:]`; only declare a stall when zero new lines have appeared for a longer window (e.g. 30s); on stall, report "command still running — monitor with `shell_read`" instead of "requires interactive input"; reserve the "interactive input" wording for the matched prompt regexes only.
2. **Minimal:** bump `STALL_DETECTION_SECONDS` (5s → 30–45s) and fix the misleading error text. Fast/low-risk but does not address head-of-line blocking on the `default` tab.

Either way, both backend copies (`shell_manager.py` and `core/backends/remote.py`) must change together, and `tests/test_shell_manager.py` / `tests/test_managers_git.py` assert the current "waiting for input" wording (`test_shell_manager.py:365,378-386`, `test_managers_git.py:745-750`) so they'd need updating.
