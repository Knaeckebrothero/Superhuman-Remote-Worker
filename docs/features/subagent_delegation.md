---
tags:
  - feature
  - design
  - orchestration
  - agent-loop
  - architecture
aliases:
  - subagent delegation
  - subjob spawning
  - delegate work
  - parallel subagents
related:
  - "[[agent_lifecycle]]"
  - "[[nats]]"
  - "[[vm_backend]]"
  - "[[projects]]"
---

# Subagent Delegation — Spawning Child Jobs from Within an Agent

Design document for letting agents delegate work to subagents (child jobs). Subagents are **branches** of the parent — they share the same VM, inherit the parent's workspace as a git branch, and work in parallel via git worktrees. The parent suspends while children run, then resumes to review diffs, resolve merge conflicts, and squash-merge results in creation order.

**Status:** Concept.

## Industry Research

Survey of how major AI providers implement subagent delegation (as of March 2026). This informed the design decisions below.

### Claude Code / Claude Agent SDK

- **Agent tool** spawns subagents as separate OS processes (JSON-lines over stdin/stdout).
- Each subagent gets a **fresh context window** — no shared conversation history. Only the task prompt passes down, only the final text result passes back. Intermediate tool calls and reasoning are discarded from the parent's perspective.
- **Filesystem shared by default**, with opt-in **Git worktree isolation** (`isolation: worktree`) for parallel code editing.
- **Foreground** (blocking) or **background** (async, `run_in_background: true`) modes. Multiple background agents run concurrently.
- **No nesting** — the Agent tool is excluded from subagent tool sets.
- ~50K token overhead per subprocess spawn (re-injects full system config each time).
- **Agent Teams** (experimental): fully independent Claude Code sessions with mailbox-based inter-agent messaging and shared task lists with file-locking.

### OpenAI Codex CLI / Agents SDK

**Codex CLI:**
- Five tools: `spawn_agent`, `send_input`, `wait_agent`, `resume_agent`, `close_agent`. Separates creation from blocking.
- Up to **6 concurrent threads**, each with its own **Git worktree**.
- Nesting capped at `max_depth: 1` by default.
- Subagents inherit parent's sandbox policy.

**Agents SDK (Python):**
- Two delegation patterns:
  - **Handoffs** — transfer entire conversation to another agent (system prompt swaps, history preserved). Control via `input_filter`, `nest_handoff_history`, `handoff_history_mapper`.
  - **`Agent.as_tool()`** — sub-agent runs as a tool call, returns string result. Parent retains control. Clean isolation: sub-agent gets only the generated input, no conversation history.
- Deterministic or LLM-driven orchestration. Guardrails run in parallel with agent execution.
- Stateless by default — persistent state requires explicit `context_variables`.

**Swarm (deprecated predecessor):**
- Three primitives: Agents, Routines, Handoffs-as-functions. Stateless, single-threaded.
- Key insight that carried forward: `transfer_to_XXX` functions are just regular tool functions that return an Agent.

### Kimi K2.5 (Moonshot AI)

- **Model-native Agent Swarm** — trained into model weights via PARL (Parallel Agent Reinforcement Learning). Not an external framework.
- **Orchestrator (trainable)** dynamically decides when/how many subagents to spawn (up to 100). **Subagents (frozen)** are instances of an earlier K2 checkpoint.
- Each subagent has **independent working memory** (isolated context). Only **distilled, task-relevant outputs** propagate back — not full interaction traces. This is "context sharding."
- Model learns through RL **when to parallelize vs. sequence**. Reward structure: `r_parallel` (incentivize spawning) + `r_finish` (reward completion) + `r_perf` (task quality), annealed so final policy optimizes purely on quality.
- **3-4.5x wall-clock speedup** vs single-agent. Critical-path metric (longest dependency chain), not total steps.
- Mitigated **serial collapse** (tendency to default to sequential) via staged reward shaping.
- Note: K2 (what we use via Groq) is single-agent only. Agent Swarm requires K2.5 via Moonshot API or self-hosted.

### Google ADK / LangGraph / A2A

