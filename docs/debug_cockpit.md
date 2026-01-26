# Debug Cockpit - Implementation Plan

## Overview

A flexible, composable Angular dashboard framework for debugging and visualizing agent execution. Features a tiling layout system with pluggable components that can be arranged in any configuration.

## Current Status

**Phase 4 In Progress** - FastAPI backend with PostgreSQL and MongoDB, three real components implemented.

Run the app:
```bash
# Terminal 1: Start FastAPI backend
cd cockpit/api
pip install -r requirements.txt
uvicorn main:app --reload --port 8085

# Terminal 2: Start Angular frontend
cd cockpit && npm start
# Open http://localhost:4200
```

Current features:
- Dark themed UI (Catppuccin Mocha)
- Timeline scrubber bar at top (60px) with play/pause button
- Hamburger menu with database links, layout presets, and settings
- **Fully configurable layout system:**
  - Drag handles between panels for resizing
  - **Component switcher dropdown** in each panel header (click title to swap components)
  - **Split buttons** to divide panels horizontally (top/bottom) or vertically (left/right)
  - **Close button** to remove panels (redistributes space to siblings)
  - Layout persistence to localStorage
  - **Preset layouts** loaded from JSON files in `assets/layout-presets/`
- **PostgreSQL Table Viewer** (DbTableComponent):
  - Table tabs: jobs, requirements, sources, citations
  - Paginated data display with column headers
  - Cell formatting by type (dates, JSON, booleans, etc.)
  - Loading spinner and error states
  - Works offline (shows empty state with backend hint)
- **Agent Activity Viewer** (AgentActivityComponent):
  - Job selector dropdown populated from PostgreSQL
  - Audit trail from MongoDB `agent_audit` collection
  - Filter buttons: All, Messages, Tools, Errors
  - Expandable entries with step-type specific details
  - Color-coded step badges (INIT, LLM, TOOL, CHECK, ROUTE, PHASE, ERROR)
  - Clickable request_id links in LLM response entries (auto-loads in Request Viewer)
  - Pagination with page navigation
  - Graceful degradation when MongoDB unavailable
- **Request Viewer** (RequestViewerComponent):
  - Document ID search field with validation (24 hex chars)
  - Metadata card: ID, job_id, model, iteration, latency, token counts
  - Messenger-style message display with role-based colors:
    - System: gray background, left-aligned
    - Human/User: green background, right-aligned
    - Assistant: blue background, left-aligned
    - Tool: purple background, monospace font
  - Tool calls display with JSON-formatted arguments
  - Collapsible reasoning section (for models with reasoning tokens)
  - Loading spinner and error states
  - Auto-load via clicking request_id in Agent Activity

## Data Architecture

The cockpit uses a three-layer architecture to fetch and manage agent state:

