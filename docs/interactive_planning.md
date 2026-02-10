# Interactive Planning

Design document for the interactive planning feature — a human-in-the-loop review cycle where the agent pauses after its first strategic phase so the user can review, discuss, and approve the plan before execution begins.

## Motivation

The current job creation flow has a disconnect between what the user describes and what the agent plans. The instruction builder chat helps users craft instructions, but it operates in isolation — without access to the actual documents, datasources, or tools the agent will use. The agent then re-interprets those instructions during its first strategic phase, effectively planning twice.

By letting the agent run its first strategic phase with full context (uploaded documents, attached datasources, all configured tools) and then pausing for human review, we:

- Ground the plan in reality rather than the user's description of their materials
- Eliminate the gap between "what I asked for" and "what the agent understood"
- Give the user meaningful control at the point where it matters most — the plan, not the prompt
- Let the instruction builder chat serve a new, more valuable role as a review companion

## How It Works

### Job Lifecycle with Interactive Planning

```
User creates job (description, documents, datasources)
        |
        v
Agent starts immediately
        |
        v
First strategic phase runs
  - Agent reads uploaded documents
  - Agent explores datasources
  - Agent builds workspace.md, plan.md
  - Agent creates todos for first tactical phase
        |
        v
Agent freezes for review  <-- NEW BEHAVIOR
        |
        v
User reviews plan in cockpit
  - Sees workspace.md, plan.md, todos
  - Uses instruction builder chat as review companion
        |
        +---> User gives feedback
        |         |
        |         v
        |     Agent resumes into another strategic iteration
        |     (revises plan based on feedback)
        |         |
        |         v
        |     Agent freezes again for review
        |         |
        |         +---> (repeat until satisfied)
        |
        +---> User approves
                  |
                  v
              Agent resumes into first tactical phase
              Runs autonomously from here
                  |
                  v
              Normal phase alternation continues
              (strategic <-> tactical until job_complete)
```

### What the Agent Produces During the First Strategic Phase

The agent's first strategic phase already creates the key artifacts the user needs to review:

| Artifact | Purpose |
|----------|---------|
| `workspace.md` | Long-term memory — agent's understanding of the task, context, constraints |
| `plan.md` | Strategic plan — phased approach, methodology, success criteria |
| `todos.yaml` | Concrete task list for the first tactical phase |
| `tools/*.md` | Auto-generated tool documentation (shows what capabilities the agent has) |

These files are the plan. The user reviews them directly — no translation layer needed.

### Role of the Instruction Builder Chat

The instruction builder is not redundant. It shifts from being a pre-job prompt crafting tool to a **review companion** that helps the user throughout the job lifecycle:

**During plan review (agent frozen after strategic phase):**
- Navigate and understand the plan — "What does phase 2 cover?", "Why did you choose this approach?"
- Discuss changes — "I think we should split the analysis into two phases"
- Formulate feedback — the chat helps the user craft precise, actionable feedback that the agent can act on optimally
- Suggest edits — the chat can propose specific modifications to workspace.md or plan.md

**During result review (job completed/frozen after execution):**
- Navigate outputs — "Summarize what was produced", "Show me the key findings"
- Identify issues — "The analysis missed section 3 of the input document"
- Draft correction instructions — help the user articulate what needs to change so the agent receives clear guidance on resume

**Key insight:** The instruction builder chat becomes the user's AI assistant for communicating with the agent. It helps bridge the gap between what the user thinks and what the agent needs to hear.

## Implementation

### 1. Freeze After First Strategic Phase

A configuration option controls whether the agent pauses for review after its initial planning phase.

**Config (`config/defaults.yaml` or per-agent config):**
```yaml
planning:
  interactive: true          # Freeze after first strategic phase for review
  # interactive: false       # Legacy behavior — no pause, immediate execution
```

**Graph behavior (`src/graph.py`):**
At the `handle_transition` node, after the first strategic phase completes:
- If `interactive: true` and this is strategic phase 1 (no tactical work has been done yet):
  - Set job status to a review state (e.g., `pending_review` or reuse existing `frozen`)
  - Persist checkpoint
  - Exit the graph

This is similar to the existing freeze mechanism but triggered automatically at a specific point.

### 2. Cockpit Review UI

The cockpit needs a review interface for frozen jobs. This could be:

**Option A — Extend the existing job detail view:**
- Show workspace.md, plan.md, and todos in formatted read-only panels
- Add a feedback text area and approve/revise buttons
- Embed the instruction builder chat alongside for discussion

**Option B — Dedicated review page:**
- Full-width layout with the plan artifacts on one side and the chat on the other
- More room for the review workflow

Either way, the core elements are:
- **Plan display** — rendered markdown for workspace.md and plan.md
- **Todo list** — structured view of the planned tasks
- **Chat panel** — instruction builder connected to the job's builder session
- **Action buttons** — "Approve" (resume into tactical) or "Request Changes" (resume into strategic with feedback)

### 3. Feedback Injection on Resume

When the user provides feedback and requests changes, the agent needs to receive that feedback when it resumes. Options:

**Option A — Append to workspace.md:**
Add a `## Review Feedback` section to workspace.md before resuming. Since workspace.md is injected into every LLM call, the agent will see it immediately.

**Option B — Inject as a message:**
Add the feedback as a HumanMessage to the checkpoint state before resuming. The agent sees it as the last thing the user said.

**Option C — Both:**
Write feedback to workspace.md for persistence and inject as a message for immediate attention.

Option C is likely best — the message ensures the agent addresses feedback immediately, while the workspace.md entry ensures it survives context compaction.

### 4. Builder Chat Integration