**Google ADK:**
- Three deterministic **workflow agents** (no LLM overhead): `SequentialAgent`, `ParallelAgent`, `LoopAgent`.
- Two delegation patterns: `sub_agents` list (LLM-driven handoff) and `AgentTool` (agent-as-tool, parent retains control).
- Shared `session.state` dictionary for inter-agent communication (blackboard pattern).
- Callback-based guardrails: `before_agent_callback`, `after_tool_callback`, etc. Return `None` to proceed, return content to override.

**LangGraph (directly relevant — we use it):**
- **Subgraphs**: Package agent workflow as a node in parent graph. Shared state keys (automatic) or isolated state (manual transform functions).
- **`Send()` API**: Dynamic parallel fan-out. Creates concurrent node invocations merged by state reducers.
- **`Command` primitive**: Combines state update + control-flow directive. Enables edgeless graphs with runtime routing.
- **Interrupts**: `interrupt()` from inside nodes, `interrupt_before`/`interrupt_after` at compile time, `Command(resume=value)` for human-in-the-loop. Works in subgraphs.

**A2A Protocol (v0.3):**
- Wire protocol for agent-to-agent communication. Agent Cards for discovery, Task lifecycle (`submitted` → `working` → `completed` | `failed` | `input-required`).
- Complementary to MCP (horizontal agent-to-agent vs. vertical agent-to-tools).
- Task `input-required` state analogous to our `frozen` status.

### Cross-Provider Patterns

| Concern | Industry consensus | Our approach |
|---------|-------------------|--------------|
| **Delegation primitive** | Tool call (universal) | `delegate_work` tool |
| **Context isolation** | Fresh context per child, only task description down, only result up | Git branch + worktree per child, squash merge results back |
| **Parent waiting** | Process-level: Codex blocks in `wait_agent`; Kimi K2.5 orchestrator waits for frozen subagents | Checkpoint + wake (resource-efficient, fits existing `--resume`) |
| **Nesting depth** | Capped at 1 everywhere | `max_depth: 1` default |
| **Parallelism** | Codex: 6 threads; K2.5: up to 100; Claude Code: unlimited background | Hard cap: 5 subagents |
| **Result format** | Final text/structured output only, no intermediate traces | Child `completion.json` + summary injection |
| **Failure handling** | Parent resumes with failure status, decides next steps | Same — partial results + status codes |

### Design Decisions Informed by Research

1. **Synchronous only, no async mode.** No provider implements truly async subagents where the parent continues its own work. Claude Code's "background" agents are the closest, but still within a live process. We follow the industry consensus: parent suspends, children run, parent resumes with results.
2. **Single tool, not spawn/wait split.** Codex separates `spawn_agent` + `wait_agent` to allow parent work between spawning and blocking. Since our parent suspends entirely (checkpoint + wake), this separation adds complexity without benefit.
3. **Checkpoint + wake over long-polling.** Validated by Kimi K2.5's frozen-subagent pattern and by our existing `--resume` infrastructure. Claude Code and Codex hold processes alive, which wastes resources for long-running jobs.
4. **Hard cap of 5 subagents.** Codex allows 6, K2.5 up to 100 (model-native). 5 is practical for our use cases and keeps resource consumption bounded.
5. **Git worktree isolation (like Codex and Claude Code).** Children branch off the parent workspace and work in their own worktree. Context is isolated at the LLM level (fresh context window) but the filesystem is shared via git — children see everything the parent had at branch point. Squash merge keeps history clean.
6. **Children default to `autonomy: full`.** Prevents the deadlock where parent waits for children that are waiting for human review. The parent is already supervised.
7. **No nesting in v1.** Universal industry practice. Easy to lift later.
8. **LangGraph `Send()` is a future option** for intra-phase parallelism (e.g., processing multiple documents), but not needed for the subagent feature itself where children are separate processes.

## Problem Statement

Some jobs are too broad or multi-disciplinary for a single agent. A research job might need both deep literature review and code analysis. A documentation job might need parallel investigation of multiple subsystems. Today the only option is a single agent doing everything sequentially, which is slow and forces one config/persona to cover all skills.

Humans can create multiple jobs manually via the cockpit or MCP tools, but the agent itself has no way to decompose work and delegate parts of it.

## Design Goals

