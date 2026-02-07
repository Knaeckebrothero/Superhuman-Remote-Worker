# Coding Agent

Design document for a coding-capable agent configuration that can implement features, fix bugs, run tests, and submit PRs — tested on this project.

> **Related**: [cloud_workspace.md](./cloud_workspace.md) covers the production sandbox/container architecture. This document focuses on the agent toolset and a local prototype to validate the approach before containerizing.

## Motivation

- Claude Code works great interactively, but TOS prohibits automated/programmatic access via subscription plans
- API pricing for top models is prohibitive for long-running agent loops
- We already have 90% of the infrastructure: phase-based execution, tool registry, workspace management, config system, git tools
- **The only truly missing piece was `run_command`** — everything else existed

## What Already Exists

The agent already has a comprehensive toolset:

| Capability | Tools | Status |
|-----------|-------|--------|
| Read files with line numbers | `read_file` (offset, limit, page ranges) | Done |
| Write/create files | `write_file` (read-before-write enforced) | Done |
| Surgical edits | `edit_file` (exact-match replace, append, prepend) | Done |
| Search file contents | `search_files` (full-text, case-insensitive, 50-result limit) | Done |
| Find/list files | `list_files` (glob patterns, recursive depth 0-3) | Done |
| File operations | `move_file`, `copy_file`, `delete_file`, `rename_file`, `file_exists` | Done |
| Directory management | `create_directory`, `delete_directory` | Done |
| Git history | `git_log`, `git_diff`, `git_show`, `git_status`, `git_tags` | Done |
| Shell execution | `run_command` (subprocess, timeout, output truncation) | **Done** |
| Task management | `next_phase_todos`, `todo_complete`, `todo_rewind`, `mark_complete`, `job_complete` | Done |
| Phase alternation | Strategic (plan/review) / Tactical (execute) | Done |
| Workspace memory | `workspace.md` injected into every LLM call | Done |
| Agent container | `Dockerfile.agent` with Python, git, curl, ripgrep, jq | Done |

## The `run_command` Tool

**Location**: `src/tools/coding/coding_tools.py`
**Registry**: Category `coding`, available in both strategic and tactical phases.

```python
@tool
def run_command(
    command: str,
    working_dir: str = None,  # Relative to workspace root (e.g., "repo")
    timeout: int = 120        # Max 600s
) -> str:
    """Execute a shell command in the workspace and return stdout/stderr.

    Returns structured output:
        Exit code: 0
        --- stdout ---
        ...
        --- stderr ---
        ...
    """
```

### Features

- **Subprocess execution** via `subprocess.run()` with `shell=True` and `capture_output=True`
- **Output truncation**: Caps stdout/stderr at 50,000 chars each (configurable via `max_output_chars` in ToolContext config). Keeps the tail of output (most useful for test results).
- **Timeout**: Default 120s, max 600s (10 minutes). Commands exceeding the timeout are killed.
- **Safety**: Blocked command list (`sudo`, `reboot`, `shutdown`, `poweroff`, `halt`, `init`, `systemctl`). Path validation via workspace manager prevents directory traversal.
- **Working directory**: Defaults to workspace root. Set `working_dir="repo"` to run in the cloned repository.

### Production (Containerized)

When running in k3s (see [cloud_workspace.md](./cloud_workspace.md)), `run_command` executes inside the agent's container. The container IS the sandbox — no nested containers needed:

```
┌─────────────────────────────────────────────────┐
│  Agent Pod (k3s Job)                             │
│                                                   │
│  python agent.py                                  │
│    ├── read_file("src/foo.py")   → reads from PVC│
│    ├── edit_file("src/foo.py")   → writes to PVC │
│    └── run_command("pytest")     → subprocess     │
│                                                   │
│  /workspace (PVC) ← agent's universe             │
│  Security: NetworkPolicy, seccomp, non-root      │
└─────────────────────────────────────────────────┘
```

## Usage

### Running a Coding Job

```bash
python agent.py --config coder \
  --git-url https://gitea.example.com/user/repo.git \
  --git-branch feature/my-feature \
  --description "Implement the feature described in the attached document" \
  --document-path docs/some_feature.md
```

