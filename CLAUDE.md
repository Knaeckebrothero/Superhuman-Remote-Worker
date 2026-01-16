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
pytest tests/                    # All tests
pytest tests/test_graph.py -v    # Single test file
pytest tests/ --cov=src          # With coverage
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
| `validator` | Validate and integrate into Neo4j | `requirements` table | graph, cypher, validation |

Data flow: Creator → `requirements` table → Validator → Neo4j

### Phase Alternation Model

The agent uses a single ReAct loop alternating between phases:

1. **Strategic Phase**: Planning, memory updates, todo creation from YAML templates
2. **Tactical Phase**: Domain-specific execution of todos
3. **Archive**: Save artifacts, clear messages, update workspace.md
4. Repeat until goal achieved

Key files per phase:
- `workspace.md` - Long-term memory (persists across phases)
- `plan.md` - Strategic plan
- `todos.yaml` - Current task list

### Configuration Inheritance

Configs use `"$extends": "defaults"` to inherit from `src/config/defaults.json`:

```json
{
  "$extends": "defaults",
  "agent_id": "creator",
  "tools": { "domain": ["extract_document_text", "web_search", ...] }
}
```

### Workspace Structure

Per-job directory: `workspace/job_<uuid>/`
- `archive/` - Phase artifacts
- `documents/` - Input documents
- `tools/` - Tool outputs
- Checkpoints: `workspace/checkpoints/job_<id>.db`

## Key Source Directories

- `src/core/` - State management, workspace, context, phase transitions
- `src/managers/` - TodoManager, MemoryManager, PlanManager
- `src/tools/` - Tool implementations and registry
- `src/database/` - PostgreSQL (asyncpg), Neo4j, MongoDB managers
- `src/api/` - FastAPI application
- `configs/` - Agent-specific configurations
- `dashboard/` - Streamlit UI

## Graph Structure (src/graph.py)

```
init_workspace → read_instructions → create_plan
       ↓
execute (ReAct) → check_todos → archive_phase → handle_transition → check_goal
```

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

### Workspace Files and Logs

Per-job files are stored in the workspace directory:
- **Workspace files**: `workspace/job_<uuid>/` - Contains `workspace.md`, `todos.yaml`, `plan.md`, and subdirectories
- **Checkpoints**: `workspace/checkpoints/job_<id>.db` - SQLite checkpoint for resume capability
- **Logs**: `workspace/logs/job_<id>.log` - Agent execution logs

```bash
# Clean up checkpoint/log files for a specific job
rm workspace/checkpoints/job_<id>.db workspace/logs/job_<id>.log

# Clean up all checkpoints and logs
rm workspace/checkpoints/job_*.db workspace/logs/job_*.log
```

### MongoDB Conversation Viewer

MongoDB stores LLM request/response history and agent audit trails for debugging. Enable by setting `MONGODB_URL` in `.env`:

```bash
MONGODB_URL=mongodb://localhost:27017/graphrag_logs
```

Use `scripts/view_llm_conversation.py` to inspect agent behavior:

```bash
# List all jobs with records
python scripts/view_llm_conversation.py --list

# View LLM conversation for a job
python scripts/view_llm_conversation.py --job-id <uuid>

# View job statistics (token usage, latency, duration)
python scripts/view_llm_conversation.py --job-id <uuid> --stats

# View complete agent audit trail (all steps)
python scripts/view_llm_conversation.py --job-id <uuid> --audit

# View only tool calls in audit trail
python scripts/view_llm_conversation.py --job-id <uuid> --audit --step-type tool_call

# View audit as timeline visualization
python scripts/view_llm_conversation.py --job-id <uuid> --audit --timeline

# Export conversation to JSON
python scripts/view_llm_conversation.py --job-id <uuid> --export conversation.json

# View single LLM request as HTML (opens in browser)
python scripts/view_llm_conversation.py --doc-id <mongodb_objectid> --temp

# View recent requests across all jobs
python scripts/view_llm_conversation.py --recent 20
```

MongoDB collections:
- `llm_requests` - Full LLM request/response with messages, model, latency, token usage
- `agent_audit` - Step-by-step execution trace (tool calls, phase transitions, routing decisions)
