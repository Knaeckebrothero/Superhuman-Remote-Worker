---
tags:
  - feature
  - orchestrator
  - cockpit
  - automations
  - v0
aliases:
  - automations mvp
  - automations v0
related:
  - "[[automations]]"
---

# Automations v0 — Implementation Spec

Focused, implementation-ready cut of the parent [Automations design](./automations.md). v0 ships cron-only triggers, a tabbed editor with presets, run-now / pause / resume, run history, an "Use the API for more" escape-hatch panel, and a small API Keys settings page. v0.5 adds event triggers without further schema migration.

The parent doc remains the long-term reference; this doc is what gets built first.

**Status:** Spec, ready to implement.

**Estimated effort:** v0 ≈ 5–6 dev days. v0.5 ≈ 5–7 dev days on top.

## Why a Phased Cut

The parent design is comprehensive and covers ~3 weeks of work. The motivating use case ("friend wants Monday-morning Instagram-post drafts emailed to him") is fully served by cron triggers alone. The competitive research confirmed that ChatGPT Tasks, Gemini Scheduled Actions, Manus, Dust, and Glide all ship schedule-only and link to external tools (n8n, Zapier) for everything else — none of them rebuilt n8n. We follow that path: native cron + a prominent API escape hatch covers the common case in v0; event triggers (our actual differentiator per the research) follow in v0.5.

## Scope

### What v0 ships

**Triggers**
- **Cron only.** Preset-first editor: "Every day / weekday / week / month at HH:MM." Custom cron behind an Advanced toggle.