**What happens:**
1. A new job is created in PostgreSQL
2. Workspace `workspace/job_<uuid>/` is initialized with `archive/`, `documents/`, `output/`, `repo/` directories
3. The repository is cloned into `workspace/job_<uuid>/repo/` and the specified branch is checked out
4. Documents are copied to `workspace/job_<uuid>/documents/`
5. The agent starts its phase alternation loop

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--config coder` | — | Use the coder expert config |
| `--git-url` | — | Git repository URL to clone into `workspace/repo/` |
| `--git-branch` | `main` | Branch to checkout (created if it doesn't exist) |
| `--description` | — | What the agent should accomplish |
| `--document-path` | — | Reference document (copied to `workspace/documents/`) |

### What the Agent Does

**Strategic Phase 1 — Orientation:**
1. Read the feature doc (`read_file`)
2. Explore the codebase (`list_files`, `search_files`)
3. Read key files to understand patterns (`read_file`)
4. Write a plan to `plan.md`
5. Create tactical todos

**Tactical Phase 1 — Implementation:**
1. For each todo: read target file, make edits, verify
2. `run_command(command="pytest tests/ -x", working_dir="repo")` after changes
3. Fix failures, mark todos complete

**Strategic Phase 2 — Review:**
1. `git_diff` to review all changes
2. `run_command(command="pytest tests/", working_dir="repo")` for full test suite
3. `run_command(command="ruff check src/", working_dir="repo")` for linting
4. Either fix issues (another tactical phase) or finalize

**Strategic Phase 3 — Delivery:**
1. Write/update documentation if needed
2. Create commit with descriptive message
3. Push branch, create PR (via `run_command(command="gh pr create ...", working_dir="repo")`)
4. `job_complete`

### Workspace Layout

```
workspace/job_<uuid>/
├── workspace.md          # Agent's persistent memory
├── plan.md               # Implementation plan
├── todos.yaml            # Current tasks
├── archive/              # Phase history
├── documents/            # The feature doc / task description
├── output/               # Final deliverables
└── repo/                 # ← Git clone of target repository
    ├── src/
    ├── tests/
    ├── cockpit/
    └── ...
```

## Implementation Details

### Files Created/Modified (Phase 1)

| File | What | Purpose |
|------|------|---------|
| `src/tools/coding/__init__.py` | **New** | Package init — `create_coding_tools()`, `get_coding_metadata()` |
| `src/tools/coding/coding_tools.py` | **New** | `run_command` tool implementation |
| `config/experts/coder/config.yaml` | **New** | 22 tools: workspace (11) + coding (1) + core (5) + git (5) |
| `config/experts/coder/instructions.md` | **New** | System prompt for coding agent behavior |
| `config/schema.json` | Modified | Added `coding` tool category to schema |
| `src/core/loader.py` | Modified | Added `coding` field to `ToolsConfig`, wired into both config parsers and `get_all_tool_names()` |
| `src/tools/registry.py` | Modified | Import + register coding metadata, `coding` category handling in `load_tools()` |
| `agent.py` | Modified | `--git-url` / `--git-branch` CLI args, passes git info into job context |
| `src/agent.py` | Modified | Repo clone logic in `_setup_job_workspace()` |
| `docker/Dockerfile.agent` | Modified | Added `ripgrep` and `jq` |
| `CLAUDE.md` | Modified | Documented `coding` tool category |

### Config: `config/experts/coder/config.yaml`

```yaml
# yaml-language-server: $schema=../../schema.json
$extends: defaults

agent_id: coder
display_name: Coding Agent

workspace:
  structure:
    - archive/
    - documents/
    - output/
    - repo/
  git_versioning: true

tools:
  workspace:
    - read_file
    - write_file
    - edit_file
    - list_files
    - search_files
    - file_exists
    - move_file
    - copy_file
    - delete_file
    - create_directory
    - delete_directory
  coding:
    - run_command
  core:
    - next_phase_todos
    - todo_complete
    - todo_rewind
    - mark_complete
    - job_complete
  git:
    - git_log
    - git_diff
    - git_show
    - git_status
    - git_tags
  document: null
  research: null
  citation: null
  graph: null
