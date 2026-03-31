---
tags:
  - architecture
  - agent
  - deployment
  - orchestration
  - infrastructure
aliases:
  - agent lifecycle
  - persistent agent
  - worker agent
  - agent modes
  - dynamic scaling
related:
  - "[[vm_backend]]"
  - "[[nats]]"
  - "[[job_auto_assign]]"
  - "[[projects]]"
  - "[[memory_light]]"
  - "[[auxiliary]]"
---

# Agent Lifecycle — Persistent and Worker Modes

Design and implementation document for the two-mode agent architecture: Persistent (permanent, interactive,
session-based) and Worker (autonomous, pooled, job-based). Same container image, same shared infrastructure, different
execution loops and lifecycle management. The persistent mode is additive — the existing worker codebase is unchanged.

**Status:** All phases implemented (2026-03-29). Interactive loop, WebSocket transport, orchestrator integration,
thread schema, job delegation tools, and cockpit chat UI are functional. Deferred items remain across phases (see
checkboxes).

## Overview

### Current State

Agents are pre-deployed as a static K8s Deployment with `replicas: 2` in `deployment/21-agent.yaml`. Each agent is a long-running process that registers with the orchestrator, sends heartbeats every 60 seconds, and picks up one job at a time from the auto-dispatch loop (runs every 30 seconds). Jobs execute the phase alternation graph (`src/graph.py`) — strategic/tactical cycles — then the agent returns to idle. There is no interactive mode, no dynamic scaling, and no persistent user sessions.

### Target Architecture

Two agent modes sharing a single container image, distinguished by a startup flag:

```
                         ┌──────────────────────────────────┐
                         │           Orchestrator            │
                         │                                   │
                         │  Persistent Agent Provisioner      │
                         │  Worker Pool Manager               │
                         │  Dispatch Loop (30s)               │
                         │  WebSocket Proxy                   │
                         └──────┬──────────────────┬─────────┘
                                │                  │
                   ┌────────────┘                  └───────────────┐
                   │                                               │
        ┌──────────▼──────────┐              ┌─────────────────────▼────────┐
        │   Worker Pool       │              │   Persistent Agent Pods      │
        │   (K8s Deployment)  │              │   (Dynamic K8s Pods)         │
        │                     │              │                              │
        │  ┌───┐ ┌───┐ ┌───┐ │              │  ┌──────┐  ┌──────┐         │
        │  │ W │ │ W │ │ W │ │              │  │ PA-A │  │ PA-B │         │
        │  └───┘ └───┘ └───┘ │              │  └──┬───┘  └──┬───┘         │
        │                     │              │     │         │             │
        │  Shared workspace   │              │     ▼         ▼             │
        │  PVC (RWX)          │              │  Dedicated VMs (optional)   │
        └─────────────────────┘              └─────────────────────────────┘
                │                                          │
                │  REST: orchestrator pushes jobs           │  WebSocket: user ↔ persistent agent
                │  via HTTP POST /job/start                │  via orchestrator proxy
```

| Aspect | Current | Target |
|--------|---------|--------|
| Deployment | Static `replicas: 2` | Worker pool (dynamic) + persistent agent pods (on-demand) |
| Interaction | Fire-and-forget jobs only | Jobs (worker) + interactive sessions (persistent) |
| Scaling | Manual replica count | Orchestrator-driven: queue depth for workers, user demand for persistent |
| State | Global mutable state in `app.py` | Worker: globals (unchanged). Persistent: session-scoped |
| Graph | Single `graph.py` (phase alternation) | `worker_graph.py` (renamed) + `persistent_graph.py` (new) |
| User sessions | None | Persistent agent sessions with WebSocket, persistence, fork, archive |

## Industry Context

All major CLI coding agents converge on the same core pattern. The differences are in execution environment and UI surface, not in the fundamental loop.

| Agent | Loop | Tools | Permission Model | Context | Session |
|-------|------|-------|------------------|---------|---------|
| **Claude Code** | `while(tool_call)` — model decides everything, no classifiers/DAG/planner | 11+ native (Bash, Read, Edit, Write, Grep, Glob, Agent, TodoWrite, NotebookEdit, WebFetch, WebSearch) + MCP | **Mode-based**: Default (ask all), Auto-accept edits, Plan (read-only). Shift+Tab cycles. Per-command allowlists in settings. | 200K window. Auto-compact at ~95%. CLAUDE.md for persistent rules (transient-injected, survives compaction). Subagents get isolated context (depth-1). | Local JSONL. Resume (`--continue`), fork (`--fork-session`). Independent — no cross-session history. |
| **Codex CLI** | Same ReAct loop. Rust workspace decouples agent from UI surfaces. | Shell, file ops, web search, MCP | **Mode-based**: `auto` (ask for out-of-scope), `readOnly`, `fullAccess`. Switch mid-session via `/permissions`. | Automatic compaction via `auto_compact_limit`. Encrypted `/responses/compact` API returns condensed model state without full message history. Static prompt prefix for cache hits. | **Item/Turn/Thread** protocol. Threads are durable, support resume/fork/archive. JSONL persistence. |
| **Gemini CLI** | Event-driven scheduler. Agent skills for extensibility. | File ops, grep, glob, browser agent, MCP, custom agent skills | Mode-based with `/plan` and `/settings`. | 1-2M token windows. `/compress` for manual compaction, configurable auto-compress threshold. Sub-agents for context isolation. No embeddings for code search — agentic grep/find/read instead. | Session-based, skills extensible. `/chat save` and `/chat resume` for conversation branching. |
| **Cursor** | ReAct with Composer orchestrator. MoE model routes by complexity. | 10+ (codebase search, file read/write, edit, terminal) | Auto mode — agent runs autonomously, user reviews diffs before apply. Background agents run in isolated VMs. | Compaction retains "stable signals" (test names, error types, stack frames). Deduplicates snippets. | IDE-scoped. Background agents work on branches, create PRs. |
| **Devin** | Multi-model pipeline: Planner → Coder → Critic. | Full dev environment (browser, terminal, editor) | Conversational — user directs via Slack/web/Linear. Status dot: green (working), orange (waiting). | Full VM with persistent state. | Persistent sessions across Slack/web/CLI/API. |

### Key Patterns

**1. The loop is just `while(tool_call)`.** Claude Code proved that a simple ReAct loop with no orchestration layers outperforms complex DAG/planner systems. The model decides everything. No classifiers, no intent routing, no forced phases. The API response `stop_reason` drives the loop: `tool_use` means continue, `end_turn` means return control to the user.

**2. Permission modes, not per-tool approval.** Every production agent uses **mode-based** control (ask-all / auto-accept / read-only), not per-tool risk annotations. The user sets a mode that governs all tools.

**3. No plan mode for interactive agents.** Claude Code and Gemini restrict to read-only tools in plan mode. Codex has no separate plan mode — the user steers mid-turn. Our persistent agent takes a different approach: instead of a plan mode, the agent collaborates with the user in conversation to build a plan, then delegates execution to a worker via MCP tools. This leverages the full phase alternation system without running it in-process.

**4. Steering / interrupts are first-class.** Codex has `turn/steer` to inject input into an active turn. Claude Code lets you type and press Enter to interrupt. The user can redirect the agent mid-execution without waiting for it to finish.

**5. Subagents for context isolation.** Claude Code spawns isolated subagents (depth-1, no access to parent context, only summaries return). We achieve the same via job delegation through MCP tools — strictly more powerful since worker jobs get full workspaces, phase alternation, and their own expert configs.

