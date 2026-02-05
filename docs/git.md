# Git Versioning for Agent Workspaces

## Problem Statement

The strategic phase struggles to understand what was done over time. This manifests in several ways:

1. **Phase transitions**: When transitioning from tactical to strategic, the agent needs to review what was accomplished. Currently it relies on archived todo files with completion notes, but these only capture what the agent chose to write, not the actual changes.

2. **Long-running jobs**: Even with the best models and largest context windows, an agent will eventually face situations where it needs to know what it did 20 phases ago - whether for documentation, debugging, or understanding the context behind changes.

3. **Debugging**: Users debugging agent behavior have to piece together what happened from scattered artifacts (workspace.md, archive files, logs). There's no single source of truth for "what changed and when."

## Solution: Git-Versioned Workspaces

Each agent workspace becomes a local git repository. The agent commits its work as it completes todos, creating an automatic audit trail of all changes.

### Benefits

| Benefit | Description |
|---------|-------------|
| **Ground truth** | Git captures actual file changes, not just what the agent remembers to note |
| **Queryable history** | Agent can use git commands to explore its own past (log, diff, show) |
| **Rollback capability** | Mistakes can be reverted with `git reset` or `git revert` |
| **Familiar tooling** | Developers can debug with standard git commands |
| **Forward-looking** | When agents work on actual codebases, git infrastructure is already in place |

### Why Not Just Improve workspace.md?

`workspace.md` is the current long-term memory mechanism, but it has limitations:

- The agent must remember to update it
- It's unstructured prose - hard to query programmatically
- It doesn't capture the actual changes, just summaries
- It can become stale or inconsistent with reality

Git captures changes automatically. Every file modification is recorded whether the agent "remembers" or not.

## Implementation Plan

### 1. Git Tools Package

Create `src/tools/git/` with read-only git inspection tools.

#### Initial Tools (v1)

| Tool | Purpose | Phases |
|------|---------|--------|
| `git_log` | View commit history with filtering | Both |
| `git_show` | Inspect a specific commit's changes | Both |
| `git_diff` | Compare current state to previous commits | Both |
| `git_status` | See uncommitted changes | Both |

#### Deferred Tools (v2+)

| Tool | Purpose | Why Deferred |
|------|---------|--------------|
| `git_blame` | See when/why specific lines were added | Less critical for initial use case |
| `git_reset` | Rollback to a previous state | Destructive - needs TodoManager sync design |
| `git_revert` | Undo a specific commit | Needs careful design around state consistency |

#### Tool Specifications

**git_log**
```
git_log(max_count: int = 10, oneline: bool = True) -> str
```
- Default shows last 10 commits in oneline format
- Use `max_count` to control how far back to look
- Useful for strategic review: "What did I do in the last tactical phase?"

**git_show**
```
git_show(commit_ref: str = "HEAD", stat_only: bool = False) -> str
```
- Shows full commit details and diff
- `stat_only=True` for just file list without full diff (context-friendly)

**git_diff**
```
git_diff(ref1: str = None, ref2: str = None, file_path: str = None) -> str
```
- No args: show uncommitted changes (working directory vs HEAD)
- One ref: compare that ref to working directory
- Two refs: compare between refs
- `file_path`: limit to specific file

**git_status**
```
git_status() -> str
```
- Shows current branch, uncommitted changes, untracked files
- Useful before committing to verify state

### 2. Workspace Git Initialization

Modify `WorkspaceManager.initialize()` to optionally create a git repository.

#### Initialization Flow

```
1. Create workspace directory structure (existing)
2. Copy template files (existing)
3. IF git_versioning enabled:
   a. Run `git init`
   b. Create .gitignore from configured patterns
   c. Configure local git user (agent@workspace.local)
   d. Stage all initial files
   e. Create initial commit: "Initialize workspace"
4. Continue with normal initialization
```

#### Configuration

```yaml
workspace:
  structure:
    - archive/
    - tools/
  instructions_template: instructions.md
  initial_files:
    workspace.md: prompts/workspace_template.md

  # Git versioning (new)
  git_versioning: true  # Enable git repository per workspace
  git_ignore_patterns:
    - "*.db"
    - "*.log"
    - "__pycache__/"
    - ".DS_Store"
```

**Default: Enabled** - Git provides value with minimal overhead. Agents that don't need it simply won't use the git tools.

#### Graceful Degradation

If git is not installed:
1. Log warning: "Git not available, workspace versioning disabled"
2. Set `git_versioning_active = False` on workspace
3. Git tools return helpful error: "Git versioning not available for this workspace"
4. Everything else works normally

### 3. GitManager Class

Create a shared `GitManager` class that encapsulates all git operations. This allows consistent git access across different modules (workspace initialization, todo completion, git tools).

#### Location: `src/managers/git_manager.py`

