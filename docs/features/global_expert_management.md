---
tags:
  - feature
  - experts
  - config
  - capabilities
  - multi-tenancy
related:
  - "[[config_matrix_db_overrides]]"
  - "[[observability_and_quotas]]"
  - "[[multi_tenancy]]"
  - "[[feature_development_pipeline]]"
aliases:
  - user-defined experts
  - custom experts
  - expert CRUD
  - capability grants
  - allowed models
---

# User-Defined Experts (DB-backed expert management + capability grants)

> Design doc **v2 — 2026-06-11**, from the "users create/edit/improve their own
> experts" planning conversation. Supersedes the undated v1 draft that
> previously lived in this file (landed alongside `d861ce1f`); v1's schema
> bones, API table, UI sketches, and anti-patterns are carried forward where
> still valid. The two structural changes vs v1: **(a)** bundled YAML experts
> are *overlaid*, not synced into the DB, mirroring the config-overrides
> system exactly; **(b)** a **capability-grants** layer (generalizing
> `users.can_use_vm`) gates what users may bake into an expert.

**Status:** Design / not started.
**Triggered by:** Live prompt + settings overrides shipped
([[config_matrix_db_overrides]], migration `0022`). Experts are the natural
next unit: a named, owned *bundle* (config fragment + persona + display
metadata) instead of per-leaf overrides. Users should create, edit, share,
and iterate on their own experts without operator filesystem access.

## Problem

Experts are static YAML files in `config/experts/`, scanned into an in-memory
cache at orchestrator startup. Users cannot create, customize, or share expert
configurations without deployment access.

| Issue | Detail |
|-------|--------|
| No user-created experts | Only operators with deployment access can add expert configs |
| No ownership or sharing | All experts globally visible; no per-user customization |
| Project experts are brittle | Scanned from a Gitea jobs-repo `experts/` directory (`main.py:15224-15352`) — undiscoverable, no UI |
| No worker/session distinction in UI | Job and session create flows show the same list, but the configs extend different bases (`defaults` vs `persistent_defaults`) with incompatible schemas |
| No CRUD UI | Experts only appear as selection grids inside job/session create flows |
| No capability gating | Nothing governs which tools/models/autonomy a user could put in a config if they *could* author one — only `users.can_use_vm` exists, as a one-off boolean column |

### How it works today (verified anchors)

| Layer | Mechanism | Location |
|-------|-----------|----------|
| Config files | YAML with `$extends` deep-merge inheritance | `config/experts/{name}/config.yaml`; merge in `src/core/loader.py:224-268` |
| Name → path resolution (agent) | `resolve_config_path()` scans disk locations | `src/core/loader.py:3122` |
| Cache + read API | `_experts_cache` + `_scan_experts()`; `GET /api/experts`, `GET /api/experts/{id}`, `POST /api/experts/reload` (admin) | `main.py:14951`, `:15000-15026`, `:15087` (`_load_expert_detail`), `:15166` |
| Project experts | Gitea jobs-repo `experts/` scan | `main.py:15224-15352` |
| Selection plumbing | `jobs.config_name`, `threads.metadata.config_name`; provisioners pass `--config {config_name}` + `AGENT_CONFIG` env | `persistent_provisioner.py:496,516`, `agent_provisioner.py:1008` |
| DB config overrides (the pattern to mirror) | `config_overrides` table → agent loads rows at job first-run behind `CONFIG_DB_OVERRIDES_ENABLED`; lazy text lookup via `MatrixResolver._db_lookup`, eager settings merge | migration `0022`; `src/core/loader.py:60-159`; `src/agent.py:1049-1078`; `src/database/postgres_db.py:976-990` |
| The existing capability grant | `users.can_use_vm` (default-deny column, admin bypass, kill-switch in `system_settings['vm_workspaces']`, enforced at point of use) | `0001_initial.sql:96-100`; `main.py:1844`; admin PATCH `main.py:18076` |
| Per-user preference bag | `users.settings` JSONB, self-service `GET/PATCH /api/users/me/settings`; **`settings.persistent_agent` is merged as the base of session `config_override`** | `0001_initial.sql:113`; `main.py:16966/16983`; merge at `main.py:11939-11948` |
| Subjob expert references | `verification.critic_config` / `scholar.scholar_config` / `curator.curator_config`, defaulting to hardcoded names; sections read from disk only | `main.py:6829, 7522, 7673`; `completion.py:144,196` |

