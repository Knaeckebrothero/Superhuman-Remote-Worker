# Persistent Agent vs Claude Code / Codex CLI / Gemini CLI

Comprehensive comparison of our persistent agent sessions against the three leading interactive coding agents. Goal: determine what it takes to offer an equivalent experience through our web UI.

Date: 2026-03-31

---

## Executive Summary

Our persistent agent has the right architectural foundation — the `while(tool_call)` loop, WebSocket transport, permission modes, config-driven tool sets, and worker delegation all match or exceed industry patterns. But we're at MVP level on the *experience* side. The three competitors have converged on a shared set of table-stakes features (session resume, OS-level sandboxing, per-token streaming, plan mode, file undo, rich context management, memory) that we lack or only partially implement. The good news: most gaps are incremental additions to existing infrastructure, not rewrites. The biggest blocker is session persistence (P0), followed by streaming fidelity (P1) and plan mode (P1).

Can we "plug in Claude Opus 4.6 and have it work like Claude Code"? **Architecturally yes, practically not yet.** The LLM integration works — any model (OpenAI, Anthropic, Google, Groq, OpenRouter, local) can power the persistent agent via config. But Claude Code's value isn't the model — it's the tooling, session management, safety layers, and UX polish around it. Those are the gaps this document catalogs.

---

## 1. Architecture Comparison

| Aspect | SRW Persistent Agent | Claude Code | Codex CLI | Gemini CLI |
|---|---|---|---|---|
| **Core loop** | `while(tool_call)` async Python | `while(tool_call)` TS/Node | Item/Turn/Thread Rust | ReAct TS/Node |
| **Transport** | WebSocket (bidirectional) | SSE + HTTP (approval via separate endpoint) | JSONL/stdio (local) | Event stream |
| **UI surface** | Angular web app | Terminal + VS Code + JetBrains + Desktop + Web + iOS + Slack | Terminal + VS Code + Desktop | Terminal + VS Code companion |
| **Deployment** | Kubernetes pod (container or VM) | Local CLI process | Local CLI process (+ cloud sandbox) | Local CLI process |
| **Model support** | Any (OpenAI, Anthropic, Google, Groq, OpenRouter, local) | Anthropic only (Claude family) | OpenAI only (GPT-5 family, + Ollama) | Google only (Gemini family) |
| **Open source** | Private | Closed (npm package) | Apache 2.0 (Rust) | Apache 2.0 (TS) |

**Our advantage:** Model-agnostic. Claude Code is locked to Anthropic models. Codex is locked to OpenAI (with Ollama escape hatch). Gemini is locked to Google. Our agent works with *any* provider, including local models via vLLM/Ollama. This is a genuine differentiator.

**Our advantage:** Remote-first. The competitors are local CLI tools. Our agent runs on infrastructure (K8s pods, VMs) accessible from any browser. This enables use cases they can't match: shared team sessions, persistent workspaces that outlive a laptop, delegation to GPU-equipped VMs, and the worker job system.

---

## 2. Tool Inventory

### 2a. Our Tools (86 across 12 categories)

| Category | Count | Tools |
|---|---|---|
| **Workspace** | 13 | read_file, write_file, edit_file, list_files, search_files, file_exists, get_document_info, create_directory, delete_directory, delete_file, move_file, rename_file, copy_file |
| **Coding** | 3 | run_command, shell_execute, shell_read |
| **Git** | 5 | git_status, git_log, git_diff, git_show, git_tags |
| **Research** | 10 | web_search, extract_webpage, crawl_website, map_website, browse_website, download_from_website, research_topic, search_papers, get_paper_info, download_paper |
| **Document** | 1 | chunk_document |
| **Citation** | 11 | cite_document, cite_web, edit_citation, generate_bibliography, list_citations, get_citation, list_sources, search_library, tag_source, annotate_source, get_annotations |
| **SQL** | 3 | sql_query, sql_execute, sql_schema |
| **MongoDB** | 5 | mongo_query, mongo_insert, mongo_update, mongo_aggregate, mongo_schema |
| **Graph** | 2 | execute_cypher_query, get_database_schema |
| **Knowledge** | 10 | kb_write, kb_read, kb_update, kb_search, kb_list, kb_related, kb_provenance, kb_contradictions, kb_unanswered, kb_export |
| **WebDAV** | 5 | webdav_read, webdav_write, webdav_list, webdav_delete, webdav_info |
| **Orchestrator** | 8 | create_worker_job, list_worker_jobs, get_worker_job, get_job_workspace_file, approve_worker_job, resume_worker_job, pause_worker_job, cancel_worker_job |