**6. Sessions are independent and durable.** No cross-session conversation history. Persistent state lives in workspace files (workspace.md / CLAUDE.md), not in the conversation. Sessions can be resumed, forked, archived. State survives reconnection via checkpointing.

**7. Context compaction is essential.** All agents compact. The pattern: summarize history, discard verbose tool outputs, preserve key signals (file paths, error messages, decisions). We already have this via `AuxiliaryLLM`'s `SummarizeTask`.

## Agent Modes

### Comparison

| Dimension | Persistent | Worker |
|-----------|------------|--------|
| **Purpose** | Interactive sessions — user collaboration, research, pair-programming | Autonomous job execution — document processing, research, coding |
| **Lifecycle** | One per user, runs 24/7 (idle when no messages), archivable | Pool member, starts at deploy time, cycles through jobs indefinitely |
| **Execution loop** | `while(tool_call)` — model decides everything, no phases | Phase alternation graph — strategic/tactical cycles with todos |
| **Transport** | WebSocket (bidirectional, real-time streaming) | REST (orchestrator pushes jobs via HTTP POST) |
| **User interaction** | Every turn + mid-turn steering + approval requests | None during execution (freeze/feedback at phase boundaries) |
| **Job delegation** | Has MCP tools to create/monitor/manage worker jobs | Executes jobs assigned by orchestrator |
| **Provisioning** | Orchestrator creates pod + optional VM per session | Static pool, orchestrator adjusts replica count |
| **State management** | Session-scoped (`persistent_app.py`) | Global singleton (`app.py`, unchanged) |
| **Scaling trigger** | Admin creates per user (test), user self-service (future) | Pending job queue depth |
| **Teardown** | Explicit archival by user or admin | Never (pool member, may be drained on scale-down) |
| **Startup** | `--mode persistent --thread-id {id}` | `--mode worker` (default) |
| **VM** | Dedicated (lives with session) | Optional (per job config) |

### Persistent

The persistent agent is a permanent interactive agent — the cloud replacement for running Claude Code locally. It functions like a CLI coding agent: the user sends a prompt, the agent decides what to do (answer directly, run a web search, write code, execute shell commands on its VM, etc.), does it, and responds. There is no special "reply" or "ask_user" tool — the conversation itself is the interaction surface. The agent speaks by returning text, and listens by waiting for the next user message.

A user opens a session in the cockpit, the orchestrator provisions a persistent agent pod (and optionally a VM), and the user collaborates with the agent in real-time via WebSocket. For the initial test deployment, one persistent agent is created per user and runs 24/7 — idle until the user sends a message.

The persistent agent does not execute autonomous jobs itself. Instead, it has access to the orchestrator's MCP tools. When the user and agent agree on a plan for heavier work, the agent creates a job on the orchestrator, which a worker picks up. The agent monitors progress and relays results back to the user. This separation keeps the persistent agent responsive and leverages the existing job infrastructure.

The interactive loop, WebSocket protocol, permission modes, and session lifecycle are all covered below.

### Worker

The worker is the existing autonomous agent, refined with dynamic pool scaling. The code changes are minimal: rename `graph.py` to `worker_graph.py`, add a `--mode` flag, and let the orchestrator manage the pool size.

Workers continue to use the global mutable state model in `src/api/app.py` — one job at a time per pod, cooperative stop mechanism, heartbeat loop. This works perfectly for the pool model and requires no refactoring.

## Shared Infrastructure

Both modes share the same container image and >95% of the codebase. The execution loop is the only structural difference.

| Component | Module | Role |
|-----------|--------|------|
| ToolContext | `src/tools/context.py` | Dependency injection for all tool factories |
| load_tools() | `src/tools/__init__.py` | Dynamic tool loading from YAML config |
| Tool registry | `src/tools/registry.py` | Tool metadata, phase filtering |
| ContextManager | `src/core/context.py` | Token counting, 3-layer compaction |
| WorkspaceManager | `src/core/workspace.py` | File I/O, git versioning, local/remote backends |
| Transient injection | `src/core/workspace_injection.py` | workspace.md, memories, knowledge injection |
| ShellManager | `src/tools/coding/shell_manager.py` | tmux-backed persistent shells |
| AuxiliaryLLM | `src/llm/auxiliary.py` | Summarization, memory extraction |
| create_llm() | `src/core/loader.py` | LLM creation, provider routing |
| KeyRing | `src/llm/key_ring.py` | API key rotation with cooldown |
| Config system | `config/`, `src/core/loader.py` | YAML configs, matrix resolvers, `$extends` |
| CitationEngine | External package | Citation management |
| RecallStore | `src/services/recall_store.py` | Memory Light retrieval (pgvector) |
| VM backends | `src/core/backends/` | LocalBackend, RemoteBackend (SSH/SFTP) |
| UniversalAgent | `src/agent.py` | Setup: connections, tools, workspace, LLMs |

The persistent agent's `while(tool_call)` loop calls the same `create_llm()`, loads tools via the same `load_tools()`, manages context via the same `ContextManager`, and injects workspace.md via the same transient injection pattern. The difference is that the persistent agent's loop waits for user input between turns, while the worker's graph drives execution via todos and phase transitions.

## Persistent Agent Design

### Lifecycle

```
Admin creates persistent agent for user (or user self-service in future)
    │
    ▼
POST /api/persistent  (orchestrator)
    │
    ├── Create thread record in DB (status: 'creating')
    ├── Create persistent agent pod via PersistentAgentProvisioner
    │   └── K8s Pod: srw-persistent-{short_id}, --mode persistent --thread-id {id}
    ├── Optionally create VM via VMProvisioner
    │
    ▼
Persistent agent pod boots
    │
    ├── Loads config (expert config from thread record)
    ├── Initializes: LLMs, tools, DB connections, MCP client (→ srw-mcp:8055)
    ├── Registers with orchestrator (agent_mode: 'persistent', thread_id: {id})
    │
    ▼
Persistent agent is running (24/7, idle until user connects)
    │
    ├── Cockpit opens WebSocket via orchestrator proxy
    │   wss://api.superhuman-remote-worker.com/ws/persistent/{thread_id}
    │       ↕  orchestrator proxies to  ↕  ws://persistent-pod:8001/ws
    │
    ▼
Interactive loop (while True)
    │
    ├── Wait for user message
    ├── Agent decides what to do (answer, search, code, shell, etc.)
    ├── LLM call with tools (stream tokens to client)
    ├── Execute tool calls (with permission checks per mode)
    ├── Check for steering input mid-turn
    ├── Context compaction when needed
    ├── Repeat until model has no more tool calls
    ├── Respond to user, wait for next message
    │
    │   [User can at any time:]
    │   ├── Switch permission mode (/supervised, /auto, /autonomous)
    │   ├── Delegate work to workers via MCP tools
    │   ├── Steer the agent mid-turn
    │   ├── Disconnect (agent stays alive, idle until reconnect)
    │
    ▼
Session ends (explicit archival by user or admin)
    │
    ├── Push workspace to Gitea (if git versioning enabled)
    ├── Extract memories → RecallStore
    ├── Thread status → 'archived'
    ├── Delete persistent agent pod
    ├── Delete VM (if provisioned)
    │
    ▼
Done
```

### Interactive Loop

The persistent agent runs a `while(tool_call)` loop — no graph nodes, no phase alternation, no todos. The model decides everything. Implemented in `src/persistent_graph.py`.

The key difference from the worker's graph: the persistent agent waits for user input between turns. A "turn" is one user message plus all the agent work that follows (tool calls, responses) until the model has nothing left to do.

