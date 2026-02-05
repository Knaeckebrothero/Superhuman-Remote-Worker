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
| **Phase tracking** | Solves the phase numbering issues in `docs/phase_number_issues.md` - git tags and `phase_state.yaml` survive context compaction |

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
| `git_tags` | List phase milestone tags | Both |

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
git_show(commit_ref: str = "HEAD", stat_only: bool = False, max_lines: int = 500) -> str
```
- Shows full commit details and diff
- `stat_only=True` for just file list without full diff (context-friendly)
- `max_lines` limits output to prevent context bloat (default: 500)

**git_diff**
```
git_diff(ref1: str = None, ref2: str = None, file_path: str = None, max_lines: int = 500) -> str
```
- No args: show uncommitted changes (working directory vs HEAD)
- One ref: compare that ref to working directory
- Two refs: compare between refs
- `file_path`: limit to specific file
- `max_lines` limits output to prevent context bloat (default: 500)

**git_status**
```
git_status() -> str
```
- Shows current branch, uncommitted changes, untracked files
- Provides clear indication of workspace state:
  - `clean`: No uncommitted changes
  - `dirty`: Modified/staged files exist (lists them)
  - `untracked`: New files not yet tracked
- Useful before committing to verify state

**git_tags**
```
git_tags(pattern: str = "phase-*") -> str
```
- Lists git tags, filtered by pattern (default: phase tags)
- Returns tags in chronological order
- Useful for understanding phase progression after context compaction
- Example output: `phase-1-strategic-complete, phase-1-tactical-complete, phase-2-strategic-complete`

#### Output Truncation

All git tools that can produce large output implement truncation:
- `max_lines` parameter (default: 500 lines)
- Hard limit: 10,000 words maximum regardless of `max_lines`
- Truncated output includes count of omitted lines/words

### 2. Workspace Git Initialization

Modify `WorkspaceManager.initialize()` to optionally create a git repository.

#### Initialization Flow

```
1. Create workspace directory structure (existing)
2. Copy template files (existing)
3. IF git_versioning enabled:
   a. Check if .git already exists
      - If yes: log "Git repository already exists, skipping initialization" and continue
      - If no: proceed with initialization
   b. Run `git init`
   c. Create .gitignore from configured patterns
   d. Configure local git user (agent@workspace.local)
   e. Stage all initial files
   f. Create initial commit: "Initialize workspace"
4. Continue with normal initialization
```

**Handling existing .git directories**: If the workspace already has a `.git` directory (e.g., resumed job, or workspace created from an existing repository), skip initialization but still enable git tools. This allows the agent to work with pre-existing repositories.

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

    # Subprocess timeout for git commands (seconds)
    DEFAULT_TIMEOUT = 60

    def __init__(self, workspace_path: Path):
        self._workspace_path = workspace_path
        self._git_available = self._check_git_available()

    @property
    def is_active(self) -> bool:
        """Check if git versioning is active for this workspace."""
        return self._git_available and (self._workspace_path / ".git").exists()

    def init_repository(self, ignore_patterns: List[str]) -> bool:
        """Initialize git repository with .gitignore.

        Returns False if .git already exists (no-op).
        """
        if (self._workspace_path / ".git").exists():
            logger.info("Git repository already exists, skipping initialization")
            return True
        ...

    def commit(self, message: str, allow_empty: bool = True) -> bool:
        """Stage all changes and commit."""
        ...

    def log(self, max_count: int = 10, oneline: bool = True) -> str:
        """Get commit history."""
        ...

    def show(self, commit_ref: str = "HEAD", stat_only: bool = False, max_lines: int = 500) -> str:
        """Show commit details with output truncation."""
        ...

    def diff(self, ref1: str = None, ref2: str = None, file_path: str = None, max_lines: int = 500) -> str:
        """Show differences with output truncation."""
        ...

    def status(self) -> str:
        """Get current status with clear dirty/clean indication."""
        ...

    def has_uncommitted_changes(self) -> bool:
        """Check if there are uncommitted changes in the workspace."""
        ...

    def tag(self, tag_name: str, message: str = None) -> bool:
        """Create a git tag at current HEAD."""
        ...

    def list_tags(self, pattern: str = None) -> List[str]:
        """List git tags, optionally filtered by pattern."""
        ...

    def _run_git(self, args: List[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
        """Execute git command in workspace directory with timeout."""
        ...

    def _truncate_output(self, output: str, max_lines: int = 500, max_words: int = 10000) -> str:
        """Truncate output to prevent context bloat."""
        lines = output.splitlines()
        words = output.split()

        truncated = False
        result = output

        if len(lines) > max_lines:
            result = "\n".join(lines[:max_lines])
            truncated = True

        if len(words) > max_words:
            result = " ".join(words[:max_words])
            truncated = True

        if truncated:
            result += f"\n\n... truncated ({len(lines)} lines, {len(words)} words total)"

        return result
```

