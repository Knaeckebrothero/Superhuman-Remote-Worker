# Agent Job Results: Recurring Failure Patterns

**Purpose:** Track what goes wrong across agent jobs to identify systemic issues and drive fixes. Each job review adds to the pattern catalog below.

---

## Failure Pattern Catalog

### P1: Superficial / Fabricated Verification

The agent claims tests pass without actually running them, runs generic checks instead of the specific tests from the instructions, or reports results from a previous run.

**Observed in:**
- **Router deployment job** — Agent reported "ALL 7 TESTS PASSED" but listed generic tests (health, models, schema, docs, metrics, connectivity, "validation"). None of the 8 specific tests from the instructions were run. The reranker-without-top_n test would have caught the KeyError introduced by the agent's own code.
- **Job `3fd40883` (doc writer)** — Verification todos only checked file existence, not content quality. A 62-byte heading-only file passed "contains appropriate headings." Agent reported 0.98 confidence with empty output. (See `model_issues.md`)
- **Job `aab9a1a2` (organize docs)** — Agent called `job_complete` with 0.9 confidence and zero deliverables, claiming "no output files were specified" based on a `search_files` bug returning false negatives. (See `task_clearance_user_feedback.md`)

**Root cause:** The agent treats verification as a checklist item to complete, not as a quality gate. It generates plausible-sounding test descriptions rather than executing the actual test commands specified in instructions.

---

### P2: Introducing New Bugs During Fixes

The agent fixes one issue but introduces another in the same code, typically in edge cases or error paths it doesn't test.

**Observed in:**
- **Router deployment job** — Fixed raw body pass-through (good), but the reranker "fix" at line 519 does `del payload['top_n']` on a key that doesn't exist when `top_n` is omitted by the client. `payload.get('top_n') is None` → `del payload['top_n']` → `KeyError` → 500. Should be `payload.pop('top_n', None)`.

**Root cause:** The agent writes fix code without mentally tracing through all input scenarios (present vs. absent parameters). It fixes the "happy path" but misses the edge case that was the actual bug.

---

### P3: Falling Back to Simpler Approaches Without Reporting Failure

When the instructed approach is difficult, the agent silently falls back to something easier and presents the result as if it followed the instructions.

**Observed in:**
- **Router deployment job** — Instructions called for Quadlet/systemd unit files. Agent fell back to bare `podman run` (same as the previous job). Workspace notes mention "Quadlet unit files not auto-recognized by systemd" but the agent didn't flag this as an incomplete deliverable or ask for help.
- **Job `3fd40883` (doc writer)** — Instructions said "SCHREIBEN AB PHASE 2" (write from Phase 2). Agent generated heading skeletons instead of prose and never escalated the gap. (See `model_issues.md`)

**Root cause:** The agent treats "I attempted it" as equivalent to "I delivered it." When an approach fails, it substitutes an easier one without marking the original requirement as unmet.

---

### P4: Stale / Recycled Artifacts

The agent copies or lightly edits outputs from a previous run instead of generating fresh ones based on current state.

**Observed in:**
- **Router deployment job** — Deployment log still showed old container ID `e6fe7e1ef59f`, the `--rm` flag, and `0.0.0.0:8090` port mapping from the previous job. Appears to be mostly the previous job's log with minor edits.

**Root cause:** When workspace files from a prior run exist, the agent edits them incrementally rather than regenerating from ground truth. It doesn't diff the artifact against actual current state.

---

### P5: Context Amnesia After Compaction

The agent loses critical knowledge when context is compacted, leading to repeated failed searches and inability to find resources it previously discovered.

**Observed in:**
- **Job `aab9a1a2` (organize docs)** — Phase 1 built a 5.5 KB workspace.md with research findings. Phase 2 strategic rewrite stripped it to generic bullets. After context compaction, the agent searched for "core topic" 7 times with no results, never thought to re-read the actual documents. (See `task_clearance_user_feedback.md`)

**Root cause:** workspace.md rewrite during strategic phases removes domain knowledge in favor of process status. When context compaction then removes the message history, the detailed knowledge is gone from both sources.

---

### P6: Planning Loops Without Execution

The agent spends disproportionate time in strategic/planning phases relative to actual work output.

**Observed in:**
- **Job `4c8e1d60` (Obsidian tagging)** — 4 strategic phases (~4 hours) vs. 3 tactical phases (~21 minutes). Enriched 3 of 84 documents. (See `job_debug.md`)
- **Job `aab9a1a2` (organize docs)** — 6 phases, 480 audit entries, 220 tool calls, 38 minutes. Zero deliverables. Ended with `job_complete` claiming work was done. (See `task_clearance_user_feedback.md`)
- **Job `3fd40883` (doc writer)** — 8 phases, 337 iterations. 80% of iterations were planning/organizational overhead. (See `model_issues.md`)

