# Agent Settings — Full Variable Inventory

This document catalogs every configurable setting exposed in the cockpit UI, across all contexts: **Job Creation**, **Session Creation**, and **Runtime Session** (live persistent chat). Use this as a basis for discussing grouping, naming, and UX improvements.

---

## Contexts where settings appear

| Context | Component | Tab layout | Notes |
|---------|-----------|------------|-------|
| **Job Creation** | `AgentSettingsComponent` inside `JobCreateComponent` | Vertical tabs: Settings, Instructions, Advanced | Full config — all settings available |
| **Session Creation** | `AgentSettingsComponent` inside `SessionCreateComponent` | Horizontal tabs: Settings, Advanced | No Instructions tab; single model instead of strategic/tactical |
| **Runtime Session** | Inline settings panel in `PersistentChatComponent` | Flat list (3 fields) | Minimal — only runtime-changeable settings |

---

## Tab: Settings

### Group: Execution

| Setting | Config path | Type | Options/Range | Job | Session | Runtime | Notes |
|---------|-------------|------|---------------|-----|---------|---------|-------|
| Autonomy | `autonomy` | enum | `full`, `review`, `partial`, `guided`, `dependent` | Y | - | - | Controls when agent freezes for human review |
| Permission Mode | `interactive.permission_mode` | enum | `supervised`, `auto_accept`, `autonomous` | - | Y | Y | Controls whether agent asks approval before actions |
| Scholar (pre-research) | `scholar.enabled` | bool | on/off | Y | - | - | Enables pre-research phase |
| Critic (verification) | `verification.enabled` | bool | on/off | Y | - | - | Enables verification sub-job after completion |
| Critic rounds | `verification.max_rounds` | int | 1, 3, 5, 10, 0 (unlimited) | Y | - | - | Number of critic feedback rounds (shown when Critic enabled) |
| Project memory | `memory.project_scoped` | bool | on/off | Y | - | - | Share memories across jobs in the same project (only shown when project has shared memory) |

### Group: Model

| Setting | Config path | Type | Options/Range | Job | Session | Runtime | Notes |
|---------|-------------|------|---------------|-----|---------|---------|-------|
| Preset | (sets strategic + tactical) | chip selector | Dynamic from `/api/models` | Y | - | - | Quick-apply model pair |
| Strategic model | `llm.strategic.model` | enum | Grouped model list from API | Y | - | - | LLM for planning/strategic phases |
| Tactical model | `llm.tactical.model` | enum | Grouped model list from API | Y | - | - | LLM for execution/tactical phases |
| Session model | `llm.model` | enum | Grouped model list from API | - | Y | Y | Single model for interactive sessions |

### Group: Tools

Tool categories are toggle-able on/off. Categories differ by mode:

| Category | Key | Description | Job | Session | Runtime |
|----------|-----|-------------|-----|---------|---------|
| Research | `tools.research` | Web search, paper search, browsing | Y | Y | - |
| Citation | `tools.citation` | Citation and literature management | Y | Y | - |
| Shell | `tools.shell` | Terminal execution (run_command, shell_execute, shell_read) | Y | Y | - |
| Communication | `tools.communication` | Email to job owner (send_message) | Y | Y | - |
| Delegation | `tools.delegation` | Spawn sub-jobs (delegate_work) — with inline depth/timeout params | Y | - | - |
| Knowledge | `tools.knowledge` | Knowledge graph and memory tools | - | Y | - |
| Git | `tools.git` | Git repository operations | - | Y | - |

**Removed**: ~~Document~~ (`chunk_document` — obsolete with 1M context models)

### Group: Data Sources

| Setting | Type | Job | Session | Runtime | Notes |
|---------|------|-----|---------|---------|-------|
| Datasource selection | multi-select checkboxes | Y | Y | - | Select from globally available datasources (PostgreSQL, Neo4j, MongoDB, WebDAV). Hidden when none exist. Each datasource has id, name, type, description. |

---

## Tab: Instructions (Job only)

