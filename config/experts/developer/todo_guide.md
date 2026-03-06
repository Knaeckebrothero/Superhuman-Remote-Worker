# Todo Crafting Guide — Developer

**You MUST read this file before calling `next_phase_todos`.** The tool will reject
your call if you haven't. This guide teaches you how to create effective, delegation-ready todos.

---

## Core Principle: One Todo = One Delegation

**Target: 5-10 todos per tactical phase.** Adapt based on task complexity:
- **Simple, well-defined tasks**: 5-7 todos (straightforward implementations)
- **Standard tasks**: 7-10 todos (feature implementation with tests)
- **Complex, multi-step tasks**: 10-15 todos (split into more phases if you need more)

Each tactical phase ends with a strategic review. More frequent reviews mean:
- Earlier detection of wrong directions
- Better-adapted delegation prompts based on what Claude Code actually produced
- Less wasted work if the approach needs to change

A phase should represent one coherent unit of work — "explore the codebase," "implement
the auth module," "add tests for the API endpoints" — not an entire feature.

---

## Todo Specificity Rules

Every todo must be specific enough to convert directly into a `claude_code` prompt.

### Vague → Delegation-Ready Examples

| Vague (fails) | Delegation-ready (works) |
|---|---|
| "Fix the tests" | "Fix failing test in repo/tests/test_auth.py::test_login_invalid_token — handle empty session token by returning 401. Follow guard pattern from repo/src/auth/refresh.py:30. Run pytest tests/test_auth.py" |
| "Add the API endpoint" | "Add GET /api/users/{id} endpoint in repo/src/routes/users.py. Return UserResponse schema from repo/src/schemas/user.py. Follow pattern from repo/src/routes/items.py:get_item. Run pytest tests/test_routes_users.py" |
| "Refactor the module" | "Extract database connection logic from repo/src/services/user_service.py into repo/src/db/connection.py. Keep the same interface. Run pytest tests/test_user_service.py to confirm no regressions" |
| "Write unit tests" | "Add tests for UserService.create_user() in repo/tests/test_user_service.py. Cover: valid input, duplicate email, missing fields. Follow test style from repo/tests/test_auth_service.py. Run pytest tests/test_user_service.py -v" |
| "Update the frontend" | "Add user profile page component in repo/frontend/src/components/UserProfile.tsx. Display name, email, avatar. Follow component pattern from repo/frontend/src/components/ItemDetail.tsx. Run npm test -- --testPathPattern=UserProfile" |
| "Set up CI" | "Create repo/.github/workflows/ci.yml with: checkout, setup Python 3.11, install requirements.txt, run pytest tests/ -v. Follow structure from existing repo/.github/workflows/lint.yml" |

### What Makes a Delegation-Ready Todo

1. **Names target files** — exact paths to read/modify (e.g., `repo/src/auth/login.py`)
2. **Names the specific change** — what to add, fix, or modify
3. **Names a reference pattern** — an existing file that shows the convention to follow
4. **Names the verification command** — the test or check to run after implementation
5. **Completable in one delegation** — if it needs more than one `claude_code` call, split it

### The Delegation Test

Before finalizing each todo, ask: "Could I convert this directly into a GOAL/CONTEXT/SCOPE/CONSTRAINTS/VERIFY prompt?"
- "Implement the feature" → What feature? Which files? What convention? Too vague.
- "Add password validation to repo/src/auth/validators.py — min 8 chars, 1 uppercase, 1 digit. Follow pattern from email_validator in same file. Run pytest tests/test_validators.py" → Clear delegation prompt.

---

## Phase Design Patterns

### 1. Codebase Exploration Phase (first phase for any new repo)

Purpose: Understand the repository before delegating any code changes.

Example todos:
- "Read repo/README.md and repo/package.json (or requirements.txt) to understand project structure and dependencies"
- "Use list_files on repo/src/ to map the directory structure. Record key paths in workspace.md"
- "Read repo/src/routes/ (or equivalent entry points) to understand the API surface"
- "Read repo/tests/ to understand the test framework, conventions, and coverage"
- "Read repo/.github/workflows/ (or CI config) to understand the build/test pipeline"
- "Update workspace.md with: framework, conventions, test command, key entry points, branch strategy"

### 2. Implementation Phase (core delegation work)

Purpose: Delegate focused code changes to Claude Code, one per todo.