### 2b. Competitor Core Tools

| Tool Type | SRW | Claude Code | Codex CLI | Gemini CLI |
|---|---|---|---|---|
| File read | read_file | Read | file read | read_file |
| File write | write_file | Write | file write | write_file |
| File edit (targeted) | edit_file | Edit | — (write only) | replace |
| File search (glob) | list_files + search_files | Glob | — (via shell) | glob |
| Content search (grep) | search_files | Grep | — (via shell) | grep_search |
| Shell execution | run_command, shell_execute | Bash | shell command | run_shell_command |
| Web search | web_search | WebSearch | web_search | google_web_search |
| Web fetch | extract_webpage | WebFetch | — | web_fetch |
| Git operations | 5 dedicated tools | via Bash | via shell | via shell |
| Image/vision input | read_file (multimodal) | Read (paste/drag) | -i flag | @file.png |
| PDF reading | read_file (rendered) | Read | — | read_file |
| Sub-agent spawn | create_worker_job | Agent | subagents (6 concurrent) | codebase_investigator |
| Task/todo tracking | — (excluded from persistent) | TaskCreate/Update/List | write_todos | write_todos |
| Memory save | workspace.md (manual) | Auto-memory | — | save_memory |
| Plan mode | — | EnterPlanMode | enter_plan_mode | enter_plan_mode |
| Notebook editing | — | NotebookEdit | — | — |
| MCP tool discovery | — | ToolSearch (deferred) | /mcp | /mcp |
| Ask user question | — | AskUserQuestion | — | ask_user |
| Browser automation | browse_website (Playwright) | via MCP (Playwright) | via MCP | browser_agent (experimental) |

### 2c. What We Have That They Don't

| Capability | Details |
|---|---|
| **Worker job delegation** | Full autonomous agents with phase alternation (strategic/tactical), not just context-isolated subagents. Can run for hours, produce structured deliverables, and be steered mid-execution. |
| **Multi-database tools** | SQL, MongoDB, Neo4j tools natively. Competitors have none — users must install MCP servers. |
| **Citation management** | 11 citation tools with full source library, bibliography generation, and provenance tracking. Academic-grade. |
| **Knowledge graph** | 10 tools for building and querying a persistent knowledge graph (Neo4j + pgvector). |
| **Cloud storage** | WebDAV integration for reading/writing to cloud file systems. |
| **Academic research** | Paper search, download, and analysis tools. |
| **Persistent shell tabs** | Named tmux tabs with scrollback management. Competitors have single-shot shell. |
| **Remote workspace** | Agent workspace can be a remote container or VM, not just local filesystem. |

### 2d. What They Have That We Don't

| Capability | Claude Code | Codex CLI | Gemini CLI | Priority |
|---|---|---|---|---|
| **Plan mode** (read-only tools) | EnterPlanMode/ExitPlanMode | enter_plan_mode/exit_plan_mode | enter_plan_mode/exit_plan_mode | P1 |
| **Task tracking in session** | TaskCreate/Update/List/Get | write_todos | write_todos | P1 |
| **AskUserQuestion** (structured) | Multi-choice questions | — | ask_user | P2 |
| **File undo/checkpoints** | Esc-Esc rewind | sandbox snapshot | /restore | P1 |
| **Notebook editing** | NotebookEdit | — | — | P3 |
| **LSP / code intelligence** | LSP tool (type errors, jump-to-def) | — | — | P3 |
| **Voice input** | /voice (push-to-talk) | — | — | P3 |

---

## 3. Session & Context Management

