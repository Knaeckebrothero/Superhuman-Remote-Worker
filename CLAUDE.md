# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Graph-RAG system for **requirement traceability and compliance checking** in a car rental business context (FINIUS). Uses LangGraph + LLMs to extract requirements from documents, validate them against a Neo4j knowledge graph, and track GoBD/GDPR compliance.

**Architecture:** Two-agent autonomous system (Creator + Validator) coordinated by an Orchestrator, now unified via the **Universal Agent** pattern. The Universal Agent is a config-driven, workspace-centric agent that can be deployed as Creator, Validator, or future agent types by changing its JSON configuration. See `masterplan.md` for design and `masterplan_roadmap.md` for implementation phases (1-5 complete, Phase 6 Testing pending).

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
pytest tests/test_workspace_manager.py   # Single test file
pytest tests/ -k "test_vector"           # Tests matching pattern

# Validate Python syntax
python -m py_compile src/**/*.py

# Metamodel validation
python validate_metamodel.py              # All checks
python validate_metamodel.py --check A1   # Specific check
```

### Local Development

```bash
# Start databases only
podman-compose -f docker-compose.dbs.yml up -d

# Run Universal Agent (preferred entry point)
python run_universal_agent.py --config creator --document-path ./data/doc.pdf --prompt "Extract requirements"
python run_universal_agent.py --config creator --job-id <uuid> --stream --verbose  # Resume with streaming
python run_universal_agent.py --config validator --port 8002                        # API server mode
python run_universal_agent.py --config creator --polling-only                       # Polling loop only

# Legacy agent runners (still functional)
python run_creator.py --document-path ./data/doc.pdf --prompt "Extract requirements"
python run_validator.py --verbose

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

**LLM/Agent settings (`config/llm_config.json`):** Model, temperature, reasoning_level per agent. Key sections: `creator_agent`, `validator_agent`, `orchestrator`, `context_management`.

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
│                  │                │                  │
│ Document → Chunk │ requirement_   │ Relevance check  │
│ Extract → Research cache (PG)    │ Fulfillment check│
│ Formulate → Cache│◄──────────────►│ Graph integration│
└──────────────────┘                └────────┬─────────┘
                                             │
                                             ▼
                                    ┌──────────────────┐
                                    │     Neo4j        │
                                    │ (Knowledge Graph)│
                                    └──────────────────┘
```

**Source locations:**
- `src/agents/universal/` - **Universal Agent**: Config-driven agent with 4-node LangGraph workflow (initialize → process ↔ tools → check → END)
- `src/agents/creator/` - Creator-specific document processing, candidate extraction, research, tools
- `src/agents/validator/` - Validator-specific relevance analysis, fulfillment checking, graph integration
- `src/agents/shared/` - Shared utilities: context manager, workspace manager, todo manager, tool registry, vector search
- `src/orchestrator/` - Job manager, monitor, reporter
- `src/core/` - Neo4j/PostgreSQL utils, metamodel validator, config
- `src/database/` - PostgreSQL schema files
- `config/agents/` - Agent configuration files (JSON) and instructions (Markdown)

**Agent data flow:**
1. Creator polls `jobs` table → processes document → writes to `requirement_cache` (status: pending)
2. Validator polls `requirement_cache` with `SKIP LOCKED` → validates → integrates into Neo4j

## Key Implementation Details

### Metamodel (FINIUS v2.0)

**Nodes:** `Requirement`, `BusinessObject`, `Message`

**Key Relationships:**
- `FULFILLED_BY_OBJECT`, `NOT_FULFILLED_BY_OBJECT` (Requirement → BusinessObject)
- `FULFILLED_BY_MESSAGE`, `NOT_FULFILLED_BY_MESSAGE` (Requirement → Message)
- `REFINES`, `DEPENDS_ON`, `TRACES_TO`, `SUPERSEDES` (Requirement → Requirement)

Schema: `data/metamodell.cql`. Seed data: `data/seed_data.cypher` (524 nodes, 1003 relationships).

### Prompt-Metamodel Coupling

Prompts in `config/prompts/` are tightly coupled to the metamodel. When modifying schema:
1. Update `data/metamodell.cql`
2. Update prompts (especially `agent_system.txt`, `chain_domain.txt`, `validator_integration.txt`)
3. Update `MetamodelValidator.ALLOWED_*` constants in `src/core/metamodel_validator.py`

### Neo4j Query Guidelines

- Use `elementId(node)` not deprecated `id(node)`
- Always use `LIMIT` for large result sets
- Fulfillment query pattern:
  ```cypher
  MATCH (r:Requirement)-[rel:FULFILLED_BY_OBJECT|NOT_FULFILLED_BY_OBJECT]->(bo:BusinessObject)
  RETURN r.rid, type(rel), rel.confidence, bo.name
  ```

### PostgreSQL Schema

Schema in `src/database/schema.sql`: `jobs`, `requirement_cache`, `llm_requests`, `agent_checkpoints`, `candidate_workspace`. Also includes `job_summary` view.

Optional vector search in `src/database/schema_vector.sql`: `workspace_embeddings` table for semantic search (requires pgvector).

### Agent State (LangGraph)

Both agents use PostgresSaver for durable execution with checkpointing.

**CreatorAgentState phases:** preprocessing → identification → research → formulation → output
**ValidatorAgentState phases:** understanding → relevance → fulfillment → planning → integration
**UniversalAgentState:** 4-node graph (initialize → process ↔ tools → check → END)

### Universal Agent Pattern

The Universal Agent (`src/agents/universal/`) is the primary agent implementation, providing a config-driven, workspace-centric autonomous pattern:

**Configuration:** Agents are defined in `config/agents/<name>.json` with:
- `llm`: Model, temperature, reasoning level
- `workspace.structure`: Directory layout for agent's filesystem
- `tools`: Tool categories (workspace, todo, domain, completion)
- `polling`: Which table to poll, status fields, intervals
- `limits`: Max iterations, context threshold, retry count
- `context_management`: Compaction settings, summarization prompts

**Key features:**
- Reads `instructions.md` from workspace to understand task context
- Uses `plans/`, `notes/`, `archive/` directories for strategic planning
- TodoManager for tactical execution with `archive_and_reset()` for context recovery
- Automatic context compaction when approaching token limits
- Dynamic tool loading from `src/agents/shared/tools/registry.py`
- Completion detection via `mark_complete` tool

**Creating a new agent type:**
1. Create `config/agents/<name>.json` based on `schema.json`
2. Create `config/agents/instructions/<name>_instructions.md`
3. Add domain-specific tools to `src/agents/shared/tools/` if needed
4. Register new tools in `src/agents/shared/tools/registry.py`

### Tool System

Tools are organized in `src/agents/shared/tools/` and loaded dynamically based on agent config:

| Category | Tools | Source |
|----------|-------|--------|
| `workspace` | read_file, write_file, list_files, search_files, etc. | `workspace_tools.py` |
| `todo` | add_todo, complete_todo, start_todo, archive_and_reset, etc. | `todo_tools.py` |
| `domain` | extract_document_text, web_search, cite_document, etc. | `document_tools.py` |
| `completion` | mark_complete | `completion_tools.py` |

Tool context (`ToolContext` dataclass) provides tools with access to workspace, todo manager, and database connections.

### Service Ports

| Service | Port |
|---------|------|
| Orchestrator | 8000 |
| Creator (Universal) | 8001 |
| Validator (Universal) | 8002 |
| Neo4j Bolt | 7687 |
| Neo4j Browser | 7474 |
| PostgreSQL | 5432 |
| Dashboard | 8501 |
| Adminer (dev) | 8080 |
