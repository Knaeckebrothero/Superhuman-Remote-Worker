---
tags:
  - feature
  - architecture
  - interactive
  - agent
aliases:
  - persistent agent
  - interactive mode
  - chat mode
related:
  - "[[projects]]"
  - "[[builder]]"
  - "[[vm_backend]]"
  - "[[memory_light]]"
  - "[[auxiliary]]"
---

# Interactive Agent Mode

Design document for adding an interactive execution mode to the agent, where the user and agent collaborate in real-time within a persistent session.

**Status:** Design phase.

## Industry Context

All major coding agents converge on the same core pattern. The differences are in execution environment and UI surface, not in the fundamental loop.

| Agent | Loop | Tools | Permission Model | Plan Mode | Context | Session |
|-------|------|-------|------------------|-----------|---------|---------|
| **Claude Code** | `while(tool_call)` — model decides everything, no classifiers/DAG/planner | 8 native (Bash, Read, Edit, Write, Grep, Glob, Task, TodoWrite) + MCP | **Mode-based**: Default (ask all), Auto-accept edits, Plan (read-only). Shift+Tab cycles. Per-command allowlists in settings. | Read-only tool restriction. No writes, no execution. User reviews plan, then exits plan mode to execute. | 200K window. Auto-compact at ~75-92%. CLAUDE.md for persistent rules. Subagents get isolated context. | Local JSONL. Resume (`--continue`), fork (`--fork-session`). Independent — no cross-session history. |
| **Codex CLI** | Same ReAct loop. Rust app-server decouples agent from UI surfaces. | Shell, file ops, web search, MCP | **Mode-based**: `auto` (ask for out-of-scope), `readOnly`, `fullAccess`. Switch mid-session via `/permissions`. | Not a separate mode — user can steer mid-turn. | Manual compaction via `thread/compact/start`. | **Item/Turn/Thread** protocol. Threads are durable, support resume/fork/archive. JSONL persistence. |
| **Gemini CLI** | Event-driven scheduler. Agent skills for extensibility. | File ops, grep, glob, browser agent, MCP, custom agent skills | Mode-based with `/plan` and `/settings`. | Read-only. Uses `ask_user` tool for bidirectional Q&A during planning. `enter_plan_mode` / `exit_plan_mode` tools. | Not documented in detail. | Session-based, skills extensible. |
| **Cursor** | ReAct with Composer orchestrator. MoE model routes by complexity. | 10+ (codebase search, file read/write, edit, terminal) | Auto mode — agent runs autonomously, user reviews diffs before apply. Background agents run in isolated VMs. | Implicit in three-layer model (Understanding → Execution → Integration). | Compaction retains "stable signals" (test names, error types, stack frames). Deduplicates snippets. | IDE-scoped. Background agents work on branches, create PRs. |
| **Devin** | Multi-model pipeline: Planner → Coder → Critic. | Full dev environment (browser, terminal, editor) | Conversational — user directs via Slack/web/Linear. Status dot: green (working), orange (waiting). | Dynamic re-planning on roadblocks. | Full VM with persistent state. | Persistent sessions across Slack/web/CLI/API. |

### Key Patterns That Matter for Us

**1. The loop is just `while(tool_call)`.** Claude Code proved that a simple ReAct loop with no orchestration layers outperforms complex DAG/planner systems. The model decides everything. No classifiers, no intent routing, no forced phases. This is the most important design decision.

**2. Permission modes, not per-tool approval.** Every production agent uses **mode-based** control (ask-all / auto-accept / read-only), not per-tool risk annotations. The user sets a mode that governs all tools. Simpler to reason about, simpler to implement, matches user mental models.

**3. Plan mode varies by agent.** Claude Code and Gemini restrict to read-only tools. Cursor uses an implicit three-layer model. Devin uses dynamic re-planning. Our approach: reuse the existing strategic/tactical phase alternation — the most powerful planning system we've built. The chat wraps it as a feedback surface. Gemini's `ask_user` pattern is adopted for interactive Q&A.