```python
async def interactive_loop(session, llm, tools, user_queue):
    while True:
        # Wait for user message (or steering input if mid-turn)
        user_msg = await user_queue.get()
        session.add_message(HumanMessage(user_msg))

        # Turn: agent works until it has nothing left to do
        while True:
            # Inject transient context (workspace.md, memories, knowledge)
            messages = build_messages(session)

            # LLM call with tools
            response = await llm.ainvoke(messages)
            session.add_message(response)

            # Stream response tokens to client
            yield TurnEvent("agent_message", response.content)

            # No tool calls? Turn is done — wait for next user message
            if not response.tool_calls:
                break

            # Execute tool calls (with permission checks)
            for tool_call in response.tool_calls:
                if needs_approval(tool_call, session.mode):
                    yield TurnEvent("approval_request", tool_call)
                    decision = await wait_for_approval(user_queue)
                    if not decision.approved:
                        session.add_tool_result(tool_call.id, "User denied this action")
                        continue

                result = await execute_tool(tool_call, tools)
                session.add_tool_result(tool_call.id, result)
                yield TurnEvent("tool_result", tool_call, result)

            # Check for steering input (user message arrived mid-turn)
            if steering := user_queue.get_nowait():
                session.add_message(HumanMessage(steering))
                # Continue the loop — LLM sees the steering message on next iteration

        yield TurnEvent("turn_complete")
```

**This is the entire execution model.** No graph nodes, no phase alternation, no routing functions. The LLM decides when to use tools and when to stop. The user's permission mode gates which tools run freely vs. need approval.

#### How This Differs from the Worker Graph

| Aspect | Worker (`worker_graph.py`) | Persistent (`persistent_graph.py`) |
|--------|--------------------|--------------------|
| **Structure** | LangGraph state machine with 8+ nodes, conditional edges | `while(tool_call)` inner loop, `while True` outer loop |
| **Loop driver** | Todos — execute until all complete, then phase transition | Conversation — execute per user turn, model decides when done |
| **Phase alternation** | Forced (strategic ↔ tactical), separate LLMs per phase | None. Single LLM, no phases. |
| **Tool filtering** | Phase-based (`filter_tools_by_phase`) | Mode-based (all tools available, permission mode gates execution) |
| **User input** | Only at resume (with feedback) | Every turn, plus steering mid-turn |
| **Planning** | Built-in (strategic phases, plan.md, todos) | Delegates to workers via MCP tools for heavy planning/execution |
| **Completion signal** | `job_complete` tool → `check_goal` node | Natural conversation end, or `/done` command |
| **State management** | `UniversalAgentState` with 30+ fields | Lighter state: messages, mode, workspace path, session metadata |
| **Context compaction** | Same | Same (`AuxiliaryLLM` → `SummarizeTask`) |

#### Interaction Modes (Within a Session)

The session flows naturally between these modes based on the conversation and permission mode:

**Chat (Supervised or Auto-Accept):** Turn-based conversation. User sends a message, agent responds (using tools as needed), then waits. No todo system, no phases — just conversation.

**Autonomous:** User gives a directive and the agent loops without waiting. The model decides when to stop (task complete, stuck, needs decision). User can steer (`turn/steer`) or interrupt (`turn/interrupt`) at any time. Safety cap: `max_autonomous_iterations` (default 50) prevents runaway loops.

**Job delegation:** For heavy or long-running work, the agent collaborates with the user to define the task, then creates a worker job via MCP tools. The agent monitors progress and relays results. The user can disconnect — the worker continues independently. This replaces plan mode: instead of running the phase alternation system in-process, the persistent agent delegates to workers that already have it.

### Session State

The persistent agent uses `src/api/persistent_app.py` — a separate FastAPI application with session-scoped state instead of globals.

```python
@dataclass
class PersistentSession:
    """State for one persistent agent session. Scoped to the WebSocket connection."""
    thread_id: str
    project_id: Optional[str]
    user_id: str
    mode: str                          # supervised | auto_accept | autonomous
    workspace: WorkspaceManager
    tool_context: ToolContext
    context_manager: ContextManager
    shell_manager: Optional[ShellManager]
    messages: List[BaseMessage]        # Conversation history
    turn_count: int
    created_at: datetime
    last_activity: datetime
    config: AgentConfig
    llm: BaseChatModel                 # Single LLM (no phase-specific split)
    tools: List[BaseTool]              # Loaded once, filtered by permission mode
```

There is no `_current_job_id`, no `_stop_requested`, no cooperative stop mechanism. The agent's lifecycle is the session's lifecycle. Pause and resume are handled by WebSocket disconnect/reconnect and checkpoint restoration.

### MCP Tools for Job Delegation

Instead of plan mode (which would require running the phase alternation graph in-process), the persistent agent delegates heavy work to workers via the orchestrator's MCP tools. The agent connects to the MCP server (`srw-mcp` on port 8055) as an MCP client — the same server that Claude Code connects to via `.mcp.json`. This reuses the existing tool definitions from `orchestrator/mcp/` without duplication.

| Tool | Purpose |
|------|---------|
| `create_job` | Create a worker job with description, config, instructions |
| `list_jobs` | List jobs (filter by project, status) |
| `get_job` | Get job details, status, progress |
| `get_job_log` | Stream job execution log |
| `get_job_summary` | Get job completion summary |
| `get_workspace_file` | Read files from a job's workspace |
| `get_workspace_overview` | List files in a job's workspace |
| `approve_job` | Approve a frozen job |
| `resume_job_with_feedback` | Resume a frozen job with feedback |
| `cancel_job` | Cancel a running job |
| `pause_job` | Pause a running job |

**Workflow example:**

```
User: "Write a comprehensive test suite for the citation engine"
Agent: [discusses approach with user, agrees on scope and structure]
Agent: [calls create_job with description, config=developer, instructions=...]
       "I've created job abc123 to write the test suite. A worker will pick it up.
        I'll monitor progress — want me to check in periodically?"
User: "Yes, check every few minutes"
Agent: [periodically calls get_job_progress, relays updates]
       "The worker is on phase 2/4 — it's written unit tests for the parser
        and is now working on integration tests for the database layer."
User: "Tell it to focus on edge cases for Unicode handling"
Agent: [calls pause_job, then resume_job_with_feedback with the user's note]
       "Done, I've sent that feedback. The worker will incorporate it."
```

This is strictly more powerful than an in-process plan mode: the user can create multiple jobs, monitor them in parallel, and steer them independently — all through natural conversation with the persistent agent.

### Protocol: Items, Turns, Threads

Adopting Codex's three-level abstraction for agent ↔ client communication:

**Item** — the atomic unit of input or output. Each item has a lifecycle: `started` → optional deltas → `completed`.

| Item Type | Direction | Description |
|-----------|-----------|-------------|
| `user_message` | Client → Agent | User input (text) |
| `agent_message` | Agent → Client | Streamed text response |
| `tool_call` | Agent → Client | Tool invocation (name, args) |
| `tool_result` | Agent → Client | Tool execution output |
| `approval_request` | Agent → Client | Permission request for gated tool |
| `approval_response` | Client → Agent | User's decision (allow/deny/allow-session) |
| `file_change` | Agent → Client | File write/edit with diff |
| `command_execution` | Agent → Client | Shell command + output |
| `context_compaction` | Agent → Client | Summary of compacted history |
| `error` | Agent → Client | Error message |

**Turn** — groups items from one user request + the agent work that follows. A turn starts when the user sends a message and ends when the agent has no more tool calls to make (or the user steers into a new direction).

