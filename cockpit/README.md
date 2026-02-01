# Debug Cockpit

Angular dashboard for debugging and visualizing Graph-RAG agent execution. Features a tiling layout system with pluggable components for viewing audit trails, graph changes, database tables, and LLM requests.

## Quick Start

```bash
# Terminal 1: Start Orchestrator backend
cd orchestrator
pip install -r requirements.txt
uvicorn main:app --reload --port 8085

# Terminal 2: Start Angular frontend
cd cockpit
npm install
npm start
# Open http://localhost:4200
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Angular Frontend                         │
│                    http://localhost:4200                     │
├─────────────────────────────────────────────────────────────┤
│                   Orchestrator Backend                       │
│                    http://localhost:8085                     │
├──────────────────┬──────────────────┬───────────────────────┤
│    PostgreSQL    │     MongoDB      │        Neo4j          │
│   (jobs, reqs)   │  (audit trail)   │   (graph changes)     │
└──────────────────┴──────────────────┴───────────────────────┘
        │
        └──── MCP Server (stdio) ← Claude Code
```

## Features

- Dark themed UI (Catppuccin Mocha)
- Timeline scrubber with global time synchronization
- Resizable panel layout with drag handles
- Component switcher dropdown in each panel
- Split buttons to divide panels

### Components

| Component | Description |
|-----------|-------------|
| Agent Activity | MongoDB audit trail with filtering and pagination |
| Graph Viewer | Neo4j graph visualization with timeline playback |
| DB Table | PostgreSQL table browser |
| Request Viewer | Full LLM request/response inspector |

## Development

```bash
# Start dev server
npm start

# Build for production
npm run build

# Run tests
npm test
```

## API Endpoints

The orchestrator backend runs on port **8085** and provides:

- `GET /api/tables` - List available PostgreSQL tables
- `GET /api/tables/{name}` - Get paginated table data
- `GET /api/jobs` - List jobs with audit counts
- `GET /api/jobs/{id}/audit` - Get paginated audit entries
- `GET /api/jobs/{id}/audit/timerange` - Get time bounds for timeline
- `GET /api/graph/changes/{id}` - Get graph deltas for visualization
- `GET /api/requests/{doc_id}` - Get full LLM request document

## Environment

The orchestrator backend requires these environment variables (see `.env.example`):

- `DATABASE_URL` - PostgreSQL connection string
- `MONGODB_URL` - MongoDB connection string (optional)
- `NEO4J_URI` - Neo4j Bolt URI
- `NEO4J_USERNAME` / `NEO4J_PASSWORD` - Neo4j credentials

## MCP Server

The MCP (Model Context Protocol) server exposes cockpit metrics to LLMs like Claude Code, enabling AI-assisted debugging of agent jobs.

### Setup

**Local development:**
```bash
cd orchestrator/mcp
pip install -r requirements.txt
python run.py
```

**Docker:**
```bash
podman-compose -f docker-compose.dev.yaml up -d orchestrator-mcp
docker exec -i graphrag-orchestrator-mcp-dev python run.py
```

### Claude Code Configuration

The project includes `.mcp.json` with the MCP server configuration. Claude Code will prompt you to enable it.

For containerized setup, create or update `.mcp.json`:
```json
{
  "mcpServers": {
    "orchestrator": {
      "command": "docker",
      "args": ["exec", "-i", "graphrag-orchestrator-mcp-dev", "python", "run.py"]
    }
  }
}
```

### Available Tools

| Tool | Description |
|------|-------------|
| `list_jobs` | List jobs with status filter |
| `get_job` | Get job details by ID |
| `get_audit_trail` | Get paginated audit entries |
| `get_chat_history` | Get conversation turns |
| `get_todos` | Get current and archived todos |
| `get_graph_changes` | Get Neo4j graph mutations timeline |
| `get_llm_request` | Get full LLM request/response |
| `search_audit` | Search audit entries by pattern |
