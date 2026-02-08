# Deployment: General-Purpose Agent Workforce

## Vision

Transform this project from a domain-specific requirement extraction system (creator/validator agents) into a **general-purpose agent workforce** that can:

- Accept any kind of job through a unified job queue
- Scale agent replicas on demand in Kubernetes
- Run unattended overnight without manual intervention
- Be deployed as a complete stack in a single namespace
- Support multiple LLM backends (self-hosted OSS, Anthropic, Gemini)

## Current State

### What Works
- Universal agent architecture with config-driven behavior
- Phase alternation model (strategic/tactical) is stable
- Workspace/todo/context management is battle-tested
- Databases: PostgreSQL (jobs), MongoDB (LLM conversation logs)
- Cockpit UI for monitoring jobs and viewing conversations
- Dockerfile.agent exists (multi-stage build, non-root user)
- Self-hosted OSS model on A100 (~100 tokens/sec)

### What Doesn't Work for Deployment
- **Config rigidity**: Creator/Validator configs are domain-specific (requirement extraction, Neo4j validation)
- **Tool organization**: Flat list of tools, hard to enable/disable capabilities
- **Polling model**: Each agent type polls a specific table with specific status fields
- **Workspace paths**: Local paths break when containerized (volume mounts, permissions)
- **Job submission**: No proper UI for creating jobs, tied to document processing
- **No compose for full stack**: Dev compose only runs databases, agents run locally
- **Cockpit incomplete**: Missing login/auth, no job submission page

## Goals

### Phase 1: Toolkit Architecture
- [ ] Refactor tools into composable toolkits
- [ ] Create generic agent config that assembles toolkits
- [ ] Make Neo4j, S3, etc. optional via toolkit selection

### Phase 2: Cockpit Expansion
- [ ] Add login/authentication (salvage from advanced-llm-chat)
- [x] Add job submission page
- [x] Drop Streamlit dashboard entirely
- [x] Cockpit becomes the single UI for everything

### Phase 3: Containerized Full Stack
- [ ] Docker Compose that runs the entire stack (databases + agents + cockpit)
- [ ] Fix workspace volume handling for containerized agents
- [ ] Document image purposes and configuration options
- [ ] Provide this as input for manual K8s manifest creation

### Phase 4: Generic Agent Pool
- [ ] Unify job queue: single `jobs` table, agents pick up any pending job
- [ ] Job metadata determines toolkits/behavior dynamically
- [ ] Remove creator/validator specific code paths

### Phase 5: Scalability
- [ ] Stateless agents that can be scaled horizontally
- [ ] Proper job locking (SELECT FOR UPDATE SKIP LOCKED)
- [ ] Health checks and graceful shutdown
- [ ] Shared workspace storage (Longhorn RWX)

## Architecture Changes

### Job Queue Redesign

**Current Model:**
```
jobs table (creator polls) → requirements table (validator polls) → Neo4j
```

**Target Model:**
```
jobs table (any agent polls) → job.type determines behavior → output varies
```

Job schema changes (implemented):
```sql
-- Job configuration columns (added)
ALTER TABLE jobs ADD COLUMN config_name VARCHAR(100) DEFAULT 'default';
ALTER TABLE jobs ADD COLUMN config_override JSONB;  -- Runtime config tweaks
ALTER TABLE jobs ADD COLUMN assigned_agent_id UUID REFERENCES agents(id);
-- Kept: creator_status, validator_status (useful for pipeline tracking)
```

### Toolkit Architecture

**Problem:** Current tool organization is a flat list per config. Adding/removing capabilities requires editing config files and understanding which tools go together.

**Solution:** Group tools into **toolkits** - coherent bundles of related functionality that can be composed.

#### Proposed Toolkits

