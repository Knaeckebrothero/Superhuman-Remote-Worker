# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Graph-RAG system for **requirement traceability and compliance checking** in a car rental business context (FINIUS). Uses LangGraph + LLMs to extract requirements from documents, validate them against a Neo4j knowledge graph, and track GoBD/GDPR compliance.

**Architecture:** Two-agent autonomous system (Creator + Validator) coordinated by an Orchestrator, unified via the **Universal Agent** pattern. The Universal Agent (`src/agent/`) is a config-driven, workspace-centric agent deployed as Creator or Validator by changing its JSON configuration (`src/config/agents/creator.json` or `src/config/agents/validator.json`).

## Commands

```bash
# Setup
pip install -r requirements.txt
pip install -e ./citation_tool[full]
cp .env.example .env

# Initialize databases with seed data
python src/scripts/app_init.py --force-reset --seed

# Run tests
pytest tests/                            # All tests
pytest tests/test_workspace_manager.py   # Single test file
pytest tests/ -k "test_vector"           # Tests matching pattern

# Metamodel validation
python validate_metamodel.py              # All checks
python validate_metamodel.py --check A1   # Specific check
```

### Local Development

```bash
# Start databases only
podman-compose -f docker-compose.dbs.yml up -d

# Run Universal Agent
python agent.py --config creator --document-path ./data/doc.pdf --prompt "Extract requirements"
python agent.py --config creator --job-id <uuid> --stream --verbose  # Resume with streaming
python agent.py --config validator --port 8002                        # API server mode
python agent.py --config creator --polling-only                       # Polling loop only

# Stop databases
podman-compose -f docker-compose.dbs.yml down -v
```

### Docker Deployment

```bash
# Full system
podman-compose up -d
podman-compose logs -f creator validator orchestrator

# Database access
podman-compose exec postgres psql -U graphrag -d graphrag
podman-compose exec neo4j cypher-shell -u neo4j -p neo4j_password

# Rebuild
podman-compose build --no-cache creator validator orchestrator
```

### Job Management

```bash
python start_orchestrator.py --document-path ./data/doc.pdf --prompt "Extract GoBD requirements" --wait
python job_status.py --job-id <uuid> --report
python list_jobs.py --status pending --stats
python cancel_job.py --job-id <uuid> --cleanup
```

## Configuration

**Environment (`.env`):** `NEO4J_URI`, `NEO4J_PASSWORD`, `DATABASE_URL`, `OPENAI_API_KEY`, `LLM_BASE_URL` (optional), `TAVILY_API_KEY`

**LLM/Agent settings (`src/config/llm_config.json`):** Model, temperature, reasoning_level per agent.

**Agent configs (`src/config/agents/*.json`):** Workspace structure, tools, polling behavior, limits. See `src/config/agents/schema.json` for full spec.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR (port 8000)                         │
│               Job Management, Monitoring, Reports                   │
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
- `src/agent/` - **Universal Agent**: Config-driven LangGraph workflow (agent.py, graph.py, state.py, context_manager.py, loader.py, workspace_manager.py, todo_manager.py)
- `src/agent/tools/` - Modular tool implementations (registry.py loads tools dynamically)
- `src/orchestrator/` - Job manager, monitor, reporter
- `src/core/` - Neo4j/PostgreSQL utils, metamodel validator, config
- `src/database/` - PostgreSQL schema files
- `src/config/agents/` - Agent configuration (JSON) and instruction templates (Markdown)

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

Schema in `src/database/schema.sql`:
- `jobs` - Job tracking (status, creator_status, validator_status, document_path)
- `requirements` - Extracted requirements with validation state. Validator queries `WHERE neo4j_id IS NULL` for unprocessed items.
- `job_summary` - View aggregating requirement counts by status

### Universal Agent Pattern

The Universal Agent (`src/agent/`) is the primary agent implementation:

**Key files:**
- `agent.py` - UniversalAgent class with run loop and LLM integration
- `graph.py` - LangGraph workflow definition
- `state.py` - UniversalAgentState TypedDict
- `context_manager.py` - Context management for runtime dependencies
- `loader.py` - Configuration and tool loading
- `app.py` - FastAPI application for containerized deployment
- `workspace_manager.py` - Filesystem workspace for agent
- `todo_manager.py` - Task management with archiving

**Configuration (`src/config/agents/<name>.json`):**
- `llm`: Model, temperature, reasoning level
- `workspace.structure`: Directory layout for agent's filesystem
- `tools`: Tool categories (workspace, todo, domain, completion)
- `polling`: Which table to poll, status fields, intervals
- `limits`: Max iterations, context threshold, retry count
- `context_management`: Compaction settings, summarization prompts

**Key features:**
- Reads `instructions.md` from workspace to understand task context
- Uses `main_plan.md` for strategic planning, `archive/` for todo history
- TodoManager for tactical execution with `archive_and_reset()` for context recovery
- Automatic context compaction when approaching token limits
- Completion detection via `mark_complete` tool

**Creating a new agent type:**
1. Create `src/config/agents/<name>.json` based on `schema.json`
2. Create `src/config/agents/instructions/<name>_instructions.md`
3. Add domain-specific tools to `src/agent/tools/` if needed
4. Register new tools in `src/agent/tools/registry.py`

### Tool System

Tools are organized in `src/agent/tools/` and loaded dynamically by the registry:

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
| Orchestrator | 8000 |
| Creator (Universal Agent) | 8001 |
| Validator (Universal Agent) | 8002 |
| Neo4j Bolt | 7687 |
| Neo4j Browser | 7474 |
| PostgreSQL | 5432 |
| Dashboard (Streamlit) | 8501 |
| Adminer (dev only) | 8080 |

## Adding New Tools

When adding a new tool to the system:

1. **Create the tool function** in the appropriate file in `src/agent/tools/`:
   - Workspace operations → `workspace_tools.py`
   - Document processing → `document_tools.py`
   - Graph operations → `graph_tools.py`
   - New category → create new `*_tools.py` file

2. **Add metadata** to the module's `*_TOOLS_METADATA` dict:
   ```python
   MYTOOLS_METADATA = {
       "my_tool": {
           "module": "mytools",
           "function": "my_tool",
           "description": "Short description for LLM context",
           "category": "domain",
       },
   }
   ```

3. **Register in registry.py** by importing and updating `TOOL_REGISTRY`

4. **Add to agent config** in `src/config/agents/<agent>.json` under the appropriate category:
   ```json
   "tools": {
       "domain": ["my_tool", ...]
   }
   ```

5. **Tool signature**: Tools receive `ToolContext` as first argument, then user parameters:
   ```python
   def my_tool(ctx: ToolContext, param1: str, param2: int = 10) -> str:
       """Tool docstring becomes LLM description."""
       return "result"
   ```

## Testing

```bash
# Run all tests
pytest tests/

# Run specific test file
pytest tests/test_todo_tools.py

# Run with verbose output
pytest tests/ -v

# Run tests matching pattern
pytest tests/ -k "workspace"

# Run with coverage
pytest tests/ --cov=src
```

Test files follow the pattern `tests/test_<module>.py`. Use `pytest.fixture` for common setup like database connections.
