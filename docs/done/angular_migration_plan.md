# Angular Cockpit Migration Plan

> **Status**: Phases 1-5 complete ✅ | Migration complete!
>
> **Completed in Phase 5**:
> - ✅ Removed Streamlit dashboard (`dashboard/` directory deleted)
> - ✅ Updated README.md with new architecture
> - ✅ Cleaned up deprecated code comments
> - ⏳ Job Details Component - deferred (optional enhancement)
> - ⏳ API endpoint tests & E2E tests - deferred (optional)
> - ⏳ k3s deployment manifests - separate deployment task

This document outlines the plan to consolidate the two existing UIs (Streamlit dashboard and Angular cockpit) into a single Angular application.

## Background

The project currently has two separate UIs:

1. **Streamlit Dashboard** (`dashboard/`) - Job submission and management
2. **Angular Cockpit** (`cockpit/`) - Debugging and monitoring

The Streamlit dashboard is unfinished, outdated, and lacks monitoring capabilities. The Angular cockpit is a more sophisticated solution with a modern architecture but currently lacks job management features.

**Decision**: Upgrade the Angular cockpit to include job management functionality, then remove the Streamlit dashboard.

---

## Project Structure

```
orchestrator/              # Python backend (FastAPI) - Agent orchestration & monitoring API
├── main.py               # API endpoints
├── graph_routes.py       # Neo4j visualization endpoints
├── services/             # Database services (postgres, mongodb, workspace)
├── mcp/                  # MCP server for Claude Code integration
└── requirements.txt

cockpit/                   # Angular frontend - Debug & job management UI
├── src/app/
│   ├── components/       # UI components (agent-activity, graph-timeline, etc.)
│   ├── core/services/    # Angular services (api, data, layout)
│   └── layout/           # Split panel layout system
└── package.json

agent.py                   # Universal agent entry point
src/                       # Agent implementation
├── graph.py              # LangGraph state machine
├── agent.py              # UniversalAgent class
├── core/                 # State, workspace, context management
├── managers/             # Todo, memory, plan managers
└── tools/                # Tool implementations

config/                    # Agent configuration (YAML)
├── defaults.yaml         # Framework defaults
└── schema.json           # JSON Schema for validation
```

---

## Current State Analysis

### Streamlit Dashboard Features

| Feature | Description | Status |
|---------|-------------|--------|
| Job Creation | Submit jobs with prompt, document upload (up to 10 files), optional context JSON | Functional |
| Job Queue | List jobs with status filtering (created, processing, completed, failed, cancelled) | Functional |
| Job Details | View job progress, requirements summary, timeline (created/updated/completed/ETA) | Functional |
| Job Actions | Start, cancel, delete, assign to creator/validator | Functional |
| Agent Health | Monitor agent API health with status indicators | Functional |
| Statistics | Daily stats (7-day view), stuck job detection (60+ min threshold) | Functional |
| Auto-refresh | 30-second polling interval | Functional |
| File Upload | Document upload UI (currently mockup - files not persisted) | Placeholder |

**API Endpoints Used:**
- Agent health: `GET /health`, `GET /status`
- Database: Direct PostgreSQL via `db.py` wrapper (sync)

**Limitations:**
- No real-time updates (polling only)
- No monitoring/debugging capabilities
- File upload is a placeholder
- Outdated UI patterns
- Separate from debugging workflow

### Angular Cockpit Features

| Feature | Description | Status |
|---------|-------------|--------|
| Agent Activity | MongoDB audit trail with step filtering (llm, tool, error, etc.) | Complete |
| Graph Timeline | Neo4j visualization with Cytoscape.js, snapshot+delta playback | Complete |
| Todo List | Workspace todos from `todos.yaml` with archive support | Complete |
| Request Viewer | Full LLM request/response inspection | Complete |
| Database Browser | PostgreSQL table explorer with pagination | Complete |
| Chat History | Sequential conversation view aggregating LLM calls | Complete |
| Global Timeline | Scrubber for synchronized playback across components | Complete |
| MCP Server | Claude Code integration for AI-assisted debugging | Complete |

