# Debug Cockpit - Implementation Plan

## Overview

A flexible, composable Angular dashboard framework for debugging and visualizing agent execution. Features a tiling layout system with pluggable components that can be arranged in any configuration.

## Current Status

**Phase 3 In Progress** - FastAPI backend and first real component (DbTableComponent) implemented.

Run the app:
```bash
# Terminal 1: Start FastAPI backend
cd cockpit/api
pip install -r requirements.txt
uvicorn main:app --reload --port 8080

# Terminal 2: Start Angular frontend
cd cockpit && npm start
# Open http://localhost:4200
```

Current features:
- Dark themed UI (Catppuccin Mocha)
- Timeline scrubber bar at top (60px) with play/pause button
- Hamburger menu with database links and settings
- 3-column resizable layout (25% / 50% / 25%) using angular-split
- Panel headers with component names
- Drag handles between panels for resizing
- Layout persistence to localStorage
- Preset layouts (`default`, `two-column`, `grid`)
- **PostgreSQL Table Viewer** (DbTableComponent):
  - Table tabs: jobs, requirements, sources, citations
  - Paginated data display with column headers
  - Cell formatting by type (dates, JSON, booleans, etc.)
  - Loading spinner and error states
  - Works offline (shows empty state with backend hint)

## Data Architecture

The cockpit uses a three-layer architecture to fetch and manage agent state:

```
┌─────────────────────────────────────────────────────────┐
│                    Angular Frontend                      │
│  ┌─────────────────────────────────────────────────┐    │
│  │              Visual Components                   │    │
│  │   (AgentChat, Workspace, DbTable, Timeline)     │    │
│  └──────────────────────┬──────────────────────────┘    │
│                         │ subscribe to signals           │
│  ┌──────────────────────▼──────────────────────────┐    │
│  │              StateService                        │    │
│  │  - currentJob: Signal<Job>                       │    │
│  │  - timelinePosition: Signal<number>             │    │
│  │  - auditWindow: Signal<AuditEntry[]>            │    │
│  │  - workspaceFiles: Signal<FileTree>             │    │
│  │  - handles buffering/windowing for large runs   │    │
│  └──────────────────────┬──────────────────────────┘    │
│                         │ HTTP calls                     │
│  ┌──────────────────────▼──────────────────────────┐    │
│  │              ApiService                          │    │
│  │  - getJobs(): Observable<Job[]>                  │    │
│  │  - getAuditWindow(jobId, from, to)              │    │
│  │  - getWorkspaceFile(jobId, path)                │    │
│  │  - getRequirements(jobId)                       │    │
│  │  - queryCypher(query), querySQL(query)          │    │
│  └──────────────────────┬──────────────────────────┘    │
└─────────────────────────┼───────────────────────────────┘
                          │ HTTP :8080
┌─────────────────────────▼───────────────────────────────┐
│              FastAPI Debug Backend                       │
│                                                          │
│  GET  /api/jobs                     → PostgreSQL         │
│  GET  /api/jobs/{id}                → PostgreSQL         │
│  GET  /api/jobs/{id}/audit          → MongoDB            │
│       ?from=<ts>&to=<ts>              (windowed query)   │
│  GET  /api/jobs/{id}/workspace      → Filesystem         │
│  GET  /api/jobs/{id}/workspace/{path} → Filesystem       │
│  GET  /api/jobs/{id}/requirements   → PostgreSQL         │
│  POST /api/cypher                   → Neo4j              │
│  POST /api/sql                      → PostgreSQL         │
│                                                          │
└────────────┬────────────┬────────────┬──────────────────┘
             │            │            │
             ▼            ▼            ▼
         PostgreSQL    MongoDB      Neo4j
          :5432        :27017       :7687
```

### Buffering Strategy for Large Runs

For long-running jobs (e.g., 20+ hours), loading the entire audit trail would consume too much memory. The `StateService` implements windowed loading:

1. **Time Window**: Only fetch audit entries within ±N minutes of current timeline position
2. **Lazy Expansion**: As user scrubs, request new windows and merge into buffer
3. **Memory Cap**: When buffer exceeds threshold, drop entries furthest from current position
4. **Backend Support**: `/api/jobs/{id}/audit?from=<timestamp>&to=<timestamp>` returns only entries in range

