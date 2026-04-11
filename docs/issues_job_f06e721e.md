# Issues Report: Job f06e721e-d754-40d2-9b95-1eeef20d3c3b

**Date**: 2026-04-10
**Job**: "Test 1" — Transcribe 24 WhatsApp audio files (.ogg)
**Model**: codex/gpt-5.4
**Config**: default (worker mode)
**Workspace**: Remote (Docker, SSH on port 2202)
**Runtime**: 53+ minutes (started 12:25 UTC, never terminated)
**Result**: No deliverable produced. Job still stuck in `processing` status.

## Fix Status (updated 2026-04-11)

| Bug | Severity | Status |
|-----|----------|--------|
| BUG-1: `read_file` broken on remote workspaces | Critical | ✅ Fixed |
| BUG-2: `mark_complete` misleading docstring | High | ✅ Fixed |
| BUG-3: `todo_complete` no deliverable validation | Medium | ⏳ Deferred (design decision) |
| BUG-4: Git pager captures shell in tmux | Medium | ✅ Fixed |
| BUG-5: `run_command` heredoc handling | Medium | ✅ Fixed |
| BUG-6: Audit API pagination broken | Low | ✅ Fixed |

Systematic / prompt issues (AGENT-1..4) deferred per scoping decision.

---

## System Bugs

### BUG-1: `read_file` audio transcription broken on remote workspaces (Critical)

**Status**: ✅ Fixed 2026-04-10. Added `Workspace.local_copy(path)` context manager in `src/core/workspace.py` — for local backends it yields the resolved path directly; for remote backends it downloads via SFTP to a temp file and cleans up on exit. `read_file` in `src/tools/workspace/files.py` now wraps image, audio, and visual-document handling in `workspace.local_copy()`, and `_handle_image_file` / `_handle_audio_file` accept a `display_name` so headers still show the original filename. Fix covers audio, images, and visual documents (PDF/PPTX/DOCX) in one shot.

**Root cause**: The `read_file` tool's audio handler passes a remote filesystem path to `AudioHelper.transcribe()`, which checks `file_path.exists()` against the **local** filesystem. Since the file only exists on the remote workspace container, the check always fails.

**Call chain**:

1. `read_file("documents/audio.ogg")` calls `workspace.exists(path)` — delegates to `RemoteBackend.exists()` which uses SFTP `stat()` on the remote. Returns `True`. (`files.py:634`)
2. `workspace.get_path(path)` calls `RemoteBackend.resolve_path()` (`remote.py:327`) which returns a string like `/home/agent-host/workspace/documents/audio.ogg` — a path on the **remote** machine.
3. `Workspace.get_path()` wraps this in `Path(...)` (`workspace.py:512`).
4. `_is_audio_file(full_path)` checks the suffix — returns `True`. (`files.py:649`)
5. `_handle_audio_file(full_path, ...)` is called. (`files.py:650`)
6. Inside, `AudioHelper.transcribe_sync(full_path, ...)` is called. (`files.py:263`)
7. `AudioHelper.transcribe()` does `file_path = Path(file_path)` then `if not file_path.exists()` — this checks the **agent pod's local filesystem**, not the remote workspace. (`audio_helper.py:242-244`)
8. The file doesn't exist locally. Returns `"[Error: audio file not found: /home/agent-host/workspace/documents/audio.ogg]"`.

**Affected files**:
- `src/tools/workspace/files.py:637-653` — passes remote path to local-only handler
- `src/services/audio_helper.py:242-245` — uses `Path.exists()` which is local-only

**Impact**: Audio transcription is completely non-functional. The only code path that worked (the local backend) has been removed — dev and production both go through SSH to a workspace container now. Audio tools need to stage files on the remote workspace via SFTP before transcription.

**Note**: The same abstraction leak likely affects `_handle_image_file()` (`files.py:642-643`) which calls `full_path.read_bytes()` — this would also fail on remote paths. Image handling may silently fail or produce different error messages.