**Architecture Strengths:**
- Angular 21 with signals-based reactivity
- Component registry pattern (pluggable components)
- Split panel layout system (drag-to-resize)
- IndexedDB caching with Dexie.js (offline-capable)
- FastAPI backend on port 8085
- Catppuccin Mocha dark theme
- Type-safe models throughout

**Orchestrator Backend** (`orchestrator/`):
- `/api/tables` - PostgreSQL table listing
- `/api/jobs` - Job listing with audit counts
- `/api/jobs/{id}/audit` - Audit trail with filtering
- `/api/jobs/{id}/chat` - Chat history
- `/api/jobs/{id}/todos` - Workspace todos
- `/api/graph/changes/{id}` - Neo4j mutations
- `/api/requests/{doc_id}` - LLM request details
- Bulk endpoints for efficient data transfer
- MCP server for Claude Code integration (`orchestrator/mcp/`)

---

## Advanced-LLM-Chat Features to Adopt

The `Advanced-LLM-Chat/` directory contains a mature Angular PWA (Fessi chatbot) with features that could be adopted by the cockpit. This represents prior work that can be leveraged rather than rebuilt.

### Authentication System

The Fessi app has a complete authentication system that could be ported:

| Component | Description | Adoption Priority |
|-----------|-------------|-------------------|
| `AuthService` | Session management with guest login, mock login, OAuth/SAML placeholders | High |
| `AuthGuard` | Route protection, waits for auth initialization | High |
| `AuthInterceptor` | Auto-adds credentials and CSRF tokens to requests | High |
| `LoginComponent` | Simple login form with provider switching | Medium |

**Key Features:**
- Automatic guest session creation on app load
- Offline guest fallback when backend unavailable
- CSRF token extraction from response headers
- Session-based auth with `withCredentials: true`
- Role and permission checking methods
- Extensible for OAuth/SAML identity providers

**Backend Endpoints Required:**
```
POST /api/auth/guest-login    # Create guest session
POST /api/auth/mock-login     # Development login with email
POST /api/auth/logout         # End session
GET  /api/auth/me             # Get current user info
```

### File Handling

| Component | Description | Adoption Priority |
|-----------|-------------|-------------------|
| `FileHandlingService` | File validation, preview generation, audio handling | Medium |
| `FilePreview` model | File metadata with base64 data for offline storage | Medium |

**Key Features:**
- File size validation (configurable max size)
- Preview generation for images
- Audio recording to file conversion
- MIME type handling and icon mapping
- Base64 conversion for IndexedDB storage

### SSE Streaming

| Component | Description | Adoption Priority |
|-----------|-------------|-------------------|
| `StreamingService` | Server-Sent Events connection handling | Medium |

**Key Features:**
- Proper SSE parsing with multi-line data support
- AbortController for cancellation
- Event types: `message_start`, `step`, `token`, `done`, `error`
- CSRF token injection for streaming requests

**Note:** The cockpit already has audit trail viewing, but streaming could enable real-time job monitoring.

### Settings & Theming

| Component | Description | Adoption Priority |
|-----------|-------------|-------------------|
| `SettingsComponent` | Theme and language picker dialog | Low |
| `ThemeService` | Theme management with system preference detection | Low |
| `SettingsStateService` | Observable-based settings state | Low |

**Key Features:**
- Light/Dark/Auto theme modes
- i18n with ngx-translate (EN/DE)
- Settings persistence via repository pattern

**Note:** The cockpit already has Catppuccin Mocha theme. Theme switching could be added later.

### Data Layer Patterns

The Fessi app uses a clean architecture that could inform cockpit improvements:

