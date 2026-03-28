---
tags:
  - feature
  - design
  - orchestration
  - workspace
related:
  - "[[subagent_delegation]]"
  - "[[vm_backend]]"
  - "[[hardened_container]]"
---

# Subjob Worktree Sharing — Critics and Scholars on the Parent's Workspace

Subjobs (critics, scholars) should run on the **same workspace backend** (VM or container) as the parent job, isolated via **git worktrees**. Today they get independent workspace containers, which wastes resources and disconnects them from the parent's actual environment.

**Status:** Implemented (Phase 1-3).

## Problem

When a parent job spawns a critic or scholar:

1. The subjob is created without the parent's `context.vm` or `context.workspace_container` — only `git_remote_url` is propagated (`main.py:4379`, `main.py:4884`)
2. The dispatcher sees no workspace backend on the subjob → `_job_needs_container()` returns `True` → provisions a **new container pod**
3. The subjob clones the Gitea repo into a fresh workspace — a full copy, not a branch view of the parent's environment
4. The parent's VM sits idle (status "reviewing") while the critic runs on a separate pod
5. The IDE button on the parent times out (VM on Tailscale IP unreachable from proxy) while the subjob's works (container on cluster IP)

This means:
- **Wasted resources** — two pods running for one logical piece of work
- **Disconnected environments** — the critic doesn't see the parent's installed packages, build artifacts, or runtime state
- **Broken review loop** — when a critic returns feedback and the parent resumes, the parent has no visibility into what the critic saw

## Design

### Core idea

Subjobs inherit the parent's workspace backend and use `git worktree add` to get their own working directory on the same filesystem. From the IDE, the operator sees the full tree:

```
/home/agent-host/
  workspace/                    <-- parent job (main branch checkout)
  worktrees/
    f32b3-critic/               <-- critic subjob (worktree on subjob/f32b3/critic branch)
    a91c7-scholar/              <-- scholar subjob (worktree)
```

Each agent process gets its own tmux session (`agent_{job_id[:12]}`), its own workspace directory (worktree), and its own LLM context. They share the underlying filesystem, git history, and code-server instance.

### Execution flow

```
Parent job running on VM
  └─ /home/agent-host/workspace (branch: main)
  │
  │  Job completes → status "reviewing"
  │  Agent process exits, VM stays alive
  │
  ├─ Orchestrator creates critic job
  │    context.vm = parent's context.vm (inherited)
  │    worktree_path = /home/agent-host/worktrees/f32b3-critic
  │    branch_name = subjob/f32b3/critic
  │
  ├─ Dispatcher detects vm.status="ready" → injects SSH config
  │    workspace.remote.workspace_path = /home/agent-host/worktrees/f32b3-critic
  │
  ├─ Agent receives job, connects to parent's VM via SSH
  │    Runs: git -C /home/agent-host/workspace fetch origin subjob/f32b3/critic
  │    Runs: git -C /home/agent-host/workspace worktree add \
  │             /home/agent-host/worktrees/f32b3-critic subjob/f32b3/critic
  │    WorkspaceManager root = /home/agent-host/worktrees/f32b3-critic
  │
  ├─ Critic works in worktree, commits, pushes
  │
  ├─ Critic completes:
  │    If verdict=approved → squash merge → parent moves to completed
  │    If verdict=returned → parent resumes with feedback (new round)
  │
  └─ VM deleted when parent reaches terminal status (completed/failed)
     Worktrees are ephemeral — cleaned up with the VM
```

### What changes

#### 1. Orchestrator — subjob creation inherits parent's workspace backend

**Files:** `orchestrator/main.py` — `_spawn_scholar_subjob()` (line ~4311) and `_trigger_verification_on_complete()` (line ~4811)

After building the subjob context dict, inject the parent's workspace backend context:

```python
parent_ctx = job.get("context") or {}
if isinstance(parent_ctx, str):
    try:
        parent_ctx = json.loads(parent_ctx)
    except (json.JSONDecodeError, ValueError):
        parent_ctx = {}

# Inherit workspace backend so subjob runs on parent's VM/container
if parent_ctx.get("vm"):
    subjob_context["vm"] = parent_ctx["vm"]
elif parent_ctx.get("workspace_container"):
    subjob_context["workspace_container"] = parent_ctx["workspace_container"]
```

Compute worktree path and pass to `create_job()`:

```python
worktree_path = f"/home/agent-host/worktrees/{short_id}-{config_name}"

subjob_job = await postgres_db.create_job(
    ...,
    worktree_path=worktree_path,  # column already exists in schema
)
```

The `worktree_path` column already exists (`schema.sql:391`) and `create_job()` already accepts it (`postgres.py:572`).

#### 2. Orchestrator — dispatcher skips provisioning for inherited backends

**File:** `orchestrator/main.py` — `_job_needs_container()` (line ~1000)

