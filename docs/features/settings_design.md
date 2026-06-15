# Agent Settings Design

Concrete placement of every configurable setting across job creation, session creation, and global user settings. This document answers the open questions from `job_settings_overhaul.md` and serves as the implementation spec for the shared `AgentSettingsComponent`.

## Open Questions from the Overhaul Doc (Resolved)

| Question | Decision | Rationale |
|----------|----------|-----------|
| Vertical tabs vs. accordion vs. horizontal tabs? | **Vertical tabs** for job creation (3+ tabs, enough horizontal space). **Horizontal tabs** for session creation (2 tabs, simpler layout). | Vertical tabs scale better for 3+ categories (VS Code, JetBrains pattern). Session creation is simpler and doesn't need the sidebar weight. |
| Presets: type-specific or type-agnostic? | **Type-agnostic** with a `scope` field (`job`, `session`, `any`). | A "fast GPT-5.4 research" preset is useful for both jobs and sessions. The `scope` field lets the UI filter relevance without enforcing rigid separation. |
| Config editor: keep or remove? | **Remove as a standalone surface.** Add a read-only "View resolved config" JSON viewer in the Advanced tab for debugging. | Two editing surfaces that control the same object is the root problem. One surface (the form), one escape hatch (read-only JSON). |

---

## Design Principles

1. **Show the real value.** Every field displays the resolved effective value from the config chain. No nulls, no `(default)` mystery labels, no zeros for unset temperatures. If the user hasn't touched it, they see what the agent will actually get.

2. **Two levels of disclosure, no more.** Primary settings visible by default. One level of "Advanced" expansion. No nested accordions, no modal-behind-modal. Research (NN/G) confirms users lose context beyond two levels.

3. **Modified = visible.** Any setting that differs from its resolved default gets a left-edge accent bar (VS Code pattern) and a per-field reset button. A summary line ("3 settings modified") at the top of each tab gives at-a-glance status.

4. **Creation is fast, configuration is thorough.** The creation flow optimizes for speed — required fields first, everything else optional. Full configuration happens in the tabbed settings area below.

5. **Shared components, contextual composition.** Each settings group is a standalone component. Job and session creation compose different subsets of the same components, ensuring consistency.

---

## Settings Inventory

Every configurable setting in the system, organized by concern. The "Audience" column indicates who typically changes this setting:

- **Everyone** — Most users will consider this setting
- **Power** — Advanced users or specific workflows
- **Admin** — Infrastructure / deployment-level, rarely changed per-job

### Identity & Context

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Description | `description` | string (required) | — | Everyone |
| Opening message | `kickoff_message` | string | empty | Everyone |
| Title (sessions) | `title` | string | "Untitled Session" | Everyone |
| Expert / Config | `config_name` | enum | `defaults` (job) / `interactive` (session) | Everyone |
| Project | `project_id` / `project_ids` | select / multi-select | user's default | Everyone |
| Files | `upload_id` | file upload | — | Everyone |
| Priority | `priority` | number 0-10 | 5 | Everyone |

### Execution Control

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Autonomy level | `autonomy` | enum | `review` | Everyone |
| Permission mode | `interactive.permission_mode` | enum | `supervised` | Everyone |
| Scholar (pre-research) | `scholar.enabled` | boolean | true | Everyone |
| Critic (verification) | `verification.enabled` | boolean | true | Everyone |
| Critic max rounds | `verification.max_rounds` | number | 5 | Power |
| Project memory | `memory.project_scoped` | boolean | true | Power |
| Delegation | `delegation.enabled` | boolean | false | Power |
| Delegation max depth | `delegation.max_depth` | number 1-3 | 1 | Power |
| Delegation timeout | `delegation.default_timeout` | number (sec) | 7200 | Admin |
| Communication | `communication.enabled` | boolean | true | Power |
| Communication blocking timeout | `communication.blocking_timeout_hours` | number | 24 | Admin |

### Model & Inference

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Model preset | (composite) | chip selector | — | Everyone |
| Strategic model | `llm.strategic.model` | dropdown | from expert | Everyone |
| Tactical model | `llm.tactical.model` | dropdown | from expert | Everyone |
| Strategic temperature | `llm.strategic.temperature` | slider 0-2 | from expert | Power |
| Tactical temperature | `llm.tactical.temperature` | slider 0-2 | from expert | Power |
| Strategic reasoning | `llm.strategic.reasoning_level` | enum | from expert | Power |
| Tactical reasoning | `llm.tactical.reasoning_level` | enum | from expert | Power |
| Strategic multimodal | `llm.strategic.multimodal` | boolean | false | Power |
| Tactical multimodal | `llm.tactical.multimodal` | boolean | false | Power |
| Top-p | `llm.top_p` | number 0-1 | null (auto) | Admin |
| Top-k | `llm.top_k` | number | null (auto) | Admin |
| Max output tokens | `llm.max_output_tokens` | number | null (auto) | Admin |
| Parallel tool calls | `llm.parallel_tool_calls` | boolean | false | Admin |
| LLM timeout | `llm.timeout` | number (sec) | 600 | Admin |
| LLM max retries | `llm.max_retries` | number | 3 | Admin |
| LLM provider | `llm.provider` | enum | null (auto) | Admin |
| LLM base URL | `llm.base_url` | string | null | Admin |