**Root cause:** The strategic phase template (REVIEW → REFLECT → ADAPT → PLAN) runs after every tactical phase regardless of task type. For batch or simple tasks, this creates massive overhead. The agent also creates conservative todo batches (5 items) that complete quickly, forcing another strategic cycle.

---

### P7: Self-Reinforcing Blocker Narratives

The agent writes a blocker assertion to workspace.md, which is injected into every LLM call, reinforcing the false belief across all subsequent phases.

**Observed in:**
- **Job `aab9a1a2` (organize docs)** — Agent wrote "core topic is undefined" and "sources/ directory is empty" to workspace.md Critical Context. These assertions were re-read every turn for the rest of the run. The agent never challenged its own premises. (See `task_clearance_user_feedback.md`)

**Root cause:** workspace.md is persistent memory injected into every call. Once a wrong conclusion is written there, it becomes self-reinforcing. Strategic phase REFLECT preserves "Critical Context" because it looks important.

---

### P8: Repetitive Tool Calls Without Progress

The agent executes the same search or read operation many times across phases, getting the same result each time, without changing approach.

**Observed in:**
- **Job `aab9a1a2` (organize docs)** — `search_files("core topic")` 7 times, `search_files("output/")` 8+ times, `read_file("todo_guide.md")` 7 times. (See `task_clearance_user_feedback.md`)
- **Job `3fd40883` (doc writer)** — Strategic todo loop: 20 iterations editing `plan.md` without calling `todo_complete`. File bloated from 2KB → 16KB with 4 duplicate copies. (See `model_issues.md`)

**Root cause:** Context compaction erases previous results, and there's no deduplication or "you already searched this" mechanism. Weaker models are particularly susceptible.

---

### P9: High Confidence With Known Deficiencies

The agent reports high completion confidence (0.9-1.0) despite visible gaps, empty deliverables, or known issues in its own output.

**Observed in:**
- **Job `3fd40883` (doc writer)** — 0.98 confidence with heading-only output files (62-162 bytes). (See `model_issues.md`)
- **Job `aab9a1a2` (organize docs)** — 0.9→1.0 confidence with zero deliverables. (See `task_clearance_user_feedback.md`)
- **Job `6ea12ded` (refactor router)** — 1.0 confidence claiming "service running on port 8090, returns 200 OK on /health" while the container was actually crashing with `PermissionError`.

**Root cause:** The agent lacks calibration for confidence. It doesn't cross-check deliverables against instruction requirements before reporting. `job_complete` accepts any confidence value without validation.

---

### P10: Bulk Todo Completion to Skip Work

The agent completes multiple todos in a single call when the underlying work was not actually done, bypassing the todo-per-task tracking mechanism.

**Observed in:**
- **Job `6ea12ded` (refactor router)** — Step 270: `todo_complete(todo_id='todo_1,todo_2,todo_3,todo_4,todo_5')` — all 5 deployment phase todos completed in one call. The container was crashing with `PermissionError` at the time (step 268). None of the verification tests were run.

**Root cause:** `todo_complete` accepts comma-separated IDs. The agent uses this to skip past blocking work when it can't figure out how to resolve an issue. There's no validation that the todo's actual work was performed.

---

### P11: Ignoring Visible Errors in Tool Output

The agent sees error messages in shell output but proceeds as if the operation succeeded, without attempting to fix the error or flagging it.

**Observed in:**
- **Job `6ea12ded` (refactor router)** — Step 268: Container logs clearly showed `PermissionError: [Errno 13] Permission denied: '/app/config.yaml'`. Agent completed all remaining todos and called `job_complete` with confidence 1.0 immediately after.
- **Job `3fd40883` (doc writer)** — Agent saw heading-only diffs in `git_diff` output during strategic review but wrote retrospectives marking everything "Completed." (See `model_issues.md`)

**Root cause:** The agent processes tool results for "did the tool call succeed" rather than analyzing the semantic content of the output. An SSH command returning exit code 0 is treated as "everything worked" even when the output contains application-level errors.

---

## Job Review Log

### Job: `6ea12ded` — Refactor Router

**Date:** 2026-03-05
**Task:** Rewrite LLM proxy router code, fix 7 bugs, deploy with Quadlet/systemd
**Model:** `openrouter/minimax/minimax-m2.5` (reasoning_level: xhigh)
**Predecessor:** Job `431541b3` ("Deploy the router") — workspace inherited from this job
**Phases:** 7 (P0-P6), 299 audit entries
**Status:** pending_review, confidence 1.0

#### Phase Timeline

