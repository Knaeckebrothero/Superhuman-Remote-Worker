# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

General-purpose LLM agent system built on LangGraph. Agents are configured via YAML to perform document processing, research, database operations, and domain-specific tasks. External databases (PostgreSQL, Neo4j, MongoDB) are attached to jobs as datasources via the cockpit UI or environment defaults.

## Commands

### Development Setup
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Then configure API keys

# System dependencies (Fedora)
sudo dnf install poppler-utils tmux    # PDF rendering (pdf2image) + persistent shell sessions
# Debian/Ubuntu: sudo apt-get install poppler-utils tmux
playwright install chromium            # Required for browser-based research tools
```

CitationEngine is a [separate repository](https://github.com/Knaeckebrothero/CitationEngine) installed via git URL in `requirements.txt`. Install extras: `[pdf]`, `[web]`, `[langchain]`, `[postgresql]`, `[vector]`, `[dev]`, `[full]` (all).

### Database Management
```bash
# Start databases (development) — see docker-compose.dev.yaml for all services
# NOTE: This project uses Podman, not Docker
podman-compose -f docker-compose.dev.yaml up -d

# Start only databases
podman-compose -f docker-compose.dev.yaml up -d postgres mongodb neo4j

# Start databases + NATS (for VM lifecycle testing)
podman-compose -f docker-compose.dev.yaml up -d postgres mongodb neo4j nats

# Start databases + MCP for Claude Code
podman-compose -f docker-compose.dev.yaml up -d postgres mongodb neo4j mcp

# Initialize everything (databases + workspace) - RECOMMENDED
python init.py

# Reset everything (WARNING: deletes all data)
python init.py --force-reset

# Initialize specific components only
python init.py --only-orchestrator     # Only databases (PostgreSQL, MongoDB)
python init.py --only-agent            # Only workspace
python init.py --skip-mongodb          # Skip MongoDB (optional component)

# Component-specific initialization (alternative)
python -m orchestrator.init            # Initialize databases only
python -m src.init                     # Initialize workspace only

# Create backup of current state
python init.py --create-backup                  # Auto-named: backups/YYYYMMDD_NNN/
python init.py --create-backup my_backup        # Named: backups/YYYYMMDD_NNN_my_backup/

# Restore from backup
python init.py --restore-backup backups/20260117_001_my_backup
```

Note: Legacy scripts are in `DEPRECATED_scripts/` and show deprecation warnings.

### Running Agents
```bash
# Run with defaults (LOG_LEVEL=INFO, streaming enabled)
python agent.py --description "Your task here"

# Run with custom config
python agent.py --config my_agent --description "Your task"

# Run with debug logging
LOG_LEVEL=DEBUG python agent.py --description "Your task"

# Run with LLM token streaming to stderr
DEBUG_LLM_STREAM=1 python agent.py --description "Your task"

# Process document
python agent.py --document-path ./data/doc.pdf --description "Extract requirements"

# Process directory of documents
python agent.py --document-dir ./data/example_data/ --description "Identify requirements"

# Start as API server
python agent.py --port 8001

# Resume from checkpoint
python agent.py --job-id <id> --resume

# Phase snapshot management
python agent.py --job-id <id> --list-phases          # List available snapshots
python agent.py --job-id <id> --recover-phase 2 --resume  # Recover to specific phase