1. **Simple tool interface** — Models already understand subagent patterns. One tool, obvious semantics.
2. **Synchronous only** — Parent always suspends and waits. No async/fire-and-forget mode. No provider in the industry implements truly async subagents (where the parent continues its own work); we follow the same pattern.
3. **Resource-efficient** — Shared VM, shared filesystem (git worktrees). Parent process exits while children work (checkpoint + wake).
4. **Branch model** — Each child is a git branch off the parent workspace at the point of delegation. Children inherit the full workspace state. No document copying needed.
5. **Review + merge loop** — Parent resumes to review each child's diff, can approve (squash merge) or resume with feedback. Merge conflicts resolved by the parent.
6. **Squash merge** — Each child's work becomes a single commit on the parent branch. No branch history bloat.
7. **Failure tolerance** — Child failures don't crash the parent. Parent decides how to handle them.
8. **Hard cap: 5 subagents** — Maximum 5 parallel children per `delegate_work` call. Prevents runaway resource consumption.

## Tool Interface

A single tool: `delegate_work`. Available in both phases (strategic and tactical). Always synchronous — the parent suspends and waits for all children to complete.

```python
delegate_work(
    tasks: List[dict],       # 1-5 dicts: {"description": str, "config": str (optional)}
    context: str = "",       # Shared context/instructions for all children
    timeout: int = 7200,     # Max wait time in seconds (default 2h)
)
```

Hard limit: 1-5 tasks. The tool rejects calls with more than 5 tasks. No `documents` parameter — children inherit the full parent workspace via git branch.

### Single Subagent

```python
delegate_work(
    tasks=[{
        "description": "Research all GraphRAG papers published in 2025",
        "config": "scholar",
    }],
    context="Focus on retrieval architectures, ignore training-only papers.",
)
```

### Multiple Parallel Subagents

```python
delegate_work(
    tasks=[
        {"description": "Research GraphRAG papers from 2025", "config": "scholar"},
        {"description": "Analyze our current retrieval pipeline", "config": "developer"},
        {"description": "Review competitor products", "config": "critic"},
    ],
    context="We are evaluating whether to adopt GraphRAG for our document processing pipeline.",
)
```

Semantics: 1 task = 1 subagent. N tasks (max 5) = N parallel subagents. Parent always waits for all to finish.

## Execution Flow

### Phase 1: Branching and Delegation

```
Parent Agent (on main branch)
     │
     │  delegate_work(tasks=[A, B, C])
     │
     ├─ git commit (snapshot current state)
     ├─ git worktree add .worktrees/subagent_0 -b subagent/0
     ├─ git worktree add .worktrees/subagent_1 -b subagent/1
     ├─ git worktree add .worktrees/subagent_2 -b subagent/2
     │
     ├─ POST /api/jobs for each task (parent_id=X, creation_order=0,1,2)
     │
     ├─ checkpoint + set status → waiting
     ╳  (process exits)
```

Each child job gets:
- Its own **git worktree** (separate working directory, same repo)
- Its own **git branch** (`subagent/0`, `subagent/1`, etc.) branching off the parent's current commit
- Full access to the parent's workspace state at the branch point (workspace.md, plan.md, documents/, etc.)
- Its own **agent process** running concurrently on the same VM

### Phase 2: Parallel Execution

All children run concurrently, each in their own worktree. They have independent context windows and make their own commits on their respective branches. The parent process is not running.

```
subagent/0 (worktree: .worktrees/subagent_0)    ── working ──▶ done
subagent/1 (worktree: .worktrees/subagent_1)    ── working ──────────▶ done
subagent/2 (worktree: .worktrees/subagent_2)    ── working ────▶ done
                                                                       │
                                          all children terminal ◀──────┘
                                          orchestrator resumes parent
```

### Phase 3: Review and Merge

When all children reach a terminal state, the parent agent resumes. It enters a **review loop** where it processes each child's branch in creation order:

```
Parent Agent (resumed)
     │
     │  For each child in creation order (0, 1, 2):
     │    │
     │    ├─ git diff main..subagent/N  (review what child changed)
     │    ├─ Read child's completion summary + deliverables
     │    │
     │    ├─ Decision:
     │    │   ├─ APPROVE  → squash merge subagent/N into main
     │    │   │              (single commit, clean history)
     │    │   │              resolve merge conflicts if any
     │    │   │
     │    │   └─ RESUME WITH FEEDBACK → child continues working
     │    │              parent goes back to waiting
     │    │
     │  After all children merged:
     │    ├─ Clean up worktrees + branches
     │    └─ Continue with own work
```

