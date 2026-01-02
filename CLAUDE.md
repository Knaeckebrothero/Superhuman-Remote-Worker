# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Graph-RAG system for **requirement traceability and compliance checking** in a car rental business context. Uses LangGraph + LLMs to analyze requirements against Neo4j, with focus on GoBD compliance (German accounting principles).

## Commands

```bash
# Install & setup
pip install -r requirements.txt
cp .env.example .env  # Edit credentials

# Run interactive UI
streamlit run main.py

# Run batch workflow on CSV requirements
python -m src.workflow

# Test database connection
python -c "from src.core.neo4j_utils import create_neo4j_connection; conn = create_neo4j_connection(); conn.connect()"

# Run standalone metamodel validation
python validate_metamodel.py              # Run all checks
python validate_metamodel.py --check A1   # Run specific check
python validate_metamodel.py --json       # JSON output

# Validate Python syntax (all src files)
python -m py_compile src/**/*.py

# Database import (requires Neo4j stopped, replace 'neo4j' with your database name)
neo4j stop
neo4j-admin database load --from-path=data/ neo4j --overwrite-destination=true
neo4j start
```

## Configuration

**Environment (`.env`):**
- `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` - Database connection
- `OPENAI_API_KEY` - LLM credentials
- `LLM_BASE_URL` - Optional endpoint for self-hosted models
- `TAVILY_API_KEY` - Optional web search capability
- `WORKFLOW_MODE` - "chain" or "agent"

**LLM settings** in `config/llm_config.json`:
- `model`, `temperature`, `max_iterations`, `reasoning_level` (low/medium/high)

## Architecture

### Core Components

**`src/agents/graph_agent.py`** - Main LangGraph agent
- `RequirementGraphAgent` with `AgentState` TypedDict
- State graph: planner → agent → tools → increment → report
- Key methods: `process_requirement()`, `process_requirement_stream()` (for UI)
- **Agent tools:**
  - `execute_cypher_query` - Run Cypher queries
  - `get_database_schema` - Inspect schema
  - `count_nodes_by_label`, `find_sample_nodes` - Explore data
  - `validate_schema_compliance` - Run metamodel validation checks (A1-C3)
  - `web_search` - Optional Tavily web search (requires API key)

**`src/core/metamodel_validator.py`** - Deterministic FINIUS compliance validator
- `MetamodelValidator` class with categorized checks:
  - **Category A (Errors):** Node labels (A1), unique constraints (A2), required properties (A3)
  - **Category B (Errors):** Relationship types (B1), directions (B2), self-loops (B3)
  - **Category C (Warnings):** Orphan requirements (C1), message content (C2), GoBD traceability (C3)
- Returns `ComplianceReport` dataclass with `to_dict()` and `format_summary()`

**`src/workflow.py`** - Batch orchestrator (`python -m src.workflow`)
- Coordinates CSV processor, Neo4j connection, and agent

**`src/chain_example.py`** - Simple one-shot chain (demo/comparison only)

**`src/ui/`** - Streamlit pages (`streamlit run main.py`)
- `home.py`: Neo4j connection settings
- `agent.py`: Interactive LangGraph agent with streaming
- `chain.py`: One-shot chain demo
- `document_ingestion.py`: Document upload and requirement extraction pipeline

**`src/core/`** - Utilities
- `config.py`: `load_config()`, `load_prompt()`, `get_project_root()`
- `neo4j_utils.py`: `Neo4jConnection` class with `connect()`, `execute_query()`, `get_database_schema()`
- `csv_processor.py`: Auto-detects delimiter (semicolon or comma)
- `document_processor.py`: PDF/DOCX/TXT/HTML text extraction and chunking
- `document_models.py`: Data models for document ingestion pipeline

### Document Ingestion Pipeline (Multi-Agent)

A four-stage pipeline for processing documents and extracting requirements into the graph:

```
Document → [Stage 1: Processing] → [Stage 2: Extraction] → [Stage 3: Validation] → [Stage 4: Integration] → Neo4j
```

**`src/agents/document_ingestion_supervisor.py`** - Pipeline orchestrator
- `DocumentIngestionSupervisor` coordinates all stages
- Supports streaming for UI progress updates
- Human-in-the-loop support for review stage
- Factory: `create_document_ingestion_supervisor(neo4j_connection)`

**`src/agents/document_processor_agent.py`** - Stage 1: Document processing
- Extracts text from PDF, DOCX, TXT, HTML
- Applies intelligent chunking (legal, technical, general strategies)
- Detects document structure, language, jurisdiction

**`src/agents/requirement_extractor_agent.py`** - Stage 2: Requirement extraction
- Pattern-based detection (modal verbs, obligation phrases)
- GoBD relevance assessment
- Entity mention extraction (BusinessObjects, Messages)
- Confidence scoring

**`src/agents/requirement_validator_agent.py`** - Stage 3: Validation
- Duplicate detection against existing graph
- Entity resolution (fuzzy matching)
- Metamodel compatibility check
- Generates suggested RIDs and relationships

**Pipeline Configuration** (`config/llm_config.json` → `document_ingestion`):
```json
{
  "chunking_strategy": "legal",     // legal, technical, general
  "max_chunk_size": 1000,
  "extraction_mode": "balanced",    // strict, balanced, permissive
  "min_confidence": 0.6,
  "duplicate_similarity_threshold": 0.95,
  "auto_integrate": false           // If true, skip human review
}
```

**Usage (Programmatic):**
```python
from src.agents import create_document_ingestion_supervisor
from src.core import create_neo4j_connection

conn = create_neo4j_connection()
conn.connect()

supervisor = create_document_ingestion_supervisor(conn)
result = supervisor.process_document("path/to/document.pdf")
```

**Usage (Streamlit UI):**
Navigate to "Document Ingestion" page in the UI to upload and process documents interactively.

### Database Metamodel (FINIUS)

**Node Types:**
- `Requirement` - `rid`, `name`, `text`, `type`, `priority`, `status`, `source`, `valueStream`, `goBDRelevant`
- `BusinessObject` - `boid`, `name`, `description`, `domain`, `owner`
- `Message` - `mid`, `name`, `description`, `direction`, `format`, `protocol`, `version`

**Relationships (allowed source→target patterns):**
- `Requirement → Requirement`: `REFINES`, `DEPENDS_ON`, `TRACES_TO`
- `Requirement → BusinessObject`: `RELATES_TO_OBJECT`, `IMPACTS_OBJECT`
- `Requirement → Message`: `RELATES_TO_MESSAGE`, `IMPACTS_MESSAGE`
- `Message → BusinessObject`: `USES_OBJECT`, `PRODUCES_OBJECT`

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
