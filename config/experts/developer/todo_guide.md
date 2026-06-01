# Todo Crafting Guide — Developer (TDD)

**You MUST read this file before calling `next_phase_todos`.** The tool will reject your call if you haven't. This guide teaches you how to create execution-ready, phase-typed, AC-traced todos.

---

## Core Principle: Spec → Red → Green → Refactor

Every developer phase is one of these types. The strategic phase chooses; the tactical phase honors that choice:

| `tdd_phase` | Goal | Writes Allowed | Writes Forbidden | Exit Gate |
|---|---|---|---|---|
| `spec` | Define `spec.yaml` with EARS acceptance criteria | workspace root (spec.yaml, spec_lock.md, plan.md) | `src/`, `tests/` | spec.yaml exists, AC have IDs and test_oracle, locked into spec_lock.md |
| `red` | Write failing tests | `tests/` | `src/` | Every in-scope AC has a test that fails with AssertionError (not ImportError) |
| `green` | Minimum implementation to pass tests | `src/` | `tests/` | All in-scope tests pass; no test edits; full suite green |
| `refactor` | Improve structure while keeping green | `src/` | `tests/` | All previously-green tests stay green |
| `integration` | PR / commit prep | minimal edits | scope changes | Commit pushed, AC IDs in commit message |

**Pick exactly one tdd_phase per tactical phase.** Mixing types in one phase is the failure mode that erases the TDD discipline.

**Target: 5-10 todos per tactical phase.** Adapt based on complexity:
- Spec phase: 3-5 todos (interview, write spec, lock to spec_lock.md, init matrix)
- Red phase: 1 todo per behavior under test (target 5-8)
- Green phase: 1 todo per failing test (target 5-8)
- Refactor phase: 3-5 focused structural changes
- Integration phase: 3-5 todos (review diff, commit, push, verify)

---

## Every Todo Must Trace to the Spec

Every red/green/refactor todo names the **AC ID(s) it serves** in its content (e.g., `AC-1`, `AC-2`). After the phase, the traceability matrix in `spec_lock.md` shows the AC status. Todos that don't trace to any AC are scope creep — either add the AC to the spec (deliberately, recorded), or drop the todo.

---

## Todo Specificity Rules

Every todo must be specific enough to act on immediately.

### Vague → Execution-Ready Examples

| Phase | Vague (fails) | Execution-ready (works) |
|---|---|---|
| spec | "Define the requirements" | "Interview user about magic-link TTL extension via kb_search of related decisions. Produce spec.yaml with 2-4 EARS acceptance criteria covering: (1) link click resets deadline, (2) watchdog skips awaiting threads. Each AC has an ID and test_oracle path. Lock spec into spec_lock.md `## Acceptance Criteria` section." |
| red | "Write tests for the feature" | "Write failing test for AC-1 in repo/tests/test_persistent_ttl.py::test_magic_link_extends_deadline. Cover: thread in awaiting_user state, magic_link_clicked event arrives, awaiting_user_deadline must equal now + ttl_default. Follow fixture style from repo/tests/test_persistent_chat.py. Run `pytest tests/test_persistent_ttl.py::test_magic_link_extends_deadline -x -v` — confirm AssertionError, NOT ImportError." |
| red | "Add a test for the bug" | "Write regression test for AC-3 (auth bypass on empty session) in repo/tests/test_auth.py::test_empty_session_rejected. Cover: empty string session token returns 401. Follow style from repo/tests/test_auth.py::test_invalid_token. Run `pytest tests/test_auth.py::test_empty_session_rejected -x -v` — must fail with assertion mismatch on status code." |
| green | "Implement the endpoint" | "Make AC-1 test pass: in repo/src/persistent/lifecycle.py:handle_event, add MagicLinkClicked branch that sets thread.awaiting_user_deadline = now() + ttl_default. Reference pattern: repo/src/persistent/lifecycle.py:handle_idle. Forbidden: editing tests/. Run `pytest tests/test_persistent_ttl.py -x` — must go red → green; then `pytest tests/ -x` — full suite stays green." |
| green | "Fix the bug" | "Make AC-3 test pass: in repo/src/auth/login.py:45, add guard `if not session_token: return Response(status=401)` before token validation. Run `pytest tests/test_auth.py::test_empty_session_rejected -x` — must turn green; `pytest tests/test_auth.py -x` — no other tests turn red." |
| refactor | "Clean up the code" | "Extract MagicLinkClicked handler from repo/src/persistent/lifecycle.py into repo/src/persistent/handlers/magic_link.py. Behavior identical. Update import in lifecycle.py. Run `pytest tests/ -x` — every previously-green test stays green." |