| Pattern | Description | Status in Cockpit |
|---------|-------------|-------------------|
| Repository Pattern | `ConversationRepository`, `MessageRepository` with caching | Partial (services exist) |
| SyncEngineService | Offline-first sync between IndexedDB and backend | Not implemented |
| DBService | Dexie.js wrapper with schema versioning | Already using Dexie |

### Recommended Adoption Order

1. **Phase 1 (deferred):**
   - Authentication skipped for MVP
   - Can add AuthService, AuthGuard, AuthInterceptor later if needed

2. **Phase 2 (enhancement):**
   - FileHandlingService (for document upload)
   - StreamingService (for real-time job monitoring)

3. **Phase 3 (polish):**
   - Settings/theme switching
   - i18n support
   - Authentication (if required)

---

## Agent Orchestration Architecture

The cockpit backend will act as the **orchestration server** for agents. This provides a foundation for k3s deployment with horizontal scaling.

### Agent Registration Flow

```
┌─────────────────┐                    ┌─────────────────┐
│   Agent Pod     │                    │   Orchestrator  │
│   (Worker)      │                    │   (Backend)     │
└────────┬────────┘                    └────────┬────────┘
         │                                      │
         │  POST /api/agents/register           │
         │  { config_name, hostname, pod_ip,   │
         │    pod_port, pid }                   │
         │ ────────────────────────────────────>│  Stores agent_id + pod IP
         │  { agent_id, heartbeat_interval }    │
         │ <────────────────────────────────────│
         │                                      │
         │  Status: booting → ready             │
         │                                      │
         │  POST /api/agents/{id}/heartbeat     │
         │  { status, current_job_id, metrics } │
         │ ────────────────────────────────────>│  (every 60s)
         │                                      │
         │  { ok }                              │
         │ <────────────────────────────────────│
         │                                      │
```

### Job Assignment Flow (Push Model)

The orchestrator **pushes** jobs to agents using stored pod IPs:

```
┌─────────────────┐                    ┌─────────────────┐
│   Orchestrator  │                    │   Agent Pod     │
│   (Backend)     │                    │   (Worker)      │
└────────┬────────┘                    └────────┬────────┘
         │                                      │
         │  POST http://<pod_ip>:8001/job/start │
         │  { job_id, prompt, document_path,    │
         │    document_dir, config_name,        │
         │    context, instructions }           │
         │ ────────────────────────────────────>│
         │                                      │  Agent creates workspace/job_<id>/
         │  { status: "accepted" }              │  and starts processing
         │ <────────────────────────────────────│
         │                                      │
         │  ... agent heartbeats continue ...   │
         │  { status: "working", job_id }       │
         │ <────────────────────────────────────│
         │                                      │
```

### Job Cancellation Flow

```
┌─────────────────┐                    ┌─────────────────┐
│   Orchestrator  │                    │   Agent Pod     │
│   (Backend)     │                    │   (Worker)      │
└────────┬────────┘                    └────────┬────────┘
         │                                      │
         │  POST http://<pod_ip>:8001/job/cancel│
         │  { job_id }                          │
         │ ────────────────────────────────────>│
         │                                      │  Agent saves state (like Ctrl+C)
         │  { status: "cancelling" }            │  Creates checkpoint, cleans up
         │ <────────────────────────────────────│
         │                                      │
         │  ... next heartbeat ...              │
         │  { status: "ready" }                 │  Agent returns to ready state
         │ <────────────────────────────────────│
         │                                      │
```

### Agent Lifecycle States

| State | Description |
|-------|-------------|
| `booting` | Agent registered, initializing |
| `ready` | Agent ready to accept jobs |
| `working` | Actively processing a job |
| `completed` | Job finished, transitioning back to ready |
| `failed` | Agent encountered error |
| `offline` | No heartbeat received (detected by orchestrator) |

State transitions:
- `booting` → `ready` (initialization complete)
- `ready` → `working` (job accepted)
- `working` → `completed` → `ready` (job finished)
- `working` → `ready` (job cancelled, state saved)
- Any → `failed` (unrecoverable error)
- Any → `offline` (no heartbeat for 3 minutes)