# Approve a frozen job (marks as completed)
python agent.py --config validator --job-id <id> --approve
```

**Environment Variables:**
| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `DEBUG_ALL` | unset | Set to `1` to include third-party library debug output |
| `DEBUG_LLM_STREAM` | unset | Set to `1` for LLM token output to stderr |
| `DEBUG_LLM_TAIL` | `500` | Characters to show in LLM debug output |

### Testing
```bash
pytest tests/                              # All tests
pytest tests/test_graph.py -v              # Single file
pytest tests/test_graph.py::test_name -v   # Single test
pytest tests/ -k "todo"                    # Tests matching pattern
pytest tests/ --cov=src                    # With coverage
```

### Linting (CitationEngine)
CitationEngine lives in its own repo: https://github.com/Knaeckebrothero/CitationEngine
```bash
cd CitationEngine
ruff check src/ tests/          # Lint
ruff format src/ tests/         # Format
mypy src/                       # Type check
```

### Cockpit (Angular Frontend)
```bash
cd cockpit
npm install                     # Install dependencies
npm start                       # Dev server at http://localhost:4200
npm run build                   # Production build
npm test                        # Run vitest tests
npm run test:watch              # Watch mode
```

**Runtime API Configuration**: Edit `cockpit/src/assets/env.js` to configure the API URL at runtime (no rebuild required):
```javascript
window['env']['apiUrl'] = 'http://your-server:8085/api';
```

### Orchestrator (Backend API)
```bash
cd orchestrator
pip install -r requirements.txt
uvicorn main:app --reload --port 8085
```

## Architecture

### Universal Agent Pattern

Single codebase configured for different roles via YAML configs in `config/`. See `config/README.md` for full documentation on creating custom agent configs.

Config structure:
- `config/defaults.yaml` - Framework defaults (all configs extend this)
- `config/schema.json` - JSON Schema for config validation
- `config/prompt_matrix.yaml` - Base prompt matrix (model family → prompt filename)
- `config/instruction_matrix.yaml` - Base instruction matrix (model family → template filename)
- `config/prompts/` - System prompts (systemprompt.txt, persona.txt, strategic.txt, tactical.txt, summarization_prompt.txt + minimax variants)
- `config/templates/` - Instruction templates (instructions.md, instructions_minimax.md, workspace_template.md, todo_guide.md, phase_retrospective_template.md)
  - `strategic_todos_initial.yaml` - First strategic phase (job startup)
  - `strategic_todos_transition.yaml` - Subsequent strategic phases (phase transitions)
  - `strategic_todos_resume.yaml` - Resumed jobs
- `config/settings_matrix.yaml` - Model-family-specific inference params & context limits
- `config/experts/` - Pre-built agent roles (developer, scholar, critic)
- `config/my_agent.yaml` - Custom single-file config
- `config/my_agent/config.yaml` - Custom directory config (with prompt/instruction overrides)

### Phase Alternation Model

The agent uses a single ReAct loop alternating between strategic and tactical phases:

```
init_workspace → init_strategic_todos
       ↓
    execute ─→ tools ─→ check_todos ──→ todos done? ──no──┐
       ↑                                                   │
       └───────────────────────────────────────────────────┘
                           │ yes
                           ↓
                    archive_phase → handle_transition → check_goal
                                                            │
                                   ┌──── goal achieved? ────┤
                                   ↓ no                     ↓ yes
                            back to execute                END
```

**Strategic Phase** (review-reflect-adapt cycle):
- **Review**: Uses git tools to see what actually changed, writes retrospective to `archive/`
- **Reflect**: Rewrites workspace.md (compact, don't append) - removes redundancy with plan.md
- **Adapt**: Updates plan.md with outcomes, adjusts phase sizing, adds intermediate phases if needed
- **Plan**: Creates right-sized todos (3-7 per phase, adapted to task complexity) via `next_phase_todos`
- Has access to `job_complete` tool (tactical does not)

**Tactical Phase** (execution mode):
- Executes domain-specific work using todos
- Uses `todo_complete` to mark items done
- Transitions back to strategic when all todos complete

### Key Todo Tools

| Tool | Phase | Purpose |
|------|-------|---------|
| `next_phase_todos` | Strategic | Stage todos for next tactical phase |
| `todo_complete` | Both | Mark current todo complete with notes |
| `todo_rewind` | Both | Roll back to re-execute failed todo |
| `mark_complete` | Both | Signal phase/task completion |
| `job_complete` | Strategic only | Signal final job completion |

### Configuration Inheritance

Configs use `$extends: defaults` to inherit from `config/defaults.yaml`. Deep merge applies: objects merge recursively, arrays replace entirely, `null` clears a key. The config schema (`config/schema.json`) provides IDE autocompletion via the YAML language server comment.

```yaml
# yaml-language-server: $schema=schema.json
$extends: defaults

agent_id: my_agent
display_name: My Custom Agent

tools:
  research:
    - web_search
  citation:
    - cite_web