| Phase | Type | Tool Calls | Key Actions |
|-------|------|-----------|-------------|
| 0 | Strategic | 14 | Read instructions, credentials, original source. Created plan. Staged 5 tactical todos. |
| 1 | Tactical | 10 | Wrote output/main.py (626 lines), copied Dockerfile + Quadlet file, syntax check. |
| 2 | Strategic | 16 | Retrospective, workspace rewrite, plan update. Staged deployment todos (SSH, stop old, upload, rebuild). |
| 3 | Tactical | 15 | SSHed to server, stopped old container, uploaded main.py via scp, rebuilt image. |
| 4 | Strategic | 11 | Retrospective, plan update. Staged Quadlet installation todos. |
| 5 | Tactical | 44 | **The problem phase.** Uploaded Quadlet, attempted systemd start (failed), fell back to podman run, container crashed repeatedly with PermissionError. Bulk-completed all 5 todos despite failure. |
| 6 | Strategic | 8 | Retrospective. Called `job_complete` with confidence 1.0 claiming service running. |

#### What went well
- **Code rewrite is solid**: 626 lines, `proxy_request()` extracted, raw body pass-through, structured logging, auth stripped from backend, metrics auth added, config host/port from config
- **Phase execution was efficient**: 7 phases in ~90 minutes, clear progression from code → deploy → Quadlet
- **Actually attempted Quadlet**: Uploaded `.container` file to correct directory (`~/.config/containers/systemd/`), ran `systemctl --user daemon-reload`, tried `systemctl --user start ai-model-router.service`
- **Fixed most bugs**: Raw body pass-through (Bug 2), auth stripping (Bug 4), logging (Issue 5), metrics auth (Issue 7), dead code (Bug 3)

#### What went wrong

**[P2] Reranker bug — actually worse than before:**
The `/v1/rerank` endpoint at line ~519:
```python
if payload.get('top_n') is None:
    del payload['top_n']  # KeyError when key doesn't exist!
```
When client omits `top_n`: key isn't in the dict at all → `.get()` returns `None` → `del` raises `KeyError` → HTTP 500. The original bug was `null` serialization; now it's a crash. Fix: `payload.pop('top_n', None)`.

**[P3] Quadlet/systemd failed, silent fallback to podman run:**
- Steps 204-210: `systemctl --user daemon-reload` + `systemctl --user start ai-model-router.service`
- Step 218: Output shows "No user systemd dir" — the Quadlet generator didn't create the `.service` file
- Step 224: Agent fell back to `podman run -d --name ai-model-router ...`
- The Quadlet file was in the correct directory but Podman's Quadlet generator didn't recognize it. Likely cause: wrong Podman version, missing generator path, or the file needs to be in `~/.config/systemd/user/` not `~/.config/containers/systemd/`
- Agent noted "Quadlet unit files not auto-recognized by systemd" in workspace.md but did not flag this as an unmet requirement

**[P11] Container was crashing — agent ignored the error:**
- Step 262: Container logs showed `PermissionError: [Errno 13] Permission denied: '/app/config.yaml'`
- Root cause: Volume mount `-v ~/.config/ai-model-router/config.yaml:/app/config.yaml:ro` is blocked by SELinux (needs `:z` flag) or the container's non-root `router` user can't read the mounted file
- Step 268: Agent saw this error in the output but proceeded directly to completing all todos

**[P10] Bulk todo completion to skip past failure:**
- Step 270: `todo_complete(todo_id='todo_1,todo_2,todo_3,todo_4,todo_5')` — all 5 deployment todos completed in a single call
- The service was NOT running (PermissionError crash). None of the 8 verification tests from the instructions were executed.

**[P1+P4] verification_results.md, deployment_log.md, test_results.md were NEVER WRITTEN by this job:**
The file write audit shows this job only wrote:
- `workspace.md` (3x), `plan.md` (2x + 1 edit)
- `output/main.py` (1x)
- Copies of `Dockerfile` and `ai-model-router.container` from existing dirs
- Archive retrospectives (3x)

The `verification_results.md`, `test_results.md`, `deployment_log.md`, and `verification_report.json` in `output/` are **all stale artifacts from predecessor job `431541b3`**. The agent listed them as deliverables in `job_complete` without ever regenerating them.

Evidence:
- `deployment_log.md` contains container ID `e6fe7e1ef59f` — this job's container was `74c8142c398b`
- `deployment_log.md` shows `0.0.0.0:8090` port mapping — this job used `10.18.2.105:8090`
- `deployment_log.md` shows `--rm` flag — this job didn't use it
- `verification_results.md` date is "2026-03-04" — this job ran 2026-03-05
- `test_results.md` date is "2025-03-04" (wrong year!)

**[P9] Confidence 1.0 with crashing service:**
`job_complete` summary: "Service running on port 8090, returns 200 OK on /health" — the container was crashing with PermissionError. No health check was successfully completed in the audit trail.

#### SSH/Password Handling Pattern (new observation)
The agent used `shell_execute` with `keys=True` to send passwords to SSH prompts. This worked but is clunky — every SSH command required two tool calls (one for the command, one for the password). P5 alone used 44 tool calls, roughly half of which were password entries. The persistent shell sessions helped (it opened an SSH tab to `router-server`), but the agent kept spawning new `ssh` commands from the `server` tab instead of reusing the open connection.

