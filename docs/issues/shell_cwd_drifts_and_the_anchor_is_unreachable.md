# Shell CWD drifts invisibly and the anchor that would fix it is unreachable (2026-08-01)

**What this is:** an implementer-ready writeup of one root cause found while investigating job
`d1894a91-009f-4413-9a3a-b15c4b364e0c` (developer/MiniMax-M3, project Better Resavio, dev cluster).
The job burned **15.7 hours** and ~750 tool calls without producing its contracted deliverable. It
was not a model failure in the usual sense: it wrote the right file with the right contents and
verified its SHA-256 correctly. It wrote it to the wrong directory, and nothing in the harness could
tell it so.

**Status:** **IMPLEMENTED** in `f41970ae` (all four slices, 15 files) and documented in `e7d29b2d`,
2026-08-01. Independently verified the same day against all six acceptance criteria, including a
by-hand replay of the `d1894a91` failure on the local k3d workspace. The drifted shell CWD now prints
as `CWD:` and the `read_file` error names the workspace-resolved absolute path, exposing the
off-by-one in one turn. The findings below describe the tree at `572c40ba`, i.e. *before* the fix;
they are kept as the record of the defect.

**Residue (open, small):** the sync-path `cd` sends (`remote.py` restore/anchor sites) are unquoted
f-strings while the async wrapper `shlex.quote`s — a `working_dir` containing spaces fails its `cd`
loudly (no silent drift, error visible in output + `CWD:` line), but quoting them is a two-line
consistency fix. Also note `run_command`'s docstring deliberately does NOT carry the "inline cd
persists" sentence — post-fix it would be false there, since stateless mode now restores on every
call.

**One-line summary:** `shell_execute`/`run_command` run in a persistent tmux tab whose working
directory an inline `cd` changes permanently, while `read_file`/`write_file` resolve against the
fixed workspace root — so the same relative path string means two different files, forever, with no
signal at either end. The mechanism that would prevent this (`working_dir`) is plumbed all the way
down to the backend but **is not a parameter on either model-facing tool**, so it is dead code in
production.

---

## What happened (evidence)

The job's first todo, verbatim from the archive:

> Recover `repo/tests/test_web_app.py` verbatim from `f8f8cfb` via
> `git show f8f8cfb:repo/tests/test_web_app.py > repo/tests/test_web_app.py` and verify SHA-256
> matches `dccae8e949edc7cfbdf20aa618520d26f599507e4195b6eaa668b1addf144f26`.

The agent ran that command from a tab it had earlier moved with an inline
`cd /home/agent-host/workspace/repo` (audit entries 194, 195, 1546). `_sandbox_cwd` — the root
`read_file` resolves against — is `/home/agent-host/workspace`. So:

| | resolves to |
|---|---|
| the shell redirect `> repo/tests/test_web_app.py` (CWD = `…/workspace/repo`) | `…/workspace/repo/repo/tests/test_web_app.py` |
| `read_file("repo/tests/test_web_app.py")` (root = `…/workspace`) | `…/workspace/repo/tests/test_web_app.py` |

Confirmed on the live workspace:

- `repo/repo/tests/test_web_app.py` — **exists, 20.7 KB, correct content**
- `repo/tests/test_web_app.py` — **absent** (45 other test files are present in that directory)
- `repo/repo/repo/` — a third level of junk nesting from the same off-by-one

The todo's own verification passed, because `sha256sum` checked **content and never location**. The
agent marked the todo complete and committed it (`a66576e3`, 18:12 UTC). Everything after that was
built on a false green.

The remaining ~14 hours: the test file is the spec for `web_app.py`'s public surface. Unable to read
or run it, the agent repeatedly introspected its own module
(`have: ['CalculatorGuest', 'CalculatorReservation', 'ReservationService', 'SCHEMA', 'application', …]`)
trying to reconstruct the contract from memory. Identical command, identical output, at audit
entries 3133, 3148, 3159, 3212 — the last at 08:56 UTC the following morning.