### What Makes an Execution-Ready Todo

1. **`tdd_phase` is set** — matches the phase's tdd_phase
2. **AC ID(s) referenced** — every red/green/refactor todo cites the AC it serves
3. **Target files named** — exact paths, with the directory restriction implied by tdd_phase
4. **Specific change named** — what to add, fix, or modify (and for green: the minimum needed)
5. **Reference pattern named** — an existing file/function that shows the convention
6. **Verification command named** — the test/check to run, AND the expected initial state (must-fail for red, must-pass for green)

### The Specificity Test

Before finalizing each todo, ask: "Could I open this todo, read the files it names, and start typing immediately — knowing which AC I'm satisfying, which directory I'm allowed to write to, and what success looks like?"

---

## Phase Design Patterns

### 1. Spec Phase (first phase for any non-trivial work)

Purpose: Lock the acceptance criteria before any code or test exists.

Example todos:
{% if has_tool("kb_search") -%}
- "Read task_brief.md and instructions.md in full. Search kb (`kb_search`) for prior decisions related to this feature. Record findings via kb_write (type=learning, tag=prior-context)."
{% else -%}
- "Read task_brief.md and instructions.md in full. Note all prior context in spec_lock.md."
{% endif -%}
- "Explore the existing codebase to identify the test framework, test command, and a 2-3 representative tests that match the style we'll need. Record framework + test command via kb_write (type=learning, tag=repository)."
- "Write spec.yaml with EARS acceptance criteria covering [describe scope]. Each AC has an ID (AC-1, AC-2, ...), an EARS statement, and a test_oracle path. Include `not_included` (explicit scope boundaries) and `done_when` (exact commands)."
- "Lock spec into spec_lock.md `## Acceptance Criteria` section (PROTECTED). Initialize the traceability matrix with each AC at `not_started`."

### 2. Red Phase (write failing tests)

Purpose: For each AC, write a test that fails for the right reason (AssertionError, not ImportError).

Example todos:
- "Write failing test for AC-1 in repo/tests/<area>/test_<feature>.py::test_<behavior>. Cover: <Given> <When> <Then>. Follow fixture style from repo/tests/<neighbor>. Run `pytest tests/<area>/test_<feature>.py::test_<behavior> -x -v` — confirm AssertionError."
- "Write failing test for AC-2 boundary case in repo/tests/<area>/test_<feature>.py::test_<boundary>. Cover: empty input returns <expected error>. Run `pytest tests/<area>/test_<feature>.py::test_<boundary> -x -v` — confirm AssertionError."
- "Update traceability matrix: AC-1 and AC-2 status → `red`. Verify all in-scope AC now have an entry."

### 3. Green Phase (minimum implementation)

Purpose: Make the failing tests pass with the minimum implementation. No speculative features.

Example todos:
- "Make AC-1 test pass: in repo/src/<file>:<function>, [minimum change]. Reference pattern: repo/src/<neighbor>. Forbidden: editing tests/. Run `pytest tests/<file>::<test> -x` — confirm red → green; `pytest tests/ -x` — full suite stays green."
- "Make AC-2 test pass: in repo/src/<file>:<function>, [minimum change]. Run `pytest tests/<file>::<test> -x`."
- "Update traceability matrix: AC-1 and AC-2 status → `green`. Run full project suite from spec.yaml done_when[0]."

### 4. Refactor Phase (optional — only when green and structure needs work)

Purpose: Improve code quality while keeping all tests green.

Example todos:
- "Run full test suite (`pytest tests/ -x -v`) and record baseline — must be all green before refactoring. Save baseline output to archive/phase_N_baseline.txt."
- "Extract <logic> from repo/src/<file> into repo/src/<new_file>. Update imports in callers. Run `pytest tests/ -x` — every previously-green test stays green."
- "Verify via git_diff: only structural changes, no behavior changes, no edits under tests/."

