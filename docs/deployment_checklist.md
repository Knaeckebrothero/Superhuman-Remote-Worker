---
tags:
  - cloud-infrastructure
  - configuration
  - orchestrator
aliases:
  - Containerization
  - Docker Compose
  - Kubernetes Deployment
related:
  - "[[datasources]]"
  - "[[debug_cockpit]]"
  - "[[deliverables]]"
  - "[[security_checklist]]"
  - "[[email_and_mobile]]"
---
# Deployment: General-Purpose Agent Workforce

## Vision

Transform this project from a domain-specific requirement extraction system (creator/validator agents) into a **general-purpose agent workforce** that can:

- Accept any kind of job through a unified job queue
- Scale agent replicas on demand in Kubernetes
- Run unattended overnight without manual intervention
- Be deployed as a complete stack in a single namespace
- Support multiple LLM backends (self-hosted OSS, Anthropic, Gemini)

## Deployment Modes

Three deployment tiers are supported (see [`docs/deployment.md`](deployment.md)):

| Tier | Target | Provisioning | Config |
|------|--------|-------------|--------|
| **Docker Compose** | Dev machines, small servers | Static workspace/agent pools via `DockerProvisioner` | `.env` file |
| **K8s single-cluster** | Local K3s, demos, small prod | Dynamic pods via `ContainerProvisioner` | `deployment-local/` Kustomize |
| **K8s multi-cluster** | Production | Dynamic pods + KubeVirt VMs + Fleet GitOps | `deployment/` + Vault/ESO |

See [`docs/docker_compose_mode.md`](docker_compose_mode.md) for the Docker Compose architecture.

## Current State

### What Works
- Universal agent architecture with config-driven behavior
- Phase alternation model (strategic/tactical) is stable
- Workspace/todo/context management is battle-tested
- Databases: PostgreSQL (jobs), MongoDB (LLM conversation logs)
- Cockpit UI for monitoring jobs and viewing conversations
- Dockerfile.agent exists (multi-stage build, non-root user)
- Self-hosted OSS model on A100 (~100 tokens/sec)

### What Has Been Resolved
- ~~Config rigidity~~ → Universal agent with YAML config inheritance (`$extends: defaults`)
- ~~Tool organization~~ → Tool categories in config, phase-specific filtering via `TOOL_REGISTRY`
- ~~Polling model~~ → Orchestrator assigns jobs to agents, agents poll orchestrator API
- ~~Workspace paths~~ → Shared PVC (`/workspace`) with Longhorn RWX in K8s, named volume in compose
- ~~Job submission~~ → Cockpit has full job creation UI + instruction builder chat
- ~~No compose for full stack~~ → Production compose with 20 services (databases, SSO, VPN, app, admin UIs)
- ~~Cockpit incomplete~~ → Keycloak SSO auth, job submission, agent management, conversation viewer

### Remaining Deployment Gaps
- **Cost tracking** — Token usage logged in MongoDB but no per-job cost estimation UI
- **Budget controls** — No automatic fallback when spending limits are exceeded
- **SELECT FOR UPDATE SKIP LOCKED** — Manual job assignment for now (auto-assign via orchestrator)

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
| `graph` | execute_cypher_query, get_database_schema | Neo4j graph operations | Neo4j datasource |
| `sql` | sql_query, sql_schema, sql_execute | PostgreSQL operations | PostgreSQL datasource |
| `mongodb` | mongo_query, mongo_aggregate, mongo_schema, mongo_insert, mongo_update | MongoDB operations | MongoDB datasource |
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
# Database tools are now injected by the orchestrator based on attached datasources.
# See docs/datasources.md for the multi-stage config pipeline.
# Example: if a Neo4j datasource is attached, the orchestrator injects:
#   graph: [execute_cypher_query, get_database_schema]
# If a PostgreSQL datasource is attached:
#   sql: [sql_query, sql_schema, sql_execute]
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

Each job gets a dedicated workspace container. The agent's home directory (`/home/agent-host`) IS the workspace.

Checkpoints: `/app/workspace/checkpoints/job_<uuid>.db`

## Docker Compose

Three compose files cover different deployment scenarios:

| File | Purpose | Custom images |
|------|---------|---------------|
| [`docker-compose.yaml`](../docker-compose.yaml) | **Production** — full stack, pre-built GHCR images | Pulled from `ghcr.io/knaeckebrothero/superhuman-remote-worker-*` |
| [`docker-compose.local.yaml`](../docker-compose.local.yaml) | **Local build** — same stack, builds from source | Built locally via `podman-compose build` |
| [`docker-compose.dev.yaml`](../docker-compose.dev.yaml) | **Development** — infrastructure only, apps run locally | Only MCP + VPN built locally |