| Toolkit | Tools | Purpose | Requires |
|---------|-------|---------|----------|
| `workspace` | read_file, write_file, edit_file, list_files, delete_file, search_files, move_file, copy_file | File operations in job workspace | - |
| `todo` | next_phase_todos, todo_complete, todo_rewind, mark_complete, job_complete | Task management and phase control | - |
| `citation` | cite_document, cite_web, list_sources, get_citation, list_citations | Source tracking and citations | PostgreSQL |
| `neo4j` | execute_cypher_query, get_database_schema, validate_schema_compliance | Graph database operations | Neo4j |
| `web` | web_search, fetch_url | Internet research | TAVILY_API_KEY |
| `document` | extract_document_text, get_document_info | PDF/document processing | - |
| `s3` | s3_upload, s3_download, s3_list, s3_delete | Cloud storage | S3 credentials |
| `dev` | run_command, compile_code, run_tests | Code execution (sandboxed) | Container runtime |
| `requirements` | list_requirements, get_requirement, add_requirement, edit_requirement | Requirement management | PostgreSQL |

#### Config Structure

**Current:** `configs/creator/config.json`
```json
{
  "tools": {
    "workspace": ["read_file", "write_file", ...],
    "domain": ["extract_document_text", "web_search", ...]
  }
}
```

**Target:** `configs/generic/config.json`
```json
{
  "toolkits": ["workspace", "todo", "citation", "web", "document"],
  "toolkit_config": {
    "dev": {
      "sandbox": "docker",
      "allowed_commands": ["python", "pytest", "npm"]
    }
  }
}
```

#### Job-Level Toolkit Override

Jobs can request additional toolkits or disable defaults:
```json
{
  "description": "Analyze this codebase and write tests",
  "toolkits": {
    "add": ["dev"],
    "remove": ["web"]
  }
}
```

#### Implementation

```
src/tools/
  toolkits/
    __init__.py         # Toolkit registry
    workspace.py        # Workspace toolkit definition
    neo4j.py            # Neo4j toolkit definition
    citation.py         # Citation toolkit definition
    ...
  registry.py           # Updated to load toolkits
```

Each toolkit is a class:
```python
class Neo4jToolkit(Toolkit):
    name = "neo4j"
    requires = ["NEO4J_URI"]  # Required env vars

    def get_tools(self, context: ToolContext) -> list[Tool]:
        return [
            execute_cypher_query,
            get_database_schema,
            validate_schema_compliance
        ]

    def validate_config(self) -> bool:
        """Check if required connections/credentials exist."""
        return bool(os.getenv("NEO4J_URI"))
```

### Cockpit Expansion

**Current State:** Cockpit has job list, job creation, agent management, conversation viewer, graph visualization.
- No authentication (deferred)
- Job submission UI ✅
- Streamlit dashboard removed ✅

**Target:** Cockpit is now the single UI for the entire system.

#### Features to Add

1. **Authentication** (salvage from advanced-llm-chat)
   - Login page
   - User sessions
   - Role-based access (admin, user, viewer)

2. **Job Submission**
   - Form to create new jobs
   - Prompt input with markdown preview
   - Toolkit selection (checkboxes)
   - File upload for document jobs
   - LLM backend selection (if multiple configured)

3. **Job Management**
   - Cancel running jobs
   - Retry failed jobs
   - Clone job with modifications
   - Bulk operations

4. **Enhanced Monitoring**
   - Real-time job status updates (WebSocket)
   - Token usage tracking per job
   - Cost estimation (for paid APIs)

#### Components to Salvage from advanced-llm-chat
- `auth/` - Login components, JWT handling
- `services/auth.service.ts` - Authentication service
- `guards/auth.guard.ts` - Route protection
- Form components for chat input (adapt for job submission)

### LLM Backend Support

**Implemented:** Multiple providers supported via `provider` field in LLM config.

#### Supported Backends

| Backend | Status | Notes |
|---------|--------|-------|
| Self-hosted OSS (vLLM) | ✅ Supported | A100 @ ~100 tok/s, via OpenAI-compatible API |
| Anthropic | ✅ Supported | Claude models via langchain-anthropic |
| Google Gemini | ✅ Supported | Via langchain-google-genai |
| OpenAI | ✅ Supported | Default, OpenAI-compatible APIs |