```json
{
  "id": "turn_001",
  "status": "in_progress | completed | interrupted",
  "items": [
    {"type": "user_message", "content": "Fix the login bug"},
    {"type": "tool_call", "name": "grep", "args": {"pattern": "login", "path": "src/"}},
    {"type": "tool_result", "name": "grep", "output": "..."},
    {"type": "agent_message", "content": "Found the issue in src/auth.py..."},
    {"type": "tool_call", "name": "edit_file", "args": {"path": "src/auth.py", ...}},
    {"type": "file_change", "path": "src/auth.py", "diff": "..."},
    {"type": "agent_message", "content": "Fixed. The session token wasn't being refreshed."}
  ]
}
```

**Thread** — the durable session container. Supports creation, resumption, forking, and archival.

```json
{
  "id": "thread_abc123",
  "project_id": "proj_456",
  "status": "active | idle | autonomous | archived",
  "mode": "supervised | auto_accept | autonomous",
  "workspace_path": "/workspace/job_abc123",
  "created_at": "2026-03-13T10:00:00Z",
  "updated_at": "2026-03-13T14:30:00Z",
  "turns": ["turn_001", "turn_002", ...]
}
```

Thread operations: **Create** (initialize workspace from project main), **Resume** (reconnect, restore from checkpoint), **Fork** (new thread ID, preserves conversation up to fork point), **Archive** (push workspace to git, extract memories, mark as historical), **Compact** (trigger context compaction manually).

### WebSocket Transport

The cockpit connects via the orchestrator's WebSocket proxy:

```
Cockpit ──WebSocket──→ Orchestrator ──WebSocket──→ Persistent Agent Pod
         (wss://api.../ws/persistent/{id})    (ws://pod:8001/ws)
```

The orchestrator acts as a transparent proxy — it forwards frames in both directions without interpretation. This avoids per-agent ingress rules and works with the existing Cloudflare Tunnel setup (the API subdomain already has SSE buffering disabled for streaming).

**Why WebSocket, not SSE?** The builder uses SSE (server → client only) because the user sends discrete HTTP requests. Interactive mode needs bidirectional streaming: steering input, approval responses, and interrupts arrive while the agent is mid-turn. WebSocket gives us this without polling.

**Client → Agent:**

```json
{"method": "turn/start", "params": {"input": "Fix the login bug"}}
{"method": "turn/steer", "params": {"input": "Actually focus on session handling"}}
{"method": "turn/interrupt"}
{"method": "approval/respond", "params": {"item_id": "...", "decision": "allow"}}
{"method": "mode/set", "params": {"mode": "autonomous"}}
{"method": "thread/compact"}
{"method": "thread/archive"}
```

**Agent → Client:**

```json
{"method": "turn/started", "params": {"turn_id": "turn_001"}}
{"method": "item/started", "params": {"item": {"type": "tool_call", "name": "grep", ...}}}
{"method": "item/delta", "params": {"item_id": "...", "text": "chunk..."}}
{"method": "item/completed", "params": {"item": {...}}}
{"method": "approval/request", "params": {"item_id": "...", "tool": "run_command", "args": {...}}}
{"method": "turn/completed", "params": {"turn_id": "turn_001"}}
{"method": "thread/status", "params": {"status": "idle"}}
{"method": "context/compacted", "params": {"summary": "..."}}
```

**Reconnection:** WebSocket drops happen. Session state lives in the LangGraph checkpointer + workspace files. On reconnect: client sends `thread/resume` with thread ID → agent loads checkpoint, restores state → sends recent items (last N turns) for UI display → user continues where they left off.

Behavior on disconnect is configurable per session:
- **Pause** (default): Agent stops, waits for reconnect
- **Continue**: Agent keeps working autonomously, user catches up on reconnect

### Permission Modes

Three modes control tool execution (following the Claude Code / Codex pattern — mode-based, not per-tool):

| Mode | Behavior | Analogy |
|------|----------|---------|
| **Supervised** (default) | Agent asks before file writes and shell commands. Reads, searches, and web lookups run freely. | Claude Code's default mode |
| **Auto-accept** | File writes execute without asking. Shell commands still require approval. | Claude Code's auto-accept edits |
| **Autonomous** | Everything executes without asking. Agent streams progress, doesn't wait. | Codex `fullAccess` |

The user switches modes via slash commands (`/supervised`, `/auto`, `/autonomous`) or a mode selector in the cockpit. Mode applies to the entire session until switched.

