# Debug Cockpit - Implementation Plan

## Overview

A flexible, composable Angular dashboard framework for debugging and visualizing agent execution. Features a tiling layout system with pluggable components that can be arranged in any configuration.

## Current Status

**Phase 5 Complete** - All MVP components implemented. Graph timeline visualization and todo viewer added.

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
- **Timeline bar (60px)** with:
  - Hamburger menu with database links, layout presets, and settings
  - **Centralized job selector** (single source of truth for job selection)
  - Refresh button for job list
  - Play/pause button (placeholder for future playback)
  - Time display and scrubber (synced to job audit time range)
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
- **Graph Timeline** (GraphTimelineComponent):
  - Cytoscape.js visualization with fcose layout
  - Timeline scrubber to step through graph operations
  - Summary bar: total operations, nodes created/modified/deleted
  - Current Cypher query display
  - Legend for node states (created, modified, deleted)
  - Snapshot/delta optimization for fast seeking
  - Fit and re-layout controls
- **Todo List Viewer** (TodoListComponent):
  - Dropdown to select current phase or archived phases
  - Progress bar showing completed/total todos
  - Todo list with status icons (pending, in_progress, completed)
  - Expandable notes and failure reasons
  - Phase metadata display (timestamps, summary)
  - Auto-loads when job is selected

## Data Architecture

The cockpit uses a three-layer architecture to fetch and manage agent state:

```
┌─────────────────────────────────────────────────────────┐
│                    Angular Frontend                      │
│  ┌─────────────────────────────────────────────────┐    │
│  │              Visual Components                   │    │
│  │  (RequestViewer, AgentActivity, DbTable,        │    │
│  │   GraphTimeline, TodoList)                      │    │
│  └──────────────────────┬──────────────────────────┘    │
│                         │ subscribe to signals           │
│  ┌──────────────────────▼──────────────────────────┐    │
│  │         Services (Signals-based state)           │    │
│  │  - StateService: db-table state                  │    │
│  │  - AuditService: agent-activity state            │    │
│  │  - RequestService: request-viewer state          │    │
│  │  - TimeService: timeline synchronization         │    │
│  │  - GraphService: graph timeline state            │    │
│  │  - TodoService: todo list state                  │    │
│  └──────────────────────┬──────────────────────────┘    │
│                         │ HTTP calls                     │
│  ┌──────────────────────▼──────────────────────────┐    │
│  │              ApiService                          │    │
│  │  - getJobs(): Observable<Job[]>                  │    │
│  │  - getJobAudit(jobId, page, filter)             │    │
│  │  - getRequest(docId): Observable<LLMRequest>    │    │
│  │  - getAuditTimeRange(jobId)                     │    │
│  │  - getGraphChanges(jobId)                       │    │
│  │  - getJobTodos(jobId)                           │    │
│  └──────────────────────┬──────────────────────────┘    │
└─────────────────────────┼───────────────────────────────┘
                          │ HTTP :8085
┌─────────────────────────▼───────────────────────────────┐
│              FastAPI Debug Backend                       │
│                                                          │
│  GET  /api/jobs                     → PostgreSQL         │
│  GET  /api/jobs/{id}                → PostgreSQL         │
│  GET  /api/jobs/{id}/audit          → MongoDB            │
│  GET  /api/jobs/{id}/audit/timerange → MongoDB           │
│  GET  /api/requests/{doc_id}        → MongoDB            │
│  GET  /api/graph/changes/{job_id}   → MongoDB            │
│  GET  /api/jobs/{id}/todos          → Filesystem         │
│  GET  /api/jobs/{id}/todos/current  → Filesystem         │
│  GET  /api/jobs/{id}/todos/archives → Filesystem         │
│  GET  /api/tables                   → PostgreSQL         │
│  GET  /api/tables/{name}            → PostgreSQL         │
│                                                          │
└────────────┬────────────┬────────────┬──────────────────┘
             │            │            │
             ▼            ▼            ▼
         PostgreSQL    MongoDB      Filesystem
          :5432        :27017     workspace/job_*
```

### Buffering Strategy for Large Runs

For long-running jobs (e.g., 20+ hours), loading the entire audit trail would consume too much memory. The `AuditService` implements pagination:

1. **Pagination**: Loads audit entries page by page (50 per page default)
2. **Filtering**: Server-side filtering by step type category (all/messages/tools/errors)
3. **Time Range**: `TimeService` fetches job start/end times for scrubber positioning

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
│   │   ├── request.model.ts          # LLMRequest, LLMMessage, LLMToolCall ✅
│   │   ├── graph.model.ts            # GraphChanges, GraphDelta, GraphSnapshot ✅
│   │   └── todo.model.ts             # TodoItem, CurrentTodos, JobTodos ✅
│   └── services/
│       ├── layout.service.ts         # Layout state, split/close, presets ✅
│       ├── component-registry.service.ts  # Component registration ✅
│       ├── api.service.ts            # HTTP client for backend ✅
│       ├── state.service.ts          # Signals state for db-table ✅
│       ├── audit.service.ts          # Signals state for agent-activity ✅
│       ├── request.service.ts        # Signals state for request-viewer ✅
│       ├── time.service.ts           # Global time synchronization ✅
│       ├── graph.service.ts          # Graph timeline state ✅
│       └── todo.service.ts           # Todo list state ✅
├── layout/
│   ├── split-panel/
│   │   └── split-panel.component.ts  # Recursive split renderer ✅
│   ├── component-host/
│   │   └── component-host.component.ts # Dynamic component loader ✅
│   └── panel-header/
│       └── panel-header.component.ts # Panel title bar ✅
├── components/
│   ├── menu/
│   │   └── menu.component.ts         # Hamburger menu with links/settings ✅
│   ├── timeline/
│   │   └── timeline.component.ts     # Top bar with job selector + scrubber ✅
│   ├── db-table/
│   │   └── db-table.component.ts     # PostgreSQL table viewer ✅
│   ├── agent-activity/
│   │   └── agent-activity.component.ts # MongoDB audit trail viewer ✅
│   ├── request-viewer/
│   │   └── request-viewer.component.ts # LLM request/response viewer ✅
│   ├── graph-timeline/
│   │   ├── graph-timeline.component.ts # Cytoscape graph viewer ✅
│   │   ├── graph-styles.ts           # Cytoscape style definitions ✅
│   │   └── timeline-renderer.ts      # Snapshot/delta rendering logic ✅
│   ├── todo-list/
│   │   └── todo-list.component.ts    # Todo list viewer ✅
│   └── placeholders/
│       ├── placeholder-a.component.ts  # "Workspace" placeholder
│       ├── placeholder-b.component.ts  # "Agent Chat" placeholder
│       └── placeholder-c.component.ts  # "Database" placeholder
├── app.ts                            # Root frame component ✅
├── app.config.ts                     # App configuration (+ provideHttpClient) ✅
└── app.routes.ts                     # Routing (unused for now)

cockpit/src/
├── styles.scss                       # Dark theme CSS variables ✅
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
├── main.py                           # FastAPI app, CORS, endpoints ✅
├── graph_routes.py                   # /api/graph/* endpoints ✅
├── requirements.txt                  # fastapi, uvicorn, asyncpg, motor ✅
├── .env                              # DATABASE_URL, MONGODB_URL
└── services/
    ├── __init__.py                   # ✅
    ├── postgres.py                   # asyncpg connection pool ✅
    ├── mongodb.py                    # motor async client ✅
    └── workspace.py                  # Workspace file access ✅