**4. Steering / interrupts are first-class.** Codex has `turn/steer` to inject input into an active turn. Claude Code lets you type and press Enter to interrupt. The user can redirect the agent mid-execution without waiting for it to finish.

**5. Subagents for context isolation.** Claude Code spawns isolated subagents (depth-1, no access to parent context, only summaries return). This prevents context bloat on exploratory work. We already have `AuxiliaryLLM` — adding a `delegate_task` tool is natural.

**6. Codex's Item/Turn/Thread protocol** is the best abstraction for agent ↔ client communication. Items are atomic units (messages, tool calls, file changes). Turns group items from one user request. Threads are durable sessions. We should adopt this model for our WebSocket protocol.

**7. Sessions are independent and durable.** No cross-session conversation history. Persistent state lives in workspace files (workspace.md / CLAUDE.md), not in the conversation. Sessions can be resumed, forked, archived. State survives reconnection via checkpointing.

**8. Context compaction is essential.** All agents compact. The pattern: summarize history, discard verbose tool outputs, preserve key signals (file paths, error messages, decisions). We already have this via `AuxiliaryLLM`'s `SummarizeTask`.

## Motivation

The current system has two extremes:

| | Autonomous Job | Builder Chat |
|--|----------------|-------------|
| Tool depth | Full (shell, research, citations, git, KB, VM, ...) | Shallow (inspect jobs, draft instructions, web search) |
| User interaction | None during execution. Fire-and-forget. | Real-time conversation. |
| Planning | Forced strategic/tactical alternation | None |
| Workspace | Full (workspace.md, plan.md, todos, archive) | None (stateless per-session) |
| Duration | Hours | Minutes |
| Use case | "Write chapter 5 of my thesis" | "Help me configure this agent" |

Neither mode supports the middle ground: an agent with deep tool access that the user can collaborate with interactively — prototyping, exploring, debugging, pair-programming, researching together, with the option to hand off longer stretches of autonomous work.

**Goal:** A single agent mode that covers the full spectrum from "answer my question" to "work on this for 3 hours" — determined by the conversation, not by configuration upfront.

## Core Model

A job is a chat session. The interaction model looks like Claude Code:

```
User: "Set up the database schema for the new feature"
Agent: [reads existing schema] [runs shell commands] [writes migration]
       "Done. Created migration in workspace/migrations/001.sql. Want me to run it?"
User: "Yes, and then seed it with test data"
Agent: [runs migration] [generates seed script] [executes it]
       "Database is up with 50 test records. Here's a sample: ..."
User: /plan
Agent: [enters plan mode — reads workspace state, reviews what's done]
       "Here's what I'd suggest for the API layer: ..."
User: "Go ahead and build the CRUD endpoints"
Agent: [works through several tool calls, streaming progress]
       [returns to user naturally when done or uncertain]
       "Built 4 endpoints. The DELETE endpoint needs a decision — soft delete or hard delete?"
User: "Soft delete. Also, check what the citation engine does for similar patterns"
Agent: [searches knowledge base] [reads citation engine code]
       "CitationEngine uses soft delete with a deleted_at timestamp. Here's the pattern: ..."
```

The user sees tool calls streaming in real-time. They can interject at any point (steering). The agent works through multiple steps when given a clear directive but returns to the user naturally when done, stuck, or uncertain.

### What This IS

Following the industry consensus:

- A **`while(tool_call)` loop** — the model decides everything. No classifiers, no forced phases, no DAG orchestration.
- **Mode-based permissions** — the user picks a mode (supervised / auto-accept / autonomous), not per-tool policies.
- **Plan mode leverages the full phase alternation system** — the chat wraps the existing job feedback mechanism.
- **Durable sessions** with resume, fork, and archive.
- **Steering** — user can inject messages mid-turn.

### What This Is NOT

