# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Graph-RAG (Retrieval-Augmented Generation) system** for **requirement traceability and compliance checking** in a car rental business context. The system uses LangChain and LLMs to automatically analyze business requirements against a Neo4j graph database, with focus on GoBD compliance (German accounting principles) and impact analysis.

### Core Workflow

The system supports **two distinct processing modes**:

1. **Chain Mode** (Default): Linear workflow using LangChain `Runnable` pattern
   - Refine requirement → Generate Cypher query → Execute query → Analyze results
   - Fast, predictable, single-pass processing
   - Best for straightforward requirements and batch processing

2. **Agent Mode**: Iterative reasoning agent using LangGraph
   - Plan → Query → Reason → Repeat (up to max iterations)
   - Can explore multiple angles and perform deep investigation
   - Best for complex, multi-faceted requirements requiring thorough analysis

The pipeline reads requirements from semicolon-delimited CSV files, uses LLM to generate and execute Cypher queries, and produces compliance reports in both JSON and TXT formats.

## Development Commands

### Running the Workflow

```bash
# Chain mode (default) - fast linear workflow
python run_workflow.py chain
# or
python -m src.main chain

# Agent mode - iterative reasoning agent
python run_workflow.py agent
# or
python -m src.main agent

# Mode can also be set via .env file (WORKFLOW_MODE=chain or agent)
python run_workflow.py
```

### Comparing Approaches

```bash
# Compare chain vs agent on a single requirement
python compare_approaches_unified.py "Which requirements are GoBD-relevant?"

# With options
python compare_approaches_unified.py "Your requirement" --max-iterations 5 --debug

# Other comparison scripts available:
# - compare_approaches.py - basic comparison
# - compare_approaches_verbose.py - detailed output
```

### Database Setup

```bash
# Import database dump (requires Neo4j stopped)
neo4j stop
neo4j-admin database load --from-path=database/ <database-name> --overwrite-destination=true
neo4j start

# Test database connection
python -c "from src.neo4j_utils import create_neo4j_connection; conn = create_neo4j_connection(); conn.connect()"
```

### Environment Configuration

Create `.env` from `.env.example` with:
- `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` - Database connection
- `CSV_FILE_PATH` - Path to requirements CSV (default: `data/requirements.csv`)
- `OPENAI_API_KEY`, `LLM_MODEL`, `LLM_TEMPERATURE` - LLM configuration
- `WORKFLOW_MODE` - "chain" or "agent" (default: "chain")
- `AGENT_MAX_ITERATIONS` - Max iterations for agent mode (default: 5)

### Dependencies

```bash
# Install dependencies
pip install -r requirements.txt

# Core packages: python-dotenv, pandas, neo4j, langchain, langchain-openai,
# langgraph (for agent mode), openai
```

## Architecture & Code Structure

### Core Modules (src/)

1. **`main.py`** - Workflow Orchestrator
   - `RequirementWorkflow` class manages the complete pipeline
   - Supports both "chain" and "agent" modes via constructor parameter
   - Coordinates CSV processor, Neo4j connection, and appropriate agent type
   - Handles setup, processing loop, output generation, and cleanup
   - Entry point: `main()` function loads env, determines mode, and runs workflow
   - Mode selection priority: command line args > env variable > default ("chain")

2. **`requirement_agent.py`** - LangChain Simple Chain Agent (Chain Mode)
   - `RequirementAgent` class wraps OpenAI LLM with LangChain **Runnable pattern**
   - **IMPORTANT**: Recently refactored from `LLMChain` to `Runnable` (prompt | llm) pattern
   - Creates three specialized chains using `PromptTemplate | ChatOpenAI`:
     - **Cypher Query Chain**: Generates Neo4j queries from natural language (includes full metamodel context)
     - **Analysis Chain**: Interprets query results for compliance/impact assessment
     - **Refinement method**: Clarifies ambiguous requirements (optional step)
   - Linear processing: single pass through refine → query → analyze
   - Key method: `process_requirement()` - end-to-end processing of single requirement
   - Returns: `Dict` with `refined_requirement`, `cypher_query`, `results`, `analysis`, `result_count`