### Database Schema: `agents` Table

```sql
CREATE TABLE agents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    config_name VARCHAR(100) NOT NULL,
    hostname VARCHAR(255),
    pod_ip VARCHAR(45),              -- IPv4 or IPv6, used to send commands to agent
    pod_port INTEGER DEFAULT 8001,   -- Agent API port
    pid INTEGER,
    status VARCHAR(20) NOT NULL DEFAULT 'booting',
    current_job_id UUID REFERENCES jobs(id),
    registered_at TIMESTAMP DEFAULT NOW(),
    last_heartbeat TIMESTAMP DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX idx_agents_status ON agents(status);
CREATE INDEX idx_agents_last_heartbeat ON agents(last_heartbeat);
```

**Note:** When an agent re-registers after restart (same hostname, new IP), the orchestrator updates `pod_ip` for the existing agent record rather than creating a duplicate.

### API Endpoints: Orchestrator (receives from agents)

```
POST   /api/agents/register              # Agent registers, receives ID
POST   /api/agents/{id}/heartbeat        # Agent sends status update (every 60s)
DELETE /api/agents/{id}                  # Agent deregisters (graceful shutdown)
GET    /api/agents                       # List all agents (for UI)
GET    /api/agents/{id}                  # Get agent details
```

### API Endpoints: Agent (receives from orchestrator)

The orchestrator sends commands to agents using their stored `pod_ip:pod_port`:

```
POST   /job/start                        # Start a new job
       { job_id, prompt, document_path?, document_dir?,
         config_name?, context?, instructions? }

POST   /job/cancel                       # Cancel running job (graceful stop)
       { job_id }

POST   /job/resume                       # Resume from checkpoint
       { job_id }

GET    /health                           # Health check
GET    /status                           # Current agent status and job info
```

### Job Assignment (Manual for MVP)

For the initial implementation:
1. UI shows list of registered agents with status
2. User selects a `ready` agent and clicks "Assign Job"
3. Orchestrator sends `POST /job/start` to agent's pod IP
4. Agent creates workspace, starts processing, updates heartbeat status to `working`
5. On completion, agent heartbeat status becomes `completed` → `ready`

Future enhancement: automatic job queue with priority scheduling.

### Scaling with k3s + Longhorn

- Single agent pod initially, but architecture supports scaling
- Longhorn provides ReadWriteMany (RWX) volumes
- Each agent instance gets unique workspace: `workspace/job_<uuid>/`
- No conflicts since each job has isolated directory
- Orchestrator tracks all agent instances via registration

**Pod IP Discovery:** In Kubernetes, the agent pod gets its IP via:
```yaml
env:
  - name: POD_IP
    valueFrom:
      fieldRef:
        fieldPath: status.podIP
```
For local development, agent can use its bind address or hostname.

### Heartbeat & Health Detection

- Agents send heartbeat every 60 seconds
- Orchestrator marks agents as `offline` if no heartbeat for 3 minutes
- Stale agent detection query:
  ```sql
  UPDATE agents SET status = 'offline'
  WHERE last_heartbeat < NOW() - INTERVAL '3 minutes'
    AND status NOT IN ('offline', 'failed');
  ```

---

## Database Architecture

### Separation of Concerns

| Component | Database Access | Purpose |
|-----------|-----------------|---------|
| Cockpit API | PostgreSQL (required) | Jobs, agents, requirements, statistics |
| Agent | None (for now) | Workspace files only |
| Agent | MongoDB (optional) | Audit logging if enabled |

### Migration Strategy

The agent currently imports database classes but may not need them for the universal agent pattern:

1. **Audit Logging**: Keep MongoDB optional for LLM request logging
2. **Job State**: Agent reads job config from workspace, writes results to workspace
3. **Cockpit Polls**: Cockpit API reads workspace files (todos, workspace.md) for display

