# Security event log (cross-user 403 audit)

**Status: ✅ shipped + k3d-verified 2026-06-11.** Closes M1.B #4 in
`docs/multi_tenancy.md` (the last open Tier 0 item in
`docs/saas_roadmap.md`). Migration `0025_security_events.sql`; helpers in
`security/access.py` (`log_security_event`, `_denied`); read endpoint
`GET /api/admin/security-events`; retention sweeper in `main.py`.
16 unit tests in `tests/test_security_events.py`. Live verification:
shadowed-admin IDE probe → 403 + row (`view_as=true`,
`resource_type=ide_entity`, full path + ip) + WARNING line in pod logs;
admin endpoint returned the row (200, 4 ms).

Known nuance: `auth_method` is NULL for session-cookie / OIDC-bearer
callers — the auth resolver only stamps `auth_method` on the PAT
(`pat`) and MCP (`mcp`) paths. Read NULL as "interactive session". Not
worth plumbing a label through the cookie path for this.

## Why

Every access gate in `orchestrator/security/access.py` denies silently:
1000 UUID-probe attempts against another user's jobs today produce 1000
identical 403 responses and **zero detection signal**. Now that app-side
admission is live and strangers can hold real accounts, "who is poking
at resources they don't own" becomes an operational question, not a
hypothetical. The same applies to non-admins probing `/api/admin/*`.

The multi-tenancy doc scoped this as: *"Emit a structured log line (or a
Mongo `security_audit` entry) when any gate raises 403. Centralize in
the access helpers so it's one log per failed gate."* (~3h.)

## Decisions

1. **Sink = Postgres table (`security_events`) + a structured
   `logger.warning` line.** Not MongoDB: the audit-store direction is
   Mongo→Postgres (`docs/features/postgres_audit_store_implementation.md`),
   so a new Mongo writer would be born legacy. Not log-line-only: pod
   logs rotate away; a table is queryable for forensics (MCP
   `query_table` works on day one) and can power a cockpit admin page
   later. The log line still fires first so even a DB outage leaves a
   trace in `kubectl logs`.

2. **Centralized in `access.py`.** One best-effort writer
   (`log_security_event`) + one `_denied(...)` wrapper that logs and
   *returns* the `HTTPException`, so each raise site becomes
   `raise await _denied(...)` — impossible to log without raising or
   raise without logging.

3. **Instrumented deny sites:**
   - All 403 raise sites in `access.py` (project member/owner, job,
     thread, builder session, sudo authority, datasource access/owner,
     MCP scope denials) — 15 sites.
   - `_require_admin` in `main.py` — non-admin probing admin endpoints
     is the strongest single signal.
   - IDE proxy HTTP + WS deny sites in `main.py` (already had plain
     `logger.warning`; upgraded — a guessed IDE URL for another user's
     entity is a probe).

4. **Deliberately NOT instrumented:**
   - `require_approved_user`'s pending-approval 403 — fires on every
     request from a not-yet-approved account; the admission pending
     list already surfaces those users. Pure noise.
   - The bool filter helpers (`user_can_access_job`,
     `user_can_access_datasource`, …) on streaming/list paths — they
     run per SSE event / per list row; logging there would flood the
     table with non-signals (filtering ≠ probing).
   - 404s — "resource doesn't exist" is mostly typos and stale UI
     state. The 403s are the high-signal case: the resource *exists*
     and belongs to someone else, i.e. the caller has a real foreign
     UUID.

5. **View-as enrichment folded in (view-as PR 4).** The user dict
   already carries `real_is_admin` (set by `require_approved_user`);
   each event records `real_is_admin` + `view_as` (true when an admin
   is shadowed), so an admin exercising the toggle is distinguishable
   from a genuine cross-user attempt. This closes the audit-enrichment
   slice of view-as PR 4 for the security log; the per-request
   *general* audit enrichment remains open.

6. **Abuse posture.** Writes happen only on the 403 path, which is
   behind authentication (anonymous callers 401 before reaching any
   gate) — so a write-flood requires a valid account, which is exactly
   the actor we want rows about. Retention sweeper
   (`SECURITY_EVENTS_RETENTION_DAYS`, default 90) bounds growth, same
   pattern as `thread_events_prune_sweeper`.

7. **Failure mode: never block the deny.** The DB write is wrapped;
   on failure it logs `logger.error` (loud, per the
   surface-silent-aux-failures principle) and the 403 proceeds
   unchanged. A broken audit trail must not turn a 403 into a 500.

## Schema (migration `0025_security_events.sql`)

| column | type | notes |
|---|---|---|
| `id` | UUID PK default `gen_random_uuid()` | |
| `created_at` | TIMESTAMPTZ default NOW() | index DESC |
| `event_type` | TEXT | `access_denied` \| `admin_denied` (future: `login_failed`, …) |
| `user_id` | UUID nullable | no FK — keep rows after user deletion (forensics) |
| `auth_method` | TEXT | `cookie` / `oidc` / `pat` / `mcp` |
| `real_is_admin` | BOOLEAN | un-shadowed privilege flag |
| `view_as` | BOOLEAN | true ⇢ admin had `X-Admin-View-As: user` on |
| `resource_type` | TEXT | `job`, `project`, `thread`, `builder_session`, `datasource`, `sudo_request`, `ide_entity`, `admin_endpoint` |
| `resource_id` | TEXT nullable | string, not UUID — some ids are slugs/paths |
| `method` | TEXT | HTTP verb, or `WS` |
| `path` | TEXT | `request.url.path` |
| `detail` | TEXT | the 403 detail string (which gate fired) |
| `client_ip` | TEXT | first `X-Forwarded-For` hop, else `request.client.host` |

Indexes: `(created_at DESC)`, `(user_id, created_at DESC)`.

No `org_id` yet — M2 multi-org is deferred; `user_id` is sufficient to
backfill an org mapping retroactively (noted as M2 consideration #2 in
`docs/multi_tenancy.md`).

## Read path

`GET /api/admin/security-events?limit=&user_id=&event_type=&since=`
(admin-gated, in the admin users family). Cockpit admin page is a
follow-up, not part of this slice — `query_table` + this endpoint cover
forensics until then. Rate-based *alerting* (e.g. SSE toast on N events
from one user in M minutes) is explicitly out of scope for v1; the
table is the primitive it would read from.

## Verification

Unit: `tests/test_security_events.py` (event written on cross-user 403;
nothing written on 404 or success; DB-write failure doesn't mask the
403; view-as enrichment recorded; admin endpoint gated + filtered).
Live on k3d: migration applies at boot; user A curls user B's job →
403 + row in `security_events` + log line; admin endpoint returns it.