3. **`requirement_agent_graph.py`** - LangGraph Iterative Agent (Agent Mode)
   - `RequirementGraphAgent` class implements LangGraph state machine
   - Uses **tool-based architecture** with LangChain tools for Neo4j queries
   - Defines `AgentState` (TypedDict) with messages, plan, queries_executed, iteration, etc.
   - State graph: plan → query → reason → decide (continue or finish)
   - Agent can execute multiple queries and investigate different angles
   - Includes tools: `execute_cypher_query`, `get_database_schema`
   - Key method: `process_requirement()` - iterative processing with max_iterations limit
   - Returns: `Dict` with `plan`, `analysis`, `iterations`, `queries_executed`, etc.

4. **`neo4j_utils.py`** - Database Connection Layer
   - `Neo4jConnection` class handles connection lifecycle
   - Methods: `connect()`, `execute_query()`, `get_database_schema()`, `close()`
   - Factory function: `create_neo4j_connection()` reads from environment
   - Includes error handling for auth failures and service unavailable scenarios

5. **`csv_processor.py`** - Requirement Input Handler
   - `RequirementProcessor` class reads CSV files
   - Auto-detects semicolon vs comma delimiters
   - Current format: `Name;Description` (semicolon-delimited)
   - Methods: `load_requirements()`, `format_requirement_with_metadata()`, `get_requirement_metadata()`

### Database Metamodel (Critical for Query Generation)

The Neo4j database uses a **strict metamodel** for requirement traceability:

**Node Types:**
- `Requirement` - Properties: `rid` (unique), `name`, `text`, `type`, `priority`, `status`, `source`, `valueStream`, `goBDRelevant`
- `BusinessObject` - Properties: `boid` (unique), `name`, `description`, `domain`, `owner`
- `Message` - Properties: `mid` (unique), `name`, `description`, `direction`, `format`, `protocol`, `version`

**Relationship Types:**
- Requirement → Requirement: `REFINES`, `DEPENDS_ON`, `TRACES_TO`
- Requirement → BusinessObject: `RELATES_TO_OBJECT`, `IMPACTS_OBJECT`
- Requirement → Message: `RELATES_TO_MESSAGE`, `IMPACTS_MESSAGE`
- Message → BusinessObject: `USES_OBJECT`, `PRODUCES_OBJECT`

**Schema File:** `database/metamodell.cql` contains constraints, indexes, and template queries.

### LLM Chain Architecture (IMPORTANT: Recently Updated)

**Current Implementation** (post-refactoring):
- Uses LangChain **Runnable pattern**: `PromptTemplate | ChatOpenAI`
- Chain composition via pipe operator (`|`) instead of explicit `LLMChain` objects
- Invocation: `chain.invoke(inputs)` returns `AIMessage` with `.content` attribute
- Three separate chains for different tasks (query generation, analysis, refinement)

**Previous Implementation** (deprecated):
- Used `LLMChain` class from `langchain.chains`
- Called via `chain.run()` or `chain(inputs)`

**When modifying chains:**
1. Use `PromptTemplate` with `input_variables` and `template`
2. Compose with `ChatOpenAI` using pipe operator: `prompt | llm`
3. Invoke with `.invoke(dict)` and access result via `.content`
4. See `requirement_agent.py` lines 43-100 (cypher chain) and 102-138 (analysis chain) for examples

### Output Structure

Results are saved to `output/` directory:
- **JSON format**: Structured data with `original_requirement`, `refined_requirement` (chain) or `plan` (agent), `cypher_query` (chain) or `queries_executed` (agent), `result_count`, `results`, `analysis`, `metadata`, `mode`, `iterations` (agent only)
- **TXT format**: Human-readable report with formatted sections, adapts to chain vs agent mode

## Key Implementation Details

### Dual Mode Architecture

The workflow system supports two distinct processing modes, selected via:
1. Command line argument: `python run_workflow.py chain` or `agent`
2. Environment variable: `WORKFLOW_MODE=chain` or `agent` in `.env`
3. Default: "chain" if not specified

**Mode affects:**
- Which agent class is instantiated (`RequirementAgent` vs `RequirementGraphAgent`)
- Processing strategy (single-pass vs iterative)
- Output format (refined requirement vs plan, single query vs multiple queries)
- Whether refinement step is used (chain mode only)

### CSV Format Support

The system handles **semicolon-delimited** CSV files as primary format (car rental dataset uses this). The processor in `csv_processor.py:44-55` tries semicolon first, then falls back to comma-delimited. For the current dataset:
- First column = `Name` (requirement title)
- Second column = `Description` (analyzed by LLM)
- 82 requirements total in `data/requirements.csv`

### LangGraph Agent State Machine