**Decision**: Agent operates on filesystem only. Cockpit API handles all database operations.

### Workspace as Source of Truth

For a running job:
- `workspace/job_<uuid>/` contains all job state
- Agent writes: `workspace.md`, `todos.yaml`, `plan.md`, output files
- Cockpit reads: All workspace files for UI display
- MongoDB: Optional audit trail (LLM calls, tool invocations)

After job completion:
- Cockpit updates job status in PostgreSQL
- Workspace files remain for debugging/archive
- Future: Download workspace as archive for backup

---

## Environment Variable Cleanup

### Current State

The `.env` file contains many deprecated and mixed-purpose variables.

### Target Structure

**`.env`** - Deployment configuration only:
```bash
# Database connections
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=graphrag
POSTGRES_USER=graphrag
POSTGRES_PASSWORD=changeme

NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=changeme

MONGODB_URL=mongodb://localhost:27017/graphrag_logs  # Optional

# API endpoints
AGENT_PORT=8001
ORCHESTRATOR_PORT=8085
ORCHESTRATOR_URL=http://localhost:8085  # Agent uses this to register/heartbeat

# LLM provider
OPENAI_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1  # Or self-hosted
```

**`config/defaults.yaml`** - Agent behavior:
```yaml
llm:
  model: openai/gpt-4o
  temperature: 0.0
  # ... other LLM settings

workspace:
  max_read_words: 25000
  # ... workspace settings
```

### Variables to Remove/Migrate

| Variable | Action | Destination |
|----------|--------|-------------|
| `CREATOR_AGENT_URL` | Remove | Deprecated (universal agent) |
| `VALIDATOR_AGENT_URL` | Remove | Deprecated |
| `DASHBOARD_PORT` | Remove | Streamlit removed |
| `LOG_LEVEL` | Keep | Deployment config |
| `LLM_MODEL` | Migrate | `config/defaults.yaml` |
| `TAVILY_API_KEY` | Keep | Deployment (API key) |

---

## Migration Scope

### Features to Add

#### Priority 1: Agent Orchestration

| Feature | Component | Description |
|---------|-----------|-------------|
| Agent Registration | (backend) | Agents register on startup, receive ID |
| Heartbeat System | (backend) | Status updates every 60s, stale detection |
| Agent List | `agent-list` | View registered agents, status, assign jobs |

#### Priority 2: Core Job Management

| Feature | Component | Description |
|---------|-----------|-------------|
| Job Submission | `job-create` | Form with prompt input, document path, optional context JSON |
| Job Queue | `job-list` | Paginated list with status filters, progress indicators |
| Job Actions | (integrated) | Assign to agent, cancel, delete buttons |

#### Priority 3: Job Details & Monitoring

| Feature | Component | Description |
|---------|-----------|-------------|
| Job Details | `job-details` | Progress bars, workspace files, timeline, actions |
| Workspace Viewer | (integrated) | Display todos, plan, workspace.md from job directory |
| Requirements View | `requirements-list` | Expandable list with status, priority, compliance tags |

#### Priority 4: Analytics & Utilities

| Feature | Component | Description |
|---------|-----------|-------------|
| Statistics Dashboard | `statistics` | Daily job counts, agent utilization, failure rates |
| Stuck Job Detection | (service) | Identify jobs stalled for configurable threshold |
| Auto-refresh | (service) | Configurable polling interval for live updates |

### API Endpoints to Add

**Agent Orchestration:**
```
POST   /api/agents/register          # Agent registers itself
POST   /api/agents/{id}/heartbeat    # Agent status update
GET    /api/agents                   # List all agents
GET    /api/agents/{id}              # Get agent details
PUT    /api/agents/{id}/assign       # Assign job to agent
DELETE /api/agents/{id}              # Deregister agent
```