| Feature | SRW | Claude Code | Codex CLI | Gemini CLI |
|---|---|---|---|---|
| **Session persistence** | PostgreSQL (thread_messages) + auto-restore on startup | JSONL transcripts (local) | JSONL rollout files | Auto-save to ~/.gemini/ |
| **Session resume** | Auto-restore from DB on pod restart + cockpit resume button | --continue, --resume, --fork | resume picker + fork | --resume, /resume, named checkpoints |
| **Context window** | Model-dependent (configurable thresholds via settings_matrix) | 200K standard, 1M extended | Model-dependent (up to 1M) | 1M (Gemini 2.5 Pro) |
| **Auto-compaction** | Same ContextManager as worker (3-tier: tool clearing, trimming, summarization) | 3-layer (HTTP, pre-request, emergency) | Auto at token limit | At 70% threshold |
| **Manual compaction** | /compact [focus] (via WS) | /compact [focus] | — | /compress |
| **Compaction strategy** | AuxiliaryLLM summarization, preserves last 15 tool results, rolling summaries, identity anchors, critical facts | Preserves 10 recent tool results, re-injects CLAUDE.md | Encrypted content preservation | XML snapshot (goal, knowledge, plan) |
| **Persistent memory** | workspace.md (transient injection, protected from compaction) + RecallStore | CLAUDE.md + auto-memory files | AGENTS.md | GEMINI.md + save_memory tool |
| **Memory across sessions** | RecallStore (pgvector, TTL-based) | Auto-memory directory per project | Thread persistence | GEMINI.md file |
| **Context visibility** | — | /context command | — | — |

### Key Gaps

**Session persistence: Resolved.** Messages are saved to PostgreSQL `thread_messages` every turn (user, AI, and tool messages). On pod restart or session resume, `_restore_session_messages()` loads the full message history from the DB and reconstructs LangChain message objects (HumanMessage, AIMessage with tool_calls, ToolMessage with paired tool_call_ids). The turn counter is restored from the last saved turn_number. The cockpit also loads history via REST for the UI. Per-turn metrics (token counts, latency, model) are stored in a `metrics` JSONB column on AI messages.

**Context compaction: NOT a gap.** The persistent agent uses the exact same `ContextManager` as the worker agent — same class, same `ensure_within_limits()` → `summarize_and_compact()` call chain, same 3-tier algorithm (tool result clearing → message trimming → LLM summarization). Workspace.md is explicitly protected from summarization via `is_workspace_injection_message()` filtering (context.py:1365) and re-injected fresh after compaction. Recent tool results (last 15), reasoning traces, identity anchors, and critical facts are all preserved. The `/compact` command triggers the same function. This was previously flagged as a gap in an earlier assessment but has since been resolved.

**Memory model (P2):** We have RecallStore (pgvector hybrid search) and workspace.md injection — architecturally on par. But the UX is weaker: no `/memory` command, no user-visible memory management, no explicit "remember this" interaction.

---

## 4. Permission & Safety Model

| Feature | SRW | Claude Code | Codex CLI | Gemini CLI |
|---|---|---|---|---|
| **Permission modes** | 3 (supervised, auto_accept, autonomous) | 6 (default, acceptEdits, plan, auto, dontAsk, bypassPermissions) | 3 (untrusted, on-request, never) + presets | 4 (default, auto_edit, yolo, plan) |
| **Per-tool allowlists** | No | Yes (glob patterns, domain scopes) | Yes (granular per-category) | Yes (tool name patterns) |
| **Protected paths** | No | .git, .claude, .vscode, .idea | .git, .agents, .codex | — |
| **OS-level sandbox** | None (in-process tools) | Linux bubblewrap, macOS seatbelt, seccomp BPF | Seatbelt, Landlock+seccomp, bubblewrap | gVisor, Docker, Seatbelt, LXC |
| **Network isolation** | None | Proxy with domain allowlists | Blocked by default, configurable | Varies by sandbox profile |
| **File checkpoints** | Git history only | Pre-edit snapshot, Esc-Esc rewind | OS-level sandbox restriction | Shadow git repo, /restore |
| **Command blocklist** | ShellManager config | — | — | — |
| **Sudo handling** | Detect + freeze + VM upgrade | — | drop-sudo / unprivileged-user | — |
| **Auto mode (AI classifier)** | — | Sonnet 4.6 safety classifier | — | — |
| **Managed policies (enterprise)** | — | MDM-deployed CLAUDE.md, org rules | System config + policy TOML | /etc/ system defaults + overrides |

