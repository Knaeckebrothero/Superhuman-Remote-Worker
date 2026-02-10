# External Datasources

## Motivation

The project originally had a tight integration with Neo4j as a first-class knowledge graph for requirement traceability. Since the agent has evolved into a general-purpose worker, the graph database is no longer a core dependency — it's just one possible data source among many.

The goal is to replace the deep Neo4j integration with a generic **datasource connector** system. Users can attach external databases to jobs through the UI, and the agent receives the appropriate tools to interact with them at runtime. This keeps the agent flexible: one job might query a Neo4j graph, another might read from a PostgreSQL analytics database, another might aggregate data across MongoDB collections.

## Supported Types

Initial connector types:

| Type | Tools Provided | Notes |
|------|---------------|-------|
| `postgresql` | SQL query, schema inspection | Read/write depending on flag |
| `neo4j` | Cypher query, schema inspection | Read/write depending on flag |
| `mongodb` | Collection query, aggregation | Read/write depending on flag |

Future candidates: S3, Redis, SharePoint, Elasticsearch, etc.

## Storage Model

A new `datasources` table in the orchestrator PostgreSQL database.

```sql
CREATE TABLE datasources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,                    -- User-provided label (e.g. "Production Analytics DB")
    description TEXT,                      -- What this datasource contains (included in agent context)
    type TEXT NOT NULL,                    -- 'postgresql', 'neo4j', 'mongodb'
    connection_url TEXT NOT NULL,          -- Full connection string (e.g. postgres://user:pass@host:5432/db)
    credentials JSONB DEFAULT '{}',       -- Additional auth details if needed beyond the URL
    read_only BOOLEAN NOT NULL DEFAULT TRUE, -- Whether the agent is allowed to write
    job_id UUID REFERENCES jobs(id),      -- NULL = available to all jobs, set = job-specific
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- One datasource of each type per job (or per global scope)
CREATE UNIQUE INDEX uq_datasource_type_job ON datasources (type, COALESCE(job_id, '00000000-0000-0000-0000-000000000000'));
```

Key design decisions:

- **`job_id = NULL`** means the datasource is global — available to any job by default.
- **`job_id = <uuid>`** means it's scoped to that specific job only.
- **One per type per scope**: A job can have at most one PostgreSQL datasource, one Neo4j datasource, etc. The unique index enforces this. This keeps tool loading simple (no disambiguation needed). The `COALESCE` in the index maps NULL job_ids to a sentinel UUID so PostgreSQL can enforce uniqueness across global datasources.
- **Resolution order**: When the agent starts, it resolves datasources for its job. A **job-specific** datasource takes precedence over a **global** one of the same type. If no job-specific datasource exists for a type, the global one (if any) is used.
- **`read_only` flag**: Defaults to `TRUE`. When enabled, the agent only receives read tools (queries, schema inspection). When disabled, write tools are also available (inserts, updates, mutations).
- **`description` field**: Included in the agent's context so it knows what the datasource contains and when to use it (e.g. "Customer order history from the ERP system").
- **Not the orchestrator's own database**: This table stores connections to *external* databases that the agent works with. The orchestrator's own PostgreSQL (configured via `DATABASE_URL`) is system infrastructure and is not a datasource — `connections.postgres` in the agent config refers to that internal DB and remains unchanged.

### Credentials

For now, credentials are stored in **plain text**. This is acceptable at the current stage of development. Encryption at rest will be added later when authentication and authorization are implemented. The connection URL contains inline credentials where possible (e.g. `postgres://user:pass@host/db`), with the `credentials` JSONB field available for cases that need separate auth tokens or certificate references.

## Tool Loading (Hybrid Approach)

### Current System

Tools are declared per agent config in `config/*.yaml` under categories (`workspace`, `core`, `graph`, `research`, etc.). The `TOOL_REGISTRY` in `src/tools/registry.py` filters tools by phase (strategic/tactical).

### Proposed: Multi-Stage Configuration

The configuration pipeline becomes:

```
1. Agent config (config/*.yaml)        -- User/developer defines base tools
2. Orchestrator datasource override    -- System injects/removes db tools based on attached datasources
3. Final resolved config               -- What the agent actually receives
```

**Stage 1 — Agent config (unchanged):** The user still declares tools in their config YAML as before. This can include or exclude any database tool categories. This is the "intent" layer.

