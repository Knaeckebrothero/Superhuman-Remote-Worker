---
tags:
  - feature
  - auth
  - admin
  - cockpit
  - orchestrator
  - ux
aliases:
  - view as user
  - admin self-narrow
  - shadow non-admin
related:
  - "[[multi_tenancy]]"
  - "[[auth_bff_and_api_tokens]]"
---

# Admin "View as User" toggle

> Admins currently see every user's jobs, projects, and datasources by default — including their own dogfooding output mixed in with other accounts' work. Add an opt-in **"View as: Me / Everyone"** toggle so admins default to a regular-user view (own resources + project memberships) and can flip to fleet-wide visibility when they actually need it.

**Status:** ✅ Shipped (PRs 1–3) and live-verified 2026-05-18. PR 4 (audit-log enrichment + remaining manual matrix walk-through) is the only slice still open.
**Triggered by:** Post-P4b multi-tenancy review — `docs/multi_tenancy.md` "Where we are now (2026-05-17)" notes that admins are god-mode by default and the only constraint mechanism is a project-scoped MCP token. UX gap: admin doing their own work drowns in fleet rows.
**Scope:** Cockpit list views (jobs / projects / datasources). Does **not** touch admin observability tools (agents, sudo queue, audit, LLM-request log, stats dashboards), which stay fleet-default.

## Verification (2026-05-18, cluster `develop`)

Live end-to-end on the deployed cluster after PRs 1–3 landed:

| Check | Result |
|---|---|
| Sidebar toggle visible to admin | ✅ `[Admin · Viewing all]` button replaces the static `[ADMIN]` chip |
| Toggle cycles `all` → `me` → `all` | ✅ Label / tooltip / `.is-narrowed` class all flip in sync |
| Per-user `localStorage` persistence | ✅ Key `srw.viewMode.<userId>` |
| Pill renders on `/jobs` in `me` mode | ✅ `● Viewing your data · Show all` next to title |
| Pill auto-hides in `all` mode | ✅ |
| "Show all" pill link → fleet view | ✅ |
| **Backend actually narrows the response** | ✅ `GET /api/jobs?limit=500` returns **12** jobs with `X-Admin-View-As: user`, **42** without — sample IDs are disjoint |
| Admin-only endpoints stay reachable while shadowed | ✅ `_require_admin` reads `real_is_admin`; `list_agents` returns the 66 agents in both modes |

## TL;DR

| Layer | Change |
|---|---|
| Backend | New `X-Admin-View-As: user` request header, interpreted by a single dependency wrapper. When present on an admin request, the user dict is returned with `is_admin = False` (and `real_is_admin = True` preserved for admin-only routes). Existing list-endpoint logic is unchanged — non-admin code paths get exercised. Header works for any auth method (cookie session, PATs, MCP tokens). |
| Frontend | New `ViewModeService` with a `viewMode` signal (`'me' \| 'all'`), persisted to `localStorage`, **default `'all'`** (silent rollout — no behavior change for existing admins). HTTP interceptor injects the header when `viewMode() === 'me'` AND the user is admin. |
| UI | Single toggle in the sidebar footer, right next to the existing admin badge (only visible to admins). A small badge on filtered list pages so it's obvious why the page is "small". |
| Endpoints out of scope | `/api/persistent/threads` (already user-only), `/api/agents/*`, `/api/sudo/requests`, `/api/llm-requests`, `/api/users`, `/api/stats/*`, `/api/audit/*` (admin-only by design). |

**Estimated effort:** ~3 days. PR 1 backend dep (~½d). PR 2 frontend service + interceptor + tests (~1d). PR 3 toggle UI + per-page badges + i18n (~1d). PR 4 validation pass + audit-log enrichment (~½d).

## Why not per-component filter buttons

The user's initial instinct was a per-component filter button. Rejected in favor of a single global toggle because:

