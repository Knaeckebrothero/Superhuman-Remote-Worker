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

**Status:** Implemented. All four phases complete — tool, review/merge, cockpit UI, hardening. 46 tests in `tests/test_delegation.py`.

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
| **Parent waiting** | Process-level: Codex blocks in `wait_agent`; Kimi K2.5 orchestrator waits for frozen subagents | Checkpoint + wake (resource-efficient, fits existing resume infrastructure) |
| **Nesting depth** | Capped at 1 everywhere | `max_depth: 1` default |
| **Parallelism** | Codex: 6 threads; K2.5: up to 100; Claude Code: unlimited background | Hard cap: 5 subagents |
| **Result format** | Final text/structured output only, no intermediate traces | Child `completion.json` + summary injection |
| **Failure handling** | Parent resumes with failure status, decides next steps | Same — partial results + status codes |

### Design Decisions Informed by Research

1. **Synchronous only, no async mode.** No provider implements truly async subagents where the parent continues its own work. Claude Code's "background" agents are the closest, but still within a live process. We follow the industry consensus: parent suspends, children run, parent resumes with results.
2. **Single tool, not spawn/wait split.** Codex separates `spawn_agent` + `wait_agent` to allow parent work between spawning and blocking. Since our parent suspends entirely (checkpoint + wake), this separation adds complexity without benefit.
3. **Checkpoint + wake over long-polling.** Validated by Kimi K2.5's frozen-subagent pattern and by our existing resume infrastructure (the orchestrator re-dispatches a paused job to a fresh agent pod). Claude Code and Codex hold processes alive, which wastes resources for long-running jobs.
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

The checkpoint approach fits naturally: the agent already supports resume (via the orchestrator), checkpoints already exist, and the orchestrator already tracks job status. The parent's process exits cleanly and restarts only when needed.

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
3. The dispatch loop picks up the parent and dispatches it for resume
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

1. **`description`** (per task) — becomes the child's job description, i.e. the same field the orchestrator passes to the agent for any dispatched job. This is what the child sees as its assignment.
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

### Nesting (Option C — Delegation-Only Depth)

The delegation depth of a job is the count of **delegation links** in its ancestor chain. A delegation link is a `parent_job_id` relationship where the child has `creation_order IS NOT NULL`. Lifecycle links (scholar/critic jobs with `creation_order IS NULL`) contribute zero to the depth count.

A job can call `delegate_work` when its delegation depth is strictly less than `max_depth` (default: 1). If blocked, the tool returns: "Delegation depth limit reached (depth={d}, max_depth={m})."

**Depth examples (max_depth=1):**

| Job | Relationship | Delegation Depth | Can Delegate? |
|-----|-------------|-----------------|---------------|
| Main job | root (`parent_job_id` NULL) | 0 | Yes |
| Scholar of main | lifecycle link (`creation_order` NULL) | 0 | Yes |
| Critic of main | lifecycle link (`creation_order` NULL) | 0 | Yes |
| Delegation child of main | delegation link (`creation_order` 0-4) | 1 | No |
| Delegation child of critic | delegation link, parent is lifecycle | 1 | No |
| Delegation child of scholar | delegation link, parent is lifecycle | 1 | No |

**Enforcement:** Depth is computed server-side via `get_delegation_depth()` in `orchestrator/database/postgres.py` using a recursive CTE. The `delegate_work` tool queries this before creating children.

**Why lifecycle links are excluded:** Scholars and critics are orchestrator-managed lifecycle hooks (pre-research, post-verification), not agent-spawned parallel work. Blocking them from delegating would prevent a critic from parallelizing verification streams or a scholar from fanning out research threads — both are high-value use cases.

**Lifecycle recursion guards (separate concern):** The guards in `_spawn_scholar_subjob` (`if job.get("parent_job_id"): return None`) and `_trigger_verification_on_complete` (`if job.get("parent_job_id") is not None: return`) prevent lifecycle recursion (scholar spawning scholar, critic spawning critic). These are orthogonal to delegation depth and must not be changed.

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

## Implementation Plan

### Already Implemented (no changes needed)

The following infrastructure already exists in the codebase:

**Database schema** (`orchestrator/database/schema.sql:284-422`):
- Jobs table has all delegation columns: `parent_job_id UUID`, `creation_order SMALLINT`, `worktree_path VARCHAR(500)`, `delegation_context TEXT`, `branch_name VARCHAR(200)`, `merge_status VARCHAR(50)`
- Status enum includes `'waiting'`
- `get_delegation_children()` returns children ordered by `creation_order` (`postgres.py:823`)
- `all_delegation_children_terminal()` checks if all siblings are in terminal state (`postgres.py:853`)
- `get_delegation_depth()` recursive CTE counting delegation links only (`postgres.py:889`)