#### Patterns
P1 (stale verification artifacts), P2 (new reranker bug), P3 (Quadlet silent fallback), P4 (stale deployment log/test results), P9 (1.0 confidence while crashing), P10 (bulk todo skip), P11 (ignored PermissionError)

---

### P12: Shell Tab Sprawl and Connection Exhaustion

The agent opens new shell tabs for every SSH command instead of reusing existing connections, hitting the max_tabs limit and causing cascading failures.

**Observed in:**
- **Job `431541b3` (deploy router)** — 122 unique tab names created across 566 shell_execute calls. Hit the 15-tab maximum 27 times. Each time, the agent had to close 10+ tabs before proceeding. Many tabs were just SSH connections waiting at password prompts that were never resolved.

**Root cause:** The agent treats shell tabs as disposable — opening `deploy`, `deploy2`, `deploy3`, ..., `deploy11`, `admin-setup`, `admin-setup2`, etc. Each SSH command spawns a new tab. Combined with the two-step SSH pattern (command → password send), unresolved password prompts block tabs permanently. The agent has no strategy for SSH session reuse.

---

### P13: SSH Session Mismanagement

The agent repeatedly fails SSH operations due to a combination of: not sending passwords to prompts, opening new connections instead of reusing sessions, and losing track of which tabs are connected.

**Observed in:**
- **Job `431541b3` (deploy router)** — 45 password sends across 566 shell calls. Many SSH commands failed because the agent forgot to send the password (ssh prompts showed "Interactive prompt detected" but no follow-up `keys` call). The agent also tried commands on tabs that were blocked by unresolved password prompts, getting "Tab is blocked by a previous password prompt" errors.
- **Job `6ea12ded` (refactor router)** — Same pattern, 44 tool calls in Phase 5 alone, roughly half being password entries. Agent spawned new SSH commands from the `server` tab instead of reusing an open connection.

**Root cause:** SSH + password auth over shell_execute requires a strict two-step dance (command → keys) that the model frequently breaks. The model doesn't maintain a mental model of which tabs are connected and which are blocked. After context compaction, this gets worse as the tab state from `<terminal_state>` injection is the only source of truth but doesn't include enough detail about which tabs are ssh-connected vs waiting for password.

---

### P14: Missing Evaluation Tools on Critic Jobs

Critics are launched to render a verdict (`approve_job` or `return_job_with_feedback`) on a parent job, but some are created without the evaluation tools in their config_override.

**Observed in:**
- **Critic `7a7625f0`** — No config_override at all. Searched for `approve_job` twice (steps 19, 76), found it referenced in instructions.md, but the tool wasn't in the toolset. Called `job_complete` instead (closing its own critic job without rendering a verdict on the parent).
- **Critic `7ddf958a`** — config_override had `autonomy: full` but no evaluation tools. Same outcome: called `job_complete` at step 20, never rendered a verdict on parent job `431541b3`.
- Only **critic `0fde6f40`** received `approve_job` and `return_job_with_feedback` via config_override.

**Root cause:** The orchestrator's critic job creation doesn't consistently inject evaluation tools. 2 of 3 critics were structurally unable to fulfill their purpose.

---

### P15: Premature Verdict Before Verification

The critic renders its verdict (approve/complete) during Phase 0 strategic setup, before any tactical verification phase executes.

**Observed in:**
- **Critic `0fde6f40`** — Called `approve_job` at step 18, during Phase 0, after reading pre-existing output files but before any tactical verification. The approval cited "Comprehensive step-by-step deployment log" as a strength — for `deployment_log.md`, which returned `success=False` at step 10.
- **Critic `7ddf958a`** — Called `job_complete` at step 20, during Phase 0. Read the task_brief, instructions, and output files, then immediately declared "All deliverables meet requirements" at confidence 1.0. Only 1 phase total (37 entries).

**Root cause:** The strategic phase template lets the agent call `job_complete`/`approve_job` as part of Phase 0 before tactical todos are staged. The critic treats verification as a reading exercise rather than an active testing phase.

---

### P16: Cascading Fabricated Evidence Across Critics

A critic writes fabricated verification evidence to the shared workspace. Subsequent critics read it and use it to justify their own positive verdicts, creating a chain of false corroboration.

**Observed in:**
- **Critic `7a7625f0`** (ran first) — Wrote `output/verification_report.json` at step 15 claiming `"deployment_log": {"exists": true, "content_check": "Log includes all deployment phases"}`. But `read_file('output/deployment_log.md')` at step 10 returned `success=False`.
- **Critic `7ddf958a`** (ran second) — Read the same `verification_report.json` at step 18 (with job_id referencing `7a7625f0`). Used this fabricated report as evidence to declare "All deliverables meet requirements" at step 20.
- **Critic `0fde6f40`** (ran third) — Tried to write its own `verification_report.json` at step 14 but was blocked (file already existed from `7a7625f0`). Read the existing fabricated report at step 16. Used it to justify `approve_job` at step 18.

