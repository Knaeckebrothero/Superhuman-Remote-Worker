# Hive Architecture: Stateless Agent Workers

## Problem

The current architecture ties agent configuration to the agent instance. Each agent pod starts with `--config researcher` and can only process jobs matching that config. Agents self-manage their identity and lifecycle, and the orchestrator has limited control over the workforce.

Problems:
1. **Scaling friction**: Separate deployments per expert type, each needing minimum replicas
2. **No workforce management**: Orchestrator can't track available agents, auto-assign jobs, or redistribute work
3. **Config coupling**: Agent loads config at startup, ignores per-job `config_name` from orchestrator
4. **No config selection UX**: Cockpit has no way to pick an expert config — users must upload raw YAML

## Architecture Overview

Agents become **stateless workers**. They have no fixed identity or configuration. They register with the orchestrator, send health pings, and execute whatever job the orchestrator assigns — with whatever config the orchestrator provides.

```
┌─────────────────────────────────────────────────────────┐
│                     Orchestrator                        │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────────┐ │
│  │ Jobs DB  │  │ Agents DB│  │ Expert Config Library │ │
│  │          │  │          │  │  (config/experts/)    │ │
│  │ job_id   │  │ agent_id │  │                       │ │
│  │ config   │  │ hostname │  │  researcher/          │ │
│  │ status   │  │ status   │  │  doc_writer/          │ │
│  │ agent_id │  │ last_ping│  │  ...                  │ │
│  └──────────┘  └──────────┘  └───────────────────────┘ │
│                                                         │
│  Responsibilities:                                      │
│  - Register agents, track health                        │
│  - Auto-assign jobs to idle agents                      │
│  - Send start/stop/resume to agents                     │
│  - Serve expert config list to cockpit UI               │
│  - Store full config override per job                   │
└─────────────┬───────────────────────────┬───────────────┘
              │ start/stop/resume         │ register/ping
              ▼                           ▼
┌─────────────────────┐   ┌─────────────────────┐
│   Agent Worker 1    │   │   Agent Worker 2    │
│                     │   │                     │
│  - No fixed config  │   │  - No fixed config  │
│  - defaults.yaml    │   │  - defaults.yaml    │
│    as base only     │   │    as base only     │
│  - Receives config  │   │  - Receives config  │
│    overrides per    │   │    overrides per    │
│    job from orch.   │   │    job from orch.   │
│  - Workspace on     │   │  - Workspace on     │
│    mounted volume   │   │    mounted volume   │
│  - LLM on ext. GPU  │   │  - LLM on ext. GPU  │
└─────────────────────┘   └─────────────────────┘
```

### Agent Lifecycle

```
Agent pod starts
    │
    ├─→ POST /api/agents/register  (retry every 60s until success)
    │       → receives agent_id (UUID from orchestrator)
    │
    ├─→ Health ping loop: POST /api/agents/{id}/ping  (every 60s)
    │       → sends: { status: "idle" | "working", job_id: ... }
    │
    ├─→ Waits for orchestrator commands:
    │
    │   POST /job/start  { job_id, config_override, documents... }
    │       → Load defaults.yaml + apply config_override
    │       → Create LLMs, tools, workspace, graph
    │       → Execute job
    │       → Report completion
    │
    │   POST /job/stop
    │       → Create phase snapshot
    │       → Save state
    │       → Return to idle
    │
    │   POST /job/resume  { job_id, config_override, phase... }
    │       → Load config, restore checkpoint
    │       → Continue execution
    │
    └─→ Pod termination: orchestrator notices missing pings after timeout
```

### Job Lifecycle

```
User creates job in Cockpit UI
    │
    ├─→ Selects expert config (or custom) → config stored with job
    ├─→ Uploads documents
    ├─→ POST /api/jobs → orchestrator stores job + config in DB
    │
    ├─→ Orchestrator picks idle agent → sends start command with config
    │
    ├─→ Agent processes job (strategic/tactical phase loop)
    │       Agent sends status updates to orchestrator
    │
    ├─→ Job completes → agent reports done → goes back to idle
    │
    └─→ OR: User stops job → orchestrator sends stop → agent snapshots → idle
             User resumes → orchestrator picks idle agent → sends resume
```

## Changes

### 1. Agent Registration System

#### Orchestrator side — `orchestrator/main.py`

**New endpoints:**

`POST /api/agents/register`
- Called by agent on startup (retries every 60s on failure)
- Orchestrator generates UUID, stores agent record
- Returns `{ agent_id: "uuid", config: { orchestrator_url, ... } }`