The `users.settings.persistent_agent → config_override` path matters for
security: a *user-writable* preference bag already flows into agent config
today. Capability enforcement must therefore validate the **merged
dispatch-time result**, not just saved experts — otherwise any expert-level
check is bypassable via a settings PATCH.

## Locked decisions

| # | Decision | Value |
|---|---|---|
| 1 | Storage model | **Overlay with fallback**, exactly like prompts/settings overrides: bundled YAML experts stay disk-resolved and canonical; the DB holds only user/admin-created rows. No YAML→DB seeding (v1 draft's sync model dropped — it duplicates truth and needs reload-overwrite hacks). Delete the row → shipped behavior returns. |
| 2 | Table | Dedicated `experts` table (migration `0026`, **not** `schema.sql` — frozen at cutover). Not a new `kind` in `config_overrides`: owner/visibility columns don't belong there. |
| 3 | Expert typing | `expert_type ∈ {worker, session}`, immutable after create. Determines the base config (`defaults.yaml` vs `persistent_defaults.yaml`). Structural, not cosmetic — the schemas are incompatible. |
| 4 | No `$extends` in user configs | The base is implied by `expert_type`. "Start from scholar" = **fork (copy)** of its fragment, not a live link. Live extension chains (cycle/depth/visibility machinery) deferred. |
| 5 | Selection | Pickers pass **`expert_id` (UUID)**; `jobs` gains a nullable `expert_id` column, threads carry `metadata.expert_id`. `config_name` remains for bundled experts and back-compat. Name-based resolution (automations, MCP convenience): **owner > project-linked > global > bundled** (most-specific wins; a personal fork named `scholar` shadows the bundled one *for that user only*). |
| 6 | Agent-side resolution | The agent loads the expert row itself at job/session start via the app-DB connection it already holds, behind an `EXPERTS_DB_ENABLED` flag — the same seam and lifecycle as `config_overrides` (load at first-run → merge → freeze into `resolved_config`). Missing row at start = **fail loud**, not silently run on base config. |
| 7 | Per-expert prompts | `experts.prompts` JSONB (v1 keys: `persona`, `instructions`) injected as the **highest-precedence layer** of the existing resolver chain: expert prompts → family DB override → global DB override → bundled file. Single text per key in v1; per-model-family variants deferred. |
| 8 | Capability grants | New scoped table `capability_grants(scope_kind ∈ {user, project, global}, scope_id, key, value_json)` + code-side catalog — the principal-scoped twin of `config_overrides` and the generalization of `can_use_vm` (which migrates in). Resolution: user → project → global row → catalog default. Admins bypass. Kill-switch `system_settings['user_experts']`. |
| 9 | Enforcement points | Twice, like `can_use_vm` today: **save-time** (422 with actionable message; editor greys out ungated controls) and **dispatch-time** on the *full merged config* (expert fragment + project override + `users.settings.persistent_agent` + job/thread `config_override`) against the **runner's** grants. Dispatch check **rejects** (no silent stripping — silent capability downgrades burn debugging time). This single checkpoint also closes the `persistent_agent` self-service hole. |
| 10 | Hard-deny sections | `llm.api_key`/`env_keys`, `connections`, `workspace.remote` are rejected in user-authored content **regardless of grants** — credentials only ever come from orchestrator dispatch injection (existing invariant). |
| 11 | Subjob references (v1) | `critic_config`/`scholar_config`/`curator_config` in user experts must name **bundled** experts (validated at save). `completion.py`'s disk readers stay untouched; DB-aware subjob resolution deferred. |
| 12 | Versioning (v1) | Lite: `version` int incremented on update + `updated_by`. Full history/rollback deferred. Running jobs are already immune to edits via the `resolved_config` freeze. **Test-drive** ships in v1: run a draft as a plain `config_override` without saving. |
| 13 | Quotas | Separate concern. `quota_limits` stays as designed in [[observability_and_quotas]] (scope-polymorphic, period-based, evaluated by the usage rollup). Grants and quota enforcement share only the dispatch seam. |
| 14 | Global publishing | Setting `is_global=TRUE` is **admin-only** in v1 (a future `publish_global` grant key can open it up). |

## Architecture

### `experts` table — migration `0026_experts.sql`

```sql
CREATE TABLE IF NOT EXISTS experts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(100) NOT NULL,        -- slug ^[a-z][a-z0-9_-]*$
    display_name VARCHAR(200) NOT NULL,
    description  TEXT,
    icon         VARCHAR(100) NOT NULL DEFAULT 'smart_toy',
    color        VARCHAR(7)   NOT NULL DEFAULT '#6B7280',
    tags         TEXT[]       NOT NULL DEFAULT '{}',
    expert_type  VARCHAR(10)  NOT NULL CHECK (expert_type IN ('worker', 'session')),
    config       JSONB        NOT NULL DEFAULT '{}',  -- fragment vs the type's base; never the merged result
    prompts      JSONB        NOT NULL DEFAULT '{}',  -- v1 keys: persona, instructions
    owner_id     UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    is_global    BOOLEAN      NOT NULL DEFAULT FALSE,
    version      INTEGER      NOT NULL DEFAULT 1,
    updated_by   UUID         REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_experts_name_owner ON experts (name, owner_id);
CREATE INDEX IF NOT EXISTS idx_experts_owner ON experts (owner_id);
CREATE INDEX IF NOT EXISTS idx_experts_type  ON experts (expert_type);

-- selection plumbing
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS expert_id UUID REFERENCES experts(id) ON DELETE SET NULL;
```

Bundled names are *not* reserved — shadowing is by design (decision 5); the
UI badges a shadowing expert. `ON DELETE SET NULL` on `jobs.expert_id` is safe
for history because `resolved_config` is frozen per job.

### `project_experts` junction (same migration)

```sql
CREATE TABLE IF NOT EXISTS project_experts (
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    expert_id       UUID NOT NULL REFERENCES experts(id) ON DELETE CASCADE,
    default_for     VARCHAR(10) CHECK (default_for IN ('worker', 'session')),  -- NULL = linked, not default
    config_override JSONB,                        -- project-level tweaks on top of the expert fragment
    linked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, expert_id)
);
-- one default worker + one default session expert per project
CREATE UNIQUE INDEX IF NOT EXISTS uq_project_default_expert
    ON project_experts (project_id, default_for) WHERE default_for IS NOT NULL;
```

(v1 draft amendment: `is_default BOOLEAN` allowed only one default *total*;
`default_for` allows one per type.)

Linking is owner-driven (link your own or a global expert to projects you're
a member of). The junction supersedes the Gitea `experts/` directory scan,
which is deprecated once this ships (read kept during a migration window).

### `capability_grants` — migration `0027_capability_grants.sql`

```sql
CREATE TABLE IF NOT EXISTS capability_grants (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_kind VARCHAR(10) NOT NULL CHECK (scope_kind IN ('user', 'project', 'global')),
    scope_id   UUID,        -- user_id | project_id | NULL for global
    key        VARCHAR(64)  NOT NULL,
    value_json JSONB        NOT NULL,
    granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CHECK ((scope_kind = 'global') = (scope_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_grants_scope_key
    ON capability_grants (scope_kind, COALESCE(scope_id, '00000000-0000-0000-0000-000000000000'), key);

-- migrate the existing one-off grant
INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'vm_workspace', 'true'::jsonb, NULL FROM users WHERE can_use_vm
ON CONFLICT DO NOTHING;
```

No FK on `scope_id` (polymorphic — same call as `quota_limits` in
[[observability_and_quotas]]). `users.can_use_vm` reads switch to the grants
service in S2; the column drops in a later cleanup migration.

**Grant catalog** (code-side, like the config catalog: key, type, default,
what it gates, description):

| Key | Type | Default | Gates in expert/override config |
|---|---|---|---|
| `vm_workspace` | bool | deny | `workspace.backend: vm` (replaces `can_use_vm`) |
| `shell_tools` | bool | deny | `tools.shell` |
| `delegation` | bool | deny | `delegation.*`, `tools.delegation` |
| `datasource_tools` | bool | allow | `tools.sql` / `mongodb` / `graph` |
| `browser` | bool | allow | `tools.browser_direct` |
| `model_selection` | list | curated subset | which `models.yaml` entries are pickable (feeds expert editor *and* session model picker) |
| `autonomy_ceiling` | enum | `review` | max `autonomy` (`full` > `review` > `partial` > `guided` > `dependent`) |

Resolution mirrors expert layering: user row → project row → global row →
catalog default. Admins bypass everything.

### Resolution flow (agent, job/session start)

```
1. Dispatch carries expert_id (env AGENT_EXPERT_ID / JobStartRequest field);
   config_name continues to carry bundled names.
2. expert_id present + EXPERTS_DB_ENABLED:
     load row from app DB (postgres_db.py, same pattern as
     list_overrides_for_family) — missing row => fail job with clear error
   else: resolve_config_path(config_name) on disk, as today.
3. Base from expert_type: defaults.yaml | persistent_defaults.yaml.
4. deep_merge: base <- expert.config (DB fragment or YAML file)
5. deep_merge: <- project_experts.config_override (orchestrator passes it in dispatch metadata)
6. Apply model_config_matrix for the resolved model family (existing).
7. Apply DB config_overrides (existing 0022 layer, family/global).
8. deep_merge: <- job/thread config_override (existing; per-job overrides win).
9. experts.prompts entries registered as the top layer of MatrixResolver:
   expert -> family DB -> global DB -> bundled file.
10. Freeze into jobs.resolved_config (existing) — edits never touch running work.
```

The orchestrator needs the same DB read for `_load_expert_detail`
(`main.py:15087`) so the UI detail view shows the resolved result, and
`/api/experts` becomes a merge of the disk scan + DB rows visible to the
caller (owned + project-linked + global), each tagged with `source`.

### Enforcement flow (orchestrator)

```
Save-time   (POST/PUT /api/experts):  validate fragment against author's
            grants + hard-deny list => 422 with the offending keys named.
Dispatch    (job create / session create / automation fire):  validate the
            FULL merged override stack against the RUNNER's grants
            => reject with actionable message. Covers experts, per-job
            overrides, and users.settings.persistent_agent uniformly.
```

A revoked grant therefore disables affected experts at next dispatch — no
background sweep needed. Shared/global experts run under the runner's grants,
not the author's.

## API surface

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/experts` | Merged list (bundled + visible DB rows). Query: `type`, `source`, `project_id` |
| `POST` | `/api/experts` | Create (grant-validated). Body: `ExpertCreate` |
| `GET` | `/api/experts/{id}` | Detail with resolved config (works for bundled names and UUIDs) |
| `PUT` | `/api/experts/{id}` | Update, owner only; bumps `version`. Bundled experts → 403 |
| `DELETE` | `/api/experts/{id}` | Owner only. Bundled → 403. See open question 1 |
| `POST` | `/api/experts/{id}/duplicate` | Fork as owned copy (any viewer; bundled experts too — this is "start from scholar") |
| `POST` | `/api/experts/reload` | Existing: re-scan bundled YAML cache (admin) |
| `GET/POST/PATCH/DELETE` | `/api/projects/{pid}/experts[/{eid}]` | Link / overrides / default-for / unlink |
| `GET` | `/api/admin/grants?scope_kind=&scope_id=` | List grants + catalog (admin) |
| `PUT/DELETE` | `/api/admin/grants/{scope_kind}/{scope_id}/{key}` | Set/clear a grant (admin; extends the `main.py:18076` user-update pattern) |
| `GET` | `/api/users/me/capabilities` | Resolved grant set for the caller (drives editor greying + pickers) |

MCP server gains the matching tools (`create_expert`, `update_expert`,
`delete_expert`, `link_expert_to_project`, …) alongside the existing
`list_experts`/`get_expert`/`reload_experts` (`mcp/client.py:1096-1149`).

## Cockpit UI

**Experts page** (nav slot + layout pattern: the datasources tab). List with
type/source filters; bundled experts badged + read-only with "Duplicate to
customize"; shadowing experts badged ("overrides built-in *scholar* for you").

**Type-aware editor** (v1 surface deliberately small):
- Identity: name, display name, description, icon, color, tags
- Persona prompt (textarea — the heart of it) + optional instructions
- Model picker (entries from resolved `model_selection` grant)
- Autonomy (capped at `autonomy_ceiling`; higher options greyed with tooltip)
- Tool toggles (curated; ungated-off toggles greyed per grants)
- Worker-only: verification/scholar/curator toggles (bundled refs only, decision 11)
- Session-only: `interactive.permission_mode`, idle timeout, greeting
- Advanced flap: raw YAML/JSON fragment editor (admin-only in v1)
- **Test-drive**: launch a session/job with the unsaved draft as `config_override`

Reuses the admin-config building blocks (typed controls from the catalog,
bundled-default panel, reset semantics) and the tool-category checkboxes from
`agent-settings`. Job-create filters `type=worker`; session-create
(`session-create.component.ts`) filters `type=session`; project-linked
experts listed first with badge. **Admin → Users** grows from the lone VM
toggle into the grants panel (per-user; project + global grant editing on the
admin config page).

## Slices (each independently shippable)

### Slice 1 — Table + resolution + selection end-to-end
- Migration `0026` (experts, project_experts, `jobs.expert_id`).
- CRUD + duplicate endpoints (no grants yet — hard-deny list only; creation
  admin-gated until S2 if we want belt-and-braces).
- `/api/experts` merge + DB-aware `_load_expert_detail`.
- Agent: `EXPERTS_DB_ENABLED` flag, expert-by-id load, base-from-type merge,
  prompts layer in MatrixResolver, fail-loud on missing row.
- Dispatch plumbing: `expert_id` through job create, session create
  (provisioner env), automations.
- **Acceptance:** create an expert via API → run one job and one session with
  it on k3d → frozen `resolved_config` contains the fragment and the persona
  is in the system prompt; deleting the row fails the *next* run loudly;
  bundled experts unaffected with the flag off.

### Slice 2 — Grants + enforcement
- Migration `0027` + catalog + grants service (user → project → global → default).
- Save-time 422; dispatch-time reject on the merged stack (incl.
  `persistent_agent`); admin bypass; `system_settings['user_experts']`
  kill-switch; `can_use_vm` reads switched to grants.
- Admin Users grants panel + `/api/users/me/capabilities`.
- **Acceptance:** non-granted user saving a `tools.shell` expert → 422 naming
  the key; grant revoked after save → next dispatch rejected with message; a
  `persistent_agent` settings PATCH smuggling `tools.shell` → session create
  rejected; VM gating behaves identically to the old column; admin bypasses.

### Slice 3 — Cockpit
- Experts page (list/create/edit/fork/delete), type-aware editor, greyed
  ungated controls fed by `/api/users/me/capabilities`, grant-fed model picker,
  picker integration (id-based, type-filtered, badges), project settings
  expert section (link/override/default-for).
- **Acceptance:** full lifecycle through the UI by a non-admin; bundled expert
  opens read-only with working fork; session-create shows only session
  experts; ungated controls visibly disabled, not hidden.

### Slice 4 — Iteration polish
- Version counter surfaced + `updated_by`; test-drive button; per-expert
  outcome stats on the detail page (success rate / avg phases from `jobs`
  by `expert_id`; cost column once the [[observability_and_quotas]] ledger
  lands); deprecate the Gitea `experts/` scan.
- **Acceptance:** editing an expert bumps the visible version and never
  changes an in-flight job; test-drive runs an unsaved draft; stats panel
  matches a hand-run SQL count.

### Deferred (explicitly out of v1)
- **AI-assisted "improve this expert"** — a builder/persistent session that
  reads the expert + transcripts of jobs that used it and proposes edits.
  Own design round once CRUD is proven.
- Live `$extends` chains between DB experts; per-model-family prompt variants
  in DB experts; per-expert `model_config_matrix` overlays; share-by-link;
  full version history/rollback; DB-aware subjob expert resolution +
  transitive grant checks for delegation chains; `publish_global` grant key.

## Anti-patterns (carried from v1, amended)

1. Mixing worker and session config in one expert — `expert_type` is immutable.
2. ~~Overwriting built-in DB rows on reload~~ → built-ins are **never in the
   DB**; the overlay makes reload-sync machinery unnecessary.
3. Storing resolved config in `experts.config` — fragments only; resolution at
   load time so base changes propagate.
4. Name as primary key — UUIDs; uniqueness is `(name, owner_id)`.
5. User-supplied `$extends` — the base is implied by `expert_type` (decision 4).
6. **New:** enforcing grants only at save time — revocation and shared experts
   make dispatch-time the check that matters; save-time is UX.

## Out of scope

- Quotas and spend limits → [[observability_and_quotas]] (`quota_limits`) and
  its billing follow-on. Same dispatch seam eventually, different evaluator.
- User preference storage → `users.settings` already exists; grants are
  deliberately a separate, admin-writable table.
- Org-level expert namespaces → [[multi_tenancy]] M2.

## Open questions

1. **Deletion semantics** when automations/threads reference an expert:
   block while referenced vs delete + fail-loud at next dispatch (S1 behavior).
   Lean: allow delete, fail loud — consistent with decision 6 and the freeze;
   add a "referenced by N automations" warning in the UI.
2. **Project-linked experts ↔ project datasources**: should a project-default
   expert auto-attach the project's datasources? (v1 keeps them independent.)
3. **Grant granularity for datasource tools**: one `datasource_tools` key or
   per-kind (`sql`/`mongodb`/`graph`)? Catalog makes splitting later cheap.
4. **Editor advanced flap**: admin-only (v1) or its own grant key?
5. Does the session model picker's existing free choice need to be narrowed by
   `model_selection` in S2, or grandfathered until S3? (Lean: enforce in S2 —
   dispatch validates anyway; UI catches up in S3.)

## References

- [[config_matrix_db_overrides]] — the overlay pattern this mirrors
  (migration `0022`, loader seam `src/core/loader.py:60-159`, admin editor).
- [[observability_and_quotas]] — `quota_limits` / `usage_rates` siblings;
  scope-polymorphic table convention `(scope_kind, scope_id, …)`.
- [[multi_tenancy]] — Tier 0 admission + visibility model the expert
  visibility rules compose with.
- `docs/db_migration.md` — migration file conventions (`0026`/`0027`,
  no `schema.sql` edits).
- `users.can_use_vm` (`0001_initial.sql:96-100`, `main.py:1844`, `:18076`) —
  the grant pattern being generalized.
- `users.settings.persistent_agent` merge (`main.py:11939-11948`) — the
  existing user-writable config path dispatch-time enforcement must cover.
