# Orchestrator

Backend API for the Graph-RAG system. Provides:
- Monitoring data to the Cockpit (Angular frontend)
- Agent registration and orchestration (planned)
- MCP server for Claude Code integration

## Quick Start

```bash
cd orchestrator
pip install -r requirements.txt
uvicorn main:app --reload --port 8085
```

## Architecture

```
orchestrator/
├── main.py              # FastAPI application and routes
├── graph_routes.py      # Neo4j graph visualization endpoints
├── services/
│   ├── postgres.py      # PostgreSQL service (jobs, requirements)
│   ├── mongodb.py       # MongoDB service (audit trail, LLM logs)
│   └── workspace.py     # Workspace file reading (todos, plan)
├── mcp/
│   ├── server.py        # FastMCP 2.0 server
│   ├── client.py        # Async HTTP client for API
│   └── run.py           # Entry point
└── requirements.txt
```

## API Endpoints

### Monitoring (Current)

| Endpoint | Description |
|----------|-------------|
| `GET /api/tables` | List PostgreSQL tables |
| `GET /api/tables/{name}` | Get paginated table data |
| `GET /api/jobs` | List jobs with audit counts |
| `GET /api/jobs/{id}/audit` | Get audit trail entries |
| `GET /api/jobs/{id}/chat` | Get chat history |
| `GET /api/jobs/{id}/todos` | Get workspace todos |
| `GET /api/graph/changes/{id}` | Get Neo4j mutations |
| `GET /api/requests/{doc_id}` | Get full LLM request |

### Agent Orchestration (Planned)

| Endpoint | Description |
|----------|-------------|
| `POST /api/agents/register` | Agent registers itself |
| `POST /api/agents/{id}/heartbeat` | Agent status update |
| `GET /api/agents` | List registered agents |
| `PUT /api/agents/{id}/assign` | Assign job to agent |

See `docs/angular_migration_plan.md` for the full orchestration architecture.

## Environment Variables

```bash
# PostgreSQL (required)
DATABASE_URL=postgresql://user:pass@localhost:5432/graphrag

# MongoDB (optional - for audit trail)
MONGODB_URL=mongodb://localhost:27017/graphrag_logs

# Neo4j (optional - for graph visualization)
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password
```

## MCP Server

The MCP server provides Claude Code with debugging tools.

```bash
# Run locally
cd orchestrator/mcp
python run.py

# Or via HTTP
uvicorn main:app --port 8055  # MCP endpoint at /mcp
```

### Available Tools

- `list_jobs` - List jobs with status filter
- `get_job` - Get job details
- `get_audit_trail` - Get paginated audit entries
- `get_chat_history` - Get conversation turns
- `get_todos` - Get workspace todos
- `get_graph_changes` - Get Neo4j mutations
- `get_llm_request` - Get full LLM request/response
- `search_audit` - Search audit entries