```

### Repo Bootstrap (in `src/agent.py`)

When `--git-url` is provided, `_setup_job_workspace()` clones the repo after workspace initialization:

1. Runs `git clone --branch <branch> <url> <workspace>/repo/`
2. If the branch doesn't exist yet, clones default branch and creates + checks out the new branch
3. Timeout: 300s for the clone operation
4. Skipped on `--resume` (workspace already has the repo)

### `instructions.md` — Key Principles

The system prompt teaches the agent to:

1. **Understand before changing**: Read files, search for patterns, understand the architecture before making edits
2. **Small, testable changes**: One logical change at a time, test after each
3. **Test-driven confidence**: Always run tests after changes, don't mark done until tests pass
4. **Match existing style**: Follow project conventions
5. **Git discipline**: Check `git_status` and `git_diff` regularly
6. **Workspace as memory**: Write discoveries and decisions to `workspace.md`
7. **Don't over-engineer**: Implement what's asked, nothing more
8. **Ask for help via workspace**: Write questions to `workspace.md` for human reviewers

## Sandbox Connection

For production deployment, the coding agent runs inside the containerized workspace described in [cloud_workspace.md](./cloud_workspace.md). The key insight:

**No container-in-container needed.** The agent process and `run_command` subprocess both run inside the same pod. The pod IS the sandbox:

| Concern | Solution | Source |
|---------|----------|--------|
| Filesystem isolation | Pod PVC, read-only root + emptyDir overlays | cloud_workspace.md |
| Network isolation | NetworkPolicy + Squid proxy for FQDN allowlist | cloud_workspace.md |
| Resource limits | Pod CPU/memory/storage limits | cloud_workspace.md |
| Credential safety | Secrets mounted outside workspace, env redaction | cloud_workspace.md |
| Cleanup | Job TTL + ownerReferences cascade PVC deletion | cloud_workspace.md |

The `Dockerfile.agent` already includes `ripgrep` and `jq` for coding support. For frontend projects (Angular/Node), a separate `Dockerfile.coder` extending the base image with Node.js would be needed.

## Implementation Roadmap

### Phase 1: Prototype (Minimum Viable) — DONE

- [x] Implement `run_command` tool with subprocess + timeout + output truncation (`src/tools/coding/`)
- [x] Register in `TOOL_REGISTRY` (category: `coding`, phases: `["strategic", "tactical"]`)
- [x] Add `coding` field to `ToolsConfig` dataclass and both config parsers
- [x] Add `coding` category to `config/schema.json`
- [x] Create `config/experts/coder/config.yaml` (22 tools)
- [x] Write `config/experts/coder/instructions.md`
- [x] Add `--git-url` / `--git-branch` params to `agent.py`
- [x] Implement repo clone in `_setup_job_workspace()` (`src/agent.py`)
- [x] Add `ripgrep` and `jq` to `Dockerfile.agent`
- [ ] Test locally: give it a simple task on this project

### Phase 2: Iteration
- [ ] Run on a real feature doc from `docs/` — observe where it struggles
- [ ] Tune system prompt based on failure modes
- [ ] Add `run_command` output truncation strategies (head+tail, grep for errors)
- [ ] Consider enhancing `search_files` with regex support (currently plain text only)

### Phase 3: Delivery Integration
- [ ] `run_command("git commit ...")` + `run_command("git push ...")` for branch delivery
- [ ] `run_command("gh pr create ...")` for PR creation (needs `gh` in container + auth)
- [ ] Connect to Gitea integration (existing, commit `a4f7d24`) for repo provisioning
- [ ] Orchestrator: "coding job" type that bootstraps a repo clone

### Phase 4: Container + Production
- [ ] Create `Dockerfile.coder` extending `Dockerfile.agent` with Node, gh CLI
- [ ] Test in container: verify `run_command` works correctly inside pod
- [ ] Apply cloud_workspace.md security (NetworkPolicy, secrets mount, etc.)
- [ ] Test on k3s cluster with real job submission via cockpit

## Open Questions

1. **Interactive commands**: What happens if the agent runs something that waits for input (e.g., `npm init`)? Currently just times out — may need stdin handling.
2. **Long-running processes**: Dev servers (`npm start`) block. Need background process support or just document "don't do that"?
3. **Auth for git push/PR**: How to provide git credentials inside the sandbox without exposing them to the agent's LLM context? Token mounted as file + gitconfig.
4. **Self-improvement**: Can the coding agent modify its own codebase? (The singularity question. For now: yes, on a feature branch, reviewed by a human.)