```json
// Request
{ "hostname": "agent-pod-7f8b9", "port": 8001 }

// Response
{ "agent_id": "a1b2c3d4-...", "heartbeat_interval": 60 }
```

`POST /api/agents/{agent_id}/ping`
- Health ping every 60 seconds
- Agent sends current status

```json
// Request
{ "status": "idle" | "working", "job_id": "..." | null }
```

`DELETE /api/agents/{agent_id}`
- Agent calls on graceful shutdown (optional)

**Cleanup logic:**
- If no ping received for 3 minutes → mark agent as `stale`
- If no ping for 24 hours → delete agent record
- If agent was working when it went stale → mark job as `interrupted` (available for reassignment)

#### Orchestrator database — `orchestrator/database/schema.sql`

Update or replace current agents table:

```sql
CREATE TABLE agents (
    id UUID PRIMARY KEY,
    hostname VARCHAR(255) NOT NULL,
    port INTEGER NOT NULL DEFAULT 8001,
    status VARCHAR(20) NOT NULL DEFAULT 'idle',  -- idle, working, stale
    current_job_id UUID REFERENCES jobs(id),
    registered_at TIMESTAMPTZ DEFAULT NOW(),
    last_ping_at TIMESTAMPTZ DEFAULT NOW()
);
```

Note: The current agents table already exists with fields like `agent_id`, `config_name`, `pod_ip`, `pod_port`, `last_heartbeat`. This is a refinement — remove `config_name` (agents are generic), rename fields for clarity.

#### Agent side — `src/api/app.py`

**On startup (`lifespan()`):**
- Agent no longer needs `--config` to start (optional, for CLI backward compat only)
- Starts registration loop: POST to orchestrator until registered
- Starts health ping background task (every 60s)

**On shutdown:**
- If working: create phase snapshot, save state
- Send DELETE to orchestrator (best-effort)

### 2. Per-Job Config Loading — `src/agent.py`

Replace agent-level config with per-job config loading.

**Current:** Config loaded once at `from_config()`, LLMs created at `initialize()`, both reused for all jobs.

**New:** `process_job()` receives config override dict, applies on top of `defaults.yaml`, creates LLMs per-job.

```python
# Current
agent = UniversalAgent.from_config("researcher")
await agent.initialize()
await agent.process_job(job_id, metadata)

# New
agent = UniversalAgent()
await agent.initialize()  # DB connections only
await agent.process_job(job_id, metadata, config_override={...})
```

#### Specific changes:

**`__init__()`:**
- No `config` parameter required
- Loads `defaults.yaml` as base config (always available locally)
- Database connections initialized here (shared across jobs)

**`from_config()` → keep for CLI backward compat:**
- `python agent.py --config researcher` still works
- Loads the named config, stores as `_cli_config_override`
- Applied in `process_job()` if no per-job override provided

**`initialize()`:**
- Only database connections
- No LLM creation (moves to per-job)

**`process_job(job_id, metadata, config_override=None)`:**
1. Start from `defaults.yaml` (base config)
2. Apply `config_override` dict on top (deep merge)
3. Create phase LLMs from merged config
4. Setup workspace, tools, graph (already per-job)
5. Execute

**LLM caching:**
- Cache LLM instances by effective settings (model + temperature + reasoning_level + base_url)
- If consecutive jobs use the same model, reuse the instance
- `_llm_cache: Dict[str, BaseChatModel]`

### 3. Expert Config Library — `config/experts/`

Preconfigured expert configs. Each is a directory with `config.yaml` and optional custom prompts:

```
config/experts/
    researcher/
        config.yaml          # Overrides for academic research
        instructions.md      # Custom system instructions
    doc_writer/
        config.yaml          # Migrated from config/doc_writer.yaml
        instructions.md      # Migrated from config/prompts/doc_writer_instructions.md
```

Each `config.yaml` uses `$extends: defaults` for inheritance. The directory format bundles custom prompts alongside the config (existing `PromptResolver` already supports this via `_deployment_dir`).

**How experts work with the new architecture:**
- Expert configs live in `config/experts/` on the **orchestrator** (shared volume or bundled in container)
- Orchestrator loads expert configs and serves them via API
- When user selects an expert in the UI → orchestrator reads the expert's config, stores the override data with the job
- When assigning the job → orchestrator sends the config override to the agent
- Agent applies it on top of `defaults.yaml`

