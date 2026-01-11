# Graph-RAG Requirements Compliance System

An intelligent workflow system for analyzing business requirements against a Neo4j graph database. Uses LangChain, LangGraph, and LLMs to automatically extract requirements from documents, validate them against existing knowledge, and integrate them into a compliance-tracking graph.

## Overview

This **Graph-RAG (Retrieval-Augmented Generation) system** is designed for **requirement traceability and compliance checking** in a car rental business context. The system focuses on:

- **GoBD Compliance**: German accounting principles (Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern)
- **GDPR Compliance**: European data protection requirements
- **Requirement Traceability**: Tracking relationships between requirements, business objects, and system messages
- **Impact Analysis**: Understanding how new requirements affect existing system components
- **Automated Validation**: LLM-powered compliance verification with citation tracking

## Architecture

The system uses a **two-agent autonomous architecture** for long-running requirement processing:

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
│                     │                      │                     │
│ - Document process  │                      │ - Graph exploration │
│ - Requirement       │   PostgreSQL Cache   │ - Relevance check   │
│   extraction        │ ◄──────────────────► │ - Fulfillment check │
│ - Research/citation │   (requirement_cache)│ - Graph integration │
└─────────────────────┘                      └─────────────────────┘
                                                        │
                                                        ▼
                                              ┌─────────────────────┐
                                              │       Neo4j         │
                                              │  (Knowledge Graph)  │
                                              └─────────────────────┘
```

### Components

- **Creator Agent**: Processes documents, extracts requirement candidates, performs web/graph research, creates citations
- **Validator Agent**: Validates requirements against existing graph, checks fulfillment status, integrates into Neo4j
- **Orchestrator**: Manages job lifecycle, monitors completion, generates reports
- **Legacy UI**: Streamlit-based interface for interactive single-requirement analysis

## Quick Start

### Docker Deployment (Recommended)

```bash
# Clone and configure
git clone https://github.com/Knaeckebrothero/Uni-Projekt-Graph-RAG.git
cd Uni-Projekt-Graph-RAG
cp .env.docker.example .env

# Edit .env with your LLM API credentials
# Required: OPENAI_API_KEY or LLM_BASE_URL
# Optional: TAVILY_API_KEY for web search

# Start all services
podman-compose up -d

# Check status
podman-compose ps

# View logs
podman-compose logs -f orchestrator
```

### Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
pip install -e ./citation_tool[full]

# Configure environment
cp .env.example .env
# Edit .env with your credentials

# Initialize PostgreSQL (requires running PostgreSQL instance)
python src/scripts/init_db.py

# Start the legacy Streamlit UI
streamlit run main.py
```

## Usage

### Create a Processing Job

```bash
# Via CLI
python start_orchestrator.py \
  --document-path ./data/gdpr-requirements.pdf \
  --prompt "Extract and validate GDPR compliance requirements" \
  --context '{"domain": "car_rental", "region": "EU"}'

# Via API (Docker)
curl -X POST http://localhost:8000/jobs \
  -F "document=@./data/document.pdf" \
  -F "prompt=Review for GoBD compliance"
```

### Monitor Job Progress

```bash
# CLI
python job_status.py --job-id <uuid> --progress

# API
curl http://localhost:8000/jobs/<uuid>
```

### Get Job Report

```bash
# CLI
python job_status.py --job-id <uuid> --report

# API
curl http://localhost:8000/jobs/<uuid>/report
```

### Legacy Interactive UI

For single-requirement analysis with streaming progress:

```bash
streamlit run main.py
```

## Configuration

### Environment Variables

```bash
# Database
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your_password
DATABASE_URL=postgresql://graphrag:password@localhost:5432/graphrag

# LLM
OPENAI_API_KEY=sk-xxxxx
LLM_BASE_URL=http://localhost:8080/v1  # For self-hosted models

# Optional
TAVILY_API_KEY=tvly-xxxxx  # Web search for Creator Agent
```

### LLM Configuration

Edit `src/config/llm_config.json`:

```json
{
  "creator_agent": {
    "model": "gpt-4",
    "temperature": 0.0,
    "reasoning_level": "high",
    "research_depth": "thorough"
  },
  "validator_agent": {
    "model": "gpt-4",
    "temperature": 0.0,
    "auto_integrate": false,
    "require_citations": true
  }
}
```

**Reasoning Levels** (for compatible models):
| Level | Use Case |
|-------|----------|
| `low` | Fast responses, simple tasks |
| `medium` | Balanced speed and detail |
| `high` | Deep analysis, complex compliance |

## Database Metamodel

The system uses a graph metamodel designed for requirement traceability:

### Node Types

| Type | Key Properties | Description |
|------|----------------|-------------|
| `Requirement` | `rid`, `name`, `text`, `goBDRelevant`, `complianceStatus` | Business/system requirements |
| `BusinessObject` | `boid`, `name`, `domain` | Business domain entities |
| `Message` | `mid`, `name`, `direction`, `format` | System messages/events |

