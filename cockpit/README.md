# Debug Cockpit

Angular dashboard for debugging and visualizing Superhuman Remote Worker agent execution. Features a tiling layout system with pluggable components for viewing audit trails, graph changes, database tables, and LLM requests.

## Quick Start

```bash
# Terminal 1: Start Orchestrator backend
cd orchestrator
pip install -r requirements.txt
uvicorn main:app --reload --port 8085

# Terminal 2: Start Angular frontend
cd cockpit
npm install
npm start
# Open http://localhost:4200
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Angular Frontend                         │
│                    http://localhost:4200                     │
├─────────────────────────────────────────────────────────────┤
│                   Orchestrator Backend                       │
│                    http://localhost:8085                     │
├──────────────────┬──────────────────┬───────────────────────┤
│    PostgreSQL    │     MongoDB      │        Neo4j          │
│   (jobs, reqs)   │  (audit trail)   │   (graph changes)     │
└──────────────────┴──────────────────┴───────────────────────┘
        │
        └──── MCP Server (stdio) ← Claude Code
```

## Features

- Dark themed UI (Catppuccin Mocha)
- Timeline scrubber with global time synchronization
- Resizable panel layout with drag handles
- Component switcher dropdown in each panel
- Split buttons to divide panels

### Components

| Component | Description |
|-----------|-------------|
| Agent Activity | MongoDB audit trail with filtering and pagination |
| Graph Viewer | Neo4j graph visualization with timeline playback |
| DB Table | PostgreSQL table browser |
| Request Viewer | Full LLM request/response inspector |

## Development

```bash
# Start dev server
npm start

# Build for production
npm run build

# Run tests
npm test
```

## Internationalization (i18n)

The cockpit ships English (`en`) and German (`de-DE`). Runtime switching is driven by `users.settings.language`; English is the source-of-truth. Full design lives in [`docs/features/i18n.md`](../docs/features/i18n.md).

**Locale files:** `src/assets/i18n/en.json` and `src/assets/i18n/de-DE.json`. Always edit `en.json` first, then mirror the change in `de-DE.json` — parity is CI-enforced.

**How to reference keys:**

```html
<!-- Templates -->
<h2>{{ 'sessions.title' | transloco }}</h2>
<input [placeholder]="'sessions.create.titlePlaceholder' | transloco" />
```

```ts
// TypeScript
private readonly transloco = inject(TranslocoService);
this.toast.error(this.transloco.translate('errors.unknown'));

// HTTP errors: prefer ErrorMessageService (maps status/code → translated message)
private readonly errors = inject(ErrorMessageService);
this.toast.error(this.errors.translate(err, 'errors.jobs.createFailed'));
```

**Key convention:**

- **Feature-grouped, dot-nested:** `sessions.create.titleLabel`, `jobs.list.empty`, `errors.http.404`. The first segment names the feature/page; deeper segments scope the context.
- **Verb-noun for action labels:** `createSession`, `deleteJob`, `saveChanges` — not `sessionCreate` or `new`.
- **Past-tense for confirmation / toast results:** `toasts.jobs.created`, `toasts.jobs.deleted`. Inline dialogs that ask before the action are present-tense (`sessions.confirmDelete`).
- **Errors:** live under `errors.*` with three sub-namespaces:
  - `errors.code.<code>` — maps structured orchestrator error codes (extension point)
  - `errors.http.<status>` — maps HTTP status families (`401`, `404`, `5xx`, `network`, `timeout`)
  - `errors.<feature>.<action>Failed` — caller-supplied fallbacks
- **Brand names stay untranslated:** `OpenAI`, `Anthropic`, `Groq`, `Keycloak`, etc. — render verbatim in both locales.

**Admin / debug surfaces stay in English** (see `src/app/debug/**`, `src/app/shared/components/statistics/**`). Not customer-facing.

**CI checks (`npm run i18n:check`):**

- `i18n:check:parity` — `de-DE.json` must have exactly the same keys as `en.json`.
- `i18n:check:hardcoded` — flags new `toast.*('literal')` / `alert('literal')` / `confirm('literal')` / `prompt('literal')` calls in `src/app`. Add `// i18n-exempt` on the line to silence intentionally. Allowlisted: `debug/**`, `statistics/**`, `*.spec.ts`.

## API Endpoints

The orchestrator backend runs on port **8085** and provides:

- `GET /api/tables` - List available PostgreSQL tables
- `GET /api/tables/{name}` - Get paginated table data
- `GET /api/jobs` - List jobs with audit counts
- `GET /api/jobs/{id}/audit` - Get paginated audit entries
- `GET /api/jobs/{id}/audit/timerange` - Get time bounds for timeline
- `GET /api/graph/changes/{id}` - Get graph deltas for visualization
- `GET /api/requests/{doc_id}` - Get full LLM request document

## Environment

The orchestrator backend requires these environment variables (see `.env.example`):

- `DATABASE_URL` - PostgreSQL connection string
- `MONGODB_URL` - MongoDB connection string (optional)
- `NEO4J_URI` - Neo4j Bolt URI
- `NEO4J_USERNAME` / `NEO4J_PASSWORD` - Neo4j credentials

## MCP Server

The MCP (Model Context Protocol) server exposes cockpit metrics to LLMs like Claude Code, enabling AI-assisted debugging of agent jobs.

### Setup

**Local development:**
```bash
cd orchestrator/mcp
pip install -r requirements.txt
python run.py
```

**Docker:**
```bash
podman-compose -f docker-compose.dev.yaml up -d orchestrator-mcp
docker exec -i srw-orchestrator-mcp-dev python run.py
```

### Claude Code Configuration

The project includes `.mcp.json` with the MCP server configuration. Claude Code will prompt you to enable it.

For containerized setup, create or update `.mcp.json`:
```json
{
  "mcpServers": {
    "orchestrator": {
      "command": "docker",
      "args": ["exec", "-i", "srw-orchestrator-mcp-dev", "python", "run.py"]
    }
  }
}
```

### Available Tools

| Tool | Description |
|------|-------------|
| `list_jobs` | List jobs with status filter |
| `get_job` | Get job details by ID |
| `get_audit_trail` | Get paginated audit entries |
| `get_chat_history` | Get conversation turns |
| `get_todos` | Get current and archived todos |
| `get_graph_changes` | Get Neo4j graph mutations timeline |
| `get_llm_request` | Get full LLM request/response |
| `search_audit` | Search audit entries by pattern |
