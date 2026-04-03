---
tags:
  - architecture
  - agent
  - sessions
aliases:
  - persistent sessions
  - session management
related:
  - "[[agent_lifecycle]]"
  - "[[persistent_agent_assessment]]"
  - "[[memory_light]]"
---

# Persistent Agent Sessions

Design document for session persistence, lifecycle, and configuration in the persistent agent.

**Status:** Design phase.

## Problem

The persistent agent holds all state in memory. Disconnect = conversation lost. This is the P0 gap.

## What Already Exists

| Capability | Reuse | What's missing |
|---|---|---|
| **Context compaction** | `ContextManager.ensure_within_limits()` + `summarize_and_compact()`. Already filters out injection messages. 3-tier: threshold → summarize → emergency truncation. | Manual `/compact` trigger (one WS method + one handler line). |
| **Memory extraction** | `extract_and_store_memories()` in `src/services/auxiliary.py`. AuxiliaryLLM chain → dedup-store to RecallStore. | One `asyncio.create_task()` call every N turns in `persistent_graph.py`. |
| **Memory retrieval + injection** | `RecallStore.retrieve()` → `create_memory_injection_messages()`. Transient tool-call pair, excluded from summarization. TTL management. | One block in `_execute_turn()` before the LLM call. |
| **Knowledge injection** | `KnowledgeStore.hybrid_search()` → `create_knowledge_injection_messages()`. Same transient pattern. | Same — one block in `_execute_turn()`, gated on `project_id`. |
| **Memory assembly** | `assemble_memories()` — agent-mode AuxiliaryLLM adjusts TTLs. | Optional periodic call. |
| **Project scoping** | `ToolContext._project_id` → RecallStore/KnowledgeStore scope queries dynamically. | One line: `tool_context.project_id = str(project_id)` in session setup. |
| **Workspace.md injection** | Already working in `persistent_graph.py`. Excluded from compaction. | Nothing. |
| **Config override** | `UniversalAgent._setup_job_workspace()` already deep-merges `metadata["config_override"]` JSONB over current config. | Pass override through when setting up persistent session. |
| **User settings** | `users.settings` JSONB + `SettingsService.updatePreferences()` + cockpit settings page. | Add `persistent_agent` key to the schema and a UI section. |
| **Builder messages pattern** | `builder_messages` table: session FK, role, content, tool_calls. REST load via `GET /builder/sessions/{id}/messages`. | Follow same pattern for thread messages. |
| **Auth** | Keycloak → `get_current_user()`. Threads have `user_id` FK. | Filter thread endpoints by authenticated user. |

## What's Actually New

### 1. Message table

Follow `builder_messages` — same schema, different FK:

```sql
CREATE TABLE IF NOT EXISTS thread_messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    thread_id UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,
    content TEXT,
    tool_calls JSONB,
    turn_number INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_thread_messages_thread ON thread_messages(thread_id);
```

Plus on `threads`:
```sql
ALTER TABLE threads ADD COLUMN total_turns INTEGER DEFAULT 0;
ALTER TABLE threads ADD COLUMN total_tokens INTEGER DEFAULT 0;
```

No separate `MessageStore` class — add `save_thread_message()` and `get_thread_messages()` to the existing `orchestrator/database/postgres.py` (where thread CRUD already lives). The agent writes via its `postgres_conn` (same DB, direct SQL). Fire-and-forget — don't block the loop.

### 2. Wiring (the bulk of the work, but no new systems)

**`persistent_graph.py` `_execute_turn()`** — add before the LLM call, matching what the worker's `execute` node does:
- `recall_store.retrieve()` → `create_memory_injection_messages()` → append to prepared messages
- `recall_store.decrement_ttl()`
- `knowledge_store.hybrid_search()` → `create_knowledge_injection_messages()` (if project_id set)
- `extract_and_store_memories()` every N turns (fire-and-forget `asyncio.create_task`)

**`persistent_session.py`** — on setup:
- `tool_context.project_id = str(project_id)` if thread has a project
- Pass `config_override` from thread metadata through to `UniversalAgent` (reuse existing `_setup_job_workspace` deep-merge)

**`persistent_app.py`** — save messages to DB after each turn:
- After `HumanMessage`: `save_thread_message(thread_id, 'user', content, turn)`
- After `AIMessage`: `save_thread_message(thread_id, 'assistant', content, tool_calls, turn)`
- On `/compact`: call existing `summarize_and_compact()`, send `context.compacted` event

### 3. Session list + history load

Follow the builder pattern — history loaded via REST, not WebSocket:
- `GET /api/persistent/threads/{id}/messages` — paginated, ordered by created_at
- Angular loads messages on component init (before WS connect), renders as historical
- WS connection opens after for live interaction
- This means history works even if the agent pod isn't running yet

Session list:
- Fetches `GET /api/persistent/threads` (filtered by authenticated user)
- Shows title, status dot, last activity, config name
- Actions: Resume (→ load history + WS connect), Archive, Delete

Create session:
- Pick config + optional project → `POST /api/persistent/threads`
- Orchestrator reads `users.settings.persistent_agent`, merges into `threads.metadata.config_override`

### Slash commands

Parsed client-side, mapped to existing WS methods:

| Command | WS Method | Already works? |
|---------|-----------|---------------|
| `/compact [focus]` | `compact` | New (one handler) |
| `/done` | `archive` | New (one handler) |
| `/auto` | `mode.set` | Yes |
| `/supervised` | `mode.set` | Yes |

### Session Lifecycle

```
create ──→ active ←──→ idle ──→ ended
```

Drop the `archived` state from the earlier design — unnecessary. `ended` means session over, agent stopped, data persists for browsing (same as completed jobs). On ending:
1. `extract_and_store_memories()` on recent conversation (existing function)
2. Generate title if untitled (existing `SummarizeTask` via AuxiliaryLLM)
3. `threads.status = 'ended'`, `ended_at = now()`

## Configuration and Model Selection

### Expert Config: `config/experts/interactive/`

Follows the existing expert pattern (`developer/`, `scholar/`, `critic/`):

```
config/experts/interactive/
├── config.yaml          # $extends: defaults, tools, interactive section
├── systemprompt.txt     # Conversation-oriented (no phase/todo/plan references)
└── persona.txt          # Interactive assistant identity
```

**Why an expert config, not a matrix entry:** The prompt/instruction matrices resolve **phases** (strategic vs tactical). The persistent agent has no phases. The expert directory's `systemprompt.txt` overrides the base via `FileResolver` (expert dir searched first) — no matrix changes needed. Settings matrix unchanged too — model-family tuning applies regardless of mode.

The existing `config/interactive.yaml` (created during implementation) becomes `config/experts/interactive/config.yaml`.

### User Settings

Stored in the existing `users.settings` JSONB under a `persistent_agent` key. Read/written via the existing `SettingsService.updatePreferences()` endpoint — no new API.

```json
{
  "persistent_agent": {
    "model": "claude-sonnet-4-6",
    "permission_mode": "auto_accept",
    "greeting": "Hey! What are we working on?",
    "config_name": "interactive",
    "command_allowlist": ["pytest*", "npm test", "git status"],
    "idle_timeout_minutes": 120
  }
}
```

**Resolution order** (highest priority wins):
1. Per-session override (`threads.metadata.config_override`)
2. User settings (`users.settings.persistent_agent`)
3. Expert config (`config/experts/interactive/config.yaml`)
4. Framework defaults (`config/defaults.yaml`)

This is the same chain the worker uses: job `config_override` → expert config → defaults. The user settings layer slots between session and expert. On session create, the orchestrator merges user prefs into the thread's `config_override` JSONB. The agent applies it via the existing `deep_merge` + `load_agent_config_from_dict` path in `_setup_job_workspace()`.

**Cockpit:** Add a "Persistent Agent" section to the existing `/settings` page. Fields: model (dropdown), permission mode (select), greeting (text), command allowlist (tag input), idle timeout (number). Uses the existing `SettingsService` — just reads/writes a different key in the same JSONB.

## Implementation Phases

### Phase A: Persistence + Resume (P0)

- [ ] `thread_messages` table migration
- [ ] `total_turns`, `total_tokens` on `threads` table
- [ ] `save_thread_message()` + `get_thread_messages()` in `orchestrator/database/postgres.py`
- [ ] Agent writes messages to DB after each turn (fire-and-forget)
- [ ] `GET /api/persistent/threads/{id}/messages` endpoint
- [ ] Angular loads history via REST on component init (before WS connect)
- [ ] Thread endpoints filter by authenticated user (`get_current_user` dependency)

### Phase B: Wire Memory + Knowledge + Compaction

All existing functions, just called from the persistent loop:

- [ ] `RecallStore.retrieve()` + `create_memory_injection_messages()` in `_execute_turn()`
- [ ] `recall_store.decrement_ttl()` every turn
- [ ] `KnowledgeStore.hybrid_search()` + `create_knowledge_injection_messages()` (gated on project_id)
- [ ] `extract_and_store_memories()` every N turns (`asyncio.create_task`)
- [ ] `tool_context.project_id = str(project_id)` in session setup
- [ ] `/compact` WS method → existing `summarize_and_compact()` + `context.compacted` event
- [ ] Slash command parsing in Angular (`/compact`, `/done`, `/auto`, `/supervised`)

### Phase C: Session List + Config + Settings

- [ ] Session list component (user's threads, status, title, last activity)
- [ ] Create session dialog (pick config + project)
- [ ] Resume flow (load history via REST → render → open WS)
- [ ] End session: `extract_and_store_memories()` + `SummarizeTask` for title + status='ended'
- [ ] `config/experts/interactive/` — config.yaml, systemprompt.txt, persona.txt
- [ ] Move `config/interactive.yaml` → `config/experts/interactive/config.yaml`
- [ ] "Persistent Agent" section in cockpit settings page
- [ ] Orchestrator merges `users.settings.persistent_agent` into `threads.metadata.config_override` on create
- [ ] Agent applies override via existing `deep_merge` path

## Open Questions

**1. Message retention.** Cap per thread? Compacted messages soft-deleted (summary only)? Or keep everything and let DB handle it?

**2. Multi-tab.** Same session in two tabs? Simplest: last connection wins, first disconnected.

**3. Workspace on end.** Keep on disk? Snapshot to S3 (infrastructure exists)? Delete after N days?