Currently defaults to provisioning a container when no backend is explicitly set. Must skip when the job inherits a ready backend:

```python
def _job_needs_container(job: dict) -> bool:
    # If job already has a ready VM or container (inherited from parent), no new container needed
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    if ctx.get("vm", {}).get("status") == "ready":
        return False
    if ctx.get("workspace_container", {}).get("status") == "ready":
        return False

    # ... existing logic unchanged ...
```

`_job_needs_vm()` does not need changes — inherited VMs have `status: "ready"`, not `requested: True`, so it correctly returns `False`.

#### 3. Orchestrator — dispatch uses worktree path

**File:** `orchestrator/main.py` — `_dispatch_job_to_agent()` (line ~636)

After the existing SSH config injection block, override `workspace_path` if the job has a `worktree_path`:

```python
# Existing code injects: remote["workspace_path"] = "/home/agent-host/workspace"
# Override with worktree path for subjobs:
worktree_path = job.get("worktree_path")
if worktree_path:
    remote["workspace_path"] = worktree_path
```

This applies to both the VM and container injection blocks.

Also add `worktree_path` to the data sent to the agent. Two options:
- **Option A**: Add `worktree_path` field to `JobStartRequest` (both orchestrator and agent sides)
- **Option B**: Pass via `context` dict (already forwarded to agent metadata)

Option B is simpler — add to `remaining_context`:

```python
if job.get("worktree_path"):
    remaining_context["worktree_path"] = job["worktree_path"]
```

The agent receives this in `metadata` (via `_process_orchestrator_job` at `app.py:458`: `metadata.update(context)`).

#### 4. Agent — worktree creation instead of clone

**File:** `src/agent.py` — workspace initialization (line ~970)

After RemoteBackend connects and before WorkspaceManager initializes, check for worktree mode:

```python
worktree_path = metadata.get("worktree_path")
if worktree_path and workspace_backend and workspace_backend.supports_shell:
    # Subjob on shared VM/container — create git worktree instead of clone
    branch_name = metadata.get("branch_name", "main")
    parent_workspace = "/home/agent-host/workspace"

    # Fetch the subjob branch (created by orchestrator on Gitea)
    workspace_backend._exec(
        f"git -C {parent_workspace} fetch origin {branch_name}", timeout=60
    )
    # Create worktree for the subjob's branch
    workspace_backend._exec(
        f"mkdir -p $(dirname {worktree_path})", timeout=10
    )
    workspace_backend._exec(
        f"git -C {parent_workspace} worktree add {worktree_path} {branch_name}",
        timeout=30,
    )
    logger.info(f"Created git worktree at {worktree_path} (branch: {branch_name})")
```

The RemoteBackend already has `_exec(command, timeout)` (line 265 of `remote.py`) which runs arbitrary SSH commands — this is exactly what we need.

