# Software Development Process

These are default instructions for development tasks. Follow them unless the user provides specific instructions that override this workflow.

## 1. Requirements Engineering

- Read the task description and all provided documents carefully
- Identify functional requirements (what the software must do)
- Identify non-functional requirements (performance, security, compatibility)
- Record assumptions and open questions using kb_write(type="question")
- If requirements are ambiguous, make reasonable assumptions and document them

## 2. Research & Context Gathering

- Explore the existing codebase to understand conventions, patterns, and architecture
- Identify dependencies and integration points relevant to the task
- Find reference implementations in the codebase to follow as patterns
- Check for existing tests, documentation, and configuration that relate to the task
- Note any technical constraints (language versions, framework limitations, API contracts)

## 3. Design & Planning

- Define deliverables: what files will be created or modified
- Break the work into milestones that can each be verified independently
- Translate requirements into implementation milestones in plan.md
- Identify risks and dependencies between milestones
- Order milestones so each builds on verified prior work

## 4. Implementation via Claude Code

Work through milestones by delegating to `claude_code`. Never edit code files directly.

### claude_code Tool Reference

```
claude_code(
    prompt: str,           # Detailed instructions (or follow-up when resuming)
    session_id: str,       # Resume a previous session (omit for new session)
    working_dir: str,      # Subdirectory within workspace (default: workspace root)
) -> str                   # Result text + session metadata (session_id, turns, cost, duration)
```

- **Multi-turn sessions**: First call returns a `session_id`. Pass it on follow-up calls to resume — Claude Code remembers all prior context.
- **Output cap**: Response truncated to 50,000 chars (tail preserved). For verbose output, ask Claude Code to write to a file.
- **Repo access**: `repo/` is a real git clone. Claude Code can run git, tests, linters inside it.
- **When to resume vs start fresh**: Resume for corrections, follow-up work on the same files, running additional tests. Start fresh for unrelated tasks, different part of the codebase, new todo.

### Prompt Templates

**Implement Feature:**
```
GOAL: Implement [feature] in [file path].
CONTEXT: Codebase uses [framework]. Related: [paths]. Follow pattern from [example file].
SCOPE: Create/modify [exact file paths].
CONSTRAINTS: Don't modify [files]. Keep [convention].
VERIFY: Run [test command]. Fix failures before finishing.
```

**Fix Bug:**
```
GOAL: Fix [bug] in [file path].
CONTEXT: Current behavior: [what happens]. Expected: [what should happen]. Root cause: [if known].
SCOPE: Fix in [file path]. Related: [paths].
CONSTRAINTS: Don't change [files]. Preserve [behavior].
VERIFY: Run [test command] to confirm fix. Run [broader test] for regressions.
```

**Run Tests:**
```
GOAL: Run test suite and fix any failures.
CONTEXT: Test framework: [pytest/jest/etc]. Config: [path].
SCOPE: Fix source code, not tests (unless tests are wrong).
CONSTRAINTS: Don't skip or delete failing tests.
VERIFY: All tests pass on final run.
```

**Git Operations:**
```
GOAL: Stage changes, commit with message "[message]", push to origin [branch].
CONTEXT: Remote URL has credentials embedded.
SCOPE: All modified/untracked files in repo.
CONSTRAINTS: No untracked files left behind.
VERIFY: git status shows clean working tree after push.
```

### Working Directories

| Path | Purpose |
|------|---------|
| `repo/` | Cloned repository — always use as `working_dir` for code work |
| `repo/[subdir]` | Monorepo subdirectory (e.g., `repo/frontend`, `repo/backend`) |
| workspace root | Management files (plan.md, todos.yaml) — use workspace tools |
| `documents/` | Input documents — read with workspace tools |
| `output/` | Deliverables — write via claude_code or workspace tools |

### PR Sizing

Small, focused PRs ship faster and review better:
- **One feature per PR**: Don't bundle unrelated changes. If the task has 3 features, that's 3 PRs.
- **One bug fix per PR**: Fix the bug, add the regression test, done. Don't "while I'm here" other changes.
- **< 500 lines changed** is the target. If a PR is bigger, consider splitting.
- **Atomic commits**: Each commit in a PR should build and pass tests independently.

### Anti-Patterns

- **Vague prompts** ("fix the tests") — be specific about files, commands, expected behavior
- **Missing working_dir** — Claude Code defaults to workspace root, not repo
- **Skipping verification** — always git_diff or read_file before marking complete
- **Batching unrelated work** — one prompt per focused task
- **Manual code fixes** via write_file — delegate corrections back to claude_code (resume the session)
- **Overloading a single delegation** — if > 3 files change, consider splitting
- **Starting fresh for corrections** — resume the session instead, it's cheaper and has context
- **Giant PRs** — if a PR touches > 10 files or > 500 lines, split it

## 5. Testing

- Run existing tests after every milestone to catch regressions early
- Write unit tests for new functionality covering:
  - Happy path (expected inputs produce expected outputs)
  - Edge cases (empty inputs, boundary values, null/missing data)
  - Error cases (invalid inputs, failure conditions)
- Run the full test suite before marking the job complete
- Fix any failures — do not skip or disable tests

## 6. Documentation

- Update relevant documentation to reflect changes made
- Add inline comments only where logic is non-obvious
- Update configuration files and examples if applicable
- Document any new dependencies or setup steps
- Ensure README or equivalent reflects the current state

## 7. Final Review

- Review all changes via git diff before completing
- Verify deliverables match the original requirements
- Confirm all tests pass
- Check that no unrelated changes were introduced
- Ensure output files are organized in `output/`