The instruction builder chat session should be linked to the job from the start (this already happens via `builder_session_id`). During review:

- The chat has access to the job's workspace files (workspace.md, plan.md, todos)
- The chat's system prompt is updated to reflect its review companion role
- Tool calls like `update_instructions` could be repurposed or extended to modify workspace files
- The chat helps the user formulate feedback that gets submitted via the "Request Changes" action

### 5. Builder Chat Tooling

The builder chat becomes significantly more useful when it can actually inspect and navigate the job's artifacts and history. Rather than just being a conversational partner, it becomes a **context-aware assistant** with deep visibility into what the agent did and produced.

#### Workspace Tools

| Tool | Purpose |
|------|---------|
| `read_workspace_file` | Read any file from the job's workspace (workspace.md, plan.md, todos.yaml, output files, etc.) |
| `list_workspace_files` | Browse the workspace directory structure |
| `search_workspace` | Full-text search across workspace files |
| `vector_search` | Semantic search over workspace content and uploaded documents — find relevant sections without knowing exact terms |

These let the user ask things like "What does the plan say about data validation?" or "Find where the agent discusses the methodology" and get grounded answers.

#### Git History Tools

| Tool | Purpose |
|------|---------|
| `git_log` | View commit history — see how workspace files evolved across phases |
| `git_diff` | Compare versions — "What changed between phase 1 and phase 2?" |
| `git_show` | View a specific commit's contents |

When git versioning is enabled, each todo completion and phase boundary creates a commit. The builder chat can use this to explain what happened chronologically: "The agent rewrote the methodology section after phase 3 — here's the diff."

#### Orchestrator MCP Tools (Debugging)

The builder chat can be given access to the same MCP tools used for debugging in Claude Code. This turns it into a **UI-based debugging assistant for non-technical users**.

| Tool | Purpose |
|------|---------|
| `get_job` | Job metadata — status, config, timestamps |
| `get_audit_trail` | Full audit trail — every LLM call, tool invocation, and error |
| `get_chat_history` | The agent's conversation turns — see its reasoning flow |
| `get_todos` | Current and archived todos across all phases |
| `get_graph_changes` | Neo4j mutations timeline (if graph datasource attached) |
| `get_llm_request` | Full LLM request/response for a specific call |
| `search_audit` | Search audit entries by keyword |

**Use cases for non-technical users:**
- "Why did the agent skip section 3?" — chat searches the audit trail and explains the agent's reasoning
- "The output seems wrong, what happened?" — chat reviews the conversation history to identify where things went off track
- "How much did this job cost in tokens?" — chat pulls usage stats from audit entries
- "Show me what tools the agent used" — chat summarizes tool invocations from the audit trail

This is powerful because it exposes debugging capabilities through natural language. A non-technical user doesn't need to understand audit trails or LLM request logs — they just ask questions and the chat navigates the data for them.

#### Tool Access Tiers

Not every review scenario needs every tool. Tool access could be tiered based on context:

| Tier | Tools | When |
|------|-------|------|
| **Basic** | read_workspace_file, list_workspace_files, search_workspace | Always available during review |
| **Research** | vector_search, web_search | When documents are attached or domain research is relevant |
| **History** | git_log, git_diff, git_show | When git versioning is enabled |
| **Debug** | MCP tools (audit trail, chat history, etc.) | When the user is investigating issues or the job has errors |

### 6. Resume Behavior

On approval:
- Agent resumes from checkpoint
- Transitions to the first tactical phase
- Executes normally from there

On feedback:
- Feedback is injected (see section 3)
- Agent resumes from checkpoint into another strategic iteration
- Agent revises plan.md, workspace.md, todos based on feedback
- Agent freezes again for review
- Cycle repeats until approved

## The Builder Chat Across the Job Lifecycle

With these tools, the builder chat is useful at every stage — not just plan review:

| Stage | Role | Key Tools |
|-------|------|-----------|
| **Pre-job** (current behavior) | Instruction crafting — help user describe what they want | web_search, update_instructions |
| **Plan review** (new) | Review companion — navigate plan, discuss approach, formulate feedback | workspace tools, git history |
| **Mid-execution** (future) | Progress monitor — "How's it going?", "What phase is it on?" | MCP tools (get_todos, get_job) |
| **Result review** (new) | Output navigator — explore results, identify issues, draft corrections | workspace tools, git history, MCP debug tools |
| **Post-mortem** (future) | Debugging assistant — "Why did it fail?", "Where did it go wrong?" | MCP debug tools (audit trail, chat history, LLM requests) |

The same chat UI, the same session continuity, but with context-appropriate tools activated at each stage.

## Open Questions

- **Should interactive planning be the default?** For quick jobs it adds friction. Could default to `true` for jobs with documents/datasources and `false` for simple description-only jobs.
- **How many review cycles before it becomes counterproductive?** Might want a soft limit or a "skip review and run" escape hatch.
- **Should the user be able to directly edit workspace files during review?** Or only provide feedback through the chat/text field?
- **Does the agent need a modified system prompt during the "planning for review" strategic phase?** It might benefit from knowing the plan will be reviewed — e.g., being more explicit about trade-offs, flagging assumptions, asking questions in the plan itself.
- **Builder chat tool access management:** How to handle tool tier activation? Automatic based on job state, or user-configurable?
- **Vector search indexing:** When are workspace files indexed? On every phase boundary? On freeze? Need to balance freshness with compute cost.
- **MCP authentication:** The builder chat backend needs access to the orchestrator MCP. Since both run server-side this is straightforward, but the tool calls need to be scoped to the current job (don't let the chat browse other users' jobs).