```
┌─────────────────────────────────────────────────────────┐
│                    Angular Frontend                      │
│  ┌─────────────────────────────────────────────────┐    │
│  │              Visual Components                   │    │
│  │  (RequestViewer, AgentActivity, DbTable, etc.)  │    │
│  └──────────────────────┬──────────────────────────┘    │
│                         │ subscribe to signals           │
│  ┌──────────────────────▼──────────────────────────┐    │
│  │         Services (Signals-based state)           │    │
│  │  - StateService: db-table state                  │    │
│  │  - AuditService: agent-activity state            │    │
│  │  - RequestService: request-viewer state          │    │
│  │  - handles buffering/windowing for large runs   │    │
│  └──────────────────────┬──────────────────────────┘    │
│                         │ HTTP calls                     │
│  ┌──────────────────────▼──────────────────────────┐    │
│  │              ApiService                          │    │
│  │  - getJobs(): Observable<Job[]>                  │    │
│  │  - getAuditWindow(jobId, from, to)              │    │
│  │  - getRequest(docId): Observable<LLMRequest>    │    │
│  │  - getWorkspaceFile(jobId, path)                │    │
│  │  - queryCypher(query), querySQL(query)          │    │
│  └──────────────────────┬──────────────────────────┘    │
└─────────────────────────┼───────────────────────────────┘
                          │ HTTP :8085
┌─────────────────────────▼───────────────────────────────┐
│              FastAPI Debug Backend                       │
│                                                          │
│  GET  /api/jobs                     → PostgreSQL         │
│  GET  /api/jobs/{id}                → PostgreSQL         │
│  GET  /api/jobs/{id}/audit          → MongoDB            │
│       ?from=<ts>&to=<ts>              (windowed query)   │
│  GET  /api/requests/{doc_id}        → MongoDB            │
│                                        (llm_requests)    │
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
│   │   ├── layout.model.ts           # LayoutConfig, ComponentType, ComponentMetadata ✅
│   │   ├── layout-preset.model.ts    # LayoutPreset interface ✅
│   │   ├── api.model.ts              # TableInfo, ColumnDef, TableDataResponse ✅
│   │   ├── audit.model.ts            # AuditEntry, AuditResponse, JobSummary ✅
│   │   └── request.model.ts          # LLMRequest, LLMMessage, LLMToolCall ✅
│   └── services/
│       ├── layout.service.ts         # Layout state, split/close, presets ✅
│       ├── component-registry.service.ts  # Component registration ✅
│       ├── api.service.ts            # HTTP client for backend ✅
│       ├── state.service.ts          # Signals state for db-table ✅
│       ├── audit.service.ts          # Signals state for agent-activity ✅
│       └── request.service.ts        # Signals state for request-viewer ✅
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
│   ├── agent-activity/
│   │   └── agent-activity.component.ts # MongoDB audit trail viewer ✅
│   ├── request-viewer/
│   │   └── request-viewer.component.ts # LLM request/response viewer ✅
│   └── placeholders/
│       ├── placeholder-a.component.ts  # "Workspace" placeholder
│       └── placeholder-b.component.ts  # "Agent Chat" placeholder
├── app.ts                            # Root frame component
├── app.config.ts                     # App configuration (+ provideHttpClient)
└── app.routes.ts                     # Routing (unused for now)

cockpit/src/
├── styles.scss                       # Dark theme CSS variables
└── assets/
    └── layout-presets/               # JSON preset files ✅
        ├── single.json               # Single full-width panel
        ├── two-column.json           # 50/50 vertical split
        ├── two-row.json              # 50/50 horizontal split
        ├── three-column.json         # Default 25/50/25 layout
        ├── left-right-stack.json     # Left + stacked right
        └── grid-2x2.json             # 2x2 grid layout
```

### FastAPI Backend (Implemented)

```
cockpit/api/
├── __init__.py                       # ✅
├── main.py                           # FastAPI app, CORS, all endpoints ✅
├── requirements.txt                  # fastapi, uvicorn, asyncpg, motor ✅
├── .env                              # DATABASE_URL, MONGODB_URL
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
│   ├── mongodb.py                    # motor async client ✅
│   └── neo4j.py                      # neo4j async driver (planned)
└── models/
    ├── __init__.py
    └── schemas.py                    # Pydantic models (planned)
```

**Current API Endpoints:**
- `GET /api/tables` - List available tables with row counts
- `GET /api/tables/{name}` - Paginated table data with columns
- `GET /api/tables/{name}/schema` - Column definitions
- `GET /api/jobs` - List jobs with audit counts (PostgreSQL + MongoDB)
- `GET /api/jobs/{id}` - Single job details with audit count
- `GET /api/jobs/{id}/audit` - Paginated audit entries (MongoDB)
  - Query params: `page`, `pageSize`, `filter` (all/messages/tools/errors)
- `GET /api/requests/{doc_id}` - Single LLM request document (MongoDB `llm_requests`)
- `GET /api/health` - Health check

## Architecture

### Panel Header Controls

Each panel header provides controls for customizing the layout:

```
┌─────────────────────────────────────────────────────────────┐
│ [v] AGENT ACTIVITY                           [━] [┃] [✕]   │
│     └─ dropdown                               │    │    │   │
│        (click to switch component)            │    │    │   │
│                                  split horiz ─┘    │    │   │
│                                  split vert ───────┘    │   │
│                                  close panel ───────────┘   │
└─────────────────────────────────────────────────────────────┘
```

| Control | Icon | Action |
|---------|------|--------|
| Component dropdown | ▼ | Click panel title to switch to a different component |
| Split horizontal | ━ | Split panel into top/bottom (50/50) |
| Split vertical | ┃ | Split panel into left/right (50/50) |
| Close | ✕ | Remove panel, redistribute space (hidden when only 1 panel) |

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
  | 'agent-activity'  // Job selector + audit trail (implemented ✅)
  | 'db-table'        // PostgreSQL table viewer (implemented ✅)
  | 'request-viewer'  // LLM request/response viewer (implemented ✅)
  | 'workspace'       // File tree + file viewer
  | 'graph'           // Neo4j visualization
  | 'timeline'        // Timeline scrubber
  | 'metrics'         // Token usage, latency stats
  | 'logs'            // Raw log viewer
  | 'json-viewer';    // Generic JSON explorer