The agent mode (`requirement_agent_graph.py`) uses LangGraph's `StateGraph` with:
- **State**: `AgentState` TypedDict with messages, plan, queries, iteration count, completion flag
- **Nodes**: `plan_requirement`, `query_database`, `reason_about_results`, `finish`
- **Edges**: Conditional routing based on iteration count and agent decision
- **Tools**: `execute_cypher_query`, `get_database_schema` (decorated with `@tool`)
- **Tool node**: `ToolNode` handles tool execution automatically

### Error Handling

- Database connection failures are caught early in `setup()` (src/main.py:54-71)
- Query execution errors are logged but don't crash the workflow
- Failed requirements are saved to output with `error` field for debugging
- Agent mode catches iteration limit and gracefully terminates

### Alternative LLM Providers

To use Anthropic Claude or Cohere instead of OpenAI:
1. Install additional packages: `anthropic`, `langchain-anthropic`, or `cohere`
2. Update `.env` with provider API key and model name
3. Modify agent initialization in both `requirement_agent.py:34-38` and `requirement_agent_graph.py` to use appropriate LangChain chat model class (e.g., `ChatAnthropic`)

## Common Development Workflows

### Choosing Between Chain and Agent Modes

**Use Chain Mode when:**
- Processing multiple requirements in batch from CSV
- Need fast, predictable results
- Requirements are straightforward queries
- Single Cypher query is sufficient

**Use Agent Mode when:**
- Analyzing complex, multi-faceted requirements
- Need thorough investigation across multiple queries
- Performing deep compliance analysis
- Exploring impacts across multiple relationship types
- Investigating requirements where initial query might not be sufficient

### Testing a Single Requirement

Use comparison scripts to test both approaches side-by-side:
```bash
python compare_approaches_unified.py "Find GoBD-relevant requirements and their impacts"
```

### Adding New Metamodel Nodes/Relationships

1. Update Neo4j schema in `database/metamodell.cql` (add constraints/indexes)
2. Execute changes in Neo4j Browser or via cypher-shell
3. Update prompt template in **both** agent implementations:
   - `requirement_agent.py:50-93` (chain mode metamodel description)
   - `requirement_agent_graph.py` tool descriptions and system prompts
4. Update README.md metamodel documentation section

### Processing a New Requirements CSV

1. Place CSV file in `data/` directory
2. Update `CSV_FILE_PATH` in `.env` to point to new file
3. Ensure CSV has either:
   - Semicolon format: `Name;Description` (preferred)
   - Comma format with `requirement` or text column
4. Run `python run_workflow.py chain` (or `agent` for thorough analysis)

### Debugging Query Generation Issues

1. Check `output/results_*.txt` for generated Cypher queries
2. Test queries manually in Neo4j Browser (http://localhost:7474)
3. For chain mode: Examine `requirement_agent.py` prompt templates (lines 50-93) for metamodel accuracy
4. For agent mode: Check `requirement_agent_graph.py` tool descriptions and system messages
5. Verify database schema: `CALL db.schema.visualization()` in Neo4j Browser

### Testing Database Connectivity

The `neo4j_utils.py` module provides a standalone test:
```python
from src.neo4j_utils import create_neo4j_connection
conn = create_neo4j_connection()
if conn.connect():
    schema = conn.get_database_schema()
    print(f"Labels: {schema['node_labels']}")
    conn.close()
```

### Modifying LLM Chains (Post-Refactoring Pattern)

When adding or modifying chains in `requirement_agent.py`:
```python
# Create chain using Runnable pattern
template = """Your prompt template here with {variables}"""
prompt = PromptTemplate(input_variables=["var1", "var2"], template=template)
chain = prompt | self.llm  # Pipe operator for composition

# Invoke chain
result = chain.invoke({"var1": "value1", "var2": "value2"})
output = result.content.strip()  # Access content attribute
```

## Important Notes

- The LLM prompts in both `requirement_agent.py` and `requirement_agent_graph.py` are **tightly coupled to the metamodel** - changes to one require changes to the other
- The system assumes Neo4j 5.x with APOC procedures available
- Temperature is set to 0.0 for deterministic query generation
- The semicolon CSV format is required for the current car rental dataset (82 requirements)
- **Recent refactoring**: `requirement_agent.py` now uses `Runnable` pattern (prompt | llm) instead of `LLMChain`
- There are TWO separate agent implementations: `requirement_agent.py` (chain mode) and `requirement_agent_graph.py` (agent mode)
- Comparison scripts in root directory allow side-by-side testing of both approaches