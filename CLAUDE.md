# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Graph-RAG (Retrieval-Augmented Generation) system** for **requirement traceability and compliance checking** in a car rental business context. The system uses LangChain and LLMs to automatically analyze business requirements against a Neo4j graph database, with focus on GoBD compliance (German accounting principles) and impact analysis.

### Core Workflow

The system follows a pipeline approach:
1. **CSV Processor** reads requirements from semicolon-delimited CSV files (`data/requirements.csv`)
2. **LLM Agent** refines requirements and generates Cypher queries based on the database metamodel
3. **Neo4j Database** executes queries against the graph structure (Requirements, BusinessObjects, Messages)
4. **Analysis Engine** uses LLM to provide compliance reports, impact assessments, and recommendations
5. **Output Generator** saves results in both JSON (structured) and TXT (human-readable) formats

## Development Commands

### Running the Workflow

```bash
# Main execution script
python run_workflow.py

# Or directly via module
python -m src.main
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

### Dependencies

```bash
# Install dependencies
pip install -r requirements.txt

# Core packages: python-dotenv, pandas, neo4j, langchain, langchain-openai, openai
```

## Architecture & Code Structure

### Core Modules (src/)

1. **`main.py`** - Workflow Orchestrator
   - `RequirementWorkflow` class manages the complete pipeline
   - Coordinates CSV processor, Neo4j connection, and LangChain agent
   - Handles setup, processing loop, output generation, and cleanup
   - Entry point: `main()` function loads env and runs workflow

2. **`requirement_agent.py`** - LangChain Agent (LLM Brain)
   - `RequirementAgent` class wraps OpenAI LLM with LangChain
   - Creates three specialized chains:
     - **Refine Chain**: Clarifies ambiguous requirements
     - **Cypher Query Chain**: Generates Neo4j queries from natural language (includes full metamodel context)
     - **Analysis Chain**: Interprets query results for compliance/impact assessment
   - Uses ReAct pattern for iterative processing
   - Key method: `process_requirement()` - end-to-end processing of single requirement

3. **`neo4j_utils.py`** - Database Connection Layer
   - `Neo4jConnection` class handles connection lifecycle
   - Methods: `connect()`, `execute_query()`, `get_database_schema()`, `close()`
   - Factory function: `create_neo4j_connection()` reads from environment
   - Includes error handling for auth failures and service unavailable scenarios

4. **`csv_processor.py`** - Requirement Input Handler
   - `RequirementProcessor` class reads CSV files
   - Auto-detects semicolon vs comma delimiters
   - Current format: `Name;Description` (semicolon-delimited)
   - Methods to extract requirement text, metadata, and format for LLM

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

### LLM Prompt Engineering

The agent in `requirement_agent.py` injects the **full metamodel description** into the Cypher generation prompt (lines 55-98). This is essential for accurate query generation. When modifying the metamodel:
1. Update `database/metamodell.cql`
2. Update the metamodel description in `RequirementAgent.create_cypher_query_chain()` prompt template
3. Update README.md documentation

### Output Structure

Results are saved to `output/` directory:
- **JSON format**: Structured data with `original_requirement`, `refined_requirement`, `cypher_query`, `result_count`, `results`, `analysis`, `metadata`
- **TXT format**: Human-readable report with formatted sections

## Key Implementation Details

### CSV Format Support

The system handles **semicolon-delimited** CSV files as primary format (car rental dataset uses this). The processor in `csv_processor.py:44-55` tries semicolon first, then falls back to comma-delimited. For the current dataset:
- First column = `Name` (requirement title)
- Second column = `Description` (analyzed by LLM)
- 82 requirements total in `data/requirements.csv`

### LLM Chain Architecture

Uses three separate LangChain `LLMChain` instances instead of a unified agent executor:
1. Requirement refinement for clarity
2. Cypher query generation with metamodel context
3. Result analysis with domain-specific prompts (GoBD compliance, impact assessment)

This separation allows independent prompt tuning for each task.

### Error Handling

- Database connection failures are caught early in `setup()` (src/main.py:54-61)
- Query execution errors are logged but don't crash the workflow (src/requirement_agent.py:252-258)
- Failed requirements are saved to output with `error` field for debugging

### Alternative LLM Providers

To use Anthropic Claude or Cohere instead of OpenAI:
1. Install additional packages: `anthropic`, `langchain-anthropic`, or `cohere`
2. Update `.env` with provider API key and model name
3. Modify `RequirementAgent.__init__()` in `requirement_agent.py:39-43` to use appropriate LangChain chat model class

## Common Development Workflows

### Adding New Metamodel Nodes/Relationships

1. Update Neo4j schema in `database/metamodell.cql` (add constraints/indexes)
2. Execute changes in Neo4j Browser or via cypher-shell
3. Update prompt template in `requirement_agent.py:55-98` to include new metamodel elements
4. Update README.md metamodel documentation section

### Processing a New Requirements CSV

1. Place CSV file in `data/` directory
2. Update `CSV_FILE_PATH` in `.env` to point to new file
3. Ensure CSV has either:
   - Semicolon format: `Name;Description` (preferred)
   - Comma format with `requirement` or text column
4. Run `python run_workflow.py`

### Debugging Query Generation Issues

1. Check `output/results_*.txt` for generated Cypher queries
2. Test queries manually in Neo4j Browser (http://localhost:7474)
3. Examine `requirement_agent.py` prompt templates for metamodel accuracy
4. Verify database schema: `CALL db.schema.visualization()` in Neo4j Browser

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

## Important Notes

- The LLM prompts in `requirement_agent.py` are **tightly coupled to the metamodel** - changes to one require changes to the other
- The system assumes Neo4j 5.x with APOC procedures available
- Temperature is set to 0.0 for deterministic query generation
- The GitHub workflow (`.github/workflows/main.yml`) is a placeholder and needs implementation
- The semicolon CSV format is required for the current car rental dataset (82 requirements)