```

### Example Configuration (Current Default)

```json
{
  "type": "split",
  "direction": "vertical",
  "sizes": [25, 50, 25],
  "children": [
    { "type": "component", "component": "request-viewer" },
    { "type": "component", "component": "agent-activity" },
    { "type": "component", "component": "db-table" }
  ]
}
```

## Pluggable Components

| Component | Description | Data Source | Status |
|-----------|-------------|-------------|--------|
| `agent-activity` | Job selector + audit trail with expandable steps | PostgreSQL + MongoDB | ✅ |
| `db-table` | PostgreSQL table browser (jobs, requirements, etc.) | PostgreSQL | ✅ |
| `request-viewer` | LLM request/response conversation viewer | MongoDB `llm_requests` | ✅ |
| `workspace` | File tree + content viewer | Filesystem API | ⬜ |
| `graph` | Neo4j knowledge graph visualization | Neo4j | ⬜ |
| `timeline` | Scrubber to navigate through time | All sources (timestamp filter) | ✅ (basic) |
| `metrics` | Token usage, latency, cost stats | MongoDB `agent_audit` | ⬜ |
| `logs` | Raw log file viewer | Filesystem `workspace/logs/` | ⬜ |
| `json-viewer` | Generic expandable JSON tree | Any JSON data | ⬜ |
| `todos` | Todo list state over time | Filesystem `todos.yaml` | ⬜ |

## Data Sources

| Source | Collection/Table | Purpose |
|--------|------------------|---------|
| MongoDB | `agent_audit` | Audit trail: LLM calls, tool executions, phase transitions |
| MongoDB | `llm_requests` | Full LLM request/response conversations with messages |
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
| `ComponentHostComponent` | ✅ Implemented | Dynamic component loader with path tracking |
| `PanelHeaderComponent` | ✅ Implemented | Header bar with dropdown, split, and close buttons |
| `TimelineComponent` | ✅ Implemented | Top scrubber bar (60px) with play/pause |

### Services

**Layout Services (Implemented)**

| Service | Status | Purpose |
|---------|--------|---------|
| `LayoutService` | ✅ Implemented | Layout state, split/close panels, preset loading, localStorage persistence |
| `ComponentRegistryService` | ✅ Implemented | Register components, get metadata by type |

**LayoutService Methods:**
- `setLayout(config)` - Set layout configuration
- `resetLayout()` - Restore default layout
- `updateSizes(path, sizes)` - Update split sizes at path
- `updateComponent(path, type)` - Change component at path
- `splitPanel(path, direction)` - Split panel into two (horizontal/vertical)
- `closePanel(path)` - Remove panel and redistribute space
- `getPanelCount()` - Count total panels
- `applyPreset(presetId)` - Apply a preset layout
- `availablePresets` - Signal with loaded presets from JSON files

**Data Services (Implemented)**

| Service | Status | Purpose |
|---------|--------|---------|
| `ApiService` | ✅ Implemented | HTTP client for tables, jobs, audit, requests endpoints |
| `StateService` | ✅ Implemented | Signals-based state for db-table (selected table, pagination, loading) |
| `AuditService` | ✅ Implemented | Signals-based state for agent-activity (jobs, entries, filters, pagination) |
| `RequestService` | ✅ Implemented | Signals-based state for request-viewer (current request, loading, error) |

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
| `DbTableComponent` | ✅ Implemented | PostgreSQL table browser with pagination |
| `AgentActivityComponent` | ✅ Implemented | MongoDB audit trail with job selector, filters, expandable steps |
| `RequestViewerComponent` | ✅ Implemented | LLM request/response viewer with messenger-style messages |
| `WorkspaceComponent` | ⬜ Planned | File tree + file viewer |
| `GraphComponent` | ⬜ Planned | Neo4j visualization (optional: vis.js or d3) |
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

# LLM Requests (MongoDB) - full request/response conversations
GET  /api/requests/{doc_id}            # Single request by ObjectId

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
uvicorn main:app --reload --port 8085

# Verify with:
curl http://localhost:8085/api/tables
curl http://localhost:8085/api/tables/jobs
curl http://localhost:8085/api/requests/<doc_id>  # Get a doc_id from llm_requests
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

### Phase 3: FastAPI Debug Backend ✅
- [x] Set up FastAPI project structure in `cockpit/api/`
- [x] Create PostgreSQL service (asyncpg) - table queries with pagination
- [x] Add CORS middleware for Angular dev server
- [x] Implement `/api/tables` endpoints (list, data, schema)
- [x] Create MongoDB service (motor) - async with graceful degradation
- [x] Implement `/api/jobs` endpoints (list, single with audit counts)
- [x] Implement `/api/jobs/{id}/audit` with pagination and filtering
- [ ] Create Neo4j service (neo4j driver)
- [ ] Implement `/api/jobs/{id}/workspace` endpoints
- [ ] Implement `/api/cypher` and `/api/sql` pass-through

### Phase 4: Angular Data Services (In Progress)
- [x] Create `ApiService` (HTTP client for table endpoints)
- [x] Create `StateService` (signals-based state for db-table)
- [x] Extend `ApiService` for jobs, audit endpoints
- [x] Create `AuditService` for job/audit state management
- [ ] Extend `ApiService` for workspace endpoints
- [ ] Implement buffering/windowing logic for large audit trails
- [ ] Connect `TimelineComponent` to `StateService`

### Phase 5: Panel Components (MVP)
- [x] `AgentActivityComponent` - job selector + audit entries with expandable steps
- [x] `RequestViewerComponent` - LLM request/response viewer with messenger-style messages
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
- [x] Preset layouts loaded from JSON files (single, two-column, two-row, three-column, left-right-stack, grid-2x2)
- [x] Component switcher dropdown in panel headers
- [x] Split/close panel buttons for dynamic layout building
- [ ] URL-based layout sharing (?layout=preset-name)
- [ ] Keyboard shortcuts for timeline (space=play/pause, arrows=scrub)
- [ ] Add Angular Material for dropdowns/inputs (optional)

### Phase 9: Docker Integration
- [ ] Create `cockpit/Dockerfile` (multi-stage: Angular build + Python API)
- [ ] Create `cockpit/nginx.conf` (serve Angular + proxy /api to uvicorn)
- [ ] Add `debug-cockpit` service to `docker-compose.dev.yaml`
- [ ] Test full stack with `podman-compose up`

## MongoDB agent_audit Schema

The `agent_audit` collection in the `graphrag_logs` database stores all agent execution steps:

```typescript
interface AuditEntry {
  _id: ObjectId;
  job_id: string;
  agent_type: string;           // "creator", "validator"
  step_number: number;          // Sequential step counter per job
  step_type: AuditStepType;
  node_name: string;            // Graph node: "init_workspace", "execute", "tools", "check_todos"
  timestamp: Date;
  iteration: number;
  latency_ms?: number;
  phase?: string;
  metadata?: Record<string, unknown>;