| Setting | Type | Job | Session | Runtime | Notes |
|---------|------|-----|---------|---------|-------|
| Custom Instructions | textarea (markdown) | Y | - | - | Free-form instructions for the agent. Pre-filled from expert config. Builder AI can edit via streaming. Has Clear and Reset-to-expert actions. |

---

## Tab: Advanced

All settings are inside collapsible accordion sections.

### Section: Inference Parameters

Per-phase in job mode (Strategic + Tactical), single set in session mode.

| Setting | Config path | Type | Range | Job | Session | Runtime | Notes |
|---------|-------------|------|-------|-----|---------|---------|-------|
| Reasoning (strategic) | `llm.strategic.reasoning` | enum | Provider-dependent (None/Low/Medium/High, or +Minimal/X-High) | Y | - | - | Available options depend on model family |
| Temperature (strategic) | `llm.strategic.temperature` | float slider | 0–2, step 0.1 | Y | - | - | |
| Multimodal (strategic) | `llm.strategic.multimodal` | bool | on/off | Y | - | - | Enable image processing |
| Reasoning (tactical) | `llm.tactical.reasoning` | enum | Same as strategic | Y | - | - | |
| Temperature (tactical) | `llm.tactical.temperature` | float slider | 0–2, step 0.1 | Y | - | - | |
| Multimodal (tactical) | `llm.tactical.multimodal` | bool | on/off | Y | - | - | |
| Reasoning (session) | `llm.reasoning` | enum | Provider-dependent | - | Y | - | |
| Temperature (session) | `llm.temperature` | float slider | 0–2, step 0.1 | - | Y | Y | Also available at runtime |
| Multimodal (session) | `llm.multimodal` | bool | on/off | - | Y | - | |
| Top-p | `llm.top_p` | float | 0–1, step 0.05 | Y | Y | - | Shared (not per-phase) |
| Top-k | `llm.top_k` | int | 0+ | Y | Y | - | Shared |
| Max output tokens | `llm.max_output_tokens` | int | 1+, step 100 | Y | Y | - | Shared |
| Parallel tool calls | `llm.parallel_tool_calls` | bool | on/off | Y | Y | - | Shared |

### Section: Delegation (Job only)

| Setting | Config path | Type | Range | Job | Session | Runtime | Notes |
|---------|-------------|------|-------|-----|---------|---------|-------|
| Enable delegation | `delegation.enabled` | bool | on/off | Y | - | - | Allow agent to spawn sub-jobs |
| Max depth | `delegation.max_depth` | int | 1, 2, 3 | Y | - | - | Shown when delegation enabled |
| Default timeout (s) | `delegation.default_timeout` | int | 60+ | Y | - | - | Shown when delegation enabled |

### Section: Limits & Safety

| Setting | Config path | Type | Range | Job | Session | Runtime | Notes |
|---------|-------------|------|-------|-----|---------|---------|-------|
| Message count threshold | `limits.message_count_threshold` | int | 1+ | Y | Y | - | Max messages before compaction |
| Tool retry count | `limits.tool_retry_count` | int | 0–10 | Y | Y | - | Retries on tool failure |
| Progress stall threshold | `limits.progress_stall_threshold` | int | 1+ | Y | Y | - | Steps without progress before stuck detection |
| Max tool calls per phase | `limits.max_tool_calls_per_phase` | int | 1+ | Y | Y | - | Hard cap per phase |

### Section: Memory Tuning

| Setting | Config path | Type | Range | Job | Session | Runtime | Notes |
|---------|-------------|------|-------|-----|---------|---------|-------|
| Memory enabled | `memory.enabled` | bool | on/off | Y | Y | - | Toggle RecallStore memory |
| Budget tokens | `memory.budget` | int | 0+, step 1000 | Y | Y | - | Token budget for memory assembly |

### Section: Context Management

| Setting | Config path | Type | Range | Job | Session | Runtime | Notes |
|---------|-------------|------|-------|-----|---------|---------|-------|
| Compact on archive | `context.compact_on_archive` | bool | on/off | Y | Y | - | Compact context at phase boundaries |
| Keep recent tool results | `context.keep_recent_tool_results` | int | 0+ | Y | Y | - | Number of recent tool results to preserve |
| Keep recent messages | `context.keep_recent_messages` | int | 0+ | Y | Y | - | Number of recent messages to preserve |