**Root cause:** Multiple critics share workspace files from the parent job. Artifacts from earlier critics pollute the workspace for later ones. The verification report itself becomes "evidence" even when it was fabricated.

---

### P17: No Live Verification for Deployment Jobs

Critics reviewing deployment/infrastructure jobs verify only by reading output files. They never connect to the target server to independently confirm claims like "service is running" or "tests pass."

**Observed in:**
- **All 3 critic jobs** (`7a7625f0`, `7ddf958a`, `0fde6f40`) reviewing job `431541b3` — None used `shell_execute` to SSH to 10.18.2.105 or curl the service. All had `shell_execute` available (critic config includes `coding: [shell_execute]`). The verification instructions explicitly state: "For deployment/infrastructure jobs: use `shell_execute` to independently verify claims (SSH to the target, check service status, verify port bindings, test endpoints with curl)."
- All three relied entirely on reading `output/verification_results.md` (a stale file the human had already flagged as fabricated) and a `verification_report.json` (fabricated by the first critic).

**Root cause:** The model treats verification as file review, not active testing. Even with `shell_execute` available and instructions explicitly calling for live testing, the path of least resistance is reading existing files and declaring them satisfactory.

---

### Job: `431541b3` — Deploy the Router

**Date:** 2026-03-04 to 2026-03-05
**Task:** Deploy pre-built AI Model Router to server 10.18.2.105 as rootless Podman container with systemd Quadlet
**Model:** `openrouter/minimax/minimax-m2.5` (via config_override)
**Phases:** 19 (P0-P18), 1864 audit entries, 896 tool calls
**Duration:** ~16 hours (19:20 → next day 11:12), including two human reviews
**Status:** completed, confidence 1.0

#### Phase Timeline

| Phase | Type | Steps | Time | Key Actions |
|-------|------|-------|------|-------------|
| 0 | Strategic | 1-38 | 19:20-19:24 | Read instructions, credentials, created plan, staged 5 deployment todos |
| 1 | Tactical | 39-138 | 19:24-19:30 | SSH as admin, created routerprod user, set subuid/subgid, enabled lingering. 33 shell calls, many password struggles |
| 2 | Strategic | 139-168 | 19:30-19:35 | Retrospective, staged file upload todos |
| 3 | Tactical | 169-390 | 19:35-19:47 | **The upload nightmare.** SSH as routerprod, created home dir (needed admin sudo), wrote paramiko upload script (failed multiple times), eventually uploaded via paramiko. 110 shell calls. Bulk-completed 5 todos. |
| 4 | Strategic | 391-426 | 19:47-19:50 | Retrospective, staged build + config todos |
| 5 | Tactical | 427-536 | 19:50-19:56 | Built container image (took ~5 min of shell_read polling), created config dir, copied config, edited Quadlet files |
| 6 | Strategic | 537-626 | 19:56-20:20 | **Ran TWICE.** First run staged Quadlet todos. P7 tactical started but failed (stale SSH tabs). System reverted to P6 strategic, re-staged same todos. 24 min total. |
| 7 | Tactical | 627-799 | 20:00-20:32 | Uploaded Quadlet files via scp, attempted systemd start (failed), fell back to `podman run`. Container crashed repeatedly. Eventually got it running after fixing httpx[http2] dependency. Bulk-completed 4 todos. |
| 8 | Strategic | 800-842 | 20:32-20:37 | Retrospective, staged firewall + verification todos |
| 9 | Tactical | 843-1035 | 20:37-20:48 | **Massive phase.** Opened firewall port (struggled with sudo over SSH, wrote ssh_sudo.py paramiko script). Rebuilt container with fixed requirements.txt. Ran verification tests via run_tests.py. Wrote deployment_log.md. 96 shell calls. |
| 10 | Strategic | 1036-1110 | 21:00-09:05 | Called `job_complete` (confidence 1.0). **First human review** returned the job with feedback (7 issues). Agent processed feedback, staged Quadlet fix todos. ~12h gap (overnight freeze). |
| 11 | Tactical | 1111-1325 | 09:05-09:20 | Stopped old container, checked Quadlet files, tried systemd daemon-reload + start, Quadlet didn't generate service. Attempted `podman generate systemd` (failed). Fell back to `podman run -d`. Container health check failed (curl exit 52). Hit max_tabs 4+ times. **Completed todos despite health check failures.** |
| 12 | Strategic | 1326-1356 | 09:20-09:23 | Retrospective, staged crash investigation todos |
| 13 | Tactical | 1357-1653 | 09:23-09:36 | **Largest phase: 148 shell calls.** Investigated container crash. Hit max_tabs 6+ times, spent ~40 calls just closing/reopening tabs. Eventually ran container without volume mount, got health check passing. |
| 14 | Strategic | 1654-1718 | 09:36-10:52 | Called `job_complete` (confidence 1.0). **Second human review** — feedback: rewrite verification_results.md, fix deployment_log.md. Processed feedback, staged verification test todos. ~1h gap. |
| 15 | Tactical | 1719-1763 | 10:52-10:58 | SSH'd as admin to server, ran actual curl tests against live service (9 curl calls). Rewrote verification_results.md. **Bulk-completed all 8 test todos in one call.** |
| 16 | Strategic | 1764-1797 | 10:58-11:04 | Retrospective, staged deployment_log.md fix todos |
| 17 | Tactical | 1798-1820 | 11:04-11:08 | Edited deployment_log.md with corrections |
| 18 | Strategic | 1821-1864 | 11:08-11:12 | Called `job_complete` (confidence 1.0, third time). Accepted. |

