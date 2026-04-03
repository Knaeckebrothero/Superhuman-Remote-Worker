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
| Backend | `workspace.backend` | enum | `local` | Admin |

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
| Model preset | Chip row | Quick-select common model combos from `env.js` |
| Strategic model | Dropdown | All models from `env.js`, shows resolved default |
| Tactical model | Dropdown | All models from `env.js`, shows resolved default |

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
| Strategic temperature | Slider 0-2, step 0.1, shows resolved value |
| Tactical temperature | Slider 0-2, step 0.1, shows resolved value |
| Strategic reasoning | Dropdown (adapts to model: none/minimal/low/medium/high/xhigh) |
| Tactical reasoning | Dropdown (adapts to model) |
| Strategic multimodal | Toggle |
| Tactical multimodal | Toggle |
| Top-p | Number input (usually left at auto) |
| Top-k | Number input (usually left at auto) |
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
| Backend | Dropdown (local, remote, container) |

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

Session creation becomes a full page (not a dialog popup). The layout is simpler than job creation: fewer fields, horizontal tabs instead of vertical, no instructions tab.

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
| Model | Dropdown + custom input | From `env.js`, not hardcoded. Single model (sessions don't have strategic/tactical phases) |

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

---

## Config Resolution Chain

Both job and session creation use the same resolution logic. The form always shows the fully resolved value.

### Jobs

```
User override (form)  >  Project override  >  Expert config  >  Framework defaults (defaults.yaml)
```

### Sessions

```
User override (form)  >  User persistent_agent settings  >  Expert config  >  Persistent defaults (persistent_defaults.yaml)
```

The `resolveDefault(path)` function walks this chain and returns the first non-undefined value. Every form field calls this on init to display the real effective value.

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

### UX

- **Load**: Dropdown in the header area. Selecting a preset populates all matching fields. The user can still modify individual fields after loading.
- **Save**: "Save as preset" button. Captures the current form state (only fields that differ from the resolved defaults). Names the preset, optionally adds a description.
- **Scope filtering**: Job creation only shows presets with scope `job` or `any`. Session creation only shows `session` or `any`.
- **Model presets coexist**: The existing `env.js` model presets (chip row) are orthogonal — they only set strategic + tactical model. Settings presets capture the full form state. Both surfaces remain.

---

## Shared Component Architecture

```
AgentSettingsComponent
├── ExecutionGroupComponent        (autonomy, scholar, critic, memory)
│   └── mode: 'job' shows autonomy, 'session' shows permission_mode
├── ModelGroupComponent            (preset chips, strategic/tactical dropdowns)
│   └── mode: 'session' shows single model dropdown instead
├── ToolsGroupComponent            (category toggles)
│   └── mode: 'session' also shows knowledge + git toggles
├── DatasourcesGroupComponent      (checkbox list, hidden if empty)
├── InstructionsComponent          (textarea, clear, reset) — job only
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
- Takes `resolvedDefaults` as input (the merged config from the resolution chain)
- Emits `overrideChange` events when the user modifies a field
- Tracks which fields are modified for the modification indicator system
- Supports `reset(field)` to revert individual fields

The host component (`JobCreateComponent` or `SessionsPageComponent`) composes these and feeds them the right resolved defaults based on the selected expert, project, and user settings.

---

## What's Not in the UI

These settings exist in the config system but are intentionally excluded from the creation forms. They're set via expert configs or infrastructure:

| Setting | Reason for exclusion |
|---------|---------------------|
| `agent_id`, `display_name`, `description`, `icon`, `color`, `tags` | Expert metadata, not user-configurable |
| `instruction_files` | Expert-defined, not per-job overridable in UI |
| `connections.postgres` | Infrastructure setting |
| `workspace.container.*`, `workspace.remote.*` | Infrastructure, set by deployment |
| `llm.api_key`, `llm.base_url` (per-phase) | Security-sensitive, set by environment |
| `research.proxy.host`, `research.proxy.port` | Infrastructure, set by deployment |
| `claude_code.model` | Set by environment |
| `memory.storage` | Always postgres, not configurable |
| `memory.observer_model`, `memory.observer_base_url` | Infrastructure |
| `context_management.summarization_template` | Expert-defined |
| `limits.response_validation.max_tag_repetitions` etc. | Safety guardrails, not user-facing |
| `communication.max_message_length`, `communication.allowed_recipients` | System defaults, not per-job |
| `browser.timeout` | Reasonable default, never needs changing |
| Individual tool arrays (`tools.workspace: [read_file, ...]`) | Category toggles are sufficient; individual tools are expert-level |

---

## Migration from Current UI

| Current surface | Becomes | Notes |
|----------------|---------|-------|
| "Advanced Options" expandable | Settings tab (common options) + Advanced tab (rare options) | No longer a single dumping ground |
| "Config Override Editor" expandable | Read-only "View resolved config" in Advanced tab | Editing happens through the form, not raw JSON |
| `buildConfigOverride()` diffing logic | `resolveDefault()` + per-field dirty tracking | Simpler, more predictable |
| Hardcoded model lists (sessions) | `env.js` model source (shared with jobs) | Single source of truth |
| Session dialog popup | Full session creation page | Routable, not a modal |
| Global persistent agent settings page | Unchanged — provides default values for session creation | Resolution chain reads from here |
