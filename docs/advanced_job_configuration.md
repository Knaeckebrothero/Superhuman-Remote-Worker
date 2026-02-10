# Advanced Job Configuration

## Problem Statement

Experts (preconfigured agents like Researcher, Coder, Writer) ship with custom `instructions.md` and `config.yaml` files that define their behavior, tools, and personality. However, when a user selects an expert in the cockpit and creates a job, the only meaningful input they can provide is a **description** field -- a short text explaining what the agent should accomplish.

This creates a fundamental gap: **experts can't receive a proper task**. The preconfigured instructions define *how* the agent works but not *what specific task* it should perform. Every expert's instructions.md ends with a generic placeholder like "Your specific task will be provided when the job is created" -- but there's no mechanism for the user to inject task-specific content into the instructions. The description field gets stored as job metadata and injected into the initial workspace, but it's not part of the agent's instruction hierarchy.

Users who want to customize an expert's behavior (tweak instructions, adjust the model, change tools) must prepare YAML/Markdown files externally and upload them through the advanced section -- a workflow inaccessible to most users.

### Current Flow

```
User selects expert (e.g., Researcher)
  -> Expert has preconfigured instructions.md (research methodology, citation rules, etc.)
  -> Expert has preconfigured config.yaml (tools, LLM model, workspace structure)
  -> User types a description ("Research RAG architectures")
  -> Job is created: config_name="researcher", description="Research RAG architectures"
  -> Agent starts, loads researcher/instructions.md verbatim from disk
  -> Description available as metadata but instructions are entirely preconfigured
```

### Pain Points

1. **No task prompt for experts**: Each expert's instructions.md defines methodology but has no slot for the user's actual task. The description field is metadata, not part of the prompt hierarchy.

2. **Config requires file upload**: To change the LLM model, temperature, or enabled tools, users must write a YAML override file and upload it through the advanced section. This requires knowledge of the config schema and is not discoverable.

3. **Instructions require file upload**: To modify an expert's instructions (add domain constraints, change output format), users must author a complete replacement instructions.md and upload it. Most users don't know the expected structure.

4. **No assisted authoring**: There's no interactive way to refine instructions. The OpenAI GPT builder showed the value of chat-based prompt crafting. We have nothing equivalent.

### Dead Code: The `instructions` Inline Field

The API models already define an `instructions: str | None` field on both `JobCreate` (orchestrator) and `OrchestratorJobRequest` (agent API). However, this field is **never wired up end-to-end**:

- **Orchestrator `create_job`** (main.py:397): Stores `instructions_upload_id` in context but ignores `job.instructions` (the inline text). It's never persisted.
- **Orchestrator `assign_job`** (main.py:1437): Builds `JobStartRequest` without setting `instructions=`. Only `instructions_upload_id` is extracted from context and forwarded.
- **Agent API** (app.py:276): Does receive `instructions` and puts it in metadata.
- **Agent `_setup_job_workspace`** (agent.py:782): Only checks for `instructions_upload_id` in metadata. Never reads `metadata["instructions"]`.

**Result**: The inline `instructions` field exists in the schema but is a dead path. No instructions text sent via this field ever reaches the agent's workspace.

## Solution

Three interconnected features, all scoped to **per-job configuration** (no persistent changes to expert templates on disk):

### 1. Inline Instructions Editor (in Job Creation)

Replace the instructions file upload dropzone with an inline Markdown editor. When a user selects an expert, the editor pre-loads that expert's default `instructions.md`. The user can read, understand, and edit the instructions directly -- adding their task, modifying behavior, removing sections -- and the result is sent as inline text with the job.

This also solves the "no task prompt" problem: users edit the `## Task` section at the bottom of the instructions to describe their specific task within the context of the expert's methodology.

### 2. Key Config Settings Form (in Job Creation)

Replace the config YAML upload dropzone with a form for the most impactful settings:

| Setting | Control | Default Source |
|---------|---------|---------------|
| LLM Model | Dropdown | Expert's `config.yaml` `llm.model` or `defaults.yaml` |
| Temperature | Slider (0.0 - 1.0) | Expert's `llm.temperature` (default: 0.0) |
| Reasoning Level | Dropdown (none/low/medium/high) | Expert's `llm.reasoning_level` (default: high) |
| Tool Categories | Toggle per category | Expert's `tools` section |

