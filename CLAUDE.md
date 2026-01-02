# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Graph-RAG system for **requirement traceability and compliance checking** in a car rental business context. Uses LangGraph + LLMs to analyze requirements against Neo4j, with focus on GoBD compliance (German accounting principles).

**Architecture:** Two-agent autonomous system (Creator + Validator) for long-running requirement extraction and graph integration. See `masterplan.md` for full design and `masterplan_roadmap.md` for implementation phases.

## Commands

```bash
# Install & setup
pip install -r requirements.txt
pip install -e ./citation_tool[full]  # Citation Engine
cp .env.example .env  # Edit credentials

# Initialize PostgreSQL database
python scripts/init_db.py

# Run interactive UI (legacy)
streamlit run main.py

# Run batch workflow on CSV requirements
python -m src.workflow

# Test database connections
python -c "from src.core.neo4j_utils import create_neo4j_connection; conn = create_neo4j_connection(); conn.connect()"

# Run standalone metamodel validation
python validate_metamodel.py              # Run all checks (A1-C5)
python validate_metamodel.py --check A1   # Run specific check
python validate_metamodel.py --json       # JSON output

# Validate Python syntax (all src files)
python -m py_compile src/**/*.py

# Database import (requires Neo4j stopped)
neo4j stop
neo4j-admin database load --from-path=data/ neo4j --overwrite-destination=true
neo4j start
```

## Configuration

**Environment (`.env`):**
- `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` - Neo4j connection
- `DATABASE_URL` - PostgreSQL connection (for two-agent system)
- `OPENAI_API_KEY` - LLM credentials
- `LLM_BASE_URL` - Optional endpoint for self-hosted models
- `TAVILY_API_KEY` - Web search capability (required for Creator Agent)
- `CITATION_*` - Citation Engine configuration

**LLM settings** in `config/llm_config.json`:
- `model`, `temperature`, `max_iterations`, `reasoning_level` (low/medium/high)
- `creator_agent` - Creator Agent specific settings
- `validator_agent` - Validator Agent specific settings
- `context_management` - Context window management settings
- `orchestrator` - Job management settings

## Architecture

### Two-Agent System (Masterplan v1.1)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          ORCHESTRATOR                                    │
│                    (Job Management & Coordination)                       │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
         ┌──────────────────────┴──────────────────────┐
         │                                              │
         ▼                                              ▼
┌─────────────────────┐                      ┌─────────────────────┐
│   CREATOR AGENT     │                      │  VALIDATOR AGENT    │
│   (Container)       │                      │   (Container)       │
│                     │                      │                     │
│ • Document process  │                      │ • Graph exploration │
│ • Requirement       │   PostgreSQL Cache   │ • Relevance check   │
│   extraction        │ ◄──────────────────► │ • Fulfillment check │
│ • Research/citation │   (requirement_cache)│ • Graph integration │
└─────────────────────┘                      └─────────────────────┘
                                                        │
                                                        ▼
                                              ┌─────────────────────┐
                                              │       Neo4j         │
                                              │  (Knowledge Graph)  │
                                              └─────────────────────┘