**Stage 2 — Orchestrator override:** When a job is created with datasources attached, the orchestrator modifies the resolved config:

- For each attached datasource type, the corresponding tool category is **ensured present** (injected if missing).
- If a datasource type is **not** attached, the corresponding tools are **removed** regardless of what the config says.
- The `read_only` flag further filters: if `read_only = TRUE`, only read tools are included; if `FALSE`, both read and write tools are available.

**Example:** A user config enables `graph` tools but the job has no Neo4j datasource attached. The orchestrator strips the graph tools. Conversely, if the user config disables graph tools but a Neo4j datasource is attached, the orchestrator adds them back — the datasource attachment is the source of truth for database tools.

This means the agent config controls non-database tools (workspace, research, coding, etc.), while the orchestrator controls database tools based on what's actually connected.

### Tool-to-Datasource Mapping

Each datasource type maps to a set of tools. These are preliminary names — final naming should match the existing convention (e.g. `execute_cypher_query` for Neo4j). New tool implementations would live alongside the existing ones: `src/tools/graph/` (Neo4j), `src/tools/sql/` (PostgreSQL), `src/tools/mongodb/` (MongoDB).

```yaml
# Internal mapping (not user-facing config)
datasource_tools:
  postgresql:
    read:
      - sql_query          # Execute SELECT queries
      - sql_schema         # Inspect tables, columns, types
    write:
      - sql_execute        # Execute INSERT/UPDATE/DELETE/DDL
  neo4j:
    read:
      - execute_cypher_query   # Execute read-only Cypher (existing tool)
      - get_database_schema    # Get labels, relationships, properties (existing tool)
    write:
      - cypher_write           # Execute write Cypher (CREATE, MERGE, DELETE)
  mongodb:
    read:
      - mongo_query        # Find documents with filters
      - mongo_aggregate    # Run aggregation pipelines
    write:
      - mongo_insert       # Insert documents
      - mongo_update       # Update documents
```

### Connection Injection

When tools are loaded, the datasource connection details are injected into the tool context (`ToolContext` in `src/tools/context.py`). Tools don't read from environment variables — they receive their connection from the resolved datasource config. This means the same tool implementation can work with different databases across different jobs.

## Default Datasources via Environment

The `.env` file can define one default datasource per type. If set, the system creates a global datasource entry (with `job_id = NULL`) on initialization.

```bash
# .env.example additions

# Default datasources (optional)
# If set, a global datasource of that type is created on init.
# These are available to all jobs unless overridden by a job-specific datasource.

# Default PostgreSQL datasource (external, NOT the orchestrator's own DB)
# DEFAULT_DS_POSTGRESQL_URL=postgres://user:pass@host:5432/analytics_db
# DEFAULT_DS_POSTGRESQL_NAME=Default PostgreSQL
# DEFAULT_DS_POSTGRESQL_READ_ONLY=true

# Default Neo4j datasource
# Neo4j needs separate credentials since bolt:// URLs don't support inline auth.
# DEFAULT_DS_NEO4J_URL=bolt://host:7687
# DEFAULT_DS_NEO4J_USERNAME=neo4j
# DEFAULT_DS_NEO4J_PASSWORD=neo4j_password
# DEFAULT_DS_NEO4J_NAME=Default Neo4j
# DEFAULT_DS_NEO4J_READ_ONLY=true

# Default MongoDB datasource
# DEFAULT_DS_MONGODB_URL=mongodb://user:pass@host:27017/mydb
# DEFAULT_DS_MONGODB_NAME=Default MongoDB
# DEFAULT_DS_MONGODB_READ_ONLY=true
```

On `python init.py`, if any `DEFAULT_DS_*_URL` variable is set, the corresponding global datasource is created (or updated if it already exists). For Neo4j, the username/password are stored in the `credentials` JSONB field since they can't be embedded in the bolt URL. For PostgreSQL and MongoDB, credentials are typically inline in the URL. If the variable is empty or unset, no default is created for that type.

## UI Integration (Cockpit)

The cockpit gets a datasource management workflow:

1. **Datasource library page**: List, create, edit, delete global datasources. Shows name, type, connection status, which jobs use it.
2. **Job creation/edit**: A dropdown or selector to attach datasources. Options include:
   - Preconfigured global datasources (from the library)
   - "Add new" to create a job-specific datasource inline
3. **Read-only toggle**: Visible per-datasource when attaching to a job.
4. **Connection test button**: Validates that the connection works before saving.