```typescript
// StateService windowing example
const WINDOW_SIZE_MS = 5 * 60 * 1000; // ±5 minutes

async loadAuditWindow(position: number) {
  const from = position - WINDOW_SIZE_MS;
  const to = position + WINDOW_SIZE_MS;
  const entries = await this.api.getAuditWindow(this.currentJob().id, from, to);
  this.mergeIntoBuffer(entries);
}

## File Structure

### Angular Frontend (Implemented)

```
cockpit/src/app/
├── core/
│   ├── models/
│   │   ├── layout.model.ts           # LayoutConfig, ComponentType, ComponentMetadata
│   │   ├── api.model.ts              # TableInfo, ColumnDef, TableDataResponse ✅
│   │   └── state.model.ts            # Job, AuditEntry, FileNode (planned)
│   └── services/
│       ├── layout.service.ts         # Signals-based layout state + persistence
│       ├── component-registry.service.ts  # Component registration
│       ├── api.service.ts            # HTTP client for backend ✅
│       └── state.service.ts          # Signals state for db-table ✅
├── layout/
│   ├── split-panel/
│   │   └── split-panel.component.ts  # Recursive split renderer
│   ├── component-host/
│   │   └── component-host.component.ts # Dynamic component loader
│   └── panel-header/
│       └── panel-header.component.ts # Panel title bar
├── components/
│   ├── menu/
│   │   └── menu.component.ts         # Hamburger menu with links/settings
│   ├── timeline/
│   │   └── timeline.component.ts     # Top scrubber bar
│   ├── db-table/
│   │   └── db-table.component.ts     # PostgreSQL table viewer ✅
│   └── placeholders/
│       ├── placeholder-a.component.ts  # "Workspace" placeholder
│       ├── placeholder-b.component.ts  # "Agent Chat" placeholder
│       └── placeholder-c.component.ts  # "Database" placeholder
├── app.ts                            # Root frame component
├── app.config.ts                     # App configuration (+ provideHttpClient)
└── app.routes.ts                     # Routing (unused for now)

cockpit/src/
└── styles.scss                       # Dark theme CSS variables
```

### FastAPI Backend (Partially Implemented)

```
cockpit/api/
├── __init__.py                       # ✅
├── main.py                           # FastAPI app, CORS, table endpoints ✅
├── requirements.txt                  # fastapi, uvicorn, asyncpg ✅
├── config.py                         # Environment variables, DB URLs (planned)
├── routes/                           # (planned - currently in main.py)
│   ├── __init__.py
│   ├── jobs.py                       # GET /api/jobs, /api/jobs/{id}
│   ├── audit.py                      # GET /api/jobs/{id}/audit
│   ├── workspace.py                  # GET /api/jobs/{id}/workspace/*
│   └── query.py                      # POST /api/cypher, /api/sql
├── services/
│   ├── __init__.py                   # ✅
│   ├── postgres.py                   # asyncpg connection pool ✅
│   ├── mongodb.py                    # motor async client (planned)
│   └── neo4j.py                      # neo4j async driver (planned)
└── models/
    ├── __init__.py
    └── schemas.py                    # Pydantic models (planned)
```

**Current API Endpoints:**
- `GET /api/tables` - List available tables with row counts
- `GET /api/tables/{name}` - Paginated table data with columns
- `GET /api/tables/{name}/schema` - Column definitions
- `GET /api/health` - Health check

## Architecture

### Tiling Layout System

The dashboard uses a recursive split-panel system. Each panel can be:
- A **component** (leaf node)
- A **split** containing two or more child panels (horizontal or vertical)

```
Example Layouts:

50/50 Vertical                33/33/33 Horizontal
┌────────────┬────────────┐   ┌──────────┬──────────┬──────────┐
│            │            │   │          │          │          │
│  Component │  Component │   │   Comp   │   Comp   │   Comp   │
│     A      │     B      │   │    A     │    B     │    C     │
│            │            │   │          │          │          │
└────────────┴────────────┘   └──────────┴──────────┴──────────┘

Nested: 50/50 → 25/25 each    Mixed: 25/25/50
┌──────┬──────┬──────┬──────┐ ┌──────┬──────┬─────────────────┐
│      │      │      │      │ │      │      │                 │
│  A   │  B   │  C   │  D   │ │  A   │  B   │       C         │
│      │      │      │      │ │      │      │                 │
└──────┴──────┴──────┴──────┘ └──────┴──────┴─────────────────┘