```

### Two Matrix Systems

The agent uses two parallel matrix systems to resolve prompt and template files, both inheriting from `MatrixResolver` (`src/core/loader.py`):

**Prompt Matrix** (`prompt_matrix.yaml`) — resolves system prompts (`systemprompt`, `persona`, `strategic`, `tactical`, `summarization`) to filenames. File search: expert directory → `config/prompts/`.

**Instruction Matrix** (`instruction_matrix.yaml`) — resolves non-prompt templates (`instructions`, `strategic_todos_initial`, `strategic_todos_transition`, `strategic_todos_resume`, `workspace_template`, `todo_guide`) to filenames. File search: expert directory → `config/templates/`.

Both use the same 4-level resolution chain:
1. Expert matrix → model-specific key → type
2. Expert matrix → `"default"` key → type
3. Base matrix → model-specific key → type
4. Base matrix → `"default"` key → type

Once the filename is determined, `FileResolver` locates the actual file — checking the expert directory first, then falling back to the framework directory. See `config/README.md` for full documentation.

### Autonomy Levels

Controls when the agent pauses for human review (`autonomy` key in config):

| Level | Behavior |
|-------|----------|
| `full` | Never freezes, runs to completion autonomously |
| `review` | Freezes at job completion for human review (default) |
| `partial` | Freezes at phase boundaries and job completion |
| `guided` | Freezes after every tactical phase |
| `dependent` | Freezes after every phase (strategic and tactical) |

### Instruction Files System

Config-driven auto-injection of guidance documents before specific tool calls:

```yaml
instruction_files:
  - file: todo_guide.md
    trigger: before_tool:next_phase_todos
    enforce: true   # passive: tool rejects until agent reads file
    # enforce: false  # active: system injects content as transient message
```

### Shell Command Execution

Two modes controlled by `shell.mode` in config (default: `stateless`):

**Stateless mode** (default): `run_command(command, timeout, tail)` — simple command→output. Runs in a hidden persistent tmux tab. Returns exit code + last 30 lines of stdout. Use `shell_read()` to page through full scrollback. Interactive prompts return an error (use sshpass, `-y` flags, etc. instead).

**Persistent mode** (`shell.mode: persistent`): `shell_execute(command, name, tail, is_async, keys)` — full tab management with named tabs, keystroke mode, and async execution. For interactive workflows (debugging, long-running processes).

Both modes use `shell_read(name, offset, lines)` for reading scrollback history.

**Implementation:** `src/tools/coding/shell_manager.py` (ShellManager, tmux control via libtmux), `src/tools/coding/shell_tools.py` (tool definitions). Requires `tmux` installed. See `docs/shell.md` for design rationale and `docs/persistent_shell.md` for the persistent mode design.

### Claude Code Delegation

The `claude_code` tool spawns Claude Code CLI sessions in print mode (`-p`) to delegate heavy work. Supports multi-turn sessions via `session_id`. Requires Claude Code CLI installed and authenticated (`claude auth login`).

```yaml
claude_code:
  model: claude-opus-4-6
  effort_level: high  # low, medium, high