### Section: Workspace

| Setting | Config path | Type | Range | Job | Session | Runtime | Notes |
|---------|-------------|------|-------|-----|---------|---------|-------|
| Max read words | `workspace.max_read_words` | int | 0+, step 1000 | Y | Y | - | Word limit for file reads |
| Max write words | `workspace.max_write_words` | int | 0+, step 1000 | Y | Y | - | Word limit for file writes |
| Git versioning | `workspace.git_versioning` | bool | on/off | Y | Y | - | Auto-commit workspace changes |

### Section: Shell

| Setting | Config path | Type | Range | Job | Session | Runtime | Notes |
|---------|-------------|------|-------|-----|---------|---------|-------|
| Mode | `shell.mode` | enum | `stateless`, `persistent` | Y | Y | - | Shell execution mode |
| Sandbox | `shell.sandbox` | bool | on/off | Y | Y | - | Run commands in sandboxed env |
| Default timeout (s) | `shell.timeout` | int | 1+ | Y | Y | - | Default command timeout |
| Sudo action | `shell.sudo_action` | enum | `freeze`, `block`, `allow` | Y | Y | - | How to handle sudo commands |

### Section: Research & Browser

| Setting | Config path | Type | Range | Job | Session | Runtime | Notes |
|---------|-------------|------|-------|-----|---------|---------|-------|
| Proxy enabled | `research.proxy_enabled` | bool | on/off | Y | Y | - | Route browser through proxy |
| Browser headless | `browser.headless` | bool | on/off | Y | Y | - | Run browser in headless mode |
| Browser use vision | `browser.use_vision` | bool | on/off | Y | Y | - | Enable vision-based browser interaction |

### Section: Auxiliary LLM

| Setting | Config path | Type | Range | Job | Session | Runtime | Notes |
|---------|-------------|------|-------|-----|---------|---------|-------|
| Auxiliary LLM enabled | `auxiliary.enabled` | bool | on/off | Y | Y | - | Enable background LLM for async tasks |
| Model | `auxiliary.model` | text input | Free-form model ID | Y | Y | - | Shown when enabled |
| Temperature | `auxiliary.temperature` | float slider | 0–2, step 0.1 | Y | Y | - | Shown when enabled |

### Section: Session (Session only)

| Setting | Config path | Type | Range | Job | Session | Runtime | Notes |
|---------|-------------|------|-------|-----|---------|---------|-------|
| Idle timeout (min) | `interactive.idle_timeout` | int | 0+ (0 = disabled) | - | Y | - | Auto-disconnect after inactivity |
| Greeting | `interactive.greeting` | text | Free-form | - | Y | - | Initial message shown to user |
| Auto-start Claude Code | `interactive.claude_code_auto_start` | bool | on/off | - | Y | - | Launch Claude Code IDE on session start |

### Resolved Config Viewer (read-only)

Shows the full merged config as JSON. Available in both Job and Session Advanced tabs.

---

## Runtime Session Settings (persistent-chat panel)

These are the only settings changeable during an active session, sent via WebSocket `config.update`:

| Setting | Signal | Type | Notes |
|---------|--------|------|-------|
| Permission Mode | `chat.permissionMode()` | enum | `supervised`/`auto_accept`/`autonomous` |
| Model | `chat.modelName()` | enum | Grouped model list from `ModelService` |
| Temperature | `chat.temperature()` | float slider | 0–2, step 0.1 |

---

## Summary counts

| | Job | Session (create) | Session (runtime) |
|---|---|---|---|
| **Settings tab** | ~10 fields | ~3 fields | - |
| **Instructions tab** | 1 textarea | - | - |
| **Advanced tab** | ~28 fields across 8 sections | ~22 fields across 7 sections | - |
| **Runtime panel** | - | - | 3 fields |
| **Total** | ~39 | ~25 | 3 |

---