Complex: 33/33/33 with horizontal splits
┌──────────┬──────────┬──────────┐
│    A     │    C     │    E     │
├──────────┼──────────┼──────────┤
│    B     │    D     │    F     │
└──────────┴──────────┴──────────┘
```

### Layout Configuration Schema

```typescript
interface LayoutConfig {
  type: 'component' | 'split';

  // For type: 'component'
  component?: ComponentType;

  // For type: 'split'
  direction?: 'horizontal' | 'vertical';
  sizes?: number[];  // percentages, e.g. [25, 25, 50]
  children?: LayoutConfig[];
}

type ComponentType =
  | 'agent-chat'      // LLM requests + reasoning steps
  | 'workspace'       // File tree + file viewer
  | 'db-table'        // PostgreSQL table viewer
  | 'graph'           // Neo4j visualization
  | 'timeline'        // Timeline scrubber
  | 'job-selector'    // Job picker dropdown
  | 'metrics'         // Token usage, latency stats
  | 'logs'            // Raw log viewer
  | 'json-viewer';    // Generic JSON explorer
```

### Example Configuration

```json
{
  "type": "split",
  "direction": "vertical",
  "sizes": [25, 50, 25],
  "children": [
    { "type": "component", "component": "workspace" },
    {
      "type": "split",
      "direction": "horizontal",
      "sizes": [70, 30],
      "children": [
        { "type": "component", "component": "agent-chat" },
        { "type": "component", "component": "timeline" }
      ]
    },
    { "type": "component", "component": "db-table" }
  ]
}
```

## Pluggable Components

| Component | Description | Data Source |
|-----------|-------------|-------------|
| `agent-chat` | LLM requests with expandable reasoning steps | MongoDB `agent_audit` |
| `workspace` | File tree + content viewer | Filesystem API |
| `db-table` | PostgreSQL table browser (jobs, requirements, etc.) | PostgreSQL |
| `graph` | Neo4j knowledge graph visualization | Neo4j |
| `timeline` | Scrubber to navigate through time | All sources (timestamp filter) |
| `job-selector` | Dropdown to pick active job | PostgreSQL `jobs` |
| `metrics` | Token usage, latency, cost stats | MongoDB `agent_audit` |
| `logs` | Raw log file viewer | Filesystem `workspace/logs/` |
| `json-viewer` | Generic expandable JSON tree | Any JSON data |
| `todos` | Todo list state over time | Filesystem `todos.yaml` |

## Data Sources

| Source | Collection/Table | Purpose |
|--------|------------------|---------|
| MongoDB | `agent_audit` | LLM requests, responses, tool calls, timestamps |
| PostgreSQL | `jobs` | Job metadata (id, status, config, timestamps) |
| PostgreSQL | `requirements` | Extracted/validated requirements |
| Filesystem | `workspace/job_<id>/` | workspace.md, todos.yaml, plan.md, analysis/* |

## Components to Migrate from Advanced-LLM-Chat

### Required Components

| Component | Source Path | Purpose |
|-----------|-------------|---------|
| `agent-steps` | `src/app/chat-ui/chat-ui-message/agent-steps/` | Expandable reasoning step panels |
| `message` model | `src/app/data/objects/message.ts` | `AgentStep`, `AgentContent` types |

### Optional Components (nice-to-have)

| Component | Source Path | Purpose |
|-----------|-------------|---------|
| `chat-ui-message` | `src/app/chat-ui/chat-ui-message/` | Message bubble styling |
| `marked` integration | Uses `ngx-markdown` | Markdown rendering in steps |

## Component Architecture

### Core Framework Components

| Component | Status | Purpose |
|-----------|--------|---------|
| `App` | ✅ Implemented | Root component with timeline header + split-panel main |
| `SplitPanelComponent` | ✅ Implemented | Recursive split container with angular-split |
| `ComponentHostComponent` | ✅ Implemented | Dynamic component loader using ViewContainerRef |
| `PanelHeaderComponent` | ✅ Implemented | Header bar with component title |
| `TimelineComponent` | ✅ Implemented | Top scrubber bar (60px) with play/pause |

### Services

**Layout Services (Implemented)**

| Service | Status | Purpose |
|---------|--------|---------|
| `LayoutService` | ✅ Implemented | Signals-based layout state, save/load presets, localStorage persistence |
| `ComponentRegistryService` | ✅ Implemented | Register components, get metadata by type |

**Data Services (Partially Implemented)**

| Service | Status | Purpose |
|---------|--------|---------|
| `ApiService` | ✅ Implemented | HTTP client for table endpoints (getTables, getTableData, getTableSchema) |
| `StateService` | ✅ Implemented | Signals-based state for db-table (selected table, pagination, loading) |

The `StateService` exposes signals that components subscribe to:
```typescript
@Injectable({ providedIn: 'root' })
export class StateService {
  // Current context
  readonly currentJob = signal<Job | null>(null);
  readonly timelinePosition = signal<number>(0);        // ms from job start
  readonly timelineDuration = signal<number>(0);        // total job duration
  readonly isPlaying = signal<boolean>(false);