**Loop detection fired and did not help.** Seven `[LOOP WARNING]` injections, all on `read_file` of
the missing path (entries 980, 996, 1049, 1073, 1546, 2785, 2807). The nudge says "consider a
different approach… or mark the current todo as blocked and move on" — avoidance advice. The agent
needed *"your file is at `repo/repo/tests/`"*, which nothing in the system is able to say.

---

## Root cause — three defects on one path

### 1. `working_dir` is plumbed end-to-end but never exposed to the model

The anchor mechanism exists and is correct:

- `src/core/workspace_backend.py:538` — `working_dir: Optional[str] = None` on the backend interface
- `src/tools/shell/shell_manager.py:532` — `run_sync(command, timeout, working_dir, tab_name)`,
  forwards to `self._backend.shell_run(..., working_dir=working_dir)`
- `src/core/backends/remote.py:1249` — `if working_dir:` → `cd {posixpath.join(_sandbox_cwd, working_dir)}`
- `src/core/backends/remote.py:1384` — *"Completed — restore working directory if it was changed"*
  → `cd {_sandbox_cwd}`

It stops one layer short of the LLM. Neither model-facing tool has the parameter:

- `src/tools/shell/shell_tools.py:372` — `run_command(command, timeout, tail)`
- `src/tools/shell/shell_tools.py:495` — `shell_execute(command, name, tail, is_async, keys, timeout)`

Both call sites drop it:

- `shell_tools.py:446` — `sm.run_sync(command, tab_name="default", timeout=timeout)`
- `shell_tools.py:636` — `sm.run_sync(command, tab_name=name, timeout=timeout)`

**Consequence:** `working_dir` is always `None` in production, so the restore at `remote.py:1384` is
gated on a condition that is never true. **It has never fired for any job.** The only way an agent
can change directory is the one way that is never undone.

This is not "the model didn't know about the parameter." The parameter is not in the schema; the
model cannot set it.

### 2. The stateless/persistent split already exists — and neither half is honoured

Each tab *is* `cd`'d to `_sandbox_cwd` at creation — `remote.py:1091` for the initial `default` tab,
`remote.py:1618` for every `tmux new-window` (`remote.py:1613`). So a new tab never inherits another
tab's directory. The drift is purely *within* a tab, and it is permanent.

We already have the architecture this issue calls for. `create_shell_tools`
(`src/tools/shell/shell_tools.py:327`) binds **mutually exclusive** tool sets by `shell.mode`:

- `"stateless"` (the code default) → `[run_command, shell_read]`
- `"persistent"` → `[shell_execute, shell_read]`

`config/worker_base.yaml:506` deliberately leaves `mode` unset so it derives from the model family
(`config/model_config_matrix.yaml` `settings.shell_mode`), with a stateless floor for families that
have no entry. That is the right shape — Codex's split, expressed as config.

Two things defeat it.

**Every configured family is `persistent`.** All twelve `shell_mode` entries in
`config/model_config_matrix.yaml` (`:132, :156, :206, :244, :257, :273, :289, :305, :332, :372,
:400, :423`) read `persistent`. The stateless floor only applies to a family with no matrix entry,
so in practice no production job gets it. The split exists on paper only.

**And "stateless" is not stateless anyway.** `run_command` calls
`sm.run_sync(command, tab_name="default", timeout=timeout)` (`shell_tools.py:446`) — the same
persistent tmux tab, with `working_dir` omitted, so defect #1 applies to it identically. A
`run_command(command="cd foo && …")` drifts its own tab permanently. Both modes therefore behave the
same way, and the difference is only which tool name the model sees.

That makes two statements the model is given untrue. `run_command`'s docstring
(`shell_tools.py:380`):

> Commands run in the workspace directory.

— false after its own first inline `cd`. And the comments at `shell_tools.py:207` and `:465` call
`default` "the single stateless tab", which it has never been.

### 3. Both ends are silent

- `shell_execute` returns `Exit code: N` + stdout. It never reports where it ran.
- `read_file`'s not-found error is `f"Error: File not found: {path}"` — the *requested* relative
  path, never the resolved absolute one (`src/tools/workspace/files.py:756`, `:870`, `:1044`).

Either one alone would have ended this in a single turn. Together, the mismatch is unobservable from
inside the agent — and, as it turned out, from outside too: the supervising officer reads pushed
Gitea state and saw the same absence the agent saw, never the file sitting one level deeper.