#### Provider Auto-Detection

Provider is auto-detected from model name prefix:
- `claude-*` → anthropic
- `gemini-*` → google
- `gpt-*`, `openai/*`, others → openai (default)

Or explicitly set via `provider` field.

#### Config Structure (Implemented)

```yaml
# config/my_agent.yaml
llm:
  model: claude-sonnet-4-20250514
  provider: anthropic  # Optional, auto-detected from model name
  temperature: 0.0
  timeout: 600
  max_retries: 3
```

#### Environment Variables

| Provider | API Key Variable |
|----------|------------------|
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| Google | `GOOGLE_API_KEY` |

#### Job-Level LLM Selection

Jobs can override LLM via `config_override`:
```json
{
  "description": "Complex reasoning task...",
  "config_override": {
    "llm": {
      "provider": "anthropic",
      "model": "claude-sonnet-4-20250514"
    }
  }
}
```

#### Cost Controls

For expensive backends (Anthropic):
- Per-job token budget
- Daily/monthly spending limits
- Automatic fallback to local if budget exceeded
- Cost tracking in MongoDB logs

### Workspace Volumes

**Problem:** Current workspace structure assumes local filesystem access.

**Solution:**
```yaml
volumes:
  workspace-data:
    driver: local  # In K8s: Longhorn RWX PVC

services:
  agent:
    volumes:
      - workspace-data:/app/workspace
    environment:
      WORKSPACE_ROOT: /app/workspace
```

Each job gets: `/app/workspace/job_<uuid>/`

Checkpoints: `/app/workspace/checkpoints/job_<uuid>.db`

## Docker Compose

The production compose file is [`docker-compose.yaml`](../docker-compose.yaml) (uses pre-built GHCR images). For development, use [`docker-compose.dev.yaml`](../docker-compose.dev.yaml) (databases only, with exposed ports for local development).

Services in the production stack:
- **postgres** - PostgreSQL 15 (jobs, agents, requirements)
- **mongodb** - MongoDB 7 (LLM request logging, audit trail)
- **gitea** - Gitea 1.22 (Git server for agent workspaces)
- **orchestrator** - FastAPI backend (job management, agent coordination)
- **vpn-sidecar** - OpenFortiVPN + SOCKS proxy for institutional research access
- **agent** - Universal agent workers (defaults to 2 replicas via `AGENT_REPLICAS`)
- **cockpit** - Angular frontend (job management UI)

Exposed ports (production): orchestrator (8085), cockpit (4000), gitea (3000). Database ports are internal-only.

## Image Documentation

| Image | Purpose | Required | Key Env Vars |
|-------|---------|----------|--------------|
| `graphrag-agent` | Universal agent worker pool | Yes | `DATABASE_URL`, `MONGODB_URL`, `TOOLKITS`, `LLM_BASE_URL` |
| `cockpit-api` | REST API for job management, auth, monitoring | Yes | `DATABASE_URL`, `MONGODB_URL`, `JWT_SECRET` |
| `cockpit-frontend` | Angular UI: login, job submission, monitoring | Yes | `API_URL` |
| `postgres:15-alpine` | Job queue, user accounts | Yes | Standard Postgres vars |
| `mongo:7` | LLM conversation logging, audit trail | Yes | Standard Mongo vars |
| `neo4j:5.15-community` | Graph database (neo4j toolkit only) | No | `NEO4J_AUTH` |

### Agent Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `MONGODB_URL` | Yes | MongoDB connection string |
| `TOOLKITS` | No | Comma-separated list of toolkits to enable (default: workspace,todo) |
| `LLM_BASE_URL` | No | Base URL for OpenAI-compatible API (self-hosted) |
| `OPENAI_API_KEY` | Conditional | Required if using OpenAI or self-hosted |
| `ANTHROPIC_API_KEY` | No | Required if using Anthropic backend |
| `GOOGLE_API_KEY` | No | Required if using Google/Gemini backend |
| `NEO4J_URI` | No | Required if neo4j toolkit enabled |
| `AGENT_CONFIG` | No | Config directory name (default: generic) |