  // Buffered data (windowed around timeline position)
  readonly auditEntries = signal<AuditEntry[]>([]);
  readonly workspaceTree = signal<FileNode[]>([]);
  readonly selectedFile = signal<FileContent | null>(null);

  // Computed
  readonly visibleEntries = computed(() =>
    this.auditEntries().filter(e => e.timestamp <= this.timelinePosition())
  );
}
```

### Pluggable Panel Components

| Component | Status | Purpose |
|-----------|--------|---------|
| `PlaceholderAComponent` | ✅ Placeholder | Will become WorkspaceComponent |
| `PlaceholderBComponent` | ✅ Placeholder | Will become AgentChatComponent |
| `PlaceholderCComponent` | ✅ Placeholder | Legacy placeholder (replaced by DbTableComponent) |
| `DbTableComponent` | ✅ Implemented | PostgreSQL table browser with pagination |
| `AgentChatComponent` | ⬜ Planned | Message list with expandable reasoning steps |
| `WorkspaceComponent` | ⬜ Planned | File tree + file viewer |
| `GraphComponent` | ⬜ Planned | Neo4j visualization (optional: vis.js or d3) |
| `JobSelectorComponent` | ⬜ Planned | Dropdown/search to select job_id |
| `MetricsComponent` | ⬜ Planned | Token usage, latency charts |
| `LogViewerComponent` | ⬜ Planned | Raw log file with search |
| `JsonViewerComponent` | ⬜ Planned | Expandable JSON tree |
| `TodosComponent` | ⬜ Planned | Todo list state viewer |

## FastAPI Debug Backend

A dedicated FastAPI backend (`cockpit/api/`) that wraps database access for the Angular frontend.

### Endpoints

```
# Jobs (PostgreSQL)
GET  /api/jobs                         # List all jobs
GET  /api/jobs/{job_id}                # Job details + metadata

# Audit Trail (MongoDB) - supports windowed queries
GET  /api/jobs/{job_id}/audit          # Full audit trail
GET  /api/jobs/{job_id}/audit?from=<ts>&to=<ts>  # Windowed by timestamp

# Workspace Files (Filesystem)
GET  /api/jobs/{job_id}/workspace      # List workspace file tree
GET  /api/jobs/{job_id}/workspace/{path}  # Read specific file content

# Requirements (PostgreSQL)
GET  /api/jobs/{job_id}/requirements   # Requirements for job

# Direct Queries (for advanced panels)
POST /api/cypher                       # Execute Cypher query on Neo4j
     Body: { "query": "MATCH (n) RETURN n LIMIT 10" }

POST /api/sql                          # Execute SQL query on PostgreSQL
     Body: { "query": "SELECT * FROM jobs LIMIT 10" }
```

### File Structure

```
cockpit/
├── api/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, CORS setup
│   ├── routes/
│   │   ├── jobs.py          # /api/jobs/* endpoints
│   │   ├── audit.py         # /api/jobs/{id}/audit endpoints
│   │   ├── workspace.py     # /api/jobs/{id}/workspace/* endpoints
│   │   └── query.py         # /api/cypher, /api/sql endpoints
│   ├── services/
│   │   ├── postgres.py      # PostgreSQL connection + queries
│   │   ├── mongodb.py       # MongoDB connection + queries
│   │   └── neo4j.py         # Neo4j connection + queries
│   └── models/
│       └── schemas.py       # Pydantic models for request/response
└── src/                     # Angular frontend
```

### Running the Backend

```bash
cd cockpit/api
pip install -r requirements.txt  # fastapi, uvicorn, asyncpg
uvicorn main:app --reload --port 8080

