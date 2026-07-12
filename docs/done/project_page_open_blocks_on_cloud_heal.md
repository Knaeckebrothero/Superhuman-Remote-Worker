# Project page takes seconds to open — `GET /api/projects/{id}` blocks on cloud/Keycloak reconciliation

## Symptom (observed 2026-07-09)

Clicking a project in the cockpit shows the loading spinner for ~5 s
(dev cluster) before the page renders. Everything else about the page
is fast once it appears.

Measured on local k3d from inside the orchestrator pod (curl with a
minted `admin-cli` id_token, see "How to re-verify" below):

| Endpoint | Time |
|---|---|
| `GET /api/projects/{id}` | **2.3–2.4 s**, consistent across runs, both default and non-default projects |
| `/jobs`, `/members`, `/datasources`, `/repositories`, `/experts`, `/knowledge/summary` | 3–40 ms each |

The cockpit fires all of these in parallel from `loadAll()`
(`project-detail.component.ts:1709`), but the page-level spinner gates
on the one slow call: `@if (isLoading() && !project())` at
`project-detail.component.ts:63` only clears when `getProject`
returns. So the perceived open time *is* the `GET /api/projects/{id}`
latency. On the dev cluster (real OpenCloud, more members, network
hops) the same architecture stretches to ~5 s.

**Not an index problem.** `jobs` has `idx_jobs_project_id`,
`job_summary` is a trivial view over `jobs`, the audit-count
enrichment is one batched query against a partial index
(`agent_audit_job_id_idx`), and the KB summary is three small
aggregates. All DB work across the page is milliseconds.

## Root cause

`GET /api/projects/{project_id}` (`orchestrator/main.py:25001`) does
synchronous external HTTP on every call. The cloud telemetry log makes
it explicit — a single request:

```
"message": "cloud op ok", "backend": "nextcloud", "op": "resolve_user_identity", "latency_ms": 2326.26
"message": "GET /api/projects/28f94424-… 200 (2331ms)"
```