Pre-fills with the expert's configured values. Changes are sent as `config_override` JSON -- already supported by the backend.

### 3. Chat-Based Instruction Builder (Artifact Pattern)

A standalone cockpit panel ("Instruction Builder") implementing the **artifact pattern** inspired by [Claude Artifacts](https://support.claude.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them), [ChatGPT Canvas](https://openai.com/index/introducing-canvas/), and [LangChain Open Canvas](https://github.com/langchain-ai/open-canvas).

The key insight: the chat conversation is **separate from the work product**. The job form fields (instructions, config, description) are shared **artifacts** that both the user and the AI can read and modify. The AI uses structured tool calls to mutate artifacts, while the user edits them directly in the form. Both sides always see the current state.

- User describes what they want in natural language
- AI responds with conversational text (explaining rationale) **and** structured tool calls (mutations to artifacts)
- The form fields update in real-time as tool-call events arrive via SSE; the editor is **locked** during AI streaming to prevent conflicting edits
- User can also edit artifacts directly in the form; changes are visible to the AI on the next turn
- Iterative conversation ("make it more formal", "add output format requirements")
- Chat session is tied to the job (lazy-created on first message, linked on job submission)

All changes are per-job only.

## Architecture

### Data Flow (Target State)

```
Cockpit Job Creation Form
  |
  |-- Expert selected -> GET /api/experts/{id} -> returns instructions + config
  |-- Instructions editor pre-fills with expert's instructions.md
  |-- Config form pre-fills with expert's settings
  |-- User edits instructions / tweaks settings
  |-- (Optional) Instruction Builder panel helps craft instructions via chat
  |
  v
POST /api/jobs
  {
    description: "Research RAG architectures",
    config_name: "researcher",
    instructions: "<full edited instructions.md content>",
    config_override: {
      "llm": { "model": "gpt-4o", "temperature": 0.2 }
    },
    datasource_ids: [...],
    builder_session_id: "uuid"  // optional, links builder chat to this job
  }
  |
  v
Orchestrator: stores instructions in context, forwards to agent on assignment
  |
  v
Agent: writes instructions text to workspace/instructions.md (takes priority over template)
Agent: merges config_override over expert's base config
```

### Backend Changes Required

#### Fix: Wire Up the `instructions` Inline Field

The existing `instructions` field needs to be connected end-to-end:

1. **Orchestrator `create_job`**: Store `job.instructions` in context:
   ```python
   if job.instructions:
       context["instructions"] = job.instructions
   ```

2. **Orchestrator `assign_job`**: Extract and forward:
   ```python
   instructions_text = job_context.get("instructions")
   # ...
   job_start = JobStartRequest(
       ...,
       instructions=instructions_text,
   )
   ```
   Also add `"instructions"` to `extracted_keys` set.

3. **Agent `_setup_job_workspace`**: Add handling before the upload check:
   ```python
   if metadata.get("instructions"):
       # Inline instructions take priority
       self._workspace_manager.write_file("instructions.md", metadata["instructions"])
   elif metadata.get("instructions_upload_id"):
       # Fall back to uploaded file
       ...
   else:
       # Fall back to template
       instructions = load_instructions(self.config)
       ...
   ```

**Note**: `config_override` is already fully wired end-to-end (stored as JSONB in the jobs table, forwarded on assignment, deep-merged by the agent at startup with LLM recreation). No additional backend work is needed for config overrides.

#### New Endpoint: `GET /api/experts/{expert_id}`

Returns expert metadata plus instructions content and resolved config values for the settings form.

```json
{
  "id": "researcher",
  "display_name": "Autonomous Researcher",
  "description": "Conducts systematic literature reviews...",
  "icon": "science",
  "color": "#89b4fa",
  "tags": ["research", "literature-review"],
  "instructions": "# Autonomous Researcher Instructions\n\nYou are an autonomous...",
  "config": {
    "llm": {
      "model": "gpt-4o",
      "temperature": 0.0,
      "reasoning_level": "high"
    },
    "tools": {
      "workspace": ["read_file", "write_file", ...],
      "core": ["next_phase_todos", "todo_complete", ...],
      "research": ["web_search", "extract_webpage", ...],
      "citation": ["cite_document", "cite_web", ...],
      "document": ["chunk_document"],
      "coding": [],
      "git": ["git_log", "git_diff", ...],
      "graph": [],
      "sql": [],
      "mongodb": []
    }
  }
}
```

Implementation: Read `config/experts/{id}/config.yaml`, merge with `config/defaults.yaml` to resolve inherited values, read `config/experts/{id}/instructions.md` (falling back to `config/prompts/instructions.md`).

#### New Endpoint: `POST /api/builder/sessions`

Creates a new builder chat session. Called lazily on the user's first message. The session starts unlinked to any job — the link is established after job submission.

```json
// Request
{
  "expert_id": "researcher"  // optional, pre-selected expert
}

// Response
{
  "session_id": "uuid"
}
```

#### New Endpoint: `POST /api/builder/sessions/{session_id}/message`

Sends a user message and streams the AI response via SSE. The request includes the current artifact state so the AI always sees the latest user edits.

```json
// Request
{
  "content": "Make the output format section more specific for CSV exports",
  "artifacts": {
    "instructions": "# Researcher Instructions\n...",
    "config": {
      "llm": { "model": "gpt-4o", "temperature": 0.0, "reasoning_level": "high" },
      "tools": { "research": ["web_search"], "citation": ["cite_web"] }
    },
    "description": "Research RAG architectures"
  }
}
```

Response is an SSE stream with these event types:

| Event | Payload | Purpose |
|-------|---------|---------|
| `token` | `{ "text": "Sure, I'll..." }` | Streamed conversational text (displayed in chat) |
| `tool_call` | `{ "tool": "update_instructions", "args": { "content": "..." } }` | Replace full instructions content |
| `tool_call` | `{ "tool": "edit_instructions", "args": { "old_text": "...", "new_text": "..." } }` | String replacement in instructions |
| `tool_call` | `{ "tool": "insert_instructions", "args": { "content": "...", "line": 42 } }` | Insert at line (omit `line` to append) |
| `tool_call` | `{ "tool": "update_config", "args": { "llm": { "temperature": 0.3 } } }` | Config artifact mutation (objects merge, arrays replace) |
| `tool_call` | `{ "tool": "update_description", "args": { "content": "..." } }` | Description artifact mutation |
| `done` | `{ "usage": { "input_tokens": 500, "output_tokens": 800 } }` | Stream complete |
| `error` | `{ "message": "..." }` | Error information |

The backend builds the LLM prompt as:
```
System prompt (static): instruction-builder persona + tool definitions
  + Current artifact state (injected fresh each turn, never summarized):
    - instructions.md content
    - config settings JSON
    - description text
  + Conversation history (auto-summarized when approaching context limit)
  + User's new message
```

Uses the same LLM provider configuration as the rest of the system:

- **OpenAI-compatible**: Uses `OPENAI_API_KEY` + optional `OPENAI_BASE_URL` (covers OpenAI, vLLM, Ollama, llama.cpp, any compatible endpoint). Model configurable via `BUILDER_MODEL` env var, defaults to `gpt-4o-mini`.
- **Anthropic**: Uses `ANTHROPIC_API_KEY`. Model configurable via `BUILDER_MODEL`, defaults to `claude-haiku-4-5-20251001`.
- **Provider selection**: If `BUILDER_LLM_PROVIDER` is set, use that. Otherwise auto-detect from `BUILDER_MODEL` name (same logic as the agent's provider auto-detection: `claude-*` → Anthropic, everything else → OpenAI-compatible).

This reuses the existing provider patterns from `src/core/loader.py` rather than introducing new LLM client code. The system prompt instructs the LLM to act as an instruction writer that understands the agent's phase alternation model, workspace structure, and tool capabilities.

Auto-summarization uses the same `BUILDER_MODEL` for simplicity.

#### Artifact Tool Definitions (Given to Builder LLM)

The builder LLM receives these as tool/function definitions:

| Tool | Parameters | Behavior |
|------|-----------|----------|
| `update_instructions` | `content: str` | Replace the full instructions.md content |
| `edit_instructions` | `old_text: str, new_text: str` | Find `old_text` in instructions and replace with `new_text` (exact string match, like the agent's `edit_file` tool) |
| `insert_instructions` | `content: str, line?: int` | Insert `content` at line number. If `line` is omitted, append to end |
| `update_config` | `llm?: {model?, temperature?, reasoning_level?}, tools?: {category: tool_list}` | Merge partial config changes. **Objects merge recursively, arrays replace entirely** (matching the agent's config inheritance semantics) |
| `update_description` | `content: str` | Replace the job description |

The AI can return **both** conversational text and tool calls in a single response. The frontend streams the text to the chat panel and applies tool-call events to the job form as they arrive. The instructions editor is **locked** (read-only) while an AI response is streaming to prevent conflicting edits.

#### Auto-Summarization

Conversation history is stored in `builder_messages` and sent to the LLM on each turn. When the message history approaches the context limit (~80% of the model's window minus the artifact state and system prompt), older messages are summarized into a compact summary stored on the session. Subsequent turns include the summary as a system message followed by only the recent unsummarized messages.

The artifact state is **never** summarized — it is injected fresh from the request payload on every turn. This mirrors the agent's workspace.md injection pattern.

### Storage Schema

```sql
-- Chat sessions for the instruction builder
CREATE TABLE builder_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID,                    -- NULL at creation, set after job submission via update
    expert_id VARCHAR(100),         -- expert used as starting point (nullable)
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    summary TEXT                    -- auto-summary of older messages (for context compaction)
);
-- No FK on job_id: the job may not exist yet (lazy linking after submission)

CREATE TABLE builder_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES builder_sessions(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,      -- 'user', 'assistant'
    content TEXT,                   -- conversational text
    tool_calls JSONB,              -- structured artifact mutations (assistant only)
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Index for efficient message retrieval
CREATE INDEX idx_builder_messages_session ON builder_messages(session_id, created_at);
```

**Session lifecycle:**
1. User opens job creation form → no session yet
2. User sends first message in builder chat → `POST /api/builder/sessions` creates session (no job link yet)
3. Each message → `POST /api/builder/sessions/{id}/message` streams response
4. User submits job → `POST /api/jobs` creates job normally → backend updates `builder_sessions.job_id` using the `builder_session_id` field from `JobCreate`
5. User abandons without submitting → orphaned session (`job_id` stays NULL, cleaned up manually as needed)

### Cockpit Components

#### Modified: `JobCreateComponent`

- **Expert selector**: On selection, call `GET /api/experts/{id}` to load full details
- **Instructions editor**: Replace file upload dropzone with a resizable textarea (Markdown). Pre-fills on expert selection. Collapsible with "Edit Instructions" toggle.
- **Config settings**: Replace YAML upload with form controls for model/temperature/reasoning/tools
- **Description field**: Unchanged -- short task summary for job listing
- **Artifact sync**: Watches `JobArtifactService` signals for real-time updates from the builder chat

#### New: `InstructionBuilderComponent`

Registered as a cockpit panel type (`instruction-builder`). Features:
- Chat message list with markdown rendering (via `ngx-markdown`)
- Auto-expanding input textarea with Enter-to-send
- Anchor-based scroll management (salvaged from Advanced-LLM-Chat)
- SSE streaming: conversational tokens stream into chat, tool-call events update artifacts in real-time
- Session-scoped message history (persisted in `builder_messages` table for context continuity)

#### New: `JobArtifactService`

Shared Angular signal service for bidirectional artifact state between the builder chat and the job creation form:

```typescript
@Injectable({ providedIn: 'root' })
export class JobArtifactService {
  /** Current artifact state — single source of truth for both panels */
  readonly instructions = signal<string | null>(null);
  readonly config = signal<Partial<ExpertConfig> | null>(null);
  readonly description = signal<string | null>(null);

  /** Session tracking */
  readonly sessionId = signal<string | null>(null);

  /** Whether AI is currently streaming (locks editor to prevent conflicts) */
  readonly streaming = signal<boolean>(false);

  /** Apply an artifact mutation from a builder tool call */
  applyToolCall(tool: string, args: Record<string, any>): void {
    switch (tool) {
      case 'update_instructions':
        this.instructions.set(args['content']);
        break;
      case 'edit_instructions':
        this.instructions.update(current =>
          current?.replace(args['old_text'], args['new_text']) ?? current
        );
        break;
      case 'insert_instructions':
        this.instructions.update(current => {
          if (!current) return args['content'];
          if (args['line'] == null) return current + '\n' + args['content'];
          const lines = current.split('\n');
          lines.splice(args['line'] - 1, 0, args['content']);
          return lines.join('\n');
        });
        break;
      case 'update_config':
        this.config.update(current => deepMerge(current ?? {}, args));
        break;
      case 'update_description':
        this.description.set(args['content']);
        break;
    }
  }

  /** Reset for a new job creation session */
  reset(): void {
    this.instructions.set(null);
    this.config.set(null);
    this.description.set(null);
    this.sessionId.set(null);
    this.streaming.set(false);
  }
}
```

**Bidirectional sync:**
- **Builder → Form**: SSE tool-call events call `applyToolCall()`, form reactively updates. The `streaming` signal is `true` during AI response, which locks the instructions editor to read-only
- **Form → Builder**: User edits in the form update the signals directly (only when `streaming` is `false`). On the next chat message, the current artifact state from the signals is sent in the request payload so the AI sees the user's manual edits

### Model List for Settings Dropdown

Hardcoded in the frontend as a curated list of commonly available models. Grouped by provider:

```typescript
// Example — update with current models at implementation time
const AVAILABLE_MODELS = [
  { group: 'OpenAI', models: ['gpt-4o', 'gpt-4o-mini', 'o3-mini'] },
  { group: 'Anthropic', models: ['claude-sonnet-4-5-20250929', 'claude-haiku-4-5-20251001'] },
  { group: 'Google', models: ['gemini-2.0-flash', 'gemini-2.5-pro'] },
  { group: 'Open Source', models: ['deepseek-r1', 'qwen-2.5-72b'] },
];
```

Users can also type a custom model name if theirs isn't listed (combo-box pattern). The backend doesn't validate model names -- it passes them through to the LLM provider.

## Implementation Roadmap

### Phase 1: Wire Up Inline Instructions + Expert Detail API

**Goal**: Fix the dead `instructions` field end-to-end and add the expert detail endpoint.

| Task | File | Detail |
|------|------|--------|
| Store inline instructions in job context | `orchestrator/main.py` (`create_job`) | Add `if job.instructions: context["instructions"] = job.instructions` |
| Forward instructions on assignment | `orchestrator/main.py` (`assign_job`) | Extract from context, set on `JobStartRequest` |
| Handle inline instructions in agent | `src/agent.py` (`_setup_job_workspace`) | Prioritize `metadata["instructions"]` over upload and template |
| Add `GET /api/experts/{expert_id}` | `orchestrator/main.py` | Read instructions.md + merged config, return JSON |

### Phase 2: Instructions Editor in Job Creation

**Goal**: Users can view and edit an expert's instructions inline.

| Task | File | Detail |
|------|------|--------|
| Add `getExpertDetail()` to API service | `cockpit/.../api.service.ts` | `GET /api/experts/{id}` |
| Add `ExpertDetail` interface | `cockpit/.../api.model.ts` | Extends `Expert` with `instructions` and `config` |
| Replace instructions upload with editor | `cockpit/.../job-create.component.ts` | Textarea with expert pre-fill, send via `instructions` field |
| Fetch expert details on selection | `cockpit/.../job-create.component.ts` | Call API, populate editor + settings |

### Phase 3: Config Settings Form

**Goal**: Users can tweak key settings via form controls.

| Task | File | Detail |
|------|------|--------|
| Replace config upload with settings form | `cockpit/.../job-create.component.ts` | Model dropdown, temperature slider, reasoning dropdown, tool toggles |
| Build `config_override` from form | `cockpit/.../job-create.component.ts` | Diff against expert defaults, only send changed values |
| Model list constant | `cockpit/.../job-create.component.ts` | Hardcoded grouped model list with custom input option |

### Phase 4: Builder Backend (Sessions + SSE Streaming + Artifact Tools)

**Goal**: Backend infrastructure for the instruction builder chat with artifact pattern.

| Task | File | Detail |
|------|------|--------|
| Add builder tables to schema | `orchestrator/database/schema.sql` | `builder_sessions` + `builder_messages` tables |
| Add builder DB queries | `orchestrator/database/postgres.py` | CRUD for sessions and messages |
| Include builder tables in init | `orchestrator/init.py` | Ensure builder tables are created during `--only-orchestrator` init |
| Add `builder_session_id` to `JobCreate` | `orchestrator/main.py` | Optional field; on job creation, update session's `job_id` to link them |
| Add `POST /api/builder/sessions` | `orchestrator/main.py` | Create session (no job link yet) |
| Add `POST /api/builder/sessions/{id}/message` | `orchestrator/main.py` | SSE endpoint: receive message + artifact state, stream response with tool calls |
| Define artifact tool schemas | `orchestrator/services/builder_tools.py` | `update_instructions`, `edit_instructions`, `insert_instructions`, `update_config`, `update_description` |
| Builder system prompt | `orchestrator/services/builder_prompt.py` | Instruction-writer persona, tool definitions, agent knowledge (phases, workspace, tools) |
| Auto-summarization | `orchestrator/services/builder_tools.py` | Summarize older messages when approaching context limit (uses same `BUILDER_MODEL`) |

### Phase 5: Builder Frontend (Chat UI + Artifact Sync)

**Goal**: Chat panel with SSE streaming and bidirectional artifact sync with the job form.

| Task | File | Detail |
|------|------|--------|
| Create `JobArtifactService` | `cockpit/.../services/job-artifact.service.ts` | Shared signal service for bidirectional artifact state + streaming lock |
| Create `BuilderStreamService` | `cockpit/.../services/builder-stream.service.ts` | SSE client for builder endpoint (salvaged from Advanced-LLM-Chat `StreamingService`) |
| Create `InstructionBuilderComponent` | `cockpit/.../components/instruction-builder/` | Chat UI with message list, input, markdown rendering |
| Register component | `cockpit/.../app.ts` + `layout.model.ts` | Add to component registry and type union |
| Connect job form to artifact service | `cockpit/.../job-create.component.ts` | Read artifact signals for form state, write back on user edits |
| Add `ngx-markdown` + `prismjs` | `cockpit/package.json` | Markdown rendering in chat messages |

### Phase 6: Polish

- Loading states and error handling throughout
- Form validation (temperature range, non-empty instructions)
- Markdown syntax highlighting in instructions editor (optional, textarea is fine for v1)
- Responsive layout for settings form on narrow panels

## File Inventory

### Files to Modify

| File | Phase | Changes |
|------|-------|---------|
| `orchestrator/main.py` | 1, 4 | Wire `instructions` field, add expert detail endpoint, add `builder_session_id` to `JobCreate`, add builder session/message endpoints |
| `orchestrator/database/schema.sql` | 4 | Add `builder_sessions` + `builder_messages` tables |
| `orchestrator/database/postgres.py` | 4 | Add builder session/message CRUD queries |
| `orchestrator/init.py` | 4 | Include builder tables in database initialization |
| `src/agent.py` | 1 | Handle `metadata["instructions"]` for workspace instructions |
| `cockpit/src/app/components/job-create/job-create.component.ts` | 2, 3, 5 | Instructions editor, config settings form, artifact service integration |
| `cockpit/src/app/core/services/api.service.ts` | 2, 5 | `getExpertDetail()`, builder session/message methods |
| `cockpit/src/app/core/models/api.model.ts` | 2 | `ExpertDetail` interface, builder types |
| `cockpit/src/app/core/models/layout.model.ts` | 5 | Add `instruction-builder` to `ComponentType` |
| `cockpit/src/app/app.ts` | 5 | Register `InstructionBuilderComponent` |
| `cockpit/package.json` | 5 | Add `ngx-markdown`, `prismjs`, `marked` |

### Files to Create

| File | Phase | Purpose |
|------|-------|---------|
| `orchestrator/services/builder_tools.py` | 4 | Artifact tool schemas + auto-summarization logic |
| `orchestrator/services/builder_prompt.py` | 4 | Builder system prompt with agent knowledge |
| `cockpit/src/app/core/services/job-artifact.service.ts` | 5 | Shared signal service for bidirectional artifact state |
| `cockpit/src/app/core/services/builder-stream.service.ts` | 5 | SSE client for builder streaming endpoint |
| `cockpit/src/app/components/instruction-builder/instruction-builder.component.ts` | 5 | Chat UI with streaming + artifact tool-call handling |

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Scope | Per-job only | Simplest to implement, no file-system writes needed, no risk of corrupting expert templates |
| Instructions priority | inline > upload > template | Inline is the new default path; upload kept for backward compat; template is the fallback |
| Config form scope | Key settings only | Full schema form is complex and rarely needed; power users can still upload YAML |
| Model list source | Hardcoded in frontend | Avoids backend complexity; easy to update; users can type custom models |
| Instruction builder LLM | Env-configured (`BUILDER_MODEL` + `BUILDER_LLM_PROVIDER`), defaults to `gpt-4o-mini` | Reuses existing provider auto-detection. Supports OpenAI-compatible (including custom endpoints) and Anthropic. Users with local LLM setups (vLLM, Ollama) can point the builder at their own inference server |
| Chat ↔ form communication | Artifact pattern via `JobArtifactService` | Inspired by Claude Artifacts / ChatGPT Canvas / Open Canvas. AI uses structured tool calls to mutate form fields; user edits are fed back to AI on next turn. Shared Angular signal service provides bidirectional reactivity. Editor locked during AI streaming to prevent conflicts |
| Builder chat persistence | Server-side (PostgreSQL) | Enables context continuity across page reloads, auto-summarization for long sessions, and audit trail linking chat to job |
| Session ↔ job linking | Post-submission update (option b) | Session created without job link. After `POST /api/jobs` with `builder_session_id`, backend updates session's `job_id`. Simpler than pre-generating job UUIDs; no need to accept external IDs on job creation |
| Session lifecycle | Lazy creation, linked after submission | Session created on first message (not on form open). Avoids empty sessions for users who don't use the builder. Orphaned sessions (no linked job) left for manual cleanup |
| Instruction edit tools | String replace + line insert (like agent's `edit_file`) | More precise than heading-based section replacement. Handles edge cases naturally (no match = no change). Familiar pattern already proven in the agent's workspace tools |
| Config merge semantics | Objects merge recursively, arrays replace | Matches existing config inheritance behavior from `defaults.yaml`. Allows precise tool list replacement (e.g., set `research: ["web_search"]` to override the full list) |
| AI response format | SSE with text + tool calls | Conversational text streams to chat immediately; tool-call events apply artifact mutations to the form in real-time. Required from day one by the artifact pattern |
| Artifact state injection | Fresh per turn (never summarized) | Mirrors agent's workspace.md pattern. Only conversation history gets compacted; artifact state is always current |
| LLM proxy | Orchestrator endpoints | Keeps API keys server-side; reuses existing infrastructure |

## Salvageable Components from Advanced-LLM-Chat

The `Advanced-LLM-Chat/` repository contains a mature Angular 19 + FastAPI chat application with agent streaming support. Several components can be adapted for the Instruction Builder panel.

### Frontend Components

| Component | Source File | What to Salvage | Adaptation Needed |
|-----------|------------|-----------------|-------------------|
| **SSE Streaming** | `src/app/services/streaming.service.ts` (225 lines) | Complete `fetch`-based SSE parser with `AbortController`, Observable wrapper, multi-line `data:` support | Simplify event types — builder only needs text streaming, not `step`/`tool_call`/`tool_result` events |
| **Chat Container** | `src/app/chat-ui/chat-ui.component.ts` (691 lines) | Anchor-based scroll management (Claude/ChatGPT style), streaming message append logic, RxJS state composition | Strip conversation persistence, pagination, file attachments, TTS. Keep scroll logic + streaming message handling |
| **Chat Template** | `src/app/chat-ui/chat-ui.component.html` (39 lines) | Clean layout: scroll container → message loop → streaming message → input field | Reuse structure nearly as-is; add "Apply to Job" button |
| **Chat Styles** | `src/app/chat-ui/chat-ui.component.scss` (190 lines) | CSS variable-based theming, scrollbar styling, responsive breakpoints, message bubble layout | Reuse directly — theming approach matches cockpit patterns |
| **Message Rendering** | `src/app/chat-ui/chat-ui-message/chat-ui-message.component.ts` (430 lines) | Markdown rendering via `ngx-markdown` with PrismJS syntax highlighting, message role styling | Strip agent steps, TTS player, edit/rate actions. Keep markdown body + role avatar |
| **Message Template** | `src/app/chat-ui/chat-ui-message/chat-ui-message.component.html` (225 lines) | Avatar + role label + markdown body layout | Simplify to role label + markdown content only |
| **Input Field** | `src/app/chat-ui/chat-ui-inputfield/chat-ui-inputfield.component.ts` | Auto-expanding textarea, Enter-to-send, shift+Enter for newline, Angular Material styling | Strip file upload and voice recording. Keep textarea auto-resize + keyboard handling |

### Backend Patterns

| Pattern | Source File | What to Salvage | Adaptation Needed |
|---------|------------|-----------------|-------------------|
| **SSE Endpoint** | `backend/api/messages.py:862-1118` | `sse_starlette.EventSourceResponse` setup, async generator pattern for LLM streaming, error event handling | Simplify significantly — no LangGraph agent, no tool use, just a direct LLM call with system prompt + conversation history |
| **LLM Provider** | `backend/services/llm_provider.py` | Multi-provider factory (`get_llm()`) auto-detecting OpenAI/Anthropic from model name, reasoning level wrapping | Already exists in orchestrator context — reference for patterns only |
| **Web Search** | `backend/services/tools/web_search_tools.py` | Tavily integration via LangChain `TavilySearchResults` (max_results=5, search_depth="advanced") | Valuable addition — lets the instruction builder LLM look up documentation and best practices when helping users write instructions |

### Key Differences to Handle

| Aspect | Advanced-LLM-Chat | Cockpit | Migration Notes |
|--------|-------------------|---------|-----------------|
| Module system | `standalone: false` (NgModule) | Standalone components | All copied components must be converted to standalone with `imports: [...]` |
| State management | RxJS `BehaviorSubject` + `combineLatest` | Angular signals preferred | Convert to signals where natural, keep RxJS for streams |
| Routing | Full router with conversation pages | Panel-based component registry | No routing needed — builder is a registered panel component |
| Dependencies | `ngx-markdown` 19.1.0, `prismjs` 1.30.0 | Not currently installed | Need to add `ngx-markdown` + `prismjs` to cockpit's `package.json` |
| API communication | Direct `HttpClient` calls | `ApiService` wrapper | Use cockpit's existing `ApiService` pattern |

### Streaming Decision: SSE from Day One

SSE streaming is required from v1 because the artifact pattern demands it: the AI response contains both conversational text (streamed to chat) **and** tool-call events (applied to the job form). With a simple POST, the user would wait for the entire response before seeing any chat text or artifact updates. With SSE:

1. Conversational tokens stream into the chat immediately (responsive UX)
2. Tool-call events arrive mid-stream and update form fields in real-time
3. The `done` event signals completion with token usage stats

The Advanced-LLM-Chat `StreamingService` provides an excellent base: `fetch`-based SSE with `AbortController`, Observable wrapper, and multi-line `data:` support. Adaptation is straightforward — we add `tool_call` as an event type alongside the existing `token`/`done`/`error` types.

### Dependency Impact

New cockpit dependencies needed for the instruction builder:
- `ngx-markdown` — Markdown rendering in chat messages (already used and proven in Advanced-LLM-Chat)
- `prismjs` — Syntax highlighting for code blocks in rendered markdown
- `marked` — Markdown parser (check actual peer dependency requirements of `ngx-markdown` version at implementation time)

These are lightweight, well-maintained libraries with no security concerns.

## Future Considerations

- **Expert template editing**: Allow saving customized experts as new templates (`POST /api/experts`). Would require writing to `config/experts/` on disk and invalidating the expert cache.
- **Instruction versioning**: Track instruction edits per-job for audit trail. Could extend `builder_messages.tool_calls` into a full artifact version history.
- **Config presets**: Named config variations (e.g., "fast mode" = gpt-4o-mini + low reasoning) as quick-select options.
- **Web search in builder**: Add Tavily web search capability to the instruction builder LLM (salvaged from Advanced-LLM-Chat) so it can look up documentation and best practices when helping users craft instructions.
- **Builder session resume**: Allow users to resume a builder chat when editing an existing job's configuration (load session by job_id).
- **Artifact diff view**: Show what the AI changed in the instructions (inline diff highlighting) after each `edit_instructions` / `insert_instructions` tool call.
- **Orphaned session cleanup**: Automated cleanup of builder sessions with no linked job (e.g., sessions older than 24h with `job_id IS NULL`). Not needed for v1 — manual cleanup is sufficient.