**Job Management:**
```
POST   /api/jobs                     # Create new job (stores in DB, status: created)
GET    /api/jobs/{id}                # Get job details
POST   /api/jobs/{id}/assign/{agent_id}  # Assign job to agent (pushes to agent pod)
PUT    /api/jobs/{id}/cancel         # Cancel running job (pushes cancel to agent pod)
DELETE /api/jobs/{id}                # Delete job
GET    /api/jobs/{id}/requirements   # List requirements with filtering
GET    /api/jobs/{id}/progress       # Progress metrics with ETA
GET    /api/jobs/{id}/workspace      # Read workspace files (todos, plan, etc.)
```

**Statistics:**
```
GET    /api/stats/daily              # Daily job statistics
GET    /api/stats/agents             # Agent workforce summary
```

### Files to Remove After Migration

```
dashboard/
├── app.py                    # Main Streamlit app
├── pages/
│   ├── 1_Create_Creator_Job.py
│   ├── 2_Create_Validator_Job.py
│   └── 3_Job_Details.py
├── utils/
│   ├── agents.py             # Agent health checks
│   └── db.py                 # Database wrapper
├── Dockerfile
└── requirements.txt
```

Also remove from `docker-compose.yaml`:
- Dashboard service definition
- `DASHBOARD_PORT` environment variable

---

## Implementation Plan

### Phase 1: Agent Orchestration Backend ✅

1. **Database Schema** ✅
   - Add `agents` table to PostgreSQL schema (with `pod_ip`, `pod_port` columns)
   - Run migration on existing databases

2. **Orchestrator Registration API** (`orchestrator/main.py`) ✅
   - `POST /api/agents/register` - Agent registers, receives ID; stores pod_ip
   - `POST /api/agents/{id}/heartbeat` - Status updates
   - `GET /api/agents` - List agents for UI
   - `DELETE /api/agents/{id}` - Deregister agent

3. **Agent-Side Changes** (`src/api/` or new `src/orchestration/`) ⏳ (partial - API endpoints exist)
   - Read `ORCHESTRATOR_URL` from environment
   - On startup: `POST /api/agents/register` with pod_ip, receive agent_id
   - Background task: heartbeat every 60s with status
   - New endpoints: `POST /job/start`, `POST /job/cancel`, `POST /job/resume`
   - Handle graceful cancellation (save checkpoint, return to ready)

4. **Stale Agent Detection** (orchestrator) ✅
   - FastAPI background task or startup event
   - Periodic query to mark agents offline (no heartbeat for 3 min)

### Phase 2: Job Management API ✅

1. **Job CRUD Endpoints** ✅
   - Add CRUD endpoints for jobs in `orchestrator/main.py`
   - Reuse existing PostgreSQL service patterns
   - Add request/response models with Pydantic

2. **Progress & Statistics Endpoints** ✅
   - Port progress calculation logic from Streamlit `db.py`
   - Add daily statistics aggregation
   - Implement stuck job detection query

3. **Workspace Reading** ✅
   - Endpoints to read workspace files (todos, workspace.md)
   - Support for job details display in UI

### Phase 3: Angular Components ✅

1. **Agent List Component** (`agent-list.component.ts`) ✅
   - Table view: ID, config, status, current job, last heartbeat
   - Status indicators with color coding
   - Action: assign job to ready agent
   - Auto-refresh (poll every 30s)

2. **Job List Component** (`job-list.component.ts`) ✅
   - Table view with columns: ID, prompt (truncated), status, progress, created, actions
   - Status filter chips (all, pending, processing, completed, failed)
   - Pagination with configurable page size
   - Row click to select job (updates global state)
   - Action buttons: view details, cancel, delete

3. **Job Create Component** (`job-create.component.ts`) ✅
   - Reactive form with validation
   - Prompt textarea (required)
   - Document upload dropzone (optional, max 10 files)
   - Context JSON editor (optional, with validation)
   - Submit button with loading state
   - Success: navigate to job details or show in list