**Cockpit "Automations" tab**
- New top-level nav item alongside Jobs / Projects / Threads.
- **List view**: Name, Trigger (humanized via `cronstrue`), Expert, Last-fired status, Next run, Enabled toggle, Actions (Run-now, Edit, Delete).
- **Editor view**: Name, Description, Cron (presets + Advanced cron), Timezone (defaults to user's browser tz), Expert dropdown, Project (optional), Prompt, Advanced section (Autonomy, Model override, Catchup window, Max fires per day, Priority).
- **Cron preview**: "Next 5 runs" widget (`cron-parser`) + natural-language explanation (`cronstrue`). **Timezone disclaimer prominently shown** below the cron field.
- **Escape-hatch panel** (sticky at bottom of the Automations tab, prominent):
  - Heading: *"Need more than schedules?"*
  - Body: *"Use the orchestrator API directly — schedule from your own cron, n8n, Zapier, or any HTTP client. Job-completion chains, inbound webhooks, branching workflows, and multi-step DAGs are all best handled there."*
  - Three links:
    1. **API documentation** → `/api/docs` (FastAPI Swagger UI, already auto-generated)
    2. **External API guide** → `docs/features/automations_api.md` (new) — curated subset of endpoints for n8n / Zapier / curl / Python consumption with examples
    3. **Create API key** → Settings → API Keys page

**API Keys settings page** (small new feature, sibling to escape-hatch)
- Path: `cockpit/src/app/views/settings/api-keys/`
- List user's API keys: name, created date, last used, optional expiration.
- **Create**: dialog with name + optional expiration. Returns the key **once** (copy-to-clipboard with a "you won't see this again" warning).
- **Revoke**: button per row, confirms before deletion.
- Backed by the consolidated `auth_tokens` table (with `kind='api'`) introduced by the auth refactor — see [auth_bff_and_api_tokens.md §3.6](./auth_bff_and_api_tokens.md). The earlier plan for a parallel `api_keys` table was reversed 2026-05-14; see [Open Decisions](#open-decisions) below.

**Cross-links: Automations ↔ Projects**
- From an automation row: if `project_id` is set, project name links to the project detail page.
- From a project detail page: new "Automations" section listing `WHERE project_id = $1`. "Create automation" button prefills `project_id` in the editor (URL query param `?project=<id>`).

**Run history**
- Each automation's "Runs" view = filtered job list `WHERE context->>'automation_id' = $1`.
- Reuses existing job detail view. Small badge on each triggered job: *"Triggered by automation: &lt;name&gt;"*.

**Endpoints (v0 subset)**

```
GET    /api/automations                  # list (filtered by owner / project)
POST   /api/automations                  # create
GET    /api/automations/{id}             # read
PATCH  /api/automations/{id}             # update
DELETE /api/automations/{id}             # delete
POST   /api/automations/{id}/run-now     # fire immediately
POST   /api/automations/{id}/pause       # enabled = false
POST   /api/automations/{id}/resume      # enabled = true; recompute next_run_at
GET    /api/automations/{id}/runs        # list of spawned jobs

GET    /api/api-keys                     # list user's API keys
POST   /api/api-keys                     # create (returns plaintext once)
DELETE /api/api-keys/{id}                # revoke
```

Deferred to v0.5: `/api/automations/{id}/preview`, `/api/automations/{id}/chain`.

**Backend services**
- New: `orchestrator/services/cron_dispatcher.py` — 60s tick, `FOR UPDATE SKIP LOCKED` CTE pattern.
- New: `orchestrator/services/automations.py` — `create_job_from_automation` helper that reuses the existing `create_job()` path.
- Migration: `orchestrator/database/migrations/app/NNNN_create_automations.sql` (number assigned at merge; next-available after auth refactor lands) — **full schema including event-trigger columns** so v0.5 needs no migration.
- API key storage is provided by the auth refactor's `0010_auth_tokens_consolidation.sql`; no separate migration in this PR.

**Safety guards in v0**
- `croniter.is_valid(expr)` validation on write → 400 on bad cron.
- `zoneinfo.available_timezones()` validation on write → 400 on unknown tz.
- Per-user soft cap: 20 automations (warn, don't block).
- Minimum cron interval: 5 minutes for non-admin users (configurable env var).
- Per-automation `max_fires_per_day` (default 100) → auto-pause on exceed + notify owner.
- Catchup window default 24h.
- DST-aware: store `Europe/Berlin` not `+01:00`; use `zoneinfo`, not `pytz`.

### What v0.5 adds (next milestone, not this one)

- Event triggers (`event_filter` schema, `event_dispatcher`, `LifecycleEvent` types).
- `emit_lifecycle_event` calls from all four terminal paths in `main.py` (`complete`, `approve`, manual cancel, timeout sweeper).
- "When something happens" trigger type in the editor.
- Event preview widget ("last 5 jobs that would have matched").
- Chain tree view.
- Per-chain cost cap (elevated to v0.5 by the November 2025 $47K incident).
- Fingerprint-based loop detection at automation layer.
- IMAP-driven `inbound_email` event source (small extension on top of the event-trigger plumbing).

### What's deferred to v1+ (Phase 2+)

- `phase_complete` events.
- Per-automation `overlap_policy` (Temporal vocabulary: `Skip / BufferOne / BufferAll / AllowAll / CancelOther / TerminateOther`).
- Auto-disable after N consecutive failures.
- MCP tools (`create_automation`, `list_automations`).
- Default-installed system automations (replace the code-level fallback for critic/curator spawn).
- Cost forecasting.
- Public automation catalog.

## Schema (v0)

Migration goes in `orchestrator/database/migrations/app/0003_create_automations.sql`. **Includes the full schema** (cron + event columns) from the parent doc — v0 only exercises the cron subset, but the database accepts both trigger types from day one so v0.5 needs no schema migration. Unused columns are nullable; cost is bytes per row, benefit is a clean upgrade.

```sql
-- migration:     0003_create_automations.sql
-- description:   Automations table (cron + future event triggers)
-- depends-on:    0002_collapse_thread_status
-- expected:      ~100ms; no full table scans
-- transactional: yes

BEGIN;
  SET LOCAL statement_timeout = '30s';
  SET LOCAL lock_timeout = '10s';

  CREATE TABLE IF NOT EXISTS automations (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      project_id UUID REFERENCES projects(id) ON DELETE CASCADE,

      name TEXT NOT NULL,
      description TEXT,
      trigger_type TEXT NOT NULL CHECK (trigger_type IN ('cron', 'event')),

      -- Cron-trigger fields
      cron_expr TEXT,
      timezone TEXT NOT NULL DEFAULT 'UTC',
      catchup_window_seconds INTEGER NOT NULL DEFAULT 86400,

      -- Event-trigger fields (unused in v0; populated in v0.5)
      event_filter JSONB,

      enabled BOOLEAN NOT NULL DEFAULT true,

      -- Job template
      expert TEXT NOT NULL,
      prompt TEXT NOT NULL,
      config_override JSONB NOT NULL DEFAULT '{}'::jsonb,
      autonomy TEXT NOT NULL DEFAULT 'review',
      priority INTEGER NOT NULL DEFAULT 5,

      -- Safety guards (event guards unused in v0; cron guards active)
      max_chain_depth INTEGER NOT NULL DEFAULT 10,
      max_fires_per_day INTEGER NOT NULL DEFAULT 100,
      fires_today_count INTEGER NOT NULL DEFAULT 0,
      fires_today_date DATE,

      -- Cron state
      next_run_at TIMESTAMPTZ,
      last_scheduled_at TIMESTAMPTZ,
      last_dispatched_at TIMESTAMPTZ,
      last_fired_at TIMESTAMPTZ,
      last_job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,
      last_status TEXT,

      run_count INTEGER NOT NULL DEFAULT 0,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

      CONSTRAINT cron_trigger_has_expr
        CHECK (trigger_type <> 'cron' OR cron_expr IS NOT NULL),
      CONSTRAINT event_trigger_has_filter
        CHECK (trigger_type <> 'event' OR event_filter IS NOT NULL)
  );

  CREATE INDEX IF NOT EXISTS idx_automations_due
      ON automations (next_run_at)
      WHERE enabled = true AND trigger_type = 'cron' AND next_run_at IS NOT NULL;

  CREATE INDEX IF NOT EXISTS idx_automations_event
      ON automations USING GIN (event_filter jsonb_path_ops)
      WHERE enabled = true AND trigger_type = 'event';

  CREATE INDEX IF NOT EXISTS idx_automations_owner ON automations (owner_id);
  CREATE INDEX IF NOT EXISTS idx_automations_project ON automations (project_id);
COMMIT;
```

No separate API key migration in this PR. The auth refactor ships `auth_tokens` (renamed from `mcp_tokens`, with `kind` column added) in `0010_auth_tokens_consolidation.sql`; this PR creates API key rows with `kind='api'` against that table. See [auth_bff_and_api_tokens.md §3.2](./auth_bff_and_api_tokens.md) for the schema.

Dependency: this PR cannot merge until the auth refactor PR 3 (which lands `auth_tokens` consolidation) is merged.

## Implementation Order

| Step | Work | Files | Effort |
|------|------|-------|--------|
| 1 | Migration: `automations` | `migrations/app/NNNN_create_automations.sql` | 0.2d |
| 2 | Postgres helpers for automations + API key helpers against `auth_tokens` | `orchestrator/database/postgres.py` | 0.5d |
| 3 | Cron dispatcher service + tests (DST, multi-replica `SKIP LOCKED`) | `services/cron_dispatcher.py`, `tests/test_cron_dispatcher.py` | 1d |
| 4 | `create_job_from_automation` helper | `services/automations.py` | 0.3d |
| 5 | API endpoints — automations CRUD + run-now + pause/resume | `orchestrator/main.py` | 1d |
| 6 | API endpoints — `/api/api-keys` CRUD over `auth_tokens` (kind='api'). Bearer dispatch in `_resolve_bearer_token` lands with the auth refactor; this PR just wires the user-facing endpoints. | `orchestrator/main.py` | 0.3d |
| 7 | Cockpit Automations tab — list + editor + cron preview + escape-hatch panel | `cockpit/src/app/views/automations/` | 1.5d |
| 8 | Cockpit API Keys settings page | `cockpit/src/app/views/settings/api-keys/` | 0.5d |
| 9 | Cockpit project-detail: Automations section + cross-link | `cockpit/src/app/views/projects/project-detail/` | 0.3d |
| 10 | External API docs page | `docs/features/automations_api.md` | 0.3d |
| 11 | i18n keys (EN + DE) | `cockpit/src/assets/i18n/{en,de-DE}.json` | 0.3d |

**Total: ~5.3 dev days.** Depends on auth refactor PR 3 (consolidation) being merged first.

## Files Created

### Backend
- `orchestrator/database/migrations/app/NNNN_create_automations.sql` (next-available number at merge time)
- `orchestrator/services/cron_dispatcher.py`
- `orchestrator/services/automations.py`
- `tests/test_cron_dispatcher.py`
- `tests/test_automations_api.py`
- `tests/test_api_keys.py` (tests the `/api/api-keys` endpoints against the consolidated `auth_tokens` table)

### Frontend
- `cockpit/src/app/views/automations/automations-page.component.ts`
- `cockpit/src/app/views/automations/automations-list.component.ts`
- `cockpit/src/app/views/automations/automations-editor.component.ts`
- `cockpit/src/app/views/automations/cron-preview.component.ts`
- `cockpit/src/app/views/automations/escape-hatch-panel.component.ts`
- `cockpit/src/app/core/services/automations.service.ts`
- `cockpit/src/app/views/settings/api-keys/api-keys-page.component.ts`
- `cockpit/src/app/views/settings/api-keys/api-key-create-dialog.component.ts`
- `cockpit/src/app/core/services/api-keys.service.ts`

### Docs
- `docs/features/automations_api.md` (external API guide for n8n/Zapier/curl users)

## Files Modified

| File | Change |
|------|--------|
| `orchestrator/main.py` | Mount `/api/automations` and `/api/api-keys` routes; start `cron_dispatcher` in lifespan at `:3008-3023`. The Bearer-dispatch path that accepts `ak_…` tokens lands with the auth refactor (see `auth_bff_and_api_tokens.md` §3.5). |
| `orchestrator/security/auth.py` | No change in this PR — the Bearer dispatch (`_resolve_bearer_token`) is added by the auth refactor and handles both `ak_…` and `srw_…` prefixes. |
| `requirements.txt` | Add `croniter` if not already present |
| `cockpit/package.json` | Add `cronstrue` and `cron-parser`. Evaluate `ngx-cron-editor` (v0.10.2, Nov 2025) as the picker; fall back to hand-built preset picker if friend-test fails |
| `cockpit/src/app/app.routes.ts` | Add `/automations` and `/settings/api-keys` routes (auth-guarded) |
| `cockpit/src/app/shell/sidebar/sidebar.component.ts` | Add "Automations" nav link with `schedule` icon |
| `cockpit/src/app/views/projects/project-detail/...` | Add "Automations" section + "Create automation for this project" button |

## API Key Auth Wiring

The Bearer dispatch (`_resolve_bearer_token`) is owned by the auth refactor — see [auth_bff_and_api_tokens.md §3.5](./auth_bff_and_api_tokens.md). It handles `ak_…` (API key, `kind='api'`) and `srw_…` (legacy MCP, `kind='mcp'`) by prefix-sniff, hashes via SHA-256, looks up against the consolidated `auth_tokens` table.

Lookup flow (for context — this PR just wires the user-facing `/api/api-keys` endpoints):
1. Header `Authorization: Bearer ak_<key>` → prefix matches `ak_`.
2. `hashlib.sha256(token).hexdigest()`.
3. `SELECT user_id, kind, scope, expires_at FROM auth_tokens WHERE token_hash = $1 AND kind = 'api'`.
4. Row found + not revoked + not expired + scope permits → set `request.state.user_id = user_id`, fire-and-forget update of `last_used_at`/`last_used_ip`.
5. Else 401.

Key format: `ak_` prefix + 32 random URL-safe characters (256 bits of entropy via `secrets.token_urlsafe(32)`). The `ak_` prefix lets future key types (`ar_` read-only, `as_` service, etc.) coexist without lookup ambiguity. Format and entropy details in [auth_bff_and_api_tokens.md §3.1](./auth_bff_and_api_tokens.md).

## Friend-Test Gates Before Shipping

These are the must-pass checks before merging v0:

1. **Non-technical user creates a working "Every Monday at 7:00 send me Instagram-post drafts" automation in under 90 seconds**, without reading docs.
2. **Cron preview shows correct next-5-runs in the user's timezone**, with the timezone disclaimer visible.
3. **Run-now button fires the job within 5 seconds**, surfaces an error toast if it fails.
4. **Pause/resume toggle is reversible** without confusing UI state.
5. **Escape-hatch panel is visible without scrolling** on a typical laptop screen, with all three links functional.
6. **API key generated in the cockpit successfully creates a job** via `curl -X POST /api/jobs -H "Authorization: Bearer ak_…"`.
7. **Project-scoped automation appears on the project detail page**, and "Create automation" from a project page prefills `project_id`.

## Open Decisions

1. **API key storage:** ~~new `api_keys` table~~ → **reversed 2026-05-14: consolidate into the renamed `auth_tokens` table** (formerly `mcp_tokens`) with a `kind` column. Rationale: the schema of `mcp_tokens` is generic; "MCP session semantics" lives in the validator path (FastMCP's `TokenVerifier`), not the data shape. The wider auth refactor consolidating sessions and tokens makes parallel tables redundant. See [auth_bff_and_api_tokens.md §3.6](./auth_bff_and_api_tokens.md) for the full reasoning. The original decision was filed before the BFF refactor was on the table.
2. **Escape-hatch placement:** sticky panel at bottom of Automations tab (chosen) vs. modal triggered by a button vs. dedicated page. Reasoning: discoverability beats compactness; sticky panel is the same pattern HomeAssistant uses for "More info" links.
3. **API key scopes (v0):** single `jobs:write` scope (chosen) vs. granular (`jobs:read`, `jobs:write`, `automations:write`). Reasoning: v0 has one consumer pattern (external triggering); split scopes when there's a real second consumer.
4. **Cron picker UI:** `ngx-cron-editor` evaluation. If it passes the friend-test, use it; if not, hand-built preset picker. Decide during step 7.
5. **`autonomy` field on `JobCreate`:** add explicit field (chosen) vs. bake into `config_override`. Reasoning: cleaner; `JobCreate` already accepts `priority` as an explicit field.
6. **Code-level fallback vs. default-installed automation** for the existing critic/curator spawn: code-level fallback for v0 (no change to current behavior). Promote to default-installed system automation in Phase 2 once events ship and stabilize.
7. **`context.tags` standardization:** v0 doesn't exercise tags (cron triggers don't filter on them), but v0.5 will. Add a `ContextDict` TypedDict in `postgres.py` as part of this work so v0.5 doesn't need a separate normalization pass.

## Notes on the Escape-Hatch Panel

The panel is the load-bearing UX element for the "we didn't rebuild n8n" decision. If users can't find the API path when their use case outgrows native automations, they bounce. Concrete copy proposal (translated for `de-DE`):

> **Need more than schedules?**
>
> Native automations cover schedules. For job-completion chains, inbound webhooks, branching workflows, or event-driven triggers, use the orchestrator API directly from any HTTP client, n8n, Zapier, or your own scripts.
>
> **API documentation** &nbsp;·&nbsp; **External API guide** &nbsp;·&nbsp; **Create API key**

Three links, no marketing language, no progress disclosure. The user knows what they need; we get out of the way.

## Out-of-Scope but Adjacent: Project Shared Folders

User mentioned "AI team with shared folders" as the inter-agent communication path that doesn't need n8n. That's a separate feature (project workspaces / artifact sharing) — not v0 scope. The escape-hatch panel could mention it in a future revision once the feature exists, but for v0 we don't reference it.

## Migration Path to v0.5

v0.5 work, after v0 lands:
1. Add `LifecycleEvent` dataclass + in-process `asyncio.Queue` (`services/lifecycle_events.py`).
2. Wire `emit_lifecycle_event` into the four terminal paths in `main.py` (`complete`, `approve`, manual cancel, timeout sweeper). After DB commit, never before.
3. Build `services/event_dispatcher.py` consuming the queue, matching against `event_filter`, calling `create_job_from_automation`.
4. Cockpit editor: add "When something happens" trigger type alongside "On a schedule." Same form, different fields.
5. Per-chain cost cap aggregator. `chain_id` is already the join key.
6. Fingerprint loop detection at automation layer.
7. Chain tree view component.
8. v0.5+1: IMAP-driven `inbound_email` event source — small additional feature on top of v0.5 plumbing.

**No v0.5 schema migration required** — `event_filter`, `max_chain_depth`, `max_fires_per_day` columns already exist from v0.

## References

- Parent design: [`automations.md`](./automations.md) — full design, competitive context, deferred work, open questions.
- Migration runbook: [`db_migration.md`](../db_migration.md).
- Job creation entry point: `orchestrator/main.py` `POST /api/jobs` handler at line 3338, helper `create_job()` (~450 lines).
- Background-task lifespan: `orchestrator/main.py:3008-3023`.
- Auto-assign dispatcher (job ordering): `orchestrator/main.py:2146`, `orchestrator/database/postgres.py:1948`.
- Existing migrations: `orchestrator/database/migrations/app/` (last applied: `0002_collapse_thread_status.sql`).