## Removing the Baked-In Neo4j Integration

The current codebase treats Neo4j as a special-case database with its own config flag, env vars, connection logic, and tools category. All of this needs to be generalized so Neo4j is just one datasource type among many. Below is a complete inventory of what needs to change.

### 1. Docker Compose: Remove Neo4j Service Entirely

**File:** `docker-compose.dev.yaml`

Remove the entire Neo4j service (lines 53-71), the `./data:/import:Z` bind mount (line 63), the named volumes `neo4j_dev_data` and `neo4j_dev_logs` (lines 256-259), and any usage instructions referencing `neo4j` (lines 21, 25).

Neo4j is no longer a project-provided service. If a user wants to use Neo4j, they run their own instance and attach it as a datasource via the UI. The `./data:/import:Z` bind mount was creating a `data/` folder in the project root whenever the dev compose was started — this goes away entirely.

The production `docker-compose.yaml` does not have a Neo4j service, so no changes needed there.

### 2. Config: Remove `connections.neo4j` Flag

**Files to change:**

| File | What to change |
|------|---------------|
| `config/defaults.yaml` (lines 99-101) | Remove `neo4j: false` from `connections` section |
| `config/schema.json` (lines 244-248) | Remove `neo4j` property from `connections` schema |
| `src/core/loader.py` (line 342) | Remove `neo4j: bool = False` from `ConnectionsConfig` |
| `src/core/loader.py` (lines 554, 675) | Remove `neo4j=connections_data.get("neo4j", False)` |
| Any custom configs in `config/` | Remove `neo4j: true/false` from `connections` sections |

The `connections` section retains only `postgres: true` (the orchestrator's own internal database, configured via `DATABASE_URL`). External datasources are no longer controlled by agent config flags — they come from the `datasources` table.

### 3. Agent: Remove Neo4j Connection Logic

**File:** `src/agent.py` (lines 260-281)

Remove the entire block that lazily initializes Neo4j from environment variables:

```python
# REMOVE this block from initialize():
if self.neo4j_conn is None and self.config.connections.neo4j:
    from src.database.neo4j_db import Neo4jDB
    neo4j_uri = os.getenv("NEO4J_URI")
    neo4j_username = os.getenv("NEO4J_USERNAME", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD")
    ...
```

Also remove the `neo4j_conn` constructor parameter and attribute (lines 85, 98).

**Replacement:** The agent's `initialize()` method queries the `datasources` table (via the orchestrator's PostgreSQL, which it already connects to) for datasources attached to its job. Resolution: job-specific datasources first, then global fallbacks. For each resolved datasource, it creates the appropriate connection (Neo4jDB, asyncpg pool, pymongo client, etc.) and injects it into `ToolContext.datasources`. This is generic — not Neo4j-specific.

### 4. Tool Context: Generalize Database Connections

**File:** `src/tools/context.py`

Replace the specific `neo4j_db` attribute with a generic datasource registry:

```python
# REMOVE:
neo4j_db: Optional["Neo4jDB"] = None

def has_neo4j(self) -> bool:
    return self.neo4j_db is not None

@property
def graph(self) -> Optional["Neo4jDB"]:
    return self.neo4j_db

# REPLACE WITH:
datasources: Dict[str, Any] = field(default_factory=dict)  # type -> connection

def has_datasource(self, ds_type: str) -> bool:
    return ds_type in self.datasources

def get_datasource(self, ds_type: str) -> Optional[Any]:
    return self.datasources.get(ds_type)
```

Tools then call `context.get_datasource("neo4j")` instead of `context.graph`. The `datasources` dict also carries metadata (read-only flag, description) alongside the connection object.

### 5. Tool Registry: Decouple Graph Category

**File:** `src/tools/registry.py`

The graph tools loading block (lines 311-324) checks `context.has_neo4j()`. This needs to check the generic datasource:

```python
# BEFORE:
if "graph" in tools_by_category:
    if not context.has_neo4j():
        logger.warning("Graph tools require neo4j_db in ToolContext")

# AFTER:
if "graph" in tools_by_category:
    if not context.has_datasource("neo4j"):
        logger.warning("Graph tools require a neo4j datasource")
```

Same pattern applies when SQL or MongoDB tools are added.

### 6. Neo4j Tools: Read Connection from Datasource

**File:** `src/tools/graph/neo4j.py`

The `create_neo4j_tools()` function currently reads `context.graph` directly. Update to:

```python
def create_neo4j_tools(context: ToolContext) -> List[Any]:
    ds = context.get_datasource("neo4j")
    if not ds:
        raise ValueError("Neo4j datasource not available")
    neo4j = ds["connection"]  # The Neo4jDB instance
    read_only = ds.get("read_only", True)
    ...
```

The `validate_schema_compliance` tool (lines 143-204) imports `MetamodelValidator` from `src/utils/metamodel_validator.py`. This validator is specific to the old requirements metamodel and should be removed or made optional. It's not part of a generic graph query workflow.

### 7. Neo4jDB Class: Keep but Decouple

**File:** `src/database/neo4j_db.py`

The `Neo4jDB` class itself is fine as a connector implementation. However:

- Remove the env var fallback defaults in `__init__` (lines 67-69). Connection details should come exclusively from the datasource config, not from environment variables.
- The domain-specific namespaces (`RequirementsNamespace`, `EntitiesNamespace`, `RelationshipsNamespace`, `StatisticsNamespace`) are remnants of the requirements-processing agent. These should be removed — the generic tools only need `execute_query` and `get_schema`.
- Keep the core connection/query methods: `connect()`, `close()`, `execute_query()`, `execute_write()`, `get_schema()`.

### 8. Database Module Exports

**File:** `src/database/__init__.py`