### 5. Integration Phase (PR / commit)

Purpose: Package the work for review.

Example todos:
- "Review all changes via `git_diff` against the job's base tag. Confirm scope matches spec.yaml feature and respects not_included."
- "Stage and commit with message `feat: <feature> (AC-1, AC-2, ...)`. Push to origin <branch>."
- "Verify via `git_log` and `git_status`: commit landed, branch clean."

### 6. Bug Fix Variant (red → green within one feature)

A bug fix is just a TDD cycle with the regression test as the first AC:
- Spec phase: Add `AC-N: When <bug-trigger>, the system shall <correct-behavior>`. Test oracle: a new regression test.
- Red phase: Write the regression test that reproduces the bug — it must fail with the bug present.
- Green phase: Fix the bug. Confirm the regression test turns green; existing tests stay green.

---

## Verification Discipline

Every change must be independently verified before `todo_complete`.

**Verification checklist per todo:**

For RED phase:
1. `git_diff` — only files under `tests/` changed? Anything in `src/`? STOP.
2. Pytest output — does the new test ID appear in the failure list?
3. Failure type — is it `AssertionError`/assertion mismatch? Or is it `ImportError`/`CollectionError`/`SyntaxError`?
4. Forbidden test pattern check — search the diff for `assert True`, `pytest.skip`, `xfail`, empty bodies, tautologies, etc.
5. Traceability — is the AC ID updated in `spec_lock.md`?

For GREEN phase:
1. `git_diff` — only files under `src/` (or config/migration paths) changed? Anything under `tests/`? STOP.
2. Pytest output — did the target test go from red to green?
3. Full suite — did `done_when` commands pass? Any new failures elsewhere?
4. Traceability — AC ID updated to `green`?

**Evidence-based completion.** Todo notes should include concrete evidence:
- Bad: "Implemented the feature, tests pass"
- Good red: "Added tests/test_persistent_ttl.py::test_extends (35 lines). pytest output: `FAILED tests/test_persistent_ttl.py::test_extends - AssertionError: 0 != 600`. AC-1 → red."
- Good green: "Modified repo/src/persistent/lifecycle.py:handle_event (+8 lines). pytest output: `tests/test_persistent_ttl.py::test_extends PASSED`. Full suite `pytest tests/ -x`: 142 passed, 0 failed. AC-1 → green."

---

## Anti-Patterns

**Phase-type violations:**
- Mixing red and green todos in one phase ("write the test and then implement") — collapse the cycle, lose the discipline
- Editing `tests/` in a green phase ("the test was wrong, I'll just fix it") — STOP, end the phase, surface in strategic
- Editing `src/` in a red phase ("just enough to make the import work") — the test must fail honestly; if the import is the problem, the test path is wrong

**Test dishonesty:**
- `assert True`, empty test bodies, `pytest.skip`/`xfail` to "make it pass"
- Tests that mirror the implementation (`assert f(x) == f(x)`)
- Mocking the unit under test
- Catching `Exception` and asserting nothing

**Spec drift:**
- Rewriting an AC to match what was built — moves the goalposts
- Adding "implicit" AC mid-stream without recording — scope creep
- Vague AC ("the system shall be correct") — not testable

**Other:**
- Blind overwrites — never `write_file` without `read_file` first
- Trusting exit code 0 — "0 tests collected" exits 0; read the test count
- Giant PRs — > 10 files or > 500 lines is too big; split
- Silent abandonment — emit `BLOCKED:` and record, never just "move on"

---

## Quick Reference

| `tdd_phase` | Typical todos | When to use |
|---|---|---|
| `spec` | 3-5 | First phase of any new feature; or when AC are revealed to be wrong |
| `red` | 5-8 | Before any implementation; one test per behavior, one todo per test |
| `green` | 5-8 | After red is verified; one todo per failing test |
| `refactor` | 3-5 | After green; only when structure genuinely needs improvement |
| `integration` | 3-5 | Final phase — PR/commit prep |

**Default per phase: 7 todos.** Spec/integration phases are smaller (3-5). Red/green phases scale with AC count. If a phase needs > 12 todos, split the spec or split the phase.
