# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Graph-RAG system for **requirement traceability and compliance checking** in a car rental business context (FINIUS). Uses LangGraph + LLMs to extract requirements from documents, validate them against a Neo4j knowledge graph, and track GoBD/GDPR compliance.

**Architecture:** Two-agent autonomous system (Creator + Validator) with a Streamlit Dashboard for job management, unified via the **Universal Agent** pattern. Agent behavior is configured via JSON files in `configs/{name}/` that extend framework defaults via `$extends`.

## Commands

```bash
# Setup
pip install -r requirements.txt
pip install -e ./citation_tool[full]
cp .env.example .env

# Initialize databases with seed data
python scripts/app_init.py --force-reset --seed

# Run tests
pytest tests/                            # All tests
pytest tests/test_graph.py               # Single test file
pytest tests/ -k "managers"              # Tests matching pattern

# Metamodel validation
python validate_metamodel.py              # All checks
python validate_metamodel.py --check A1   # Specific check
```

### Local Development

```bash
# Start databases only (no dev compose file - use main docker-compose.yml with selective up)
podman-compose up -d postgres neo4j

# Run Universal Agent (config resolution: configs/{name}/ → src/config/{name}.json)
python agent.py --config creator --document-path ./data/doc.pdf --prompt "Extract requirements"
python agent.py --config creator --document-dir ./data/context/ --prompt "Extract all requirements"
python agent.py --config creator --job-id <uuid> --stream --verbose  # Resume with streaming
python agent.py --config validator --port 8002                        # API server mode
python agent.py --config creator --polling-only                       # Polling loop only

# Stop databases
podman-compose down
```

### Docker Deployment

```bash
# Full system
podman-compose up -d
podman-compose logs -f creator validator dashboard

# Database access
podman-compose exec postgres psql -U graphrag -d graphrag
podman-compose exec neo4j cypher-shell -u neo4j -p neo4j_password

# Rebuild
podman-compose build --no-cache creator validator dashboard
```

### Job Management