# Verify with:
curl http://localhost:8080/api/tables
curl http://localhost:8080/api/tables/jobs
```

## Implementation Phases

### Phase 1: Angular Setup ✅
- [x] Create Angular 21 app in `cockpit/`
- [x] Add `angular-split` for resizable panels
- [x] Configure SCSS theming (dark mode - Catppuccin Mocha)
- [x] Set up basic app structure

### Phase 2: Layout Framework ✅
- [x] Create `LayoutService` for config management (signals-based)
- [x] Create root `App` component (layout renderer)
- [x] Create `SplitPanelComponent` (recursive splits with angular-split)
- [x] Create `ComponentHostComponent` (dynamic loader)
- [x] Create `ComponentRegistryService`
- [x] Create `PanelHeaderComponent` (title display)
- [x] Create `TimelineComponent` (top scrubber bar with play/pause)
- [x] Create `MenuComponent` (hamburger menu with links/settings)
- [x] Test with placeholder components (A, B, C)
- [x] localStorage persistence for layout

### Phase 3: FastAPI Debug Backend (In Progress)
- [x] Set up FastAPI project structure in `cockpit/api/`
- [x] Create PostgreSQL service (asyncpg) - table queries with pagination
- [x] Add CORS middleware for Angular dev server
- [x] Implement `/api/tables` endpoints (list, data, schema)
- [ ] Create MongoDB service (motor)
- [ ] Create Neo4j service (neo4j driver)
- [ ] Implement `/api/jobs` endpoints
- [ ] Implement `/api/jobs/{id}/audit` with windowed queries
- [ ] Implement `/api/jobs/{id}/workspace` endpoints
- [ ] Implement `/api/cypher` and `/api/sql` pass-through

### Phase 4: Angular Data Services (Partially Complete)
- [x] Create `ApiService` (HTTP client for table endpoints)
- [x] Create `StateService` (signals-based state for db-table)
- [ ] Extend `ApiService` for jobs, audit, workspace endpoints
- [ ] Extend `StateService` for job context and timeline
- [ ] Implement buffering/windowing logic for large audit trails
- [ ] Connect `TimelineComponent` to `StateService`
- [ ] Add job selection to state

### Phase 5: Panel Components (MVP)
- [ ] `JobSelectorComponent` - dropdown to pick job, wired to StateService
- [ ] `AgentChatComponent` - display audit entries as expandable steps
- [ ] `WorkspaceComponent` - file tree + viewer
- [ ] `JsonViewerComponent` - generic expandable JSON tree
- [ ] Wire all components to StateService signals

### Phase 6: Panel Components (Extended)
- [x] `DbTableComponent` - PostgreSQL table browser with pagination
- [ ] `GraphComponent` - Neo4j visualization (uses /api/cypher)
- [ ] `MetricsComponent` - token/latency charts from audit data
- [ ] `LogViewerComponent` - raw logs with search
- [ ] `TodosComponent` - todo state over time

### Phase 7: Copy & Adapt Chat Components (Optional)
- [ ] Copy `agent-steps` component from Advanced-LLM-Chat
- [ ] Adapt for Angular 21 standalone components
- [ ] Integrate into `AgentChatComponent`

### Phase 8: Polish & Presets
- [ ] Preset layouts (default, minimal, full, debug)
- [ ] URL-based layout sharing (?layout=preset-name)
- [ ] Keyboard shortcuts for timeline (space=play/pause, arrows=scrub)
- [ ] Add Angular Material for dropdowns/inputs (optional)

### Phase 9: Docker Integration
- [ ] Create `cockpit/Dockerfile` (multi-stage: Angular build + Python API)
- [ ] Create `cockpit/nginx.conf` (serve Angular + proxy /api to uvicorn)
- [ ] Add `debug-cockpit` service to `docker-compose.dev.yaml`
- [ ] Test full stack with `podman-compose up`

## MongoDB agent_audit Schema

```typescript
interface AuditEntry {
  _id: ObjectId;
  job_id: string;
  timestamp: Date;
  type: 'request' | 'response' | 'tool_call' | 'tool_result' | 'error';