## Non-settings fields (outside AgentSettingsComponent)

These are set on the parent form, not inside the settings component:

### Job Creation
- Title (text)
- Description (textarea)
- Expert selector (card grid)
- Project selector (dropdown)
- Priority (enum: Low/Normal/High)
- Builder AI chat (for iterating on instructions)

### Session Creation
- Title (text)
- Projects (multi-select chips)
- Expert selector (card grid)

---

## Deep Audit: Tools, Datasources, Delegation, Experts

### Tool Inventory (83 tools, 17 registry categories)

#### System tools (always-on, not toggleable — by design)

Removing these cripples the agent. They should never appear in the UI.

| Category | Count | Purpose |
|----------|-------|---------|
| **workspace** | 13 | File I/O (read, write, edit, list, search, copy, move, delete, etc.) |
| **core** | 6 | Phase/todo lifecycle (next_phase_todos, todo_complete, job_complete, etc.) — excluded in persistent mode |
| **session_task** | 3 | task_add, task_complete, task_list — force-injected in persistent sessions |
| **evaluation** | 2 | approve_job, return_job_with_feedback — injected for critic sub-jobs only |

#### User-facing tool categories (currently in UI)

| Category | Count | What they actually do | Job UI | Session UI |
|----------|-------|-----------------------|--------|------------|
| **research** | 10 | Web search (Tavily), academic papers (arXiv, Semantic Scholar), browser automation (Playwright+OpenAI). All tactical-only. | Y | Y |
| **citation** | 11 | Citation/source management via CitationEngine: create, edit, annotate, tag, search, bibliography. Both phases. | Y | Y |
| **document** | 1 | Just `chunk_document` — splits PDF/DOCX/TXT into chunks. **Thin category.** | Y | Y |
| **coding** | 3 | `run_command`, `shell_execute`, `shell_read`. General-purpose terminal via tmux. **Misnamed — not coding-specific.** | Y | Y |
| **knowledge** | 10 | Project knowledge base (system Neo4j + pgvector): write, read, search, relationships, export. Both phases. | **NO** | Y |
| **git** | 5 | Read-only git inspection (log, show, diff, status, tags). Git writes go through shell. | **NO** | Y |

#### Datasource-injected categories (dual-mode system)

**Key design**: `read_only` is a project-level setting (on `project_datasources` junction table). One datasource can be read-only in project A and read-write in project B.

| Category | Read-only mode (tools) | Read-write mode (CLI) | Issue |
|----------|------------------------|-----------------------|-------|
| **sql** (PostgreSQL) | sql_query, sql_schema | No tools — injects PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE, expects `psql` | **psql not in agent Docker image** |
| **graph** (Neo4j) | execute_cypher_query, get_database_schema | No tools — injects NEO4J_URI/USERNAME/PASSWORD, expects `cypher-shell` | **cypher-shell not in agent Docker image** |
| **mongodb** | mongo_query, mongo_aggregate, mongo_schema | No tools — injects MONGOSH_URI, expects `mongosh` | **mongosh not in agent Docker image** |
| **cloud** (WebDAV) | cloud_list, cloud_read, cloud_info | Always uses tools (adds cloud_write, cloud_delete). No CLI equivalent. | `cloud:` key missing from defaults.yaml |

#### Hidden categories (no UI toggle)

| Category | Count | How it works |
|----------|-------|--------------|
| **communication** | 1 | `send_message` — emails job owner. Controlled by `communication.enabled` config flag. Always on by default. |
| **orchestrator** | 8 | Job lifecycle management (create, list, get, approve, resume, cancel, pause, get_file). Force-injected in persistent sessions, ignores config. |
| **delegation** | 1 | `delegate_work` — **PLACEHOLDER, not implemented.** Registry entry has `placeholder: True`. No source code exists. |

#### Dead code

- `claude_code` tool: defined in `src/tools/coding/claude_code.py` but never imported into registry. Should be removed.

---

### Datasource Lifecycle