1. **One state, one source of truth.** Three or four buttons all wired to four signals = four chances for them to drift out of sync. One signal flipped once, everything updates.
2. **Mirrors the regular-user experience faithfully.** "View as: Me" should look *exactly* like what a non-admin sees across the whole UI, not like a regular user with some pages still in fleet mode.
3. **Discoverability.** A global toggle in the sidebar footer is visible everywhere; per-page buttons are easy to miss on pages users land on infrequently.
4. **Easier rollback / kill switch.** Default-off the feature flag → toggle disappears, behavior reverts. Per-page version needs four removals.

Per-component buttons stay possible later as a refinement (e.g., "show only my jobs" within an already-filtered list).

## Backend design

### The "shadow non-admin" pattern

A new request header `X-Admin-View-As: user` is interpreted by a thin wrapper around `require_approved_user`:

```python
# orchestrator/security/access.py

VIEW_AS_HEADER = "X-Admin-View-As"

async def require_approved_user(request: Request, db) -> dict[str, Any]:
    """[existing implementation unchanged]"""
    user = await _load_session_user(request, db)
    if not user or not user.get("is_approved"):
        raise HTTPException(status_code=403, detail="User not approved")

    # Shadow-as-user: admin opts into being treated as a regular user
    # for this request. Preserves real_is_admin so admin-only endpoints
    # can still authorize via require_admin().
    view_as = request.headers.get(VIEW_AS_HEADER, "").lower()
    if view_as == "user" and user.get("is_admin"):
        user = {**user, "is_admin": False, "real_is_admin": True}
    else:
        user = {**user, "real_is_admin": bool(user.get("is_admin"))}

    return user


async def require_admin(request: Request, db) -> dict[str, Any]:
    """Admin-only gate. Tolerates the view-as shadow."""
    user = await require_approved_user(request, db)
    if not user.get("real_is_admin"):
        raise HTTPException(status_code=403, detail="Admin required")
    return user
```

**Why a header, not a query param:**
- Single HTTP interceptor sets it; zero changes at endpoint call sites.
- Doesn't bloat the OpenAPI schema with `?view_as_user=true` on dozens of GETs.
- Easy to filter in audit log: one column, present or absent.
- The header convention `X-Admin-View-As` is self-documenting in network traces.

**Why `real_is_admin` instead of stripping the flag entirely:**
- Admin-only endpoints (`/api/agents`, `/api/sudo/requests`, …) gate via `require_admin`. They must still work for an admin in "View as: Me" mode (e.g., admin checks the agent list, then flips back to view-as-me to look at their own jobs — they shouldn't be 403'd on the agents page). `real_is_admin` is the authoritative privilege flag for those gates; `is_admin` is the "what scope of data do I see right now" flag.
- The handful of audit-emitting endpoints can log both: `actor=user_id`, `view_as_user=bool`, `real_admin=bool`.

### Which endpoints become "view-as" aware

**Automatic via `require_approved_user`** (no per-endpoint change):
- `GET /api/jobs` — already runs non-admins through `get_visible_jobs(owner_user_id, visible_project_ids, ...)`. Shadow-as-user → that branch fires.
- `GET /api/projects` — already runs non-admins through `get_projects_for_user(caller_id)`.
- `GET /api/datasources` — already filters per-row via `user_can_access_datasource(user, ...)`.

That's the whole list-endpoint surface. The per-resource gates (`require_job_access`, `require_project_access`, `require_datasource_access`) all check `user.is_admin` first and short-circuit; under shadow-as-user they fall through to membership/ownership checks. Net result: a deep link to a job the admin doesn't own returns 403 when the admin is in view-as-user mode, which is exactly what we want for honest UX.

**Endpoints that ignore the shadow (use `require_admin`):**
- `/api/agents/*` — fleet infrastructure
- `/api/sudo/requests` — admin queue
- `/api/llm-requests` — cost/usage observability
- `/api/users` — user management
- `/api/stats/*`, `/api/audit/*` — observability
- All `/api/internal/*` and agent-callable endpoints (gated by `require_internal`, orthogonal)