  // For requests
  messages?: Array<{role: string, content: string}>;
  model?: string;

  // For responses
  content?: string;
  usage?: {prompt_tokens: number, completion_tokens: number};

  // For tool calls
  tool_name?: string;
  tool_input?: object;
  tool_output?: object;
}
```

## AgentStep Type (from chat app)

```typescript
interface AgentStep {
  id: string;
  title: string;
  content: string;
  type: 'thought' | 'tool_call' | 'tool_result' | 'observation';
  duration?: number;
  metadata?: Record<string, unknown>;
}
```

## Mapping: AuditEntry → AgentStep

```typescript
function auditToStep(entry: AuditEntry): AgentStep {
  switch (entry.type) {
    case 'request':
      return {
        id: entry._id.toString(),
        title: `LLM Request (${entry.model})`,
        content: entry.messages?.map(m => `${m.role}: ${m.content}`).join('\n'),
        type: 'thought',
        metadata: { model: entry.model }
      };
    case 'tool_call':
      return {
        id: entry._id.toString(),
        title: `Tool: ${entry.tool_name}`,
        content: JSON.stringify(entry.tool_input, null, 2),
        type: 'tool_call',
        metadata: entry.tool_input
      };
    case 'tool_result':
      return {
        id: entry._id.toString(),
        title: `Result: ${entry.tool_name}`,
        content: JSON.stringify(entry.tool_output, null, 2),
        type: 'tool_result',
        metadata: entry.tool_output
      };
    // ...
  }
}
```

## Docker Configuration

### Dockerfile

```dockerfile
# Build stage
FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

# Production stage
FROM nginx:alpine
COPY --from=build /app/dist/cockpit/browser /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
```

### docker-compose.dev.yaml addition

```yaml
services:
  debug-cockpit:
    build: ./cockpit
    ports:
      - "4200:80"
    depends_on:
      - mongodb
      - postgres
    environment:
      - API_URL=http://host.docker.internal:8001
```

## Preset Layouts

### `default` - Balanced 3-column
```
┌──────────┬─────────────────┬──────────┐
│          │                 │          │
│ Workspace│   Agent Chat    │ DB Table │
│   25%    │      50%        │   25%    │
│          │                 │          │
├──────────┴─────────────────┴──────────┤
│              Timeline                  │
└────────────────────────────────────────┘
```

### `minimal` - Chat focus
```
┌────────────────────────────────────────┐
│ Job Selector                           │
├────────────────────────────────────────┤
│                                        │
│             Agent Chat                 │
│                                        │
├────────────────────────────────────────┤
│              Timeline                  │
└────────────────────────────────────────┘
```

### `full` - Everything visible
```
┌──────────┬─────────────────┬──────────┐
│ Job      │                 │ Metrics  │
│ Selector │   Agent Chat    ├──────────┤
├──────────┤                 │ Todos    │
│          │                 │          │
│ Workspace├─────────────────┼──────────┤
│          │    DB Table     │  Graph   │
└──────────┴─────────────────┴──────────┘
│              Timeline                  │
└────────────────────────────────────────┘
```

### `debug` - Logs and raw data
```
┌─────────────────┬─────────────────────┐
│                 │                     │
│   Log Viewer    │    JSON Viewer      │
│                 │                     │
├─────────────────┴─────────────────────┤
│              Timeline                 │
└───────────────────────────────────────┘
```

## Decisions Made

1. **Backend API location**: ✅ Separate FastAPI backend in `cockpit/api/` (not added to existing agents)
2. **Data flow**: ✅ StateService (signals) → ApiService (HTTP) → FastAPI → Databases
3. **Large runs**: ✅ Windowed loading with buffering (±5 min around timeline position)

## Open Questions

1. **Authentication**: Should the debug cockpit require auth, or is it dev-only?
2. **Real-time updates**: Should the cockpit poll for updates on running jobs, or is historical replay enough?
3. **Layout editor**: Should users be able to drag-and-drop to rearrange panels, or just pick presets?
4. **Caching**: Should the FastAPI backend cache recent queries, or always hit the databases?