### Key Gaps

**No sandbox (P2):** Our tools run in-process with full access. Every competitor has OS-level sandboxing. Mitigated by the VM backend (persistent agents on VMs get full isolation), but local dev has no safety net.

**Permission granularity (P2):** We have 3 coarse modes. Competitors have per-tool allowlists with pattern matching (e.g., Claude Code's `Bash(npm run *)` or Gemini's `run_shell_command(git)`). Our `auto_accept` mode auto-approves everything except shell commands — no middle ground.

**File checkpoints (P1):** Claude Code snapshots every file before editing. Gemini has a shadow git repo for restore. We rely on workspace git history, which requires manual git operations to revert. No quick "undo last edit" in the UI.

**Our advantage — Sudo gate:** Our sudo approval system (C plugin + Go daemon + NATS + cockpit UI) is unique. No competitor has this. When an agent needs elevated privileges, the command is intercepted and held for human approval with auto-approval rules.

---

## 5. Streaming & Real-time UX

| Feature | SRW | Claude Code | Codex CLI | Gemini CLI |
|---|---|---|---|---|
| **Token streaming** | Partial (chunk-based, Anthropic returns final chunk) | Per-token SSE | Per-token JSONL | Per-token |
| **Typing effect** | Only for OpenAI/Groq (Anthropic shows all at once) | Always | Always | Always |
| **Tool call display** | Card with name + status + collapsible result | Inline with context | TUI with syntax highlighting | Inline |
| **Diff display** | None (raw tool result) | VS Code inline diff, CLI diff | Syntax-highlighted diffs | VS Code companion diff |
| **Streaming interrupt** | Mid-stream + between tools (Stop button) | Type during generation to redirect | Tab to queue, Enter to inject | — |
| **Progress indicators** | Thinking dots, tool spinner | Status bar, thinking tokens visible | Streaming display | Status line |

### Key Gaps

**Anthropic streaming (P1):** When using Claude models, our agent shows the entire response at once after the final chunk arrives. Claude Code has per-token SSE streaming with all Anthropic models. The root cause is LangChain's `astream()` abstraction. Fix: use the raw Anthropic SDK's `messages.stream()` or switch to `astream_events()`.

**No diff rendering (P2):** File edits show as tool result text. Claude Code shows inline diffs in VS Code. Codex shows syntax-highlighted diffs. Gemini opens VS Code's native diff viewer. Our UI should render file changes as visual diffs (before/after), not raw text.

~~**Mid-generation interrupt (P2):**~~ *Resolved.* Interrupt flag is now checked inside the `astream()` chunk loop and before each tool execution (`persistent_graph.py`). Partial responses are preserved in message history. The UI "Stop" button and WebSocket `interrupt` protocol already existed. Note: Claude Code also supports *typing mid-generation to redirect* (steer), which we don't — our interrupt is a full stop, not a redirect.

---

## 6. Customization & Extensibility

| Feature | SRW | Claude Code | Codex CLI | Gemini CLI |
|---|---|---|---|---|
| **Instruction files** | YAML config + prompt/instruction matrix | CLAUDE.md hierarchy + @imports | AGENTS.md hierarchy | GEMINI.md hierarchy + @imports |
| **Custom tools** | Config-driven tool categories | MCP servers + custom tools | MCP servers | MCP servers + extensions |
| **Hooks/lifecycle** | Instruction files (passive injection) | 24 events, 4 handler types (PreToolUse, PostToolUse, etc.) | 11 events (BeforeTool, AfterTool, etc.) | 11 events |
| **Custom commands** | — | Skills (markdown + YAML frontmatter) | Slash commands (markdown files) | Custom commands (TOML) + skills |
| **Plugins** | — | Plugin system with marketplace | Plugin bundles | Extensions with gallery |
| **MCP protocol** | REST tools only (not MCP wire protocol) | First-class (stdio, http, sse, ws) | First-class (stdio, streamable HTTP) | First-class (stdio, SSE, HTTP) |
| **Themes** | Catppuccin (fixed) | — | /theme + custom .tmTheme | /theme + custom themes |