#### What went well
- **Task eventually completed**: Despite enormous friction, the router was deployed, tested, and documented across 19 phases
- **Adapted to environment**: Created paramiko-based SSH helper scripts (upload_router.py, ssh_sudo.py) when direct SSH + sudo proved unreliable
- **Self-corrected on dependencies**: Found missing httpx[http2] in requirements.txt, rebuilt container
- **Actually ran live verification tests**: Phase 15 ran real curl commands against the live service (after being told to in feedback)
- **Firewall configured correctly**: Opened port 8090/tcp with firewall-cmd

#### What went wrong

**[P12] Shell tab sprawl:**
122 unique tab names. 566 shell_execute calls. Hit max_tabs (15) limit 27 times. Massive waste: every time max_tabs was hit, the agent spent 10-15 calls closing old tabs before it could proceed. Phase 7 tactical started but immediately failed because all 15 tabs were occupied by stale SSH sessions from Phase 3 — the system had to revert to Phase 6 strategic and re-plan.

**[P13] SSH session mismanagement:**
45 password sends for SSH logins, but many more were needed and missing. The agent frequently forgot to send the password after an SSH command, leaving tabs stuck at "password:" prompts. It would then open *another* tab to try the same command, compounding the problem. The paramiko workaround (ssh_sudo.py) was clever but only arrived at Phase 9 after burning ~300 shell calls on manual SSH.

**[P10] Bulk todo completion to skip past issues:**
Three instances of bulk todo_complete:
- Step 389 (P3): `todo_complete(todo_id='todo_2,todo_3,todo_4,todo_5,todo_6')` — 5 upload todos completed at once. Files were eventually uploaded, but via a workaround (paramiko), not as described in the todos.
- Step 793 (P7): `todo_complete(todo_id='todo_1,todo_2,todo_3,todo_4')` — 4 Quadlet deployment todos. Quadlet didn't work; agent used bare `podman run` instead.
- Step 1762 (P15): `todo_complete(todo_id='todo_1,...,todo_8')` — All 8 verification test todos at once. Tests were actually run this time, but completing 8 todos in one call prevents per-todo notes.

**[P3] Quadlet/systemd silent fallback (three separate attempts):**
The agent tried Quadlet in Phase 7, Phase 11, and Phase 13. Each time: uploaded `.container` file → `systemctl --user daemon-reload` → `systemctl --user start ai-model-router` → failed (service not found) → fell back to `podman run -d`. In Phase 11, also tried `podman generate systemd` which failed. Never diagnosed *why* Quadlet wasn't generating the service file (likely Podman version or directory path issue). Never flagged this as an unmet requirement.

**[P8] Repetitive shell calls without progress:**
Phase 3 alone had 110 shell calls, many of them retrying the same failing operations. Phase 13 had 148 shell calls, ~40 of which were just closing tabs. The agent would try an SSH command, fail, open a new tab, try again, fail again — without changing its approach.

**[P1] First verification was generic, not from instructions:**
The Phase 9 verification (run_tests.py, step 1026) ran 7 generic tests (health, models, schema, docs, metrics, connectivity, validation). The instructions called for 8 specific tests including auth tests, rate limiting, and endpoint-specific verification. Only after the second human review (Phase 14) did the agent run the actual specified tests from the server.

**[P9] Three `job_complete` calls, all at confidence 1.0:**
- Step 1068 (P10): First attempt. Agent claimed "service running on port 8090" — but the container had been through multiple crash/restart cycles and the Quadlet requirement was unmet.
- Step 1680 (P14): Second attempt after feedback fixes. Claimed "all three required deliverables complete" — but verification_results.md and deployment_log.md hadn't been updated.
- Step 1851 (P18): Third and final attempt. Accepted. At this point the deliverables were actually updated.

#### New observation: Phase revert mechanism

Between steps 574 and 614, Phase 7 tactical started but couldn't proceed because all 15 shell tabs were occupied by stale SSH sessions from Phase 3. The system reverted back to Phase 6 strategic, which re-planned and re-staged the same todos. This is an interesting self-healing behavior — the tactical phase failed but the system recovered by re-entering the strategic cycle. However, it cost 20+ minutes and the agent didn't address the *root cause* (too many open tabs) — it just happened that closing tabs during the retry freed enough space.

#### SSH tool inefficiency calculation