### Squash Merge

Each approved child becomes a **single squash commit** on the parent's branch:

```
main:  ─── A ─── B ─── [delegate] ─── [subagent/0 squash] ─── [subagent/1 squash] ─── [subagent/2 squash] ─── ...
```

This keeps the parent's git history readable. The full branch history of each child is discarded after merge (but preserved in the child's job record if needed for audit).

Merge order is deterministic: subagent/0 merges first, then subagent/1, then subagent/2 — regardless of which finished first. This means subagent/1's merge may conflict with changes from subagent/0. The parent agent resolves these conflicts during the review phase.

### Why Checkpoint + Wake, Not Long-Polling

| Approach | Pros | Cons |
|----------|------|------|
| **Long-poll in tool** | Simple, no orchestrator changes | Parent process idles for hours, wastes memory/compute, checkpoint grows stale |
| **Graph wait node** | No process exit needed | Still holds resources, complex graph modification |
| **Checkpoint + wake** | Resource-efficient, clean separation | Requires orchestrator completion hook, resume injection |

The checkpoint approach fits naturally: the agent already supports `--resume`, checkpoints already exist, and the orchestrator already tracks job status. The parent's process exits cleanly and restarts only when needed.

## Data Model Changes

### `jobs` Table

```sql
ALTER TABLE jobs ADD COLUMN parent_job_id UUID REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE jobs ADD COLUMN creation_order SMALLINT;     -- 0-based index within sibling group, determines merge order
ALTER TABLE jobs ADD COLUMN branch_name VARCHAR(100);    -- git branch name (e.g., "subagent/0")
ALTER TABLE jobs ADD COLUMN worktree_path VARCHAR(500);  -- path to git worktree directory
ALTER TABLE jobs ADD COLUMN delegation_context TEXT;     -- shared context string from parent
```

### New Job Status

Add `waiting` to the job status enum (alongside `created`, `assigned`, `running`, `frozen`, `completed`, `failed`, `cancelled`):

```sql
-- The parent enters 'waiting' status when it suspends for children
ALTER TYPE job_status ADD VALUE 'waiting';
```

### Child Job Result Storage

When a child completes, its results are accessible via:
- **`git diff main..subagent/N`** — the primary review mechanism, shows exactly what the child changed
- `output/completion.json` in the child worktree (already exists)
- The `jobs` table row (summary, status, confidence — already stored by `finalize_job`)

No new table needed. The parent queries children by `parent_job_id` on resume, ordered by `creation_order`.

## Orchestrator Changes

### Job Creation Extension

`POST /api/jobs` accepts optional parent/branch fields:

```json
{
  "description": "Research GraphRAG papers from 2025",
  "config_name": "scholar",
  "parent_job_id": "uuid-of-parent",
  "creation_order": 0,
  "branch_name": "subagent/0",
  "worktree_path": "/workspace/job_<parent>/.worktrees/subagent_0",
  "delegation_context": "Focus on retrieval architectures..."
}
```

Validation: parent job must exist and be in `running` or `waiting` status.

### Completion Hook

When any job reaches a terminal state (`completed`, `failed`, `cancelled`, `frozen`), check:

```python
async def on_job_terminal(job_id: str):
    """Check if all sibling child jobs are done; if so, resume parent."""
    job = await get_job(job_id)
    if not job.parent_job_id:
        return  # not a child job

    siblings = await get_jobs_by_parent(job.parent_job_id)
    all_done = all(s.status in TERMINAL_STATUSES for s in siblings)

    if all_done:
        await resume_parent_job(job.parent_job_id, siblings)
```

### Parent Resume

When all children are done, the orchestrator:

1. Sets parent status back to `assigned` (or a new `resuming` status)
2. Builds a results summary from child jobs **in creation order**
3. The dispatch loop picks up the parent for `--resume`
4. On resume, the parent enters the review + merge loop

### Merge Order