**Per-command allowlists** (like Claude Code's `.claude/settings.json`): For frequently-approved commands (e.g., `npm test`, `pytest`, `git status`), the user can add patterns to an allowlist. These skip approval even in supervised mode. Stored in the project config or session config.

### VM Integration

Persistent agents can optionally get a dedicated VM for shell isolation and heavier workloads:

- VM provisioned alongside the agent pod via `VMProvisioner` (same mechanism as current job VMs)
- Workspace backend set to `remote` (RemoteBackend with SSH/SFTP)
- VM persists across WebSocket disconnects — it's tied to the session, not the connection
- Idle timeout: configurable, triggers cleanup on prolonged inactivity
- Agent pod + VM are a unit: both created together, both destroyed on archival

For lightweight sessions (quick questions, instruction drafting), the persistent agent can run without a VM using a local workspace.

### Session Persistence

Sessions survive disconnects:

- **Conversation state:** LangGraph checkpointer (SQLite or PostgreSQL)
- **Workspace state:** Files on disk (local or remote VM)
- **Persistent context:** workspace.md + compaction summaries survive reconnection

**Resume flow:**
1. User reopens session in cockpit
2. Cockpit sends `thread/resume` via WebSocket
3. Agent loads checkpoint, restores messages and state
4. Sends recent items (last N turns) for cockpit display
5. User continues where they left off

**Disconnect behavior** (configurable per session):
- `pause` (default): Agent stops, waits for reconnect
- `continue`: Agent keeps working autonomously, user catches up on reconnect

**Forking:** Create a new thread from an existing one. Preserves conversation history up to the fork point. Both threads continue independently. Use case: "let me try a different approach without losing this one."

**Archival:** User types `/done` or session is explicitly archived:
1. Push workspace changes to Gitea (job branch → merge to main)
2. Extract memories via AuxiliaryLLM → RecallStore
3. Thread status → `archived`
4. Delete persistent agent pod + VM
5. Thread becomes historical record in project timeline

### Workspace Files in Interactive Mode

| File | Interactive Role |
|------|-----------------|
| `workspace.md` | Same — persistent memory, transient-injected every turn |
| `plan.md` | Optional — user doesn't have to use it, agent may create it during collaboration |
| `todos.yaml` | Not used (no phase alternation) |
| `archive/` | Populated on archival |
| `output/` | Deliverables, same as today |

### Configuration

Interactive mode uses the same config system:

```yaml
# yaml-language-server: $schema=schema.json
$extends: defaults

agent_id: interactive
display_name: Interactive Assistant

interactive:
  enabled: true
  default_mode: supervised         # supervised | auto_accept | autonomous
  max_autonomous_iterations: 50    # safety cap for autonomous stretches
  idle_timeout: 3600               # seconds before session auto-pauses
  disconnect_behavior: pause       # pause | continue
  welcome_message: true            # send persona-based greeting on connect
  command_allowlist:               # commands that skip approval in supervised mode
    - "pytest*"
    - "npm test"
    - "git status"
    - "git log*"
    - "git diff*"

# Same tool config as any agent
tools:
  workspace:
    - read_file
    - write_file
    - list_directory
  coding:
    - run_command
    - shell_read
  research:
    - web_search
    - browse_website
  citation:
    - cite_web
    - cite_document
  # ... all tool categories available
```

Expert configs can customize the experience. A "developer" expert might default to `auto_accept` with a liberal allowlist. A "researcher" expert might default to `supervised`.

## Worker Agent Design

### What Stays Unchanged

The entire worker codebase remains as-is:

- `src/api/app.py` — globals (`_agent`, `_current_job_id`, `_stop_requested`), lifespan, endpoints
- `src/api/orchestrator_client.py` — registration, heartbeat, job reporting
- Phase alternation graph — renamed from `graph.py` to `worker_graph.py`, import path updated
- `/job/start`, `/job/cancel`, `/job/pause`, `/job/resume` endpoints
- `_process_orchestrator_job()` background task with cooperative stop
- Verification trigger, critic verdict handling
- All graph nodes: execute, check_todos, archive_phase, handle_transition, check_goal

The only code change to the worker path is the graph rename and a `--mode` flag check at startup.

### Graph Rename

```
src/graph.py           →  src/worker_graph.py
src/agent.py imports   →  from .worker_graph import build_phase_alternation_graph
```

No logic changes. All existing tests continue to work with the renamed import.

### Dynamic Pool Scaling

The orchestrator gains a new background task: `worker_pool_manager()`, running alongside `auto_assign_dispatcher()`.

```
Worker Pool Manager (runs every 30s)
    │
    ├── Count pending jobs: status IN ('created', 'paused'), assigned_agent_id IS NULL
    ├── Count worker agents: agent_mode = 'worker', status IN ('ready', 'working')
    │
    ├── IF pending > (idle_workers × scale_up_threshold):
    │   └── Scale up: PATCH Deployment replicas += scale_increment
    │       (capped at WORKER_POOL_MAX)
    │
    ├── IF idle workers > idle_threshold AND no scaling recently:
    │   └── Scale down: mark excess workers as 'draining'
    │       → draining workers finish current job, don't accept new
    │       → once idle, PATCH Deployment replicas -= drained_count
    │
    └── Respect cooldown between scaling operations
```

The orchestrator patches the worker Deployment's `spec.replicas` directly via the K8s API. This is simpler than setting up an HPA with custom metrics and doesn't require Prometheus.

### Scaling Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKER_POOL_MIN` | 1 | Minimum worker replicas (never scale below) |
| `WORKER_POOL_MAX` | 10 | Maximum worker replicas (never scale above) |
| `WORKER_SCALE_UP_RATIO` | 2 | Scale up when pending jobs > idle workers × ratio |
| `WORKER_SCALE_DOWN_IDLE_MINUTES` | 15 | Minutes of idleness before considering scale-down |
| `WORKER_SCALE_COOLDOWN_SECONDS` | 120 | Minimum time between scaling operations |
| `WORKER_SCALE_INCREMENT` | 2 | Number of replicas to add per scale-up event |
| `WORKER_DEPLOYMENT_NAME` | `srw-agent` | K8s Deployment name to patch |
| `WORKER_DEPLOYMENT_NAMESPACE` | `superhuman-remote-worker` | K8s namespace |

### Graceful Drain

When the pool manager decides to scale down, it must not kill agents mid-job:

1. Pool manager marks N excess agents as `draining` (via metadata flag or new status)
2. Dispatcher skips draining agents when matching pending jobs
3. Draining agent finishes its current job normally
4. On completion, draining agent reports `completed` status (not `ready`)
5. Pool manager detects the drained agent, reduces Deployment replica count
6. K8s terminates the now-idle pod

The cooperative stop mechanism (`_stop_requested` in `app.py`) could be reused: the orchestrator sends a drain signal via the existing `/job/pause` endpoint pattern, but with a "drain" reason that tells the agent "finish your job but don't go back to ready."

## Orchestrator Changes

### Persistent Agent Provisioner

New service: `orchestrator/services/persistent_provisioner.py`, following the `VMProvisioner` pattern.

```python
class PersistentAgentProvisioner:
    """Provisions persistent agent pods on demand.

    Follows the VMProvisioner pattern: dual-backend (NATS for cross-cluster,
    direct K8s for same-cluster), graceful degradation when neither available.
    """

    async def create_agent(
        self,
        thread_id: str,
        user_id: str,
        expert_config: str = "defaults",
        project_id: Optional[str] = None,
        with_vm: bool = False,
        vm_image: Optional[str] = None,
        cpu_cores: int = 2,
        memory: str = "4Gi",
    ) -> bool: ...

    async def delete_agent(self, thread_id: str) -> bool: ...

    async def get_agent_status(self, thread_id: str) -> Optional[dict]: ...

    @property
    def is_available(self) -> bool: ...
```

Persistent agent pods are standalone K8s Pods (not Deployment replicas). Each is created dynamically with a unique name (`srw-persistent-{short_id}`) and labels for discovery.

When a persistent agent also needs a VM, the provisioner coordinates both: create the VM first (via existing `VMProvisioner`), wait for it to be ready, then create the agent pod with the VM's SSH details injected as environment variables.

### Worker Pool Manager

New background task in `orchestrator/main.py`, started alongside `auto_assign_dispatcher()` and `stale_agent_detector()`:

```python
async def worker_pool_manager(shutdown_event: asyncio.Event) -> None:
    """Manage worker pool size based on job queue depth.

    Runs every 30 seconds. Scales the worker Deployment up or down
    based on pending job count vs available worker count.
    """
```

Requires K8s API access (via `kubernetes` Python client or direct HTTP to the API server). The orchestrator pod already has a ServiceAccount — may need additional RBAC for patching Deployments.

### WebSocket Proxy

The orchestrator proxies WebSocket connections from the cockpit to persistent agent pods:

```
GET /ws/persistent/{thread_id}  →  Upgrade to WebSocket
    │
    ├── Look up thread → get agent → get pod_ip:pod_port
    ├── Open WebSocket to ws://pod_ip:pod_port/ws
    ├── Bidirectional frame forwarding (transparent proxy)
    ├── On disconnect: close both sides
    └── On agent pod not found: return 404 before upgrade
```

Implementation: FastAPI's WebSocket support + `websockets` library for the upstream connection. The proxy adds minimal latency (one hop within the cluster network).

### New API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/persistent` | Create persistent agent session (provisions pod + optional VM) |
| `GET` | `/api/persistent` | List active sessions (filter by user, project, status) |
| `GET` | `/api/persistent/{thread_id}` | Agent status, connectivity info, session metadata |
| `DELETE` | `/api/persistent/{thread_id}` | Archive session, tear down agent + VM |
| `POST` | `/api/persistent/{thread_id}/fork` | Fork session (new thread, preserves history) |
| `GET` | `/ws/persistent/{thread_id}` | WebSocket proxy to agent pod |
| `GET` | `/api/workers/pool` | Pool status (current/min/max replicas, scaling state) |
| `PATCH` | `/api/workers/pool` | Manual pool size override (admin) |

### Dispatch Filter

The existing `_try_dispatch_pending_jobs()` dispatcher only matches workers. Persistent agents never enter the dispatch pool — they are created for specific threads, not for generic job assignment.

The filter is simple: `get_available_agents()` already queries by status. Adding `AND agent_mode = 'worker'` (or equivalently, persistent agents register with `agent_mode = 'persistent'` which the existing query naturally excludes since persistent agents never report `status = 'ready'` in the dispatch sense).

## Database Schema

### Agents Table Changes

```sql
-- Migration: Add agent_mode to agents table
DO $$ BEGIN
    ALTER TABLE agents ADD COLUMN agent_mode VARCHAR(20) DEFAULT 'worker';
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Migration: Add thread_id to agents table (persistent agents serve one thread)
DO $$ BEGIN
    ALTER TABLE agents ADD COLUMN thread_id UUID;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
CREATE INDEX IF NOT EXISTS idx_agents_thread_id ON agents(thread_id);

-- Migration: Update status constraint to include 'draining'
DO $$ BEGIN
    ALTER TABLE agents DROP CONSTRAINT IF EXISTS valid_agent_status;
    ALTER TABLE agents ADD CONSTRAINT valid_agent_status
        CHECK (status IN ('booting', 'ready', 'working', 'completed',
                          'failed', 'offline', 'draining'));
END $$;
```

### Threads Table

```sql
-- ============================================================================
-- THREADS TABLE
-- Tracks persistent agent sessions (parallel to jobs table for workers)
-- ============================================================================

CREATE TABLE IF NOT EXISTS threads (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Ownership
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id UUID REFERENCES projects(id) ON DELETE SET NULL,

    -- Agent assignment
    agent_id UUID REFERENCES agents(id) ON DELETE SET NULL,

    -- Session state
    status VARCHAR(20) NOT NULL DEFAULT 'creating',
    mode VARCHAR(20) NOT NULL DEFAULT 'supervised',

    -- Configuration
    expert_config VARCHAR(100) DEFAULT 'defaults',
    config_override JSONB DEFAULT NULL,

    -- Workspace
    workspace_path TEXT,

    -- VM (if provisioned)
    vm_context JSONB DEFAULT NULL,

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    archived_at TIMESTAMP WITH TIME ZONE,

    -- Resource tracking
    total_tokens_used INTEGER DEFAULT 0,
    total_requests INTEGER DEFAULT 0,

    -- Extensible metadata
    metadata JSONB DEFAULT '{}',

    CONSTRAINT valid_thread_status
        CHECK (status IN ('creating', 'active', 'idle', 'autonomous',
                          'disconnected', 'archived', 'failed')),
    CONSTRAINT valid_thread_mode
        CHECK (mode IN ('supervised', 'auto_accept', 'autonomous'))
);

CREATE INDEX IF NOT EXISTS idx_threads_user_id ON threads(user_id);
CREATE INDEX IF NOT EXISTS idx_threads_project_id ON threads(project_id);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);
CREATE INDEX IF NOT EXISTS idx_threads_agent_id ON threads(agent_id);
```

### Thread Status Transitions

```
creating ──→ active ──→ idle ──→ active       (user reconnects)
                │         │
                │         └──→ archived        (idle timeout)
                │
                ├──→ autonomous ──→ active     (user sets /autonomous, then steers)
                │         │
                │         └──→ idle            (autonomous work completes)
                │
                ├──→ disconnected ──→ active   (user reconnects)
                │         │
                │         └──→ archived        (disconnect timeout)
                │
                └──→ archived                  (user archives /done)

creating ──→ failed                            (pod creation failed)
```

| Status | Meaning |
|--------|---------|
| `creating` | Agent pod being provisioned, not yet ready |
| `active` | User connected, interactive loop running |
| `idle` | User connected but no active turn (waiting for input) |
| `autonomous` | Agent working autonomously (user set /autonomous mode) |
| `disconnected` | WebSocket dropped, agent paused or continuing per config |
| `archived` | Session ended, workspace pushed, pod deleted |
| `failed` | Agent pod failed to start or crashed unrecoverably |

## Container and Deployment

### Single Image, Mode Flag

The container entrypoint (`agent.py`) accepts a `--mode` flag:

```bash
# Worker (default) — loads app.py, registers for dispatch pool
python agent.py --mode worker --port 8001

# Persistent — loads persistent_app.py, registers for specific thread
python agent.py --mode persistent --thread-id {uuid} --port 8001
```

The mode determines:
- Which FastAPI application to create (`app.py` vs `persistent_app.py`)
- Which graph to import (`worker_graph.py` vs `persistent_graph.py`)
- Registration behavior (worker: dispatch pool. Persistent: specific thread)
- Heartbeat semantics (worker: "ready" means "give me a job". Persistent: "active" means "session alive")

### Worker Deployment

The existing `deployment/21-agent.yaml` becomes the worker pool:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: srw-worker
  namespace: superhuman-remote-worker
spec:
  replicas: 2  # Managed by orchestrator's worker_pool_manager
  selector:
    matchLabels:
      app: srw-worker
  template:
    metadata:
      labels:
        app: srw-worker
        agent-mode: worker
    spec:
      containers:
        - name: worker
          image: ghcr.io/knaeckebrothero/superhuman-remote-worker-agent:latest
          args: ["--mode", "worker", "--port", "8001"]
          # ... same env, resources, probes as current 21-agent.yaml
```

Changes from current manifest:
- Rename from `srw-agent` to `srw-worker`
- Add `agent-mode: worker` label
- Add `--mode worker` to container args
- `replicas` managed by orchestrator (initial value: 2)

### Persistent Agent Pod Template

Persistent agent pods are created dynamically by the provisioner. Template:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: srw-persistent-{short_id}
  namespace: superhuman-remote-worker
  labels:
    app: srw-persistent
    agent-mode: persistent
    thread-id: "{thread_id}"
spec:
  restartPolicy: Never  # Persistent agents don't auto-restart — orchestrator manages lifecycle
  containers:
    - name: persistent
      image: ghcr.io/knaeckebrothero/superhuman-remote-worker-agent:latest
      args: ["--mode", "persistent", "--thread-id", "{thread_id}", "--port", "8001"]
      ports:
        - containerPort: 8001
      env:
        # Same env vars as workers (DB connections, API keys, etc.)
        # Plus persistent-specific:
        - name: AGENT_MODE
          value: "persistent"
        - name: THREAD_ID
          value: "{thread_id}"
      resources:
        requests:
          memory: "1Gi"
          cpu: "500m"
        limits:
          memory: "4Gi"
          cpu: "2000m"
      livenessProbe:
        httpGet:
          path: /health
          port: 8001
        periodSeconds: 30
      readinessProbe:
        httpGet:
          path: /ready
          port: 8001
        periodSeconds: 10
```

Persistent agents get higher resource limits than workers (4Gi vs 2Gi memory) because interactive sessions may involve heavier shell workloads, larger context windows, and concurrent tool execution.

`restartPolicy: Never` — if a persistent agent crashes, the orchestrator detects it via the stale agent detector (no heartbeat for 3 minutes) and marks the thread as `failed`. The user can create a new session and resume from the checkpoint.

### Networking

Persistent agents are reachable only through the orchestrator's WebSocket proxy. No per-agent Service or Ingress needed. The orchestrator looks up the agent's pod IP from the agents table and connects directly within the cluster network.

For local development, persistent agents run as local processes (`python agent.py --mode persistent --thread-id test --port 8002`) and the orchestrator proxies to `localhost:8002`.

## Implementation Phases

> **Implementation note (2026-03-29):** All 6 phases were implemented in a single pass with the following deviations
> from the original plan:
> - **No graph rename.** `src/graph.py` stays as-is. The worker mode is unchanged — persistent mode is purely additive.
> - **No K8s deployment rename.** The existing `srw-agent` Deployment is not renamed to `srw-worker`. The
    `--mode worker` default preserves current behavior.
> - **No worker pool scaling.** The `worker_pool_manager` background task is deferred. Dynamic replica management
    requires RBAC changes and production testing.
> - **Phases 1–4 were collapsed** because the additive approach (no renames, no worker-side changes) made them
    naturally parallel.

### Phase 1: Worker Rename and Pool Foundation

Foundation work — no persistent agent code, no interactive loop. Existing tests must pass.

- [x] ~~Rename `src/graph.py` → `src/worker_graph.py`, update all imports~~ **Skipped** — graph stays as
  `src/graph.py`, no renames needed for additive approach
- [x] Add `--mode` flag to `agent.py` CLI (default: `worker`, only `worker` supported initially) — `agent.py:188-196`
- [x] Add `agent_mode` column to agents table (migration in `schema.sql`) — `orchestrator/database/schema.sql`
- [x] ~~Add `draining` to agent status constraint~~ **Already existed** in schema.sql
- [x] Worker registration includes `agent_mode: 'worker'` in metadata — `src/api/orchestrator_client.py:register()`
  defaults to `agent_mode="worker"`
- [x] `get_available_agents()` filters by `agent_mode = 'worker'` — `orchestrator/database/postgres.py:1701` adds
  `AND COALESCE(agent_mode, 'worker') = 'worker'`
- [ ] ~~Rename K8s Deployment from `srw-agent` to `srw-worker`, add labels~~ **Deferred** — not needed for additive
  approach
- [ ] Worker pool manager background task (orchestrator): monitor queue depth, patch replicas — **Deferred**
- [ ] K8s RBAC: grant orchestrator ServiceAccount permission to patch Deployments — **Deferred**
- [ ] Scaling configuration env vars (`WORKER_POOL_MIN`, `WORKER_POOL_MAX`, etc.) — **Deferred**
- [ ] Graceful drain: `draining` status, dispatcher skips draining agents — **Deferred** (status already in constraint)
- [ ] Update docker-compose.dev.yaml agent service with `--mode worker` — **Deferred** (default is already worker)

### Phase 2: Persistent Agent Provisioner and Thread Schema

Infrastructure for persistent agent sessions — no interactive loop yet, just the provisioning and lifecycle.

- [x] Create `threads` table (migration in `schema.sql`) — columns: id, title, user_id, project_id, agent_id, status,
  permission_mode, config_name, timestamps, metadata
- [x] `orchestrator/services/persistent_provisioner.py` — skeletal provisioner following VMProvisioner pattern,
  graceful no-op when K8s unavailable
- [ ] Persistent agent pod K8s template (Pod spec, env vars, probes) — **Deferred** to production deployment
- [x] Persistent agent REST endpoints on orchestrator (`/api/persistent/threads` CRUD) — `orchestrator/main.py`
- [x] Registration endpoint (agent boots → registers with `agent_mode: 'persistent'`, `thread_id`) — extended
  `AgentRegistration` model + `register_agent()` DB method
- [x] Thread status tracking (created → active → idle → ended lifecycle) — thread CRUD in
  `orchestrator/database/postgres.py`
- [ ] Stale agent detection (reuse `stale_agent_detector`, mark thread as `failed`) — **Deferred**
- [x] Basic `src/api/persistent_app.py` skeleton: health endpoint, registration, no interactive loop yet — **Exceeded
  **: full app with WebSocket + interactive loop implemented
- [ ] Cleanup on thread archival/deletion: delete pod, delete VM — **Deferred** (provisioner is skeletal)

### Phase 3: Persistent Agent Interactive Loop and WebSocket

The core interactive agent implementation.

- [x] `src/persistent_graph.py` — the `while(tool_call)` loop with `run_persistent_loop()` + `_execute_turn()` inner
  loop
- [x] `PersistentSession` dataclass for session-scoped state — `src/api/persistent_session.py` with workspace, tools,
  LLM, context manager, shell manager, memory
- [x] WebSocket endpoint on agent pod (`ws://agent:8001/ws/chat`) — `src/api/persistent_app.py`
- [x] ~~Item/Turn/Thread protocol (JSON over WebSocket)~~ **Simplified**: JSON protocol with `method` field —
  `turn.started`, `token`, `tool.started`, `tool.completed`, `permission.request`, `turn.completed`, `ready`, `error`,
  `greeting`, `mode.changed`
- [x] LLM streaming (token-by-token via `astream()`) — `src/persistent_graph.py:_execute_turn()`
- [x] Tool execution with permission mode enforcement — three modes: supervised (ask for all writes/shell),
  auto_accept (ask for shell only), autonomous (no approval)
- [x] Transient injection: workspace.md — injected as `<workspace_memory>` SystemMessage before each LLM call
- [x] Context compaction (reuse `ContextManager.ensure_within_limits()`)
- [ ] Steering: user injects message mid-turn — **Partial**: interrupt flag exists but mid-turn message injection not
  yet implemented
- [x] Interrupt: user cancels active turn — `check_interrupt()` callback checked before each LLM call
- [ ] Session checkpoint/restore (for reconnection) — **Deferred**: messages are in-memory only, no persistence across
  restarts

**Implementation notes:**

- The loop uses direct `tool.ainvoke()` instead of LangGraph's `ToolNode` — simpler for the non-graph architecture
- Phase-specific tools (`next_phase_todos`, `todo_complete`, `todo_list`, `todo_rewind`, `mark_complete`,
  `job_complete`) are excluded from the tool set
- `PersistentSession.setup()` composes around `UniversalAgent` without modifying it — pulls initialized LLMs, DB
  connections, and config
- Config extended with `InteractiveConfig` dataclass (`src/core/loader.py`) parsed from `interactive:` section in YAML

### Phase 4: Orchestrator WebSocket Proxy

Connect the cockpit to persistent agent pods through the orchestrator.

- [x] WebSocket proxy endpoint: `GET /ws/persistent/{thread_id}` → upgrade → forward to agent pod —
  `orchestrator/main.py`
- [x] Proxy implementation: bidirectional frame forwarding (transparent) — follows existing IDE proxy pattern (
  `/api/ide/{job_id}/proxy/`)
- [ ] Reconnection handling: agent pod alive check, state recovery — **Deferred**
- [ ] Thread status updates from WebSocket events (active ↔ idle ↔ disconnected) — **Deferred**
- [ ] Idle timeout detection: no activity → mark thread `idle` → configurable cleanup — **Deferred** (config key
  exists: `interactive.idle_timeout_minutes`)
- [ ] CORS/auth for WebSocket upgrade (reuse existing auth middleware) — **Deferred**
- [ ] Cloudflare Tunnel: verify WebSocket frames pass through (SSE buffering already disabled) — **Deferred** to
  deployment

### Phase 5: MCP Integration and Session Lifecycle

Give persistent agents the ability to create and manage worker jobs.

- [x] ~~MCP tool access: load orchestrator MCP tools into agent's tool set via MCP client (→ srw-mcp:8055)~~ *
  *Implemented differently**: Created a new `orchestrator` tool category (`src/tools/orchestrator/`) with 8 LangChain
  tools that call the orchestrator REST API directly via httpx. This avoids MCP client protocol overhead and extra
  dependencies while providing the same functionality. Tools: `create_worker_job`, `list_worker_jobs`,
  `get_worker_job`, `get_job_workspace_file`, `approve_worker_job`, `resume_worker_job`, `cancel_worker_job`,
  `pause_worker_job`.
- [x] Agent can call `create_job`, `get_job`, `pause_job`, `resume_job_with_feedback`, etc. — all implemented as
  LangChain tools auto-loaded by `PersistentSession`
- [ ] Job progress monitoring: agent periodically checks job status, streams updates to user — **Deferred** (agent can
  manually call `get_worker_job` but no auto-polling)
- [ ] Session forking: `POST /api/persistent/{thread_id}/fork` → new thread, checkpoint copy — **Deferred**
- [ ] Session archival: git push, memory extraction, thread status update, pod teardown — **Deferred**
- [ ] Disconnect behavior: configurable `pause` vs `continue` per session — **Deferred**
- [ ] Token usage tracking per thread (`total_tokens_used`, `total_requests`) — **Deferred**
- [ ] Thread history in cockpit: list archived sessions, view conversation logs — **Deferred** (Phase 6)

**Implementation notes:**

- The `orchestrator` tool category is registered in `src/tools/registry.py` alongside existing categories.
- `PersistentSession._setup_tools()` always injects orchestrator tools regardless of config — they're fundamental to
  the persistent agent's delegation capability.
- Tools use `ORCHESTRATOR_URL` env var (same as the worker's `orchestrator_client.py`).
- The `orchestrator` list in `config/defaults.yaml` is empty by default (workers don't need them). The persistent
  session injects them programmatically.

### Phase 6: Cockpit UI

Frontend for persistent agent sessions.

- [x] Interactive chat component (WebSocket client) —
  `cockpit/src/app/shared/components/persistent-chat/persistent-chat.component.ts`
- [x] WebSocket connection management (connect, reconnect, disconnect) —
  `cockpit/src/app/core/services/persistent-chat.service.ts` with signals for connection state, messages, streaming
  text, tool calls, permission requests
- [x] Streaming markdown rendering for agent messages — ngx-markdown with live `streamingText()` signal, thinking dots
  animation
- [x] Tool call display with expandable results — inline cards with spinner while running, collapsible `<details>` for
  results
- [x] Approval request UI (approve / deny / ~~allow-for-session~~) — permission request banner with approve/deny
  buttons, auto-dismissed on response
- [ ] File change diffs (inline) — **Deferred**
- [x] Mode indicator + switcher (supervised / auto / autonomous) — dropdown in header, sends `mode.set` over WebSocket
- [ ] Slash commands (`/auto`, `/supervised`, `/autonomous`, `/done`) — **Deferred** (mode switcher covers the same
  functionality)
- [ ] Session list with resume / fork / archive actions — **Deferred** (requires thread CRUD UI + session persistence)
- [x] Status indicator (green = connected, yellow = connecting, gray = disconnected, red = error) — dot + label in
  header
- [ ] Worker job monitoring inline (when agent creates a job, show progress) — **Deferred**

**Implementation notes:**

- Route: `/chat` with "Chat" sidebar entry (Material Symbols `chat` icon)
- Page wrapper: `cockpit/src/app/simple/pages/chat/chat-page.component.ts`
- Connection dialog allows direct WebSocket URL for local dev (`ws://localhost:8001/ws/chat`)
- Service uses native `WebSocket` API (not HttpClient or SSE). All state as Angular signals — no RxJS for chat state.
- Component is standalone with Catppuccin dark theme, consistent with existing cockpit styling.
- Interrupt button shown during agent turns, sends `interrupt` method over WebSocket.
- Angular build clean, 101 existing cockpit tests pass.

## Open Questions

**1. Multi-session persistent agents.** Initially, one session per agent pod. Could a single agent handle multiple sessions (multiple WebSocket connections, isolated contexts)? This would reduce pod management overhead. Defer to post-MVP.

**2. Resource limits.** Persistent agents may run for hours with active shell sessions and large contexts. Start with 1Gi request / 4Gi limit, monitor, and adjust. Consider allowing users to choose resource tiers (light / standard / heavy).

**3. Cost tracking and budgets.** Persistent agents can accumulate significant token usage over a session. Should there be per-session token budgets? Per-user daily limits? Display cost estimates in the cockpit? Defer to post-MVP, but track `total_tokens_used` from the start.

**4. Pod persistence across orchestrator restarts.** If the orchestrator restarts, it must rediscover existing persistent agent pods (by label query: `app=srw-persistent`) and reconcile with the threads table. Same pattern as the stale agent detector, but at startup.

**5. Shared vs dedicated workspace.** Workers share an RWX PVC (`srw-workspace`). Persistent agents with VMs use the VM's filesystem. Persistent agents without VMs — should they use the shared PVC (risk of cross-session interference) or per-session PVCs (more isolation, more storage management)? Recommend per-session PVCs for isolation.

**6. Builder chat coexistence.** The builder chat (SSE-based, instruction drafting) and persistent agent sessions serve different purposes. Should they coexist as separate cockpit features, or should persistent agents eventually replace the builder? Keep both initially — builder for quick instruction editing, persistent agent for deep collaboration.

**7. Scaling limits.** How many concurrent persistent agent sessions should the system support? Each is a pod with 1-4Gi memory. On a cluster with 128Gi total, that's 32-128 concurrent agents. Is this sufficient? Monitor and adjust.

**8. Agent config per session.** Persistent agents use expert configs (developer, scholar, etc.) to customize persona, tools, and LLM. Should the user choose the expert config when creating a session, or should it default to a generic "interactive" config? Recommend: user selects at session creation, with "interactive" as the default.

## Sources

Architecture research that informed the persistent agent design:

- [How Claude Code Works — Official Docs](https://code.claude.com/docs/en/how-claude-code-works) — Agentic loop, permission modes, context management, session model
- [How the Agent Loop Works — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/agent-loop) — `stop_reason`-driven loop, tool execution cycle
- [Effective Context Engineering for AI Agents — Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — Prompt assembly, transient injection patterns
- [Unrolling the Codex Agent Loop — OpenAI](https://openai.com/index/unrolling-the-codex-agent-loop/) — Item/Turn/Thread protocol, compaction, sandbox architecture
- [Codex CLI — GitHub](https://github.com/openai/codex) — Open source Rust implementation, ThreadManager/CodexThread/Session
- [Codex CLI Features — OpenAI Docs](https://developers.openai.com/codex/cli/features) — Three-tier permission framework, mid-execution steering
- [Gemini CLI — GitHub](https://github.com/google-gemini/gemini-cli) — Open source TypeScript implementation, CoreToolScheduler, agent skills
- [Gemini CLI Plan Mode — Google Developers Blog](https://developers.googleblog.com/plan-mode-now-available-in-gemini-cli/) — Read-only plan mode, `ask_user` tool
- [How Cursor Shipped its Agent — ByteByteGo](https://blog.bytebytego.com/p/how-cursor-shipped-its-coding-agent) — ReAct orchestrator, context compaction, sandbox infrastructure
- [Devin 2025 Performance Review — Cognition](https://cognition.ai/blog/devin-annual-performance-review-2025) — Multi-interface sessions, human-in-the-loop patterns

## References

- [[vm_backend]] — Workspace backend abstraction, RemoteBackend for persistent agent VMs
- [[nats]] — NATS messaging layer (potential future transport)
- [[job_auto_assign]] — Current dispatch logic, priority queue (worker mode)
- [[projects]] — Project infrastructure (sessions are project-scoped)
- [[memory_light]] — Memory system (shared by both modes, project-scoped recall)
- [[auxiliary]] — AuxiliaryLLM (summarization, memory extraction — same in persistent mode)
- [[verification_phase]] — Critic follow-up (worker mode only)
- [[builder]] — Builder chat (existing interaction model, stays separate)