### Tools

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Research tools | `tools.research` | toggle (category) | from expert | Everyone |
| Citation tools | `tools.citation` | toggle (category) | from expert | Everyone |
| Document tools | `tools.document` | toggle (category) | from expert | Everyone |
| Coding tools | `tools.coding` | toggle (category) | from expert | Everyone |
| Knowledge tools | `tools.knowledge` | toggle (category) | from expert | Power |
| Git tools | `tools.git` | toggle (category) | from expert | Power |
| Individual tool selection | `tools.<category>` | array | from expert | Admin |

### Data Sources

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Datasources | `datasource_ids` | checkbox list | [] | Everyone |

### Instructions

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Custom instructions | `instructions` | textarea (markdown) | from expert | Everyone |
| Instruction files | `instruction_files` | array | from expert | Admin |

### Communication (user settings page)

These settings exist on the global user settings page (`settings.component.ts`) and affect how agents communicate with the user. They are not per-job/session — they apply globally. Included here for completeness.

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Async reply delivery | `communication.delivery.async_reply` | enum | `immediate_interrupt` | Power |
| Urgent override | `communication.delivery.urgent_override` | boolean | true | Power |
| Email channel | `communication.channels.email` | boolean | false | Power |
| Cockpit channel | `communication.channels.cockpit` | boolean | true | Power |
| Ntfy channel | `communication.channels.ntfy` | boolean | false | Power |
| Slack webhook | `communication.channels.slack_webhook` | boolean | false | Power |
| Discord webhook | `communication.channels.discord_webhook` | boolean | false | Power |
| Quiet hours enabled | `communication.quiet_hours.enabled` | boolean | false | Power |
| Quiet hours start | `communication.quiet_hours.start` | time string | null | Power |
| Quiet hours end | `communication.quiet_hours.end` | time string | null | Power |
| Quiet hours timezone | `communication.quiet_hours.timezone` | string | null | Power |

These are **not** part of the `AgentSettingsComponent` — they remain on the global settings page. They're listed here so the settings inventory is complete.

### Session-Specific

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Greeting | `interactive.greeting` | text | "Hello! I'm ready..." | Power |
| Idle timeout | `interactive.idle_timeout_minutes` | number (min) | 30 | Power |
| Command allowlist | `command_allowlist` | comma-separated | null (all allowed) | Power |
| Claude Code auto-start | `shell.auto_start_claude_code` | boolean | false | Power |

### Workspace (rarely user-facing)

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Directory structure | `workspace.structure` | string[] | expert default | Admin |
| Max read words | `workspace.max_read_words` | number | 25000 | Admin |
| Max write words | `workspace.max_write_words` | number | 10000 | Admin |
| Git versioning | `workspace.git_versioning` | boolean | true (job) / false (session) | Admin |
| Backend | `workspace.backend` | enum (`remote`, `container`) | `remote` | Admin |

### Limits & Safety (rarely user-facing)

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Message count threshold | `limits.message_count_threshold` | number | 300 | Admin |
| Tool retry count | `limits.tool_retry_count` | number | 3 | Admin |
| Response validation | `limits.response_validation.enabled` | boolean | true | Admin |
| Max content length | `limits.response_validation.max_content_length` | number | 50000 | Admin |
| Progress stall threshold | `limits.progress_stall_threshold` | number | 30 | Admin |
| Max tool calls per phase | `limits.max_tool_calls_per_phase` | number | 200 | Admin |

### Context Management (rarely user-facing)

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Compact on archive | `context_management.compact_on_archive` | boolean | true | Admin |
| Keep recent tool results | `context_management.keep_recent_tool_results` | number | 150 | Admin |
| Keep recent messages | `context_management.keep_recent_messages` | number | 10 | Admin |
| Max summary length | `context_management.max_summary_length` | number | 20000 | Admin |

### Memory Tuning (rarely user-facing)

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Memory enabled | `memory.enabled` | boolean | true | Power |
| Budget tokens | `memory.budget_tokens` | number | 10000 | Admin |
| Observer interval | `memory.observer_interval` | number | 5 | Admin |
| Assembler interval | `memory.assembler_interval` | number | 7 | Admin |
| Default TTL | `memory.default_ttl` | number | 10 | Admin |
| Importance threshold | `memory.importance_threshold` | number 0-1 | 0.3 | Admin |
| Dedup threshold | `memory.dedup_threshold` | number 0-1 | 0.85 | Admin |
| Embedding model | `memory.embedding_model` | string | qwen3-embedding-8b | Admin |

### Auxiliary LLM (rarely user-facing)

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Auxiliary enabled | `auxiliary.enabled` | boolean | true | Admin |
| Auxiliary model | `auxiliary.model` | string | openai/gpt-oss-120b | Admin |
| Auxiliary temperature | `auxiliary.temperature` | number | 0.0 | Admin |
| Memory extraction | `auxiliary.tasks.extract_memories.enabled` | boolean | true | Admin |
| Knowledge curation | `auxiliary.tasks.curate_knowledge.enabled` | boolean | true | Admin |

### Research & Browser (rarely user-facing)

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Proxy enabled | `research.proxy.enabled` | boolean | false | Admin |
| Proxy type | `research.proxy.type` | enum | socks5 | Admin |
| Browser headless | `browser.headless` | boolean | true | Admin |
| Browser use vision | `browser.use_vision` | boolean | false | Admin |

### Shell (rarely user-facing)

| Setting | Key | Type | Default | Audience |
|---------|-----|------|---------|----------|
| Shell mode | `shell.mode` | enum | stateless | Admin |
| Shell sandbox | `shell.sandbox` | boolean | true | Admin |
| Shell timeout | `shell.default_timeout` | number (sec) | 120 | Admin |
| Blocked commands | `shell.blocked_commands` | string[] | [reboot, shutdown, ...] | Admin |
| Sudo action | `shell.sudo_action` | enum | freeze | Admin |