```

**Current API Endpoints:**

| Endpoint | Method | Source | Description |
|----------|--------|--------|-------------|
| `/api/tables` | GET | PostgreSQL | List available tables with row counts |
| `/api/tables/{name}` | GET | PostgreSQL | Paginated table data with columns |
| `/api/tables/{name}/schema` | GET | PostgreSQL | Column definitions |
| `/api/jobs` | GET | PostgreSQL + MongoDB | List jobs with audit counts |
| `/api/jobs/{id}` | GET | PostgreSQL + MongoDB | Single job details |
| `/api/jobs/{id}/audit` | GET | MongoDB | Paginated audit entries (filter: all/messages/tools/errors) |
| `/api/jobs/{id}/audit/timerange` | GET | MongoDB | First/last timestamps for job |
| `/api/requests/{doc_id}` | GET | MongoDB | Single LLM request document |
| `/api/graph/changes/{job_id}` | GET | MongoDB | Parsed Cypher operations with snapshots/deltas |
| `/api/jobs/{job_id}/todos` | GET | Filesystem | All todos (current + archives) |
| `/api/jobs/{job_id}/todos/current` | GET | Filesystem | Current todos.yaml |
| `/api/jobs/{job_id}/todos/archives` | GET | Filesystem | List archived todo files |
| `/api/jobs/{job_id}/todos/archives/{filename}` | GET | Filesystem | Specific archived todo file |
| `/api/health` | GET | - | Health check |

## Architecture

### Timeline Bar Layout

The top bar provides centralized controls for job selection and time navigation:

```
┌────────────────────────────────────────────────────────────────────────┐
│ [☰] │ [Job Dropdown ▼] [↻] │ [▶] [00:00] [═══════════════] [03:45]    │
│      │                      │                                          │
│ Menu │   Job Selection      │           Playback Controls              │
└────────────────────────────────────────────────────────────────────────┘
```

| Control | Description |
|---------|-------------|
| ☰ Menu | Database links, layout presets, settings |
| Job Dropdown | Select job to view (fetches from `/api/jobs`) |
| ↻ Refresh | Reload job list |
| ▶ Play/Pause | Toggle playback (placeholder) |
| Time Display | Current position / total duration |
| Scrubber | Drag to seek through time range |

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
  | 'agent-activity'   // Audit trail viewer ✅
  | 'db-table'         // PostgreSQL table viewer ✅
  | 'request-viewer'   // LLM request/response viewer ✅
  | 'graph-timeline'   // Neo4j graph visualization ✅
  | 'todo-list'        // Todo list viewer ✅
  | 'placeholder-a'    // Workspace (placeholder)
  | 'placeholder-b'    // Agent Chat (placeholder)
  | 'placeholder-c'    // Database (placeholder)
  | 'workspace'        // File tree + file viewer (planned)
  | 'metrics'          // Token usage, latency stats (planned)
  | 'logs'             // Raw log viewer (planned)
  | 'json-viewer';     // Generic JSON explorer (planned)
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
| `agent-activity` | Audit trail with expandable steps | MongoDB `agent_audit` | ✅ |
| `db-table` | PostgreSQL table browser | PostgreSQL | ✅ |
| `request-viewer` | LLM request/response conversation viewer | MongoDB `llm_requests` | ✅ |
| `graph-timeline` | Neo4j graph visualization with timeline | MongoDB (Cypher parsing) | ✅ |
| `todo-list` | Todo list viewer (current + archived) | Filesystem `todos.yaml` | ✅ |
| `workspace` | File tree + content viewer | Filesystem API | ⬜ Planned |
| `metrics` | Token usage, latency, cost stats | MongoDB `agent_audit` | ⬜ Planned |
| `logs` | Raw log file viewer | Filesystem `workspace/logs/` | ⬜ Planned |
| `json-viewer` | Generic expandable JSON tree | Any JSON data | ⬜ Planned |

## Data Sources

| Source | Collection/Table | Purpose |
|--------|------------------|---------|
| MongoDB | `agent_audit` | Audit trail: LLM calls, tool executions, phase transitions |
| MongoDB | `llm_requests` | Full LLM request/response conversations with messages |
| PostgreSQL | `jobs` | Job metadata (id, status, config, timestamps) |
| PostgreSQL | `requirements` | Extracted/validated requirements |
| Filesystem | `workspace/job_<id>/` | workspace.md, todos.yaml, plan.md, analysis/* |
| Filesystem | `workspace/job_<id>/archive/` | Archived todos (todos_phase*.md) |

## Component Architecture

### Core Framework Components

| Component | Status | Purpose |
|-----------|--------|---------|
| `App` | ✅ | Root component with timeline header + split-panel main |
| `SplitPanelComponent` | ✅ | Recursive split container with angular-split |
| `ComponentHostComponent` | ✅ | Dynamic component loader with path tracking |
| `PanelHeaderComponent` | ✅ | Header bar with dropdown, split, and close buttons |
| `TimelineComponent` | ✅ | Top bar with job selector, play/pause, scrubber |
| `MenuComponent` | ✅ | Hamburger menu with links/settings |

### Services

**Layout Services**

| Service | Status | Purpose |
|---------|--------|---------|
| `LayoutService` | ✅ | Layout state, split/close panels, preset loading, localStorage persistence |
| `ComponentRegistryService` | ✅ | Register components, get metadata by type |

**Data Services**

| Service | Status | Purpose |
|---------|--------|---------|
| `ApiService` | ✅ | HTTP client for all backend endpoints |
| `StateService` | ✅ | Signals-based state for db-table |
| `AuditService` | ✅ | Signals-based state for agent-activity (jobs, entries, filters, pagination) |
| `RequestService` | ✅ | Signals-based state for request-viewer |
| `TimeService` | ✅ | Global time synchronization (start/end times, current offset, playhead) |
| `GraphService` | ✅ | Graph timeline state (deltas, snapshots, current index) |
| `TodoService` | ✅ | Todo list state (current todos, archives, selected phase) |

### Pluggable Panel Components

| Component | Status | Purpose |
|-----------|--------|---------|
| `DbTableComponent` | ✅ | PostgreSQL table browser with pagination |
| `AgentActivityComponent` | ✅ | MongoDB audit trail with filters, expandable steps |
| `RequestViewerComponent` | ✅ | LLM request/response viewer with messenger-style messages |
| `GraphTimelineComponent` | ✅ | Cytoscape graph visualization with timeline scrubbing |
| `TodoListComponent` | ✅ | Todo list viewer with phase selection |
| `PlaceholderAComponent` | Placeholder | Will become WorkspaceComponent |
| `PlaceholderBComponent` | Placeholder | Will become AgentChatComponent |
| `PlaceholderCComponent` | Placeholder | Future use |
| `WorkspaceComponent` | ⬜ Planned | File tree + file viewer |
| `MetricsComponent` | ⬜ Planned | Token usage, latency charts |
| `LogViewerComponent` | ⬜ Planned | Raw log file with search |
| `JsonViewerComponent` | ⬜ Planned | Expandable JSON tree |

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
- [x] Implement `/api/jobs/{id}/audit/timerange` for scrubber
- [x] Implement `/api/graph/changes/{job_id}` for graph timeline
- [x] Create workspace service for todo file access
- [x] Implement `/api/jobs/{id}/todos/*` endpoints

### Phase 4: Angular Data Services ✅
- [x] Create `ApiService` (HTTP client for all endpoints)
- [x] Create `StateService` (signals-based state for db-table)
- [x] Create `AuditService` for job/audit state management
- [x] Create `RequestService` for LLM request state
- [x] Create `TimeService` for global time synchronization
- [x] Create `GraphService` for graph timeline state
- [x] Create `TodoService` for todo list state
- [x] Centralize job selection in TimelineComponent

### Phase 5: Panel Components (MVP) ✅
- [x] `AgentActivityComponent` - audit entries with expandable steps
- [x] `RequestViewerComponent` - LLM request/response viewer with messenger-style messages
- [x] `DbTableComponent` - PostgreSQL table browser with pagination
- [x] `GraphTimelineComponent` - Cytoscape graph visualization with timeline
- [x] `TodoListComponent` - Todo list viewer with phase selection

### Phase 6: Panel Components (Extended)
- [ ] `WorkspaceComponent` - file tree + viewer
- [ ] `MetricsComponent` - token/latency charts from audit data
- [ ] `LogViewerComponent` - raw logs with search
- [ ] `JsonViewerComponent` - generic expandable JSON tree

### Phase 7: Backend Extensions
- [ ] Create Neo4j service (neo4j driver) for direct queries
- [ ] Implement `/api/cypher` pass-through
- [ ] Implement `/api/sql` pass-through
- [ ] Implement `/api/jobs/{id}/workspace` file browser endpoints

### Phase 8: Polish & Presets
- [x] Preset layouts loaded from JSON files
- [x] Component switcher dropdown in panel headers
- [x] Split/close panel buttons for dynamic layout building
- [ ] URL-based layout sharing (?layout=preset-name)
- [ ] Keyboard shortcuts for timeline (space=play/pause, arrows=scrub)
- [ ] Playback implementation (auto-advance through timeline)

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
    request_id: string;         // Links to llm_requests._id
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
2. **Data flow**: ✅ Services (signals) → ApiService (HTTP) → FastAPI → Databases
3. **Job selection**: ✅ Centralized in TimelineComponent (single source of truth)
4. **Graph visualization**: ✅ Cytoscape.js with snapshot/delta optimization
5. **Todo access**: ✅ Direct filesystem access via workspace service (not in MongoDB)
6. **Layout editor**: ✅ Split/close buttons + component dropdown for dynamic layouts, plus JSON presets for quick setup

## What's Still Missing

### High Priority
- [ ] **WorkspaceComponent** - File browser for workspace.md, plan.md, analysis files
- [ ] **Playback implementation** - Auto-advance through timeline
- [ ] **Keyboard shortcuts** - Space=play/pause, arrows=scrub

### Medium Priority
- [ ] **MetricsComponent** - Token usage, latency charts, cost tracking
- [ ] **Neo4j direct queries** - `/api/cypher` endpoint
- [ ] **SQL direct queries** - `/api/sql` endpoint

### Lower Priority
- [ ] **LogViewerComponent** - Raw log file viewing with search
- [ ] **JsonViewerComponent** - Generic expandable JSON tree
- [ ] **Docker integration** - Dockerfile, nginx config, compose service
- [ ] **URL-based layout sharing** - `?layout=preset-name`
- [ ] **Real-time updates** - WebSocket or polling for running jobs

## Open Questions

1. **Authentication**: Should the debug cockpit require auth, or is it dev-only?
2. **Real-time updates**: Should the cockpit poll for updates on running jobs, or is historical replay enough?
3. **Caching**: Should the FastAPI backend cache recent queries, or always hit the databases?
