# Global Expert Management

## Problem

Experts are currently static YAML files in `config/experts/`, loaded at orchestrator startup and cached in memory. Users cannot create, customize, or share expert configurations without filesystem access to the deployment. This limits the system to a fixed set of built-in experts managed by operators.

### Current limitations

| Issue | Detail |
|-------|--------|
| No user-created experts | Only operators with deployment access can add expert configs |
| No ownership or sharing | All experts are globally visible, no per-user customization |
| No project scoping | Project-specific experts require a Gitea jobs repo with `experts/` directory — brittle and undiscoverable |
| No worker/session distinction in UI | Job creation and session creation show the same expert list, but they need fundamentally different configs (`$extends: defaults` vs `$extends: persistent_defaults`) |
| No CRUD UI | The cockpit has no dedicated expert management — experts only appear as selection grids inside job/session create flows |

### How it works today

| Layer | Mechanism | Location |
|-------|-----------|----------|
| Config files | YAML with `$extends` inheritance | `config/experts/{name}/config.yaml` |
| Cache | `_experts_cache` dict, populated by `_scan_experts()` at startup | `main.py:9574-9619` |
| API (read-only) | `GET /api/experts`, `GET /api/experts/{id}`, `POST /api/experts/reload` | `main.py:9626-9946` |
| Project experts | Scanned from Gitea jobs repo `experts/` directory | `main.py:9847-9946` |
| Cockpit | Expert selector grids in job-create and session-create components | `job-create.component.ts`, `session-create.component.ts` |
| Config resolution | `_load_expert_detail()` merges expert YAML with defaults + settings_matrix | `main.py:9726-9795` |

### Why it needs to change

1. **Users need custom experts.** Different teams want domain-specific personas (e.g., "Security Auditor", "Data Pipeline Engineer") without operator intervention.
2. **Experts should be shareable.** A team lead creates an expert tuned for their project — team members should be able to use it without duplicating config.
3. **Worker vs session is a first-class distinction.** A "developer" expert that runs phased jobs is fundamentally different from an "interactive developer" for chat sessions. The UI must surface this clearly.
4. **Parity with datasources.** Datasources already have full CRUD, ownership, global/project scoping, and a dedicated management tab. Experts should follow the same pattern.

---

## Design

### Expert Types

Every expert is either a **worker** expert or a **session** expert. This maps directly to the existing `$extends` mechanism:

| Type | Base Config | Used For | Key Features |
|------|------------|----------|--------------|
| **Worker** | `defaults.yaml` | Jobs (phased execution) | Phases, todos, verification, scholar, curator, delegation, autonomy levels |
| **Session** | `persistent_defaults.yaml` | Persistent threads (interactive chat) | WebSocket sessions, permission_mode, idle_timeout, greeting, no phases |

This distinction is **structural**, not cosmetic — a worker expert cannot be used for a session and vice versa, because they inherit different base configs with incompatible schemas (worker has `delegation`, `verification`, `autonomy`; session has `interactive.permission_mode`, `interactive.greeting`).

### Expert Sources

Experts can come from three sources, with a clear priority order:

| Source | Storage | Managed By | Priority |
|--------|---------|------------|----------|
| **Built-in** | YAML files in `config/experts/` | Operators (deployment) | Lowest — fallback defaults |
| **User-created** | PostgreSQL `experts` table | Users via cockpit UI | Middle — personal customizations |
| **Project-specific** | PostgreSQL `project_experts` or Gitea repo | Project owners | Highest — project-scoped overrides |

When resolving an expert by name, project-specific > user-created > built-in. This mirrors datasource resolution.

### Ownership Model (same as datasources)

| Field | Purpose |
|-------|---------|
| `created_by` | Owner user UUID. NULL for built-in (YAML-sourced) experts |
| `is_global` | `TRUE` = visible to all users. `FALSE` = visible only to owner |

| Action | Who can do it |
|--------|--------------|
| View in expert panel | Owner, or anyone if `is_global = true`, or project member if linked to their project |
| Use on a job/session | Owner, or anyone if global, or project member if linked to job's/session's project |
| Link to a project | Owner only |
| Edit / delete | Owner only (built-in experts are read-only) |
| Duplicate (fork) | Any user who can view it — creates a new owned copy |

---

## Database Schema

### `experts` table

