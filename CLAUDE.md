# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Graph-RAG system for requirement traceability and compliance checking. Uses LangGraph and LLMs to extract requirements from documents, validate them, and track GoBD/GDPR compliance.

## Commands

### Development Setup
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -e ./citation_tool[full]
cp .env.example .env  # Then configure API keys
```

### Database Management
```bash
# Start databases (development)
podman-compose -f docker-compose.dev.yaml up -d

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
```

**Environment Variables:**
| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |
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

### Linting (citation_tool only)
```bash
cd citation_tool
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

### Validation
```bash
python validate_metamodel.py --check all --json
```

## Architecture

### Universal Agent Pattern

Single codebase configured for different roles via JSON configs in `config/`. See `config/README.md` for full documentation on creating custom agent configs.

Config structure:
- `config/defaults.yaml` - Framework defaults (all configs extend this)
- `config/schema.json` - JSON Schema for config validation
- `config/prompts/` - System prompts (strategic.txt, tactical.txt, systemprompt.txt)
- `config/templates/` - File templates (workspace_template.md, strategic_todos_*.yaml, phase_retrospective_template.md)
- `config/my_agent.yaml` - Custom single-file config
- `config/my_agent/config.yaml` - Custom directory config (with prompt overrides)

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
- **Plan**: Creates right-sized todos (5-10 complex, 10-15 moderate, 15-20 simple) via `next_phase_todos`
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

Tool categories in config:
- `workspace`: File operations (read_file, write_file, list_files, etc.)
- `core`: Task management + completion (next_phase_todos, todo_complete, todo_rewind, mark_complete, job_complete)
- `document`: Document processing (extract_document_text, chunk_document, identify_requirement_candidates)
- `research`: Web search (web_search)
- `citation`: Citation management (cite_document, cite_web, list_sources, etc.)
- `graph`: Neo4j operations (execute_cypher_query, get_database_schema, validate_schema_compliance)
- `git`: Workspace version control (git_log, git_show, git_diff, git_status, git_tags)

**Phase-specific tool filtering**: `job_complete` is only available during strategic phases. Tactical phases cannot signal job completion directly.

### Multi-Database Architecture

| Database | Purpose | Connection |
|----------|---------|------------|
| PostgreSQL | Jobs, agents, requirements, citations | Async with asyncpg, namespace pattern: `db.jobs.create()` |
| MongoDB | LLM request logging (optional) | Audit trail and token tracking |
| Neo4j | Knowledge graph (optional) | Graph visualization and querying |

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

**Git Versioning**: When enabled (`workspace.git_versioning: true` in config), each workspace is a git repo. Auto-commits on todo completion, tags mark phase boundaries. Use `git_log`, `git_show`, `git_diff` tools to query history.

### Context Management

Token limits trigger automatic summarization:
- `context_threshold_tokens`: 80000 (default in defaults.yaml)
- `message_count_threshold`: 200 messages
- `keep_recent_tool_results`: 10 most recent preserved during compaction

## Key Source Directories

- `init.py` - Root initialization script (orchestrates all components)
- `src/graph.py` - LangGraph state machine (phase alternation graph)
- `src/agent.py` - UniversalAgent main class
- `src/init.py` - Agent workspace initialization
- `src/core/` - State management, workspace, context, phase transitions
  - `state.py` - UniversalAgentState TypedDict
  - `workspace.py` - WorkspaceManager for job directories
  - `loader.py` - Config loading, LLM creation, tool registry
  - `context.py` - ContextManager for token counting/compaction
- `src/managers/` - TodoManager, MemoryManager, PlanManager
- `src/services/` - Helper services
  - `vision_helper.py` - VisionHelper for image/document descriptions
  - `document_renderer.py` - DocumentRenderer for PDF/PPTX/DOCX to PNG
  - `description_cache.py` - DescriptionCache for caching vision outputs
- `src/tools/` - Tool implementations and registry
  - `registry.py` - Tool metadata registry with phase filtering
  - `context.py` - ToolContext dependency injection
  - `description_generator.py` - Generates tool documentation for workspace
- `src/database/` - PostgreSQL (asyncpg), MongoDB managers
  - `postgres_db.py` - Async PostgreSQL with namespaces
  - `schema.sql` - PostgreSQL schema
- `src/api/` - FastAPI application (agent API)
- `config/` - Configuration files, defaults, schema, and prompt templates
- `orchestrator/` - Backend API for monitoring and agent orchestration
  - `orchestrator/init.py` - Database initialization (PostgreSQL, MongoDB)
  - `orchestrator/main.py` - FastAPI endpoints
  - `orchestrator/database/` - Database layer (postgres.py, mongodb.py, schema.sql)
  - `orchestrator/services/` - Services (workspace)
  - `orchestrator/mcp/` - MCP server for Claude Code integration
- `cockpit/` - Angular frontend for debugging and job management
- `citation_tool/` - Separate installable package for citation management

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
- `TAVILY_API_KEY` - Web search
- `MONGODB_URL` - LLM request archiving (audit trail)
- `NEO4J_URI` - Knowledge graph (if connections.neo4j: true)

**Vision Model** (for text-only agents):
- `VISION_API_KEY` - API key for vision model (defaults to `OPENAI_API_KEY`)
- `VISION_BASE_URL` - Vision API endpoint (defaults to `OPENAI_BASE_URL`)
- `VISION_MODEL` - Model to use (default: `gpt-4o-mini`)
- `VISION_TIMEOUT` - Request timeout in seconds (default: `120`)

## Service Ports

| Service | Port |
|---------|------|
| Agent API | 8001 |
| Orchestrator API | 8085 |
| Cockpit Frontend (docker) | 4000 |
| Cockpit Frontend (npm start) | 4200 |
| PostgreSQL | 5432 |
| MongoDB | 27017 |

## Debugging

**Workspace files**: `workspace/job_<uuid>/` (workspace.md, todos.yaml, plan.md)

**Checkpoints**: `workspace/checkpoints/job_<id>.db` (SQLite for resume)

**Phase Snapshots**: `workspace/phase_snapshots/job_<id>/phase_<n>/` - Created at phase boundaries for recovery. Each snapshot includes checkpoint.db, workspace.md, plan.md, todos.yaml, and archive/.

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

**Orchestrator MCP Server** (for Claude Code integration):
The project includes `.mcp.json` for MCP server configuration. Claude Code can use these tools for AI-assisted debugging:

| Tool | Description |
|------|-------------|
| `list_jobs` | List jobs with status filter |
| `get_job` | Get job details by ID |
| `get_audit_trail` | Get paginated audit entries |
| `get_chat_history` | Get conversation turns |
| `get_todos` | Get current and archived todos |
| `get_graph_changes` | Get Neo4j graph mutations timeline |
| `get_llm_request` | Get full LLM request/response |
| `search_audit` | Search audit entries by pattern |

The MCP server is located at `orchestrator/mcp/`.