---

## Job Creation Layout

The creation flow has two zones: the **header area** (always visible, contains required fields) and the **settings area** (tabbed, below the header).

### Header Area (Always Visible)

```
┌─────────────────────────────────────────────────────────────┐
│  Create New Job                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Project: [dropdown]           Priority: [1-10 dropdown]    │
│                                                             │
│  Description *                                              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                                                     │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  Opening Message (optional)                                 │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                                                     │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  Expert                                                     │
│  ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐        │
│  │ Dev   │ │Scholar│ │Critic │ │Default│ │Custom │        │
│  └───────┘ └───────┘ └───────┘ └───────┘ └───────┘        │
│                                                             │
│  Files                                                      │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  Drop files here or click to browse                 │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  Preset: [dropdown ▾] ─── or ─── [Save current as preset]  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

Fields in the header:
- **Project** — dropdown, defaults to user's default project
- **Priority** — dropdown (1-10), defaults to 5
- **Description** — required textarea
- **Opening message** — optional textarea
- **Expert** — card grid (existing pattern)
- **Files** — drag-and-drop zone
- **Preset** — dropdown to load a saved preset, plus "Save as preset" action

### Settings Area (Vertical Tabs)

Three tabs: **Settings**, **Instructions**, **Advanced**.

#### Tab 1: Settings (default tab)

The most commonly adjusted options. Grouped into logical sections with light dividers.

```
┌──────────────┐ ┌──────────────────────────────────────────┐
│              │ │                                          │
│  ● Settings  │ │  Execution ─────────────────────────     │
│              │ │                                          │
│  Instructions│ │  Autonomy        [review ▾]              │
│              │ │  Scholar          [■ on]                  │
│  Advanced    │ │  Critic           [■ on]  Rounds: [5 ▾]  │
│              │ │  Project memory   [■ on]                  │
│              │ │                                          │
│              │ │  Model ──────────────────────────────     │
│              │ │                                          │
│              │ │  Preset   [───────────] [chip] [chip]    │
│              │ │  Strategic [openai/gpt-oss-120b ▾]       │
│              │ │  Tactical  [openai/gpt-oss-120b ▾]       │
│              │ │                                          │
│              │ │  Tools ──────────────────────────────     │
│              │ │                                          │
│              │ │  [■] Research  [■] Citation               │
│              │ │  [■] Document  [■] Coding                 │
│              │ │                                          │
│              │ │  Data Sources ───────────────────────     │
│              │ │                                          │
│              │ │  [■] PG Main (postgresql, RO)             │
│              │ │  [■] Neo4j Knowledge (neo4j)              │
│              │ │                                          │
│              │ │                          3 settings modified │
│              │ │                                          │
└──────────────┘ └──────────────────────────────────────────┘
```

**Execution group:**
| Setting | Control | Shows |
|---------|---------|-------|
| Autonomy | Dropdown | `full`, `review`, `partial`, `guided`, `dependent` |
| Scholar | Toggle | On/off |
| Critic | Toggle + dropdown | On/off, rounds (1, 3, 5, 10, unlimited) |
| Project memory | Toggle | On/off (only shown if project selected and project has memory) |

**Model group:**
| Setting | Control | Shows |
|---------|---------|-------|
| Model preset | Chip row | Quick-select common model combos from `environment.modelPresets` (env.js) |
| Strategic model | Dropdown with optgroups | All models from `environment.models` (env.js), grouped by provider. Shows resolved default |
| Tactical model | Dropdown with optgroups | Same source. Shows resolved default |

> **Note:** Models are loaded from `window.env.models` at runtime (set via `env.js` injection, not hardcoded). If env is not configured, dropdowns show only "Default". The existing `environment.ts` (lines 41-42) already reads these — no change needed for the model source.

**Tools group:**
| Setting | Control | Shows |
|---------|---------|-------|
| Research | Toggle | Category on/off |
| Citation | Toggle | Category on/off |
| Document | Toggle | Category on/off |
| Coding | Toggle | Category on/off |

**Data Sources group:**
| Setting | Control | Shows |
|---------|---------|-------|
| (each datasource) | Checkbox | Name, type icon, RO badge, description |

Only shown if datasources exist. If none, the section is hidden entirely.

#### Tab 2: Instructions

```
┌──────────────┐ ┌──────────────────────────────────────────┐
│              │ │                                          │
│  Settings    │ │  Custom Instructions                     │
│              │ │                                          │
│  ● Instruct. │ │  ┌────────────────────────────────────┐  │
│              │ │  │                                    │  │
│  Advanced    │ │  │  (monospace, markdown, 16 rows)    │  │
│              │ │  │                                    │  │
│              │ │  │                                    │  │
│              │ │  │                                    │  │
│              │ │  └────────────────────────────────────┘  │
│              │ │                                          │
│              │ │  [Clear]  [Reset to expert default]      │
│              │ │                                          │
│              │ │  ⓘ Builder AI can edit instructions      │
│              │ │    via the job builder chat.             │
│              │ │                                          │
└──────────────┘ └──────────────────────────────────────────┘
```

A single focused surface. The full-height textarea encourages thorough instructions.

#### Tab 3: Advanced

Settings that fewer than 20% of users will change. Organized into collapsible sections (one level of accordion, never nested).

```
┌──────────────┐ ┌──────────────────────────────────────────┐
│              │ │                                          │
│  Settings    │ │  ▶ Inference Parameters                  │
│              │ │  ▶ Delegation                            │
│  Instructions│ │  ▶ Limits & Safety                       │
│              │ │  ▶ Memory Tuning                         │
│  ● Advanced  │ │  ▶ Context Management                    │
│              │ │  ▶ Workspace                             │
│              │ │  ▶ Shell                                 │
│              │ │  ▶ Research & Browser                    │
│              │ │  ▶ Auxiliary LLM                         │
│              │ │                                          │
│              │ │  ─────────────────────────────────────   │
│              │ │  View resolved config (JSON)             │
│              │ │  ┌────────────────────────────────────┐  │
│              │ │  │ { "llm": { "strategic": { ...   │  │
│              │ │  └────────────────────────────────────┘  │
│              │ │                                          │
└──────────────┘ └──────────────────────────────────────────┘
```

Each accordion section expands to show its settings:

**Inference Parameters** (expanded):
| Setting | Control |
|---------|---------|
| Strategic temperature | Slider 0-2, step 0.1, shows resolved value. Note: effective value may differ from expert default when settings matrix applies model-family overrides |
| Tactical temperature | Slider 0-2, step 0.1, same caveat |
| Strategic reasoning | Dropdown (options computed by `getReasoningOptions()` based on selected model — see Reasoning Options Logic section) |
| Tactical reasoning | Dropdown (same logic) |
| Strategic multimodal | Toggle |
| Tactical multimodal | Toggle |
| Top-p | Number input (usually left at auto; settings matrix may set per model family) |
| Top-k | Number input (usually left at auto; settings matrix may set per model family) |
| Max output tokens | Number input (usually left at auto) |
| Parallel tool calls | Toggle |

**Delegation:**
| Setting | Control |
|---------|---------|
| Enabled | Toggle |
| Max depth | Dropdown (1, 2, 3) — only when enabled |
| Default timeout | Number input (seconds) — only when enabled |
| Allowed configs | Multi-select — only when enabled |

**Limits & Safety:**

> **Note:** Context-related limits (`model_max_context_tokens`, `context_threshold_tokens`, `summarization_safe_limit`, `summarization_chunk_size`, `message_count_min_tokens`) are **always derived from the settings matrix** per model family and cannot be overridden by experts or users. They are intentionally excluded from the UI. The limits below are the user-configurable execution limits.

| Setting | Control |
|---------|---------|
| Message count threshold | Number input |
| Tool retry count | Number input |
| Response validation enabled | Toggle |
| Max content length | Number input — only when validation on |
| Progress stall threshold | Number input |
| Max tool calls per phase | Number input |

**Memory Tuning:**
| Setting | Control |
|---------|---------|
| Memory enabled | Toggle |
| Budget tokens | Number input |
| Observer interval | Number input |
| Assembler interval | Number input |
| Default TTL (days) | Number input |
| Importance threshold | Slider 0-1 |
| Dedup threshold | Slider 0-1 |
| Embedding model | Text input |

**Context Management:**
| Setting | Control |
|---------|---------|
| Compact on archive | Toggle |
| Keep recent tool results | Number input |
| Keep recent messages | Number input |
| Max summary length | Number input |

**Workspace:**
| Setting | Control |
|---------|---------|
| Directory structure | Tag/chip input |
| Max read words | Number input |
| Max write words | Number input |
| Git versioning | Toggle |
| Backend | Dropdown (`remote` default or `container`). `local` has been removed entirely — the agent never operates on its own filesystem. The orchestrator always injects `backend=remote` with workspace credentials from K8s ContainerProvisioner or DockerProvisioner |

**Shell:**
| Setting | Control |
|---------|---------|
| Mode | Dropdown (stateless, persistent) |
| Sandbox | Toggle |
| Default timeout | Number input (seconds) |
| Blocked commands | Tag/chip input |
| Sudo action | Dropdown (freeze, block, allow) |

**Research & Browser:**
| Setting | Control |
|---------|---------|
| Proxy enabled | Toggle |
| Proxy type | Dropdown — only when proxy on |
| Browser headless | Toggle |
| Browser use vision | Toggle |

**Auxiliary LLM:**
| Setting | Control |
|---------|---------|
| Enabled | Toggle |
| Model | Dropdown |
| Temperature | Slider 0-2 |
| Extract memories | Toggle — only when aux enabled |
| Curate knowledge | Toggle — only when aux enabled |
| Assemble memories | Toggle — only when aux enabled |

**View resolved config:** Read-only JSON viewer at the bottom. Shows the full merged config the agent will receive. Collapses by default. Replaces the old config editor — no manual JSON editing.

### Footer

```
┌─────────────────────────────────────────────────────────────┐
│                           [Reset All]  [Create Job]         │
└─────────────────────────────────────────────────────────────┘
```

---

## Session Creation Layout

Session creation becomes a full page (not a dialog popup). This requires a new route in `app.routes.ts` (currently session creation is a dialog overlay in `sessions-page.component.ts`, not routable). The layout is simpler than job creation: fewer fields, horizontal tabs instead of vertical, no instructions tab.

**Current state of session creation** (`sessions-page.component.ts`, 745 lines):
- 5 fields only: title, config (hardcoded 4-option dropdown), model (hardcoded 10-model list), projects (multi-select), permission mode
- Models are NOT from `env.js` — they're hardcoded at lines 98-113
- Config names are NOT from the API — hardcoded at lines 88-93 (interactive, defaults, developer, scholar)
- Backend accepts `temperature` in `ThreadCreateRequest` (line 7773) but the UI doesn't expose it
- Form state is plain class properties, not signals

**Session lifecycle (backend):** When a session is created, the orchestrator provisions a workspace and assigns an agent via one of two paths:
- **K8s mode:** Creates a dedicated agent pod per thread (PersistentProvisioner)
- **Docker Compose pool mode:** Finds an idle persistent agent from a static pool and attaches the thread via `POST /session/attach` (`persistent_app.py`). The agent can later detach (`POST /session/detach`) and be reused for another session. `config_override` and `project_ids` are passed at attach time. Pool agents report `available` (idle) or `ready` (attached) status via heartbeat. `MAX_SESSIONS_PER_PROCESS` env var triggers container restart after N sessions to guard against state leakage.

This pool mode is transparent to the UI — the session creation form is identical regardless of backend. The `config_override` built by the form reaches the agent through either path.

### Header Area (Always Visible)

```
┌─────────────────────────────────────────────────────────────┐
│  New Session                                                │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Title                                                      │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                                                     │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  Projects                                                   │
│  ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐                   │
│  │Proj A │ │Proj B │ │Proj C │ │  +    │                   │
│  └───────┘ └───────┘ └───────┘ └───────┘                   │
│                                                             │
│  Expert                                                     │
│  ┌────────┐ ┌───────┐ ┌───────┐ ┌───────┐                  │
│  │Interact│ │ Dev   │ │Scholar│ │Default│                  │
│  └────────┘ └───────┘ └───────┘ └───────┘                  │
│                                                             │
│  Preset: [dropdown ▾]                                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

Fields in the header:
- **Title** — text input, defaults to "Untitled Session"
- **Projects** — multi-select chips (existing pattern)
- **Expert** — card grid (same component as job creation, filtered to session-relevant experts)
- **Preset** — dropdown to load a saved preset

No description, no opening message, no file upload — sessions are conversational.

### Settings Area (Horizontal Tabs)

Two tabs: **Settings**, **Advanced**.

```
  ┌──────────┐ ┌──────────┐
  │ Settings │ │ Advanced │
  └──────────┘ └──────────┘
```

#### Tab 1: Settings (default)

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  Permissions ─────────────────────────────────              │
│                                                             │
│  Permission Mode   [supervised ▾]                           │
│  Command Allowlist [pytest*, npm test, git status]          │
│                                                             │
│  Model ──────────────────────────────────────               │
│                                                             │
│  Model     [openai/gpt-oss-120b ▾]                          │
│                                                             │
│  Session ─────────────────────────────────────              │
│                                                             │
│  Idle Timeout (min) [30]                                    │
│  Greeting           [Hello! I'm ready to help...]           │
│  Claude Code        [□ Auto-start on session launch]        │
│                                                             │
│  Tools ──────────────────────────────────────               │
│                                                             │
│  [■] Research  [■] Citation  [■] Document  [■] Coding      │
│  [□] Knowledge [■] Git                                      │
│                                                             │
│  Data Sources ───────────────────────────────               │
│                                                             │
│  [■] PG Main (postgresql, RO)                               │
│  [□] Neo4j Knowledge (neo4j)                                │
│                                                             │
│                                       2 settings modified   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Permissions group:**
| Setting | Control | Notes |
|---------|---------|-------|
| Permission mode | Dropdown | `supervised`, `auto_accept`, `autonomous` |
| Command allowlist | Tag/chip input | Glob patterns for allowed shell commands |

**Model group:**
| Setting | Control | Notes |
|---------|---------|-------|
| Model | Dropdown with optgroups + custom input | From `environment.models` (env.js), **replacing current hardcoded 10-model list** at lines 98-113. Single model (sessions don't have strategic/tactical phases) |

**Session group:**
| Setting | Control | Notes |
|---------|---------|-------|
| Idle timeout | Number input (minutes) | 0 = disabled |
| Greeting | Text input | Custom welcome message |
| Claude Code auto-start | Toggle | Launch Claude Code automatically |

**Tools group:**
| Setting | Control | Notes |
|---------|---------|-------|
| (same as job creation) | Toggles | Category on/off, but also shows Knowledge and Git toggles since sessions use these more often |

**Data Sources group:**
| Setting | Control | Notes |
|---------|---------|-------|
| (same as job creation) | Checkboxes | Only shown if datasources exist |

#### Tab 2: Advanced

Same accordion pattern as job creation, but with session-relevant sections only.

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  ▶ Inference Parameters                                     │
│  ▶ Limits & Safety                                          │
│  ▶ Memory Tuning                                            │
│  ▶ Context Management                                       │
│  ▶ Workspace                                                │
│  ▶ Shell                                                    │
│  ▶ Research & Browser                                       │
│  ▶ Auxiliary LLM                                            │
│                                                             │
│  ─────────────────────────────────────                      │
│  View resolved config (JSON)                                │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ { "llm": { "model": "openai/gpt-oss-120b", ...  │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Inference Parameters** (session-specific — no strategic/tactical split):
| Setting | Control |
|---------|---------|
| Temperature | Slider 0-2 |
| Reasoning level | Dropdown |
| Multimodal | Toggle |
| Top-p | Number input |
| Top-k | Number input |
| Max output tokens | Number input |

The remaining accordion sections (Limits, Memory, Context, Workspace, Shell, Research, Auxiliary) use the same components as job creation. No Delegation section (not applicable to sessions).

### Footer

```
┌─────────────────────────────────────────────────────────────┐
│                           [Cancel]  [Create Session]        │
└─────────────────────────────────────────────────────────────┘
```

---

## Global User Settings (Persistent Agent Section)

The existing settings page stays. It provides user-level defaults that pre-populate the session creation form. When a user opens the session creation page, the Settings tab reads its initial values from here.

```
┌─────────────────────────────────────────────────────────────┐
│  Persistent Agent Defaults                                  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  These defaults apply to all new sessions unless            │
│  overridden during session creation.                        │
│                                                             │
│  Model            [openai/gpt-oss-120b ▾]                   │
│  Config           [interactive ▾]                            │
│  Permission Mode  [supervised ▾]                             │
│  Idle Timeout     [30] minutes                               │
│  Greeting         [Hello! I'm ready to help...]             │
│  Command Allowlist [pytest*, npm test]                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

No changes to what's stored here. The only change is that the session creation form now surfaces these as editable starting values rather than invisible global defaults.

**Additional user-level preferences** (also on the settings page, in the "Preferences" section) that affect jobs via the dispatch pipeline:

| Setting | Key in `users.settings` | Applied at dispatch as |
|---------|------------------------|----------------------|
| Default model | `default_model` | `llm.model` (gap-filler only) |
| Default autonomy | `default_autonomy` | `autonomy` (gap-filler only) |
| Default reasoning | `default_reasoning_level` | `llm.reasoning_level` (gap-filler only) |
| Default auxiliary model | `default_auxiliary_model` | `auxiliary.model` (gap-filler only) |
| Embedding provider | `embedding_provider` | `env_keys` (always applied) |

These are **lowest-priority** — they only fill gaps when the job/project/expert config doesn't set them. They don't appear in the job creation form because they're invisible fallbacks. The "View resolved config" JSON viewer should reflect them.

---

## Config Resolution Chain

Both job and session creation use the same resolution logic. The form always shows the fully resolved value.

### UI Resolution (what the form shows)

#### Jobs

```
User override (form)  >  Project override  >  Expert config  >  Framework defaults (defaults.yaml)
```

#### Sessions

```
User override (form)  >  User persistent_agent settings  >  Expert config  >  Persistent defaults (persistent_defaults.yaml)
```

The `resolveDefault(path)` function walks this chain and returns the first non-undefined value. Every form field calls this on init to display the real effective value.

### Full Runtime Resolution (what the agent actually gets)

After creation, two additional layers are applied before the agent starts. The UI chain is a simplification — the full chain is:

```
AT CREATION (form → API):
  form override  >  project.default_config_override  >  config_name (expert)

AT DISPATCH (orchestrator/main.py:563-850):
  + datasource-driven tool overrides
  + workspace config injection (provisioner-dependent):
    - K8s ContainerProvisioner: dynamic pod, SSH key at /run/secrets/vm-ssh-key
    - DockerProvisioner: static pool assignment, SSH key at /run/secrets/ssh/id_ed25519
    - VMProvisioner: QEMU VM, SSH credentials from VM context
    - context field: provisioner="docker"|"k8s", host, port (variable, not hardcoded 22)
  + user API keys (user > project > env var fallback)
  + user preferences as lowest-priority gap-fillers:
    users.settings.default_model           → llm.model (only if not already set)
    users.settings.default_autonomy        → autonomy (only if not set)
    users.settings.default_reasoning_level → llm.reasoning_level (only if not set)
    users.settings.default_auxiliary_model  → auxiliary.model (only if not set)
    users.settings.embedding_provider      → env_keys

AT AGENT START (src/agent.py → src/core/loader.py):
  + safety guard: WorkspaceConfig rejects workspace.backend=local; agent only accepts backend=remote with SSH credentials
  + $extends chain resolved (expert → defaults.yaml)
  + settings_matrix.yaml applied (model-family inference params):
    - temperature, top_p, top_k per model family (expert explicit values win)
    - limits.* always from matrix (expert cannot override)
  + config_override deep-merged on top
  + env_keys set as os.environ
  + resolved config frozen to DB for resume
```

**Implication for the UI:** For the Settings and Instructions tabs, the UI resolution chain is accurate enough — expert defaults are what users expect to see. For the "View resolved config" JSON viewer in the Advanced tab, consider fetching the full resolved config from a `GET /api/config/resolve` endpoint that applies the settings matrix.

### Settings Matrix

The settings matrix (`config/settings_matrix.yaml`) maps model families to inference parameters. It is the **sole source of truth for context limits** — experts cannot override limits. For LLM params (temperature, top_p), expert explicit values take precedence over the matrix.

Current model families: `default`, `minimax`, `o-series`, `deepseek`, `gemini`, `claude-opus`, `gpt-5`, `gpt-oss`.

When the user changes the model in the UI, the effective temperature may differ from the expert default because a different model family's settings matrix entry applies. The UI does not replicate this logic — it shows the expert default, which is close enough for the form. The JSON viewer should show the true resolved config.

---

## Modification Indicators

When a setting differs from its resolved default:

| Indicator | Where | Purpose |
|-----------|-------|---------|
| Accent left border | Individual field | Immediately identifies which fields were changed |
| Reset button (×) | Individual field, right side | One-click revert to resolved default |
| "N settings modified" | Bottom of each tab | At-a-glance count without scanning every field |
| Tooltip on modified field | Hover | "Modified from default: 0.7" — shows what the default was |

Unmodified fields show no extra UI. The accent bar only appears on change.

---

## Presets

### Data Model

```typescript
interface SettingsPreset {
  id: string;
  user_id: string;
  project_id?: string | null;  // null = user-level; set = project-level (deferred, but column included in schema)
  name: string;
  description?: string | null;
  scope: 'job' | 'session' | 'any';
  settings: PresetSettings;
  created_at: string;
  updated_at: string;
}

interface PresetSettings {
  // Identity (optional — presets don't set description/title)
  config_name?: string;
  priority?: number;

  // Execution
  autonomy?: string;
  enable_scholar?: boolean;
  enable_critic?: boolean;
  critic_max_rounds?: number;
  delegation_enabled?: boolean;

  // Model
  config_override?: Record<string, unknown>;  // Full LLM overrides

  // Tools
  tool_categories?: Record<string, boolean>;  // { research: true, citation: false, ... }

  // Instructions
  instructions?: string;

  // Data
  datasource_ids?: string[];

  // Session-specific
  permission_mode?: string;
  idle_timeout_minutes?: number;
  greeting?: string;
  command_allowlist?: string[];
}
```

### Backend Schema

```sql
CREATE TABLE agent_presets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = user-level preset
    name VARCHAR(255) NOT NULL,
    description TEXT,
    scope VARCHAR(20) NOT NULL DEFAULT 'any',  -- 'job', 'session', 'any'
    settings JSONB NOT NULL DEFAULT '{}',       -- PresetSettings as JSONB
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_preset_user_name UNIQUE (user_id, name)
);

CREATE INDEX idx_presets_user ON agent_presets(user_id);
CREATE INDEX idx_presets_project ON agent_presets(project_id) WHERE project_id IS NOT NULL;
CREATE TRIGGER update_agent_presets_updated_at BEFORE UPDATE ON agent_presets
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

### Backend Endpoints

Follow the existing patterns in `orchestrator/main.py` (user settings at line 9179, project config at line 9536):

```
GET    /api/presets              → list user's presets (filter by ?scope=job|session|any)
POST   /api/presets              → create preset from current settings
GET    /api/presets/{id}         → get preset detail
PUT    /api/presets/{id}         → update preset (owner check: user_id must match)
DELETE /api/presets/{id}         → delete preset (owner check)
```

Project-level preset endpoints are **deferred** but the schema supports them:

```
GET    /api/projects/{id}/presets        → list project presets (deferred)
POST   /api/projects/{id}/presets        → create project preset (deferred)
```

### UX

- **Load**: Dropdown in the header area. Selecting a preset populates all matching fields. The user can still modify individual fields after loading.
- **Save**: "Save as preset" button. Captures the current form state (only fields that differ from the resolved defaults). Names the preset, optionally adds a description.
- **Scope filtering**: Job creation only shows presets with scope `job` or `any`. Session creation only shows `session` or `any`.
- **Model presets coexist**: The existing `env.js` model presets (chip row) are orthogonal — they only set strategic + tactical model. Settings presets capture the full form state. Both surfaces remain.

---

## Shared Component Architecture

### Frontend Implementation Constraints

The cockpit codebase uses these patterns — the shared component must follow them:

- **Angular 21+** standalone components (`standalone: true`, explicit `imports: [...]`, no NgModules)
- **Signals** for all state (`signal()`, `computed()`) — not RxJS-heavy patterns
- **Template-driven forms** with `[(ngModel)]` and `(ngModelChange)` — no reactive forms (FormGroup/FormBuilder)
- **`inject()`** for dependency injection — no constructor parameters
- **Vanilla CSS** with Catppuccin Mocha theme (CSS variables: `--app-bg`, `--panel-bg`, `--text-primary`, `--accent-color`, `--border-color`) — no UI library (no Material, Bootstrap, PrimeNG)
- **Inline styles** via `styles: [...]` in component decorator, scoped to the component
- **No NgModules** — all components are standalone with explicit imports

### Component Tree

```
AgentSettingsComponent
├── ExecutionGroupComponent        (autonomy, scholar, critic, memory)
│   └── mode: 'job' shows autonomy, 'session' shows permission_mode
├── ModelGroupComponent            (preset chips, strategic/tactical dropdowns)
│   └── mode: 'session' shows single model dropdown instead
│   └── includes getReasoningOptions() logic (maps model → available reasoning levels per provider)
├── ToolsGroupComponent            (category toggles)
│   └── mode: 'session' also shows knowledge + git toggles
├── DatasourcesGroupComponent      (checkbox list, hidden if empty)
├── InstructionsComponent          (textarea, clear, reset) — job only
│   └── exposes instructionsContent signal for host to wire to JobArtifactService
└── AdvancedAccordionComponent     (all accordion sections)
    ├── InferenceParamsSection
    ├── DelegationSection          — job only
    ├── LimitsSafetySection
    ├── MemoryTuningSection
    ├── ContextManagementSection
    ├── WorkspaceSection
    ├── ShellSection
    ├── ResearchBrowserSection
    ├── AuxiliaryLlmSection
    └── ResolvedConfigViewer       (read-only JSON)
```

Each sub-component:
- Takes `resolvedDefaults` as input signal (the merged config from the resolution chain)
- Emits `overrideChange` events (via `output()`) when the user modifies a field
- Tracks which fields are modified for the modification indicator system
- Supports `reset(field)` to revert individual fields

The host component (`JobCreateComponent` or `SessionsPageComponent`) composes these and feeds them the right resolved defaults based on the selected expert, project, and user settings.

### Reasoning Options Logic

The `ModelGroupComponent` must include reasoning level logic currently in `job-create.component.ts:1954-2014`. This maps model name to available reasoning levels:

| Model pattern | Available reasoning levels |
|---------------|--------------------------|
| `openrouter/*` prefix | none, minimal, low, medium, high, xhigh |
| `groq/*` prefix | default only (no reasoning support) |
| `gpt-oss*` | none, minimal, low, medium, high, xhigh |
| `claude*`, `gemini*` | default only (no reasoning support) |
| OpenAI (`gpt-*`, `o1*`, `o3*`, `o4*`), DeepSeek, Qwen, Llama | none, low, medium, high |
| No model selected | none, low, medium, high (standard set) |

This logic should be extracted as a pure function so it can be tested independently and potentially aligned with the backend's `detect_model_family()` function.

### Artifact Service Integration

The `InstructionsComponent` must **not** depend on `JobArtifactService` directly — that's a job-creation-specific concern (builder AI streaming edits). Instead:

1. `InstructionsComponent` exposes an `instructionsContent` signal (readable/writable)
2. The host `JobCreateComponent` binds this signal bidirectionally with the artifact service via effects (current pattern at lines 1621-1625 and 1891-1895)
3. `SessionsPageComponent` doesn't use the instructions tab at all, so no wiring needed

---

## What's Not in the UI

These settings exist in the config system but are intentionally excluded from the creation forms. They're set via expert configs, infrastructure, or other UI surfaces:

| Setting | Reason for exclusion |
|---------|---------------------|
| `agent_id`, `display_name`, `description`, `icon`, `color`, `tags` | Expert metadata, not user-configurable |
| `instruction_files` | Expert-defined, not per-job overridable in UI |
| `connections.postgres` | Infrastructure setting |
| `workspace.container.*`, `workspace.remote.*` | Infrastructure, injected by orchestrator at dispatch time. Remote host/port/key_path vary by provisioner: K8s (`/run/secrets/vm-ssh-key`), Docker Compose (`/run/secrets/ssh/id_ed25519`). Port is no longer hardcoded to 22 |
| `llm.api_key`, `llm.base_url` (per-phase) | Security-sensitive, set by environment |
| `research.proxy.host`, `research.proxy.port` | Infrastructure, set by deployment |
| `claude_code.model` | Set by environment |
| `memory.storage` | Always postgres, not configurable |
| `memory.observer_model`, `memory.observer_base_url` | Infrastructure |
| `context_management.summarization_template` | Expert-defined |
| `limits.response_validation.max_tag_repetitions` etc. | Safety guardrails, not user-facing |
| `limits.model_max_context_tokens`, `limits.context_threshold_tokens` etc. | Derived at load from a single base window in `model_config_matrix.yaml` per model family (the threshold/msg-min leaves are fixed fractions of the base). Not a per-job field; the base is the model's true max, overridable per-model via Admin → Models `context_window` |
| `communication.max_message_length`, `communication.allowed_recipients` | System defaults, not per-job |
| `browser.timeout` | Reasonable default, never needs changing |
| Individual tool arrays (`tools.workspace: [read_file, ...]`) | Category toggles are sufficient; individual tools are expert-level |
| `config_upload_id` | Separate mechanism (upload a YAML config file). Stays as-is, unaffected by this overhaul. Available in the existing file upload area for power users who want to upload a full config |
| User-level `default_model`, `default_autonomy`, `default_reasoning_level`, `default_auxiliary_model` | Applied during dispatch as lowest-priority gap-fillers (`orchestrator/main.py:727-794`). Managed on the global user settings page, not per-job. They only matter when the user sets nothing explicitly |
| `communication.delivery.*`, `communication.channels.*`, `communication.quiet_hours.*` | Global user settings, not per-job. Managed on the existing settings page |

---

## Migration from Current UI

| Current surface | Becomes | Notes |
|----------------|---------|-------|
| "Advanced Options" expandable | Settings tab (common options) + Advanced tab (rare options) | No longer a single dumping ground |
| "Config Override Editor" expandable (928 lines) | Read-only "View resolved config" in Advanced tab | Editing happens through the form, not raw JSON. Reuse `setSmartOverride()` and `getDisplayValue()` patterns |
| `buildConfigOverride()` diffing logic (lines 2044-2137) | `resolveDefault()` + per-field dirty tracking | Simpler, more predictable |
| `prefillConfigFromExpert()` (lines 2159-2225) | `resolveDefault()` chain fills all fields automatically | No more null→prefill→reset flow |
| `getReasoningOptions()` (lines 1954-2014) | Pure function in `ModelGroupComponent` | Maps model → reasoning levels per provider |
| Hardcoded model lists (sessions, lines 98-113) | `environment.models` from `env.js` (shared with jobs) | Single source of truth |
| Hardcoded config dropdown (sessions, lines 88-93) | `GET /api/experts` (shared with jobs) | Single source of truth |
| Session dialog popup | Full session creation page | Routable (new route in `app.routes.ts`), not a modal |
| Global persistent agent settings page | Unchanged — provides default values for session creation | Resolution chain reads from here |
| `cockpit/src/assets/schema.json` (711 lines) | Synced from `config/schema.json` (848 lines) | Add build step or CI check; currently 137 lines out of sync |
| Tool categories (4 hardcoded: research, citation, document, coding) | Load dynamically from expert config `tools` keys | Sessions also show knowledge + git categories |