```
Cockpit: user selects datasources → datasource_ids[]
  ↓
Orchestrator: clones global ds as job-scoped → resolve one per type (job > project > global)
  ↓
_build_datasource_tool_override():
  ├─ read_only=true  → tools_override[category] = read-only tool list
  ├─ read_only=false + webdav → tools_override[category] = full tool list
  └─ read_only=false + managed → tools_override[category] = [] (CLI mode)
  ↓
Agent: _setup_job_tools()
  ├─ read-write managed → inject typed env vars, skip tool connection
  ├─ read-only managed → create driver connection, store in ToolContext
  ├─ generic → inject env vars from credentials
  └─ repository → git clone into ./repos/{slug}/
  ↓
workspace.md gets datasource index + KB gets retrieval-optimized notes
```

**Critical gap**: CLI clients (psql, cypher-shell, mongosh) are NOT in `Dockerfile.agent`. Read-write mode only works when workspace is a remote VM with these preinstalled. No dynamic install logic exists and agent runs as non-root.

---

### Delegation / Sub-Agent System (5 mechanisms)

| Mechanism | Trigger | Workspace | Suspends? | Status |
|-----------|---------|-----------|-----------|--------|
| **Scholar** | Orchestrator, pre-job | Git worktree on same VM | Parent waits | Working |
| **Critic** | Orchestrator, post-job | Git worktree on same VM | Parent waits | Working |
| **delegate_work** | Agent tool call | Git worktrees, squash-merge back | Parent suspends | **PLACEHOLDER** |
| **Orchestrator tools** (8) | Persistent agent | Independent workspace | Fire-and-forget | Working |
| **claude_code** | Agent tool call | Same workspace (subprocess) | No | **DEAD CODE** |

Scholar/Critic/delegate_work = parent-child (shared workspace via worktrees). Orchestrator tools = independent dispatch. These should stay separate.

---

### Expert × Tool Category Matrix

| Category | defaults | critic | curator | developer | scholar | interactive |
|----------|----------|--------|---------|-----------|---------|-------------|
| research | 10 | `[]` | `[]` | `[]` (fixed) | 9 | 1 (web_search) |
| citation | 11 | `[]` | `[]` | `[]` (fixed) | `[]` | `[]` |
| ~~document~~ | ~~1~~ | ~~`[]`~~ | ~~`[]`~~ | ~~`[]`~~ | ~~`[]`~~ | ~~`[]`~~ | **DELETE CATEGORY** |
| shell (was coding) | 2 | 1 | `[]` | 2 | 1 | 2 |
| knowledge | 10 | inherited (10) | 9 | inherited (10) | inherited (10) | `[]` |
| git | 5 | 5 | 5 | 4 | 5 | 4 |
| communication | 1 | inherited | inherited | inherited | inherited | `[]` |
| delegation | `[]` | **enabled** | `[]` | `[]` | **enabled** | `[]` |

**Merge rules**: Dicts merge recursively. Arrays replace entirely. `null` removes the key.

---

### Design Decisions

| # | Issue | Decision | Status |
|---|-------|----------|--------|
| 1 | "coding" is misnamed | **Rename to "shell"** — tools are general terminal execution, not coding-specific | TODO |
| 2 | "document" has 1 tool (chunk_document) | **Delete the tool and the category.** Models have 1M context windows now; chunking adds no value. | TODO |
| 3 | Knowledge has no Job UI toggle | **Not needed as a tool toggle.** Knowledge graph access is already controlled by the "Project memory" toggle (per-project setting in project detail + override in ExecutionGroup). Git is part of the strategic/tactical loop and should not be toggleable for jobs. | Resolved (already works) |
| 4 | Communication has no toggle | **Add as a tool category** called "Communication" with its own toggle in the Tools group. | TODO |
| 5 | CLI clients not in agent Docker image | **Should be auto-installed.** The assumption was CLI clients would be installed automatically when needed. Needs investigation — may need Dockerfile changes or dynamic installation logic. | TODO (infrastructure) |
| 6 | Developer expert uses `null` not `[]` | **Fixed.** Changed `config/experts/developer/config.yaml` to use `[]` for disabled categories. | DONE |
| 7 | claude_code is dead code | **Delete it.** Anthropic bans this usage pattern and it never worked well. | TODO |
| 8 | delegate_work is placeholder | **Should become a tool toggle** in the Tools group (not a separate Advanced accordion section). When implemented, delegation appears as a tool category alongside Shell, Research, etc. The Advanced delegation params (max_depth, timeout) show inline when the toggle is on. | TODO (design) |
| 9 | Critic/Scholar enable delegation | Review when delegate_work is implemented. Currently harmless (placeholder guard). | Deferred |
| 10 | Interactive disables knowledge entirely | Noted. May want to revisit — limits session usefulness. | Deferred |
| 11 | Re-enabling category restores defaults, not expert's list | Noted. Low priority — edge case. | Deferred |
| 12 | `cloud:` key missing from defaults.yaml | Add for consistency. | TODO (minor) |
| 13 | Neo4j has no separate write tool | Noted. Low priority — read/write modes get same tools. | Deferred |

