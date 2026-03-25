# Superhuman Remote Worker

A self-improving AI agent system. Three specialized agents form a continuous innovation cycle: one explores ideas, one tears them apart, one builds the survivors. The system gets better on its own.

Built on LangGraph with a config-driven architecture. Same codebase, different YAML configs, different roles.

## The Innovation Cycle

The typical human-AI workflow looks like this: you have an idea, you dump it on the AI, the AI builds it, then you spend forever refactoring because your inner perfectionist won't let you merge something that works but isn't elegant. Repeat.

This system replaces that loop with three agents that run it continuously:

```
         ┌──────────────────────────────────────────────────┐
         │                                                  │
         ▼                                                  │
   ┌──────────┐     writes ideas     ┌──────────┐          │
   │  SCHOLAR │ ──────────────────► │  CRITIC  │          │
   │          │                      │          │          │
   │ Explores │     reviews &        │ Reviews  │          │
   │ the web, │     rates them       │ rejects  │          │
   │ codebase,│ ◄────────────────── │ or       │          │
   │ papers,  │  feedback/issues     │ approves │          │
   │ logs     │                      └────┬─────┘          │
   └──────────┘                           │                │
                              approved ideas               │
                              & issue fixes                │
                                          │                │
                                          ▼                │
                                   ┌──────────┐            │
                                   │DEVELOPER │            │
                                   │          │            │
                                   │ Builds,  │ ───────────┘
                                   │ tests,   │  code changes feed
                                   │ ships    │  back into the cycle
                                   │ PRs      │
                                   └──────────┘
```

**Scholar** — The idea factory. Continuously scans the web, digs through the codebase, analyzes past agent runs, and runs experiments. Produces a high volume of idea artifacts. Doesn't self-filter — that's the Critic's job. Also takes inspiration from issues and feedback the Critic raises about the existing codebase.

**Critic** — The quality gate. Reads Scholar's proposals, reviews code diffs, audits the codebase for tech debt, runs tests. Everything gets a verdict: APPROVED, REJECTED, or NEEDS INVESTIGATION. Harsh, direct, evidence-based. Every claim cites file:line. No approval with failing tests.

**Developer** — The PR factory. Picks up approved ideas and Critic-identified issues, delegates implementation to Claude Code sessions, verifies results via git, and ships focused PRs. One feature per PR, one bug fix per PR. Throughput over perfection.

The cycle repeats. Scholar sees the new code and finds more to explore. Critic reviews what Developer shipped. Developer picks up the next batch. The system improves because the loop never stops.

### Why This Works

The problem with human-in-the-loop AI development isn't the AI — it's the human bottleneck. You can only review so fast, you get attached to your ideas, and your perfectionism stalls shipping.

This system applies evolutionary pressure:
- **Random mutation** — Scholar generates volume, most of it mediocre, some of it brilliant
- **Selection pressure** — Critic filters ruthlessly, only the good stuff survives
- **Implementation** — Developer builds the survivors, fast and focused

No human bottleneck in the loop. You set the direction, the system iterates.

### The General Secretary

The default config (`config/defaults.yaml`) is the **General Secretary** — a jack-of-all-trades with all tools enabled and no specialization. This is the agent you talk to directly for ad-hoc tasks, the one that doesn't fit neatly into the innovation cycle. It's the escape hatch for "just do this thing."

## What It Can Do

Beyond the innovation cycle, each agent is a general-purpose LangGraph worker that can:
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
cp docker/keycloak/realm-export.json.example docker/keycloak/realm-export.json
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
- `KEYCLOAK_ADMIN_USER` / `KEYCLOAK_ADMIN_PASSWORD` — SSO admin (default: admin/admin)
- `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` — Email via ProtonMail Bridge or other SMTP
- `LOG_LEVEL` — DEBUG, INFO, WARNING, ERROR (default: INFO)

Edit `docker/keycloak/realm-export.json` for OIDC client secrets if needed (see the example file for details).

### 3. Start All Services

```bash
podman-compose up -d
```

This starts:
- **PostgreSQL** — Job tracking and data storage (+ SSO databases for Keycloak/Nextcloud)
- **PostgreSQL (Vector)** — Citations, embeddings, knowledge index (pgvector)
- **MongoDB** — LLM request logging and audit trail
- **Neo4j** — Graph database for agent datasources
- **Keycloak** — SSO identity provider (OIDC for all services)
- **Gitea** — Git server for agent workspace repositories (Keycloak OIDC login)
- **Nextcloud** — Cloud storage / WebDAV datasource
- **VPN Sidecars** — Route LLM and research traffic through university network
- **Orchestrator** — Backend API for job management and agent coordination
- **Agent** — Worker instances (defaults to 2 replicas via `AGENT_REPLICAS`)
- **MCP Server** — Claude Code integration (port 8055)
- **Cockpit** — Web UI for job management and monitoring
- **NATS** — Messaging for VM lifecycle (optional)
- **MinIO** — S3-compatible object storage for snapshots and IDE sessions (optional)

### 4. Access Services

| Service | URL |
|---------|-----|
| Cockpit (Web UI) | http://localhost:4000 |
| Keycloak SSO | http://localhost:8180 |
| Orchestrator API | http://localhost:8085 |
| Gitea | http://localhost:3000 |
| Nextcloud | http://localhost:8800 |
| MCP Server | http://localhost:8055 |
| pgAdmin | http://localhost:5050 |
| MinIO Console | http://localhost:9001 |
| Dozzle (logs) | http://localhost:9999 |

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

For local builds (no GHCR access), use `docker-compose.local.yaml` instead — it builds all custom images from source.

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
         ┌─────────────┬───────┴───────┬─────────────┐
         │             │               │             │
         ▼             ▼               ▼             ▼
   ┌──────────┐ ┌──────────┐  ┌───────────┐ ┌────────────┐
   │ SCHOLAR  │ │  CRITIC  │  │ DEVELOPER │ │ GENERAL    │
   │          │ │          │  │           │ │ SECRETARY  │
   │ R&D,     │ │ Review,  │  │ Claude    │ │            │
   │ ideas,   │ │ quality  │  │ Code PRs  │ │ All tools, │
   │ research │ │ gating   │  │           │ │ ad-hoc     │
   └──────────┘ └──────────┘  └───────────┘ └────────────┘
         │             │               │
         ▼             ▼               ▼
   ┌───────────┐  ┌───────────┐  ┌─────────────────┐
   │ PostgreSQL│  │  MongoDB  │  │  Datasources    │
   │ (system)  │  │ (logging) │  │  (per-job)      │
   └───────────┘  └───────────┘  └─────────────────┘
```

### Expert Lineup

| Expert | Config | Role |
|--------|--------|------|
| **General Secretary** | `config/defaults.yaml` | Default — all tools, no specialization, direct human interaction |
| **Scholar** | `config/experts/scholar/` | Continuous R&D exploration, idea generation, web research |
| **Critic** | `config/experts/critic/` | Code review, proposal review, codebase audits, test execution |
| **Developer** | `config/experts/developer/` | Claude Code delegation, PR factory, implementation |

All experts share the same universal agent codebase. Configs live in `config/` and use `$extends: defaults` for inheritance. See [config/README.md](config/README.md) for details.

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