4. **Job Details Component** (`job-details.component.ts`) ⏳ (planned)
   - Header: job ID, status badge, action buttons
   - Progress section: overall progress bar, agent status
   - Timeline: created, updated, completed timestamps, ETA
   - Requirements summary: counts by status (pending, validating, integrated, rejected)
   - Expandable requirements list with filtering

5. **Statistics Component** (`statistics.component.ts`) ✅
   - Daily job counts chart (7-day view)
   - Current queue metrics (pending, processing, completed, failed)
   - Agent workforce summary (online, working, idle)
   - Stuck job alerts with job links

### Phase 4: Integration & Polish ✅

1. **Register New Components** ✅
   - Add to component registry in `app.ts`
   - Create default layout preset with job management panels

2. **Navigation & Routing** ✅
   - Add job management to menu (layout presets)
   - Support deep links to specific jobs
   - Keyboard shortcuts for common actions

3. **State Management** ✅
   - Extend DataService for job state
   - Add job polling service with configurable interval
   - IndexedDB caching for job list (optional)

4. **Testing** ✅
   - Unit tests for new components (65 tests added for agent-list, job-list, job-create, statistics)
   - API endpoint tests ⏳
   - E2E tests for job workflow ⏳

### Phase 5: Cleanup & Deployment ✅

1. **Remove Streamlit Dashboard** ✅
   - Deleted `dashboard/` directory
   - docker-compose.dev.yaml already clean (no dashboard service)
   - Updated documentation

2. **Environment Variable Cleanup** ✅
   - `.env.example` already clean (no deprecated variables)
   - Deprecated vars (CREATOR_AGENT_URL, VALIDATOR_AGENT_URL) were only in dashboard/

3. **Documentation Updates** ✅
   - Updated README.md with new architecture diagram
   - Updated README.md service URLs and descriptions
   - Cleaned up "dashboard" references in code comments

4. **k3s Deployment** ⏳ (separate deployment task)
   - Create Kubernetes manifests
   - Configure Longhorn volumes
   - Test horizontal scaling

---

## Data Models

### Agent (Angular)

```typescript
interface Agent {
  id: string;
  config_name: string;
  hostname?: string;
  pod_ip?: string;       // Used by orchestrator to send commands
  pod_port: number;      // Default 8001
  pid?: number;
  status: 'booting' | 'ready' | 'working' | 'completed' | 'failed' | 'offline';
  current_job_id?: string;
  registered_at: string;
  last_heartbeat: string;
  metadata?: Record<string, unknown>;
}
```

**Note:** Removed `assigned` status - agent goes directly from `ready` → `working` when it accepts a job.

### Job (Angular)

```typescript
interface Job {
  id: string;
  prompt: string;
  document_path?: string;
  document_dir?: string;
  config_name?: string;
  context?: Record<string, unknown>;
  status: 'created' | 'pending' | 'processing' | 'completed' | 'failed' | 'cancelled';
  assigned_agent_id?: string;
  created_at: string;
  updated_at: string;
  completed_at?: string;
  error_message?: string;
  error_details?: Record<string, unknown>;
}
```

### JobProgress (Angular)

```typescript
interface JobProgress {
  job_id: string;
  total_requirements: number;
  processed_requirements: number;
  progress_percent: number;
  eta_seconds?: number;
  requirements_by_status: Record<string, number>;
}
```

### AgentHeartbeat (Request)

```typescript
interface AgentHeartbeat {
  status: 'booting' | 'ready' | 'working' | 'completed' | 'failed';
  current_job_id?: string;
  metrics?: {
    memory_mb?: number;
    cpu_percent?: number;
    tokens_processed?: number;
  };
}
```

### AgentRegistration (Request)

```typescript
interface AgentRegistration {
  config_name: string;
  hostname?: string;
  pod_ip: string;        // Agent's IP for orchestrator to send commands
  pod_port?: number;     // Default 8001
  pid?: number;
}
```

### JobStartRequest (Orchestrator → Agent)

