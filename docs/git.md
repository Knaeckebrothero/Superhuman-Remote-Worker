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
| **Queryable history** | Agent can use git commands to explore its own past (log, diff, show, blame) |
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

## Design

### Commit Granularity

**Option: Commit per todo (recommended)**

Each `todo_complete` call triggers a git commit. This:
- Reinforces that each todo is an atomic unit of work
- Provides fine-grained history for debugging
- Makes it easy to pinpoint which todo caused an issue

Commit message format:
```
[Phase 3] todo_4: Extract requirements from chapter 2

Completed: 2026-02-01T14:23:00
Notes: Found 12 requirements, 3 marked as ambiguous
```

**Alternative options:**
- Commit per phase (coarser, less debugging value)
- Agent-decided commits (more flexible, less predictable)
- Branch per phase with todo commits (complex but maximum flexibility)

### Agent Git Tools

Rather than injecting diffs into context, give the agent git tools and let it query history as needed:

| Tool | Purpose |
|------|---------|
| `git_log` | View commit history (`git log --oneline`) |
| `git_show` | Inspect a specific commit's changes |
| `git_diff` | Compare current state to previous commits |
| `git_blame` | See when/why specific lines were added |
| `git_reset` | Rollback to a previous state (tactical phase only?) |

This approach:
- Lets the agent decide when it needs historical context
- Avoids bloating context with potentially large diffs
- Gives the agent autonomy in how it uses history

### Initialization

When a workspace is created:

1. Run `git init` in the workspace directory
2. Create `.gitignore` (exclude checkpoints, logs, cache files)
3. Configure local git user (`agent@workspace.local`)
4. Initial commit with workspace template files

### Integration Points

**`todo_complete` tool** - Natural place to trigger commits:
- Already knows todo ID, content, and completion notes
- Can construct meaningful commit messages
- Atomic: one todo = one commit

**`archive_phase` node** - Alternative if committing per-phase:
- Already handles phase boundary logic
- Knows phase number and type
- Would commit after archiving todos

**`on_tactical_phase_complete`** - Could inject git summary:
- Add brief `git log --oneline -5` to phase marker message
- Remind strategic phase it can query git for details

## Configuration

```yaml
workspace:
  git_versioning: true  # Enable git repository per workspace
  git_ignore_patterns:
    - "*.db"
    - "*.log"
    - "__pycache__/"
    - ".DS_Store"
    - "checkpoints/"
```

Disabled by default for backward compatibility.

## Implementation Considerations

### Git Dependency

Git must be available in the execution environment:
- **Development**: Already installed on most dev machines
- **Container**: Include git in the Dockerfile

Graceful degradation: if git is not installed, log a warning and continue without versioning.

### Commit Message Structure

Structured commit messages enable programmatic parsing:

```
[Phase {n}] todo_{id}: {todo_content_summary}

Completed: {iso_timestamp}
Phase type: {strategic|tactical}
Notes: {completion_notes}
```

### Tool Phase Restrictions

Consider which git tools are available in which phase:

| Tool | Strategic | Tactical | Rationale |
|------|-----------|----------|-----------|
| `git_log` | Yes | Yes | Read-only, always useful |
| `git_show` | Yes | Yes | Read-only, always useful |
| `git_diff` | Yes | Yes | Read-only, always useful |
| `git_blame` | Yes | Yes | Read-only, always useful |
| `git_reset` | No? | Yes? | Destructive, needs careful design |

### Rollback Semantics

If the agent can rollback:
- What happens to the todo state? (TodoManager must sync with git state)
- Should rollback restore archived todos?
- How does this interact with checkpoints?

This needs careful design. Initial implementation could omit rollback and add it later.

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

### Code Repository Integration

When agents work on actual codebases (not just workspaces), this same infrastructure applies:
- Clone target repo into workspace
- Agent commits as it works
- Changes can be reviewed before pushing

### Multi-Agent Collaboration

If multiple agents work on the same workspace:
- Each agent could have its own branch
- Merge conflicts handled at strategic phase boundaries
- Git history shows which agent made which changes

## Open Questions

1. **Commit on todo_complete vs. end of phase?** - Per-todo seems better for debugging, but creates more commits.

2. **Should strategic phase auto-commit?** - Strategic phase modifies workspace.md and plan.md. Should these be committed?

3. **How to handle rollback + todo state?** - If agent rolls back, TodoManager state must match.

4. **What if a commit fails?** - Should todo_complete fail? Or log warning and continue?

5. **Git hooks?** - Could use pre-commit hooks for validation, but adds complexity.
