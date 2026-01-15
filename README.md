# Graph-RAG Requirements Compliance System

A Graph-RAG system for requirement traceability and compliance checking. Uses LangGraph and LLMs to extract requirements from documents, validate them against a Neo4j knowledge graph, and track GoBD/GDPR compliance.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Production Deployment](#production-deployment)
- [Development Setup](#development-setup)
- [Architecture](#architecture)
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
- `TAVILY_API_KEY` - For web search functionality in Creator agent
- `NEO4J_PASSWORD`, `POSTGRES_PASSWORD` - Custom database passwords
- `LOG_LEVEL` - DEBUG, INFO, WARNING, ERROR (default: INFO)
- Port overrides: `CREATOR_PORT`, `VALIDATOR_PORT`, `DASHBOARD_PORT`, etc.

### 3. Start All Services

```bash
podman-compose up -d
```

This starts:
- **PostgreSQL** - Job tracking and requirement cache
- **Neo4j** - Knowledge graph storage
- **Creator Agent** - Document processing and requirement extraction
- **Validator Agent** - Requirement validation and graph integration
- **Dashboard** - Streamlit UI for job management

### 4. Access Services

| Service | URL |
|---------|-----|
| Dashboard | http://localhost:8501 |
| Neo4j Browser | http://localhost:7474 |
| Creator API | http://localhost:8001 |
| Validator API | http://localhost:8002 |

### 5. Common Operations

```bash
# View logs
podman-compose logs -f
podman-compose logs -f creator validator

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

### 4. Initialize Databases

```bash
# First time setup with sample data
python scripts/app_init.py --seed

# Reset everything (deletes all data)
python scripts/app_init.py --force-reset --seed
```

### 5. Run Agents Locally

```bash
# Process a document
python agent.py --config creator --document-path ./data/doc.pdf --prompt "Extract requirements"

# Run Creator as API server
python agent.py --config creator --port 8001

# Run Validator as API server
python agent.py --config validator --port 8002

# Run with streaming output
python agent.py --config creator --document-path ./data/doc.pdf --prompt "Extract requirements" --stream --verbose

# Perform a full scale test
python agent.py --config creator --document-dir ./data/example_data/ --prompt "Identify possible requirements for a medium sized car rental company based on the provided GoBD document." --stream --verbose
```

### 6. Crash Recovery & Checkpointing

The agent automatically creates checkpoints during execution, enabling resume after crashes:

```bash
# Start a job with explicit job ID (for later resume)
python agent.py --config creator --job-id my-job-123 --document-path ./data/doc.pdf --prompt "Extract requirements"

# If the agent crashes, resume from the last checkpoint
python agent.py --config creator --job-id my-job-123 --resume

# Resume with streaming output
python agent.py --config creator --job-id my-job-123 --resume --stream --verbose
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

### 7. Database Management

```bash
# Reset PostgreSQL only
python scripts/app_init.py --only-postgres --force-reset

# Reset Neo4j with seed data
python scripts/app_init.py --only-neo4j --force-reset --seed

# View init script options
python scripts/app_init.py --help

# Nuclear option: remove all Docker volumes and reinitialize
podman-compose -f docker-compose.dev.yaml down -v
podman-compose -f docker-compose.dev.yaml up -d
python scripts/app_init.py --seed
```

### 8. Stop Databases

```bash
podman-compose -f docker-compose.dev.yaml down      # Keep data
podman-compose -f docker-compose.dev.yaml down -v   # Remove data
```

## Architecture

The system uses a **Universal Agent** pattern - a single config-driven agent that can be deployed as either Creator or Validator by changing its configuration:

```
┌─────────────────────────────────────────────────────────────────────┐
│                           DASHBOARD                                 │
│                  (Streamlit UI - Job Management)                    │
└───────────────────────────────────┬─────────────────────────────────┘
                                    │
         ┌──────────────────────────┴──────────────────────────┐
         │                                                      │
         ▼                                                      ▼
┌───────────────────────────┐                    ┌───────────────────────────┐
│     UNIVERSAL AGENT       │                    │     UNIVERSAL AGENT       │
│   (config: creator)       │                    │   (config: validator)     │
│                           │                    │                           │
│ - Document processing     │  PostgreSQL Cache  │ - Graph exploration       │
│ - Requirement extraction  │ ◄────────────────► │ - Relevance checking      │
│ - Research & citations    │   (requirements)   │ - Fulfillment validation  │
│                           │                    │ - Neo4j integration       │
└───────────────────────────┘                    └───────────────────────────┘
                                                              │
                                                              ▼
                                                  ┌─────────────────────┐
                                                  │       Neo4j         │
                                                  │  (Knowledge Graph)  │
                                                  └─────────────────────┘
```

**Universal Agent:**

The same agent codebase (`src/agent/`) serves both roles. Behavior is determined by configuration:

| Config | Purpose | Tools | Polls |
|--------|---------|-------|-------|
| `creator` | Extract requirements from documents | document, search, citation, cache | `jobs` table |
| `validator` | Validate and integrate into graph | graph, cypher, validation | `requirements` table |

Configs live in `configs/{name}/` and extend framework defaults via `$extends`. See [CLAUDE.md](CLAUDE.md) for the full configuration system.

**Data flow:**
1. Creator polls `jobs` table → processes document → writes to `requirements` table
2. Validator queries pending requirements → validates → integrates into Neo4j

For detailed configuration, API reference, and development guidelines, see [CLAUDE.md](CLAUDE.md).

## License

Creative Commons Attribution 4.0 International License (CC BY 4.0). See [LICENSE.txt](LICENSE.txt).
