# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Graph-RAG system for requirement traceability and compliance checking. Uses LangGraph and LLMs to extract requirements from documents, validate them against a Neo4j knowledge graph, and track GoBD/GDPR compliance.

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

# Initialize with sample data
python scripts/app_init.py --seed

# Reset databases
python scripts/app_init.py --force-reset --seed
python scripts/app_init.py --only-postgres --force-reset
python scripts/app_init.py --only-neo4j --force-reset

# Create backup of current state
python scripts/app_init.py --create-backup                  # Auto-named: backups/YYYYMMDD_NNN/
python scripts/app_init.py --create-backup my_backup        # Named: backups/YYYYMMDD_NNN_my_backup/

# Restore from backup
python scripts/app_init.py --restore-backup backups/20260117_001_my_backup
```

### Running Agents
```bash
# Process document
python agent.py --config creator --document-path ./data/doc.pdf --prompt "Extract requirements" --stream --verbose

# Process directory
python agent.py --config creator --document-dir ./data/example_data/ --prompt "Extract requirements" --stream --verbose

# Start as API server
python agent.py --config creator --port 8001
python agent.py --config validator --port 8002

# Resume from checkpoint
python agent.py --config creator --job-id <id> --resume
```

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

### Validation
```bash
python validate_metamodel.py --check all --json
```

## Architecture

### Universal Agent Pattern

Single codebase configured for different roles via JSON configs in `configs/`:

| Config | Purpose | Polls | Key Tools |
|--------|---------|-------|-----------|
| `creator` | Extract requirements from documents | `jobs` table | document, search, citation, cache |
| `validator` | Validate and integrate into Neo4j | `requirements` table | execute_cypher_query, get_database_schema, validate_schema_compliance |

Data flow: Creator → `requirements` table → Validator → Neo4j

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

**Strategic Phase** (planning mode):
- Reviews instructions and creates plan
- Updates workspace.md (long-term memory)
- Creates todos for tactical execution via `next_phase_todos`
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

Configs use `"$extends": "defaults"` to inherit from `src/config/defaults.json`. Deep merge applies - set value to `null` to clear inherited defaults.

```json
{
  "$extends": "defaults",
  "agent_id": "creator",
  "tools": { "domain": ["extract_document_text", "web_search", ...] }
}
```

Tool categories in config:
- `workspace`: File operations (read_file, write_file, etc.)
- `todo`: Task management (next_phase_todos, todo_complete, todo_rewind)
- `domain`: Agent-specific tools (set per config)
- `completion`: Phase/job signaling (mark_complete, job_complete)

### Multi-Database Architecture

| Database | Purpose | Connection |
|----------|---------|------------|
| PostgreSQL | Jobs, requirements, citations | Async with asyncpg, namespace pattern: `db.jobs.create()` |
| Neo4j | Knowledge graph | Session-based, namespace pattern: `db.requirements.create()` |
| MongoDB | LLM request logging (optional) | Audit trail and token tracking |

### Workspace Structure

Per-job directory: `workspace/job_<uuid>/`
- `workspace.md` - Long-term memory (always in system prompt, persists across context compaction)
- `plan.md` - Strategic plan
- `todos.yaml` - Current task list (managed by TodoManager)
- `archive/` - Phase artifacts and archived todos (`todos_phase_{n}.yaml`)
- `documents/` - Input documents
- `tools/` - Auto-generated tool documentation (one `.md` file per tool)
- `analysis/` - Validator working files (e.g., `requirement_input.md`)
- Checkpoints: `workspace/checkpoints/job_<id>.db` (SQLite)

### Context Management

Token limits trigger automatic summarization:
- `context_threshold_tokens`: 80000 (default)
- `message_count_threshold`: 200 messages
- `keep_recent_tool_results`: 10 most recent preserved during compaction

## Key Source Directories

- `src/graph.py` - LangGraph state machine (phase alternation graph)
- `src/agent.py` - UniversalAgent main class
- `src/core/` - State management, workspace, context, phase transitions
  - `state.py` - UniversalAgentState TypedDict
  - `workspace.py` - WorkspaceManager for job directories
  - `loader.py` - Config loading, LLM creation, tool registry
  - `context.py` - ContextManager for token counting/compaction
- `src/managers/` - TodoManager, MemoryManager, PlanManager
- `src/tools/` - Tool implementations and registry
  - `registry.py` - Tool metadata registry with phase filtering
  - `context.py` - ToolContext dependency injection
  - `graph_tools.py` - Neo4j tools (execute_cypher_query, get_database_schema, validate_schema_compliance)
  - `description_generator.py` - Generates tool documentation for workspace
- `src/database/` - PostgreSQL (asyncpg), Neo4j, MongoDB managers
  - `postgres_db.py` - Async PostgreSQL with namespaces
  - `neo4j_db.py` - Neo4j session-based with namespaces
  - `schema.sql` - PostgreSQL schema
- `src/api/` - FastAPI application
- `configs/` - Agent-specific configurations (`creator/`, `validator/`)
- `dashboard/` - Streamlit UI
- `citation_tool/` - Separate installable package for citation management

## Environment Variables

Required in `.env`:
- `OPENAI_API_KEY` - LLM API key
- `LLM_BASE_URL` - Custom endpoint (optional, for vLLM/Ollama)
- `TAVILY_API_KEY` - Web search (Creator agent)
- Database URLs configured in `.env.example`

## Service Ports

| Service | Port |
|---------|------|
| Dashboard | 8501 |
| Creator API | 8001 |
| Validator API | 8002 |
| Neo4j Bolt | 7687 |
| Neo4j HTTP | 7474 |
| PostgreSQL | 5432 |
| MongoDB | 27017 |

## Debugging

**Workspace files**: `workspace/job_<uuid>/` (workspace.md, todos.yaml, plan.md)

**Checkpoints**: `workspace/checkpoints/job_<id>.db` (SQLite for resume)

**Logs**: `workspace/logs/job_<id>.log`

```bash
# Clean up checkpoint/log files
rm workspace/checkpoints/job_*.db workspace/logs/job_*.log
```

**MongoDB LLM Viewer** (requires `MONGODB_URL` in .env):
```bash
python scripts/view_llm_conversation.py --list                    # List jobs
python scripts/view_llm_conversation.py --job-id <uuid>           # View conversation
python scripts/view_llm_conversation.py --job-id <uuid> --stats   # Token usage stats
python scripts/view_llm_conversation.py --job-id <uuid> --audit   # Full audit trail
```