Results are always reviewed and merged in **creation order**, not completion order. If the parent calls `delegate_work(tasks=[A, B, C])`, it reviews A first, merges A, then reviews B (which may now conflict with A's changes), merges B, then C.

This is enforced by the `creation_order` column (integer, 0-based):

```sql
SELECT * FROM jobs WHERE parent_job_id = :parent_id ORDER BY creation_order ASC;
```

Rationale: deterministic ordering makes the merge predictable. The parent constructed the tasks in a specific order, and merging in that order ensures conflicts are resolved incrementally rather than all at once.

### Resume Injection

The parent's checkpoint contains the `delegate_work` tool call. On resume, the graph provides the child results as the tool response, plus the branch diffs:

```json
{
  "delegate_work_result": {
    "status": "all_complete",
    "children": [
      {
        "job_id": "uuid-1",
        "description": "Research GraphRAG papers from 2025",
        "status": "completed",
        "confidence": 0.85,
        "summary": "Found 23 relevant papers. Key findings: ...",
        "branch": "subagent/0",
        "files_changed": 5,
        "insertions": 342,
        "deletions": 12,
        "surviving_processes": []
      },
      {
        "job_id": "uuid-2",
        "description": "Analyze our current retrieval pipeline",
        "status": "completed",
        "confidence": 0.92,
        "summary": "Current pipeline uses BM25 + reranking. Bottleneck is ...",
        "branch": "subagent/1",
        "files_changed": 3,
        "insertions": 128,
        "deletions": 0,
        "surviving_processes": [
          {"pid": 12345, "command": "node dev-server.js", "port": 8200}
        ]
      }
    ],
    "duration_seconds": 4832
  }
}
```

The parent then uses `git_diff` to review each branch, decides whether to approve or resume with feedback, and kills or keeps any surviving processes.

## Agent-Side Implementation

### Tool Module

New file: `src/tools/core/delegation.py`

Tool metadata:
```python
DELEGATION_TOOLS_METADATA = {
    "delegate_work": {
        "module": "core.delegation",
        "function": "delegate_work",
        "description": "Delegate work to 1-5 subagent jobs that branch off your workspace",
        "category": "core",
        "phases": ["strategic", "tactical"],
    },
}
```

### Tool Behavior (`delegate_work`)

1. Validate inputs (task descriptions non-empty, config names valid, 1-5 tasks)
2. Commit current workspace state (snapshot for branch point)
3. For each task (index `i`):
   - Create git branch `subagent/{i}` from current HEAD
   - Create git worktree at `.worktrees/subagent_{i}`
   - Call orchestrator API to create child job with `parent_job_id`, `branch_name`, `worktree_path`
4. Return message: `"Created 3 subagent branches. Suspending to wait for results."`
5. Set graph state flag: `awaiting_children = True` with child job IDs

### Graph Integration — Suspension

The `execute` node (or a post-tool check) detects `awaiting_children == True`:
- Saves checkpoint
- Sets parent job status to `waiting` via orchestrator API
- Exits the graph loop cleanly (not an error, not completion — a suspension)

### Graph Integration — Resume and Review Loop

On resume, if `awaiting_children` was set, the parent enters a review loop:

1. Query orchestrator for child results (ordered by `creation_order`)
2. Inject results summary as the tool response for `delegate_work`
3. For each child in creation order, the parent agent:
   - Runs `git diff main..subagent/{i}` to see what changed
   - Reads the child's `output/completion.json` for summary + confidence
   - Decides: **approve** or **resume with feedback**
4. If approved: `git merge --squash subagent/{i}` + `git commit` with a descriptive message
   - If merge conflicts occur, the parent agent resolves them (it has context about what both sides intended)
5. If resumed with feedback: child goes back to `running`, parent goes back to `waiting`
6. After all children are merged: clean up worktrees (`git worktree remove`) and branches (`git branch -d`)
7. Clear `awaiting_children` flag, continue normal execution

### Child Agent Workspace

Each child agent's workspace is the git worktree, not a separate job directory. The child sees:
- All files from the parent workspace at the branch point
- Its own branch where it makes commits normally (auto-commits on todo completion, etc.)
- The same VM, same filesystem, same datasource connections

The child's `WorkspaceManager` points to the worktree path instead of the parent's main workspace directory. The child does not know it's a subagent — it works normally within its worktree.

## Configuration and Kickoff

### Agent Config

Children **inherit the parent's resolved config** by default. The optional `config` field in the task dict overrides it with a different existing config (e.g., `scholar`, `developer`, `critic`). The parent never creates configs or writes instruction files for children — it picks from what's already available.

```yaml
# In parent's agent config YAML
delegation:
  enabled: true               # default: false
  max_depth: 1                 # nesting depth (1 = no grandchildren)
  default_timeout: 7200        # 2 hours
  max_timeout: 14400           # 4 hours
  allowed_configs:             # which configs children can use (empty = any)
    - scholar
    - developer
    - critic
```

The tool is only loaded when `delegation.enabled: true`. Maximum 5 subagents per call is a hard limit enforced in the tool, not configurable.

### Kickoff Message

The child's kickoff comes from two sources:

1. **`description`** (per task) — becomes the child's job description, equivalent to `--description` on the CLI. This is what the child sees as its assignment.
2. **`context`** (shared across all tasks) — injected as additional context. Provides shared background that all children need.

The child starts a normal job lifecycle from there — first strategic phase, todo creation, etc. It doesn't know it's a subagent (except that `delegate_work` is unavailable due to `max_depth`).

### What the Parent Does NOT Do

- Does not write instruction files or system prompts for children
- Does not craft YAML configs for children
- Does not pre-populate the child's workspace with new files (the branch already has everything)
- Does not set up the child's plan.md (the child creates its own plan in its first strategic phase)

The parent's job is to decompose the problem and describe each subtask clearly in the `description` field. Everything else is inherited or handled by the child's normal startup flow.

## Autonomy Interaction

Child jobs are forced to `autonomy: full` regardless of parent setting. This prevents deadlocks where the parent waits for children that are waiting for human review. The parent itself provides the review gate — it reviews diffs and approves/rejects during the merge phase.

## Edge Cases

### Child Failure
If a child fails or is cancelled, the parent still resumes. The failed child's branch is available for inspection (`git diff`, `git log`) but the parent skips its merge. The parent decides whether to retry (spawn a new delegation), work around it, or fail itself.

### Merge Conflicts
Since children branch from the same commit and are merged in creation order, later children may conflict with earlier ones. The parent agent resolves conflicts during the review phase — it has the context to understand what both sides intended. If the conflict is too complex, the parent can resume the child with feedback asking it to rebase.

### Timeout
If children don't complete within the timeout, the orchestrator:
1. Cancels remaining children
2. Resumes the parent with partial results + timeout indicator
3. Already-completed children's branches are still available for merge
4. Parent decides next steps

### Parent Cancellation
If the parent is cancelled while waiting:
1. All children are cancelled too (cascade)
2. Worktrees and branches are preserved for inspection (manual cleanup)

### Worktree Cleanup
After all children are merged (or skipped due to failure):
- `git worktree remove .worktrees/subagent_{i}` for each child
- `git branch -d subagent/{i}` for each merged branch
- `.worktrees/` directory cleaned up

If the parent fails during the merge phase, worktrees persist on disk. A cleanup routine (or the next `init.py` run) should detect orphaned worktrees.

### Nesting
For v1, `max_depth: 1` is enforced. If a child tries to call `delegate_work`, the tool returns an error: "Subagent nesting is not supported (max_depth=1)." The orchestrator checks the parent chain: if the job's `parent_job_id` is non-null, delegation is blocked.

### Git Versioning Required
Delegation requires `workspace.git_versioning: true` in the agent config. If git versioning is disabled, `delegate_work` returns an error explaining the requirement.

## Resource Isolation

### Industry Context

No AI coding agent has elegantly solved local multi-agent resource conflicts:
- **Claude Code**: No isolation at all. Agents actively kill each other's processes fighting over ports. [Open issue #34385](https://github.com/anthropics/claude-code/issues/34385).
- **Codex CLI**: Per-command network namespaces via bubblewrap (most sophisticated), but this is command-level, not agent-session-level.
- **Cursor / E2B**: Brute force — separate VM per agent. Solves everything but requires cloud infra.

Since our subagents share a VM, we implement a four-layer isolation strategy plus process tracking for the parent's review phase.

### Layer 1: Port Range Allocation

Each subagent gets a deterministic port range based on `creation_order`:

| Subagent | Port Range |
|----------|------------|
| Parent (reserved) | 8000-8099 |
| subagent/0 | 8100-8199 |
| subagent/1 | 8200-8299 |
| subagent/2 | 8300-8399 |
| subagent/3 | 8400-8499 |
| subagent/4 | 8500-8599 |

Base port = `8000 + (creation_order + 1) * 100`. Each agent gets 100 ports. Injected into the child's kickoff context and enforced/advised by the shell tools.

### Layer 2: Kickoff Context Injection

Each child receives sibling awareness in its job description, auto-generated by `delegate_work`:

```
=== SUBAGENT ENVIRONMENT ===
You are subagent 1 of 3 (branch: subagent/1, worktree: .worktrees/subagent_1).
Your assigned port range: 8200-8299. Use these ports for any servers or services.
Do NOT use ports outside your range — other subagents are running concurrently.

Sibling subagents (do not interfere with their processes or files):
  - subagent/0: worktree .worktrees/subagent_0, ports 8100-8199
  - subagent/2: worktree .worktrees/subagent_2, ports 8300-8399
================================
```

This is prepended to the child's `delegation_context`, not the `description`. The child sees it as environmental context, not as its assignment.

### Layer 3: Network Namespaces (Optional, Defense-in-Depth)

When available, each subagent's tmux session runs inside its own network namespace. This provides hard isolation — even if an agent ignores port assignments, it can't collide with siblings.

```bash
# Create namespace for subagent 0
ip netns add subagent_0
# Run agent process inside it
ip netns exec subagent_0 python agent.py ...
```

This is opt-in (`delegation.network_isolation: true` in config) because it adds complexity and may interfere with agents that need to reach shared services (databases, APIs on the host). When disabled, port range allocation + awareness injection are the primary defense.

### Layer 4: Sibling Manifest File

At delegation time, `delegate_work` writes `.subagents.json` in the repo root (tracked by git on each branch):

```json
{
  "parent_job_id": "uuid-parent",
  "delegation_timestamp": "2026-03-21T14:30:00Z",
  "subagents": [
    {
      "creation_order": 0,
      "branch": "subagent/0",
      "worktree": ".worktrees/subagent_0",
      "port_range": [8100, 8199],
      "description": "Research GraphRAG papers from 2025",
      "config": "scholar",
      "job_id": "uuid-0"
    },
    {
      "creation_order": 1,
      "branch": "subagent/1",
      "worktree": ".worktrees/subagent_1",
      "port_range": [8200, 8299],
      "description": "Analyze our current retrieval pipeline",
      "config": "developer",
      "job_id": "uuid-1"
    }
  ]
}
```

Each subagent can read this file to understand its environment. Shell tools can reference it for port validation.

### Process Tracking

Each subagent's shell manager tracks processes started during execution. The `ShellManager` already records commands per tmux session. We extend this to track **long-running processes** (servers, watchers, background jobs) that are still alive when the child completes.

When a child job finishes, the agent records its surviving processes in the completion data:

```json
{
  "surviving_processes": [
    {"pid": 12345, "command": "node server.js", "port": 8100, "started_at": "..."},
    {"pid": 12350, "command": "python -m http.server 8101", "port": 8101, "started_at": "..."}
  ]
}
```

This is collected by scanning the child's tmux session and process group for alive processes at job completion time.

### Parent Process Review

When the parent resumes for the review phase, it receives each child's surviving processes in the results payload:

```json
{
  "children": [
    {
      "job_id": "uuid-0",
      "branch": "subagent/0",
      "status": "completed",
      "surviving_processes": [
        {"pid": 12345, "command": "node server.js", "port": 8100}
      ]
    }
  ]
}
```

The parent agent can then decide for each child:
- **Kill all** — clean up the child's processes before merging (`kill -TERM <pid>`)
- **Keep some** — if a process is useful (e.g., a dev server the parent wants to test against)
- **Ignore** — let them die naturally when the tmux session is cleaned up

Process cleanup happens during worktree cleanup: when `git worktree remove` runs, any processes still rooted in that worktree's directory should be terminated first. The cleanup routine:

1. List surviving processes for the child (from completion data or by scanning the process group)
2. Send `SIGTERM` to each, wait briefly
3. Send `SIGKILL` if any survive
4. Remove tmux session
5. Remove worktree

### Configuration

```yaml
delegation:
  # ... existing keys ...
  network_isolation: false     # opt-in network namespaces per subagent
  port_range_base: 8000        # base port for range allocation
  port_range_size: 100         # ports per subagent
```

## Cockpit UI

### Job List
- Child jobs show a "parent" badge with a link to the parent job
- Parent jobs in `waiting` status show a "waiting for N children" indicator
- Expand/collapse to see the child job tree

### Job Detail
- New "Children" tab on parent jobs showing child status, progress, results
- New "Parent" link on child jobs

## Implementation Phases

### Phase 1: Core Plumbing
- `parent_job_id`, `creation_order`, `branch_name`, `worktree_path` columns in DB
- `waiting` job status
- Orchestrator: create child jobs with parent reference
- Orchestrator: completion hook to detect all-children-done
- Orchestrator: resume parent job

### Phase 2: Agent Tool + Git Integration
- `delegate_work` tool: validation, branch creation, worktree setup
- Port range allocation + kickoff context injection
- Sibling manifest file (`.subagents.json`)
- Graph suspension logic (detect `awaiting_children`, checkpoint, exit)
- Resume injection (child results + branch stats as tool response)
- Child `WorkspaceManager` pointed at worktree path
- Config keys + validation

### Phase 3: Review + Merge Loop
- Parent review flow: `git diff`, read completion.json, approve/resume decision
- Surviving process tracking at child completion
- Parent process review (kill/keep decision)
- Squash merge implementation with conflict detection
- Resume-with-feedback flow (child goes back to working)
- Worktree + branch + process + tmux cleanup after merge
- Merge conflict resolution by parent agent

### Phase 4: Cockpit UI
- Job tree visualization (parent → children with branches)
- Parent/child navigation
- Waiting status display
- Branch diff viewer

### Phase 5: Hardening
- Timeout enforcement
- Cascade cancellation
- Nesting depth enforcement
- Orphaned worktree cleanup
- Resource limit tracking (total token spend across parent + children)
- Optional network namespace isolation (`delegation.network_isolation`)
- Port range enforcement in shell tools (warn/block out-of-range binds)

## Inheritance Defaults

Subagents are branches, so they naturally inherit everything up to the branch point. The key decisions are about what's *not* inherited or what's overridden:

| Property | Inherited? | Rationale |
|----------|-----------|-----------|
| **Workspace files** | Yes — via git branch | Full workspace state at branch point (all tracked files) |
| **VM / user** | Yes — shared | Same VM, same user account, different worktree directories |
| **Project** | Yes | Children are part of the same project scope |
| **Datasources** | Yes | Same DB connections, same VM network |
| **Config** | Inherited by default, overridable per task | Parent picks from existing configs, never creates new ones |
| **Autonomy** | No — forced to `full` | Prevents deadlock; parent is the review gate |
| **Git history** | Yes — via branch | Children can see full commit history of the parent |
| **Context window** | No — fresh | Each child starts with a clean LLM context (context sharding) |
| **Memory (recall store)** | Read-only if project-scoped | Children can recall project memories but write to their own store |
| **Tmux sessions** | No — separate | Each child gets its own tmux session for shell tools |

## Resolved Design Decisions

1. **Cost tracking**: Not needed — all LLM requests are already tracked per-job in MongoDB. Parent + child token spend is queryable via existing audit trail.
2. **Merge automation**: Always require explicit parent review. No auto-approve based on confidence. The review loop is the core value of the delegation model.
3. **Shared knowledge base**: Children get full read/write access to Neo4j. Neo4j handles concurrent writes natively. Subagents work on different tasks so conflicts are unlikely; minor duplicates can be noticed and cleaned up later. Don't overengineer.
4. **Worktree disk space**: Not a concern. Git worktrees share the object store — only working tree files are duplicated. Typical workspaces (documents, markdown, configs) are small.
5. **Child checkpoints**: Already solved. Children are separate jobs with their own `job_id`, so checkpoints go to `workspace/checkpoints/job_<child_id>.db` — no conflict with the parent.