Keep `Neo4jDB` in exports (it's still a valid connector class), but remove the namespace-based examples from the module docstring that reference `neo4j_db.requirements.create()`.

### 9. Environment Variables: Clean Up

**File:** `.env.example`

Remove the dedicated Neo4j section (lines 34-37):

```bash
# REMOVE:
# Neo4j (Knowledge Graph) - only if connections.neo4j: true in config
# NEO4J_URI=bolt://localhost:7687
# NEO4J_USERNAME=neo4j
# NEO4J_PASSWORD=neo4j_password
```

Remove the Neo4j port overrides (lines 236-237):

```bash
# REMOVE:
# NEO4J_BOLT_PORT=7687
# NEO4J_HTTP_PORT=7474
```

These are replaced by the `DEFAULT_DS_NEO4J_*` variables described in the "Default Datasources via Environment" section above.

The `docker-compose.dev.yaml` Neo4j service ports can keep their defaults or use the new naming convention.

### 10. Config Schema: Rename `graph` Tool Category

**File:** `config/schema.json` (lines 209-216)

The `graph` tool category description says "Neo4j graph operation tools". With the datasource model, this category should be renamed or its description generalized. Options:

- **Keep `graph`** with updated description: "Graph database tools (Neo4j)" — it's still accurate since Neo4j is the only graph DB we support initially.
- **Rename to `neo4j`** to match the datasource type — more explicit, avoids confusion if we later add a different graph DB.

Recommendation: Keep `graph` for now since the tool names (`execute_cypher_query`, `get_database_schema`) are Neo4j-specific anyway. Rename later if we add support for other graph databases.

### 11. MetamodelValidator and validate_metamodel.py: Remove or Deprecate

**Files:**
- `src/utils/metamodel_validator.py` — Validator library
- `validate_metamodel.py` — Standalone CLI script at project root

The validator checks compliance against a specific requirements metamodel (node labels, relationship types, quality gates). It's tightly coupled to the old requirements-processing use case. With a generic datasource model, the agent shouldn't assume what's in the graph.

Options:
- **Remove entirely** — clean break from the old model.
- **Move to a plugin/extension** — keep for users who still do requirements work, but don't include it in the base toolset.

The `validate_schema_compliance` tool in `src/tools/graph/neo4j.py` that calls it should be removed from the default graph tools. The standalone `validate_metamodel.py` script at the project root should also be removed (or moved alongside the validator if we keep it as a plugin).

### 12. Requirements.txt

**File:** `requirements.txt`

Keep `neo4j>=5.16.0` as a standard dependency. All datasource drivers ship in `requirements.txt` so they're available at runtime when a user attaches a datasource. Add `pymongo` when MongoDB tools are implemented.

### 13. Default Config: Remove Graph Tools from defaults.yaml

**File:** `config/defaults.yaml` (lines 86-90)

The default config currently lists graph tools:

```yaml
graph:
  - execute_cypher_query
  - get_database_schema
  - validate_schema_compliance
```

With the orchestrator override model, database tools are injected based on attached datasources — not declared in the agent config. Remove the `graph` section from defaults. It will be added back dynamically by the orchestrator when a Neo4j datasource is attached.

### 14. CLAUDE.md Documentation

Update the following sections in `CLAUDE.md`:
- **Multi-Database Architecture table**: Remove Neo4j row or mark as "via datasource connector"
- **Environment Variables**: Remove `NEO4J_URI` line, add `DEFAULT_DS_*` section
- **Tool categories**: Update `graph` description
- **`connections.neo4j`** references throughout
- **Validation section**: Remove `validate_metamodel.py` reference

## Migration Path

To get from the current state to the datasource connector system:

1. **Create the `datasources` table** in the orchestrator schema.
2. **Add orchestrator API endpoints** for CRUD on datasources (and attaching to jobs).
3. **Remove `connections.neo4j`** from config, schema, and loader.
4. **Generalize `ToolContext`** — replace `neo4j_db` with `datasources` dict.
5. **Refactor Neo4j tools** to read connection from `context.get_datasource("neo4j")`.
6. **Strip Neo4jDB namespaces** — keep only the generic query/schema methods.
7. **Remove Neo4j service** entirely from `docker-compose.dev.yaml` (service, volumes, bind mount).
8. **Clean up `.env.example`** — remove `NEO4J_*` vars, add `DEFAULT_DS_*` vars.
9. **Implement the orchestrator config override stage** for job creation.
10. **Add cockpit UI** for datasource management.
11. **Remove graph tools from `config/defaults.yaml`** — orchestrator injects them.
12. **Update CLAUDE.md** and other documentation.

## Implementation Roadmap

### Phase 1: Foundation (Backend)

Remove the baked-in Neo4j integration and lay the groundwork for generic datasources.

**1.1 — Database & Schema**
- Create `datasources` table in `orchestrator/database/schema.sql`
- Add orchestrator API endpoints for datasource CRUD
- Add default datasource seeding in `orchestrator/init.py` (reads `DEFAULT_DS_*` env vars)

**1.2 — Remove Neo4j Coupling**
- Remove `connections.neo4j` from `config/defaults.yaml`, `config/schema.json`, `src/core/loader.py` (ConnectionsConfig)
- Remove Neo4j service, volumes, and `./data:/import:Z` bind mount from `docker-compose.dev.yaml`
- Remove `NEO4J_*` env vars from `.env.example`, add `DEFAULT_DS_*` section
- Remove `neo4j_conn` from `src/agent.py` constructor and `initialize()`
- Remove `validate_metamodel.py` and `src/utils/metamodel_validator.py`
- Remove `validate_schema_compliance` tool from `src/tools/graph/neo4j.py`
- Strip domain namespaces from `src/database/neo4j_db.py` (keep `connect`, `close`, `execute_query`, `execute_write`, `get_schema`)
- Remove graph tools section from `config/defaults.yaml`

**1.3 — Generalize ToolContext**
- Replace `neo4j_db` attribute with `datasources: Dict[str, Any]` on `ToolContext`
- Add `has_datasource(type)` and `get_datasource(type)` methods
- Remove `has_neo4j()` and `graph` property
- Update `src/tools/registry.py` graph tools loading to use `context.has_datasource("neo4j")`

**Deliverable:** Neo4j decoupled, datasources table exists, API endpoints available. Agent still works without any datasources attached (no regressions). Neo4j tools still work if a datasource is manually inserted into the table.

---

### Phase 2: Orchestrator-Driven Datasource Resolution (DONE)

The orchestrator resolves datasources and sends them to the agent as part of the existing `JobStartRequest` payload — the same way it already sends config, uploads, and git remote URLs. This is cleaner than having the agent query orchestrator internals because:
- The orchestrator already owns the assignment/resume flow
- The agent stays dumb — it doesn't query orchestrator tables
- Both assign and resume paths get identical treatment
- Config override (tool injection/stripping) happens in one central place

**2.1 — Datasources in JobStartRequest**
- Added `datasources: list[dict] | None` field to `JobStartRequest` on both orchestrator and agent sides
- Also added to `JobResumeRequest` (agent model) and the orchestrator's resume payload dict
- Each datasource dict contains: `type`, `name`, `description`, `connection_url`, `credentials`, `read_only`

**2.2 — Orchestrator Resolves Datasources During Assign & Resume**
- `assign_job_to_agent()` calls `resolve_datasources_for_job()`, builds a stripped payload via `_build_datasources_payload()`, and passes it to the `JobStartRequest`
- `resume_job()` does the same, adding `datasources` to the resume payload dict
- Internal fields (`id`, `job_id`, `created_at`, `updated_at`) are stripped before sending

**2.3 — Orchestrator Config Override (Tool Injection/Stripping)**
- `_build_datasource_tool_override()` modifies `config_override` to ensure tool categories match attached datasources
- For each known datasource type (currently Neo4j → `graph` category), if attached: inject tools; if not attached: strip tools (`graph: []`)
- Respects `read_only` flag (currently same tool set for read/write since `execute_cypher_query` handles both)
- Called in both `assign_job_to_agent` and `resume_job`

**2.4 — Agent Receives and Connects**
- `_process_orchestrator_job()` in `src/api/app.py` passes `datasources` through to job metadata
- `_setup_job_tools()` in `src/agent.py` reads `metadata["datasources"]`, creates connections via `_create_datasource_connection()`, and injects into `ToolContext.datasources`
- Currently supports Neo4j; PostgreSQL and MongoDB raise `NotImplementedError` (Phase 3)
- Connections are closed in `_close_datasource_connections()` on job completion, failure, or cancellation

**Deliverable:** Orchestrator resolves datasources and sends them to agent. Agent creates connections and injects into ToolContext. Neo4j tools work end-to-end via the datasource connector. Config override ensures graph tools are present only when a Neo4j datasource is attached.

---

### Phase 3: SQL & MongoDB Tools (DONE)

Added tool implementations for PostgreSQL and MongoDB datasource types, following the Neo4j pattern.

**3.1 — PostgreSQL Datasource Tools**
- Created `src/tools/sql/` package with `sql_query`, `sql_schema`, `sql_execute` tools
- `sql_query` executes read-only SELECT queries (uses `SET TRANSACTION READ ONLY`)
- `sql_schema` inspects tables, columns, types, constraints, and indexes
- `sql_execute` runs write SQL (INSERT/UPDATE/DELETE/DDL) with commit
- Uses `psycopg` (sync driver, already in requirements)
- Registered in `src/tools/registry.py` under `sql` category

**3.2 — MongoDB Datasource Tools**
- Created `src/tools/mongodb/` package with `mongo_query`, `mongo_aggregate`, `mongo_schema`, `mongo_insert`, `mongo_update` tools
- `mongo_query` finds documents with JSON filters (limit 100)
- `mongo_aggregate` runs aggregation pipelines
- `mongo_schema` lists collections or describes one (sample fields, indexes, doc count)
- `mongo_insert` inserts single or multiple documents
- `mongo_update` updates matching documents with update operators
- Uses `pymongo` (sync driver, already in requirements)
- Registered in `src/tools/registry.py` under `mongodb` category

**3.3 — Tool-to-Datasource Mapping**
- Added `postgresql` and `mongodb` entries to `DS_TOOL_MAP` in `orchestrator/main.py`
- Read-only mode: `sql_query`/`sql_schema` for PostgreSQL; `mongo_query`/`mongo_aggregate`/`mongo_schema` for MongoDB
- Write mode adds: `sql_execute` for PostgreSQL; `mongo_insert`/`mongo_update` for MongoDB
- Added `sql: []` and `mongodb: []` to `config/defaults.yaml` and `config/schema.json`

**3.4 — Agent Connection Support**
- Implemented `postgresql` and `mongodb` in `_create_datasource_connection()` (`src/agent.py`)
- PostgreSQL: `psycopg.connect()` with connection test
- MongoDB: `MongoClient` with ping test, database extracted from URL path
- Added `_datasource_clients` dict for proper MongoDB client cleanup

**Deliverable:** All three datasource types (PostgreSQL, Neo4j, MongoDB) have working tools. Agent can connect to any combination based on attached datasources.

---

### Phase 4: Cockpit UI (DONE)

Added full datasource management UI to the Angular cockpit frontend.

**4.1 — TypeScript Models & API Service**
- Added `Datasource`, `DatasourceCreateRequest`, `DatasourceUpdateRequest`, `DatasourceTestResult` interfaces to `cockpit/src/app/core/models/api.model.ts`
- Added `datasource_ids?: string[]` to `JobCreateRequest`
- Added `getDatasources`, `getDatasource`, `createDatasource`, `updateDatasource`, `deleteDatasource`, `testDatasource`, `getJobDatasources` methods to `ApiService`

**4.2 — DatasourceListComponent**
- Created `cockpit/src/app/components/datasource-list/datasource-list.component.ts`
- Full CRUD panel: create/edit form, type filter chips (All/PostgreSQL/Neo4j/MongoDB), refresh
- Table with type badge, name, masked URL, read-only flag, scope (Global/Job), and actions (Test/Edit/Delete)
- Inline connection test results per row
- Create/edit form with conditional Neo4j username/password fields
- "Test Connection" button in form with inline result display
- Registered in `app.ts` as `datasource-list` / "Datasources"

**4.3 — Datasource Picker in Job Creation**
- Added multi-select checkbox list in the Advanced section of `JobCreateComponent`
- Loads global datasources on init, each row shows type icon + name + description + read-only badge
- Selected IDs sent as `datasource_ids` in `JobCreateRequest`
- Reset clears selections

**4.4 — Backend: datasource_ids on Job Creation**
- Added `datasource_ids: list[str] | None` to `JobCreate` model in `orchestrator/main.py`
- On job creation, each selected global datasource is cloned as a job-scoped entry
- Failures logged as warnings (don't fail the job creation)

**Deliverable:** Users can manage datasources via a dedicated panel and attach them to jobs during creation through the cockpit UI.

---

### Phase 5: Documentation & Cleanup (DONE)

Updated all documentation to reflect the datasource connector architecture.

**5.1 — CLAUDE.md Updates**
- Updated project overview to reflect general-purpose agent system
- Removed stale `validate_metamodel.py` Validation section
- Updated tool categories: fixed `graph` description, added `sql` and `mongodb` categories with orchestrator injection note
- Replaced single Neo4j row in Multi-Database Architecture with two tables: system databases (PostgreSQL, MongoDB) and external datasources (PostgreSQL, Neo4j, MongoDB via connector)
- Added `src/tools/graph/`, `src/tools/sql/`, `src/tools/mongodb/` to Key Source Directories
- Updated `src/database/` directory listing to include all three database managers
- Generalized MCP `get_graph_changes` tool description

**5.2 — config/README.md Updates**
- Replaced single `graph: []` entry with all three database tool categories (`graph`, `sql`, `mongodb`) with comments explaining orchestrator injection
- Added "Multi-Stage Config Pipeline" section documenting the three-stage config resolution (agent config → orchestrator override → final resolved config)

**5.3 — Stale Reference Cleanup**
- `src/database/__init__.py` — Already clean (verified; mentions "via datasource connector")
- `cockpit/agent-activity` component — Removed `validate_schema_compliance` from tool category map, added `sql_*` and `mongo_*` tools
- `docs/deployment.md` — Updated toolkit table (added sql/mongodb rows), replaced stale Neo4jToolkit code example with datasource connector reference
- `docs/tool_issues.md` — Updated tool list (removed validate_schema_compliance, added sql/mongodb tools)
- `docs/agent_improvements.md` — Updated verification gate example to use current tools
- `docs/done/` files left as historical archives (not modified)

**Deliverable:** Documentation matches the new architecture. All active code and docs updated. No stale `validate_schema_compliance` or `connections.neo4j` references in active files.

## Design Decisions

### Error Handling: Return Errors to the Agent

The current Neo4j tools handle connection failures and query errors by catching exceptions and returning error strings to the agent (e.g. `"Error executing query: ..."`). The agent then decides how to react — retry, adjust the query, or report the problem. This pattern works well and should be preserved for all datasource tools. No reconnection/retry mechanism at the tool level.

### All Drivers Shipped in requirements.txt

All datasource drivers (`neo4j`, `pymongo`, etc.) are included in `requirements.txt` as standard dependencies. Since we don't know when a user will attach a datasource, the drivers must be available at runtime without extra install steps.

### Resolved Config View in Cockpit

When the orchestrator injects or strips database tools based on attached datasources, this should be visible in the cockpit as a "resolved config" view. This shows what the agent actually received (after the orchestrator override) vs. what the base config declared. Useful for debugging situations like "why doesn't my agent have graph tools?" — the answer is visible: no Neo4j datasource is attached.

### Orchestrator API Endpoints

The exact API surface for datasource CRUD and job attachment will be determined during implementation based on how the cockpit UI is modeled.

## Open Questions

None at this time. Decisions above capture all resolved items.