## Refactoring Tasks

### Phase 1: Toolkit Architecture
1. **Create toolkit base class** - `src/tools/toolkits/base.py`
2. **Refactor existing tools into toolkits** - workspace, todo, citation, neo4j, web, document, requirements
3. **Update tool registry** - Load tools from toolkits instead of flat lists
4. **Create generic config** - `configs/generic/config.json` using toolkit syntax
5. **Environment-based toolkit validation** - Skip toolkits if required env vars missing

### Phase 2: Cockpit Expansion
6. **Add authentication to cockpit-api** - JWT-based, salvage from advanced-llm-chat (deferred)
7. **Create login page in cockpit-frontend** - Route guards, token storage (deferred)
8. ~~**Build job submission page** - Form with prompt, toolkit selection, file upload~~ ✅
9. ~~**Add job management actions** - Cancel, retry, clone~~ ✅
10. ~~**Delete Streamlit dashboard** - `dashboard/` directory~~ ✅

### Phase 3: Database & Job Queue
11. ~~**Add job configuration columns** - config_name, config_override, assigned_agent_id~~ ✅
12. **Implement SELECT FOR UPDATE SKIP LOCKED** - Proper job claiming (deferred - manual assignment for now)
13. ~~**Add assigned_agent_id column** - Track which agent instance has the job~~ ✅
14. **Graceful shutdown handling** - Release claimed jobs on SIGTERM (deferred)

### Phase 4: Multi-LLM Backend
15. ~~**Create LLM backend abstraction** - Support OpenAI, Anthropic, Google APIs~~ ✅
16. ~~**Add backend configuration** - Multiple backends in config, job-level selection~~ ✅
17. **Cost tracking** - Log token usage and estimated cost per job (deferred)
18. **Budget controls** - Per-job limits, daily caps for expensive backends (deferred)

### Phase 5: Containerization
19. **Fix Dockerfile workspace paths** - Ensure `/app/workspace` works with volumes
20. **Write docker-compose.yaml** - Full stack with optional Neo4j profile
21. **Test full stack locally** - Verify agents can claim and complete jobs
22. **Document K8s translation** - How to convert compose to manifests

### Phase 6: Cleanup
23. **Remove creator/validator configs** - After generic is proven
24. **Remove domain-specific code paths** - Requirement extraction, validation logic
25. **Update CLAUDE.md** - Reflect new architecture
26. **Archive old documentation** - Move to docs/done/

## Migration Path

### Week 1: Foundation
1. Implement toolkit architecture (tasks 1-5)
2. Create generic config with basic toolkits
3. Test locally: agent can pick up and complete simple jobs

### Week 2: UI & Auth
4. Add authentication to Cockpit (tasks 6-7) - deferred
5. ~~Build job submission page (tasks 8-9)~~ ✅
6. ~~Delete Streamlit dashboard (task 10)~~ ✅

### Week 3: Production Ready
7. ~~Add job configuration schema (tasks 11, 13)~~ ✅
8. Write docker-compose.yaml (tasks 19-21)
9. Test full stack in containers locally

### Week 4: Deploy & Iterate
10. Deploy to K8s cluster
11. Run overnight jobs unattended
12. Add multi-LLM support based on needs (tasks 15-18)
13. Clean up legacy code (tasks 23-26)

## Kubernetes Notes (for manual manifest creation)

Based on the docker-compose, you'll create:
- **Namespace**: `graphrag` or similar
- **Deployments**: agent (scalable), cockpit-api, cockpit-frontend
- **StatefulSets**: postgres, neo4j, mongodb (or use external/managed)
- **Services**: ClusterIP for internal, LoadBalancer/Ingress for cockpit
- **PVCs**: Longhorn RWX for workspace, RWO for databases
- **ConfigMaps**: Non-sensitive config
- **Secrets**: Database passwords, API keys

The docker-compose serves as the source of truth for what containers need.