```sql
CREATE TABLE IF NOT EXISTS experts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,           -- unique identifier (slug), e.g. "security-auditor"
    display_name VARCHAR(200) NOT NULL,
    description TEXT,
    icon VARCHAR(100) DEFAULT 'smart_toy',
    color VARCHAR(7) DEFAULT '#6B7280',
    tags TEXT[] DEFAULT '{}',
    expert_type VARCHAR(10) NOT NULL DEFAULT 'worker',  -- 'worker' or 'session'
    config JSONB NOT NULL DEFAULT '{}',   -- expert-specific overrides (merged with defaults at resolution)
    instructions TEXT,                     -- custom instructions injected into workspace
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    is_global BOOLEAN NOT NULL DEFAULT FALSE,
    source VARCHAR(20) NOT NULL DEFAULT 'user',  -- 'builtin', 'user', 'project'
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Prevent duplicate names per owner
CREATE UNIQUE INDEX IF NOT EXISTS uq_expert_name_owner
    ON experts (name, COALESCE(created_by, '00000000-0000-0000-0000-000000000000'));

-- Filter by type efficiently
CREATE INDEX IF NOT EXISTS idx_experts_type ON experts (expert_type);

-- Filter by owner
CREATE INDEX IF NOT EXISTS idx_experts_created_by ON experts (created_by);

-- Trigger for updated_at
CREATE TRIGGER set_experts_updated_at
    BEFORE UPDATE ON experts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

### `project_experts` junction table

```sql
CREATE TABLE IF NOT EXISTS project_experts (
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    expert_id UUID NOT NULL REFERENCES experts(id) ON DELETE CASCADE,
    is_default BOOLEAN DEFAULT FALSE,     -- if TRUE, this is the project's default expert for new jobs/sessions
    config_override JSONB,                -- project-level config overrides on top of expert config
    linked_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (project_id, expert_id)
);

-- Only one default expert per project per type
CREATE UNIQUE INDEX IF NOT EXISTS uq_project_default_expert
    ON project_experts (project_id, is_default) WHERE is_default = TRUE;
```

### Migration of built-in experts

On startup (or via `POST /api/experts/reload`), sync YAML experts into the `experts` table:

```sql
INSERT INTO experts (name, display_name, description, icon, color, tags, expert_type, config, instructions, source, is_global)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'builtin', TRUE)
ON CONFLICT (name, COALESCE(created_by, '00000000-0000-0000-0000-000000000000'))
DO UPDATE SET display_name = EXCLUDED.display_name, description = EXCLUDED.description,
    icon = EXCLUDED.icon, color = EXCLUDED.color, tags = EXCLUDED.tags,
    config = EXCLUDED.config, instructions = EXCLUDED.instructions,
    updated_at = NOW()
WHERE experts.source = 'builtin';  -- only overwrite built-in, never user-created
```

The `expert_type` is derived from the `$extends` field:
- `$extends: defaults` -> `expert_type = 'worker'`
- `$extends: persistent_defaults` -> `expert_type = 'session'`

---

## API Endpoints

### Global Expert CRUD

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/experts` | List experts. Query params: `type` (worker/session), `source` (builtin/user), `include_global` (bool) |
| `POST` | `/api/experts` | Create user expert. Body: `ExpertCreate` |
| `GET` | `/api/experts/{id}` | Get expert detail with resolved config (merged with defaults + settings_matrix) |
| `PUT` | `/api/experts/{id}` | Update expert. Owner only. Built-in experts return 403 |
| `DELETE` | `/api/experts/{id}` | Delete expert. Owner only. Built-in experts return 403. Cascades to project_experts |
| `POST` | `/api/experts/{id}/duplicate` | Fork expert as owned copy. Any viewer can duplicate |
| `POST` | `/api/experts/reload` | Re-sync built-in experts from YAML to DB |

### Project Expert Linking

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/projects/{pid}/experts` | List experts linked to project (with project-level overrides) |
| `POST` | `/api/projects/{pid}/experts/{eid}` | Link expert to project |
| `PATCH` | `/api/projects/{pid}/experts/{eid}` | Update project-level overrides (config_override, is_default) |
| `DELETE` | `/api/projects/{pid}/experts/{eid}` | Unlink expert from project |

### Request/Response Models

```python
class ExpertCreate(BaseModel):
    name: str                             # slug: ^[a-z][a-z0-9_-]*$
    display_name: str
    description: str | None = None
    icon: str = "smart_toy"
    color: str = "#6B7280"
    tags: list[str] = []
    expert_type: Literal["worker", "session"] = "worker"
    config: dict[str, Any] = {}           # overrides on top of defaults
    instructions: str | None = None
    is_global: bool = False

class ExpertUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    tags: list[str] | None = None
    config: dict[str, Any] | None = None
    instructions: str | None = None
    is_global: bool | None = None

class ProjectExpertSettings(BaseModel):
    is_default: bool | None = None
    config_override: dict[str, Any] | None = None
```

### Expert Resolution

The existing `_load_expert_detail()` function needs to work with DB-stored experts in addition to YAML files. Resolution logic:

```python
async def resolve_expert(expert_id: str, project_id: str | None = None) -> ExpertDetail:
    # 1. Check project-specific override
    if project_id:
        pe = await db.get_project_expert(project_id, expert_id)
        if pe:
            expert = await db.get_expert(pe["expert_id"])
            # Apply project config_override on top
            if pe.get("config_override"):
                expert["config"] = _deep_merge(expert["config"], pe["config_override"])

    # 2. Fall back to DB expert (user-created or built-in)
    if not expert:
        expert = await db.get_expert_by_name(expert_id)

    # 3. Determine base config from expert_type
    base = load_defaults(expert["expert_type"])  # defaults.yaml or persistent_defaults.yaml

    # 4. Deep merge: base <- expert config
    merged = _deep_merge(base, expert["config"])

    # 5. Apply settings_matrix
    merged = _apply_settings_matrix_to_config(merged, settings_matrix)

    return ExpertDetail(
        id=expert["name"],
        display_name=expert["display_name"],
        config=merged,
        instructions=expert.get("instructions"),
        defaults_tools=base.get("tools"),
        settings_matrix=settings_matrix,
    )
```

### Backward Compatibility

The existing `config_name` field on jobs and threads (`VARCHAR(100)`) continues to work:
- Built-in expert names (e.g., "scholar", "developer") resolve to DB rows with `source='builtin'`
- User-created experts resolve by `name` field
- Existing jobs/threads with `config_name` values keep working — resolution checks DB first, falls back to YAML scan if not found (during migration window)

---

## Cockpit UI

### Experts Tab (new page)

Add an "Experts" tab to the cockpit navigation (same position pattern as the existing "Datasources" tab). The page follows the same layout as `datasource-list.component.ts`.

#### List View

```
[Worker ▼] [Session ▼] [All Sources ▼]     [+ New Expert]

┌─────────────────────────────────────────────────────────┐
│  🔬 Scholar                                    builtin  │
│  Research and deep-dive analysis agent                  │
│  Tags: research, analysis        Type: worker           │
│  [Edit] [Duplicate] [Delete]                            │
├─────────────────────────────────────────────────────────┤
│  🛠️ Developer                                  builtin  │
│  Implementation and PR factory                          │
│  Tags: coding, development       Type: worker           │
│  [Edit] [Duplicate] [Delete]                            │
├─────────────────────────────────────────────────────────┤
│  💬 Interactive                                builtin  │
│  Conversational persistent sessions                     │
│  Tags: chat, interactive         Type: session          │
│  [Edit] [Duplicate] [Delete]                            │
├─────────────────────────────────────────────────────────┤
│  🔒 Security Auditor                       user/global  │
│  Security review and vulnerability analysis             │
│  Tags: security, audit           Type: worker           │
│  [Edit] [Duplicate] [Delete]                            │
└─────────────────────────────────────────────────────────┘
```

**Filters:**
- **Type filter**: Worker / Session / All (default: All)
- **Source filter**: Built-in / User / All (default: All)
- Built-in experts show a badge and have Edit/Delete disabled (Duplicate always available)

#### Create/Edit Form

The form is **type-aware** — selecting Worker vs Session changes which config sections are available:

```
Expert Type:  (o) Worker   ( ) Session

