# Graph-RAG Requirements Compliance System

A Graph-RAG system for requirement traceability and compliance checking. Uses LangGraph and LLMs to extract requirements from documents, validate them against a Neo4j knowledge graph, and track GoBD/GDPR compliance.

## Table of Contents

- [Quick Start](#quick-start)
- [Installation](#installation)
  - [Docker Deployment](#docker-deployment)
  - [Local Development](#local-development)
- [Architecture](#architecture)
- [License](#license)

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt
pip install -e ./citation_tool[full]

# Configure environment
cp .env.example .env  # Edit with your API credentials

# Start databases
podman-compose -f docker-compose.dbs.yml up -d

# Initialize with seed data
python src/scripts/app_init.py --force-reset --seed

# Run the agent
python agent.py --config creator --document-path ./data/doc.pdf --prompt "Extract requirements"
```

## Installation

### Docker Deployment

```bash
# Clone and configure
git clone https://github.com/Knaeckebrothero/Uni-Projekt-Graph-RAG.git
cd Uni-Projekt-Graph-RAG
cp .env.docker.example .env
# Edit .env with your API credentials (OPENAI_API_KEY or LLM_BASE_URL)

# Start all services
podman-compose up -d

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

# Start databases only
podman-compose -f docker-compose.dbs.yml up -d

# Initialize databases
python src/scripts/app_init.py --force-reset --seed

# Run Universal Agent
python agent.py --config creator --document-path ./data/doc.pdf --prompt "Extract requirements"
python agent.py --config creator --port 8001  # API server mode
python agent.py --config validator --port 8002  # Validator server
```

## Architecture

The system uses a **Universal Agent** pattern - a single config-driven agent that can be deployed as either Creator or Validator by changing its configuration:

```
┌─────────────────────────────────────────────────────────────────────┐
│                          ORCHESTRATOR                               │
│                    (Job Management & Coordination)                  │
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