Jobs are managed via the **Streamlit Dashboard** (http://localhost:8501) or CLI scripts:

```bash
# Dashboard (primary interface)
cd dashboard && streamlit run app.py

# CLI debugging tools (in scripts/)
python scripts/job_status.py --job-id <uuid> --progress
python scripts/list_jobs.py --status pending --stats
python scripts/cancel_job.py --job-id <uuid> --force
```

## Configuration

**Environment (`.env`):** `NEO4J_URI`, `NEO4J_PASSWORD`, `DATABASE_URL`, `OPENAI_API_KEY`, `LLM_BASE_URL` (optional), `TAVILY_API_KEY`

**Agent configs:** Two-tier configuration system with inheritance:
- `src/config/defaults.json` - Framework defaults (LLM, workspace, tools, polling, limits)
- `configs/{name}/config.json` - Deployment configs that extend defaults via `$extends`
- `configs/{name}/*.md` - Deployment-specific prompts (override framework prompts)

Config resolution: `--config creator` checks `configs/creator/config.json` first, falls back to `src/config/creator.json`.

See `src/config/schema.json` for full spec.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      DASHBOARD (port 8501)                          │
│                 Streamlit UI - Job Management                       │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
       ┌──────────────────┴──────────────────┐
       │                                      │
       ▼                                      ▼
┌──────────────────┐                ┌──────────────────┐
│ CREATOR (8001)   │                │ VALIDATOR (8002) │
│ (Universal Agent)│                │ (Universal Agent)│
│                  │                │                  │
│ Document → Extract  requirements  │ Relevance check  │
│ Research → Cache │ (PostgreSQL)  │ Fulfillment check│
│                  │◄──────────────►│ Graph integration│
└──────────────────┘                └────────┬─────────┘
                                             │
                                             ▼
                                    ┌──────────────────┐
                                    │     Neo4j        │
                                    │ (Knowledge Graph)│
                                    └──────────────────┘
```

**Source locations:**
- `src/agent.py`, `src/graph.py` - **Universal Agent**: Config-driven LangGraph workflow
- `src/core/` - Agent internals (state.py, context.py, loader.py, workspace.py, archiver.py)
- `src/core/loader.py` - **Config system**: `resolve_config_path()`, `load_and_merge_config()`, `PromptResolver`
- `src/managers/` - **Manager layer** (todo.py, plan.py, memory.py) for nested loop architecture
- `src/tools/` - Modular tool implementations (registry.py loads tools dynamically)
- `src/api/` - FastAPI application for containerized deployment
- `src/config/` - Framework defaults (defaults.json) and prompts (Markdown in `prompts/`)
- `src/utils/` - Shared utilities (metamodel validator, document processor, citation utils)
- `src/database/` - Database classes (postgres_db.py, neo4j_db.py, mongo_db.py) and queries/
- `configs/` - **Deployment configs** (creator/, validator/) with `config.json` + prompt overrides
- `dashboard/` - **Streamlit Dashboard** for job management (multi-page: app.py, pages/, db.py, agents.py)
- `scripts/` - CLI tools and init scripts (app_init.py, init_*.py, job_status.py, list_jobs.py, cancel_job.py)
- `agent.py` - **Entry point**: Top-level script that imports from `src/agent.py`

**Manager layer:** Task management uses `src/managers/` (todo.py, plan.py, memory.py) for the nested loop graph architecture (`src/graph.py`).

**Agent data flow:**
1. Creator polls `jobs` table → processes document → writes to `requirements` table (status: pending)
2. Validator queries `requirements` where `neo4j_id IS NULL` → validates → integrates into Neo4j → sets `neo4j_id`

## Key Implementation Details

### Metamodel (FINIUS v2.0)

**Nodes:** `Requirement`, `BusinessObject`, `Message`

**Key Relationships:**
- `FULFILLED_BY_OBJECT`, `NOT_FULFILLED_BY_OBJECT` (Requirement → BusinessObject)
- `FULFILLED_BY_MESSAGE`, `NOT_FULFILLED_BY_MESSAGE` (Requirement → Message)
- `REFINES`, `DEPENDS_ON`, `TRACES_TO`, `SUPERSEDES` (Requirement → Requirement)

Schema: `data/metamodell.cql`. Seed data: `data/seed_data.cypher`.

### Neo4j Query Guidelines

- Use `elementId(node)` not deprecated `id(node)`
- Always use `LIMIT` for large result sets
- Fulfillment query pattern:
  ```cypher
  MATCH (r:Requirement)-[rel:FULFILLED_BY_OBJECT|NOT_FULFILLED_BY_OBJECT]->(bo:BusinessObject)
  RETURN r.rid, type(rel), rel.confidence, bo.name
  ```

### PostgreSQL Schema

Schema in `src/database/queries/postgres/schema.sql`, database class in `src/database/postgres_db.py`:
- `jobs` - Job tracking (status, creator_status, validator_status, document_path)
- `requirements` - Extracted requirements with validation state. Validator queries `WHERE neo4j_id IS NULL` for unprocessed items.
- `job_summary` - View aggregating requirement counts by status

Neo4j database class in `src/database/neo4j_db.py`.
MongoDB database class in `src/database/mongo_db.py`.

### Universal Agent Pattern

The Universal Agent (`src/`) is the primary agent implementation with a **nested loop architecture**:

**Key files:**
- `agent.py` - Top-level entry point (imports from src/)
- `src/agent.py` - UniversalAgent class with run loop and LLM integration
- `src/graph.py` - **Nested loop graph** (initialization → outer loop → inner loop)
- `src/core/state.py` - UniversalAgentState TypedDict with loop control fields
- `src/core/context.py` - Context management (ContextManager, token counting, compaction)
- `src/core/loader.py` - **Config system** with inheritance and prompt resolution
- `src/core/workspace.py` - Filesystem workspace for agent
- `src/core/archiver.py` - Audit logging for graph execution (MongoDB-backed)
- `src/api/app.py` - FastAPI application for containerized deployment

**Manager layer (`src/managers/`):**
- `todo.py` - **TodoManager** (stateful): Task tracking with add/complete/archive
- `plan.py` - **PlanManager** (service): Read/write main_plan.md, phase detection
- `memory.py` - **MemoryManager** (service): Read/write workspace.md, section management

**Nested loop architecture:**
```
INITIALIZATION → read_instructions → create_plan → init_todos
                                        ↓
OUTER LOOP (strategic) → read_plan → update_memory → create_todos
                                        ↓
INNER LOOP (tactical) → execute (ReAct) → check_todos → archive_phase
                                        ↓
                            check_goal → END or back to OUTER LOOP
```

**Configuration inheritance:**
```
src/config/defaults.json           ← Framework defaults
        ↑ $extends
configs/creator/config.json        ← Deployment overrides (agent_id, tools, polling, etc.)
configs/creator/instructions.md    ← Deployment prompts (override framework prompts/)
```

Merge semantics (`deep_merge` in loader.py):
- Objects (dicts): Recursively merge
- Arrays (lists): Override replaces entirely
- Scalars: Override replaces
- `null` in override: Clears the key

Config fields:
- `$extends`: Parent config name (resolved via `resolve_config_path()`)
- `agent_id`, `display_name`: Agent identity
- `llm`: Model, temperature, reasoning level, base_url
- `workspace.structure`: Directory layout, `instructions_template`
- `tools`: Categories (workspace, todo, domain, completion)
- `polling`: Table, status_field, intervals, use_skip_locked
- `limits`: max_iterations, context_threshold_tokens
- `connections`: postgres, neo4j (boolean flags)

**Key features:**
- Reads `instructions.md` from workspace to understand task context
- Uses `main_plan.md` for strategic planning (read at phase transitions)
- Uses `workspace.md` as persistent memory (injected into system prompt, like CLAUDE.md)
- TodoManager for tactical execution with `archive()` at phase completion
- Automatic context compaction when approaching token limits
- Completion detection via `mark_complete` tool
- Audit logging via archiver (tracks LLM calls, tool calls, phase transitions)

**Graph execution flow:**
The nested loop graph creates its own managers inside `build_nested_loop_graph()`. The graph uses an audited tool node (`create_audited_tool_node`) that logs all tool calls and results for debugging and compliance.

**Creating a new agent type:**
1. Create `configs/<name>/config.json` extending `defaults`:
   ```json
   {"$extends": "defaults", "agent_id": "<name>", "display_name": "<Name> Agent", ...}
   ```
2. Create `configs/<name>/instructions.md` (task instructions)
3. Optionally add `configs/<name>/summarization_prompt.md` (override)
4. Add domain-specific tools to `src/tools/` if needed
5. Register new tools in `src/tools/registry.py`

### Tool System

Tools are organized in `src/tools/` and loaded dynamically by the registry:

| Category | Source File | Example Tools |
|----------|-------------|---------------|
| `workspace` | `workspace_tools.py` | read_file, write_file, list_files, search_files, get_workspace_summary |
| `todo` | `todo_tools.py` | todo_write, archive_and_reset |
| `domain` | `document_tools.py` | extract_document_text, chunk_document |
| `domain` | `search_tools.py` | web_search (Tavily) |
| `domain` | `citation_tools.py` | cite_document, cite_web, list_sources, get_citation |
| `domain` | `cache_tools.py` | add_requirement, list_requirements, get_requirement |
| `graph` | `graph_tools.py` | execute_cypher, get_schema, create_requirement_node |
| `completion` | `completion_tools.py` | mark_complete, job_complete |

**Tool context** (`ToolContext` dataclass in `tools/context.py`) provides tools with access to workspace, todo manager, and database connections.

**On-demand descriptions:** Tools marked with `defer_to_workspace=True` in their metadata use short descriptions in the LLM context, with full documentation generated in `workspace/tools/<name>.md`. See `description_override.py` and `description_generator.py`.

### Service Ports

| Service | Port |
|---------|------|
| Dashboard (Streamlit) | 8501 |
| Creator (Universal Agent) | 8001 |
| Validator (Universal Agent) | 8002 |
| Neo4j Bolt | 7687 |
| Neo4j Browser | 7474 |
| PostgreSQL | 5432 |

## Adding New Tools

When adding a new tool to the system:

1. **Create the tool function** in the appropriate file in `src/tools/`:
   - Workspace operations → `workspace_tools.py`
   - Document processing → `document_tools.py`
   - Graph operations → `graph_tools.py`
   - New category → create new `*_tools.py` file

2. **Add metadata** to the module's `*_TOOLS_METADATA` dict:
   ```python
   MYTOOLS_METADATA = {
       "my_tool": {
           "module": "src.tools.mytools",
           "function": "my_tool",
           "description": "Short description for LLM context",
           "category": "domain",
       },
   }
   ```

3. **Register in registry.py** by importing and updating `TOOL_REGISTRY`

4. **Add to deployment config** in `configs/<agent>/config.json` under the appropriate category:
   ```json
   "tools": {
       "domain": ["my_tool", ...]
   }
   ```

5. **Tool signature**: Tools receive `ToolContext` as first argument, then user parameters:
   ```python
   from src.tools.context import ToolContext

   def my_tool(ctx: ToolContext, param1: str, param2: int = 10) -> str:
       """Tool docstring becomes LLM description."""
       return "result"
   ```

## Testing

```bash
# Run all tests
pytest tests/

# Run specific test file
pytest tests/test_graph.py

# Run with verbose output
pytest tests/ -v

# Run tests matching pattern
pytest tests/ -k "managers"

# Run with coverage
pytest tests/ --cov=src

# Manager tests (nested loop architecture)
pytest tests/test_managers_todo.py tests/test_managers_plan.py tests/test_managers_memory.py -v
```

**Key test files:**
- `tests/test_managers_*.py` - Tests for TodoManager, PlanManager, MemoryManager
- `tests/test_graph.py` - Tests for routing functions and graph nodes

Test files follow the pattern `tests/test_<module>.py`. Use `pytest.fixture` for common setup.
