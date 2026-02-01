# Graph-RAG Requirements Compliance System

A Graph-RAG system for requirement traceability and compliance checking. Uses LangGraph and LLMs to extract requirements from documents, validate them against a Neo4j knowledge graph, and track GoBD/GDPR compliance.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Production Deployment](#production-deployment)
- [Development Setup](#development-setup)
- [Architecture](#architecture)
- [Debugging](#debugging)
- [License](#license)

## Prerequisites

- **Docker or Podman** with Compose support
- **Git**
- **Python 3.11+** (development only)

## Production Deployment

Deploy the complete system using pre-built container images.

### 1. Clone and Configure

```bash
git clone https://github.com/Knaeckebrothero/Uni-Projekt-Graph-RAG.git
cd Uni-Projekt-Graph-RAG
cp .env.example .env
```

### 2. Edit Environment Variables

Edit `.env` with your configuration:

**Required:**
- `OPENAI_API_KEY` - Your OpenAI API key, or compatible API key
- `LLM_BASE_URL` - Custom endpoint URL (if using self-hosted models)

**Optional:**
- `TAVILY_API_KEY` - For web search functionality
- `NEO4J_PASSWORD`, `POSTGRES_PASSWORD` - Custom database passwords
- `LOG_LEVEL` - DEBUG, INFO, WARNING, ERROR (default: INFO)
- Port overrides: `AGENT_PORT`, `DASHBOARD_PORT`, etc.

### 3. Start All Services

```bash
podman-compose up -d
```

This starts:
- **PostgreSQL** - Job tracking and data cache
- **Neo4j** - Knowledge graph storage (optional)
- **Agent** - Universal agent for document processing
- **Dashboard** - Streamlit UI for job management

### 4. Access Services

| Service | URL |
|---------|-----|
| Dashboard | http://localhost:8501 |
| Neo4j Browser | http://localhost:7474 |
| Agent API | http://localhost:8001 |

### 5. Common Operations

```bash
# View logs
podman-compose logs -f
podman-compose logs -f agent

# Check service status
podman-compose ps

# Restart services
podman-compose restart

# Stop all services
podman-compose down

# Stop and remove all data
podman-compose down -v
```

## Development Setup

Run databases in containers while developing agents locally with Python.

### 1. Clone and Set Up Python Environment

```bash
git clone https://github.com/Knaeckebrothero/Uni-Projekt-Graph-RAG.git
cd Uni-Projekt-Graph-RAG

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt
pip install -e ./citation_tool[full]
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your API credentials
```

### 3. Start Databases

```bash
podman-compose -f docker-compose.dev.yaml up -d
```

This starts PostgreSQL, Neo4j, and MongoDB (for optional LLM request logging).

### 4. Initialize Databases and Workspace

```bash
# Initialize everything (databases + workspace)
python init.py

# Reset everything (WARNING: deletes all data)
python init.py --force-reset
```

### 5. Run Agents Locally

```bash
# Process a document (uses defaults config)
python agent.py --document-path ./data/doc.pdf --prompt "Extract requirements"

# Run as API server
python agent.py --port 8001

# Run with custom config
python agent.py --config my_agent --port 8001

# Run with streaming output
python agent.py --document-path ./data/doc.pdf --prompt "Extract requirements" --stream --verbose

# Process a directory of documents
python agent.py --document-dir ./data/example_data/ --prompt "Identify possible requirements" --stream --verbose
```

### 6. Crash Recovery & Checkpointing

The agent automatically creates checkpoints during execution, enabling resume after crashes:

```bash
# Start a job with explicit job ID (for later resume)
python agent.py --job-id my-job-123 --document-path ./data/doc.pdf --prompt "Extract requirements"

# If the agent crashes, resume from the last checkpoint
python agent.py --job-id my-job-123 --resume

# Resume with streaming output
python agent.py --job-id my-job-123 --resume --stream --verbose
```

**How it works:**
- Checkpoints: `workspace/checkpoints/job_<id>.db` (SQLite)
- Logs: `workspace/logs/job_<id>.log`
- LangGraph saves state after every graph node execution
- Resuming with the same `--job-id` continues from the last checkpoint
- Checkpoints and logs are kept after completion for debugging

**Cleanup:**
```bash
# Remove all checkpoint files
rm workspace/checkpoints/job_*.db

# Remove all log files
rm workspace/logs/job_*.log

# Remove files for specific job
rm workspace/checkpoints/job_my-job-123.db
rm workspace/logs/job_my-job-123.log
```

### 7. Initialization Scripts

The system provides modular initialization scripts for different deployment scenarios:

**Root Initialization (Recommended for Development):**
```bash
# Initialize everything (databases + workspace)
python init.py

# Reset everything (WARNING: deletes all data)
python init.py --force-reset

# Initialize only databases (orchestrator components)
python init.py --only-orchestrator

# Initialize only workspace (agent components)
python init.py --only-agent

# Skip optional MongoDB
python init.py --skip-mongodb
```

**Component-Specific Initialization:**
```bash
# Database initialization only (PostgreSQL + MongoDB)
python -m orchestrator.init

# Database initialization with options
python -m orchestrator.init --force-reset       # Reset databases
python -m orchestrator.init --skip-mongodb      # Skip MongoDB
python -m orchestrator.init --verify            # Verify connectivity only

# Workspace initialization only
python -m src.init

# Workspace initialization with options
python -m src.init --force-reset                # Clean all workspaces
python -m src.init --verify                     # Verify workspace structure
```

**Backup and Restore:**
```bash
# Create backup (auto-named: backups/YYYYMMDD_NNN/)
python init.py --create-backup

# Create backup with custom name
python init.py --create-backup my_backup

# Restore from backup
python init.py --restore-backup backups/20260201_001_my_backup
```

**Nuclear Option:**
```bash
# Remove all Docker volumes and reinitialize
podman-compose -f docker-compose.dev.yaml down -v
podman-compose -f docker-compose.dev.yaml up -d
python init.py
```

### 8. Stop Databases

```bash
podman-compose -f docker-compose.dev.yaml down      # Keep data
podman-compose -f docker-compose.dev.yaml down -v   # Remove data
```

## Architecture

The system uses a **Universal Agent** pattern - a single config-driven agent that can be customized for different tasks by changing its YAML configuration:

```
┌─────────────────────────────────────────────────────────────────────┐
│                           DASHBOARD                                 │
│                  (Streamlit UI - Job Management)                    │
└───────────────────────────────────┬─────────────────────────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────┐
                    │     UNIVERSAL AGENT       │
                    │                           │
                    │ - Document processing     │
                    │ - Research & citations    │
                    │ - Tool execution          │
                    │ - Phase-based planning    │
                    └─────────────┬─────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
                    ▼                           ▼
        ┌─────────────────────┐     ┌─────────────────────┐
        │     PostgreSQL      │     │       Neo4j         │
        │   (Jobs & Cache)    │     │  (Knowledge Graph)  │
        └─────────────────────┘     └─────────────────────┘
```

**Universal Agent:**

Agent behavior is determined by YAML configuration in `config/`:

- `config/defaults.yaml` - Framework defaults (all configs extend this)
- `config/my_agent.yaml` - Custom single-file config
- `config/my_agent/config.yaml` - Directory config with prompt overrides

Configs use `$extends: defaults` to inherit and override specific settings. See [config/README.md](config/README.md) for creating custom configs and [CLAUDE.md](CLAUDE.md) for development guidelines.

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

## License

Creative Commons Attribution 4.0 International License (CC BY 4.0). See [LICENSE.txt](LICENSE.txt).