### Remaining Issues (not yet decided)

- **Datasource tools in the UI**: When a datasource is attached, should the injected tools appear as read-only indicators in the Tools section? Currently invisible to the user.
- **Session knowledge toggle**: Sessions don't show the project memory toggle. Should they?

---

## Research: How Other Applications Organize Settings

### IDE/Editor Patterns

**VS Code** — Sidebar tree: Commonly Used > Text Editor > Workbench > Window > Features > Application > Extensions. "Commonly Used" surfaces ~15 high-impact settings. Full-text search filters the entire tree. Modified settings get a blue left-border indicator. Each setting has a per-field "Reset to Default." Dual view: GUI form or raw JSON.

**JetBrains** — Tree dialog: Appearance & Behavior > Keymap > Editor > Plugins > VCS > Build/Deploy > Languages > Tools > Advanced Settings. "Advanced Settings" is an explicit bottom-level catch-all. Per-setting scope annotations (project vs IDE-global). Search highlights matching text within panels.

**Cursor** — Top-level: General > Models > Features > Rules > MCP > Beta. Model picker is grouped checkboxes. Inference params not in main UI (live in settings files). Privacy toggle prominent. Beta features in a dedicated section with warnings.

**Claude Code CLI** — No GUI. Three-tier config: global > project > user-project. Slash commands for runtime changes (`/model`, `/permissions`). Interactive permission prompts on first use (allow once / always / deny).

### AI Agent Platform Patterns

**OpenAI Playground / Custom GPTs** — Model selector at top, system prompt as main textarea, right sidebar for inference params (temperature, max tokens, top-p, penalties). Tools are toggles in a separate section. "More creative" / "More precise" presets as quick-set shortcuts. Temperature is always visible; top-p/penalties under "Advanced."

**Anthropic Console** — Model dropdown at top, system prompt central, right-side panel: temperature, max tokens, top-k, top-p. Flat layout, almost everything visible at once. Compare mode for A/B testing.

**OpenAI Assistants** — Single-page form: Name, Instructions, Model, Tools (code interpreter/file search/functions), Files, Temperature/Top-P. No tabs. Tools are checkboxes with inline expansion.

**Dify.ai** — Split-panel: prompt editor left, "Model & Parameters" right sidebar, separate tabs for Tools/Knowledge/Variables. Debug mode with live parameter tuning. Emerging standard for LLM app builders.

**GitHub Copilot** — Feature toggles first, model selection second. Zero inference params exposed. Agent "instructions" via file (`.github/copilot-instructions.md`) rather than UI.

**Cross-platform AI pattern**: Model selection is always first. System prompt/instructions gets the most screen real estate. Temperature is the only "always visible" inference param. Tools/capabilities are toggles, separated from inference params.

### CI/CD & DevOps Patterns

**Jenkins** — Single scrollable page: General > SCM > Build Triggers > Build Environment > Build Steps > Post-build. "Advanced..." buttons expand hidden fields per section. Parameters are a first-class top-level concept.

**Kubernetes Dashboards (Lens/Rancher)** — Metadata > Pod Spec > Scheduling > Networking > Scaling. Two-column layout (form left, live YAML preview right). "Show Advanced" toggles per section.