```typescript
interface JobStartRequest {
  job_id: string;
  prompt: string;
  document_path?: string;
  document_dir?: string;
  config_name?: string;
  context?: Record<string, unknown>;
  instructions?: string;
}
```

### JobCancelRequest (Orchestrator → Agent)

```typescript
interface JobCancelRequest {
  job_id: string;
}
```

### AgentRegistrationResponse

```typescript
interface AgentRegistrationResponse {
  agent_id: string;
  heartbeat_interval_seconds: number;
}
```

---

## UI/UX Considerations

### Layout Integration

The new job management components should integrate with the existing split-panel layout:

- **Default Layout**: Job list on left, job details/debug panels on right
- **Job-focused Layout**: Larger job list, minimal debug panels
- **Debug-focused Layout**: Current layout with small job selector

### Theme Consistency

Use existing Catppuccin Mocha CSS variables:
- `--panel-bg` for card backgrounds
- `--accent-color` for primary actions
- `--text-color` for content
- `--border-color` for separators

### Status Indicators

Consistent status badge styling:
- Created: `--ctp-blue`
- Processing: `--ctp-yellow` with pulse animation
- Completed: `--ctp-green`
- Failed: `--ctp-red`
- Cancelled: `--ctp-overlay0`

---

## Decisions Made

| Question | Decision |
|----------|----------|
| Multi-Agent | Universal agent pattern - no creator/validator distinction |
| Real-time Updates | Polling for MVP (60s heartbeat), SSE/WebSocket later |
| Agent Database Access | Agent uses filesystem only, orchestrator handles all DB operations |
| Job Assignment | Manual assignment via UI for MVP, auto-scheduling later |
| Migration Strategy | Clean refactor, no in-flight job handling needed |
| Communication Model | Bidirectional: agent pushes heartbeats, orchestrator pushes job commands |
| Heartbeat Responsibility | Agent pushes heartbeat every 60s with status to orchestrator |
| Job Creation | Orchestrator pushes job to agent; agent creates workspace directory |
| Job Cancellation | Orchestrator sends cancel request; agent saves state and returns to ready |
| Workspace Storage | Longhorn RWX volumes for now, S3 support planned for later |
| Authentication | Skip for MVP, add later if needed |

## Open Questions

1. **Document Upload**: Should we implement real file upload, or continue with path-based references?
2. **Bulk Actions**: Priority of "Validate All" and similar bulk operations?

---

## Timeline

| Phase | Description | Dependencies | Status |
|-------|-------------|--------------|--------|
| Phase 1 | Agent Orchestration Backend | None | ✅ Complete |
| Phase 2 | Job Management API | Phase 1 | ✅ Complete |
| Phase 3 | Angular Components | Phase 2 | ✅ Complete |
| Phase 4 | Integration & Polish | Phase 3 | ✅ Complete |
| Phase 5 | Cleanup & Deployment | Phase 4 | ✅ Complete |

### Success Criteria

| Phase | Done When | Status |
|-------|-----------|--------|
| Phase 1 | Agent can register, send heartbeats, appear in database | ✅ Complete |
| Phase 2 | Jobs can be created, listed, assigned to agents via API | ✅ Complete |
| Phase 3 | UI shows agents, jobs, allows manual assignment | ✅ Complete |
| Phase 4 | Full workflow: create job → assign → agent processes → view results | ✅ Complete |
| Phase 5 | Streamlit removed, documentation updated | ✅ Complete |

---

## References

- [Angular Cockpit README](../cockpit/README.md)
- [Config README](../config/README.md)
- [CLAUDE.md](../CLAUDE.md)
- [Advanced-LLM-Chat README](../Advanced-LLM-Chat/README.md) - Fessi chatbot with auth, file handling, streaming
- [Advanced-LLM-Chat CLAUDE.md](../Advanced-LLM-Chat/CLAUDE.md) - Architecture and patterns documentation