---

## How Claude Code and Codex handle this

Both were checked against source, not recalled.

**Codex** ([`shell_spec.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/shell_spec.rs),
[`unified_exec.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/unified_exec.rs))
separates *persistent process* from *persistent directory*. A `session_id` is issued only for a
process that is still running, addressed via `write_stdin`. Ordinary commands take a `workdir`
parameter, documented as "Defaults to the turn cwd" — re-anchored per call, never drifting. The
handler carries this comment above the field:

> `// Keep this raw until after environment selection; relative paths must be resolved against the`
> `// selected environment cwd, not the process cwd.`

And the entire model-facing description of their shell tool on Unix is two lines:

> Runs a shell command and returns its output.
> - Always set the `workdir` param when using the shell_command function. Do not use `cd` unless absolutely necessary.

**Claude Code** avoids the problem by not having a persistent shell: CWD is tracked by the harness
rather than carried by a live process, shell state (env vars, functions) does not persist, and
Read/Write/Edit accept absolute paths only. When a command leaves the shell elsewhere, the harness
resets it and emits `Shell cwd was reset to <path>` — a runtime signal requiring no model compliance.

**The shared principle:** ambient shell state is opt-in and explicit in both. We already agree with
it — `shell.mode` is exactly that opt-in. But every family opts in, and the opt-*out* half was never
built to be stateless, so the principle is stated in our config and contradicted by our code.

**Which to copy.** Codex's architecture is the portable one — we need real persistent tabs (dev
servers, `keys=True`, `shell_read`), so Claude Code's "no persistent shell" is not available to us
without removing a feature. But Codex's defense is a *prompt* instruction, and we deliberately run
non-frontier models: this job ignored seven plain-language LOOP WARNINGs. So take Codex's structure
and Codex's instruction, and put Claude Code's *runtime* signal underneath both as the part we
actually rely on.

---

## The fix

Ordered by value-per-effort. Slices 1–3 are independent and can land separately; slice 4 depends on 1.

### Slice 1 — expose `working_dir` on both shell tools

Add `working_dir: Optional[str] = None` to:

- `run_command` (`shell_tools.py:372`) → forward at `shell_tools.py:446`
- `shell_execute` (`shell_tools.py:495`) → forward at `shell_tools.py:636`

Document it as *relative to the workspace root* (that is what `remote.py:1251` joins against).

**Two traps.** `shell_execute` has three paths and `working_dir` is only meaningful on one:
- `keys=True` sends keystrokes to a live process — the parameter is meaningless; reject it or ignore
  it, do not silently `cd`.
- `is_async=True` dispatches via `_tmux_send_keys` without the sentinel wrapper, so the restore at
  `remote.py:1384` does not run. Either wire the restore for that path too or document that async
  commands do not restore.

**Free win on the same change.** In stateless mode, have `run_command` *always* pass a
`working_dir` (defaulting to the workspace root rather than `None`). That makes `remote.py:1249`
`cd` in and `remote.py:1384` restore on every call, which is what "stateless" was supposed to mean
all along — the mode becomes honest for the cost of one non-`None` default, and its docstring at
`shell_tools.py:380` becomes true instead of aspirational.

### Slice 2 — report the resolved CWD on every shell result

Include the tab's working directory in the result alongside the exit code
(`remote.py` ~`:1392`, the `parts = [f"Exit code: {exit_code}"]` block).

Prefer folding it into the existing sentinel command (see `build_sentinel_command`, referenced at
`remote.py:1262`) so it costs no extra SSH round-trip. `tmux display-message -p '#{pane_current_path}'`
works but adds a round-trip per command — acceptable fallback, not the first choice.

This is the highest-value slice. It converts an invisible, permanent condition into a fact printed
on every single call, and it works regardless of whether the model obeys instructions.

### Slice 3 — resolved absolute path in `read_file`'s not-found error

At `files.py:756`, `:870`, `:1044`, replace the bare message with the resolved absolute path and a
recovery hint. Use the module's existing path-resolution helper — do not re-implement the join.