```

### Directory Structure

```
src/
├── agents/
│   ├── creator/           # Creator Agent (Phase 2)
│   │   ├── creator_agent.py
│   │   ├── document_processor.py
│   │   ├── candidate_extractor.py
│   │   ├── researcher.py
│   │   └── tools.py
│   ├── validator/         # Validator Agent (Phase 3)
│   │   ├── validator_agent.py
│   │   ├── relevance_analyzer.py
│   │   ├── fulfillment_checker.py
│   │   ├── graph_integrator.py
│   │   └── tools.py
│   ├── shared/            # Shared utilities
│   │   ├── context_manager.py  # Context window management
│   │   ├── checkpoint.py       # State persistence
│   │   ├── workspace.py        # Working data storage
│   │   └── todo_manager.py     # Task tracking
│   └── [existing agents]
├── core/
│   ├── config.py
│   ├── neo4j_utils.py
│   ├── postgres_utils.py     # PostgreSQL connection
│   ├── citation_utils.py     # Citation Engine integration
│   └── metamodel_validator.py
├── orchestrator/          # Orchestrator (Phase 4)
│   ├── job_manager.py
│   ├── dispatcher.py
│   ├── monitor.py
│   └── reporter.py
└── ui/
```

### Core Components

**`src/agents/graph_agent.py`** - Main LangGraph agent (legacy)
- `RequirementGraphAgent` with `AgentState` TypedDict
- State graph: planner → agent → tools → increment → report
- Key methods: `process_requirement()`, `process_requirement_stream()` (for UI)
- **Agent tools:**
  - `execute_cypher_query` - Run Cypher queries
  - `get_database_schema` - Inspect schema
  - `count_nodes_by_label`, `find_sample_nodes` - Explore data
  - `validate_schema_compliance` - Run metamodel validation checks (A1-C5)
  - `web_search` - Optional Tavily web search (requires API key)

**`src/core/metamodel_validator.py`** - Deterministic FINIUS compliance validator
- `MetamodelValidator` class with categorized checks:
  - **Category A (Errors):** Node labels (A1), unique constraints (A2), required properties (A3)
  - **Category B (Errors):** Relationship types (B1), directions (B2), self-loops (B3)
  - **Category C (Warnings):** Orphan requirements (C1), message content (C2), GoBD traceability (C3), unfulfilled requirements (C4), compliance status consistency (C5)
- Returns `ComplianceReport` dataclass with `to_dict()` and `format_summary()`

**`src/core/postgres_utils.py`** - PostgreSQL utilities for shared state
- `PostgresConnection` - Async connection pool management
- Job management: `create_job()`, `update_job_status()`
- Requirement cache: `create_requirement()`, `get_pending_requirement()` (with SKIP LOCKED)
- LLM logging: `log_llm_request()`
- Checkpointing: `save_checkpoint()`, `get_latest_checkpoint()`

**`src/agents/shared/`** - Shared agent utilities
- `ContextManager` - LLM context window management with pre_model_hook
- `CheckpointManager` - State persistence and recovery
- `Workspace` - Working data storage (chunks, candidates, notes)
- `TodoManager` - Task tracking for autonomous agents

### Database Metamodel (FINIUS v2.0)

**Node Types:**
- `Requirement` - `rid`, `name`, `text`, `type`, `priority`, `status`, `source`, `valueStream`, `goBDRelevant`, `gdprRelevant`, `complianceStatus`
- `BusinessObject` - `boid`, `name`, `description`, `domain`, `owner`
- `Message` - `mid`, `name`, `description`, `direction`, `format`, `protocol`, `version`

**Relationships (allowed source→target patterns):**
- `Requirement → Requirement`: `REFINES`, `DEPENDS_ON`, `TRACES_TO`, `SUPERSEDES`
- `Requirement → BusinessObject`: `RELATES_TO_OBJECT`, `IMPACTS_OBJECT`, `FULFILLED_BY_OBJECT`, `NOT_FULFILLED_BY_OBJECT`
- `Requirement → Message`: `RELATES_TO_MESSAGE`, `IMPACTS_MESSAGE`, `FULFILLED_BY_MESSAGE`, `NOT_FULFILLED_BY_MESSAGE`
- `Message → BusinessObject`: `USES_OBJECT`, `PRODUCES_OBJECT`

**Compliance Status:** Requirements have `complianceStatus` property: `'open'`, `'partial'`, `'fulfilled'`

### PostgreSQL Schema

```sql
-- Core tables (migrations/001_initial_schema.sql)
jobs                 -- Job tracking
requirement_cache    -- Shared queue between agents
llm_requests         -- LLM request logging
agent_checkpoints    -- Recovery state persistence
candidate_workspace  -- Creator Agent working data
```

## Key Implementation Details

### Prompt-Metamodel Coupling

Prompts in `config/prompts/` are **tightly coupled to the metamodel**. When modifying schema:
1. Update `data/metamodell.cql`
2. Update `config/prompts/agent_system.txt` and `chain_domain.txt`
3. Update `MetamodelValidator.ALLOWED_*` constants in `src/core/metamodel_validator.py`

### Neo4j Query Guidelines

- Use `elementId(node)` instead of deprecated `id(node)`
- Prefer property-based queries over ID-based queries
- Always use `LIMIT` for potentially large result sets

### Adding New Metamodel Nodes/Relationships

1. Update schema in `data/metamodell.cql`
2. Update `MetamodelValidator` constants (`ALLOWED_NODE_LABELS`, `REQUIRED_PROPERTIES`, `ALLOWED_RELATIONSHIPS`)
3. Update prompts in `config/prompts/`
4. Execute schema changes in Neo4j Browser

### Alternative LLM Providers

To use Anthropic Claude:
1. `pip install langchain-anthropic`
2. Add `ANTHROPIC_API_KEY` to `.env`
3. Modify `src/agents/graph_agent.py` to use `ChatAnthropic` instead of `ChatOpenAI`

## Implementation Status

Refer to `masterplan_roadmap.md` for detailed implementation phases:

- **Phase 1: Foundation & Infrastructure** - Directory structure, PostgreSQL schema, metamodel updates, shared utilities, citation integration, configuration
- **Phase 2: Creator Agent** - Document processing, candidate extraction, research, citation creation
- **Phase 3: Validator Agent** - Relevance analysis, fulfillment checking, graph integration
- **Phase 4: Orchestrator** - Job management, monitoring, CLI interface
- **Phase 5: Containerization** - FastAPI apps, Docker, health endpoints
- **Phase 6: Testing & Hardening** - Unit tests, integration tests, error recovery