Name:         [security-auditor        ]
Display Name: [Security Auditor        ]
Description:  [Security review and ... ]
Icon:         [verified_user  ] (picker)
Color:        [#EF4444] (color picker)
Tags:         [security] [audit] [+]
Global:       [x] Visible to all users

── LLM Settings ──────────────────────
Model:        [anthropic/claude-sonnet-4-6    ▼]
Reasoning:    [high ▼]

── Tools ─────────────────────────────
[x] workspace    [x] core    [x] research
[ ] shell        [x] git     [ ] citation
...

── Instructions ──────────────────────
[                                       ]
[  Custom instructions for this expert  ]
[                                       ]

── Advanced (worker only) ────────────
Autonomy:     [review ▼]
Verification: [x] Enable critic review
Scholar:      [ ] Enable research phase
...

── Advanced (session only) ───────────
Permission:   [supervised ▼]
Idle Timeout: [30] minutes
Greeting:     [Hello! I'm ready to ...]

                        [Cancel] [Save]
```

**Key behaviors:**
- Switching type resets type-specific advanced sections
- The config editor shows a structured form (not raw JSON) matching the existing `agent-settings.component.ts` pattern
- Built-in experts open in read-only mode with a "Duplicate to customize" button
- The tool selector reuses the existing tool category checkboxes from `agent-settings`

### Job Create: Expert Selector

The existing expert selector grid in `job-create.component.ts` filters to show **only worker-type experts**:

```typescript
// Current: shows all experts
this.experts = this.apiService.getExperts();

// New: filter by type
this.experts = this.apiService.getExperts({ type: 'worker' });
```

Additionally, project-linked experts appear first (with a project badge), followed by global/owned experts.

### Session Create: Expert Selector

The existing selector in `session-create.component.ts` filters to show **only session-type experts**:

```typescript
this.experts = this.apiService.getExperts({ type: 'session' });
```

### Project Settings: Expert Tab

Add an "Experts" section to project settings (alongside the existing datasources section):

```
Linked Experts                              [+ Link Expert]

┌─────────────────────────────────────────────────────────┐
│  🛠️ Developer               ⭐ Default for jobs         │
│  Config override: reasoning_level=high                  │
│  [Set Default] [Override Config] [Unlink]               │
├─────────────────────────────────────────────────────────┤
│  💬 Interactive              ⭐ Default for sessions     │
│  No overrides                                           │
│  [Set Default] [Override Config] [Unlink]               │
└─────────────────────────────────────────────────────────┘
```

**Project-level overrides:**
- `is_default`: One default worker expert for jobs, one default session expert for sessions
- `config_override`: Project-specific tweaks (e.g., always use high reasoning, enable specific tools)

---

## Config Resolution (updated flow)

The current resolution chain adds a DB lookup step:

```
1. Determine expert_type from DB record
2. Load base config:
   - worker  -> config/defaults.yaml
   - session -> config/persistent_defaults.yaml
3. Deep merge: base <- expert.config (from DB)
4. Deep merge: <- project config_override (if project-linked)
5. Deep merge: <- job/thread config_override (per-job overrides from UI)
6. Apply settings_matrix (model-family defaults)
7. Result: resolved_config stored on job/thread
```

This preserves the existing `$extends` semantics but moves the expert config source from YAML files to DB rows, with YAML files serving as seed data for built-in experts.

---

## MCP Server Integration

Update the existing MCP tools to support the full expert lifecycle:

| Tool | Current | New |
|------|---------|-----|
| `list_experts` | Lists YAML-based experts | Lists all experts (built-in + user) with type filter |
| `get_expert` | Reads YAML config | Reads from DB with full resolution |
| `reload_experts` | Rescans YAML directory | Re-syncs YAML -> DB for built-in experts |
| `create_expert` | N/A | Creates user expert via DB |
| `update_expert` | N/A | Updates user expert |
| `delete_expert` | N/A | Deletes user expert |
| `link_expert_to_project` | N/A | Links expert to project |
| `unlink_expert_from_project` | N/A | Unlinks expert from project |

---

## Migration Plan

### Phase 1: Database + API (backend)

| Change | File | What |
|--------|------|------|
| Add `experts` table | `schema.sql` | New table with indexes and trigger |
| Add `project_experts` table | `schema.sql` | Junction table for project linking |
| Seed built-in experts | `postgres.py` | `sync_builtin_experts()` reads YAML, upserts to DB |
| CRUD methods | `postgres.py` | `create_expert`, `get_expert`, `update_expert`, `delete_expert`, `list_experts` |
| Project linking methods | `postgres.py` | `link_expert_to_project`, `unlink_expert_from_project`, `list_project_experts` |
| Resolution method | `postgres.py` | `resolve_expert()` with full merge chain |
| REST endpoints | `main.py` | CRUD + project linking + duplicate + reload |
| Update `_load_expert_detail` | `main.py` | Read from DB instead of YAML cache |
| Update `list_experts` endpoint | `main.py` | Add `type` and `source` query params |

**Result**: Full expert CRUD via API. Existing job/session creation works unchanged — `config_name` resolves via DB.

### Phase 2: Cockpit UI

| Change | File | What |
|--------|------|------|
| Expert list page | `cockpit/src/app/simple/pages/experts/` | New page component with CRUD |
| Expert form component | `cockpit/src/app/shared/components/expert-form/` | Type-aware create/edit form |
| Navigation update | Shell/layout components | Add "Experts" tab |
| API service methods | `api.service.ts` | `createExpert`, `updateExpert`, `deleteExpert`, etc. |
| API models | `api.model.ts` | `Expert`, `ExpertDetail`, `ExpertCreate`, `ExpertUpdate` interfaces |
| Job create filter | `job-create.component.ts` | Filter experts to `type=worker` |
| Session create filter | `session-create.component.ts` | Filter experts to `type=session` |
| Project expert linking | Project settings component | Link/unlink experts, set defaults, config overrides |

**Result**: Full expert management UI with type-aware filtering.

### Phase 3: Advanced features (deferred)

| Feature | What |
|---------|------|
| Expert versioning | Track config changes over time, rollback to previous versions |
| Expert templates | Pre-built starting points for common use cases |
| Expert sharing | Share expert via link/code without making globally visible |
| Expert analytics | Track which experts are most used, success rates by expert |
| Expert validation | Validate config against schema before save, surface errors in UI |

---

## Files to Modify

### Phase 1

| File | Changes |
|------|---------|
| `orchestrator/database/schema.sql` | Add `experts` and `project_experts` tables |
| `orchestrator/database/postgres.py` | Add expert CRUD + linking + resolution methods |
| `orchestrator/main.py` | Add CRUD endpoints, update `_load_expert_detail`, update `_scan_experts` to seed DB |
| `orchestrator/mcp/server.py` | Add create/update/delete/link expert MCP tools |
| `orchestrator/services/formatters.py` | Update expert formatting for new fields |

### Phase 2

| File | Changes |
|------|---------|
| `cockpit/src/app/core/models/api.model.ts` | Add expert CRUD interfaces |
| `cockpit/src/app/core/services/api.service.ts` | Add expert CRUD + linking methods |
| `cockpit/src/app/simple/pages/experts/` | New expert list page (follows datasource-list pattern) |
| `cockpit/src/app/shared/components/expert-form/` | New type-aware expert form component |
| `cockpit/src/app/simple/simple-shell.component.ts` | Add "Experts" tab to navigation |
| `cockpit/src/app/shared/components/job-create/job-create.component.ts` | Filter to worker experts |
| `cockpit/src/app/shared/components/session-create/session-create.component.ts` | Filter to session experts |

---

## Anti-Patterns to Avoid

1. **Mixing worker and session configs in one expert.** The two types have incompatible schemas. An expert is always one or the other — enforced at creation time, immutable after.
2. **Allowing edits to built-in experts.** Built-in experts are operator-managed via YAML. Users fork (duplicate) them instead of editing. The DB rows with `source='builtin'` are overwritten on reload.
3. **Storing resolved config in the experts table.** The `config` column stores only the expert's overrides, not the merged result. Resolution happens at read time, ensuring base config changes propagate immediately.
4. **Using expert name as primary key.** Names can collide across owners. Use UUID primary key, enforce uniqueness on `(name, owner)`.
5. **Letting users create experts with `$extends` in config.** The `$extends` mechanism is implicit — `expert_type` determines the base config. The config column contains only overrides, never meta-directives.

## Open Questions

1. **Should project-linked experts inherit project datasource settings?** E.g., a project's "developer" expert auto-gets the project's linked datasources. Currently datasources and experts are selected independently per job — coupling them adds convenience but reduces flexibility.
2. **Should experts have their own settings_matrix overrides?** Currently, per-expert `settings_matrix.yaml` is supported for YAML experts. Do user-created experts need this, or is the global matrix sufficient?
3. **How to handle expert deletion when jobs reference it?** Options: soft-delete (mark inactive), prevent deletion if referenced, or fall back to built-in default. Recommend: prevent deletion if active jobs exist, allow if only historical.