Example todos:
- "Delegate: Add UserService class in repo/src/services/user_service.py with create, read, update, delete methods. Follow pattern from repo/src/services/item_service.py. Run pytest tests/test_user_service.py"
- "Delegate: Add user routes in repo/src/routes/users.py — CRUD endpoints using UserService. Follow repo/src/routes/items.py pattern. Run pytest tests/test_routes_users.py"
- "Verify via git_diff: confirm only expected files changed, no unrelated modifications"
- "Delegate: Add migration script in repo/migrations/003_add_users_table.py. Follow pattern from repo/migrations/002_add_items_table.py. Run migration and verify schema"

### 3. Testing Phase (verify and harden)

Purpose: Add tests, fix failures, ensure coverage.

Example todos:
- "Delegate: Add unit tests for UserService in repo/tests/test_user_service.py. Cover: create valid user, duplicate email error, missing required fields, update nonexistent user. Run pytest tests/test_user_service.py -v"
- "Delegate: Add integration tests for user API routes in repo/tests/test_routes_users.py. Cover: CRUD operations, auth required, invalid input. Run pytest tests/test_routes_users.py -v"
- "Delegate: Run full test suite (pytest tests/ -v), fix any regressions introduced by the user feature"
- "Verify via git_diff: confirm test files are substantive (not empty stubs or skipped tests)"

### 4. PR/Commit Phase (package and ship)

Purpose: Create clean, focused commits and PRs.

Example todos:
- "Review all changes via git_diff(revision='phase_N_start') — confirm scope matches the feature"
- "Delegate: Clean up any debug prints, commented-out code, or TODO markers. Run linter. Run full test suite"
- "Delegate: Stage all changes, commit with message 'feat: add user CRUD endpoints with tests', push to origin feature/users"
- "Verify via git_log: confirm commit is clean and push succeeded"

### 5. Bug Fix Phase (diagnose and fix)

Purpose: Investigate a specific bug, implement the fix, add regression tests.

Example todos:
- "Read the bug report/error log. Identify the affected file and function"
- "Read repo/src/affected_file.py to understand current behavior"
- "Delegate: Fix [specific bug] in repo/src/affected_file.py:line. Root cause: [explanation]. Add guard clause for [condition]. Run pytest tests/test_affected.py"
- "Delegate: Add regression test in repo/tests/test_affected.py::test_bug_description that reproduces the original bug and confirms the fix. Run pytest tests/test_affected.py -v"
- "Verify via git_diff: confirm fix is minimal and targeted, no unrelated changes"

### 6. Refactoring Phase (restructure without changing behavior)

Purpose: Improve code structure while preserving all existing behavior.

Example todos:
- "Run full test suite (pytest tests/ -v) and record baseline results — all tests must pass before refactoring"
- "Delegate: Extract [logic] from repo/src/module.py into repo/src/new_module.py. Update imports in all consumers. Run pytest tests/ -v"
- "Verify via git_diff: confirm only structural changes, no behavior changes"
- "Run full test suite again — same tests must pass with same results"

---

## Session Management Guidance

- **New session** for each independent todo — gives Claude Code a clean context
- **Resume session** (pass `session_id`) when:
  - A delegation needs corrections after verification
  - You need to run additional tests on the same changes
  - Follow-up work touches the same files
- **Always save `session_id`** in todo completion notes for potential follow-ups

---

## Verification Discipline

Every delegation must be independently verified. Do not trust Claude Code's self-reported output.

**Verification checklist per todo:**
1. `git_diff` — Are the changes what you expected? No unrelated files?
2. `read_file` — Spot-check key files for correctness
3. Test output — Did the specified tests actually pass? Read the output for errors even if exit code is 0.
4. Scope check — Did the delegation stay within the specified files?

**Evidence-based completion:** Todo notes should include concrete evidence:
- Bad: "Implemented the feature, tests pass"
- Good: "Added UserService in repo/src/services/user_service.py (4 methods). pytest tests/test_user_service.py: 8 passed, 0 failed. git_diff shows 2 files changed: user_service.py (+95), test_user_service.py (+120)"

---

## Quick Reference

| Phase type | Typical todos | When to use |
|---|---|---|
| Codebase Exploration | 5-7 | Starting work on a new or unfamiliar repo |
| Implementation | 5-10 | Building new features or modules |
| Testing | 5-7 | Adding/improving test coverage |
| PR/Commit | 3-5 | Packaging work for review |
| Bug Fix | 4-6 | Investigating and fixing specific bugs |
| Refactoring | 5-7 | Restructuring code without behavior changes |

**Default to 7 todos.** Go lower (5) for focused phases like bug fixes or PR cleanup.
Go higher (10) for implementation phases with multiple files. If you need more than 15,
split into two phases.