#### Storage Location

**GitManager is stored on WorkspaceManager**, not ToolContext. This is because:
- TodoManager takes `workspace` in its constructor and needs git access for auto-commits
- WorkspaceManager is the natural owner of workspace-level infrastructure
- ToolContext can access git via `context.workspace_manager.git_manager`

```python
# WorkspaceManager
class WorkspaceManager:
    def __init__(self, ...):
        ...
        self._git_manager: Optional[GitManager] = None

    @property
    def git_manager(self) -> Optional[GitManager]:
        return self._git_manager
```

#### ToolContext Addition

Add convenience method to ToolContext for tools:

```python
# ToolContext
def has_git(self) -> bool:
    """Check if git manager is available and active."""
    if not self.has_workspace():
        return False
    gm = self.workspace_manager.git_manager
    return gm is not None and gm.is_active
```

#### Usage Across Modules

- **WorkspaceManager**: Creates GitManager during `initialize()`, stores as `_git_manager`
- **TodoManager**: Accesses via `self._workspace.git_manager` for auto-commits
- **Git Tools**: Access via `context.workspace_manager.git_manager` for queries

### 4. Auto-Commit on Todo Completion

Each `todo_complete` call triggers a git commit, reinforcing that each todo is an atomic unit of work.

#### Integration Point: TodoManager.complete()

```python
def complete(self, todo_id: str, notes: Optional[List[str]] = None) -> Optional[TodoItem]:
    """Mark a todo as completed and commit changes."""
    # ... existing completion logic ...

    # Auto-commit if git versioning is active (access via workspace)
    git_mgr = self._workspace.git_manager
    if git_mgr and git_mgr.is_active:
        self._commit_todo_completion(todo, git_mgr)

    return todo

def _commit_todo_completion(self, todo: TodoItem, git_mgr: GitManager) -> None:
    """Commit workspace changes for completed todo."""
    message = self._build_commit_message(todo)
    success = git_mgr.commit(message, allow_empty=True)
    if not success:
        logger.warning(f"Git commit failed for {todo.id}")
    # Don't fail the todo completion - just log and continue
```

**Note**: TodoManager accesses GitManager via `self._workspace.git_manager` rather than storing a separate reference. This keeps the dependency chain simple: TodoManager → WorkspaceManager → GitManager.

**Note on `allow_empty=True`**: Empty commits are allowed intentionally. A todo might involve read-only analysis that doesn't change files. Empty commits won't bloat diffs and maintain the audit trail. Can be revisited if they cause noise.

#### Commit Message Format

```
[Phase 3 Tactical] todo_4: Extract requirements from chapter 2

Completed: 2026-02-01T14:23:00Z
Notes: Found 12 requirements, 3 marked as ambiguous
```

```
[Phase 3 Strategic] todo_2: Review phase results

Completed: 2026-02-01T15:10:00Z
Notes: Updated workspace.md with findings
```

Structured format enables programmatic parsing for debugging and analysis.

### 6. Phase Tracking Integration

Git versioning solves the phase numbering issues documented in `docs/phase_number_issues.md`. Instead of maintaining phase state only in memory (which gets lost during context compaction), git becomes the authoritative source of phase progress.

#### Problem Recap

From `phase_number_issues.md`:
- Phase identity is implicit - agent reconstructs from context, degrades over time
- Context compaction destroys state
- Separate counters for strategic/tactical create confusion

#### Solution: Paired Numbering with Git as Ground Truth

**`phase_state.yaml`** - Single source of truth at workspace root:

```yaml
phase_number: 3
phase_type: tactical
phase_name: "Requirement Extraction"
started_at: 2026-02-01T14:00:00Z
```

**Git tags** - Queryable milestones at phase boundaries:

```
phase-1-strategic-complete
phase-1-tactical-complete
phase-2-strategic-complete
phase-2-tactical-complete
phase-3-strategic-complete
```

