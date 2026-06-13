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

**Status:** v0 backend + cockpit shipped 2026-05-18, live-verified on the experimental cluster (`superhuman-remote-worker.com`). The editor race fix landed in `0a193cca` and shipped with the next deploy. A follow-up verification on 2026-05-20 confirmed the two remaining load-bearing paths that the initial probes had skipped — the background cron tick actually fires scheduled jobs, and PAT (`ak_*`) auth works end-to-end against `/api/automations`. A **polish sweep on 2026-05-20** closed three of the four `Open follow-ups` (project-scoped list view, auto-disable notification, cockpit unit-test coverage) and was live-verified end-to-end the same day; only the v0.5-deferred `/api/automations/{id}/preview` endpoint remains open. The feature is functional in production. See [Implementation log](#implementation-log), [Live verification](#live-verification-2026-05-18-superhuman-remote-workercom), [Follow-up verification](#follow-up-verification-2026-05-20-cron-tick--pat-auth), and [Open follow-ups](#open-follow-ups-not-blockers) below.

**Estimated effort:** v0 ≈ 5–6 dev days (came in at ~5 — the API Keys page line item was already done by the auth refactor PR 3). v0.5 ≈ 5–7 dev days on top.

## Implementation log

Landed in a single sweep on 2026-05-18 (after the auth refactor's `0010_auth_tokens_consolidation.sql` had made `auth_tokens` available). No per-PR split; everything below shipped together.

### Backend

| Concern | File(s) |
|---|---|
| Schema | `orchestrator/database/migrations/app/0015_create_automations.sql` — full cron + event-trigger columns, four indexes (cron-due partial, event-filter GIN partial, owner, project), CHECK constraints enforcing per-type required fields |
| DB helpers | `orchestrator/database/postgres.py` — `create_automation`, `get_automation`, `list_automations`, `update_automation` (allowlist-validated kwargs), `delete_automation`, `list_automation_runs` (joins `jobs.context->>'automation_id'`), and dispatcher-side `fetch_next_due_cron_automation` / `advance_automation_after_fire` / `skip_automation_fire` / `auto_disable_automation` taking an explicit conn so they compose into one transaction |
| Job materialization | `orchestrator/services/automations.py` — `create_job_from_automation(db, row, *, trigger_kind)`. Injects `autonomy` into `config_override` (jobs schema has no top-level autonomy column; dispatch reads it from `config_override.autonomy` at `main.py:933-935`). Stamps `context.automation_id`, `automation_name`, `automation_trigger` so the runs-view join works and the cockpit job-detail badge has the breadcrumb back |
| Cron tick loop | `orchestrator/services/cron_dispatcher.py` — 60s tick (env-tunable via `AUTOMATIONS_TICK_SECONDS`). Per-row transaction: `SELECT … FOR UPDATE SKIP LOCKED` claim → croniter advance in row's `timezone` (zoneinfo, DST-aware) → fire (or skip if past `catchup_window_seconds`, or auto-disable if `fires_today_count >= max_fires_per_day`, or auto-disable if `croniter.is_valid(cron_expr)` now returns false on a stored row). Multi-replica safe by construction — concurrent ticks see disjoint subsets of due rows |
| API surface | `orchestrator/routers/__init__.py` + `orchestrator/routers/automations.py` — **first `APIRouter` in the project**, starting the [main.py monolith refactor](../issues/orchestrator_main_py_monolith.md). 9 endpoints: CRUD + run-now + pause + resume + runs. Pattern: late `from main import postgres_db` inside each handler body to dodge circular import at module load time (same convention `orchestrator/auth/bff.py` set). ACL: caller-owned by default; project-scoped reads/writes via `require_project_member` (viewer for read, editor for create/update/delete). The `run-now` handler also late-imports `_trigger_dispatch` so the spawned job is picked up by auto-assign within a tick instead of waiting its 30s |
| Wiring | `orchestrator/main.py` — `app.include_router(automations_router)` alongside `bff_router`/`graph_router`/`uploads_router`; `cron_dispatcher_loop(postgres_db, _shutdown_event, on_job_created=_trigger_dispatch)` mounted in the existing `asyncio.create_task` background lineup at `:3267`-ish, awaited on shutdown alongside the other sweepers |
| Dep | `requirements.txt` + `orchestrator/requirements.txt` — `croniter>=2.0.0`. (The dep was already in `orchestrator/requirements.txt`; the top-level file got it added too per the codebase convention — `docker/Dockerfile.orchestrator` builds from `orchestrator/requirements.txt`, not the top-level one, so the orchestrator-side has to carry every orchestrator dep.) |

### Cockpit

| Concern | File(s) |
|---|---|
| HTTP service | `cockpit/src/app/core/services/automations.service.ts` — signal-based; `automations`/`isLoading` signals; full CRUD + run-now / pause / resume / runs; `loadMine()` and `loadByProject(id)` flavors |
| Page | `cockpit/src/app/views/automations/automations-page.component.ts` (+ `.html` + `.scss`) — list + inline editor (sliding card pattern; list is replaced by editor when active). Preset chips: every day / every weekday / every Monday / first-of-month / every hour / custom. Time picker for non-custom presets; free-form input for custom. Timezone defaults to `Intl.DateTimeFormat().resolvedOptions().timeZone`. Advanced section (collapsed) for autonomy / priority / max_fires_per_day / catchup_window |
| Cron preview | `cron-preview.component.ts` — uses `cronstrue` for humanization and `cron-parser` for the next-5-runs widget. Timezone disclaimer always shown. Invalid input renders `cron-preview--invalid` state with cronstrue's underlying error detail |
| Escape-hatch | `escape-hatch-panel.component.ts` — three links: API documentation (`environment.apiUrl` → `/api/docs`), External API guide (GitHub blob URL to `docs/features/automations_api.md`), Create API key (`/settings/api-keys`). Always visible at bottom of the page |
| Nav | `cockpit/src/app/app.routes.ts` — `/automations` route, `authGuard`. `cockpit/src/app/shell/sidebar/sidebar.component.ts` — "Automations" link with `schedule` icon, between Data Sources and Create |
| Project cross-link | `cockpit/src/app/views/project-detail/project-detail.component.ts` — "Manage automations" ghost button in the overview tab's action row; navigates to `/automations?project=<id>`. The `/automations` page reads the query param and opens the editor pre-filled |
| Deps | `cockpit/package.json` — `cron-parser ^4.9.0`, `cronstrue ^2.50.0` |
| i18n | `cockpit/src/assets/i18n/{en,de-DE}.json` — 28 keys under `automations.*`, plus `nav.automations` and `projectDetail.overview.manageAutomations`. Parity check passes (1446 keys both sides) |

### Tests

| File | What it covers |
|---|---|
| `tests/test_cron_dispatcher.py` | 30 tests across DST-correctness (Europe/Berlin spring-forward + fall-back), cron/timezone validation, and the per-tick state machine paths (no-due → false, due → fires, catchup-window skip, max_fires_per_day auto-disable, yesterday's counter doesn't block, invalid stored cron auto-disables) |
| `tests/test_automations_service.py` | 6 tests on the row→job translation: template fields map to `create_job` args, `context.automation_id`/`name`/`trigger` stamped, autonomy injected into `config_override`, template-level `config_override.autonomy` takes precedence over row-level default, trigger_kind propagates, project_id passes through |

Full backend suite (5361 tests) re-ran clean after the changes (1 unrelated `test_database_phase1::test_connect_disconnect` skipped — env needs a live Postgres on `:5432`).

### Docs

- `docs/features/automations_api.md` — external integration guide linked from the escape-hatch panel. TL;DR + PAT setup + endpoint reference + n8n / Zapier / curl / Python recipes + rate-limit notes + roadmap callouts (outbound webhooks deferred, etc.)

## Live verification (2026-05-18, `superhuman-remote-worker.com`)

Playwright drive against the dev cluster using the owning user's cookie session. 11 of 12 probes passed end-to-end; the 12th surfaced a UX race that has since been fixed in tree (see next section).

1. Sidebar **Automations** link with `schedule` icon, between Data Sources and Create — active state lights when on `/automations`. ✓
2. Empty state renders `automations.list.empty` copy + escape-hatch panel below, all three links target the right URLs (`https://api.superhuman-remote-worker.com/api/docs`, GitHub blob URL for the API guide, `/settings/api-keys`). ✓
3. "New automation" opens the editor inline; timezone defaults to the user's browser tz (Europe/Berlin in this case) via `Intl.DateTimeFormat`; expert dropdown populated with 7 entries from `/api/experts`. ✓
4. Cron preview for `every day / 09:00 / Europe/Berlin` shows "At 09:00" + 5 future runs in local time + the timezone disclaimer. ✓
5. Create → returned row persisted with `cron_expr: "0 9 * * *"`, `timezone: "Europe/Berlin"`, `next_run_at: "2026-05-19T07:00:00Z"` — **DST handling verified live**: 09:00 CEST = 07:00 UTC. ✓
6. Run-now spawned job `6a002d2e-c364-4c60-9c32-b6d08a7c8b1a` with `status=processing` (auto-assign nudge worked — the `_trigger_dispatch` callback woke the dispatcher). The runs endpoint returned that job with `config_name=bughunter` and the prompt matching the template. ✓
7. Pause → `enabled=false`, `next_run_at=null`, "Paused" badge visible on the row, "Next run" meta-item removed. ✓
8. Resume → `enabled=true`, `next_run_at` recomputed via `compute_initial_next_run` and surfaced in the row. ✓
9. Project list cross-link: "Manage automations" on a project's overview tab navigates to `/automations?project=<uuid>`. ✓
10. Reverse cross-link: a project-scoped automation row renders an "Open project" link pointing back at the project detail page. ✓
11. Backend validation: `POST /api/automations` with `cron_expr: "not a cron"` → 400 `Invalid cron expression: 'not a cron'`; with `timezone: "Europe/Atlantis"` → 400 `Unknown timezone: 'Europe/Atlantis'`. ✓
12. UI cron preview validation: typing a bad expression in custom-cron mode renders the `cron-preview--invalid` state with the i18n'd error and cronstrue's underlying detail ("Expression contains invalid values: 'this'"). ✓

Delete confirmed (204 + row gone) on cleanup; the test rows were removed before close.

## Bug found + fixed: editor race on `?project=` cross-link

**Symptom (probe 12 follow-up).** Opening `/automations?project=<uuid>` from a project's "Manage automations" button opened the editor with everything visually correct — Name field empty, Expert combobox showing "Bug Hunter" as the native default. Filling Name + Prompt + clicking Create surfaced the validation banner ("Name, expert, prompt, and a valid cron expression are required.") instead of saving the row. A direct `POST /api/automations` with the same body via `fetch` succeeded (201), proving the backend was fine.

**Cause.** `ngOnInit` ran two unrelated subscriptions in parallel: one for `api.getExperts()` (async), and one for `route.queryParamMap.subscribe(...)` which called `openEditor(null, projectId)` synchronously the moment the page bound. The `openEditor` seed read `this.experts()[0]?.id ?? ''` — but `experts` was still `[]` at that point, so the editor signal got `expert: ''`. The DOM `<select>` then defaulted visually to its first option ("Bug Hunter") which masked the divergence. Save-time validation read the signal, saw `e.expert === ''`, and rejected.

This affected any user with a slow `/api/experts` response opening the editor via the project cross-link — not just Playwright. Manually clicking "New automation" after the page settled was always fine because by then `experts` had loaded.

**Fix** in `cockpit/src/app/views/automations/automations-page.component.ts` (build clean; pending the next deploy):

```ts
this.api.getExperts().subscribe({
  next: (list) => {
    this.experts.set(list);
    const pending = this.route.snapshot.queryParamMap.get('project');
    if (pending && !this.editor()) {
      this.openEditor(null, pending);
    }
  },
  error: () => this.experts.set([]),
});
```

Two changes from the original: (a) the queryParam-driven `openEditor` moves inside the `next` callback so it can't fire before experts are ready; (b) reads from `route.snapshot.queryParamMap` instead of subscribing — we don't need to react to later URL changes, only the initial one. The `!this.editor()` guard keeps the same idempotency the subscription gave.

The fix landed squashed into `0a193cca` ("Introduce streaming reasoning content extraction for Chat Completions") and shipped with the subsequent `deploy: update image tags to sha-0a193cc` rollout.

## Follow-up verification (2026-05-20): cron tick + PAT auth

The 2026-05-18 acceptance run covered every UI- and HTTP-driven path but skipped two paths that aren't directly clickable: the *background* cron tick (everything previous to this had been observed only via the `/run-now` button, which is a different code path) and PAT (`ak_*`) auth on `/api/automations` (the integration guide promises it, but it had never actually been exercised). Both probes ran against the live dev cluster.

### Cron tick fires scheduled automations

Created an `* * * * *` automation in `Europe/Berlin` at `09:53:02 UTC`. Server seeded `next_run_at = 09:54:00 UTC`. Polled state without ever clicking *Run now*:

| Tick | Scheduled (`next_run_at` before fire) | Actually fired (`last_fired_at` after fire) | Spawned job |
|---|---|---|---|
| 1 | `09:54:00 UTC` | **`09:54:55 UTC`** | (first row in `/runs`) |
| 2 | `09:55:00 UTC` | **`09:55:55 UTC`** | `c13d8f9e-bd60-4b75-93af-7c11e31b0af5` (status `processing`) |

Both fires landed `:55` seconds past the minute, which proves the dispatcher loop is on a stable 60s cadence offset from the minute boundary (pod start time determined the phase). Worst-case dispatch lag = `TICK_SECONDS`, exactly as designed. `run_count`, `fires_today_count`, `last_scheduled_at`, `last_dispatched_at`, `last_fired_at`, and `next_run_at` all advanced cleanly after each fire. The second-tick job dispatched to an agent and reached `processing` within ~5s — the `on_job_created=_trigger_dispatch` callback woke the auto-assign loop. Automation deleted (204) before a third tick to avoid leaving a recurring fire in the cluster.

### PAT auth on `/api/automations`

| Step | Request shape | Result |
|---|---|---|
| Mint PAT | `POST /api/api-keys` (cookie) with `scopes=[jobs:read, jobs:write]`, `expires_in_days=30` | 200, returned `ak_uT2…` |
| List with PAT | `GET /api/automations`, `credentials: 'omit'` + `Authorization: Bearer ak_…` | 200, body identical to the cookie response |
| Create with PAT | `POST /api/automations` with same Bearer header | 201; `owner_id` resolved to the same user the PAT belongs to |
| Delete with PAT | `DELETE /api/automations/{id}` with same Bearer header | 204 |
| Revoke PAT | `DELETE /api/api-keys/{id}` (cookie) | 200 |

`credentials: 'omit'` on the PAT calls means the cookie session couldn't sneak in — the Bearer header is doing all the work. This validates the integration guide we shipped in `automations_api.md`: n8n / Zapier / curl can drive the API end-to-end with just an `ak_*` token, no browser session needed. The PAT dispatcher in `security/auth.py` (added by the auth refactor PR 3) correctly resolves the token to its owning user across both read and write endpoints.

### Net result

The "definitely needed" verification list is empty. Every path the feature relies on — cron tick, manual `run-now`, pause/resume, project cross-link (after the race fix), PAT auth, validation surfaces, runs list join, delete — has been observed working live. The remaining items in [Open follow-ups](#open-follow-ups-not-blockers) are quality-of-life, not functional gaps.

## Open follow-ups (not blockers)

The three polish items below shipped on 2026-05-20 as a single sweep and were live-verified against the dev cluster on the same day; only the v0.5-deferred preview endpoint remains.

- ~~**Project-scoped list view.**~~ ✅ **Shipped + live-verified 2026-05-20.** `/automations?project=<id>` now keeps the list scoped to that project after save/run-now/delete; a header chip ("Scoped to *X* · *N* automation(s) · Show all") surfaces the active filter and the "New automation" button defaults to creating inside the scoped project. `clearProjectFilter()` drops the filter and `router.navigate(['/automations'], {queryParams: {}, replaceUrl: true})` keeps the URL coherent. The chip falls back to a localized "this project" placeholder when `api.getProject()` returns null (access denied). Implementation: `cockpit/src/app/views/automations/automations-page.component.ts` (new `projectFilter` + `projectFilterName` signals, `refreshList`/`clearProjectFilter`/`openNewEditor` helpers) + `.html` + `.scss` (new `.filter-chip` recipe) + 3 EN/DE i18n keys (`automations.list.projectFilter.{label,fallbackName,clear}`). **Live verification:** opened `Fessi` project → "Manage automations" → chip read "Scoped to Fessi · 0 automation(s)"; created two rows (one via the cross-link editor, one via the New-button while scoped); after-save chip count incremented 0 → 1 → 2 (proves `loadByProject` not `loadMine`); API confirmed both rows carry `project_id=4fc6ca10-…` (proves `openNewEditor`'s pre-fill); "Show all" cleared the chip and URL dropped the `?project=` param.
- ~~**Notification on auto-disable.**~~ ✅ **Shipped + live-verified 2026-05-20.** The dispatcher now captures the auto-disable reason inside the same Postgres transaction and emits an SSE event (`event_type='automation_auto_disabled'`) + email (`send_system_notification`) to the owner *after* the txn commits — so we never notify about a disable that gets rolled back. Notifications skip silently when `notification_service.is_available` is false, and transport failures are logged but never re-raise (the disable still stands). Quiet hours are *not* honored for safety events — the owner needs to know. Implementation: `orchestrator/services/notification_service.py` (new `notify_automation_auto_disabled` method), `orchestrator/services/email.py` (new `send_system_notification` for non-job system mail), `orchestrator/services/cron_dispatcher.py` (restructured `_process_one_due_automation` to capture `pending_disable` inside the txn and call `_emit_auto_disable_notification` post-commit). +6 backend tests covering both disable paths, the no-fire-no-notify happy path, the catchup-skip path, transport-failure isolation, and the not-yet-connected case. **Live verification:** subscribed `/api/notifications/events` from the browser, created a trap automation (`* * * * *`, `max_fires_per_day=1`); first tick fired the job at 12:36:26 UTC (advance to 12:37:00); second tick at 12:37:26 UTC tripped the cap, set `enabled=false` + `next_run_at=null`, and broadcast `{type: "automation_auto_disabled", automation_id, automation_name: "Polish verify - disable trip", reason: "max_fires_per_day=1 reached", cockpit_url}`. SSE event timestamp matched the row's `updated_at` within ms, confirming the notification fires post-commit as designed.
- ~~**Cockpit unit test coverage for the new components.**~~ ✅ **Shipped 2026-05-20.** +31 vitest specs across three files: `cron-preview.component.spec.ts` (6 tests, including a DST round-trip), `escape-hatch-panel.component.spec.ts` (3 tests for the FastAPI Swagger URL derivation), and `automations-page.component.spec.ts` (22 tests covering the `derivePresetCron` / `inferPresetFromCron` round-trip, the project-filter wiring, `clearProjectFilter`, `openNewEditor`, and `projectFilterDisplayName` fallback). Small refactor in `cron-preview.component.ts`: extracted a pure exported `computeCronSummary()` so the summary logic is testable without TestBed + template compilation. Total cockpit suite: 327 tests (was 291), all green.
- **`/api/automations/{id}/preview`** endpoint (deferred to v0.5 per the original spec). Today the cockpit computes the next-5-runs client-side via `cron-parser` — fine for cron, but an event-trigger preview needs server-side state.

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
- New: `orchestrator/services/automations.py` — `create_job_from_automation` helper. Calls `db.create_job()` then the shared `orchestrator/services/job_provisioning.py::provision_job_repo(...)` so cron/run-now jobs get a Gitea repo + access grant like manual jobs. (Provisioning extraction landed 2026-06-13; originally this path called only `db.create_job()` and the spawned jobs had no repo — see `automations.md` correction + `docs/issues/`.)
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