```

### KeyRing (API Key Rotation)

`src/llm/key_ring.py` provides tiered API key fallback with cooldown-based rotation. Comma-separated keys in environment variables (e.g., `OPENAI_API_KEY=key1,key2,key3`) are automatically rotated on auth/quota failures. Thread-safe, with masked key logging.

### Resolved Config JSONB

On first run, `serialize_resolved_config()` captures the fully resolved config (agent config + all prompt text + all instruction text) and stores it in the `resolved_config` JSONB column on the jobs table. On resume, `load_config_from_resolved()` reconstructs the config from this snapshot, bypassing disk resolution entirely. This prevents config drift when files change between runs.

Tool categories (`workspace`, `core`, `document`, `research`, `citation`, `graph`, `sql`, `mongodb`, `git`, `coding`, `evaluation`) map to modules under `src/tools/`. See `config/README.md` for the full tool listing per category. Database tool categories (`graph`, `sql`, `mongodb`) are injected/stripped by the orchestrator based on attached datasources, not by config YAML. The `coding` category provides shell tools (see "Shell Command Execution" above).

**Phase-specific tool filtering**: Tools declare phase availability via `phases` in `TOOL_REGISTRY` (`src/tools/registry.py`). `filter_tools_by_phase()` removes unavailable tools before binding to the LLM. `job_complete` is strategic-only.

### Multi-Database Architecture

**System databases** (infrastructure, configured via environment variables):

| Database | Purpose | Connection |
|----------|---------|------------|
| PostgreSQL (App DB) | Jobs, agents, requirements, datasources, builder | `DATABASE_URL`, standard postgres:15 |
| PostgreSQL (Vector DB) | Citations, sources, source_embeddings, memories, knowledge_index | `VECTOR_DB_URL`, pgvector/pgvector:pg15 |
| MongoDB | LLM request logging (optional) | Audit trail and token tracking |

The App DB uses standard PostgreSQL (no pgvector). All tables requiring vector operations live in the Vector DB (`VECTOR_DB_URL`, required). Schema: `orchestrator/database/vector_schema.sql`.

**External datasources** (user-configured, attached to jobs via the cockpit UI or `DEFAULT_DS_*` env vars):

| Type | Tools Provided | Connection |
|------|---------------|------------|
| PostgreSQL | sql_query, sql_schema, sql_execute | Via datasource connector (`src/tools/sql/`) |
| Neo4j | execute_cypher_query, get_database_schema | Via datasource connector (`src/tools/graph/`) |
| MongoDB | mongo_query, mongo_aggregate, mongo_schema, mongo_insert, mongo_update | Via datasource connector (`src/tools/mongodb/`) |

See `docs/datasources.md` for the full datasource connector architecture.

### Workspace Structure

Per-job directory: `workspace/job_<uuid>/`
- `workspace.md` - Long-term memory (always in system prompt, persists across context compaction)
- `plan.md` - Strategic plan
- `todos.yaml` - Current task list (managed by TodoManager)
- `archive/` - Phase artifacts: archived todos (`todos_phase_{n}.yaml`) and retrospectives (`phase_{n}_retrospective.md`)
- `documents/` - Input documents
- `tools/` - Auto-generated tool documentation (one `.md` file per tool)
- `analysis/` - Validator working files (e.g., `requirement_input.md`)
- `.git/` - Git repository for workspace versioning (when `workspace.git_versioning: true`)
- Checkpoints: `workspace/checkpoints/job_<id>.db` (SQLite)

**Git Versioning**: When enabled (`workspace.git_versioning: true` in config), each workspace is a git repo. Auto-commits on todo completion with formatted messages, `.gitignore` auto-configured from `workspace.git_ignore_patterns`, tags mark phase boundaries (`phase_N_start`, `phase_N_end`). Use `git_log`, `git_show`, `git_diff` tools to query history.

### Context Safety (Three-Layer System)

The agent has a three-layer defense against context window overflow:

- **Layer 0** (HTTP-level): `ReasoningChatOpenAI` in `src/llm/reasoning_chat.py` catches token limit errors at the HTTP request layer, raising `ContextOverflowError`
- **Layer 1** (Pre-request): The `execute` node in `src/graph.py` validates token count *before* calling the LLM, triggering compaction proactively
- **Layer 2** (Emergency recovery): If an overflow still occurs, the graph catches `ContextOverflowError` and performs emergency compaction, then retries

Compaction preserves the 10 most recent tool results and sanitizes orphaned `ToolMessage`s (via `sanitize_message_history` in `src/core/context.py`).

Config keys (defaults — overridden per model family via `config/settings_matrix.yaml`):
- `limits.context_threshold_tokens`: 80000 (default, model-dependent)
- `limits.message_count_threshold`: 300 messages
- `limits.summarization_chunk_size`: 80000 (default, model-dependent)
- `context_management.keep_recent_tool_results`: 150

### Workspace-Centric Memory Model

Long-term memory lives in files, not in LLM context:

- `workspace.md` is injected into every LLM call as a transient fake tool result (see `src/core/workspace_injection.py`). It is never stored in state, preventing it from being summarized away during context compaction. This is the agent's persistent memory across context windows.
- `plan.md` holds the strategic plan, updated at phase boundaries.
- `archive/` preserves phase history (retrospectives + archived todos) for review during strategic phases.

This separation means workspace.md survives context compaction while the conversation history gets summarized. Shell state (`<terminal_state>`) uses the same transient injection pattern for persistent terminal tabs.

### Memory Light (Opt-in Recall System)

When `memory.enabled: true` in config, agents get a PostgreSQL-backed memory system (pgvector hybrid search) that stores and retrieves insights across context compactions. Memories are extracted from 5 channels (observer, todo completion, compaction, phase archive, tool errors) and injected as transient messages like workspace.md. Disabled by default. See `docs/features/memory_light.md` for full design and config keys.

### VM Lifecycle Management (Optional)

The orchestrator can provision KubeVirt VMs for agent jobs, with two auto-selected backends:

| Mode | When | How |
|------|------|-----|
| **Direct** (same-cluster) | `kubernetes` client available + `VM_TEMPLATE_PATH` set | Orchestrator calls KubeVirt API directly |
| **NATS** (cross-cluster) | `NATS_URL` configured | Orchestrator publishes to NATS, VM Controller on agent cluster handles K8s API |
| **Disabled** | Neither configured | No VM features, system works as before |

NATS takes priority when both are available. All VM features are fully optional — the system degrades gracefully when unconfigured, following the same pattern as MongoDB (`orchestrator/database/mongodb.py`).

**Implementation:** `orchestrator/services/vm_provisioner.py` (unified provisioner, auto-selects backend), `orchestrator/services/nats_bridge.py` (NATS subscriptions + publishers), `vm-controller/controller.py` (agent cluster side). REST endpoints: `POST/GET /api/vms`, `GET/DELETE /api/vms/{job_id}`.

**NATS subjects:**

| Subject | Direction | Purpose |
|---------|-----------|---------|
| `vm.lifecycle.create` | Orchestrator → VM Controller | Request VM creation |
| `vm.lifecycle.delete` | Orchestrator → VM Controller | Request VM teardown |
| `vm.lifecycle.get` | Orchestrator ↔ VM Controller | Request/reply for live status |
| `vm.lifecycle.status` | VM Controller → Orchestrator | Creation/deletion results |
| `agent.vm.{job_id}.control` | Orchestrator → Daemon | Freeze/resume/terminate |
| `agent.vm.*.register` | Daemon → Orchestrator | VM ready notification |
| `agent.vm.*.heartbeat` | Daemon → Orchestrator | Periodic health updates |
| `agent.vm.*.status` | Daemon → Orchestrator | Agent process exit |
| `sudo.request.{vm_id}.{job_id}` | Daemon → Orchestrator | Sudo approval request/reply |

Control commands (freeze/resume/terminate) require NATS since they target the management daemon inside the VM. On same-cluster without NATS, `cancel_job` compensates by deleting the VM directly via K8s API.

### Sudo Approval Gate (Optional)

Human-in-the-loop privilege escalation for agents in VMs. When an agent runs `sudo`, the command is intercepted by a plugin, forwarded via a Go daemon over NATS to the orchestrator, and held for human approval via the cockpit UI or MCP tools.

**Components:** C plugin (`sudo-gate-plugin/`), Go daemon (`sudo-gated/`), orchestrator service (`orchestrator/services/sudo_gate.py`), cockpit UI (`/sudo` route), MCP tools (`list_sudo_requests`, `approve_sudo_request`, `deny_sudo_request`).

**REST endpoints:** `/api/sudo/events` (SSE), `/api/sudo/requests` (CRUD), `/api/sudo/rules` (auto-approval management).

All sudo gate features are fully optional — the system works without them (agents get unrestricted sudo, the pre-gate default). See `docs/features/sudo_approval_gate.md` for the full design and implementation roadmap.

See `docs/features/vm_backend.md` for the full workspace backend design and `docs/features/nats.md` for the messaging layer architecture.

## Key Entry Points

- `init.py` - Root initialization script (orchestrates database + workspace setup)
- `agent.py` - CLI entry point (delegates to `src/agent.py`)
- `src/graph.py` - LangGraph state machine (phase alternation graph, the core loop)
- `src/agent.py` - UniversalAgent main class
- `src/core/loader.py` - Config loading, LLM creation, matrix resolvers, resolved config serialization
- `src/core/context.py` - ContextManager (token counting, compaction, three-layer safety)
- `src/tools/registry.py` - Tool metadata registry with phase filtering
- `orchestrator/main.py` - FastAPI orchestrator endpoints (job management, agent heartbeat, VM lifecycle)
- `orchestrator/services/vm_provisioner.py` - Unified VM provisioner (NATS or direct K8s)
- `orchestrator/services/nats_bridge.py` - NATS bridge for cross-cluster VM communication
- `orchestrator/services/builder_tools.py` - Instruction builder tool schemas (cockpit chat assistant)
- `orchestrator/mcp/` - MCP server for Claude Code integration

**Directory layout:** `src/core/` (state, workspace, context, phase transitions, shell injection), `src/core/backends/` (LocalBackend, RemoteBackend for VM workspaces), `src/managers/` (Todo, Memory, Plan, Git), `src/services/` (vision, document rendering, embeddings, recall), `src/llm/` (LLM wrappers, key rotation), `src/tools/` (tool implementations by category), `src/database/` (PostgreSQL/Neo4j/MongoDB managers, SQL in `queries/postgres/*.sql`), `src/api/` (agent FastAPI app), `config/` (YAML configs, prompts, templates), `orchestrator/` (backend API + MCP + builder), `orchestrator/services/` (nats_bridge, vm_provisioner, sudo_gate, gitea, builder, completion), `cockpit/` (Angular frontend), `vm-controller/` (KubeVirt VM lifecycle on agent cluster), `sudo-gated/` (Go daemon for sudo approval gate), `sudo-gate-plugin/` (C sudo approval plugin), [`CitationEngine`](https://github.com/Knaeckebrothero/CitationEngine) (separate repo, pip-installed).

**Design documents:** `docs/` contains concept/design documents for features — `docs/persistent_shell.md`, `docs/datasources.md`, `docs/features/` (memory_light, projects, repo_datasource, prompting, summary_tool, etc.). These are architectural specs, not user-facing docs.

### Vision Services

The agent supports visual content analysis from documents and images via `src/services/`:

| Service | Purpose |
|---------|---------|
| `VisionHelper` | Generates text descriptions of images using a multimodal model |
| `DocumentRenderer` | Renders PDF/PPTX/DOCX pages as PNG images |
| `DescriptionCache` | Caches vision descriptions to avoid repeated API calls |

**Multimodal Configuration** (`config/defaults.yaml`):
```yaml
llm:
  model: gpt-4o
  multimodal: true   # Model can process images directly
  # OR
  multimodal: false  # Model is text-only, uses VisionHelper for descriptions
  parallel_tool_calls: false  # Set to true for models that handle parallel calls well
  reasoning_level: high       # For DeepSeek R1-style models: high, medium, low
```

**How it works:**
- `multimodal: true` → Agent receives base64-encoded page screenshots
- `multimodal: false` → Agent receives AI-generated text descriptions of visual content

**Enhanced `read_file` tool:**
```python
read_file(
    path="doc.pdf",
    page_start=1,
    page_end=3,
    describe="What values are shown in this chart?"  # Optional visual query
)
```

Supports: PDF, PPTX, DOCX, PNG, JPG, GIF, WebP, BMP, TIFF

## Environment Variables

Required in `.env`:
- `OPENAI_API_KEY` - LLM API key (or compatible API)
- `DATABASE_URL` - PostgreSQL connection string
- `LLM_BASE_URL` - Custom endpoint (optional, for vLLM/Ollama)

**Optional providers:**
- `ANTHROPIC_API_KEY` - For Claude models (claude-*)
- `GOOGLE_API_KEY` - For Gemini models (gemini-*)
- `GROQ_API_KEY` - For Groq fast inference
- `OPENROUTER_API_KEY` - For OpenRouter (openrouter/* models, 300+ models via unified API)
- `OPENROUTER_REFERER` - Optional: your site URL for OpenRouter leaderboard
- `OPENROUTER_TITLE` - Optional: your app name for OpenRouter leaderboard
- `TAVILY_API_KEY` - Web search
- `VECTOR_DB_URL` - Vector DB connection (required, pgvector instance for citations + memories + knowledge)
- `MONGODB_URL` - LLM request archiving (audit trail)
- `DEFAULT_DS_*` - Default datasources (see `docs/datasources.md`)

**Vision Model** (for text-only agents):
- `VISION_API_KEY` - API key for vision model (defaults to `OPENAI_API_KEY`)
- `VISION_BASE_URL` - Vision API endpoint (defaults to `OPENAI_BASE_URL`)
- `VISION_MODEL` - Model to use (default: `gpt-4o-mini`)
- `VISION_TIMEOUT` - Request timeout in seconds (default: `120`)

**VM Lifecycle** (optional):
- `NATS_URL` - NATS server URL for cross-cluster VM communication
- `VM_TEMPLATE_PATH` - Path to KubeVirt VM template YAML (for direct same-cluster provisioning)
- `VM_NAMESPACE` - Target K8s namespace for VMs (default: `agent-vms`)
- `DEFAULT_VM_IMAGE` - Default VM container disk image

## Service Ports

| Service | Port |
|---------|------|
| Agent API | 8001 |
| NATS (client connections) | 4222 |
| NATS (monitoring) | 8222 |
| MCP Server (Claude Code) | 8055 |
| Orchestrator API | 8085 |
| Cockpit Frontend (docker) | 4000 |
| Cockpit Frontend (npm start) | 4200 |
| Gitea | 3000 |
| pgAdmin | 5050 |
| PostgreSQL (App DB) | 5432 |
| PostgreSQL (Vector DB) | 5433 |
| Mongo Express | 8081 |
| MongoDB | 27017 |
| VPN Workstation forward | 8090 (host) → 8080 (container) |
| Dozzle (container logs) | 9999 |

## Debugging

**Workspace files**: `workspace/job_<uuid>/` (workspace.md, todos.yaml, plan.md)

**Checkpoints**: `workspace/checkpoints/job_<id>.db` (SQLite for resume)

**Phase Snapshots**: `workspace/phase_snapshots/job_<id>/phase_<n>/` - Created automatically at phase boundaries by `PhaseSnapshotManager` (`src/core/phase_snapshot.py`). Each snapshot includes checkpoint.db, workspace.md, plan.md, todos.yaml, and archive/. To recover a corrupted job to a specific phase:
```bash
python agent.py --job-id <id> --list-phases            # See available snapshots
python agent.py --job-id <id> --recover-phase 2 --resume  # Roll back to phase 2 and continue
```

**Logs**: `workspace/logs/job_<id>.log`

```bash
# Clean up checkpoint/log files
rm workspace/checkpoints/job_*.db workspace/logs/job_*.log
```

**MongoDB LLM Viewer** (requires `MONGODB_URL` in .env):
```bash
python DEPRECATED_scripts/view_llm_conversation.py --list                    # List jobs
python DEPRECATED_scripts/view_llm_conversation.py --job-id <uuid>           # View conversation
python DEPRECATED_scripts/view_llm_conversation.py --job-id <uuid> --stats   # Token usage stats
python DEPRECATED_scripts/view_llm_conversation.py --job-id <uuid> --audit   # Full audit trail
```

**Instruction Builder** (cockpit chat assistant):
The orchestrator provides an SSE-based chat endpoint (`/api/builder/stream`) that powers the cockpit's instruction builder. The builder LLM has server-side tools (`orchestrator/services/builder_tools.py`) for job inspection, database queries, monitoring, citations, and workspace file editing. Tool calls are dispatched via `orchestrator/services/builder_dispatch.py`. Workspace edit tools (`write_workspace_file`, `edit_workspace_file`) are forwarded to the frontend as proposals requiring user approval; all other tools execute server-side.

**Orchestrator MCP Server** (for Claude Code integration):
The project includes `.mcp.json` for MCP server configuration. The MCP server runs on port 8055 (HTTP transport) as a separate container (`srw-mcp`) and proxies to the orchestrator API on 8085. Health check: `http://localhost:8055/health`. See `orchestrator/mcp/` for the full tool listing (debug, actions, git history, workspace, monitoring, database inspection, citations).