Verification: a grep for `require_admin` calls before this lands, plus the existing `scripts/check_endpoint_auth.py` inventory, gives us the exhaustive list. Anything currently using `require_approved_user` + inline `is_admin` check should be flipped to `require_admin` if it's truly admin-only, OR left alone if "view as user means you see less" is the desired behavior.

### One backend wrinkle to validate

`/api/persistent/threads` already filters by `user_id=str(user["id"])` *unconditionally* (no admin override on the list). That means admins cannot currently see other users' threads in the list view. This is technically already "view-as-user behavior, always on". We have three options:

1. **Leave it.** Threads are private-by-design; admins shouldn't need fleet visibility.
2. **Add admin override and respect the toggle.** Symmetric with the other three list endpoints.
3. **Add admin override only in "View as: All" mode, hidden in "View as: Me".**

**Recommended: option 1 — leave it.** Persistent chat is inherently private (it's the user's interactive session, not a project artifact). If admins ever need cross-user thread visibility it should be a separate audit/debug page, not a fleet toggle on the user list.

### Audit logging enrichment

Every authenticated request already logs `actor_user_id`. Add two columns to whatever audit table receives these (probably MongoDB's `request_log` if it exists, or a new column on whatever Postgres table the BFF writes):

- `view_as_user: bool` — whether the header was present
- `real_is_admin: bool` — whether the actor is *actually* admin

So when investigating "admin Alice took action X", the audit clearly shows whether she was in fleet mode or shadow mode at the time.

## Frontend design

### `ViewModeService`

```ts
// cockpit/src/app/core/services/view-mode.service.ts

@Injectable({providedIn: 'root'})
export class ViewModeService {
  private readonly userService = inject(UserService);
  private readonly storageKey = 'srw.viewMode';

  readonly viewMode = signal<'me' | 'all'>(this.loadInitial());

  // Only flips have effect when the user is admin.
  readonly effectiveMode = computed(() => {
    return this.userService.currentUser()?.is_admin ? this.viewMode() : 'me';
  });

  setMode(mode: 'me' | 'all'): void {
    this.viewMode.set(mode);
    try {
      localStorage.setItem(this.storageKey, mode);
    } catch { /* private-browsing fallback */ }
  }

  private loadInitial(): 'me' | 'all' {
    try {
      const v = localStorage.getItem(this.storageKey);
      return v === 'me' ? 'me' : 'all'; // default = 'all' (no behavior change for existing admins)
    } catch {
      return 'all';
    }
  }
}
```

**Default = `'all'`** (locked 2026-05-18). Pairs with silent rollout: existing admins see no behavior change, they discover the toggle when they click their admin badge in the sidebar. Promotion to default-`'me'` is a one-line follow-up after the feature has soaked.

**Per-user key isolation.** If multiple admins use the same browser profile (unlikely but possible), the key should include the user id: `srw.viewMode.<userId>`. Adds two lines.

**Cross-tab sync.** Optional: subscribe to `window.addEventListener('storage', ...)` and update the signal when other tabs change it. v1 can skip; the inconsistency is small ("tab A says Me, tab B still says All until refresh").

### HTTP interceptor

A new interceptor (or an extension to `auth.interceptor.ts`) injects the header when applicable:

```ts
export const viewAsInterceptor: HttpInterceptorFn = (req, next) => {
  const viewMode = inject(ViewModeService);
  const userService = inject(UserService);

  if (!isOrchestratorRequest(req.url)) return next(req);
  if (!userService.currentUser()?.is_admin) return next(req);
  if (viewMode.viewMode() !== 'me') return next(req);

  return next(req.clone({
    headers: req.headers.set('X-Admin-View-As', 'user'),
  }));
};
```

Registered in `app.config.ts` *after* `authInterceptor` (CSRF + cookies first, then view-mode header).

### Toggle UI

**Location:** sidebar footer, right next to the existing admin badge (`sidebar.component.ts:178-180`). Click on the badge → cycles `'me' ↔ 'all'`. The badge label shifts based on state:

```
[Avatar] Alice Admin    [ADMIN · Viewing My Data]    🔔 ⚙️ ↪
[Avatar] Alice Admin    [ADMIN · Viewing All]        🔔 ⚙️ ↪
```

Hover tooltip: "Click to switch to fleet-wide view" / "Click to view only your data".

**Alternative considered:** a separate dropdown picker. Rejected as overkill for a binary toggle that only admins see.

**Mobile / simple layout.** The `simple/` shell uses tab-based navigation, not a sidebar. The toggle goes in its settings/profile sheet — admins on mobile flip it once and forget.

### Per-page badge

On the three list pages (`jobs`, `projects`, `datasources`), a small pill near the page title when in `'me'` mode:

```
Jobs    [Viewing your data · Show all]
```

Click "Show all" → calls `viewMode.setMode('all')`. This solves "admin lands on Jobs page wondering why it's empty / so small" without forcing them to scan the sidebar footer for the toggle.

The pill is absent in `'all'` mode (no need to draw attention).

## Per-endpoint applicability matrix

| Endpoint | Mode | Why |
|---|---|---|
| `GET /api/jobs` | Respects | User-content list. |
| `GET /api/projects` | Respects | User-content list. |
| `GET /api/datasources` | Respects | User-content list. |
| `GET /api/persistent/threads` | Already filtered by user (no admin override exists) | Inherently private. |
| `GET /api/jobs/{id}/*` | Per-resource gates handle it | Shadow suppresses admin override → 403 on others' jobs, which is correct UX. |
| `GET /api/agents/*` | Ignores (admin-only) | Fleet infrastructure. |
| `GET /api/sudo/requests` | Ignores (admin-only) | Admin queue. |
| `GET /api/llm-requests` | Ignores (admin-only) | Cost observability. |
| `GET /api/users` | Ignores (admin-only) | User management. |
| `GET /api/audit/*`, `GET /api/stats/*` | Ignores (admin-only) | Observability. |
| `/api/internal/*`, agent-callable | Ignores (`require_internal`) | Orthogonal — not user-callable. |
| Pending-action counts (sidebar badges) | Already user-scoped | `get_pending_action_counts(user_id=...)`. No change needed. |

## Edge cases

1. **Deep link to another user's job while in view-as-me.** Per-resource gate (`require_job_access`) returns 403. Cockpit shows the standard "Forbidden" page. **This is correct** — view-as-user means "treat me as a regular user across the board", and a regular user can't open that job either. The fix is "switch to View as All", not "automatically punch through the gate".

2. **MCP project-scoped token + view-as-user.** Token scope already narrows everything to the project. Shadow-as-user further narrows to "your stuff within that project". Both apply naturally. Verify with a test.

3. **Admin promotes a user mid-session.** The promoted user's `is_admin` is loaded at session start (from `/auth/me`). They'd need to refresh to see the toggle appear. Acceptable.

4. **Admin demotes themselves.** They'd lose the toggle on next refresh; `viewMode` value sits unused in localStorage. Harmless.

5. **Empty state changes.** "No jobs yet" UI on the jobs page needs to handle "no jobs *because you're filtered to view-as-me and haven't created any yet*". Tweak copy: include "(viewing your data — switch to View All if you're looking for fleet data)".

6. **Pagination / search.** Shadow header rides along on every list request automatically (it's at the interceptor level), so paginated and searched lists stay consistent.

7. **WebSocket / SSE.** `EventSource` cannot set custom headers. If we ever add a "view-as" notion to a streaming endpoint, it has to land as a query param on the URL. Not a v1 problem — none of the streaming endpoints (`/ws/persistent/*`, `/api/jobs/{id}/stream`) are list-style aggregations.

8. **Default change for existing admins.** **Decided 2026-05-18:** default = `'all'` paired with silent rollout. No behavior change on day 1; admins discover the toggle by clicking the badge. Promotion to default-`'me'` is a one-line follow-up after the feature has soaked. No Helm flag needed — the localStorage default just changes in a future PR.

9. **Aggregate admin widgets (Dashboard tiles).** **Decided 2026-05-18:** the shadow-header mechanism propagates uniformly — there is no per-widget logic. Widgets that call list endpoints under the hood narrow automatically when admin is in `'me'` mode. Widgets that call admin-only stats endpoints (`require_admin`-gated) show fleet data in both modes, because their underlying endpoint never had a non-admin code path. This is the "simplest" behavior: one mechanism, no special cases. Empty-ish dashboards in `'me'` mode are honest — that's what a user sees.

## Decisions locked (2026-05-18)

| # | Decision | Value | Notes |
|---|---|---|---|
| 1 | Server-side mechanism | **Request header `X-Admin-View-As: user`** with shadow-non-admin in `require_approved_user` | Single dependency-level switch; no per-endpoint changes. Preserves `real_is_admin` for `require_admin` gates. |
| 2 | Default mode for admins | **`'all'`** | No behavior change for existing admins on day 1; silent rollout. Promote to `'me'` later via one-line PR after soak. |
| 3 | UI placement (desktop) | **Sidebar footer, on the admin badge itself** | Click cycles `'me' ↔ 'all'`. Label shifts: `[ADMIN · Viewing My Data]` vs `[ADMIN · Viewing All]`. |
| 4 | UI placement (mobile/simple) | **Profile sheet in `simple/` shell** | Same toggle, mobile-shaped. |
| 5 | Per-page badge | **Yes — pill near page title in `'me'` mode only** | "Viewing your data · Show all". Absent in `'all'` mode. |
| 6 | Persistence | **`localStorage`, key `srw.viewMode.<userId>`** | Per-user keyed for shared-browser safety. |
| 7 | `/api/persistent/threads` admin override | **Leave as-is** | Already user-scoped unconditionally. Inherently private. |
| 8 | Cross-tab sync | **Out of scope for v1** | Acceptable minor inconsistency. |
| 9 | Audit log enrichment | **Add `view_as_user: bool` + `real_is_admin: bool`** | Investigability: distinguish fleet-mode admin actions from shadow-mode. |
| 10 | First-run nudge | **None — silent rollout** | Pairs with `default = 'all'`. No surprise behavior, no toast needed. |
| 11 | Widget scope (Q1) | **Uniform via shadow mechanism** | No per-widget logic. List-backed widgets narrow; admin-only stats widgets stay fleet. |
| 12 | PAT/automation exposure (Q2) | **Yes — header works for any auth** | Dependency-level interpretation; PATs and MCP tokens send the header too. Useful for n8n dogfooding. |
| 13 | "View as specific user" (Q3) | **Out of v1, architected for** | Document the future `user:<uuid>` header value. Dependency wrapper supports it: load target user dict, preserve `real_is_admin` from caller. ~½d follow-up when needed. Connects to user's "impersonation later" intent. |

## Phased rollout

### ✅ PR 1 — Backend dependency (shipped 2026-05-18, commit `ddadad3c`)
- `VIEW_AS_HEADER = "X-Admin-View-As"` added to `orchestrator/security/auth.py`; `require_approved_user` shadows admins to `is_admin=False, real_is_admin=True` when the header is `user`.
- `_require_admin` in `main.py` switched from `is_admin` to `real_is_admin`.
- Inline `is_admin` audit done: six visibility-style sites left as-is (list-jobs filter, create-project ownership forgery, VM gate, etc.) — under shadow they correctly narrow.
- `tests/conftest.py:_make_user` updated so the 3-user fixture mirrors the new contract (`real_is_admin` alongside `is_admin`).
- `tests/test_view_as_user.py`: 11 cases covering the design's coverage matrix (a–d) + edges (`user:<uuid>` reserved-no-op, mixed-case header, unapproved-bypass attempt, returned-dict-is-a-copy).
- Smoke-tested live via MCP (`list_agents` 66 rows, `list_jobs` 5 rows) after deploy.

### ✅ PR 2 — Frontend service + interceptor (shipped 2026-05-18, commit `13881289`)
- `cockpit/src/app/core/services/view-mode.service.ts`: signal + per-user `localStorage.getItem('srw.viewMode.<userId>')`, `effect()` rehydrates when active user changes, `effectiveMode` computed for UI badges.
- `cockpit/src/app/core/interceptors/view-as.interceptor.ts`: injects `X-Admin-View-As: user` for admin requests with `viewMode === 'me'`. Skips cross-origin, non-admin, and `'all'` mode requests. Registered AFTER `authInterceptor` in `app.config.ts`.
- 11 service specs + 7 interceptor specs; full cockpit suite 291/291.

### ✅ PR 3 — Toggle UI + per-page pill + i18n (shipped 2026-05-18, commit `d3c15ce6`)
- `cockpit/src/app/shell/view-mode-toggle/`: standalone admin-only button in sidebar footer. Replaces the static `[ADMIN]` span. Label shifts between `Admin · Viewing all` and `Admin · Viewing my data`, gets `.is-narrowed` warn-toned style in `me` mode.
- `cockpit/src/app/shell/view-mode-pill/`: per-page pill rendered next to `<h1>` on `/jobs`, `/projects`, `/datasources` only when admin AND `viewMode === 'me'`. "Show all" link flips back to `all`.
- i18n: `admin.viewMode.{role,scopeAll,scopeMe,tooltip.*,pill.*}` in `en.json` + `de-DE.json` (parity check clean).
- Skipped per locked decisions: first-run nudge (silent rollout), mobile/`simple/` shell parity (that shell doesn't exist yet).

### PR 4 — Validation + audit (~½ day, OPEN)
- Sidebar + Jobs already verified live (see §"Verification" above). Still TODO:
  - Click through Projects + Datasources list pages in both modes; confirm pill placement and that filtered lists narrow.
  - Walk the admin-only pages (`/admin/llm`, `/admin/users`, Settings tabs) in `me` mode; confirm they all stay 200 (regression check on the `real_is_admin` flip).
  - Send `X-Admin-View-As: user` with a PAT (curl) and verify list endpoints narrow — the cookie path is proven; PATs go through the same `require_approved_user`, but explicit confirmation is cheap.
- Add `view_as_user: bool` + `real_is_admin: bool` to the audit table the orchestrator writes per-request (MongoDB `agent_audit` or whatever the BFF/api-token request log is using). One-line insert payload change.
- No Helm default flip needed — default is `'all'` (locked).

## Future extensions (post-v1)

- **"View as <specific user>"** (Q3 above): the user's "perhaps we can even add impersonation here later" — supported by the dependency-wrapper architecture via `X-Admin-View-As: user:<uuid>`. ~½ day backend + a user-picker UI on the toggle. Useful for support workflows ("Alice reports she can't see project X").
- **Default promotion to `'me'`**: one-line localStorage default change once the toggle has soaked and the team is comfortable. Optionally gated behind a Helm flag if any tenant wants the old behavior.
- **Per-page filter sub-toggles**: if the global toggle proves too coarse, e.g., "show all projects but only my jobs". No demand signal for this yet.
- **Server-side user preference**: if cross-device sync becomes important, move the persistence from localStorage to a `users.preferences` JSONB column. Not needed for v1.

## Out of scope (v1)

- Per-page filter buttons (the rejected alternative — possible follow-on if the global toggle isn't granular enough).
- "View as <specific user>" for support workflows (see Future extensions).
- Server-side user preference for default view mode (localStorage is enough; see Future extensions).
- Cross-tab signal sync (acceptable minor inconsistency).
- Mobile-shell parity beyond the profile sheet entry (the simple layout has fewer pages this would apply to).
- Admin-only "audit fleet visibility" page that lists threads / other private resources cross-user. If we ever need it, separate doc.