  // Step-type specific data (merged at top level):

  // For step_type="initialize"
  workspace?: { created: boolean };
  phase_alternation?: boolean;
  strategic_todos?: number;
  instructions_length?: number;

  // For step_type="llm_call"
  llm?: {
    model: string;
    input_message_count: number;
  };
  state?: { message_count: number };

  // For step_type="llm_response"
  llm?: {
    model: string;
    response_content_preview: string;  // First 500 chars
    tool_calls: Array<{ name: string; call_id: string }>;
    metrics: { output_chars: number; tool_call_count: number };
  };

  // For step_type="tool_call"
  tool?: {
    name: string;
    call_id: string;
    arguments: Record<string, unknown>;  // Truncated to 200 chars per value
  };

  // For step_type="tool_result"
  tool?: {
    name: string;
    call_id: string;
    result_preview: string;     // First 500 chars
    result_size_bytes: number;
    success: boolean;
    error?: string;
  };

  // For step_type="error"
  error?: {
    type: string;
    message: string;
    recoverable?: boolean;
    attempts?: number;
  };
}

type AuditStepType =
  | 'initialize'      // Workspace/todo init
  | 'llm_call'        // Before LLM invocation
  | 'llm_response'    // After LLM response
  | 'tool_call'       // Before tool execution
  | 'tool_result'     // After tool execution
  | 'check'           // Decision checkpoints
  | 'routing'         // Phase transitions
  | 'phase_complete'  // Phase completed
  | 'error';          // Error events
```

## MongoDB llm_requests Schema

The `llm_requests` collection stores full LLM request/response conversations:

```typescript
interface LLMRequest {
  _id: ObjectId;
  job_id: string;
  agent_type: string;           // "creator", "validator"
  timestamp: Date;
  model: string;
  iteration?: number;
  latency_ms?: number;
  request: {
    messages: LLMMessage[];
    message_count: number;
  };
  response: LLMMessage;
  metrics?: {
    input_chars: number;
    output_chars: number;
    tool_calls: number;
    token_usage?: {
      prompt_tokens?: number;
      completion_tokens?: number;
      reasoning_tokens?: number;
    };
  };
}