- Not a new agent type or a separate codebase. Same `UniversalAgent`, same tools, same config system.
- Not the builder chat with more tools bolted on. The builder stays as-is for instruction drafting.
- Not replacing autonomous jobs. Autonomous execution is one extreme of the interaction spectrum — reachable via handoff.

## Permission Modes

Following the Claude Code / Codex pattern — mode-based, not per-tool:

| Mode | Behavior | Analogy |
|------|----------|---------|
| **Supervised** (default) | Agent asks before file writes and shell commands. Reads, searches, and web lookups run freely. | Claude Code's default mode |
| **Auto-accept** | File writes execute without asking. Shell commands still require approval. | Claude Code's auto-accept edits |
| **Autonomous** | Everything executes without asking. Agent streams progress but doesn't wait. | Codex `fullAccess` / Claude Code `--dangerously-skip-permissions` |

The user switches modes via:
- Slash commands in the chat: `/supervised`, `/auto`, `/autonomous`
- Or a mode selector in the cockpit UI

Mode applies to the **entire session** (or until switched). No per-tool annotations needed. This is simpler and matches how every production agent works.

**Plan mode is not a permission mode.** It's a command (`/plan "description"`) that creates an internal job running the full phase alternation system. The chat wraps the job's feedback mechanism. See the Plan Mode section for details.

