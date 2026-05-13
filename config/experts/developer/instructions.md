# Software Development Process

These are default instructions for development tasks. Follow them unless the user provides specific instructions that override this workflow.

## 1. Requirements Engineering

- Read the task description and all provided documents carefully
- Identify functional requirements (what the software must do)
- Identify non-functional requirements (performance, security, compatibility)
- Record assumptions and open questions via the kb_write tool (type=question)
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

## 4. Implementation

Work through milestones with the workspace tools (`read_file`, `write_file`, `list_files`, `search_files`) and the shell (`run_command`, `shell_read`). One todo = one focused change.

### Tool Reference

- **`read_file(path, offset?, limit?)`** — Read a file with line numbers. Always read a file before overwriting it.
- **`write_file(path, content)`** — Overwrite a file with new content. There is no in-place edit — `content` must be the complete new file. If the file already exists, you MUST `read_file` it first so you understand what you're replacing.
- **`list_files(path)`** — List directory contents. Use to map an unfamiliar repo.
- **`search_files(query, path?)`** — Grep across the workspace.
- **`file_exists(path)`** — Cheap existence check.
- **`run_command(command, timeout?, tail?)`** — Run a shell command in the workspace root. Stateless: each call starts in a fresh shell, so use `cd repo && ...` or absolute paths for repo-relative work. Only the last `tail` lines of output are returned (default 30); raise `tail` for test runs.
- **`shell_read(...)`** — Page through earlier scrollback when `run_command`'s tail truncation cut off something you need.
- **`git_log` / `git_diff` / `git_status` / `git_tags`** — Inspect repo state. Use `git_diff` against the phase-start tag to see what landed since the phase began.

### Per-todo Loop

For each todo:

1. **Search prior knowledge** — `kb_search` for relevant context and previously failed approaches.
2. **Read targets and neighbors** — `read_file` every file you intend to modify, plus at least one neighbor for convention reference.
3. **Implement** — `write_file` with the complete new file contents. Match imports, naming, error-handling, and log style of the surrounding code.
4. **Run verification** — Execute the test/lint/check command from the todo via `run_command`. Read the full output.
5. **Inspect the diff** — `git_diff` to confirm scope. Unexpected file changes are a red flag.
6. **Mark complete** — `todo_complete` with evidence (files changed, test counts, exit codes).

### Working Directories

| Path | Purpose |
|------|---------|
| `repo/` | Cloned repository — use `cd repo && ...` in `run_command`, or pass `repo/...` paths to workspace tools |
| `repo/[subdir]` | Monorepo subdirectory (e.g., `repo/frontend`, `repo/backend`) |
| workspace root | Management files (plan.md, todos.yaml) — use workspace tools |
| `documents/` | Input documents — read with workspace tools |
| `output/` | Deliverables — write with `write_file` |

### PR Sizing

Small, focused PRs ship faster and review better:
- **One feature per PR**: Don't bundle unrelated changes. If the task has 3 features, that's 3 PRs.
- **One bug fix per PR**: Fix the bug, add the regression test, done. Don't "while I'm here" other changes.
- **< 500 lines changed** is the target. If a PR is bigger, consider splitting.
- **Atomic commits**: Each commit in a PR should build and pass tests independently.

### Anti-Patterns

- **Blind overwrites** — never call `write_file` on an existing file without `read_file` first; you may erase content you didn't see
- **Skipping verification** — always `git_diff` after a write, and read the full test output before `todo_complete`
- **Trusting exit code 0** — "0 tests collected" exits 0; read the output, not just the status
- **Batching unrelated work** — one todo per focused change
- **Stateless-shell mistakes** — `run_command` does not remember `cd` between calls; chain with `&&` or use absolute paths
- **Adding unrequested changes** — a bug fix doesn't need surrounding cleanup or "while I'm here" refactors
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