```
Error: File not found: /home/agent-host/workspace/repo/tests/test_web_app.py
  (resolved from workspace root; you passed "repo/tests/test_web_app.py")
  Use `search_files` to locate it if you expected it elsewhere.
```

Note `search_files` and `list_files` were both enabled for this job and never called once. Naming
the tool at the moment of failure is the cheapest way to build the reflex.

### Slice 4 — tool descriptions and guardrail examples

- Fix the false claim at `shell_tools.py:380` ("Commands run in the workspace directory") and the
  "single stateless tab" comment near `:437`.
- Add Codex's line to both tool descriptions: *"Always set `working_dir`. Do not use `cd` unless
  absolutely necessary — an inline `cd` persists for the rest of the job."*
- Update the examples in `config/guardrails/default.yaml` (`run_command` at `:94`, `shell_execute`
  at `:101`) so **every** example passes `working_dir`. Examples are what models copy.
- Check the family variants (`config/guardrails/{gemma,gpt_oss,codex,…}.yaml`) — they deep-merge over
  `default.yaml`, so only variants that *override* these two keys need the same edit.

---

## Acceptance criteria

1. `run_command` and `shell_execute` both accept `working_dir`; it reaches
   `remote.py:1249` and the restore at `remote.py:1384` fires. Prove with a test that asserts the
   restore, not just the initial `cd`.
2. **Persistent mode keeps its state, visibly.** After
   `shell_execute(command="cd /some/where && pwd")`, a subsequent `shell_execute(command="pwd")` on
   the same tab still reports the drifted directory — we are not taking the feature away — **but the
   result string now names the directory it ran in.**
3. **Stateless mode is actually stateless.** After `run_command(command="cd /tmp && pwd")`, a
   subsequent `run_command(command="pwd")` reports the workspace root. Drift does not survive the
   call. This is the criterion that distinguishes the two modes; today nothing does.
4. `read_file` on a missing path returns the resolved absolute path and mentions `search_files`.
5. No guardrail example in any family file uses a bare inline `cd`.
6. Replaying the failure by hand: with CWD drifted one level, `read_file("repo/tests/x")` and a
   shell redirect to `repo/tests/x` produce output that makes the mismatch evident without needing
   to already know about it.

## Implementation and verification record (2026-08-01)

| Criterion | Result | Evidence |
|---|---|---|
| 1. Both tools forward `working_dir` and restore it | **Pass** | `tests/test_run_command.py` asserts forwarding/default normalization. `TestRemoteBackendShellRun::test_working_dir_restores_between_calls` asserts both the initial `cd` and the restore command. `TestRemoteBackendShellSend::test_async_working_dir_wraps_command_and_restore` covers the async path; raw keystrokes reject a backend-level `working_dir`. |
| 2. Persistent mode preserves drift and reports it | **Pass** | `TestRemoteBackendShellRun::test_without_working_dir_keeps_persistent_cwd_visible` reports `/tmp` on both calls. The live k3d replay also left a persistent tab in `/tmp`, with both results reporting `CWD: /tmp`. |
| 3. Stateless mode restores the workspace root | **Pass** | `TestRemoteBackendShellRun::test_working_dir_restores_between_calls` reports `/tmp` for the command and the workspace root on the following call. The live k3d replay produced the same sequence. |
| 4. Missing reads identify the resolved path and recovery tool | **Pass** | The three not-found paths are covered by `test_read_file_error_reports_resolved_path_and_search_hint`, `test_read_file_race_uses_same_resolved_not_found_message`, and `test_missing_file_reports_resolved_path_and_search_hint`. |
| 5. Guardrail examples anchor commands | **Pass** | `TestResolveGuardrails::test_all_shell_example_overrides_set_working_dir_without_inline_cd` scans the default and family overrides; every shell example sets `working_dir` and none uses inline `cd`. Only `gemma.yaml` overrides the affected keys and therefore needed the family edit. |
| 6. Original mismatch is visible in one turn | **Pass** | On the live k3d workspace, a shell drifted one level reported that directory in `CWD:`, while `read_file("repo/tests/x")` reported the different workspace-root absolute path and suggested `search_files`. |

Verification totals:

- Focused shell/workspace/guardrail tests: **322 passed, 0 skipped**.
- `pytest tests/test_run_command.py -q -rs`: **29 passed, 0 skipped**. This file ran; it did not
  auto-skip.
- Full pre-change baseline, without `-x`: **8 failed, 12,285 passed, 23 skipped, 33 warnings** in
  775.71s.
- Full post-change suite, without `-x`: **8 failed, 12,299 passed, 23 skipped, 35 warnings** in
  784.60s. The same eight environment-dependent cases failed: one local PostgreSQL connection to
  `:5432` and seven live-MCP cases. There were no new failures.
- `ruff check src/ orchestrator/ tests/`: **passed**.
- `ruff format --check src/ orchestrator/ tests/`: **882 files already formatted**.
- Live k3d SSH/tmux exercise: **passed** for persistent drift visibility, stateless restore, async
  restore, raw-keystroke handling, and the shell/read mismatch replay. The temporary tmux session
  and test directory were removed afterward.

Contradictions found while implementing the verified write-up:

- The cited `files.py:1044` site was the `edit_file` precheck, not a `read_file` branch. It was
  relocated by symbol and updated because it is the third named not-found site.
- `tests/test_run_command.py` does not use real tmux and does not contain an auto-skip. It was already
  mock-backed at the reference commit. Real tmux therefore needs the separate k3d behavioral gate.
- This checkout's pre-existing full-suite baseline was eight failures, not approximately eleven;
  the two anticipated CI-workflow assertion failures did not occur.

## Tests

- `tests/test_run_command.py` scripts `backend.shell_run`; it is not tmux-dependent and has no
  auto-skip. It covers model-facing schemas, forwarding, defaults, and tool-mode behavior.
- `tests/test_workspace_backends.py::TestRemoteBackendShellRun` and
  `::TestRemoteBackendShellSend` prove the sentinel parsing and exact SSH/tmux commands, including
  restore commands. These are transport-mocked tests, not a live tmux integration test.
- The automated suite has no live-tmux end-to-end coverage. For shell behavior changes, use the
  local k3d workspace SSH/tmux exercise as the live behavioral gate.
- Drive `WorkspaceManager` against `tests/_fs_backend.py::FilesystemTestBackend` for the
  `read_file` message change (no SSH needed).
- Full gate: `pytest tests/ -q --tb=short`, then
  `ruff check src/ orchestrator/ tests/` and `ruff format --check src/ orchestrator/ tests/`.
- Always capture the full-suite baseline without `-x`. The 2026-08-01 implementation run had eight
  pre-existing environment-dependent failures (one live PostgreSQL and seven live-MCP cases); see
  the exact before/after totals above rather than assuming a fixed known-noise count.

## Out of scope / follow-ups

- **The wider class.** CWD is only what bit us. A persistent tab also carries env vars, activated
  virtualenvs and shell functions — equally sticky, equally invisible. The slices above fix CWD and
  make the mode boundary real; the rest of the class survives in `persistent` mode by design.
- **Is `persistent` the right default for every family?** All twelve matrix entries choose it, on
  the rationale "capable family". This job is the counter-example: MiniMax-M3 is capable enough to
  be granted a stateful multi-tab shell and then lost 15.7 hours to exactly the statefulness that
  grant confers. Capability at tool-calling is not the same axis as capability at tracking invisible
  shell state, and the matrix currently conflates them. Worth re-deciding once slices 1–2 land and
  the drift is at least visible — not before, since the data we have is from a build where it wasn't.
- **Contract-path verification.** A todo that verified content but not location produced a false
  green that misled the job for 14 hours. This is what the `manifest_status.json` /
  `required_deliverables` seal is for — see `docs/issues/phase_model_overhead_amnesia_loop.md` §P-2.
- **Todos that embed literal commands.** The brief handed the agent an exact command string carrying
  a hidden CWD assumption. Todos should state the contract (file at path P with hash H) and let the
  agent choose the command. Filed here for the record; belongs with the strategic-todo templates.
- **Loop detection cannot diagnose.** Seven warnings, no effect, because the nudge advises avoidance
  rather than naming the discrepancy. Related:
  `docs/issues/agent_phase_guardrails_burn_legitimate_work.md`.