**Archive files** - Extended naming with phase number:

```
archive/todos_phase_1_strategic_20260201_120000.md
archive/todos_phase_1_tactical_20260201_130000.md
archive/todos_phase_2_strategic_20260201_140000.md
```

#### Paired Numbering Model

Strategic and tactical phases share the same number since they're logically paired:

```
Phase 1: Strategic (plan) → Tactical (execute)
Phase 2: Strategic (plan) → Tactical (execute)
Phase 3: Strategic (plan) → Tactical (execute)
```

- `phase_number` starts at 1
- Increments when transitioning from tactical → strategic (starting a new pair)
- Both strategic and tactical in a pair share the same number

#### Phase Transition Flow

```
Tactical Phase 2 completes
    │
    ├─ 1. Archive todos → archive/todos_phase_2_tactical_{ts}.md
    ├─ 2. Update phase_state.yaml → {phase_number: 3, phase_type: strategic, ...}
    ├─ 3. Git commit: "[Phase 2 Tactical] Complete - archived N todos"
    └─ 4. Git tag: phase-2-tactical-complete
    │
    ▼
Strategic Phase 3 begins
```

#### Recovery After Context Compaction

When the agent needs to know where it is:

1. **Read `phase_state.yaml`** → Current phase number and type
2. **Run `git tag -l "phase-*"`** → See completed phases
3. **Run `git log --oneline -5`** → See recent work

This survives any context compaction because it queries files and git, not memory.

#### TodoManager Additions

```python
class TodoManager:
    def __init__(self, ...):
        ...
        self._phase_number: int = 1  # Paired phase counter
        self._current_phase_name: str = ""  # Human-readable name

    def get_phase_info(self) -> dict:
        """Get current phase information for commits and state files."""
        return {
            "phase_number": self._phase_number,
            "phase_type": "strategic" if self._is_strategic_phase else "tactical",
            "phase_name": self._current_phase_name or self._staged_phase_name,
        }

    def increment_phase_number(self) -> None:
        """Increment phase number (called on tactical → strategic transition)."""
        self._phase_number += 1
```

#### Commit Message Builder

```python
def _build_commit_message(self, todo: TodoItem) -> str:
    """Build structured commit message for completed todo."""
    phase_type = "Strategic" if self._is_strategic_phase else "Tactical"
    header = f"[Phase {self._phase_number} {phase_type}] {todo.id}: {todo.content}"

    body_lines = [
        f"Completed: {datetime.now(timezone.utc).isoformat()}",
    ]

    if todo.notes:
        body_lines.append(f"Notes: {'; '.join(todo.notes)}")

    return header + "\n\n" + "\n".join(body_lines)
```

#### GitManager Tag Method

```python
def tag(self, tag_name: str, message: str = None) -> bool:
    """Create a git tag at current HEAD.

    Args:
        tag_name: Tag name (e.g., "phase-2-tactical-complete")
        message: Optional tag message (creates annotated tag)

    Returns:
        True if successful, False otherwise
    """
    args = ["tag"]
    if message:
        args.extend(["-a", tag_name, "-m", message])
    else:
        args.append(tag_name)

    result = self._run_git(args)
    return result.returncode == 0

def list_tags(self, pattern: str = None) -> List[str]:
    """List git tags, optionally filtered by pattern.

    Args:
        pattern: Glob pattern (e.g., "phase-*")

    Returns:
        List of tag names
    """
    args = ["tag", "-l"]
    if pattern:
        args.append(pattern)

    result = self._run_git(args)
    if result.returncode != 0:
        return []

    return result.stdout.strip().split("\n") if result.stdout.strip() else []
```

#### Phase State File Updates

The graph transition logic handles `phase_state.yaml` updates:

```python
def update_phase_state(workspace: WorkspaceManager, todo_manager: TodoManager) -> None:
    """Update phase_state.yaml during transition."""
    info = todo_manager.get_phase_info()

    state = {
        "phase_number": info["phase_number"],
        "phase_type": info["phase_type"],
        "phase_name": info["phase_name"],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    workspace.write_file("phase_state.yaml", yaml.dump(state, default_flow_style=False))
```

#### Integration with Archive

Update `TodoManager.archive()` to use phase-aware naming:

```python
def archive(self, phase_name: str = "") -> str:
    """Archive todos with phase-aware naming."""
    # Use phase info for filename
    phase_type = "strategic" if self._is_strategic_phase else "tactical"
    ts_str = timestamp.strftime("%Y%m%d_%H%M%S")

    # New naming: todos_phase_{N}_{type}_{ts}.md
    filename = f"todos_phase_{self._phase_number}_{phase_type}_{ts_str}.md"
    archive_path = f"archive/{filename}"

    # ... rest of archive logic ...
```

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
| `git_tags` | Yes | Yes | Check phase progression anytime |

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
- [x] Create `src/managers/git_manager.py`
- [x] Implement `GitManager` with:
  - [x] `init_repository()` - skip if .git exists
  - [x] `commit()` - stage all and commit
  - [x] `log()` - commit history
  - [x] `show()` - commit details with truncation
  - [x] `diff()` - differences with truncation
  - [x] `status()` - current state with dirty/clean indication
  - [x] `has_uncommitted_changes()` - boolean check
  - [x] `tag()` - create git tag at HEAD
  - [x] `list_tags()` - list tags with optional pattern filter
  - [x] `_run_git()` - subprocess wrapper with timeout (60s default)
  - [x] `_truncate_output()` - max 500 lines / 10k words
- [x] Add graceful degradation when git unavailable
- [x] Export from `src/managers/__init__.py`
- [x] Write tests for GitManager (57 tests)

### Phase 2: Git Tools Package
- [x] Create `src/tools/git/__init__.py`
- [x] Create `src/tools/git/git_tools.py` with git_log, git_show, git_diff, git_status, git_tags
- [x] Tools access GitManager via `context.workspace_manager.git_manager`
- [x] Add GIT_TOOLS_METADATA with proper phase configuration (both phases)
- [x] Register in `src/tools/registry.py`
- [x] Add `git` category to `config/defaults.yaml`
- [x] Add `has_git()` convenience method to ToolContext
- [x] Write tests for git tools (24 tests)

### Phase 3: Workspace Initialization
- [x] Add `git_versioning` and `git_ignore_patterns` to workspace config schema
- [x] Update `WorkspaceManager.initialize()` to:
  - [x] Create GitManager instance
  - [x] Call `init_repository()` if git_versioning enabled
  - [x] Create initial `phase_state.yaml` file
  - [x] Store as `self._git_manager`
- [x] Add `git_manager` property to WorkspaceManager
- [x] Add `has_git()` convenience method to ToolContext (done in Phase 2)
- [x] Update `config/defaults.yaml` with git config
- [x] Update `config/schema.json` with git fields
- [x] Update `src/core/loader.py` WorkspaceConfig and ToolsConfig with git fields
- [x] Update `src/agent.py` to pass git config to WorkspaceManagerConfig
- [x] Write tests for git initialization (16 tests)

### Phase 4: Phase Tracking Integration
- [x] Add to TodoManager:
  - [x] `_phase_number: int` field (starts at 1)
  - [x] `_current_phase_name: str` field
  - [x] `get_phase_info()` method
  - [x] `increment_phase_number()` method
  - [x] `set_phase_name()` method
  - [x] `phase_number` and `current_phase_name` properties
- [x] Update `TodoManager.archive()` for phase-aware naming:
  - [x] Filename format: `todos_phase_{N}_{type}_{ts}.md`
  - [x] Header includes phase info
- [x] Add `_build_commit_message()` with phase info
- [x] Add phase state persistence to `export_state()` / `restore_state()`
- [x] Write tests for phase tracking (18 new tests)

### Phase 5: Auto-Commit Integration
- [x] Add `_commit_todo_completion()` to TodoManager
- [x] Hook into `TodoManager.complete()` - access git via `self._workspace.git_manager`
- [x] Write tests for auto-commit (6 tests)

### Phase 6: Phase Transition Updates
- [x] Update graph transition logic to:
  - [x] Update `phase_state.yaml` on transitions
  - [x] Create git tag on phase completion (e.g., `phase-2-tactical-complete`)
  - [x] Commit phase state changes
  - [x] Increment `phase_number` on tactical → strategic transition
- [x] Write tests for phase transitions with git (14 tests)

### Phase 7: Agent Instructions
- [x] Update `config/prompts/tactical.txt` with feature-oriented language
- [x] Update `config/prompts/strategic.txt` with git review instructions
- [x] Add instructions for reading `phase_state.yaml` on context recovery
- [ ] Test agent behavior with new prompts (manual testing)