**Per-command allowlists** (like Claude Code's `.claude/settings.json`): For frequently-approved commands (e.g., `npm test`, `pytest`, `git status`), the user can add patterns to an allowlist. These skip approval even in supervised mode. Stored in the project config or session config.

## The Agent Loop

### Core: `while(tool_call)`

Following Claude Code's proven pattern — no orchestration layers, no classifiers:

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

### How This Differs from the Current Graph

| Aspect | Current `graph.py` | Interactive Loop |
|--------|--------------------|--------------------|
| **Structure** | LangGraph state machine with 8+ nodes, conditional edges | `while(tool_call)` inner loop, `while True` outer loop |
| **Loop driver** | Todos — execute until all complete, then phase transition | Conversation — execute per user turn, model decides when done |
| **Phase alternation** | Forced (strategic ↔ tactical), separate LLMs per phase | None. Plan mode restricts tools, doesn't change the LLM. |
| **Tool filtering** | Phase-based (`filter_tools_by_phase`) | Mode-based (all tools available, permission mode gates execution) |
| **User input** | Only at resume (with feedback) | Every turn, plus steering mid-turn |
| **Completion signal** | `job_complete` tool → `check_goal` node | Natural conversation end, or `/done` command |
| **State management** | `UniversalAgentState` with 30+ fields | Lighter state: messages, mode, workspace path, session metadata |
| **Context compaction** | Same | Same (`AuxiliaryLLM` → `SummarizeTask`) |

### What We Reuse Unchanged

The loop is simpler, but the infrastructure underneath is identical:

- **`ToolContext`** — dependency injection for all tool factories
- **`load_tools()`** / tool registry — same tool loading pipeline
- **`ContextManager`** — compaction, 3-layer context safety
- **Transient injection** — workspace.md as fake ToolCall result, memories, knowledge
- **`ShellManager`** — tmux-backed persistent shells
- **`AuxiliaryLLM`** — summarization (compaction), memory extraction
- **`WorkspaceManager`** — file operations, git versioning
- **VM backend** — `RemoteBackend` for SSH/SFTP workspace on VMs
- **`KeyRing`** — API key rotation
- **Config system** — YAML configs, matrix resolvers, `$extends` inheritance
- **LLM creation** — `create_llm()`, phase-specific configs, provider routing

## Protocol: Items, Turns, Threads

Adopting Codex's three-level abstraction for agent ↔ client communication:

### Item

The atomic unit of input or output. Each item has a lifecycle: `started` → optional deltas → `completed`.

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

### Turn

Groups items from one user request + the agent work that follows. A turn starts when the user sends a message and ends when the agent has no more tool calls to make (or the user steers into a new direction).

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

### Thread

The durable session container. Supports creation, resumption, forking, and archival.

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

**Thread operations:**
- **Create**: New session, initialize workspace from project main
- **Resume**: Reconnect to existing session, restore from checkpoint
- **Fork**: Branch from existing session (new thread ID, preserves conversation up to fork point)
- **Archive**: Push workspace to git, extract memories, mark as historical
- **Compact**: Trigger context compaction manually

## WebSocket Transport

The agent API (`src/api/app.py`) gets a WebSocket endpoint:

```
ws://agent:8001/ws/thread/{thread_id}
```

### Client → Agent

```json
{"method": "turn/start", "params": {"input": "Fix the login bug"}}
{"method": "turn/steer", "params": {"input": "Actually focus on session handling"}}
{"method": "turn/interrupt"}
{"method": "approval/respond", "params": {"item_id": "...", "decision": "allow"}}
{"method": "mode/set", "params": {"mode": "plan"}}
{"method": "thread/compact"}
{"method": "thread/archive"}
```

### Agent → Client

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

### Why WebSocket, Not SSE?

The builder uses SSE (server → client only) because the user sends discrete HTTP requests. Interactive mode needs bidirectional streaming: steering input, approval responses, and interrupts arrive while the agent is mid-turn. WebSocket gives us this without polling.

### Reconnection

WebSocket drops happen. Session state lives in the LangGraph checkpointer + workspace files. On reconnect:

1. Client sends `thread/resume` with thread ID
2. Agent loads checkpoint, restores state
3. Sends recent conversation history (items from last N turns) for UI display
4. User continues where they left off

Behavior on disconnect is configurable per session:
- **Pause** (default): Agent stops, waits for reconnect
- **Continue**: Agent keeps working autonomously, user catches up on reconnect

## Plan Mode (Reusing the Phase Alternation System)

Plan mode doesn't reinvent planning — it reuses the existing strategic/tactical machinery. The chat becomes a wrapper around the job feedback mechanism that already exists.

**How it works:**

1. User types `/plan` (or asks the agent to plan something)
2. Agent creates an **internal job** scoped to the current thread's workspace
3. The job enters its **first strategic phase** — the full existing pattern: review, reflect, adapt, plan
4. The agent runs through the strategic/tactical phase alternation loop autonomously
5. Progress streams to the chat as status updates (current phase, current todo, tool calls)
6. When the job **freezes** (per autonomy level) or **completes**, results flow back to the chat
7. The user reviews the results in the chat and can:
   - Send feedback → the existing `resume_with_feedback` mechanism continues the job
   - Approve → the job completes, results stay in the workspace
   - Redirect → user types a new message, returning to normal chat mode
8. User exits plan mode with `/chat` or just by sending a normal message

**Why reuse the phase system instead of read-only restriction?**

The strategic/tactical system is the most powerful planning machinery we have — workspace.md, plan.md, todos, phase retrospectives, git versioning, memory extraction. Reducing plan mode to "same loop, fewer tools" throws all of that away. Instead, the chat wraps the existing system:

- `/plan "Design the API layer"` → creates a job with that description → agent plans and executes using the full phase alternation system → results appear in chat
- User feedback in chat → feeds into the job's `resume_with_feedback` flow
- The chat is the UI surface; the job system does the actual work

This also means plan mode can handle **real work**, not just read-only exploration. The user can ask the agent to plan and execute a multi-phase task, watch progress in the chat, and steer via feedback messages.

**`ask_user` tool:** Added to the agent's tool set (in both interactive and autonomous modes). When the agent is uncertain or needs a decision, it calls `ask_user` instead of guessing. In interactive mode, this sends a message to the chat and waits for a response. In autonomous mode, this triggers a freeze (same as the current freeze-for-review pattern). This makes autonomous jobs more interactive too — the agent can ask questions instead of making assumptions.

## Subagents (Existing Subjob System)

No new subagent infrastructure needed. The existing **subjob system** was designed for exactly this:

- The agent creates a subjob via the orchestrator API (already implemented)
- The subjob gets its own workspace, tools, context, and LLM
- It runs the full agent loop (or the interactive loop, depending on config)
- Results flow back to the parent job/thread
- Subjobs work on branches within the parent's repo (`subjob/<short-id>/<type>`)
- Squash merge on completion

Subjobs are **more powerful than Claude Code's depth-1 subagents** because they get:
- Their own workspace with git versioning
- Full tool access (not limited to parent's tools)
- Phase alternation (can do multi-phase work)
- Persistence (can be resumed if they fail)
- Their own expert config (can use a different persona/model)

In interactive mode, the agent creates subjobs the same way it does in autonomous mode. The chat shows subjob progress and results inline. The user can even chat with a subjob directly if needed.

**Steering for normal jobs:** The `ask_user` tool + the existing freeze/feedback mechanism means autonomous jobs also get a form of steering. When an autonomous agent calls `ask_user`, it freezes and the user can respond via the cockpit or the chat (if connected). This bridges the gap between fully autonomous and fully interactive.

## Interaction Modes (Within a Single Session)

The session flows naturally between these modes based on the conversation and the permission mode:

### Chat (Supervised or Auto-Accept mode)

Turn-based conversation. User sends a message, agent responds (using tools as needed), then waits for the next message.

- Agent uses tools per turn, respecting the permission mode
- Streams tokens + tool calls to the client in real-time
- Waits for user input after each turn completes
- No todo system, no phases — just conversation

### Autonomous

User gives a directive and the agent loops without waiting. The model decides when to stop (task complete, stuck, needs decision).

- User sets mode to `autonomous` or says "go ahead and do X"
- Agent loops: LLM → tools → LLM → tools → ...
- Streams all items to the client (user watches progress)
- User can steer (`turn/steer`) or interrupt (`turn/interrupt`) at any time
- Safety cap: `max_autonomous_iterations` (default 50) prevents runaway loops
- Agent naturally returns to waiting when the task is done or it needs a decision

### Plan (Full Phase Alternation)

User types `/plan "Design the API layer"`. Agent creates an internal job and runs the full strategic/tactical system. Chat becomes a wrapper around the job feedback mechanism. See Plan Mode section above.

### Handoff (Bridge to Full Autonomous Job)

For truly long-running work that doesn't need interaction. User says "run this overnight" or `/plan "Write the full test suite"`:

1. An internal job is created (same as plan mode, but with autonomy set to `full`)
2. The full phase alternation system runs
3. User can disconnect — agent continues
4. Agent freezes on completion (per autonomy level config)
5. User reconnects later, sees results in the chat, continues chatting

Plan mode and handoff mode use the **same mechanism** (internal job via phase alternation). The difference is the autonomy level and whether the user stays connected:
- `/plan` → autonomy `review` (freezes for feedback, user watches in chat)
- `/run` or "run this overnight" → autonomy `full` (runs to completion, user can disconnect)

## Architecture

### Agent-Side

```
┌─────────────────────────────────────────────┐
│  Agent Process (src/api/app.py)             │
│                                             │
│  ┌─────────────┐  ┌──────────────────────┐  │
│  │ WebSocket   │  │ Interactive Loop     │  │
│  │ Handler     │──│                      │  │
│  │ (per thread)│  │ while(tool_call):    │  │
│  └─────────────┘  │   inject context     │  │
│        │          │   LLM call           │  │
│        │          │   stream response    │  │
│        │          │   check permissions  │  │
│        │          │   execute tools      │  │
│        │          │   check for steering │  │
│        │          └──────────────────────┘  │
│        │                    │               │
│  ┌─────┴────────────────────┴────────────┐  │
│  │          Shared Infrastructure        │  │
│  │  ToolContext │ ContextManager │ Tools  │  │
│  │  ShellMgr   │ WorkspaceMgr   │ LLMs   │  │
│  │  AuxLLM     │ RecallStore    │ KB     │  │
│  │  VM Backend │ KeyRing        │ Config │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

New code:
- `src/graph_interactive.py` — the interactive `while(tool_call)` loop
- WebSocket handler in `src/api/app.py`
- Session/thread management
- Permission mode enforcement

### Orchestrator-Side

Minimal changes. The orchestrator manages threads the same way it manages jobs:

- `POST /api/threads` → create thread (assigns agent, returns `{thread_id, agent_ws_url}`)
- `GET /api/threads/{id}` → thread metadata
- `GET /api/threads` → list user's threads (with status, project filter)
- `DELETE /api/threads/{id}` → terminate thread
- `POST /api/threads/{id}/archive` → archive thread
- `POST /api/threads/{id}/fork` → fork thread

The orchestrator does NOT proxy the WebSocket. The cockpit connects directly to the agent.

```
Cockpit ──WebSocket──→ Agent (tool execution, streaming, interaction)
Cockpit ──REST──→ Orchestrator (thread management, agent assignment)
```

### Cockpit-Side

A new chat component (or evolution of the builder):

- WebSocket connection to the assigned agent
- Streaming markdown rendering for agent messages
- Tool call display with expandable results
- Approval request UI (approve / deny / allow-for-session buttons)
- File change diffs (inline, like a code review)
- Mode indicator + switcher (supervised / auto / autonomous)
- Slash commands (`/plan`, `/auto`, `/supervised`, `/compact`, `/done`)
- Plan mode job progress display (phase, todos, streaming output)
- Session list with resume / fork / archive actions
- Status indicator (green = working, orange = waiting, gray = idle)

## Session Lifecycle

### Creation

```
User clicks "New Chat" in cockpit
  → POST /api/threads {project_id, expert_config (optional)}
  → Orchestrator assigns agent, creates thread record
  → Returns {thread_id, agent_ws_url}
  → Cockpit opens WebSocket to agent
  → Agent initializes: workspace (from project main), tools, LLM, context
  → Agent sends welcome message (based on expert persona, if configured)
  → User starts chatting
```

### Workspace Initialization

Interactive sessions use the project's workspace:

- Clone from project's jobs repo `main` branch (accumulated state from previous jobs/sessions)
- `workspace.md` carries forward project context / long-term memory
- Datasources resolved via project scope (three-level: job > project > global)
- Knowledge base and memory scoped to project

For non-project sessions (quick one-offs): a temporary workspace with no git backing.

### Persistence & Resume

Sessions persist across disconnects:

- **Conversation state**: LangGraph checkpointer (SQLite or PostgreSQL)
- **Workspace state**: Files on disk (or remote VM)
- **Persistent context**: workspace.md + compaction summaries survive reconnection

Resume flow:
1. Cockpit fetches thread metadata from orchestrator
2. Opens WebSocket to assigned agent
3. Agent loads checkpoint, restores state
4. Sends recent items for cockpit display
5. User continues where they left off

### Forking

Like `claude --continue --fork-session` or Codex's `thread/fork`:
- Creates a new thread ID
- Preserves conversation history up to the fork point
- Both threads can continue independently
- Use case: "let me try a different approach without losing the current one"

### Archival

User types `/done` or the session is explicitly archived:
1. Push workspace changes to git (job branch → merge to main)
2. Extract memories via `AuxiliaryLLM` → `RecallStore`
3. Curate knowledge if KB is enabled
4. Thread status → `archived`
5. Thread becomes historical record in project timeline

## Workspace Files in Interactive Mode

| File | Interactive Role |
|------|-----------------|
| `workspace.md` | Same — persistent memory, transient-injected every turn |
| `plan.md` | Updated in plan mode, optional — user doesn't have to use it |
| `todos.yaml` | Only used during handoff to autonomous mode |
| `archive/` | Populated on archival or after autonomous stretches |
| `output/` | Deliverables, same as today |

## Configuration

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

## Relation to Existing Features

### Jobs ↔ Threads

Threads and jobs are two interaction modes for the same underlying system:

| | Job (autonomous) | Thread (interactive) |
|--|---|---|
| **Created via** | Orchestrator API / cockpit job-create | Cockpit "New Chat" |
| **Execution** | Phase alternation graph | `while(tool_call)` loop |
| **User interaction** | None (until freeze) | Every turn |
| **Workspace** | Same | Same |
| **Project scope** | Same | Same |

**Schema approach**: Add `interaction_mode` column to the `jobs` table (`autonomous` | `interactive`). Interactive threads are just jobs with a different execution mode. This keeps project history unified — jobs and threads appear in the same timeline.

Handoff from interactive → autonomous is a mode switch on the same job record.

### Builder Chat

Stays as-is. The builder drafts instructions / configures agents. The interactive agent does actual work. Different purposes, potentially same cockpit page (tabs: "Chat" | "Configure").

### MCP Server

The MCP server currently exposes orchestrator tools to Claude Code. Future: MCP could expose the interactive agent's tools, giving Claude Code access to VMs, knowledge bases, datasources through the agent's tool layer.

### Projects

Interactive sessions are naturally project-scoped. The project provides workspace state, datasources, knowledge base, memory, and expert configs.

## Implementation Phases

### Phase 1: Interactive Loop + WebSocket

The core. Build the `while(tool_call)` loop and WebSocket transport.

- [ ] `src/graph_interactive.py` — interactive loop (not a LangGraph graph — just an async function)
- [ ] Session state class (messages, mode, workspace path, thread metadata)
- [ ] Transient injection (reuse `create_workspace_tool_messages`, memory injection, knowledge injection)
- [ ] Context compaction (reuse `ContextManager` with `AuxiliaryLLM`)
- [ ] WebSocket endpoint on agent API (`ws://agent:8001/ws/thread/{thread_id}`)
- [ ] Item/Turn/Thread protocol (JSON over WebSocket)
- [ ] Tool loading (reuse `_setup_job_tools()` — same ToolContext, same tools)
- [ ] Basic permission enforcement (supervised mode: ask for writes + shell)
- [ ] Steering: `turn/steer` injects input into active turn
- [ ] Session initialization (workspace from project, tool loading, LLM creation)
- [ ] LLM streaming (token-by-token via `astream()`)

### Phase 2: Permission Modes + Plan Mode

- [ ] All four modes: supervised, auto-accept, autonomous, plan
- [ ] Mode switching via WebSocket command (`mode/set`)
- [ ] Plan mode: create internal job → run phase alternation → stream progress → feedback via chat
- [ ] `ask_user` tool — works in both interactive (WebSocket response) and autonomous (freeze) modes
- [ ] Command allowlist (patterns for pre-approved commands)
- [ ] Autonomous mode iteration cap
- [ ] Interrupt handling (`turn/interrupt`)

### Phase 3: Orchestrator Integration + Session Lifecycle

- [ ] Thread CRUD endpoints on orchestrator
- [ ] Agent assignment for interactive threads
- [ ] Thread ↔ project linkage
- [ ] Checkpoint-based resume across disconnects
- [ ] Thread forking
- [ ] Thread archival (git push, memory extraction, knowledge curation)
- [ ] `interaction_mode` column on jobs table

### Phase 4: Cockpit UI

- [ ] Interactive chat component
- [ ] WebSocket connection management (connect, reconnect, disconnect)
- [ ] Streaming markdown rendering
- [ ] Tool call display (expandable results)
- [ ] File change diffs (inline)
- [ ] Approval request UI (approve / deny / allow-for-session)
- [ ] Mode indicator + switcher
- [ ] Slash commands
- [ ] Thread list with resume / fork / archive
- [ ] Status indicator (green / orange / gray)

### Phase 5: Subjobs + Advanced Features

- [ ] Subjob creation from interactive sessions (reuse existing subjob infrastructure)
- [ ] Subjob progress/results displayed inline in chat
- [ ] Handoff to autonomous mode (create todos, switch to phase alternation graph)
- [ ] Reconnect with autonomous progress replay
- [ ] Session forking in UI
- [ ] VM lifecycle for long-lived interactive sessions (idle timeout, cleanup)
- [ ] Token usage tracking per thread
- [ ] `ask_user` tool in normal autonomous jobs (freeze + wait for feedback)

## Open Questions

1. **LangGraph or plain async?** The interactive loop is simple enough to be a plain async function (no graph nodes, no conditional edges). But LangGraph's checkpointing is valuable for resume. Worth investigating whether we can use LangGraph's checkpointer without the full graph abstraction.

2. **Agent assignment**: Interactive sessions need sticky assignment (user stays on one agent). What happens if the agent pod restarts? Checkpoint + reconnect to a different agent instance?

3. **VM lifecycle for interactive**: Autonomous jobs spin up a VM at start and tear it down at completion. Interactive sessions are long-lived — the VM should persist across disconnects. Idle timeout for cleanup?

4. **Concurrent sessions per agent**: Can one agent process handle multiple interactive sessions? Each session needs its own LLM context, tools, and workspace — probably one session per agent process initially.

5. **Cost control**: Interactive sessions can run for hours. Token budget awareness? Usage tracking? Per-turn cost display in the UI?

6. **Internal job lifecycle**: When `/plan` creates an internal job, does it get a full DB record? Or is it a lightweight in-process job? Full DB record means it shows up in project history (good for traceability). In-process means less overhead. Probably full DB record for consistency with the existing system.

7. **`ask_user` in autonomous jobs**: When an autonomous agent calls `ask_user`, it should freeze and wait for feedback. Should this be a new freeze reason (`waiting_for_input`) distinct from `review`? The cockpit could show these differently — "Agent has a question" vs "Job complete, ready for review."

8. **Subjob visibility in chat**: When the interactive agent creates a subjob, how much of the subjob's progress should be visible in the chat? Full streaming (noisy) vs summary on completion (clean) vs expandable inline view (best UX, more work)?

## Sources

Architecture research that informed this design:

- [Claude Code Fundamentals — DeepWiki](https://deepwiki.com/FlorianBruniaux/claude-code-ultimate-guide/3-claude-code-fundamentals) — `while(tool_call)` loop, 8 native tools, subagent architecture
- [How Claude Code Works — Official Docs](https://code.claude.com/docs/en/how-claude-code-works) — Agentic loop, permission modes, context management, session model
- [Codex App Server — OpenAI Docs](https://developers.openai.com/codex/app-server/) — Item/Turn/Thread protocol, approval system, sandbox policies
- [Codex CLI Features — OpenAI Docs](https://developers.openai.com/codex/cli/features) — Three-tier permission framework, mid-execution steering
- [Codex App Server Architecture — InfoQ](https://www.infoq.com/news/2026/02/opanai-codex-app-server/) — Bidirectional JSON-RPC, conversation primitives
- [How Cursor Shipped its Agent — ByteByteGo](https://blog.bytebytego.com/p/how-cursor-shipped-its-coding-agent) — ReAct orchestrator, context compaction, sandbox infrastructure
- [Gemini CLI Plan Mode — Google Developers Blog](https://developers.googleblog.com/plan-mode-now-available-in-gemini-cli/) — Read-only plan mode, `ask_user` tool
- [Devin 2025 Performance Review — Cognition](https://cognition.ai/blog/devin-annual-performance-review-2025) — Multi-interface sessions, human-in-the-loop patterns

## References

- [[projects]] — Project infrastructure (sessions are project-scoped)
- [[builder]] — Builder chat (existing interaction model, stays separate)
- [[vm_backend]] — VM workspace backend (reusable for interactive sessions)
- [[memory_light]] — Memory system (project-scoped recall across sessions)
- [[auxiliary]] — AuxiliaryLLM (summarization, memory extraction — same in interactive mode)