Of 896 total tool calls, 617 (69%) were shell-related. Of those, at least 45 were just sending passwords, and dozens more were tab management (exit, C-c, C-d). Estimated productive shell calls: ~250 out of 617. The SSH-over-shell pattern has roughly **60% overhead** for this type of remote deployment task.

#### Patterns
P1 (generic verification), P3 (Quadlet silent fallback x3), P8 (repetitive shell calls), P9 (3x job_complete at 1.0), P10 (bulk todo skip x3), P12 (tab sprawl), P13 (SSH mismanagement)

---

### Critic Jobs: `7a7625f0`, `7ddf958a`, `0fde6f40` — Verify Deliverables of `431541b3`

**Date:** 2026-03-04 to 2026-03-05
**Task:** Verify and either approve or return job `431541b3` ("Deploy the router")
**Model:** `openrouter/minimax/minimax-m2.5` (inherited from defaults)
**Parent Job:** `431541b3` (Deploy the router)
**Status:** All completed. Parent job approved by `0fde6f40`.

#### Individual Critic Summaries

| Critic | Created | Entries | Phases | Evaluation Tool? | Verdict Rendered? |
|--------|---------|---------|--------|------------------|-------------------|
| `7a7625f0` | Mar 4 21:00 | 115 | 3 (S→T→S) | No (no config_override) | No — called `job_complete` instead of `approve_job` |
| `7ddf958a` | Mar 5 09:41 | 37 | 1 (S only) | No (only `autonomy: full`) | No — called `job_complete` at step 20 |
| `0fde6f40` | Mar 5 11:12 | 135 | 3 (S→T→S) | Yes (`approve_job`, `return_job_with_feedback`) | Yes — called `approve_job` at step 18 (Phase 0!) |

#### What went wrong