interface LLMMessage {
  type: string;           // "SystemMessage", "HumanMessage", "AIMessage", "ToolMessage"
  role: string;           // "system", "human", "assistant", "tool"
  content: string;
  tool_calls?: Array<{    // Only for AIMessage
    id: string;
    name: string;
    args: Record<string, unknown>;
  }>;
  tool_call_id?: string;  // Only for ToolMessage
  name?: string;          // Only for ToolMessage
  additional_kwargs?: {
    reasoning_content?: string;  // Model reasoning (for supported models)
  };
}
```

### Linking Audit Entries to Requests

The `llm_response` audit entries include a `request_id` field that links to the `llm_requests` collection:

```typescript
// In agent_audit collection, llm_response entries:
{
  step_type: "llm_response",
  llm: {
    model: "openai/gpt-4o",
    request_id: "679abc123...",  // Links to llm_requests._id
    response_content_preview: "...",
    // ...
  }
}
```

This enables clicking on request_id in the Agent Activity panel to load the full conversation in the Request Viewer.

### Filter Mappings

The API supports filtering audit entries by category:

| Filter | Step Types |
|--------|------------|
| `all` | No filtering |
| `messages` | `llm_call`, `llm_response` |
| `tools` | `tool_call`, `tool_result` |
| `errors` | `error` |

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

Presets are loaded from JSON files in `cockpit/src/assets/layout-presets/`. Users can select them from the hamburger menu under "Layouts".

### Available Presets

| Preset | Description |
|--------|-------------|
| `single` | Single full-width panel |
| `two-column` | 50/50 vertical split |
| `two-row` | 50/50 horizontal split (top/bottom) |
| `three-column` | Default 25/50/25 layout |
| `left-right-stack` | Left panel + two stacked on right |
| `grid-2x2` | Four equal panels in a 2x2 grid |

### `three-column` (Default)
```
┌──────────┬─────────────────┬──────────┐
│          │                 │          │
│ Request  │ Agent Activity  │ DB Table │
│ Viewer   │      50%        │   25%    │
│   25%    │                 │          │
├──────────┴─────────────────┴──────────┤
│              Timeline                  │
└────────────────────────────────────────┘
```

### `single`
```
┌────────────────────────────────────────┐
│                                        │
│           Agent Activity               │
│              100%                      │
│                                        │
├────────────────────────────────────────┤
│              Timeline                  │
└────────────────────────────────────────┘
```

### `left-right-stack`
```
┌───────────────────┬────────────────────┐
│                   │   Request Viewer   │
│  Agent Activity   ├────────────────────┤
│       50%         │     DB Table       │
│                   │                    │
├───────────────────┴────────────────────┤
│              Timeline                  │
└────────────────────────────────────────┘
```

### `grid-2x2`
```
┌───────────────────┬────────────────────┐
│  Agent Activity   │  Request Viewer    │
├───────────────────┼────────────────────┤
│     DB Table      │  Agent Activity    │
├───────────────────┴────────────────────┤
│              Timeline                  │
└────────────────────────────────────────┘
```

### Adding Custom Presets

Create a JSON file in `cockpit/src/assets/layout-presets/`:

```json
{
  "id": "my-preset",
  "name": "My Custom Layout",
  "description": "Optional description",
  "config": {
    "type": "split",
    "direction": "vertical",
    "sizes": [50, 50],
    "children": [
      { "type": "component", "component": "agent-activity" },
      { "type": "component", "component": "db-table" }
    ]
  }
}
```

Then add the filename (without `.json`) to the `presetFiles` array in `layout.service.ts`.

## Decisions Made

1. **Backend API location**: ✅ Separate FastAPI backend in `cockpit/api/` (not added to existing agents)
2. **Data flow**: ✅ StateService (signals) → ApiService (HTTP) → FastAPI → Databases
3. **Large runs**: ✅ Windowed loading with buffering (±5 min around timeline position)
4. **Layout editor**: ✅ Split/close buttons + component dropdown for dynamic layouts, plus JSON presets for quick setup

## Open Questions

1. **Authentication**: Should the debug cockpit require auth, or is it dev-only?
2. **Real-time updates**: Should the cockpit poll for updates on running jobs, or is historical replay enough?
3. **Caching**: Should the FastAPI backend cache recent queries, or always hit the databases?
