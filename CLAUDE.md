# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Graph-RAG (Retrieval-Augmented Generation) system** for **requirement traceability and compliance checking** in a car rental business context. The system uses LangGraph and LLMs to analyze business requirements against a Neo4j graph database, with focus on GoBD compliance (German accounting principles) and impact analysis.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env  # Edit with your credentials

# Run the Streamlit application
streamlit run main.py

# Or run the batch workflow
python -m src.workflow
```

## Development Commands

### Interactive Application

```bash
streamlit run main.py
```

The Streamlit application (`main.py`) provides a multi-page interface:
- **Home**: Database connection settings (defaults from .env)
- **Agent**: Iterative LangGraph agent with streaming progress display
- **Chain**: Simple one-shot chain with step-by-step progress

### Batch Workflow

```bash
# Run the LangGraph agent workflow on CSV requirements
python -m src.workflow
```

### Testing

```bash
# Test database connection
python -c "from src.core.neo4j_utils import create_neo4j_connection; conn = create_neo4j_connection(); conn.connect()"
```

### Database Setup

```bash
# Import database dump (requires Neo4j stopped)
neo4j stop
neo4j-admin database load --from-path=data/ <database-name> --overwrite-destination=true
neo4j start
```

Schema file: `data/metamodell.cql`
Database dump: `data/neo4j.dump`

## Environment Configuration

Create `.env` from `.env.example`:
- `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` - Database connection
- `CSV_FILE_PATH` - Path to requirements CSV (default: `data/requirements.csv`)
- `OPENAI_API_KEY` - LLM API key (only credentials in .env)
- `LLM_BASE_URL` - Optional custom endpoint for self-hosted models (gpt-oss, vLLM, llama.cpp)
- `WORKFLOW_MODE` - "chain" or "agent" for workflow selection

LLM settings (model, temperature, max_iterations, reasoning_level) are in `config/llm_config.json`.

## Architecture

### Project Structure

```
project/
├── main.py                    # Streamlit entry point
├── config/                    # Configuration files
│   ├── llm_config.json        # LLM settings (model, temperature, reasoning_level)
│   └── prompts/
│       ├── agent_system.txt   # Agent system prompt
│       └── chain_domain.txt   # Chain domain context
├── src/
│   ├── chain_example.py       # Simple chain demo (for comparison)
│   ├── workflow.py            # Batch workflow orchestrator
│   ├── __init__.py            # Package exports
│   ├── agents/
│   │   └── graph_agent.py     # LangGraph iterative agent
│   ├── core/
│   │   ├── config.py          # Configuration loader utilities
│   │   ├── neo4j_utils.py     # Neo4j connection layer
│   │   └── csv_processor.py   # CSV requirement parser
│   └── ui/
│       ├── __init__.py        # UI helper functions
│       ├── home.py            # Home page (connection settings)
│       ├── agent.py           # Agent analysis page
│       └── chain.py           # Chain analysis page
├── data/
│   ├── metamodell.cql         # Neo4j schema
│   ├── neo4j.dump             # Database dump
│   ├── output_schema.json     # Structured output schema
│   └── requirements.csv       # Input requirements
└── output/                    # Analysis results
```

### Two Approaches

| Approach | File | Purpose |
|----------|------|---------|
| **Agent** (Main) | `src/agents/graph_agent.py` | LangGraph iterative agent with tools |
| **Chain** (Demo) | `src/chain_example.py` | Simple one-shot chain for comparison |

The **Agent** is the main implementation - it iteratively queries the database, reasons about results, and can refine its approach. The **Chain** is a simple demonstration to show why the agent approach is superior.

### Core Modules

1. **`src/workflow.py`** - Workflow orchestrator
   - `RequirementWorkflow` class manages the pipeline
   - Coordinates CSV processor, Neo4j connection, and agent
   - Entry point: `python -m src.workflow`

2. **`src/agents/graph_agent.py`** - LangGraph Agent
   - `RequirementGraphAgent` with `AgentState` TypedDict
   - Tools: `execute_cypher_query`, `get_database_schema`, `count_nodes_by_label`, `find_sample_nodes`
   - State graph: planner → agent → tools → increment → report
   - Key method: `process_requirement()` → returns `{plan, queries_executed, iterations, analysis}`
   - Streaming: `process_requirement_stream()` for dashboard integration

3. **`src/chain_example.py`** - Simple Chain Demo
   - `SimpleChain` class with 4-step linear flow
   - Uses Pydantic models for structured output (based on `data/output_schema.json`)
   - No iteration, no tool calling - demonstrates limitations

4. **`src/core/config.py`** - Configuration utilities
   - `load_config()` loads JSON from `config/` directory
   - `load_prompt()` loads prompt text from `config/prompts/`
   - `get_project_root()` returns project root path

5. **`src/core/neo4j_utils.py`** - Database connection layer
   - `Neo4jConnection` class: `connect()`, `execute_query()`, `get_database_schema()`, `close()`
   - Factory: `create_neo4j_connection()` reads from environment

6. **`src/core/csv_processor.py`** - Requirement input handler
   - `RequirementProcessor` class reads semicolon-delimited CSV (auto-detects delimiter)
   - Format: `Name;Description` (82 requirements in current dataset)

### Database Metamodel

**Node Types:**
- `Requirement` - `rid`, `name`, `text`, `type`, `priority`, `status`, `source`, `valueStream`, `goBDRelevant`
- `BusinessObject` - `boid`, `name`, `description`, `domain`, `owner`
- `Message` - `mid`, `name`, `description`, `direction`, `format`, `protocol`, `version`

**Relationships:**
- `Requirement → Requirement`: `REFINES`, `DEPENDS_ON`, `TRACES_TO`
- `Requirement → BusinessObject`: `RELATES_TO_OBJECT`, `IMPACTS_OBJECT`
- `Requirement → Message`: `RELATES_TO_MESSAGE`, `IMPACTS_MESSAGE`
- `Message → BusinessObject`: `USES_OBJECT`, `PRODUCES_OBJECT`

## Key Implementation Details

### Agent vs Chain Comparison

| Aspect | Simple Chain | LangGraph Agent |
|--------|--------------|-----------------|
| Query Refinement | None - one shot | Iterative refinement |
| Error Recovery | None - fails silently | Retries with different approach |
| Schema Exploration | Static dump | Dynamic tool calls |
| Reasoning | Linear 3-step | Multi-step with loops |
| Missing Data | Reports as-is | Explores alternatives |

### Prompt Templates

LLM prompts are stored in `config/prompts/` and are **tightly coupled to the metamodel**. When modifying the database schema:
1. Update `data/metamodell.cql`
2. Update `config/prompts/agent_system.txt` (agent system prompt)
3. Update `config/prompts/chain_domain.txt` (chain domain context)
4. Update prompts in `src/agents/graph_agent.py` if needed

### Neo4j Query Guidelines

- Use `elementId(node)` instead of deprecated `id(node)`
- Prefer property-based queries over ID-based queries
- Always use `LIMIT` for potentially large result sets

### Output Structure

Results saved to `output/` directory:
- **JSON**: `{original_requirement, plan, queries_executed, iterations, analysis}`
- **TXT**: Human-readable report with formatted sections

### Structured Output Schema

The chain example uses Pydantic models based on `data/output_schema.json`:
- `Identification` - Requirement text and metadata (entity_name, requirement_type, status, source_origin)
- `KnowledgeRetrieval` - found_facts (what was found) and missing_elements (what was not found)
- `Analysis` - compliance_matrix (criteria, result, observation) and risk_assessment (level, justification)
- `Evaluation` - verdict (Satisfied/Not Satisfied/Partially Satisfied) and summary_reasoning
- `Recommendations` - action_items list
- `ConversationalSummary` - user-friendly message

### Error Handling

- Database connection failures caught early in `setup()`
- Query execution errors logged but don't crash workflow
- Failed requirements saved with `error` field
- Agent catches iteration limit and gracefully terminates

## Common Tasks

### Adding New Metamodel Nodes/Relationships

1. Update Neo4j schema in `data/metamodell.cql`
2. Execute changes in Neo4j Browser
3. Update prompts in `src/agents/graph_agent.py` and `src/chain_example.py`

### Processing a New Requirements CSV

1. Place CSV in `data/` directory
2. Update `CSV_FILE_PATH` in `.env`
3. Ensure format: `Name;Description` (semicolon-delimited) or comma-delimited with `requirement` column
4. Run `python -m src.workflow`

### Debugging Query Generation

1. Check `output/results_*.txt` for generated Cypher queries
2. Test queries manually in Neo4j Browser (http://localhost:7474)
3. Verify schema: `CALL db.schema.visualization()` in Neo4j

### Alternative LLM Providers

To use Anthropic Claude or other providers:
1. Install: `pip install anthropic langchain-anthropic`
2. Add provider API key to `.env` (e.g., `ANTHROPIC_API_KEY`)
3. Update model name in `config/llm_config.json`
4. Modify agent initialization in `src/agents/graph_agent.py` to use `ChatAnthropic`