### Relationships

```
Requirement → Requirement:    REFINES, DEPENDS_ON, TRACES_TO, SUPERSEDES
Requirement → BusinessObject: RELATES_TO_OBJECT, IMPACTS_OBJECT, FULFILLED_BY_OBJECT, NOT_FULFILLED_BY_OBJECT
Requirement → Message:        RELATES_TO_MESSAGE, IMPACTS_MESSAGE, FULFILLED_BY_MESSAGE, NOT_FULFILLED_BY_MESSAGE
Message → BusinessObject:     USES_OBJECT, PRODUCES_OBJECT
```

### Database Setup

**Option 1: Import sample data**
```bash
neo4j stop
neo4j-admin database load --from-path=data/ neo4j --overwrite-destination=true
neo4j start
```

**Option 2: Create schema only**
```bash
# Execute data/metamodell.cql in Neo4j Browser
```

## API Reference

### Orchestrator Endpoints (port 8000)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/jobs` | Create new processing job |
| `GET` | `/jobs` | List all jobs |
| `GET` | `/jobs/{id}` | Get job details |
| `GET` | `/jobs/{id}/report` | Get full job report |
| `DELETE` | `/jobs/{id}` | Cancel job |

### Health Endpoints (all services)

| Endpoint | Description |
|----------|-------------|
| `/health` | Liveness check |
| `/ready` | Readiness (DB connectivity) |
| `/status` | Detailed agent state |
| `/metrics` | Prometheus metrics |

### Service Ports

| Service | Port |
|---------|------|
| Orchestrator | 8000 |
| Creator Agent | 8001 |
| Validator Agent | 8002 |
| Neo4j Browser | 7474 |
| Neo4j Bolt | 7687 |
| PostgreSQL | 5432 |
| Adminer (dev) | 8080 |

## CLI Reference

```bash
# Job management
python start_orchestrator.py --document-path FILE --prompt TEXT [--wait]
python job_status.py --job-id UUID [--report|--progress]
python list_jobs.py [--status STATUS] [--stats]
python cancel_job.py --job-id UUID [--cleanup]

# Metamodel validation
python validate_metamodel.py [--check CHECK_ID] [--json]

# Batch processing (legacy)
python -m src.workflow
```

## Example Use Cases

### GoBD Compliance Check

```bash
python start_orchestrator.py \
  --document-path ./data/billing-spec.pdf \
  --prompt "Identify GoBD-relevant requirements and verify compliance" \
  --wait
```

### GDPR Impact Analysis

```bash
python start_orchestrator.py \
  --document-path ./data/eu-expansion.docx \
  --prompt "Extract GDPR requirements for EU market expansion" \
  --context '{"region": "EU", "focus": "data_protection"}'
```

### Requirement Traceability

Query the graph after processing:

```cypher
// Find unfulfilled GoBD requirements
MATCH (r:Requirement {goBDRelevant: true, complianceStatus: 'open'})
RETURN r.rid, r.name, r.text

// Show fulfillment relationships
MATCH (r:Requirement)-[rel:FULFILLED_BY_OBJECT|NOT_FULFILLED_BY_OBJECT]->(bo:BusinessObject)
RETURN r.name, type(rel), rel.confidence, bo.name
```

## Project Structure

```
├── src/
│   ├── agents/
│   │   ├── creator/        # Creator Agent
│   │   ├── validator/      # Validator Agent
│   │   ├── shared/         # Shared utilities
│   │   └── graph_agent.py  # Legacy single-agent
│   ├── core/               # Database utils, config
│   ├── orchestrator/       # Job management
│   └── ui/                 # Streamlit pages
├── config/
│   ├── llm_config.json     # LLM settings
│   └── prompts/            # Agent prompts
├── data/
│   ├── metamodell.cql      # Neo4j schema
│   └── requirements.csv    # Sample data (82 requirements)
├── docker/                 # Dockerfiles
├── migrations/             # PostgreSQL schema
└── docker-compose.yml
```

## Development

### Run with Hot Reload

```bash
podman-compose -f docker-compose.yml -f docker-compose.dev.yml up
```

### Run Tests

```bash
# Syntax check
python -m py_compile src/**/*.py

# Metamodel validation
python validate_metamodel.py
```

### Database Access

```bash
# PostgreSQL
podman-compose exec postgres psql -U graphrag -d graphrag

# Neo4j
podman-compose exec neo4j cypher-shell -u neo4j -p password
```

## Documentation

- `masterplan.md` - System design and architecture
- `masterplan_roadmap.md` - Implementation phases and status
- `CLAUDE.md` - AI assistant guidance
- `data/metamodell.cql` - Database schema definition

## License

This project is licensed under Creative Commons Attribution 4.0 International License (CC BY 4.0). See [LICENSE.txt](LICENSE.txt) for details.

## Contact

- [GitHub](https://github.com/Knaeckebrothero)
- [Email](mailto:OverlyGenericAddress@pm.me)