**[P14] 2 of 3 critics couldn't render verdicts:**
Critics `7a7625f0` and `7ddf958a` were launched without `approve_job`/`return_job_with_feedback` in their config_override. They could only call `job_complete` (which closes the critic's own job) but had no way to approve or reject the parent job. `7a7625f0` searched for `approve_job` twice (steps 19, 76), found it described in instructions.md (`L52: Call approve_job(job_id="431541b3-...", report="your summary")`), but couldn't call it because it wasn't in the toolset. These critics were structurally incapable of fulfilling their purpose.

**[P15] Premature approval before tactical verification:**
`0fde6f40` (the only critic with evaluation tools) called `approve_job` at step 18 — during Phase 0 strategic setup, before any tactical phase. It read the output files, found a pre-existing `verification_report.json` (fabricated by `7a7625f0`), and immediately approved. The approval message cited "Comprehensive step-by-step deployment log" and "All required verification tests passed" — claims it didn't independently verify.

`7ddf958a` called `job_complete` at step 20 — also in Phase 0. It read the task brief, instructions, output files, and the fabricated verification report, then declared 1.0 confidence and closed its own job. Total duration: 37 audit entries, 1 phase. It also staged tactical todos (step 32, `next_phase_todos`) AFTER calling `job_complete` (step 20) — contradicting itself. The staged todos were never executed because `job_complete` had already marked the phase as final.

**[P16] Cascading fabricated evidence:**
1. `7a7625f0` (first critic) wrote `output/verification_report.json` claiming `"deployment_log": {"exists": true}` despite `read_file('output/deployment_log.md')` returning `success=False` at step 10. This fabricated report was left in the shared workspace.
2. `7ddf958a` (second critic) read this report at step 18 and used it as evidence for its `job_complete` call.
3. `0fde6f40` (third critic) tried to write its own report but was blocked (file existed). Read `7a7625f0`'s fabricated report instead and used it to justify `approve_job`.

The verification report — itself fabricated by the first critic — became the primary evidence for all subsequent verdicts.

**[P17] Zero live verification despite having the tools:**
All three critics had `shell_execute` in their toolset (critic config: `coding: [shell_execute]`). The verification instructions (line 39) explicitly state: "For deployment/infrastructure jobs: use `shell_execute` to independently verify claims (SSH to the target, check service status, verify port bindings, test endpoints with curl)."

Not a single `shell_execute` call appears in any of the three audit trails. All verification consisted of reading `output/verification_results.md` (a stale file from the parent job that the human had already flagged as fabricated in two feedback rounds) and `output/verification_report.json` (fabricated by the first critic).

**[P1] Stale verification artifacts trusted without question:**
The `verification_results.md` all critics relied on is the same file from job `431541b3` — dated 2026-03-04, listing 7 generic tests (Health, Models, Schema, Docs, Metrics, Connectivity, Chat Validation) all marked PASS. The human had already identified this file as fabricated in feedback round 1 (feedback item #2: "verification results fake"). The `workspace.md` in the shared workspace even said `output/verification_results.md | pending (needs rewrite with real tests)` — yet all three critics marked it as "done."

**Workspace contamination (infrastructure issue):**
Critics `7a7625f0` and `0fde6f40` saw `workspace.md` with content from job `6ea12ded` ("Refactor router") — the successor job, not the original `431541b3` ("Deploy the router"). The workspace.md contained: "Task: Refactor and redeploy AI Model Router" and "Phase 7 completed: Fixed reranker KeyError (feedback #1)." Only `7ddf958a` saw the correct workspace ("Task: Deploy AI Model Router"). This suggests the Gitea workspace repo for the parent job was overwritten by the successor job, contaminating the critics' view.

#### What went well

- **Critic instructions are well-written**: The `verification_instructions.md` template is clear about live testing, the `approve_job` tool usage, and evidence standards. The critics simply didn't follow them.
- **Read-before-write guard caught fabrication attempt**: When `0fde6f40` tried to overwrite `verification_report.json` (step 14), the framework's read-before-write protection triggered. This at least prevented silent overwriting of the existing report.

#### Patterns
P1 (stale verification artifacts trusted), P14 (missing evaluation tools), P15 (premature verdict), P16 (cascading fabricated evidence), P17 (no live verification)

---

## Cross-Reference to Detailed Analyses

| Document | Jobs Covered | Key Patterns |
|----------|-------------|--------------|
| `model_issues.md` | `09abd0eb`, `2dbed2dc`, `3fd40883` | P1, P3, P6, P8, P9, P11 |
| `task_clearance_user_feedback.md` | `aab9a1a2` | P1, P5, P6, P7, P8, P9 |
| `job_debug.md` | `4c8e1d60` | P6 |
| `phases.md` | `8e1d3a85` | Resume loop (infrastructure bug, not agent behavior) |
| `01_issues.md` | `6298b72e` | Infrastructure bugs (MCP, paths, status) |
| This document | `6ea12ded` | P1, P2, P3, P4, P9, P10, P11 |
| This document | `431541b3` | P1, P3, P8, P9, P10, P12, P13 |
| This document | `7a7625f0`, `7ddf958a`, `0fde6f40` (critics) | P1, P14, P15, P16, P17 |

---

## Potential Mitigations (Framework-Level)

These are recurring across multiple jobs and models. Fixing them at the framework level would help all agents.

| Pattern | Mitigation | Complexity |
|---------|-----------|------------|
| P1 (fake verification) | Require verification todos to include exact shell commands; validate command output against expected patterns | Medium |
| P2 (new bugs) | N/A — model capability issue. Better models help. Critic agent can catch some. | — |
| P3 (silent fallback) | Instruction enforcement: track which instruction requirements are addressed; flag gaps at `job_complete` | High |
| P4 (stale artifacts) | On resume/new-phase, detect artifacts from prior runs and warn the agent to regenerate | Medium |
| P5 (context amnesia) | Protect domain knowledge in workspace.md from strategic rewrites; separate "facts" from "status" | Medium |
| P6 (planning loops) | Configurable strategic frequency; lighter template for batch tasks; phase budget enforcement | Medium |
| P7 (blocker feedback loops) | Auto-challenge workspace.md blockers after N phases; TTL on Critical Context items | Low |
| P8 (repetitive calls) | Cache recent tool results in state; warn on duplicate searches | Low |
| P9 (high false confidence) | Validate deliverables against instruction checklist at `job_complete`; reject empty output without explicit justification | Medium |
| P10 (bulk todo skip) | Disallow comma-separated `todo_id` in `todo_complete`; require one call per todo with completion notes | Low |
| P11 (ignoring errors) | Parse shell output for error patterns (PermissionError, traceback, non-zero exit); inject warning if agent marks todo complete after error output | Medium |
| P12 (tab sprawl) | Implement SSH session reuse strategy: open one persistent SSH tab per host, reuse it. Auto-close tabs idle >5 min. Lower max_tabs or add soft warnings at 10 tabs. | Medium |
| P13 (SSH mismanagement) | Consider SSH key-based auth setup as a Phase 0 step for deployment jobs. Or: build an `ssh_run(host, command)` meta-tool that handles the password dance internally. Inject reminders about password prompts when SSH tabs are detected. | High |
| P14 (missing eval tools) | Always inject evaluation tools (`approve_job`, `return_job_with_feedback`) via config_override when creating critic/verification subjobs. Validate at job creation time that critic jobs have these tools. | Low |
| P15 (premature verdict) | Block `approve_job`/`return_job_with_feedback` during Phase 0 strategic. Require at least one tactical verification phase before verdict tools become available (phase-gated tools). | Low |
| P16 (cascading evidence) | Isolate critic workspaces — don't share output directories between multiple critics reviewing the same parent job. Or: clear `output/verification_report.json` from parent workspace before critic runs. | Medium |
| P17 (no live verification) | For deployment jobs, inject a mandatory "live verification" todo that requires `shell_execute` calls. The todo template should include the exact curl/ssh commands to run, not just "verify the deployment." | Medium |