**Orchestrator completion flow** (`orchestrator/main.py`):
- `_handle_delegation_child_completion()` (line 5119): checks if all siblings done → builds child_results summary → stores in parent context as `delegation_results` → transitions parent `waiting` → `paused` → triggers dispatch
- `_squash_merge_subjob()` (line 215): merges subjob branch into parent via Gitea PR, pre-merge cleanup of job-scoped files
- `complete_job()` calls both (lines 5778-5807)

**Job creation** (`orchestrator/main.py:2408`):
- `JobCreate` model accepts `parent_job_id`, `creation_order`, `worktree_path`, `delegation_context`
- Gitea branching creates `subjob/{short_id}/{config_name}` branches for subjobs

**Agent workspace** (`src/agent.py:1020-1052`):
- Worktree creation for subjobs on remote backends (SSH `git worktree add`)
- Fallback to standard workspace init on failure
- `GitManager.from_worktree()` (`src/managers/git_manager.py:795`) initializes GitManager on existing worktrees

**Config** (`src/core/loader.py:927-945`, `config/defaults.yaml:259-264`):
- `DelegationConfig` dataclass: `enabled`, `max_depth`, `default_timeout`, `max_timeout`, `allowed_configs`
- `config.tools.delegation: []` in defaults (opt-in per expert)
- Critic and scholar configs enable delegation (`config/experts/{critic,scholar}/config.yaml`)

**Prompts and guides**:
- Critic strategic prompts have `{% if has_tool("delegate_work") %}` blocks with detailed delegation guidance
- Scholar strategic prompts and todo_guide.md have delegation phase templates
- Tool registry has placeholder entry (`src/tools/registry.py:78-85`)

---

### Phase 1: `delegate_work` Tool Implementation

**Goal**: Working tool that creates child jobs, suspends parent, resumes with results.

#### 1.1 Tool module: `src/tools/delegation/delegate_work.py`

New file. The tool function:

```python
async def delegate_work(
    tasks: list[dict],       # 1-5 dicts: {"description": str, "config": str (optional)}
    context: str = "",       # Shared context for all children
    timeout: int = 7200,     # Max wait in seconds (default 2h)
    *,
    # Injected by tool loader (not user-facing):
    tool_context: ToolContext,
) -> str:
```

