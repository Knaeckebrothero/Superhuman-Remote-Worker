# Superhuman Remote Worker

An AI agent you can treat like a remote employee. Message it a task — research, data analysis, document processing, database work, coding — and it gets it done autonomously. Built on LangGraph with a config-driven architecture that adapts to any role.

## How It Works

You describe a job in plain language. The agent breaks it down into phases, plans its approach, executes using the tools it needs, and delivers results — just like a remote worker on your team. It self-manages through strategic planning and tactical execution cycles, maintaining its own workspace, notes, and progress tracking across arbitrarily long tasks.

**What it can do:**
- Research topics on the web and synthesize findings
- Process and analyze documents (PDF, DOCX, PPTX, images)
- Query and manipulate databases (PostgreSQL, Neo4j, MongoDB)
- Write, review, and manage structured output
- Execute multi-step workflows with checkpointing and crash recovery
- Manage citations and literature

**What makes it different:**
- **Persistent memory** — workspace files survive context window limits, so it never loses track of long tasks
- **Phase-based execution** — alternates between strategic planning and tactical work, adapting its plan as it learns
- **Crash recovery** — checkpoints at every step, resume any job from where it left off
- **Config-driven roles** — same codebase, different YAML configs for different specializations
- **Multi-database support** — attach PostgreSQL, Neo4j, or MongoDB datasources to any job

## Table of Contents