**Fix options**:
1. Download the file from the remote workspace to a local temp file before passing to `AudioHelper`:
   ```python
   if _is_audio_file(full_path):
       if hasattr(workspace._backend, '_sftp'):  # remote backend
           with tempfile.NamedTemporaryFile(suffix=full_path.suffix, delete=False) as tmp:
               workspace._backend.download(path, tmp.name)
               result = _handle_audio_file(Path(tmp.name), ...)
       else:
           result = _handle_audio_file(full_path, ...)
   ```
2. Add a `workspace.download_to_local(path) -> Path` method that handles this transparently for all backends.
3. Have `AudioHelper` accept file bytes instead of a file path.

---

### BUG-2: `mark_complete` does not stop the graph (High)

**Status**: ✅ Docstring fixed 2026-04-10. `mark_complete` in `src/tools/core/job.py` now accurately states it writes `output/completion.json` but does NOT stop the agent loop, and directs the agent to use `job_complete` instead. The tool still exists (it's useful as a task-complete report separate from job termination); only the misleading claim was removed. Did not adopt the alternative of making `mark_complete` also set `should_stop=True` — the two tools serve different purposes and conflating them would be worse.

**Root cause**: The agent has two completion tools — `mark_complete` and `job_complete` — and the agent called the wrong one. But the fundamental issue is that `mark_complete` is misleadingly named and documented.

**`mark_complete`** (`job.py:72-122`):
- Docstring says: "Signal that the assigned task is complete. This will write a completion report and **end the agent loop**." (line 78-79)
- Reality: It only writes `output/completion.json` to the workspace filesystem. It does **not** set `should_stop`, `goal_achieved`, or any graph state. The graph continues looping.

**`job_complete`** (`job.py:124-254`):
- Actually stops the graph by setting `is_final_phase=True` via a global dict, which triggers `finalize_job()` during the next strategic phase transition.
- Can only be called during strategic phases.

**The problem**: The docstring for `mark_complete` explicitly claims it will "end the agent loop" — this is false. The agent (codex/gpt-5.4) trusted the docstring and called `mark_complete` expecting the job to terminate. Instead, the graph kept running for 25+ more minutes, burning LLM tokens on a post-mortem review phase that was never requested.

**Impact**: Wasted compute (25+ extra LLM calls after the agent intended to stop), job stuck in `processing` indefinitely, orchestrator never notified of completion.

**Fix options**:
1. Fix the `mark_complete` docstring to accurately describe what it does (writes a file, does NOT stop the loop)
2. Have `mark_complete` actually set `should_stop=True` in the graph state
3. Remove `mark_complete` entirely — it's redundant with `job_complete` plus `write_file`
4. Add a phase gate: if `mark_complete` is called from a tactical phase, auto-trigger a transition to strategic phase so `job_complete` can be called

---

### BUG-3: `todo_complete` has no deliverable validation (Medium)

**Root cause**: `todo_complete` (`todo.py:159-271`) is a pure bookkeeping operation — it flips a status flag and counts remaining todos. There is no check that the work described in the todo was actually performed.

**What happened**: The agent marked all 5 tactical todos as "completed" even though:
- `output/transcriptions.md` was never created (the primary deliverable)
- Every `read_file` call on audio files returned an error
- 0 out of 1 deliverables were produced

The system recorded 5/5 todos complete, archiving the phase as successful, while the actual deliverable is entirely missing.

**Impact**: Phase archives and progress tracking become unreliable. The orchestrator (and users watching the cockpit) see "5/5 todos completed" and may assume the work is done.

**Note**: `job_complete` (lines 186-211) does have deliverable validation — it checks that listed files exist and are non-empty, and rejects high-confidence completions with missing deliverables. But this gate only fires at final job completion, not per-todo.

**Fix options**:
1. Add optional deliverable fields to todos and validate on completion
2. Add a phase-level deliverable check in `archive_phase` that warns or blocks when expected outputs are missing
3. At minimum, log a warning when a todo is marked complete but no workspace writes occurred during its execution

---

### BUG-4: Git pager captures shell in tmux (Medium)

**Status**: ✅ Fixed 2026-04-10. Added `git config --global core.pager cat` for the `agent-host` user in both `docker/Dockerfile.workspace` (container workspaces) and `docker/agent-vm-base/scripts/provision.sh` (VM workspaces). All git commands now emit directly to stdout instead of invoking `less`, so there is no interactive pager to get stuck on. Existing containers/VMs need to be rebuilt to pick up this change.

**Root cause**: When the agent runs git commands via `run_command`, git's default pager (`less`) opens interactively in the tmux pane. The agent cannot interact with `less`, so the shell gets permanently stuck.

**What happened**:
- The agent called `git_log` and `git_diff` tools (read-only git tools in `src/tools/git/git_tools.py`), which opened the `less` pager in the git tmux window
- The pager captured tmux window 2 ("git"), rendering it permanently stuck (3,470 lines of buffered `less --help` output)
- A garbled file was created: `e-1-tactical-complete -m Phase 1 tactical complete` (13,067 bytes of `less --help` output) — this appears to be the result of a git tag command that was mangled by tmux, producing a file with that name instead of executing the intended command
- The agent had to fall back to `run_command("git --no-pager log ...")` with `GIT_PAGER=cat` to get git output

**Impact**: Permanently stuck tmux windows, garbled workspace files, wasted iterations debugging shell state.

**Fix options**:
1. Set `GIT_PAGER=cat` or `git config core.pager cat` in the workspace environment by default
2. Set `PAGER=cat` globally in the workspace container environment
3. Have the git tools pass `--no-pager` to all git commands
4. Add pager detection in `shell_manager` — if output stalls and looks like a pager prompt, send `q` to exit

---

### BUG-5: `run_command` heredoc handling causes stuck shells (Medium)

**Status**: ✅ Fixed 2026-04-11. Added a module-level `build_sentinel_command(command, sentinel)` helper in `src/tools/shell/shell_manager.py`. Single-line commands keep the existing `f'{command}; echo "{sentinel} $?"'` chaining (preserves the interactive-prompt detection for commands like `read answer`). Multi-line commands get wrapped in an outer `bash << "SRW_DELIM_<uuid>"` heredoc with a unique START marker, so inner heredocs (`python3 <<'PY' ... PY`) and multi-statement scripts are read by inner bash from the captured heredoc body instead of being typed line-by-line into tmux. Output extraction uses the START marker to locate where the user command's stdout begins. Applied identically to `shell_run` in `src/core/backends/remote.py` (imports the helper). Added 4 regression tests in `tests/test_run_command.py`. An initial attempt using plain `\n` separation was abandoned because it silently auto-fed the sentinel echo as stdin to `read`-style commands, breaking interactive-prompt detection; the heredoc-wrap avoids that.

**Note**: The latent trailing-comment bug (`ls # something` swallowing the sentinel echo) is NOT fixed by this — single-line commands still use `;` chaining. Separate, not scoped to BUG-5.

**Root cause**: When the agent uses `run_command` with a heredoc (e.g., a multi-line Python script), the tmux `send_keys` API can mangle the heredoc delimiter because the shell tool appends an internal sentinel string to the command. This causes the heredoc to never close, leaving the shell in an interactive input-waiting state.

**What happened**:
- The agent tried to run a Python heredoc to check for whisper modules
- The heredoc marker `PY` was appended with the sentinel string, causing a syntax error in the heredoc
- The shell got stuck waiting for the heredoc to close
- 5 subsequent `run_command` calls failed because the shell was still stuck
- The agent spent 3 iterations (12:42-12:47) trying to close the stuck heredoc by sending just `PY`

**Impact**: Wasted iterations, cascading failures for subsequent commands.

---

### BUG-6: Audit API pagination broken (Low)

**Status**: ✅ Fixed 2026-04-11. Root cause was naming, not broken pagination: the endpoint only accepted `page`/`pageSize`, so the REST-idiomatic `offset`/`limit`/`order` params were silently dropped by FastAPI. Extended `MongoDB.get_job_audit()` (`orchestrator/database/mongodb.py:180`) and `GET /api/jobs/{id}/audit` (`orchestrator/main.py:6632`) to accept `offset`/`limit`/`order` in addition to `page`/`pageSize`. If both styles are given, offset/limit wins. Response now echoes both styles. `order=desc` is supported (flips the `step_number` sort direction). Existing callers (cockpit `api.service.ts`, MCP client, builder_dispatch) all use `page`/`pageSize` and are untouched. Added `tests/test_audit_pagination.py` with 4 unit tests covering the contract (offset/limit, page/pageSize back-compat, order param, default response shape). The `/audit/bulk` endpoint was left alone — it already uses offset/limit for its own purpose (IndexedDB seeding, up to 5000 entries).

**Observation**: The orchestrator's `/api/jobs/{id}/audit` endpoint does not properly support `offset`, `limit`, or `order` parameters. It always returns the first 50 entries regardless of parameters. The `page` and `pageSize` params work but are undocumented in the typical `limit`/`offset` pattern.

**Impact**: Debugging job failures via the API is difficult — you can only see the first 50 of 274+ audit entries without knowing the correct pagination params.

---

## Agent Behavior Issues

### AGENT-1: Called `mark_complete` instead of `job_complete`

The agent called `mark_complete` (which only writes a file) instead of `job_complete` (which actually stops the graph). This is partly a system issue (BUG-2: misleading docstring) but also an LLM judgment failure — the agent had access to both tools and their schemas.

### AGENT-2: Marked todos complete despite no deliverable

The agent marked all 5 tactical todos as "completed" even though every transcription attempt failed and `output/transcriptions.md` was never created. The todos explicitly described producing transcript content, but the agent completed them administratively after documenting the blocker. This inflates progress metrics and misleads monitoring.

### AGENT-3: Never attempted sudo or escalation despite clear paths available

The agent checked whether `whisper`, `faster_whisper`, `openai`, and `torch` were installed (none were), and confirmed `ffmpeg` was available. It then tried converting `.ogg` to `.wav` via ffmpeg, but never attempted installation or escalation. The system provides **three clear escalation paths**, none of which the agent used:

1. **`sudo pip install openai-whisper`** — The `run_command` tool docstring explicitly tells the agent: *"SUDO NOTE: Commands prefixed with `sudo` may pause the job while the operator decides whether to upgrade to a VM environment."* (`shell_tools.py:247-251`). Running `sudo` in the container would trigger `sudo_action: freeze`, which pauses the job and presents the operator with a VM upgrade option. The operator could approve the upgrade, the job would be re-dispatched to a VM with full sudo access, and the agent could install whisper and complete the transcription. This is exactly what the sudo freeze mechanism was designed for.

2. **`send_message(mode="blocking")`** — The `send_message` tool is available in the default config (`config/defaults.yaml:148`) and supports a `blocking` mode that freezes the job until the operator replies. The agent could have sent a message like "read_file cannot transcribe audio in this environment — please advise whether to upgrade to a VM or use an alternative transcription approach." This would pause the job cleanly and get human input.

3. **`pip install --user openai-whisper`** — Even without sudo, the agent could have tried installing whisper in user space via `pip install --user`. The workspace container has Python and pip. This doesn't require sudo or a VM upgrade.

Instead, the agent wrote a blocker note to the knowledge base and gave up with 0.12 confidence, never attempting any of these paths. The `run_command` docstring even suggests the fallback: *"If not upgraded, try an alternative approach (pip install as user, compile from source in userspace, etc.)."*

**Root cause assessment**: This is likely a model-level issue — codex/gpt-5.4 did not connect the "whisper not installed" blocker with the available escalation mechanisms, despite the `run_command` docstring describing the exact scenario. The instructions system may need to more prominently surface the sudo → VM upgrade workflow as a standard resolution for missing dependencies.

### AGENT-4: Continued running indefinitely after intended completion

After calling `mark_complete` at 12:47, the agent continued for 25+ more minutes through an unplanned strategic review phase. It wrote a retrospective, updated the knowledge base, revised the plan, created new todos, attempted WAV conversion (which also failed due to the same BUG-1), and eventually wrote a blocker report to `output/transcriptions.md`. The job never terminated.

---

## Timeline Summary

| Time (UTC) | Event |
|------------|-------|
| 12:25:30 | Job starts, workspace initialized |
| 12:25-12:28 | Strategic phase 0: reads instructions, creates plan, stages todos |
| 12:28:53 | Phase 0 archived, transitions to tactical phase 1 |
| 12:40:55 | Phase transition complete (12-min gap — likely context compaction) |
| 12:41:04 | Tactical phase 1 begins — attempts audio transcription |
| 12:41:22 | First `read_file` on `.ogg` — fails with "audio file not found" |
| 12:41:55 | Copies file to simplified name — same error |
| 12:42:17 | Tries `run_command` heredoc — shell gets stuck |
| 12:43:29 | Copies file to workspace root — same error |
| 12:47:29 | Writes blocker to knowledge base, cleans up test files |
| 12:47:36 | Calls `mark_complete` (confidence 0.12) — writes completion.json |
| 12:47:48-12:48:43 | Marks all 5 todos as "completed" |
| 12:48:50 | Tactical phase 1 archived |
| 12:49-12:57 | Unplanned strategic phase 2: post-mortem review |
| 13:01:57 | Writes `archive/phase_1_retrospective.md` |
| 13:05-13:09 | Rewrites plan, creates workspace.md, stages phase 2 todos |
| 13:12:35 | Finds ffmpeg, converts 3 `.ogg` to `.wav` — conversion works |
| 13:12:51 | `read_file` on `.wav` — same "audio file not found" error (BUG-1) |
| 13:14-13:16 | Converts 6 more files, all `read_file` calls fail |
| 13:16:44 | Writes blocker report to `output/transcriptions.md` |
| 13:18:45 | Last recorded activity — job still `processing`, never terminated |

**Total LLM calls**: 51+
**Total tool calls**: ~200+
**Deliverables produced**: 0 (the `output/transcriptions.md` that was eventually written contains only a blocker report, not transcriptions)

---

## Recommendations

### Critical (blocking audio jobs)

1. **Fix BUG-1** — audio transcription on remote workspaces is completely broken. Add a `workspace.download_to_local(path)` method and use it in `_handle_audio_file` (and `_handle_image_file`) to download remote files before passing them to local-only services like `AudioHelper`. This is the root cause of the job failure.

### High Priority

2. **Fix BUG-2** — either fix the `mark_complete` docstring to stop claiming it "ends the agent loop", or make it actually set `should_stop=True`. Currently it's a trap — every model will trust the docstring and call it expecting termination.
3. **Surface escalation paths in instructions** — the `run_command` docstring mentions sudo → VM upgrade, but the agent didn't connect this to its "whisper not installed" blocker. Consider adding an instruction rule like: *"When you encounter a missing system dependency that blocks your task, try `sudo <install command>` — this will pause the job and offer the operator a VM upgrade, which is the designed escalation path."* This could go in `config/templates/` or `config/prompts/`.
4. **Set `GIT_PAGER=cat`** in the workspace container environment (BUG-4) — one-line fix in the Dockerfile or container entrypoint that prevents a whole class of stuck-shell issues.

### Medium Priority

5. **Add a phase-level deliverable check** — when archiving a tactical phase, verify that expected outputs exist before recording the phase as successful. At minimum, log a warning when todos are marked complete but no workspace writes occurred.
6. **Harden `run_command` heredoc handling** (BUG-5) — either strip/escape the sentinel from heredoc delimiters, or detect and recover from stuck-input states automatically.
