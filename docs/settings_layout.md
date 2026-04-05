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
| Document | `tools.document` | Document processing and chunking | Y | Y | - |
| Coding | `tools.coding` | Shell command execution | Y | Y | - |
| Knowledge | `tools.knowledge` | Knowledge graph and memory tools | - | Y | - |
| Git | `tools.git` | Git repository operations | - | Y | - |

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