- [Quick Start](#quick-start)
- [Production Deployment](#production-deployment)
- [Development Setup](#development-setup)
- [Architecture](#architecture)
- [Debugging](#debugging)
- [License](#license)

## Quick Start

```bash
# Clone and set up
git clone <repo-url>
cd Superhuman-Remote-Worker
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Add your API keys

# Start databases
podman-compose -f docker-compose.dev.yaml up -d
python init.py

# Give it a task
python agent.py --description "Research the current state of EU AI regulation and summarize key requirements"
```

## Production Deployment

Deploy the complete system using containers.

### 1. Clone and Configure

```bash
git clone <repo-url>
cd Superhuman-Remote-Worker
cp .env.example .env
```

### 2. Edit Environment Variables

Edit `.env` with your configuration:

**Required:**
- `OPENAI_API_KEY` — LLM API key (or compatible provider)
- `LLM_BASE_URL` — Custom endpoint URL (if using self-hosted models)

**Optional:**
- `ANTHROPIC_API_KEY` — For Claude models
- `GOOGLE_API_KEY` — For Gemini models
- `TAVILY_API_KEY` — For web search
- `MONGODB_URL` — LLM request logging and audit trail
- `LOG_LEVEL` — DEBUG, INFO, WARNING, ERROR (default: INFO)

### 3. Start All Services

```bash
podman-compose up -d
```

This starts:
- **PostgreSQL** — Job tracking and data storage
- **MongoDB** — LLM request logging and audit trail (optional)
- **Gitea** — Git server for agent workspace repositories
- **Orchestrator** — Backend API for job management and agent coordination
- **Agent** — Worker instances (defaults to 2 replicas via `AGENT_REPLICAS`)
- **Cockpit** — Web UI for job management and monitoring

### 4. Access Services

| Service | URL |
|---------|-----|
| Cockpit (Web UI) | http://localhost:4000 |
| Orchestrator API | http://localhost:8085 |
| Gitea | http://localhost:3000 |

### 5. Common Operations

```bash
# View logs
podman-compose logs -f
podman-compose logs -f agent

# Scale workers
podman-compose up -d --scale agent=4

# Stop all services
podman-compose down

# Stop and remove all data
podman-compose down -v
```

## Development Setup

Run databases in containers while developing locally with Python.

### 1. Set Up Python Environment

```bash
git clone <repo-url>
cd Superhuman-Remote-Worker

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# System dependencies (Fedora)
sudo dnf install poppler-utils         # PDF rendering
# Debian/Ubuntu: sudo apt-get install poppler-utils
playwright install chromium            # Browser-based research
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

### 4. Initialize

```bash
python init.py                          # Initialize everything
python init.py --force-reset            # Reset everything (WARNING: deletes all data)
python init.py --only-orchestrator      # Databases only
python init.py --only-agent             # Workspace only
```

### 5. Run the Agent

```bash
# Give it a task
python agent.py --description "Your task here"

# With a custom agent config
python agent.py --config my_agent --description "Your task"

# Process a document
python agent.py --document-path ./data/doc.pdf --description "Extract key findings"

# Process a directory of documents
python agent.py --document-dir ./data/reports/ --description "Compare and summarize these reports"

# Run as an API server
python agent.py --port 8001

# Resume a crashed job
python agent.py --job-id <id> --resume

# Debug mode
LOG_LEVEL=DEBUG python agent.py --description "Your task"
```

### 6. Backup and Restore

```bash
python init.py --create-backup                          # Auto-named backup
python init.py --create-backup before_experiment        # Named backup
python init.py --restore-backup backups/20260201_001    # Restore
```

### 7. Testing

```bash
pytest tests/                              # All tests
pytest tests/test_graph.py -v              # Single file
pytest tests/ -k "todo"                    # Pattern match
pytest tests/ --cov=src                    # With coverage
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                           COCKPIT                                │
│                   (Web UI — Job Management)                      │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
                 ┌───────────────────────────┐
                 │       ORCHESTRATOR        │
                 │                           │
                 │  Job queue & coordination │
                 │  Agent health monitoring  │
                 │  Statistics & API         │
                 └─────────────┬─────────────┘
                               │
             ┌─────────────────┼─────────────────┐
             │                 │                 │
             ▼                 ▼                 ▼
┌────────────────────┐  ┌───────────┐  ┌─────────────────┐
│  UNIVERSAL AGENT   │  │ PostgreSQL│  │  Datasources    │
│                    │  │ (system)  │  │  (per-job)      │
│ Config-driven      │  └───────────┘  │                 │
│ Phase planning     │                 │ PostgreSQL      │
│ Tool execution     │                 │ Neo4j           │
│ Crash recovery     │                 │ MongoDB         │
└────────────────────┘                 └─────────────────┘
```

### Universal Agent

A single agent codebase configured for different roles via YAML. Configs live in `config/` and use `$extends: defaults` for inheritance. See [config/README.md](config/README.md) for details.

### Phase Alternation

The agent alternates between two modes:

- **Strategic phase** — Reviews progress, reflects on what worked, updates the plan, creates the next batch of tasks
- **Tactical phase** — Executes tasks using domain-specific tools until all todos are complete

This loop continues until the job is done. Each phase boundary creates a snapshot for recovery.

### Workspace-Centric Memory

Long-term memory lives in files, not in the LLM context window:

- `workspace.md` — Injected into every LLM call, survives context compaction
- `plan.md` — Strategic plan, updated at phase boundaries
- `archive/` — Phase retrospectives and completed task lists

This means the agent can work on tasks that exceed any single context window.

## Debugging

- **Workspace files**: `workspace/job_<uuid>/` (workspace.md, todos.yaml, plan.md)
- **Checkpoints**: `workspace/checkpoints/job_<id>.db` (SQLite)
- **Logs**: `workspace/logs/job_<id>.log`
- **Phase snapshots**: `workspace/phase_snapshots/job_<id>/phase_<n>/`

```bash
# Phase recovery
python agent.py --job-id <id> --list-phases
python agent.py --job-id <id> --recover-phase 2 --resume

# Clean up
rm workspace/checkpoints/job_*.db workspace/logs/job_*.log
```

See [CLAUDE.md](CLAUDE.md) for full development documentation.

## License

Creative Commons Attribution 4.0 International License (CC BY 4.0). See [LICENSE.txt](LICENSE.txt).