**Validation** (return error string, don't raise):
- 1-5 tasks, each with non-empty `description`
- `config` values (if provided) in `delegation.allowed_configs` or allow-list empty
- `timeout` ≤ `delegation.max_timeout`
- `delegation.enabled == True` (should be guaranteed by tool loading, but defense-in-depth)
- `git_versioning == True` (worktrees require git)
- Depth check: call orchestrator `GET /api/jobs/{job_id}/delegation-depth` or use cached job metadata. Must be < `max_depth`

**Execution steps:**
1. Commit current workspace state: `git_manager.commit("Delegation snapshot before spawning N subagents")`
2. Push to Gitea: `git_manager.push()` (so children can branch from this commit)
3. Build port range context per child (Layer 2 awareness injection)
4. Write `.subagents.json` manifest to workspace root
5. For each task (index `i`):
   - Build child job payload:
     - `description`: task description (with subagent environment block prepended to `delegation_context`)
     - `config_name`: task `config` or parent's config_name
     - `parent_job_id`: current job_id
     - `creation_order`: `i`
     - `delegation_context`: shared `context` + port range + sibling info
     - `config_override`: `{"autonomy": "full", "delegation": {"enabled": false}}` (no nesting v1)
     - `project_id`: parent's project_id
     - `priority`: parent's priority (inherit)
   - POST to orchestrator `POST /api/jobs` via `tool_context.orchestrator_client`
   - Record child job_id
6. Return formatted message: `"Created {N} subagent jobs. Suspending to wait for results.\n\nChildren:\n- subagent/0: {desc} ({config})\n..."`
7. Set delegation state on tool_context for graph suspension detection

**Files to create:**
- `src/tools/delegation/__init__.py`
- `src/tools/delegation/delegate_work.py`

**Files to modify:**
- `src/tools/registry.py`: Remove `placeholder: True` from `delegate_work` entry, update module path to `delegation.delegate_work`

#### 1.2 Orchestrator client: `create_delegation_job()` method

Add to `src/api/orchestrator_client.py`:

```python
async def create_delegation_job(
    self,
    description: str,
    config_name: str,
    parent_job_id: str,
    creation_order: int,
    delegation_context: str = "",
    config_override: dict | None = None,
    project_id: str | None = None,
    priority: int = 5,
) -> dict[str, Any] | None:
```

Simple wrapper around `POST /api/jobs` with delegation-specific fields. Returns the created job dict or None on failure.

#### 1.3 Tool context: expose orchestrator client and job metadata

**`src/tools/context.py`** (ToolContext dataclass):
- Add `orchestrator_client: Optional[Any] = None` attribute
- Add `_job_metadata: Dict[str, Any] = field(default_factory=dict)` for job_id, project_id, priority, config_name, repo_name

No new freeze mechanism needed — ToolContext already has `request_freeze()` / `consume_freeze_request()` (lines 433-455), used by `send_message` for blocking freezes. The delegation tool reuses this.

**`src/agent.py:_setup_job_tools()`**:
- Pass orchestrator client reference to ToolContext
- Pass job metadata to ToolContext

#### 1.4 Graph suspension: reuse existing freeze mechanism

The `request_freeze()` → `consume_freeze_request()` pattern already exists in ToolContext (line 433) and is consumed in `audited_tools` (graph.py:2942-2960). When a tool calls `request_freeze()`, the audited_tools node:
1. Writes `output/job_frozen.json`
2. Commits to git
3. Sets `result["should_stop"] = True`

The delegation tool calls:
```python
tool_context.request_freeze({
    "freeze_type": "delegation",
    "child_job_ids": child_ids,
    "child_count": len(tasks),
})
```

**Required fix in audited_tools** (graph.py:2954): The existing freeze handling sets `result["should_stop"] = True` but does NOT propagate `freeze_data` through graph state. For delegation, the orchestrator's `determine_job_status()` needs `freeze_type` to return `"waiting"`. Add:

```python
result["should_stop"] = True
result["freeze_data"] = freeze_req  # NEW: propagate to report_completion()
```

This also benefits the existing `blocking_message` freeze flow (currently falls through to `pending_review` because freeze_data never reaches the orchestrator — not a bug, but now it's explicit).

The graph then flows: `tools` → `check_todos` → (sees `should_stop`) → `check_goal` → END. The `freeze_data` propagates through graph state → `report_completion()` → orchestrator.

#### 1.5 Orchestrator: handle delegation freeze in `determine_job_status()`

**`orchestrator/services/completion.py:determine_job_status()`** (line 250):

Add delegation handling before the phase boundary fallback (before line 320):

```python
if freeze_type == "delegation":
    return ("waiting", None)
```

This ensures the parent is set to `waiting` when it reports completion with a delegation freeze.

#### 1.6 Orchestrator: skip cleanup for delegation-waiting jobs

**`orchestrator/main.py:complete_job()`** — the snapshot/cleanup section (lines 5835-5898) should skip when the new status is `"waiting"`:

```python
if new_status not in ("waiting", "reviewing"):
    # Snapshot and cleanup VM/workspace
    ...
```

This preserves the VM/workspace while children are running on it.

#### 1.7 Resume injection: delegation results as tool response

When the parent resumes after all children complete, `delegation_results` is already in the job context (stored by `_handle_delegation_child_completion()`). The resume path needs to inject this.

**`src/graph.py:restore_todo_state()`** (line 2209):

After restoring todos, check for delegation results in metadata:
```python
delegation_results = metadata.get("delegation_results")
if delegation_results:
    # Inject as a HumanMessage summarizing child results
    results_msg = _format_delegation_results(delegation_results)
    return {
        ...existing returns...,
        "messages": [HumanMessage(content=results_msg)],
    }
```

The results message includes per-child: job_id, status, config, summary, confidence, branch_name. The parent agent then uses `git_diff` and `read_file` tools to review each child's changes and decide on merging.

**Merge flow**: The existing `_squash_merge_subjob()` auto-merges via Gitea PR when each child completes. This is the right behavior for critic/scholar subjobs but NOT for delegation children, where the parent should review before merging.

**Change**: In `complete_job()` (line 5778-5782), skip auto-merge for delegation children:

```python
# Skip auto-merge for delegation children — parent reviews and merges
if job.get("parent_job_id") and job.get("creation_order") is None:
    merge_result = await _squash_merge_subjob(job_id)
```

Delegation children (have `creation_order`) keep their branches until the parent explicitly merges them. The parent agent uses `git_diff`, `git_merge_squash` (Phase 2) tools to review and merge each child's branch in creation order during the review phase.

#### 1.8 GitManager: add worktree management methods

**`src/managers/git_manager.py`** — add methods for delegation:

```python
def worktree_add(self, path: str, branch: str, create_branch: bool = True) -> bool:
    """Create a git worktree at the given path on the specified branch."""

def worktree_remove(self, path: str, force: bool = False) -> bool:
    """Remove a git worktree."""

def worktree_list(self) -> list[dict]:
    """List all worktrees."""

def merge_squash(self, branch: str) -> tuple[bool, str]:
    """Squash-merge the given branch into current HEAD. Returns (success, message)."""
```

These work on both local (subprocess) and remote (backend.shell_run) paths, following the existing `_use_backend` pattern.

#### 1.9 Port range allocation

In `delegate_work`, generate the environment block per child:

```python
def _build_subagent_env_block(creation_order: int, total: int, tasks: list) -> str:
    base_port = 8000 + (creation_order + 1) * 100
    # Returns the "=== SUBAGENT ENVIRONMENT ===" block from the design doc
```

This is prepended to each child's `delegation_context`.

---

### Phase 2: Review & Merge Tools

**Goal**: Parent agent can review child diffs and merge them after resume.

#### 2.1 Git tools for delegation review

The parent already has `git_diff`, `git_status`, `git_tags` tools. Add or enhance:

**`src/tools/git/git_tools.py`** — add:
- `git_merge_squash(branch: str)`: Squash-merge a branch into current HEAD, commit with descriptive message
- `git_worktree_cleanup(branch: str)`: Remove worktree and delete branch after merge

These are available in both phases (strategic and tactical).

#### 2.2 Worktree cleanup after review

After the parent merges all children, it should clean up:
1. For each child branch: `git worktree remove .worktrees/subagent_{i}` + `git branch -d subagent/{i}`
2. Remove `.subagents.json`
3. Commit cleanup

This can be done by the parent agent using the git tools, or automated in the merge tool.

#### 2.3 Resume-with-feedback for children

If the parent rejects a child's work, it can resume the child with feedback. Use existing `OrchestratorClient.resume_job()` method (line 590).

Add a tool: `resume_delegation_child(job_id: str, feedback: str)`:
- Calls `POST /api/jobs/{job_id}/resume` with feedback
- Sets parent back to `waiting` (call orchestrator)
- Re-suspends the graph (same delegation freeze pattern)

---

### Phase 3: Cockpit UI

**Goal**: Visualize delegation relationships in the web UI.

#### 3.1 Job list enhancements

**`cockpit/src/app/simple/pages/sessions/sessions-page.component.ts`** (or equivalent job list):
- Show "parent" badge with link for child jobs
- Show "waiting for N children" indicator for parent jobs in `waiting` status
- Expandable tree view for parent → children hierarchy

#### 3.2 Job detail enhancements

- New "Children" tab on parent job detail: shows child jobs with status, progress, config, branch
- "Parent" link on child job detail pages
- Branch diff viewer: show `git diff main..subagent/N` for each child

#### 3.3 Real-time status updates

- SSE/WebSocket updates for child job progress (already exists for regular jobs)
- Parent job detail auto-refreshes when children complete

---

### Phase 4: Hardening

**Goal**: Production safety and edge case handling.

#### 4.1 Timeout enforcement

In `_handle_delegation_child_completion()` or a separate periodic task:
- Check delegation timeout from parent's config
- If elapsed > timeout: cancel remaining children, resume parent with partial results

#### 4.2 Cascade cancellation

When a parent job is cancelled:
- Find all delegation children via `get_descendant_jobs()` (already exists, postgres.py:935)
- Cancel each non-terminal child
- Clean up worktrees

#### 4.3 Orphaned worktree cleanup

Add to `init.py` or a periodic maintenance task:
- Scan for `.worktrees/` directories without corresponding running jobs
- Remove orphaned worktrees and branches
- Log warnings for manual review

#### 4.4 Resource limit tracking

- Total token spend queryable via existing MongoDB audit trail (per-job `llm_requests`)
- Add optional `delegation.max_total_tokens` config to cap aggregate spend across parent + children

#### 4.5 Port range awareness (deferred)

Network namespace isolation (Layer 3) is deferred. Port range allocation and awareness injection (Layer 2) are implemented in Phase 1. Port range *enforcement* in shell tools (warn/block out-of-range binds) is deferred to this phase.

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

## Integration Tests

Test file: `tests/test_delegation.py`

### Phase 1 Tests

**TestDelegateWorkTool:**
- `test_validate_empty_tasks`: Rejects empty task list
- `test_validate_too_many_tasks`: Rejects >5 tasks
- `test_validate_empty_description`: Rejects task with empty description
- `test_validate_timeout_exceeds_max`: Rejects timeout > max_timeout
- `test_validate_disallowed_config`: Rejects config not in allowed_configs
- `test_validate_git_versioning_required`: Rejects when git_versioning=False
- `test_validate_depth_exceeded`: Rejects when delegation depth >= max_depth
- `test_creates_child_jobs`: Verify orchestrator API called with correct payloads
- `test_port_range_allocation`: Verify port range blocks in delegation_context
- `test_subagent_manifest`: Verify `.subagents.json` written correctly
- `test_sets_delegation_result`: Verify tool_context.delegation_result set for graph suspension

**TestGraphDelegationSuspension:**
- `test_audited_tools_detects_delegation`: Verify `should_stop=True` and `freeze_data` set after delegation tool
- `test_graph_exits_after_delegation`: Verify graph reaches END after delegation suspension
- `test_freeze_data_contains_child_ids`: Verify freeze_data has correct structure

**TestDetermineJobStatusDelegation:**
- `test_delegation_freeze_returns_waiting`: `freeze_type="delegation"` → status "waiting"
- `test_delegation_children_skip_auto_merge`: Children with creation_order skip `_squash_merge_subjob()`
- `test_cleanup_skipped_for_waiting`: VM/workspace cleanup skipped when status="waiting"

**TestDelegationResume:**
- `test_resume_injects_delegation_results`: Verify HumanMessage with child results injected on resume
- `test_delegation_results_format`: Verify message includes per-child status, summary, branch info
- `test_resume_clears_stop_flags`: Verify should_stop/goal_achieved cleared on delegation resume

**TestGitManagerWorktree:**
- `test_worktree_add_local`: Create worktree on local filesystem
- `test_worktree_remove_local`: Remove worktree on local filesystem
- `test_worktree_list`: List worktrees
- `test_merge_squash`: Squash-merge branch into current HEAD

**TestPortRangeAllocation:**
- `test_port_range_single_child`: Verify port range for 1 child
- `test_port_range_five_children`: Verify port ranges for max children
- `test_env_block_format`: Verify subagent environment block format

### Phase 2 Tests

**TestDelegationReview:**
- `test_git_merge_squash_tool`: Verify squash merge via git tool
- `test_worktree_cleanup_tool`: Verify worktree and branch cleanup
- `test_resume_child_with_feedback`: Verify child resume + parent re-suspension

### Verification Commands

```bash
# Phase 1: Core delegation
pytest tests/test_delegation.py -v -x

# Phase 1: Specific test class
pytest tests/test_delegation.py::TestDelegateWorkTool -v
pytest tests/test_delegation.py::TestGraphDelegationSuspension -v
pytest tests/test_delegation.py::TestDetermineJobStatusDelegation -v
pytest tests/test_delegation.py::TestDelegationResume -v
pytest tests/test_delegation.py::TestGitManagerWorktree -v

# Full suite (ensure no regressions)
pytest tests/ -x -q --tb=short

# Lint
ruff check src/tools/delegation/ tests/test_delegation.py
ruff format src/tools/delegation/ tests/test_delegation.py
```

## File Change Summary

### New Files
| File | Phase | Purpose |
|------|-------|---------|
| `src/tools/delegation/__init__.py` | 1 | Package init, exports metadata |
| `src/tools/delegation/delegate_work.py` | 1 | Tool implementation |
| `tests/test_delegation.py` | 1 | Integration tests |

### Modified Files
| File | Phase | Changes |
|------|-------|---------|
| `src/tools/registry.py` | 1 | Remove placeholder, update module path |
| `src/api/orchestrator_client.py` | 1 | Add `create_delegation_job()` method |
| `src/tools/tool_context.py` | 1 | Add orchestrator_client, job_metadata, delegation_result |
| `src/agent.py` | 1 | Pass orchestrator client + metadata to ToolContext |
| `src/graph.py` | 1 | Delegation suspension in audited_tools, resume injection in restore_todo_state |
| `orchestrator/services/completion.py` | 1 | Handle `freeze_type="delegation"` → "waiting" |
| `orchestrator/main.py` | 1 | Skip auto-merge for delegation children, skip cleanup for waiting |
| `src/managers/git_manager.py` | 1 | Add worktree_add, worktree_remove, worktree_list, merge_squash |
| `src/tools/git/git_tools.py` | 2 | Add git_merge_squash, git_worktree_cleanup tools |