```python
class GitManager:
    """Manages git operations for a workspace directory."""

    def __init__(self, workspace_path: Path):
        self._workspace_path = workspace_path
        self._git_available = self._check_git_available()

    @property
    def is_active(self) -> bool:
        """Check if git versioning is active for this workspace."""
        return self._git_available and (self._workspace_path / ".git").exists()

    def init_repository(self, ignore_patterns: List[str]) -> bool:
        """Initialize git repository with .gitignore."""
        ...

    def commit(self, message: str, allow_empty: bool = True) -> bool:
        """Stage all changes and commit."""
        ...

    def log(self, max_count: int = 10, oneline: bool = True) -> str:
        """Get commit history."""
        ...

    def show(self, commit_ref: str = "HEAD", stat_only: bool = False) -> str:
        """Show commit details."""
        ...

    def diff(self, ref1: str = None, ref2: str = None, file_path: str = None) -> str:
        """Show differences."""
        ...

    def status(self) -> str:
        """Get current status."""
        ...

    def _run_git(self, args: List[str]) -> subprocess.CompletedProcess:
        """Execute git command in workspace directory."""
        ...
```

#### Usage Across Modules

- **WorkspaceManager**: Uses `GitManager.init_repository()` during workspace creation
- **TodoManager**: Uses `GitManager.commit()` on todo completion
- **Git Tools**: Use `GitManager.log/show/diff/status()` for agent queries

### 4. Auto-Commit on Todo Completion

Each `todo_complete` call triggers a git commit, reinforcing that each todo is an atomic unit of work.

#### Integration Point: TodoManager.complete()

```python
def complete(self, todo_id: str, notes: Optional[List[str]] = None) -> Optional[TodoItem]:
    """Mark a todo as completed and commit changes."""
    # ... existing completion logic ...

    # Auto-commit if git versioning is active
    if self._git_manager and self._git_manager.is_active:
        self._commit_todo_completion(todo)

    return todo

def _commit_todo_completion(self, todo: TodoItem) -> None:
    """Commit workspace changes for completed todo."""
    message = self._build_commit_message(todo)
    success = self._git_manager.commit(message, allow_empty=True)
    if not success:
        logger.warning(f"Git commit failed for {todo.id}")
    # Don't fail the todo completion - just log and continue
```

**Note on `allow_empty=True`**: Empty commits are allowed intentionally. A todo might involve read-only analysis that doesn't change files. Empty commits won't bloat diffs and maintain the audit trail. Can be revisited if they cause noise.

#### Commit Message Format

```
[Phase 3] todo_4: Extract requirements from chapter 2

Completed: 2026-02-01T14:23:00Z
Phase: tactical
Notes: Found 12 requirements, 3 marked as ambiguous
```

Structured format enables programmatic parsing for debugging and analysis.

### 5. Agent Instructions: Todo-as-Feature Model

The key behavioral change: reframe todos as "features" that get committed, not just "tasks" to check off.

#### Current Mental Model (task-oriented)

> "Work through todos in order, completing each before moving on"

#### New Mental Model (feature-oriented)

> "Each todo is a discrete unit of work. Complete it fully, then commit your changes before moving to the next."

This reinforces:
- **Atomicity**: Each todo should be self-contained
- **Completeness**: Finish the work before marking done
- **Traceability**: Changes are recorded automatically

#### Updated Tactical Prompt

```
You are currently executing your plan.
Your current focus lies on systematically working through your todolist.
Using your domain tools to implement this part of your plan.

Each todo represents a discrete unit of work - like a feature or task that gets committed to your workspace history. Complete each todo fully before moving on.

You are the executor! Your focus lies on putting your plan.md into action by:
- Using domain tools to accomplish your current todos
- Recording progress and results in workspace files
- Completing each todo as an atomic unit of work
- Staying focused - save planning thoughts for workspace.md notes

Your responsibility in this tactical phase is:
- Work through todos in order, completing each fully before moving on
- Use domain tools for the actual work (document extraction, search, database, citations)
- Save outputs to workspace files for future reference
- Mark todos complete using `todo_complete` (this commits your changes)

Remember: Each `todo_complete` creates a checkpoint of your work. Make sure the todo is truly done before marking it complete.

When stuck:
- Document the issue in workspace.md
- Move to the next todo if possible
- Handle errors gracefully and continue
- Note ideas for the strategic phase to consider
```

#### Updated Strategic Prompt