**AWX/Ansible** — Tabbed layout: Details > Launch > Survey > Notifications > Schedules. "Survey" concept: user-facing input form layered over admin-defined config, separating operator inputs from technical structure.

**Cross-platform DevOps pattern**: Universal "What / How / Constraints" split. Advanced options behind toggles, never inline. YAML/code preview alongside forms. Tabbed layouts replacing single-page scroll.

### Chat/Bot Builder Patterns

**Botpress** — Three zones: Flow canvas, Knowledge Base, Agent Settings (model + persona + temperature in one form). Test chat panel always visible alongside settings.

**Intercom/Zendesk AI** — Identity (name, tone) > Knowledge Sources > Behavior Rules > Escalation > Channels. "Tone of voice" as a simple dropdown rather than raw prompt editing.

**Cross-platform bot pattern**: Three-bucket separation is universal: Identity/Persona, Capabilities/Tools, Technical/Inference. Single-page with collapsible sections preferred. Creation and editing use the same UI surface.

### UX Best Practices (NNGroup, Material Design, Apple HIG)

1. **Grouping**: Cluster by user mental model, not technical implementation. Frequency-of-use as secondary organizer.
2. **Ordering**: Most frequently used first. Required before optional. Critical > Common > Rare > Dangerous.
3. **Progressive disclosure**: Accordions best for settings pages. "Show advanced" toggle for clear novice/expert split. Avoid separate "simple/advanced" mode toggles — users rarely switch and get confused about what's hidden.
4. **Layout**: Sidebar nav + content area for 5+ categories. Tabs for 3–7 parallel categories. Single scrollable page for <15 settings.
5. **Defaults**: Mark modified values with subtle indicator (dot, bold, badge). Per-field/section reset. Show default value as placeholder or helper text.
6. **Mobile**: Full-width controls, drill-down navigation, toggle switches over checkboxes, sticky save bar.

---

## Proposed Optimal Grouping

Based on research + design decisions above. Core insight: **Identity/Behavior → Capabilities → Technical**, ordered by user impact and frequency of change.

### Proposed Tool Categories (after cleanup)

| Category key | UI label | Tools | Job | Session | Notes |
|-------------|----------|-------|-----|---------|-------|
| `research` | Research | web_search, extract_webpage, crawl_website, map_website, browse_website, download_from_website, search_papers, download_paper, get_paper_info, research_topic | Y | Y | Web + academic paper research |
| `citation` | Citation | cite_document, cite_web, list_sources, get_citation, list_citations, edit_citation, annotate_source, get_annotations, tag_source, search_library, generate_bibliography | Y | Y | Source tracking + bibliography |
| `shell` | Shell | run_command, shell_execute, shell_read | Y | Y | **Renamed from "coding"** — general terminal execution |
| `communication` | Communication | send_message | Y | Y | **NEW toggle** — email to job owner |
| `delegation` | Delegation | delegate_work | Y | - | **Moved from Advanced** — tool toggle + inline params (depth, timeout) |
| `knowledge` | Knowledge | kb_write, kb_read, kb_list, kb_search, ... (10 tools) | - | Y | Session-only toggle. Job access controlled by Project Memory setting. |
| `git` | Git | git_log, git_show, git_diff, git_status, git_tags | - | Y | Session-only toggle. Always-on for jobs (part of strategic/tactical loop). |

**Removed**: `document` category (delete chunk_document tool — obsolete with 1M context models)

**Not toggleable** (by design): workspace, core, session_task, evaluation, orchestrator — system tools

**Datasource-injected** (controlled by datasource attachment + read_only flag, not toggles): sql, graph, mongodb, cloud

### Proposed structure: Job Creation

**Tab 1: Configure** (most-used, visible by default)