### Key Gaps

**No MCP client (P3):** All three competitors support MCP as a first-class protocol. Our orchestrator *exposes* an MCP server (port 8055), but the persistent agent doesn't *consume* MCP tools — it uses direct REST calls to the orchestrator. Adding MCP client support would let us auto-discover tools from any MCP server.

**No hooks system (P3):** Competitors have lifecycle hooks that can block, modify, or react to tool calls programmatically. We have instruction files that inject guidance text before specific tools — useful but passive (can't block or modify). A hooks system would enable: auto-formatting after edits, linting before commits, custom permission validation, notifications, logging.

**No plugin/extension system (P3):** All three competitors have plugin/extension systems for packaging and distributing custom capabilities. We have config-driven expert configs which serve a similar purpose but aren't distributable or composable.

---

## 7. Sub-agents & Delegation

| Feature | SRW | Claude Code | Codex CLI | Gemini CLI |
|---|---|---|---|---|
| **Sub-agent type** | Worker jobs (full autonomous agents) | Isolated context subagents | Up to 6 concurrent threads | Isolated subagents |
| **Spawning** | create_worker_job (REST to orchestrator) | Agent tool (in-process) | Subagent tool | Sub-agent tools |
| **Isolation** | Separate container/VM | Own context window | Own context + sandbox | Own context |
| **Startup latency** | High (container boot + registration) | Low (in-process fork) | Low (thread spawn) | Low (in-process) |
| **Real-time progress** | Poll via get_worker_job | Background notification | Task list updates | — |
| **Multi-agent teams** | Worker orchestration via tools | Agent Teams (experimental, 1M ctx each) | Worktrees + A2A | Cloud VMs (8 parallel) |
| **Nesting depth** | Workers can't spawn workers | 1 level (no nesting) | Max depth 1 by default | No nesting |

### Our Advantage

Worker job delegation is strictly more powerful than subagents. Our workers are full autonomous agents with:
- Phase alternation (strategic planning + tactical execution + retrospectives)
- Their own workspace, git versioning, and tool set
- Configurable autonomy levels
- Mid-execution steering via `resume_worker_job` with feedback
- Different expert configs per worker (developer, scholar, critic)

The trade-off is startup latency (container boot vs in-process fork). For quick tasks, subagents win. For heavy tasks (multi-hour research, document generation, code projects), workers win.

### Gap: In-session Task Tracking (P1)

All three competitors have todo/task tools available during interactive sessions. Our persistent agent excludes the phase/todo tools (`next_phase_todos`, `todo_complete`, etc.) because they're tied to the worker's phase alternation model. We should add lightweight session-scoped task tracking (similar to Claude Code's `TaskCreate`/`TaskUpdate`) that doesn't require the full phase system.

---

## 8. UI Comparison

| Feature | SRW Cockpit | Claude Code CLI | Codex CLI | Gemini CLI |
|---|---|---|---|---|
| **Platform** | Web browser (any device) | Terminal (+ IDE, web, desktop, mobile) | Terminal (+ IDE, desktop) | Terminal (+ VS Code) |
| **Message rendering** | Markdown (ngx-markdown) | Markdown (terminal) | Markdown (Ink TUI) | Markdown (terminal) |
| **Tool call display** | Collapsible cards | Inline with status | TUI cards | Inline |
| **Diff view** | None | IDE inline diff | Syntax-highlighted | VS Code companion diff |
| **Permission dialog** | Approve/Deny/Auto-accept buttons | y/n keyboard | Keyboard approval | Keyboard + Ctrl+Y toggle |
| **Slash commands** | /compact, /done, /auto, /supervised, /autonomous | 30+ commands | 10+ commands + custom | 37+ commands with 60+ subcommands |
| **File browser** | IDE button (code-server/Gitea) | IDE integration | — | IDE companion |
| **Session management** | Sessions list + filter tabs | --continue/--resume/--fork | resume picker | /resume + session browser |
| **Theme** | Catppuccin dark (fixed) | — | Configurable themes | Configurable themes |
| **Keyboard shortcuts** | Enter to send, Shift+Enter newline | Extensive (Shift+Tab, Ctrl+O, etc.) | Extensive | Vim mode available |
| **Mobile support** | Yes (responsive web) | iOS app + Remote Control | — | — |
| **Collaborative** | Shared sessions (multi-user potential) | Single user | Single user | Single user |

### Our Advantages

1. **Web-based** — accessible from any device, no CLI installation, shareable URLs
2. **Session list with management** — visual overview of all sessions, filter by status, one-click create/end/delete
3. **IDE integration** — code-server and Gitea buttons for workspace inspection
4. **Mobile-friendly** — responsive design works on phones/tablets
5. **Multi-user potential** — Keycloak auth, user-scoped sessions, team collaboration possible

### UI Gaps

| Gap | Details | Priority |
|---|---|---|
| **No diff rendering** | File changes are raw text, not visual diffs | P2 |
| **No file browser in chat** | Must open external IDE to inspect files | P2 |
| ~~**Raw citation markup**~~ | ~~Fixed: custom marked extension renders citations as links~~ | ~~P1~~ |
| **Limited slash commands** | 6 commands (/compact, /done, /undo, /auto, /supervised, /autonomous) vs 30+ in competitors | P2 |
| **No plan review UI** | Not needed — planning via worker jobs, not interactive sessions | — |
| ~~**No progress/status bar**~~ | ~~Fixed: status bar shows model, turn count, permission mode~~ | ~~P2~~ |
| **Sessions indistinguishable** | All named "Local Session (interactive)" | P1 (see UI assessment) |
| ~~**Sidebar routing confusion**~~ | ~~Fixed: Sessions link always navigates to session list~~ | ~~P1~~ |
| ~~**Grammar: "1 turns"**~~ | ~~Fixed: conditional pluralization~~ | ~~P3~~ |

---

## 9. Headless / Automation

| Feature | SRW | Claude Code | Codex CLI | Gemini CLI |
|---|---|---|---|---|
| **Non-interactive mode** | Worker agents dispatched via orchestrator REST API | -p/--print flag | codex exec | -p/--prompt flag |
| **Output formats** | Job artifacts in workspace | text, json, stream-json | json, stream-json | json, stream-json |
| **CI/CD integration** | Custom (orchestrator API) | GitHub Action (claude-code-action) | GitHub Action (codex-action) | GitHub Action |
| **SDK** | Python (orchestrator client) | Python + TypeScript Agent SDK | TypeScript SDK | — |
| **Scheduled tasks** | Cron via orchestrator | /loop + cloud scheduled | — | — |
| **Structured output** | — | --json-schema | --output-schema | — |

### Our Advantage

Our worker agent system IS the headless mode — and it's more powerful than any competitor's. Workers run full phase alternation with strategic planning, can be monitored via the cockpit, steered mid-execution, and produce structured deliverables. The persistent agent adds interactive sessions on top.

---

## 10. "Can We Just Plug In Claude Opus 4.6?"

**Yes, technically.** Set `llm.model: claude-opus-4-6` in the interactive config and the agent will use Opus 4.6 for all inference. We already support Anthropic models via `ANTHROPIC_API_KEY`.

**But it won't feel like Claude Code.** Here's what's different:

| Claude Code Feature | Our Status | Gap |
|---|---|---|
| Model (Opus 4.6) | Supported | None |
| Per-token streaming | Partial (chunk-based for Anthropic) | Responses appear all at once |
| CLAUDE.md persistent instructions | Equivalent via workspace.md (protected from compaction) + config prompts | Different mechanism, similar effect |
| Auto-memory across sessions | RecallStore exists but no user-facing commands | No `/memory` equivalent |
| File edit + undo | edit_file exists, no undo | Need file checkpoints |
| Plan mode | Missing | Need read-only tool filtering |
| Session resume | Missing | Messages lost on disconnect |
| Subagent spawning | Worker delegation (slower but more powerful) | Different trade-off |
| Sandbox + protected paths | Missing for local dev | Need OS-level isolation |
| 1M context window | Supported (pass model config) | None |
| Extended thinking | Supported via `reasoning_level: high` | None |
| Effort levels | Supported via config | None |
| Agent teams | Worker orchestration (different model) | Different architecture |
| Hooks | Instruction files only (passive) | Need programmable hooks |
| Auto mode (AI safety classifier) | Missing | Unique to Claude Code |

**Bottom line:** The model works. The tool set is actually larger than Claude Code's. But the *session experience* (streaming, resume, undo, plan mode) needs hardening.

---

## 11. Priority Roadmap

### P0 — Must-have for production sessions

~~**Session persistence**~~ — *Resolved.* Messages saved to PostgreSQL every turn, restored into LangChain message history on pod restart via `_restore_session_messages()`. Turn counter restored. Per-turn metrics (tokens, latency) stored. Cockpit resume button resets thread status and navigates to chat.

~~**Context compaction hardening**~~ — *Resolved.* Same `ContextManager` as worker, workspace.md protected from summarization, `/compact` command implemented, 15 recent tool results preserved. No remaining gap.

### P1 — Critical for usability parity

| Item | Effort | Notes |
|---|---|---|
| **Per-token streaming (Anthropic)** | Medium | Use raw Anthropic SDK `messages.stream()` instead of LangChain `astream()`, or `astream_events()`. |
| **Plan mode** | Low | Not needed — planning handled by worker jobs with phase alternation. Interactive sessions have user supervision. |
| ~~**Session task tracking**~~ | ~~Low~~ | ~~Resolved. New `SessionTaskManager` + `task_add/task_complete/task_list` tools. Task bar UI in chat with checklist. WS `tasks.updated` events.~~ |
| ~~**File checkpoints / undo**~~ | ~~Medium~~ | ~~Resolved. `_snapshot_callback` on ToolContext snapshots files before write/edit. `/undo` slash command restores via `undo_turn()`. WS `file.checkpoint` + `files.restored` events.~~ |
| ~~**Mid-turn interrupt**~~ | ~~Low~~ | ~~Resolved. Interrupt flag now checked inside the `astream()` chunk loop (mid-generation) and before each tool execution. Partial responses are preserved in message history. UI "Stop" button + WebSocket `interrupt` method already existed.~~ |
| ~~**Citation rendering in UI**~~ | ~~Low~~ | ~~Resolved. Custom marked inline extension parses `【cite_web/cite_document】` into rendered links/spans. Registered via `MARKED_EXTENSIONS` in app.config.ts.~~ |
| ~~**Session naming/title**~~ | ~~Low~~ | ~~Resolved. Chat header shows `sessionTitle` loaded from REST. Status bar shows model name, turn count, permission mode.~~ |

### P2 — Important for competitive parity

| Item | Effort | Notes |
|---|---|---|
| **Diff rendering in UI** | Medium | Show file changes as visual before/after diffs in chat, not raw text. |
| **Permission allowlists** | Low | Per-tool pattern matching (e.g., auto-approve `run_command(git *)`, `run_command(npm test)`). |
| **Inline file viewer** | Medium | View/browse workspace files within the chat UI without opening external IDE. |
| **More slash commands** | Low | /plan, /memory, /status, /context, /undo, /clear, /model. |
| ~~**Status bar**~~ | ~~Low~~ | ~~Resolved. Status bar with model chip, turn count, and permission mode. Context usage % still missing (needs backend support).~~ |
| **Sandbox (basic)** | High | OS-level isolation for local dev. VM backend already covers remote. |
| **AskUserQuestion tool** | Low | Structured multi-choice questions in chat (rendered as buttons). |

### P3 — Nice-to-have / differentiators

| Item | Effort | Notes |
|---|---|---|
| **MCP client protocol** | Medium | Consume MCP servers instead of direct REST. Auto-discover orchestrator tools. |
| **Hooks system** | High | Programmable lifecycle hooks (PreToolUse, PostToolUse, etc.) with block/modify/log. |
| **Plugin/extension system** | High | Distributable packages of tools, configs, and hooks. |
| **Custom themes** | Low | Theme selector in settings. |
| **Notebook editing** | Medium | Jupyter notebook cell editing tool. |
| **LSP / code intelligence** | High | Language server integration for type errors, jump-to-def. |
| **Voice input** | Medium | Push-to-talk with Whisper transcription (Whisper infra already exists). |

---

## 12. What We Should NOT Copy

1. **Local-only architecture.** Our remote-first model is better for production. Don't regress to local-only to match CLI tools.
2. **Single-model lock-in.** Our model-agnostic design is a strength. Don't hardcode Anthropic-specific features.
3. **Terminal-first UX.** Our web UI is more accessible. Invest in the web experience rather than building a CLI.
4. **Subagent model for heavy work.** Our worker delegation is strictly more powerful for tasks that take hours. Keep it.
5. **Bypassing permissions entirely.** "YOLO mode" (Codex/Gemini) is a security liability. Our supervised/auto_accept/autonomous model is safer. Add granularity, not escape hatches.

---

## 13. What Makes Us Unique

| Differentiator | Details |
|---|---|
| **Worker job delegation** | Spawn full autonomous agents with strategic planning, not just context-isolated subagents |
| **Model-agnostic** | Any LLM provider (OpenAI, Anthropic, Google, Groq, OpenRouter, local) |
| **Remote-first** | Runs on infrastructure (K8s, VMs), accessible from any browser |
| **Multi-database** | Native SQL, MongoDB, Neo4j tools — no MCP setup needed |
| **Knowledge graph** | Persistent knowledge base with provenance, contradiction detection |
| **Citation management** | Academic-grade source library and bibliography |
| **Sudo approval gate** | Human-in-the-loop privilege escalation (unique) |
| **Web UI** | Visual session management, IDE integration, mobile-friendly |
| **Autonomy levels** | Graduated human-in-the-loop from full to dependent (5 levels for workers) |
| **Phase alternation** | Strategic planning + tactical execution + retrospectives (workers) |

---

## Appendix: Feature-by-Feature Checklist

| Feature | SRW | CC | Codex | Gemini | Notes |
|---|:---:|:---:|:---:|:---:|---|
| Interactive chat loop | Y | Y | Y | Y | |
| Multi-turn conversation | Y | Y | Y | Y | |
| Token streaming | Partial | Y | Y | Y | Anthropic models lack per-token |
| File read/write/edit | Y | Y | Y | Y | |
| Shell execution | Y | Y | Y | Y | |
| Web search | Y | Y | Y | Y | |
| Git tools | Y | Y | Y* | Y* | *via shell commands |
| Session resume | Y | Y | Y | Y | DB restore + cockpit resume |
| Context compaction | Y | Y | Y | Y | Same ContextManager as worker |
| Plan mode | N/A | Y | Y | Y | Handled by worker jobs |
| File undo/checkpoints | Y | Y | Y | Y | Snapshot + /undo |
| OS-level sandbox | N | Y | Y | Y | **P2 gap** |
| Per-tool allowlists | N | Y | Y | Y | **P2 gap** |
| Diff rendering | N | Y | Y | Y | **P2 gap** |
| Task tracking | Y | Y | Y | Y | SessionTaskManager + task bar UI |
| Memory system | Partial | Y | Partial | Y | |
| MCP client | N | Y | Y | Y | **P3 gap** |
| Hooks/lifecycle | N | Y | Y | Y | **P3 gap** |
| Subagent/delegation | Workers | Y | Y | Y | Different model, ours is more powerful |
| IDE integration | External | Y | Y | Y | IDE buttons in UI |
| Headless/CI mode | Workers | Y | Y | Y | Workers ARE headless mode |
| Custom instructions | Y | Y | Y | Y | |
| Image/vision input | Y | Y | Y | Y | |
| PDF reading | Y | Y | N | Y | |
| Multi-database tools | Y | N | N | N | **Our advantage** |
| Knowledge graph | Y | N | N | N | **Our advantage** |
| Citation management | Y | N | N | N | **Our advantage** |
| Worker delegation | Y | N | N | N | **Our advantage** |
| Web UI | Y | Partial | N | N | **Our advantage** |
| Model-agnostic | Y | N | N | N | **Our advantage** |
| Sudo approval gate | Y | N | N | N | **Our advantage** |

---

*See also: `docs/features/persistent_agent_assessment.md` (internal gap analysis, 2026-03-30), `docs/coding_agent_ui_assessment.md` (UI review, 2026-03-31), `docs/interactive_planning.md` (autonomy levels & PR review design).*