### Services in the production/local-build stack (20 services):

**Databases:**
- **postgres** — PostgreSQL 15 (jobs, agents + Keycloak/Nextcloud DBs via `init_sso_dbs.sh`)
- **postgres-vector** — pgvector/pg15 (citations, embeddings, knowledge index)
- **mongodb** — MongoDB 7 (LLM request logging, audit trail)
- **neo4j** — Neo4j 5 with APOC (graph datasource for agents)

**Identity & Storage:**
- **keycloak** — Keycloak 26.2 SSO (OIDC for cockpit, Gitea, Nextcloud, pgAdmin)
- **gitea** — Gitea 1.22 (Git server, Keycloak OIDC login, admin auto-bootstrap in K8s)
- **nextcloud** — Nextcloud 31 (WebDAV cloud storage, Keycloak OIDC)

**VPN Sidecars:**
- **vpn-cluster** — OpenFortiVPN + port forward to GPU cluster for LLM inference
- **vpn-research** — OpenFortiVPN + SOCKS5 proxy for institutional research access
- **vpn-workstation** — OpenFortiVPN + port forward to AI workstation (embeddings, vision)

**Application:**
- **orchestrator** — FastAPI backend (job management, agent coordination, SSO, notifications)
- **agent** — Universal agent workers (defaults to 2 replicas via `AGENT_REPLICAS`)
- **mcp** — MCP server for Claude Code integration (port 8055)
- **cockpit** — Angular SSR frontend (job management UI)

**Optional Infrastructure:**
- **nats** — NATS JetStream messaging (required for VM lifecycle)
- **minio** — S3-compatible object storage (VM snapshots, IDE sessions)

**Admin UIs & Utilities:**
- **pgadmin** — PostgreSQL admin UI
- **mongo-express** — MongoDB admin UI
- **codex-proxy** — CLIProxyAPI OAuth proxy (codex/* models via ChatGPT subscription)
- **dozzle** — Container log viewer

### Exposed ports (production/local-build):

| Service | Port |
|---------|------|
| Orchestrator API | 8085 |
| Cockpit (Web UI) | 4000 |
| Keycloak SSO | 8180 |
| Gitea | 3000 |
| MCP Server | 8055 |
| Nextcloud | 8800 |
| pgAdmin | 5050 |
| Mongo Express | 8081 |
| MinIO API / Console | 9000 / 9001 |
| Codex OAuth Proxy | 8317 |
| Dozzle | 9999 |

Database ports (postgres, vector, mongodb, neo4j) are internal-only in production. The dev compose exposes them for local debugging.

### Prerequisites

```bash
cp .env.example .env                                          # Configure API keys, VPN, SSO, OIDC secrets
```

## Image Documentation

| Image | Source | Purpose |
|-------|--------|---------|
| `superhuman-remote-worker-agent` | `docker/Dockerfile.agent` | Universal agent worker pool |
| `superhuman-remote-worker-orchestrator` | `docker/Dockerfile.orchestrator` | FastAPI backend API |
| `superhuman-remote-worker-cockpit` | `docker/Dockerfile.cockpit` | Angular SSR frontend |
| `superhuman-remote-worker-mcp` | `docker/Dockerfile.mcp` | MCP server (Claude Code) |
| `superhuman-remote-worker-vpn` | `docker/vpn/Dockerfile` | OpenFortiVPN + microsocks sidecar |
| `keycloak:26.2` | Official (Quay) | SSO identity provider |
| `postgres:15` | Official | App database |
| `pgvector/pgvector:pg15` | Official | Vector database (citations, embeddings) |
| `mongo:7` | Official | LLM audit trail |
| `neo4j:5` | Official | Graph database |
| `gitea:1.22-rootless` | Official | Git server |
| `nextcloud:31-apache` | Official | Cloud storage / WebDAV |
| `nats:2.10-alpine` | Official | JetStream messaging |
| `minio:latest` | Official (Quay) | S3 object storage |

See `.env.example` for the full list of environment variables per service.

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

## Related

- [[datasources]] - Datasource connector system for external databases
- [[debug_cockpit]] - Angular frontend deployed as part of the stack
- [[deliverables]] - Job delivery pipeline within the deployed infrastructure
- [[security_checklist]] - Security considerations for production deployment
- [[email_and_mobile]] - Notification system for autonomous operation