| Group | Settings | Rationale |
|-------|----------|-----------|
| **Model** | Preset chips, Strategic model, Tactical model | Model is always first (universal AI platform pattern). Presets serve the 80% case. |
| **Behavior** | Autonomy, Scholar toggle, Critic toggle + rounds | Renamed from "Execution" — defines *how* the agent behaves. Project Memory stays here (per-project, shows conditionally). |
| **Tools** | Research, Citation, Shell, Communication, Delegation (with inline depth/timeout when on) | Capabilities the agent has. Delegation moved here from Advanced. |
| **Data Sources** | Datasource checkboxes | What external data the agent can query. Hidden when none exist. |

**Tab 2: Instructions** (unchanged)

| Group | Settings | Rationale |
|-------|----------|-----------|
| **Custom Instructions** | Markdown textarea | Dedicated tab — needs full height. Builder AI can edit via streaming. |

**Tab 3: Advanced** (progressive disclosure via accordions)

| Section | Settings | Change from current |
|---------|----------|---------------------|
| **Inference** | Reasoning, Temperature, Multimodal (per-phase: Strategic/Tactical), Top-p, Top-k, Max output tokens, Parallel tool calls | Unchanged |
| **Safety & Limits** | Message count threshold, Tool retry count, Progress stall threshold, Max tool calls per phase, Sudo action | **Merged**: sudo_action moved from Shell (it's a safety policy) |
| **Memory** | Memory enabled, Budget tokens | Project Memory stays in Behavior group above |
| **Context** | Compact on archive, Keep recent tool results, Keep recent messages | Unchanged |
| **Shell & Workspace** | Shell mode, Sandbox, Shell timeout, Max read/write words, Git versioning | **Merged**: Shell + Workspace both configure execution environment |
| **Research & Browser** | Proxy enabled, Browser headless, Browser use vision | Unchanged |
| **Auxiliary LLM** | Enabled, Model, Temperature | Unchanged |
| **Resolved Config** | JSON viewer | Always last |

### Proposed structure: Session Creation

**Tab 1: Configure**

| Group | Settings | Rationale |
|-------|----------|-----------|
| **Model** | Model dropdown, Temperature slider | Model first. Temperature promoted from Advanced (only "always visible" inference param). |
| **Behavior** | Permission Mode | Primary behavior toggle for sessions. |
| **Tools** | Research, Citation, Shell, Communication, Knowledge, Git | Session adds Knowledge + Git toggles. No Delegation (sessions use orchestrator tools instead). |
| **Data Sources** | Datasource checkboxes | Same as job. |
| **Session** | Idle timeout, Greeting, Auto-start Claude Code | **Promoted from Advanced** — these are session essentials. |

**Tab 2: Advanced**

| Section | Settings | Change from current |
|---------|----------|---------------------|
| **Inference** | Reasoning, Multimodal, Top-p, Top-k, Max output tokens, Parallel tool calls | Temperature promoted to Configure tab |
| **Safety & Limits** | Same as job minus delegation | Same |
| **Memory** | Memory enabled, Budget tokens | Same |
| **Context** | Same as job | Same |
| **Shell & Workspace** | Same as job | Same |
| **Research & Browser** | Same | Same |
| **Auxiliary LLM** | Same | Same |
| **Resolved Config** | JSON viewer | Always last |

### Summary of all changes

| Change | Why |
|--------|-----|
| Rename "coding" → "shell" | Accurate — tools are general terminal execution |
| Delete "document" category + chunk_document tool | Obsolete — 1M context models don't need chunking |
| Add "communication" toggle | send_message was hidden, should be user-controllable |
| Move delegation from Advanced accordion to Tools group | It's a capability (tool toggle), not a tuning knob |
| Rename "Execution" → "Behavior" | Matches user mental model |
| Model always first | Universal AI platform pattern |
| Promote Temperature to Configure (session) | Only "always visible" inference param |
| Promote Session settings to Configure | Idle timeout/greeting are essentials |
| Move Sudo Action to Safety & Limits | Safety policy, not shell config |
| Merge Shell + Workspace accordions | Both configure execution environment |
| Knowledge: no change for jobs | Already controlled by Project Memory toggle (project-level + per-job override in Behavior group) |
| Git: no change for jobs | Always-on — part of strategic/tactical loop design |