WorkspaceManager then initializes with the worktree path as its root (already handled by `workspace_path` in config). The worktree IS a valid git repo (`.git` is a file pointing to the parent's `.git` dir), so `git_versioning` works normally.

**Skip clone**: When `worktree_path` is set, skip the normal `git clone` + `checkout_branch` flow in `_initialize_git()`. The worktree already has the right branch checked out.

**File:** `src/managers/git_manager.py`

Add a convenience method:

```python
@classmethod
def from_worktree(cls, worktree_path: Path) -> Optional["GitManager"]:
    """Create a GitManager for an existing git worktree."""
    if not (worktree_path / ".git").exists():
        return None
    mgr = cls(worktree_path)
    mgr._run_git(["config", "user.email", "agent@workspace.local"])
    mgr._run_git(["config", "user.name", "Agent"])
    return mgr
```

#### 5. Worktree cleanup

**Approach: No explicit cleanup during job lifecycle.**

Worktrees live on the VM/container filesystem. They are ephemeral:
- **VM jobs**: VM is deleted when parent reaches terminal status (`main.py:5190-5197`). All worktrees go with it.
- **Container jobs**: Container pod is deleted on parent completion (`main.py:5200-5209`). emptyDir wiped.
- **Multi-round critics**: Worktree persists across resume cycles (critic goes "waiting" → resumed → works → completes). No need to recreate between rounds.

If explicit cleanup is ever needed (e.g., 5+ parallel delegation subagents), it can be added later as a post-merge SSH command. Not needed for the critic/scholar use case (max 1-2 concurrent subjobs).

#### 6. IDE — root job only, root directory

**Already implemented** in the current session:
- `canOpenIde()` returns `false` for jobs with `parent_job_id` (`job-list.component.ts:1112`)
- `hasSnapshot()` returns `false` for subjobs (`job-review.component.ts:786`)
- The root job's IDE opens at `/home/agent-host/workspace`
- The operator can browse into `../worktrees/` from code-server's file explorer

### What does NOT change

| Component | Reason |
|-----------|--------|
| **Gitea branch model** | Subjobs still get branches (`subjob/{short_id}/{config}`) created before dispatch |
| **Squash merge flow** | `_squash_merge_subjob()` works on the Gitea-side branch, independent of worktrees |
| **Agent context isolation** | Each subjob has its own LLM context window, workspace.md, todos.yaml |
| **Tmux sessions** | Each agent process creates `agent_{job_id[:12]}` — unique per job, no conflicts |
| **Parent suspension** | Parent stays "reviewing"/"waiting" while subjobs run |
| **Database schema** | `worktree_path` column already exists (`schema.sql:389-391`) |
| **`create_job()` API** | Already accepts `worktree_path` parameter (`postgres.py:572`) |
| **VM/container teardown** | Only happens on parent terminal status — subjobs don't trigger it |

## Implementation phases

### Phase 1: Context inheritance and dispatch (orchestrator)

**File:** `orchestrator/main.py`

| Change | Location | Lines |
|--------|----------|-------|
| Inherit parent `context.vm` / `context.workspace_container` into scholar context | `_spawn_scholar_subjob()` | ~4311, after context dict is built |
| Pass `worktree_path` to `create_job()` | `_spawn_scholar_subjob()` | ~4341 |
| Same inheritance for critic context | `_trigger_verification_on_complete()` | ~4811, after context dict is built |
| Pass `worktree_path` to `create_job()` | `_trigger_verification_on_complete()` | ~4846 |
| Skip container provisioning for inherited backends | `_job_needs_container()` | ~1000, add early return |
| Use `worktree_path` as `workspace_path` in dispatch | `_dispatch_job_to_agent()` | ~650, after SSH config injection |
| Pass `worktree_path` in context to agent | `_dispatch_job_to_agent()` | ~779, add to `remaining_context` |

### Phase 2: Worktree creation on agent

**Files:** `src/agent.py`, `src/managers/git_manager.py`

| Change | Location |
|--------|----------|
| Detect `worktree_path` in metadata | `_setup_job_workspace()` ~line 970 |
| Run `git fetch` + `git worktree add` via `RemoteBackend._exec()` | Before WorkspaceManager init |
| Add `GitManager.from_worktree()` class method | `git_manager.py` |
| Skip `git clone` when worktree is already set up | `_initialize_git()` or workspace init guard |

### Phase 3: Testing

| Test | Scope |
|------|-------|
| `_spawn_scholar_subjob` inherits parent VM context | Unit (mock DB) |
| `_trigger_verification_on_complete` inherits parent container context | Unit (mock DB) |
| `_job_needs_container` returns `False` for inherited VM | Unit |
| `_job_needs_container` returns `False` for inherited container | Unit |
| Dispatch injects `worktree_path` as `workspace_path` | Unit |
| `GitManager.from_worktree()` works on a real worktree | Integration (tmpdir) |
| Agent creates worktree via `_exec()` on RemoteBackend | Integration (mock SSH) |

## Edge cases

| Scenario | Handling |
|----------|----------|
| **Parent VM deleted before subjob dispatched** | Inherited `context.vm` is stale. Dispatch connects to SSH → fails → job retries or falls back to container. Add a status check at dispatch time. |
| **Multiple concurrent subjobs on same VM** | Each gets its own worktree + tmux session. Git worktrees support concurrent use. No filesystem conflicts. |
| **Subjob needs packages not on parent** | Install in user-writable dirs (pip/npm without sudo). Packages persist on shared filesystem and are available to the parent too. |
| **Parent completes before subjob (race)** | VM teardown only happens on parent terminal status (`completed`/`failed`). "Reviewing"/"waiting" status blocks teardown. |
| **Worktree branch not in local clone** | `git fetch origin {branch}` before `git worktree add`. Gitea creates the branch before dispatch. |
| **Critic multi-round flow** | Worktree persists across rounds. Critic goes waiting→resumed→working→completed without worktree recreation. Branch is preserved (`delete_branch_after_merge=False`). |
| **Parent has no git repo (edge)** | Fall back to current behavior (independent container). `worktree_path` only set when parent has `repo_name`. |
| **Local dev (no VM/container)** | `worktree_path` not set → existing `git clone` flow. No change to local dev experience. |

## Relationship to full delegation feature

This is a subset of the design in `docs/features/subagent_delegation.md`. The full delegation feature adds:
- `delegate_work` tool for agent-initiated task decomposition (up to 5 parallel subagents)
- Parent review loop with approve/resume-with-feedback
- `creation_order`-based deterministic merge ordering
- `waiting` job status for parent suspension

The worktree sharing mechanism designed here is identical to what the full delegation feature will use. Implementing it now for critics/scholars validates the approach and the infrastructure (`worktree_path` column, dispatch injection, agent worktree init) before the larger feature lands.