```
You are currently revising your strategy.
Your current focus lies on planning your approach and reviewing progress.
As of now you don't want to do domain-specific work.

Your focus lies exclusively on things such as:
- **Reviewing what was accomplished** using git tools (git_log, git_diff) to see actual changes
- Exploring and understanding the current situation by searching the workspace
- Reflecting and learning by maintaining workspace.md with insights from completed phases
- Planning and reorganizing your approach by refining plan.md
- Preparing for the next execution phase with clear, actionable todos

Your responsibilities in this strategic phase:
- Review the previous tactical phase: use `git_log` and `git_diff` to see what actually changed
- Update workspace.md with learnings - but verify against actual changes, not just memory
- Maintain the execution plan.md
- Break down work into phases with 5-20 todos each
- Create todos for the upcoming execution phase using `next_phase_todos`
- Call `job_complete` only when the entire plan is fully executed

IMPORTANT: Don't rely solely on your memory of what was done. Use git tools to verify:
- `git_log --oneline -10` to see recent commits
- `git_diff HEAD~5` to see changes from last 5 todos
- This is your ground truth for what actually happened

Remember right now it's all about strategy! Keep in mind that you're here to reason not to execute:
- Do NOT execute domain work (document processing, database operations, etc.)
- Do NOT call job_complete until ALL phases in plan.md are marked complete
- Call `todo_complete` after finishing each of your strategic todos

For the final strategic todo: call `next_phase_todos` first, THEN `todo_complete`
```

#### Agent-Driven Review

The agent decides when and how to use git tools. No automatic injection of git summaries during phase transitions - this keeps the system simple and gives the agent autonomy. The strategic prompt instructs the agent to use git tools for review, but the agent chooses which tools and when.

## Design Decisions

### Resolved Questions

| Question | Decision | Rationale |
|----------|----------|-----------|
| Commit granularity | Per-todo | Fine-grained history, atomic units, easier debugging |
| Strategic phase commits | Yes | workspace.md and plan.md changes should be tracked too |
| Default enabled | Yes | Low overhead, high value; unused tools don't hurt |
| Commit failure handling | Log warning, continue | Don't block agent progress for git issues |

### Tool Phase Availability

All git tools are read-only and available in both phases:

| Tool | Strategic | Tactical | Rationale |
|------|-----------|----------|-----------|
| `git_log` | Yes | Yes | Review history anytime |
| `git_show` | Yes | Yes | Inspect commits anytime |
| `git_diff` | Yes | Yes | Compare states anytime |
| `git_status` | Yes | Yes | Check current state anytime |

Destructive tools (reset, revert) are deferred until we design TodoManager synchronization.

## Future Extensions

### Branch-per-Phase Model

Each tactical phase could be a branch:
```
main
├── phase-1
├── phase-2
└── phase-3 (current)
```

Strategic phases merge completed work back to main. This provides:
- Isolation between phases
- Clean merge points for review
- Ability to abandon a phase without affecting main

### Rollback with State Sync

When implementing `git_reset`:
1. Reset git to target commit
2. Parse commit message to find phase/todo
3. Restore TodoManager state to match
4. Handle archived todos appropriately

This requires commit messages to be machine-parseable (already designed for this).

### Code Repository Integration

When agents work on actual codebases (not just workspaces):
- Clone target repo into workspace
- Agent commits as it works
- Changes can be reviewed before pushing
- Same git tools work on any repo

### Multi-Agent Collaboration

If multiple agents work on the same workspace:
- Each agent could have its own branch
- Merge conflicts handled at strategic phase boundaries
- Git history shows which agent made which changes

## Implementation Checklist

### Phase 1: GitManager Class
- [ ] Create `src/managers/git_manager.py`
- [ ] Implement `GitManager` with init, commit, log, show, diff, status methods
- [ ] Add graceful degradation when git unavailable
- [ ] Write tests for GitManager

### Phase 2: Git Tools Package
- [ ] Create `src/tools/git/__init__.py`
- [ ] Create `src/tools/git/git_tools.py` with git_log, git_show, git_diff, git_status
- [ ] Tools should use GitManager (injected via ToolContext)
- [ ] Add GIT_TOOLS_METADATA with proper phase configuration
- [ ] Register in `src/tools/registry.py`
- [ ] Add `git` category to `config/defaults.yaml`
- [ ] Write tests for git tools

### Phase 3: Workspace Initialization
- [ ] Add `git_versioning` and `git_ignore_patterns` to workspace config schema
- [ ] Update `WorkspaceManager.initialize()` to create GitManager and call `init_repository()`
- [ ] Store GitManager reference on WorkspaceManager
- [ ] Update `config/defaults.yaml` with git config
- [ ] Write tests for git initialization

### Phase 4: Auto-Commit Integration
- [ ] Pass GitManager to TodoManager (via ToolContext or constructor)
- [ ] Add `_commit_todo_completion()` to TodoManager
- [ ] Add `_build_commit_message()` helper
- [ ] Hook into `TodoManager.complete()`
- [ ] Write tests for auto-commit

### Phase 5: Agent Instructions
- [ ] Update `config/prompts/tactical.txt` with feature-oriented language
- [ ] Update `config/prompts/strategic.txt` with git review instructions
- [ ] Test agent behavior with new prompts