`resolve_user_identity` (`services/cloud/nextcloud.py:329`,
`services/cloud/opencloud.py:360`) performs up to two *sequential*
user-search calls against the cloud backend (email, then display
name — ~1.2 s each on Nextcloud's OCS API), and its result is a
stable fact: the user's cloud account ID. Nothing persists it, so it
is re-resolved from scratch on every project page open.

Two code paths hit it per GET:

1. **Non-default projects**: `_ensure_project_cloud_resources`
   (`main.py:24773`) runs its member-sync step **unconditionally on
   every GET** — Keycloak `ensure_project_group`, then per member
   `_sync_project_member_to_groups` (`main.py:24741`): Keycloak
   group-add + `resolve_user_identity` + `add_user_to_group` against
   the cloud backend. The docstring calls this "cheap enough to
   include in every GET"; the telemetry says ~2.3 s minimum, scaling
   with member count.
2. **Default projects**: the heal is skipped, but the
   `cloud_storage_url` branch (`main.py:25020-25039`) resolves the
   owner's home Space via `resolve_user_identity` + `get_user_home` —
   the same ~2.3 s.

Crucially, the per-GET member sync is **not doing primary work**.
`add_project_member` (`main.py:25186`, sync call at `:25206`) already
writes both groups at mutation time. The on-GET sync exists purely as
drift repair for rare failure windows (project created while Keycloak
admin auth was broken, Space adopted out-of-band). The current design
pays N external HTTP calls on every page view to repair a condition
that almost never exists.

## Impact

- Every project open costs 2–5 s of spinner; the most-visited page in
  the cockpit feels broken-slow.
- Keycloak and the cloud backend receive group-ensure/user-search/
  group-add traffic proportional to **page views**, not membership
  changes.
- A read endpoint performs writes (group creation/membership) as a
  side effect — surprising for callers, and concurrent opens duplicate
  the work.
- When the cloud backend is degraded, project pages hang or slow
  further even though nothing about the project data needs the
  backend.

## Solution (decided 2026-07-09)

Three parts. Part 2 is the smallest change with the biggest effect and
can ship first.

### 1. Reads stop reconciling — heal becomes throttled + background

Remove the blocking `await _ensure_project_cloud_resources(project)`
from `get_project`. Keep the *trigger* (on-open repairs exactly the
projects people actually use, at the moment they use them) but run it
as fire-and-forget with a per-project cooldown:

```python
# module level
_project_heal_last: dict[str, float] = {}   # project_id -> monotonic ts
_HEAL_COOLDOWN_S = 3600

# in get_project, replacing the await
now = time.monotonic()
if now - _project_heal_last.get(project_id, 0) > _HEAL_COOLDOWN_S:
    _project_heal_last[project_id] = now
    asyncio.create_task(_ensure_project_cloud_resources(project))
```

- The existing `_project_heal_locks` already dedups concurrent folder
  creation; the cooldown dict dedups the member-sync spam.
- The folder-creation branch moves to background with it. Consequence:
  the *rare* legacy project without a folder handle returns
  `cloud_storage_url = None` on its first open and gets the deep-link
  on the next load — the cockpit already renders `None` by hiding the
  button, so this degrades gracefully.
- `create_project` (`main.py:24946`) keeps its **blocking** call —
  creation time is the primary provisioning path and must stay
  synchronous.

### 2. Persist cloud identity per user

Add a per-backend identity cache on the `users` row (new migration
`orchestrator/database/migrations/app/NNNN_users_cloud_identity.sql`;
regenerate `schema_current.sql` via `scripts/schema-snapshot.sh`):

```sql
ALTER TABLE users ADD COLUMN cloud_identity jsonb NOT NULL DEFAULT '{}'::jsonb;
-- shape: {"<backend_id>": {"user_id": "…", "home_browser_url": "…", "resolved_at": "…"}}
```

A helper (e.g. `resolve_user_identity_cached(postgres_db, user_row,
backend)`) checks `cloud_identity[backend.backend_id]` first, falls
back to `backend.resolve_user_identity(...)`, and persists the result.
Rules:

- **Positive results persist indefinitely** — a cloud account ID is
  stable.
- **Negative results are not persisted** (user may simply not have
  logged into the cloud yet — the docstring at `nextcloud.py:340`
  calls this a valid state). At most an in-memory short-TTL negative
  cache to protect a single burst.
- Keyed by `backend_id`, so prod-private (Nextcloud) and dev
  (OpenCloud) coexist and a backend switch is a cache miss, not a
  wrong answer.

Switch the callers: `_sync_project_member_to_groups`, the
default-project home branch in `get_project`, and any other
`resolve_user_identity` call site that starts from a `users` row
(grep at implementation time). This fixes every consumer, not just
this page.

### 3. Default-project `cloud_storage_url` becomes DB-only

Extend the same JSONB entry with `home_browser_url` (populated on
first successful `get_user_home`). The default-project branch in
`get_project` then reads owner → `cloud_identity` → URL with zero
external calls; on cache miss it background-resolves (part 1's task)
and returns `None` once. Non-default projects already build the URL
from the stored folder handle — cheap, unchanged.

End state: `GET /api/projects/{id}` is pure Postgres on the warm
path; external systems see sync traffic proportional to membership
changes plus a trickle of throttled background repairs; the
self-healing property survives.

## Acceptance criteria

- `GET /api/projects/{id}` makes **zero external HTTP calls** on the
  warm path (no `httpx` / `cloud op` telemetry lines for the request
  id), for both default and non-default projects.
- Access-log latency (`GET /api/projects/… 200 (NNNNms)`) drops from
  ~2300 ms to < 100 ms on k3d.
- `add_project_member` still lands the member in both groups
  (mutation path untouched).
- Drift repair still works: a project whose members are missing from
  the groups heals in background after one open (verify with the k3d
  smoke: new member sees the Space in OpenCloud without re-login).
- `pytest tests/ -k "project"` green; new tests for the cached
  resolver (hit, miss-then-persist, negative-not-persisted).

## How to re-verify the measurement

From inside the orchestrator pod (BFF cookie auth means no browser
token to steal; mint one via the public `admin-cli` client — use the
**id_token**, not access_token):

```sh
TOKEN=$(curl -s -X POST http://srw-keycloak:8080/realms/srw/protocol/openid-connect/token \
  -d grant_type=password -d client_id=admin-cli -d username=test -d password=test -d scope=openid \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id_token'])")
curl -s -o /dev/null -w "%{time_total}\n" -H "Authorization: Bearer $TOKEN" \
  http://localhost:8085/api/projects/<id>
```

Then correlate with `services.cloud.telemetry` `latency_ms` lines in
the orchestrator log for the same `request_id`.

## Related code

- `orchestrator/main.py:25001` — `get_project` handler (blocking heal
  at `:25008`, default-project URL branch at `:25020-25039`)
- `orchestrator/main.py:24773` — `_ensure_project_cloud_resources`
  (unconditional member sync at `:24859-24884`)
- `orchestrator/main.py:24741` — `_sync_project_member_to_groups`
- `orchestrator/main.py:25186` — `add_project_member` (mutation-time
  sync already exists at `:25206`)
- `orchestrator/services/cloud/nextcloud.py:329` /
  `opencloud.py:360` — `resolve_user_identity` (the 2×~1.2 s
  sequential searches)
- `orchestrator/services/keycloak_admin.py:97` —
  `ensure_project_group` (group search per call)
- `cockpit/src/app/views/project-detail/project-detail.component.ts:63`
  — spinner gate on `getProject`

## Status

Diagnosed and measured 2026-07-09 (k3d, telemetry-confirmed).
**Implemented + live-verified same day**; shipped in `5f8b6047`
(deployed to dev 2026-07-10). As built:

- Migration `0051_users_cloud_identity.sql` + `schema_current.sql` regen.
- `Database.get_user_cloud_identity` / `merge_user_cloud_identity`
  (atomic nested `jsonb_set(... || ...)` merge).
- `orchestrator/services/cloud/identity.py` — `resolve_user_identity_cached`,
  `peek_home_browser_url` (pure DB), `get_home_browser_url_cached`. The
  helpers read the cache with a dedicated single-column query instead of
  widening user SELECTs (deviation from the sketch above; decouples them
  from every call-site's row shape).
- `main._fire_background_repair(key, coro)` — fire-and-forget with 1h
  per-key cooldown + strong task refs; `get_project` uses it for the heal
  (`project-heal:{id}`) and the default-project home-URL resolve
  (`home-url:{user}:{backend}`). `create_project` keeps its blocking call.
- Cached resolver also switched in: `_sync_project_member_to_groups`, the
  citation live re-fetch, `_build_default_project_mount_row`, and the
  session-share late-provision path.
- Tests: `tests/test_cloud_identity_cache.py` (cache semantics, throttle,
  non-blocking get_project); `tests/test_thread_mount_rows.py` fakes widened.
  Full suite 8438 passed (1 pre-existing env-dependent failure:
  `test_connect_disconnect` needs a host-local Postgres).

**Measured after (k3d, same benchmark):** 60 ms cold / 2–5 ms warm, vs
2300–2400 ms before (~500×). Log-verified: background resolutions completed
3–4 s *after* their responses were served (correlated by request_id), exactly
once per key with repeats throttled; positive resolution persisted
`{"nextcloud": {"user_id": "admin", "home_browser_url": …, "resolved_at": …}}`
to `users.cloud_identity`; negative resolutions (owner absent from the cloud
backend) correctly left the cache empty and retried on the next cooldown
window.