Note: Expert prompt files (instructions.md, strategic.txt, etc.) need to be accessible to the agent. In docker-compose, the `config/` directory is a shared volume. In K8s, expert configs are bundled in the agent container image (they're part of the codebase).

### 4. Expert Discovery Endpoint — `orchestrator/main.py`

`GET /api/experts` — List available expert configurations for the cockpit UI.

```json
{
  "experts": [
    {
      "id": "defaults",
      "display_name": "Generalist Agent",
      "description": "General-purpose agent with all tools available.",
      "is_default": true
    },
    {
      "id": "researcher",
      "display_name": "Academic Researcher",
      "description": "Conducts systematic literature reviews with web search, paper retrieval, and citation management.",
      "tools_summary": ["web_search", "extract_webpage", "search_papers", "cite_web"],
      "is_default": false
    },
    {
      "id": "doc_writer",
      "display_name": "Documentation Writer",
      "description": "Writes comprehensive project documentation with source analysis.",
      "tools_summary": ["web_search", "chunk_document", "cite_document"],
      "is_default": false
    }
  ]
}
```

Implementation: Scan `config/experts/` at startup, parse each `config.yaml` to extract metadata. Cache the result.

### 5. Orchestrator Job Assignment — `orchestrator/main.py`

Update `POST /api/jobs` and job assignment flow:

**Job creation:**
- UI sends `expert_id` (e.g. "researcher") or `config_override` (custom YAML) or neither (defaults)
- If `expert_id` provided: orchestrator loads expert config, stores as `config_override` in job record
- If `config_override` provided: store directly
- If neither: job uses defaults (no override)

**Job assignment (auto or manual):**
- Orchestrator picks an idle agent
- Sends `POST /job/start` to agent with:
  - `job_id`
  - `config_override` (the merged expert config / custom override from job record)
  - `documents` (upload references)
- Agent applies override on top of its local `defaults.yaml` and runs

**Job stop:**
- Orchestrator sends `POST /job/stop` to agent
- Agent creates phase snapshot, returns to idle
- Orchestrator marks job as `paused`, clears `assigned_agent_id`

**Job resume:**
- Orchestrator picks any idle agent (may be different pod than before)
- Sends `POST /job/resume` with job_id + config_override
- Agent loads checkpoint, applies config, continues

### 6. Agent API Updates — `src/api/app.py`

**Startup (`lifespan()`):**
- Create `UniversalAgent()` with no config (just defaults)
- Initialize (DB connections only)
- Start registration loop (background task)
- Start health ping loop (background task)

**`POST /job/start`:**
- Receive `job_id` + `config_override` dict
- Pass `config_override` to `agent.process_job()`
- No more `config_name` — just raw config data

**`POST /job/stop` (new):**
- Signal agent to stop current job gracefully
- Agent creates phase snapshot before stopping

**`POST /job/resume`:**
- Receive `job_id` + `config_override` dict
- Pass to `agent.process_job(..., resume=True)`

**Health/status endpoints:**
- `/health` and `/status` report agent_id (UUID from orchestrator) and current status
- When idle: `{ agent_id: "uuid", status: "idle", config: "defaults" }`
- When working: `{ agent_id: "uuid", status: "working", job_id: "...", config: "researcher" }`

### 7. Cockpit UI — Expert Selector

**`job-create.component.ts`:**
- Fetch experts from `GET /api/experts` on component init
- Display as selectable cards above the description field
- Default selection: "Generalist Agent"
- "Custom" option reveals existing YAML upload field
- Send selected `expert_id` in `JobCreateRequest`

**`api.service.ts`:** Add `getExperts()` method.

**`api.model.ts`:** Add `Expert` interface, update `JobCreateRequest` with `expert_id` field.

### 8. Config Schema Update — `config/schema.json`

Add `description` as optional field to the config schema. This allows expert configs to self-describe for the discovery endpoint.

### 9. Migrate doc_writer — `config/doc_writer.yaml`

- Move `config/doc_writer.yaml` → `config/experts/doc_writer/config.yaml`
- Move `config/prompts/doc_writer_instructions.md` → `config/experts/doc_writer/instructions.md`
- Delete old files

### 10. Researcher Expert — `config/experts/researcher/`

First new expert: academic researcher for systematic literature reviews.

**`config.yaml`:**
```yaml
$extends: defaults

agent_id: researcher
display_name: Academic Researcher
description: >
  Conducts systematic literature reviews with web search,
  academic paper retrieval, and citation management.

llm:
  model: openai/gpt-oss-120b
  multimodal: false

workspace:
  structure:
    - archive/
    - tools/
    - sources/
    - drafts/
    - notes/
  instructions_template: instructions.md
  max_read_words: 50000

tools:
  workspace: [read_file, write_file, edit_file, list_files, delete_file, search_files, move_file, copy_file, get_workspace_summary, get_document_info]
  core: [next_phase_todos, todo_complete, todo_list, todo_rewind, mark_complete, job_complete]
  document: [chunk_document]
  research: [web_search, extract_webpage, crawl_website, map_website, search_papers, download_paper, get_paper_info, browse_website, research_topic]
  citation: [cite_document, cite_web, list_sources, get_citation, list_citations, edit_citation]
  graph: []
  git: [git_log, git_show, git_diff, git_status, git_tags]

research:
  web_search_enabled: true
```

**`instructions.md`:**
- Role definition: autonomous academic researcher
- SLR methodology (search strategy → screening → extraction → synthesis)
- Quality standards: proper citations, academic writing, reproducible search
- Phase guidance for strategic (plan search) vs tactical (execute and write)

## File Summary

| File | Action | Description |
|------|--------|-------------|
| `config/experts/researcher/config.yaml` | Create | Researcher expert config |
| `config/experts/researcher/instructions.md` | Create | Researcher system instructions |
| `config/experts/doc_writer/config.yaml` | Create | Migrate from `config/doc_writer.yaml` |
| `config/experts/doc_writer/instructions.md` | Create | Migrate from doc_writer_instructions.md |
| `config/doc_writer.yaml` | Delete | Migrated to experts/ |
| `config/schema.json` | Modify | Add `description` field |
| `src/agent.py` | Modify | Per-job config loading, LLM caching |
| `src/core/loader.py` | Modify | Add `config/experts/` to resolution path |
| `src/api/app.py` | Modify | Registration loop, health pings, per-job config |
| `orchestrator/main.py` | Modify | Agent registration, expert discovery, job assignment |
| `orchestrator/database/schema.sql` | Modify | Update agents table |
| `orchestrator/database/postgres.py` | Modify | Agent CRUD operations |
| `cockpit/src/app/components/job-create/` | Modify | Expert selector UI |
| `cockpit/src/app/core/services/api.service.ts` | Modify | Add `getExperts()` |
| `cockpit/src/app/core/models/api.model.ts` | Modify | Add `Expert` interface |

## Implementation Order

1. **Expert config library** — Create `config/experts/`, researcher config, migrate doc_writer
2. **Config resolution** — Update `resolve_config_path()` for experts path
3. **Per-job config loading** — Refactor `UniversalAgent` to load config in `process_job()`
4. **Agent registration** — Registration endpoint, health pings, agents table update
5. **Config passthrough** — Wire config override through `src/api/app.py`
6. **Expert discovery** — `GET /api/experts` on orchestrator
7. **Job assignment** — Auto-assign jobs to idle agents
8. **Cockpit UI** — Expert selector in job creation
9. **Tests**

## Verification

```bash
# CLI backward compat still works
python agent.py --config researcher --description "Test task"

# Agent starts without config, registers with orchestrator
python agent.py --port 8001
# → Agent registers, sends health pings, waits for jobs

# Expert discovery
curl http://localhost:8085/api/experts

# Create job with expert config via API
curl -X POST http://localhost:8085/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"description": "Literature review on RAG systems", "expert_id": "researcher"}'

# Cockpit: open http://localhost:4200, create job, pick "Academic Researcher"
```

## Open Design Decisions

### Expert prompt file distribution
Expert configs reference custom prompt files (instructions.md, etc.). These need to be accessible to the agent pod. Options:
- **Docker-compose (dev):** Shared volume mount of `config/` — works out of the box
- **K8s (prod):** Bundle `config/experts/` in the agent container image (it's part of the codebase and changes with deploys)

### Config override format
When the orchestrator sends config to an agent, it sends the **override dict** (not the full merged config). The agent always starts from its local `defaults.yaml` and applies the override. This means `defaults.yaml` must be consistent across all agent pods (guaranteed since it's in the container image).

### Concurrent job limit
Currently agents process one job at a time. This is fine for now — the orchestrator manages concurrency by tracking which agents are idle vs working. Future: an agent could report capacity > 1 during registration.

## Future Experts

Adding a new expert = creating a directory in `config/experts/`:

```bash
mkdir config/experts/compliance_checker
# Add config.yaml + instructions.md
# Orchestrator discovers it automatically via GET /api/experts
```

Potential experts:
- **Requirements Extractor** — Extract and classify requirements from documents
- **Compliance Checker** — Validate requirement coverage against regulatory frameworks
- **Technical Writer** — Write technical documentation from code/architecture
- **Code Reviewer** — Review code for quality, security, and best practices
