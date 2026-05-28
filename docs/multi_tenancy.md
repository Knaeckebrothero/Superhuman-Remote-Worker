# Multi-User Isolation, SaaS Readiness, and Multi-Tenancy

**Status:** M1.A (API data isolation) is shipped end-to-end as of 2026-05-18 — Track A, P4 follow-up, P4b/Track B, and the admin "view as user" toggle (PRs 1–3) are all live with **0 unscoped endpoints** (was 55). What's left for **M1 (SaaS readiness)** is operational hygiene (~1 day), cloud storage per-user OAuth (~1–2 weeks, the actual hosted-product blocker), abuse-prevention primitives (rate limiting / pod quotas / workspace egress), and user account lifecycle (self-deletion + data export). **M2 (Organizations / multi-tenancy)** is deferred until M1 is solid and there's real demand.

**German context:** "Mandantenfähigkeit" = **M2**. M1 ("multi-user isolation") is the prerequisite.
**Related:** [`docs/features/auth_bff_and_api_tokens.md`](features/auth_bff_and_api_tokens.md) (auth foundation), [`docs/features/admin_view_as_user.md`](features/admin_view_as_user.md) (admin UX, shipped 2026-05-18).

---

## TL;DR

Two milestones, in order:

- **M1 — SaaS readiness.** Goal: deploy on AWS, open signups to strangers, and *no user data leaks to another user* — even under abuse. Includes data isolation (API + cloud storage), abuse prevention (rate limiting / pod quotas / egress controls), and user account lifecycle (self-deletion + data export). API isolation is fully shipped; the rest is the work this doc tracks.
- **M2 — Organizations (multi-tenancy).** Goal: multiple organizations (teams of users) share a single deployment, fully isolated from each other — including from other tenants' admins. Deliberately deferred. Adding it stays cheap because M1.A centralized visibility in `orchestrator/security/access.py`.

This doc previously framed the work as "Phase 1 (per-user) → Phase 2 (multi-tenant)". The rename to M1/M2 reflects the destination ("SaaS-ready") rather than the substrate, and surfaces operational items (rate limiting, pod quotas, egress) that the old framing parked in "out of scope".

The historical Phase-1 audit and per-bundle implementation log are preserved under [Historical context](#historical-context) — every line of Track A still applies and is shipped. **No work is being re-scoped; only re-labelled.**

---

## Where we are now (2026-05-18)

**API auth surface: fully closed.** Every HTTP endpoint (232 total) and every WebSocket endpoint (2 total) has an explicit gate. The endpoint inventory at `docs/security/endpoint_inventory.txt` reports **0 unscoped** (down from 55 at the C2 baseline). The snapshot test (`tests/test_endpoint_inventory.py`) enforces this going forward — no new endpoint can ship without a gate.

**Coverage map:**

| Surface | Status | Where |
|---|---|---|
| REST `/api/*` (232 endpoints) | All gated | `docs/security/endpoint_inventory.txt` |
| WebSocket `/api/ide/{job}/proxy/*` | `resolve_ws_user` (BFF cookie) | `orchestrator/main.py:7742` |
| WebSocket `/ws/persistent/{thread}` | `resolve_ws_user` (BFF cookie) | `orchestrator/main.py:12710` |
| SSE `/api/sudo/events` | Per-user filter (F6) | F6 implementation |
| SSE `/api/notifications/events` | Per-user feed | pre-existing |
| MCP tools (97 of 99 previously ignored scope) | Scope plumbed via `user['scopes']` everywhere (F7) | `security/access.py` scope helpers |
| Agent ↔ orchestrator (Track B) | `X-Internal-Key` shared secret (Layer 2) + opt-in edge path strip (Layer 1) | P4b |
| Datasource credentials | Always redacted on REST (F3) | `redact_datasource[s]` helpers |
| Job/project/thread/source/citation/sudo/stats/admin reads | Per-user OR project-member OR admin visibility | P2 G1-G5 |
| Admin "view as user" toggle | `X-Admin-View-As: user` shadow header in `require_approved_user`; cockpit toggle + per-page pill | [Admin view-as design](features/admin_view_as_user.md) — PRs 1–3 shipped 2026-05-18 |

**Sharing model:** Project membership is the share unit. `project_members(project_id, user_id, role)` with `viewer/editor/owner` roles. Adding user B to project A (via `POST /api/projects/{id}/members`, owner-gated) gives them visibility into every job, file, datasource, knowledge note, and citation in that project. **No per-resource or public-link sharing today** — those would be product features, not auth gaps.

**What still leaks (M1 work remaining — cross-cutting infrastructure the API gates can't reach):**

| # | Gap | Severity | M1 slice | Lives in |
|---|---|---|---|---|
| 1 | **Cloud storage uses one shared service account** (OpenCloud/Nextcloud). A path-scoping bug → user A reads user B's files. | **High** (hosted-product blocker) | M1.C | `src/services/cloud_sync/`, OpenCloud admin token in `srw` secret |
| 2 | **Chromium profile is per-agent, not per-job** (`/tmp/agent-chromium-cdp-profile`). Cookies/sessions persist across jobs on the same agent pod. | Medium | M1.B #2 | `src/tools/research/browser.py:101` |
| 3 | **MongoDB has no retention/TTL** on `llm_requests` + `agent_audit`. Stale prompts/responses hoarded indefinitely. | Low | M1.B #3 | MongoDB indices |
| 4 | **Keycloak self-registration broken** — `VERIFY_EMAIL` fires with no SMTP, and `default-roles-srw` doesn't carry the `user` role. Strangers can't sign up; also blocks live E2E cross-user verification. | **High** (onboarding broken) | M1.B #1 | Keycloak realm config |
| 5 | **No audit log of cross-user 403 attempts.** 1000 probe attempts → 1000 silent 403s, no detection signal. | Medium | M1.B #4 | new — centralize in `access.py` |
| 6 | **No per-user API rate limiting.** UUID enumeration / endpoint hammering / LLM-cost runaway are unprevented. | Medium (high for SaaS) | M1.D #1 | new — FastAPI middleware |
| 7 | **No per-agent K8s `ResourceQuota` / `LimitRange`.** One user can OOM the cluster or saturate CPU. | Medium (high for SaaS) | M1.D #2 | new — Helm chart |
| 8 | **Unrestricted workspace egress.** Agent pods can crypto-mine / port-scan / spam from your AWS IPs. | Medium (high for SaaS) | M1.D #3 | new — NetworkPolicy |
| 9 | **No user self-deletion + data export.** GDPR-shaped, plus support workflows. | Low (table-stakes for SaaS) | M1.E | new — REST endpoints + cascade |
| 10 | **View-as PR 4** — audit-log enrichment (`view_as_user` + `real_is_admin` on per-request audit row); remaining matrix walk. | Low | M1.B #5 | [view-as PR 4](features/admin_view_as_user.md#pr-4--validation--audit-d-open) |

Items 1–5 are listed in [Historical context §Phase 2 sketch](#phase-2-sketch-historical) for traceability; this is the M1-shaped re-roll-up. See [Milestone 1](#milestone-1--saas-readiness) below for the prioritized plan.

---

## Why milestones, not phases

The original framing was "Phase 1 (per-user isolation) → Phase 2 (multi-tenant)". That's still accurate but the gravity changed:

- Solving Phase 1 was always a prerequisite for any hosted/subscription model — but the operational pieces (cloud storage isolation, abuse prevention, account lifecycle) needed parity work, and the old framing parked them as "out of scope" or "Phase 2".
- Reframing the destination as "SaaS readiness" forces us to think about **strangers** as users, not colleagues. That mindset turns up rate limiting, pod quotas, workspace egress, and user self-deletion — none of which mattered for "single org, all users vetted".
- Keeping the org wrapper as M2 (instead of folding it into "everything for hosted") prevents scope drift while we ship M1's deltas.

We are not re-scoping. Every line of Track A still applies and is shipped; the historical Phase 1 plan is preserved verbatim in [Historical context](#historical-context) so the implementation log stays intact.

---

## Milestone 1 — SaaS readiness

**Goal.** Deploy on AWS, open signups to individual subscribers, and accept that some of them will probe, abuse, or accidentally hammer the system. **No cross-user data leak. No "one user takes the cluster down". No GDPR-shaped landmines.**

**Explicitly out of M1:** payments, billing, ToS, AUP, cookie banners, privacy policy text, marketing, support workflows. M1 is just the data-isolation + abuse-prevention layer beneath any product on top.

**Readiness gate:** every item below either ✅ shipped or explicitly traced to a deferred ticket. Until then, do not open signups beyond invited beta users.

### M1.A — API data isolation (✅ shipped)

Every REST / WebSocket / SSE endpoint and every MCP tool gates on **per-user OR project-member OR admin**. Sharing primitive is `project_members(project_id, user_id, role)` with `viewer/editor/owner`. The endpoint inventory snapshot test (`tests/test_endpoint_inventory.py`) prevents regression.

- **Track A** (data isolation, ~10.9d): all H1–H5, F1–F7, G1–G5, C1–C2 bundles shipped 2026-05-16 with 293 unit tests + live verification. Details in [Historical context §Track A](#track-a--data-isolation-roadmap-historical).
- **P4 follow-up** (unscoped-endpoint backlog, ~3d): 6 bundles shipped 2026-05-17. Inventory: 55 → 0 unscoped.
- **Track B / P4b** (agent ↔ orchestrator boundary): shared-secret `X-Internal-Key` (Layer 2, always on, in chart) + optional edge-layer path block (Layer 1, opt-in per deployment). Shipped + live-verified 2026-05-17.
- **Admin view-as toggle** ([design](features/admin_view_as_user.md)): admins default to fleet-wide view but can flip to "view as me" to dogfood the regular-user UX (and to operate as a regular user when they're working on their own stuff). PRs 1–3 shipped + live-verified 2026-05-18 (12 jobs in `me` mode vs 42 in `all` mode on disjoint IDs).

This entire layer is the "**no path-scoping bug in API code can leak user A's data to user B**" guarantee. The remaining M1 work is everything the API gates *cannot* reach.

### M1.B — Cross-job infrastructure hygiene (quick wins, ~1 day total)

Five operational items, each independently shippable. Together they're a single short sprint.

| # | Item | Risk it closes | Effort | Status |
|---|---|---|---|---|
| 1 | **Keycloak self-registration fix** — wire SMTP for `VERIFY_EMAIL` (or skip-verify for dev); add the realm-level `user` role to `default-roles-srw` | Strangers literally can't sign up today; also blocks live E2E cross-user verification of the entire M1.A surface | ~2h | Open ([memory](../.claude/projects/-home-ghost-Repositories-Superhuman-Remote-Worker/memory/project_keycloak_self_registration_broken.md)) |
| 2 | **Chromium profile per-job** — `/tmp/agent-chromium-cdp-profile` → `…-{job_id}` + teardown hook (`src/tools/research/browser.py:101`) | Cookies / logged-in sessions from job A persist into job B on the same agent pod | ~2h | Open |
| 3 | **MongoDB TTL** on `llm_requests` + `agent_audit` (pick window: 90d? 1y?) | Prompts/responses hoarded indefinitely; privacy posture; GDPR retention story | ~1h | Open |
| 4 | **Cross-user 403 audit log** — emit a structured event when any `security/access.py` helper raises 403 | Today 1000 probe attempts → 1000 silent 403s; no detection signal | ~3h | Open |
| 5 | **View-as PR 4** — `view_as_user: bool` + `real_is_admin: bool` on the per-request audit row; remaining matrix walk-through across non-Jobs list pages | Investigators can't tell whether an admin action ran in fleet mode or shadow mode | ~½d | Open |

**Recommended landing order:** **#1 first** (smallest fix, biggest test-unblock — every subsequent M1 item benefits from being able to provision a real second user), then #2 / #3 / #4 / #5 in any order.

**Larger follow-on to #1 — cockpit-owned auth UI (~5–9 days).** Once the SMTP + default-role fix lands and proves the Keycloak-themed register page works, the natural next iteration is replacing Keycloak's themed pages with cockpit-native forms (password login + register, social providers, optional magic-link email-only sign-in). Design captured 2026-05-28 at [features/cockpit_owned_auth_ui.md](features/cockpit_owned_auth_ui.md). Three independently-shippable slices; not blocking the M1.B quick wins but is the v1 unlock for public-facing signup.

### M1.C — Cloud storage per-user OAuth (~1–2 weeks)

**The actual hosted-product blocker.** `src/services/cloud_sync/` authenticates to OpenCloud (and BYO Nextcloud) as a single shared admin service account; every user's files live under that one identity, namespaced by path. A path-scoping bug → user A reads user B's files. This is the only remaining place in the system where a single bug in our code = cross-user file exposure.

**Fix:** per-user OAuth.

- Each cockpit login also mints an OpenCloud OAuth token (OpenCloud already supports OIDC against our Keycloak).
- The orchestrator stores the user's refresh token in the consolidated `auth_tokens` table (or an `oauth_tokens` adjunct).
- All cloud-storage reads use *that user's* token. The shared admin token is retained only for orchestrator-internal operations (provisioning a new user's home folder, etc.) and is never used to serve user-facing reads.
- Refresh-token rotation handled at the BFF layer in the same shape as the Keycloak refresh.

**Touches:** `src/services/cloud_sync/*`, OpenCloud OIDC config, `auth_tokens` schema, `auth/bff.py` (token persistence on login), cockpit (no UI change expected). Detailed design pending — flag this as the next big initiative after M1.B.

**Do not open signups to strangers until this is done.** Every "fetch user file" call is otherwise one bug away from cross-tenant exposure.

### M1.D — Abuse prevention (design pending, ~6–9d total)

The previous "Out of scope (Phase 1)" line listed rate limiting as "not now". For strangers on AWS it's table stakes.

| # | Item | What it prevents | Rough effort |
|---|---|---|---|
| 1 | **Per-user API rate limiting** | UUID enumeration, endpoint hammering, LLM-cost runaway. Per-user (not per-IP) so paid-tier hooks attach later. | 2–3d (FastAPI middleware — slowapi or hand-rolled; per-route caps via decorator; Redis-backed counter to survive orchestrator restarts/scaling) |
| 2 | **K8s `ResourceQuota` + `LimitRange` per agent pod** | One user OOM-ing the cluster, CPU saturation, accidentally-infinite agent loops | 2–3d (Helm chart: per-user namespace OR labels-based quota; container CPU/RAM caps already exist on the pool but per-user totals don't; concurrent-jobs-per-user enforcement at dispatch time) |
| 3 | **Workspace egress `NetworkPolicy`** | Crypto-mining from your AWS IPs, port scanning, spam relay, ending up on an IP reputation list | 2–3d (default-deny egress + allowlist for LLM endpoints + Git + an opt-in per-user "permitted destinations" list; needs a CNI that supports egress policies — Cilium or Calico) |

Each needs a short design doc before implementation — these are new territory, not "wire up an existing primitive". Bundleable in parallel, but writing the designs is the bottleneck.

### M1.E — User account lifecycle (design pending, ~3–5d total)

Two flows SaaS needs that a single-org self-host doesn't.

| # | Item | What it covers | Rough effort |
|---|---|---|---|
| 1 | **User self-deletion** — `DELETE /api/me` with a written cascade plan | Account cancellation; GDPR "right to be forgotten". The cascade matrix is the hard part: jobs (soft-delete? hard-delete? what about jobs in shared projects?), threads (own), datasources (creator-owned), `project_members` (remove; lockout-prevention for last-owner projects), cloud-storage files (delete via M1.C OAuth path), Keycloak account (via admin API). | 2–3d (the matrix decisions take longer than the code) |
| 2 | **User data export** — `POST /api/me/export` returning a downloadable archive | GDPR data-portability; support workflows ("I want to see exactly what's stored about me"). Walks every resource that joins on `user_id` and serializes. Reuses the project knowledge-export pattern (F5). | 1–2d |

Today `DELETE /api/users/{id}` is admin-only (H2). M1.E is the user-facing self-service version of the same operation.

### M1.F — Admin UX hardening (✅ shipped, PR 4 open)

Triggered by the post-P4b finding that admins were god-mode by default and couldn't dogfood the regular-user UX without juggling accounts. **PRs 1–3 shipped + live-verified 2026-05-18** ([design](features/admin_view_as_user.md)). PR 4 (audit enrichment + remaining matrix walk) is folded into [M1.B #5](#m1b--cross-job-infrastructure-hygiene-quick-wins-1-day-total) above.

### M1 readiness gate

Pre-launch checklist. **Every line must be ✅ before opening signups beyond invited beta users.**

- [x] **M1.A** — every API endpoint gated; inventory test enforces; admin view-as toggle live
- [ ] **M1.B** — Keycloak self-reg works for strangers; Chromium per-job; MongoDB TTL; 403 audit log; view-as PR 4
- [ ] **M1.C** — cloud storage per-user OAuth (the actual blocker)
- [ ] **M1.D** — rate limiting + pod quotas + workspace egress allowlist (designs written *and* implementations shipped)
- [ ] **M1.E** — user self-deletion + data export

When all five tick, **M1 is done** and you can open signups to strangers without burning either user data or your AWS bill.

---

## Milestone 2 — Organizations (multi-tenancy)

**Goal.** Multiple organizations (teams of users) share one deployment, each fully isolated from the others — **including from administrators of other orgs.** *"Mandantenfähigkeit"* in the formal sense. A user can belong to multiple orgs; every resource belongs to exactly one org.

**Trigger.** Real demand. Not yet — individual-subscriber use cases sit comfortably inside M1.

**Why it stays cheap.** M1.A centralized visibility in `orchestrator/security/access.py`. Adding `organizations.id` as an additional constraint is a single change to `user_visible_project_ids` — every consumer (jobs / projects / datasources / threads / etc.) inherits the new restriction without a per-endpoint edit. The org wrapper is *additive*.

### M2.A — Organizations schema (3–5 days)

- New table `organizations(id, name, slug, created_at, …)`.
- `projects.organization_id` FK, `NOT NULL` after backfill (existing rows → an implicit `default` org).
- New table `organization_members(org_id, user_id, role)` with `owner/admin/member` roles.
- `user_visible_project_ids(user)` augments to include projects in orgs the user is a member of.
- Cockpit: org switcher (current org sets a per-session cookie), invite flow, org admin pane.

Schema design is straightforward — `project_members` is the template.

### M2.B — Per-tenant primitives

Things M1's "single org, many users" model didn't need.

| # | Item | Notes |
|---|---|---|
| 1 | **Per-org quotas** | M1.D quotas are per-user. For paying tiers, per-org caps (concurrent jobs, monthly LLM spend, datasource count, storage GB) become the canonical unit. **Extends** M1.D rather than replacing it. |
| 2 | **Per-org audit boundary** | An admin of org A must not see org B's audit log. The 403-audit primitive from M1.B needs `org_id` from day one or a retroactive backfill. |
| 3 | **Per-org branding / settings** | Logo, default expert configs, SSO config (if M2.C goes the per-realm route). Not security-critical, but the natural shape once you have orgs. |
| 4 | **Cross-org membership UX** | A user in multiple orgs needs a "which org am I working in right now" switcher. URL prefixing (`/o/{org-slug}/…`) is the conventional pattern. |

### M2.C — Realm strategy decision

| Approach | When to choose | Cost |
|---|---|---|
| **One Keycloak realm, org-as-group** | Different orgs share infrastructure (logins, MFA, OAuth providers). Same realm = same identity surface. | Cheap — add a Keycloak group per org. |
| **Realm-per-tenant** | Hard identity isolation; per-org SSO/SAML; regulatory compliance (financial, healthcare). | Expensive — provisioning, BFF rewrite, per-realm OIDC discovery. |

Default is "one realm" until a tenant requirement forces otherwise.

### M2 readiness gate

- [ ] **M1 fully done** (M2 builds on M1.A's centralized visibility *and* M1.D's quota plumbing)
- [ ] Organizations schema + helpers (M2.A)
- [ ] Per-org quota plumbing (extends M1.D, not parallel)
- [ ] Per-org audit boundary (extends M1.B #4)
- [ ] Realm decision made + implemented for chosen path (M2.C)
- [ ] Cross-org membership UX (cockpit switcher + URL prefixing)

---

> **Historical context (below).** The remaining sections preserve the original audit, Phase 1 plan, Track B design, severity-ordered roadmap, and Phase 2 sketch verbatim. They document **what M1.A delivered** and the design decisions behind it — kept for future readers who need to understand "why was this built this way" or "what was broken before this landed." Implementation status reflected in the strikethrough/checkmarks in each row.

## Original audit (2026-05-15, historical)

> Preserved verbatim from the deep audit that motivated this whole effort. **Every leak listed below has been closed** — see [Where we are now](#where-we-are-now-2026-05-17) for the post-implementation state and the per-PR fix references in the [Severity-ordered roadmap](#severity-ordered-roadmap). This section is kept for context: it documents what was broken pre-Track A so future readers can understand the design choices.

### Authentication is in good shape

Resolution: `orchestrator/security/auth.py:72-120` (`get_current_user()`). Three paths, all return a uniform user dict:

1. **BFF cookie** (`srw_session`, HttpOnly). Default for browsers.
2. **Bearer token** — JWT (Keycloak), PAT (`ak_…`), or legacy MCP token (`srw_…`). Validated against the consolidated `auth_tokens` table.
3. **Internal forwarded headers** (`X-MCP-User-Id` + `X-Internal-Key`). MCP server forwards these after its own OAuth validation. Trusted only if `MCP_INTERNAL_KEY` matches.

Two convenience dependencies exist and work:
- `require_approved_user()` — gates on `is_approved`. ~85 endpoints.
- `_require_admin()` — gates on `is_admin`. ~10 admin endpoints.

User provisioning: on first Keycloak login, `upsert_user_from_oidc()` (`postgres.py:5020-5126`) atomically creates the `users` row, a default project named `"{display_name}'s Project"`, and a `project_members(role='owner')` row. The `is_approved` flag is derived from Keycloak realm roles on every request — there's no DB approval column or admin UI. To approve a user, an admin assigns them the `user` role in the Keycloak Admin Console.

`users.is_admin` is a DB column but the source of truth is the Keycloak `realm_access.roles` claim, re-evaluated on every request. The DB column is a cache that lags until the user's next login.

### Authorization gaps — REST API

#### What's already correct

- `list_threads` (`main.py:10455`) — filters by `user_id` in SQL.
- `get_thread`, `end_thread`, SSE `/api/persistent/threads/{id}/stream` — check `thread.user_id == caller.id`, return 403 otherwise. **Bug:** the check allows access if `user_id IS NULL` (line 12361, 10561) — orphan threads after user deletion are public.
- `list_mcp_tokens` (`main.py:13412`) — scoped to caller's own tokens.
- All `/api/admin/*` endpoints — gated by `_require_admin()`.
- `GET /api/notifications/events` (SSE, `main.py:5171`) — per-user notification feed via `notification_feed.subscribe_sse(user_id)`.

#### What leaks (REST)

| Endpoint | File:line | Current behavior |
|---|---|---|
| `GET /api/jobs` | `main.py:3529` | Returns all jobs unless `user_id` query passed; MCP scope filtering partial |
| `GET /api/jobs/{id}` | `main.py:3571` | Grants access if no MCP scope header |
| `GET /api/jobs/{id}/audit` | `main.py:7676` | Full MongoDB audit trail for any job ID |
| `GET /api/jobs/{id}/llm-requests` | `main.py:12718` | Full LLM prompts + completions for any job ID |
| `GET /api/jobs/{id}/citations` | `main.py:8932` | All citations for any job ID |
| `GET /api/jobs/{id}/memories` | `main.py:9205` | All memories for any job ID |
| `POST /api/projects` | `main.py:15877` | Accepts arbitrary `body.user_id` — no check it matches caller |
| `GET /api/projects` | `main.py:15935` | Returns all projects if `user_id` omitted; the comment at `:15943` documents it as "admin view" without enforcing |
| `GET /api/projects/{id}` | `main.py:15953` | Returns any project |
| `GET /api/projects/{id}/members` | `main.py:16118` | Returns any project's members (incl. emails) |
| `POST /api/projects/{id}/members` | `main.py:16127` | **Anyone can add anyone as member with any role** — including making themselves owner of an arbitrary project |
| `PATCH /api/projects/{id}/members/{uid}` | `main.py:16171` | Anyone can change anyone's role on any project |
| `DELETE /api/projects/{id}/members/{uid}` | `main.py:16184` | Anyone can remove anyone (only the last-owner constraint is enforced) |
| `GET /api/projects/{id}/knowledge`, `/{note_id}`, `/search` | `main.py:17050, 17116, 17155` | No membership check |
| `POST /api/projects/{id}/knowledge/export` | `main.py:17330` | Exports full project knowledge base for any project ID |
| `GET /api/projects/{id}/datasources` | `main.py:16320` | No membership check |
| `GET /api/datasources` | `main.py:8514` | Returns all datasources globally |
| `GET /api/datasources/{id}` | `main.py:8533` | **Returns decrypted plaintext credentials (passwords, API keys, SSH connection strings)** — encryption at rest is bypassed at the response boundary |
| `GET /api/sources` | `main.py:8815` | Returns all sources globally |
| `GET /api/sources/{id}` | `main.py:8882` | Returns any source |
| `GET /api/agents`, `GET /api/agents/{id}` | `main.py:9915, 9932` | Full agent fleet incl. pod IPs and hostnames |
| `GET /api/stats/*` | `main.py:8748-8810` | System-wide aggregates |
| `GET /api/sudo/requests`, `/{id}` | `main.py:5398, 5416` | All sudo requests |
| `POST /api/sudo/requests/{id}/approve` | `main.py:5427` | **Any authenticated user can approve any job's sudo request** — privilege escalation, since approval is a trusted decision |
| `DELETE /api/users/{user_id}` | `main.py:15667` | No caller check; no Keycloak sync; no orphan cleanup |

#### What leaks (Builder sessions)

`builder_sessions` table exists with a `user_id` column (`schema.sql:1003-1025`), but the read/write endpoints don't consult it:

| Endpoint | File:line | Current behavior |
|---|---|---|
| `GET /api/builder/sessions/{id}` | `main.py:17436` | Returns any session by ID |
| `GET /api/builder/sessions/{id}/messages` | `main.py:17509` | Returns any session's full message history |
| `POST /api/builder/sessions/{id}/message` | `main.py:17518` | **Streams LLM tokens for any session ID, and the body carries `active_job_id` / `active_project_id` that the handler operates on without checking access** |

`POST /api/builder/sessions` (create) and `GET /api/builder/sessions` (list) are correctly user-scoped.

#### What leaks (streaming surfaces)

| Endpoint | File:line | Current behavior |
|---|---|---|
| `WS /api/ide/{job_id}/proxy/{path:path}` | `main.py:7546` | **Critical — unauthenticated WebSocket proxy to the workspace's code-server IDE. Full file r/w, terminal, code execution for any guessed job UUID. No auth check at all.** |
| `SSE /api/sudo/events` | `main.py:5363` | No auth. Broadcasts all sudo requests system-wide to any subscriber. |

### MCP server — 97 of 99 tools ignore scope

`orchestrator/mcp/server.py:74-98` (`_get_client()`) correctly extracts the authenticated MCP token's user and scope and injects them as `X-MCP-User-Id` / `X-MCP-Scope` / `X-Internal-Key` headers. The transport-level plumbing is right.

But the backend ignores these headers everywhere except `list_jobs` and `get_job` (`main.py:3543, 3580`). All other MCP tools delegate to the same leaking REST endpoints listed above. **An MCP token currently marked `scope='user'` grants global read access to ~97 resources** (audit trails, LLM requests, projects, knowledge, datasources, sources, agents, sudo requests, etc.).

The `scope='all'` value's semantics are undefined in the codebase; today it acts as "no filter."

### Agent ↔ orchestrator boundary (Track B)

This is a structurally different problem from API data isolation. It's about *who* the orchestrator trusts to be an agent, and what the agent receives.

**Open agent registration** (`main.py:9393`): `POST /api/agents/register` has no auth check. Any host on the network can:
1. POST `config_name`, `pod_ip`, `pod_port`, `hostname` and receive a fresh `agent_id` UUID.
2. Heartbeat as that agent at `POST /api/agents/{id}/heartbeat` (`main.py:9863`) — also no auth.
3. Wait for the orchestrator's auto-assign dispatcher to send it a `JobStartRequest`.

**Credentials in dispatch payload** (`main.py:1078-1154`): `JobStartRequest` includes:
- `datasources_payload` with **plaintext credentials** for non-managed connectors (`_build_datasources_payload`, `main.py:8352`).
- `config_override.llm.api_key` and `config_override.env_keys.*` populated by `_inject_dispatch_credentials()` (`main.py:728`).
- SSH host/port/key path for the workspace.

If a rogue agent registered with a controlled `pod_ip`, the orchestrator would POST this payload to the attacker's HTTP server. The credentials are scoped correctly to the job's user/project at resolution time, but the trust boundary at delivery is just "we sent it to whoever was registered."

**Sudo approval not user-scoped** (`main.py:5427`): The agent (via VM daemon) submits sudo requests via NATS, correctly bound to a `job_id`. But the approval endpoint has no auth check on the approver — any authenticated user can approve any job's sudo request. This is a trust escalation, not just data leak.

**Workspace isolation is good** between jobs: each job gets a fresh assigned workspace (Docker pool slot / K8s pod / VM), and teardown is enforced (SSH `rm -rf` for Docker, pod deletion for K8s). But within an agent process running multiple jobs in sequence (`--loop`), Chromium's `--user-data-dir=/tmp/agent-chromium-cdp-profile` is hardcoded (`src/tools/research/browser.py:101`) and not cleared between jobs — so browser cookies/sessions persist across jobs on the same agent.

### Cross-cutting data surfaces

- **MongoDB audit / LLM requests:** `get_job_audit()` and `list_llm_requests()` in `orchestrator/database/mongodb.py` filter only by `job_id`. Authorization is the REST caller's responsibility. Since the REST endpoints don't check, the full Mongo trail (prompts, completions, tool args) is exposed for any guessed job ID. No retention policy or TTL index.
- **Memories / RecallStore:** SQL functions `memory_hybrid_search` and `memory_project_hybrid_search` (`vector_schema.sql:262, 337`) correctly filter by `job_id` or `project_id`. The leak is at the REST layer (`/api/jobs/{id}/memories`), not in the storage.
- **Sources by `content_hash`:** Dedupe is intentional and correct. But the shared `name` / `description` / metadata is visible to any job linking the source; if user B ingests user A's document, they see user A's source metadata. Fine if we accept it; flag for awareness.
- **Vector index (HNSW):** Global index over all embeddings. Query-time filtering by `job_id` is correct in the SQL functions, but the REST endpoints don't gate access to those functions.
- **Cloud storage (OpenCloud / Nextcloud):** One service account across all users (`orchestrator/services/cloud/opencloud.py:98-116`). No per-user OAuth. Compromise of the service account credential = access to all users' files. This is the largest single-blast-radius issue if/when this becomes a hosted product.
- **Datasource credentials:** Encrypted at rest (`postgres.py:29-69`), decrypted before return on `GET /api/datasources/{id}` — and that endpoint has no auth check. So encryption-at-rest provides no protection from this leak.
- **System provider API keys:** Stored in `system_api_keys`, listed via admin-only endpoint (`main.py:13875`), returned as prefix-only. Correct.
- **Neo4j:** No multi-tenancy in the driver. Each agent uses the datasource's connection per job. Safe assuming each project has its own Neo4j instance; risky if a single instance is shared.

### Schema — mostly ready, with clarifications

| Table | Owner column | Project link | Notes |
|---|---|---|---|
| `users` | — | `default_project_id` (NOT NULL FK) | `is_admin` is a cache of KC realm role; `is_approved` is not a DB column at all |
| `projects` | (via `project_members`) | — | No `created_by`. `is_default=true` for personal projects |
| `project_members` | composite `(project_id, user_id)` + `role` | — | Roles: `owner`/`editor`/`viewer`. The authoritative membership table. |
| `jobs` | `user_id` (indexed) | `project_id` (indexed, nullable) | Ready |
| `threads` | `user_id` | `project_id` (nullable) | **No index on `user_id`** — should add |
| `builder_sessions` | `user_id` (nullable) | `job_id` (nullable) | Has the column, endpoints just don't use it |
| `auth_tokens` | `user_id` (CASCADE) | — | `scope` (legacy MCP) + `scopes[]` (new PAT, currently permissive per migration 0010 comment) |
| `datasources` | `created_by` (nullable) | `project_id` column **and** `project_datasources(project_id, datasource_id)` junction | Migration 0010 backfilled column → junction. Junction is canonical going forward; column is legacy |
| `agents` | — | — | Pure infra; no user dimension. Should be admin-only at the API |
| `sudo_approval_requests` | — | `job_id` (FK CASCADE) | Access via job |
| `sources` (vector) | — | — | Shared by `content_hash`; access via `job_sources` join |
| `job_sources` | — | — | The only access path to a source |
| `citations` (vector) | — | `job_id` | Access derived from job access |
| `memories` (vector) | — | `job_id` (NOT NULL) + `project_id` (nullable) | Access via job (project_id is just a tag for cross-job sharing within a project) |
| `knowledge_index` (vector) | — | `project_id` (NOT NULL) | Access via project membership |

**Helper that already does the right thing:** `get_projects_for_user(user_id)` at `postgres.py:5503-5543` — JOINs `project_members`, returns only the user's projects with `user_role`. Currently unused by the API (per the leak inventory above, `GET /api/projects` ignores it). Phase 1 should make it the canonical path.

---

## Phase 1 plan (Track A: data isolation) — historical (M1.A delivered this)

### Principles

1. **The API is authoritative**; the Cockpit is a courtesy.
2. **One model, applied uniformly**: a user can see a resource iff (a) they own it, (b) they're a member of a project that owns it, or (c) they're an admin.
3. **No more inline checks**: introduce a small set of FastAPI dependencies and shared SQL helpers.
4. **MCP scope is the same model**: a token's scope further restricts the same visibility set; it doesn't have its own parallel path.
5. **Fail closed**: when in doubt, deny.

### Building blocks (new code)

**File: `orchestrator/security/access.py` (new)** — sketched API:

```python
async def user_visible_project_ids(user, db) -> set[UUID] | Literal["all"]:
    """Project IDs the user can see (via project_members).
    Admins return the sentinel 'all'."""

def user_visible_jobs_clause(user) -> tuple[str, dict]:
    """SQL fragment usable in WHERE.
    For users: (jobs.user_id = :uid OR jobs.project_id = ANY(:projects))
    For admins: TRUE"""

def require_project_member(role: Literal['viewer','editor','owner'] = 'viewer'):
    """Dependency. Resolves project_id from path, checks membership, returns project row.
    Admins bypass."""

def require_job_access():
    """Dependency. Resolves job_id from path, checks ownership or project membership,
    returns job row. Admins bypass."""

def require_builder_session_owner():
    """Dependency. Resolves session_id from path, checks ownership.
    Admins bypass."""

def apply_mcp_scope(user, mcp_scope, base_clause) -> tuple[str, dict]:
    """Apply MCP token scope as an additional WHERE filter on top of user visibility.
    Used by every list/get endpoint, not just two."""
```

**Schema migration** (`orchestrator/database/migrations/app/NNNN_threads_user_index.notx.sql`):

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_threads_user_id ON threads(user_id);
```

### Per-endpoint fixes (grouped by surface)

1. **Jobs family** — `list_jobs`, `get_job`, `*/audit`, `*/llm-requests`, `*/citations`, `*/memories`. `Depends(require_job_access())` for get-by-id; `user_visible_jobs_clause` for list. MongoDB queries inherit auth from the REST handler.
2. **Projects family** — `list_projects` (use `get_projects_for_user` always), `get_project`, `*/members` (read), `*/datasources`, `*/knowledge`, `*/knowledge/{id}`, `*/knowledge/search`, `*/knowledge/export`. Wrap each with `Depends(require_project_member())`.
3. **Project mutations** — `create_project` (force `user_id = caller.id`), `add_project_member` / `update_project_member` / `remove_project_member` (require `role='owner'`). These are the privilege-escalation paths; close them first.
4. **Datasources** — `list_datasources` becomes admin-only or scoped via project membership (junction table). `get_datasource` requires project membership via junction. **Also: redact credentials from the response by default** — full credential read should require explicit `?include_credentials=true` and a higher gate (e.g. owner role on the project).
5. **Sources/citations** — Source visibility derives from job visibility through `job_sources`. Add `job_sources(source_id)` index if missing for performance.
6. **Stats** — Recompute aggregates over visible job set; admins unchanged.
7. **Sudo requests** — Reuse `require_job_access` for list/get **and approve** (the approve check is the trust-escalation fix).
8. **Agents** — `list_agents` / `get_agent` become admin-only. Add a separate `GET /api/me/active-jobs` projection for non-admin users to see their own running work, without pod IPs.
9. **Builder sessions** — `get_builder_session`, `get_builder_messages`, `send_builder_message` all wrap with `Depends(require_builder_session_owner())`. Also verify `body.active_job_id` / `body.active_project_id` against the caller's access in `send_builder_message`.
10. **User deletion** — `DELETE /api/users/{id}` gate on `_require_admin()`; consider a follow-up to either soft-delete or move orphans to an admin-owned project.
11. **Thread orphan bug** — change the `if thread.get("user_id") and ...` check to `if thread.get("user_id") != caller.id` (fail closed when NULL).

### MCP tools — apply scope uniformly

Each MCP tool today is a thin wrapper around a REST call. Once the REST endpoints enforce per-call authorization (above), the MCP tools inherit it for free. The remaining work is to ensure `apply_mcp_scope()` is consulted by every list/get helper (not just two endpoints) so that an MCP token with `scope='user'` correctly restricts beyond the user's full access — and to write down what `scope='all'` means (recommend: "admin equivalent for this token's user, requires the user to actually be admin").

### Streaming surfaces

- **`/api/ide/{job_id}/proxy/*` (WebSocket):** Add cookie-based auth (use `resolve_ws_user`) and `require_job_access` equivalent before opening the proxy. **This should ship as a hotfix, ahead of the rest of Phase 1.**
- **`/api/sudo/events` (SSE):** Gate on `_require_admin()` or scope to the user's visible job set.

### Cockpit changes

1. Remove the UI-only "mine" filter in `job-list.component.ts:794-798`.
2. Add ownership-aware route guards for `/projects/:id`, `/debug/...`, etc.
3. Hide nav items the user can't act on.
4. After the IDE proxy hotfix, verify the embedded IDE still works for legitimate users.

### Testing

- **Fixture:** three users (`userA`, `userB`, `admin`), two projects, one job per (project, user).
- **Test matrix:** for every list endpoint, assert userA sees only their data, userB only theirs, admin sees both.
- **For every get-by-id**, assert userA accessing userB's resource returns the expected status (see open Q #1).
- **MCP scope negative tests:** a token with `scope='user'` for userA accessing userB's job ID must be denied.
- **SSE/WebSocket:** subscribing to another user's thread ID, IDE proxy, sudo events — all must reject.
- **Builder sessions:** send a message to a stranger's session must 403.
- **Project member mutations:** a non-member trying to invite themselves must 403.
- **Regression net:** CI lint that fails if any `@app.get/.post/.delete/.patch("/api/...")` is added without either a `Depends(require_*)` or an explicit `# nosec: public` comment.

### Migration / rollout

- Existing data already has `user_id` and `project_id` populated on jobs and threads.
- Orphan handling: jobs/threads with `user_id IS NULL` become admin-only by default. Optionally backfill to a system "orphans" project.
- Projects with no members: admin-only by default.
- No data loss; purely additive query restrictions.

### PR breakdown

See the consolidated **Severity-ordered roadmap** below — bundles P0-P3 cover all the Track A work in dependency-aware order.

---

## Track B: Agent ↔ orchestrator boundary — historical (P4b delivered this, part of M1.A)

Separate workstream. Doesn't block the current single-org self-hosted use case (the cluster network is trusted by design), but blocks any hosted/multi-tenant deployment because a compromised pod or a foothold on the cluster network = full credential exfiltration via dispatch interception.

### What's wrong (recap)

- `POST /api/agents/register` and `POST /api/agents/{id}/heartbeat` have no auth.
- Agents are identified only by a UUID returned at registration. Spoofing is trivial if the UUID is known or guessable.
- `JobStartRequest` ships plaintext credentials to whatever `pod_ip:pod_port` is registered.
- No way for the orchestrator to verify the receiving agent is the intended one for the job.
- Chromium profile persists across jobs on the same agent (`/tmp/agent-chromium-cdp-profile`).

### Approach options

Three rough paths, each progressively stronger:

1. **Pre-shared secret + network policy.** Cheapest: agents present a shared `AGENT_BOOTSTRAP_SECRET` at registration, get back a per-agent token, use it for heartbeats and accept-dispatch ACK. Add a K8s NetworkPolicy that only allows pods in the agent namespace to reach the agent endpoints. Closes the "rogue external host" attack; doesn't help against a compromised cluster pod.
2. **Per-job dispatch tokens.** Orchestrator generates a short-lived JWT per job, signs it with a secret only the orchestrator holds. Agent must ACK dispatch with that token. Stops one agent from impersonating another's job, but credentials still ship in plaintext at the moment of dispatch.
3. **mTLS between agents and orchestrator.** Proper solution. Cert-manager or a static CA, agents authenticate with client certs, certs rotate. Combine with per-job tokens for fine-grained authz.

Decision needed: how hard do we want to harden this for the current state (self-hosted, trusted cluster) vs. for a hosted future. The minimum recommended for "good enough for self-hosted with multiple users on one cluster" is option 1. Option 2 is the right Phase 1 of Track B if we're heading toward subscription. Option 3 is the eventual destination.

Also in Track B regardless of which option is chosen:
- Clear Chromium profile between jobs (workspace cleanup hook).
- Encrypt credentials in the dispatch payload, decrypt only after the agent has authenticated and accepted the job.
- Audit log every dispatch with `(job_id, agent_id, dispatched_at)` so a future incident can be traced.

### PR breakdown

See the consolidated **Severity-ordered roadmap** below — Track B is grouped there alongside Track A.

---

## Severity-ordered roadmap (M1.A implementation log — historical)

Findings ranked by severity, then grouped into shippable bundles in dependency order. P0s are independent of each other and should land as soon as possible. P1+ depend on the `access.py` foundation (bundle F1).

### Severity rubric

- **P0** — anonymous or any-authenticated exploit with privileged blast radius (shell access, mass deletion, takeover-of-resource). Ship as a hotfix, ideally same week.
- **P1** — any-authenticated exploit with high blast radius (credential exfil, LLM hijack, full project data dump). Land in the next ~1-2 weeks.
- **P2** — authenticated info-disclosure across the API surface. No single trivial escalation but each leaks meaningful data. Next sprint.
- **P3** — cleanup, hardening, defense-in-depth.
- **Track B** — agent ↔ orchestrator infrastructure auth. Decoupled from data isolation; doesn't block current self-hosted single-org use but blocks any hosted/subscription path.
- **Phase 2** — organizations, cloud storage per-user OAuth, retention.

### P0 — Hotfixes (ship this week)

**Status: all five live-verified on the dev deployment 2026-05-16** (`sha-987575b`). See the verification matrix at the end of this section.

Five independent small PRs. None depend on the `access.py` foundation; each is a few-line auth-check addition. Total ~2d.

| ID | Finding | Fix | File:line | Est. |
|---|---|---|---|---|
| **H1** | `/api/ide/{job_id}/proxy/{path}` HTTP + WebSocket — no auth at all; full IDE access (shell, file r/w, code exec) on any guessed job/thread UUID | Add cookie auth via `resolve_ws_user` (WS) / `require_approved_user` (HTTP) + job-or-thread access check before opening the proxy. After landing, verify the embedded IDE still works for legitimate users | `main.py:7481, 7546` | 0.5d |
| **H2** | `DELETE /api/users/{id}` — only requires `require_approved_user`; **any approved user can delete any other user, including admins** | Swap to `_require_admin()`. Self-service deletion isn't exposed; add a separate endpoint if needed (with Keycloak sync + orphan handling) | `main.py:15666` | 0.25d |
| **H3** | Project member mutations (`add_project_member`, `update_project_member`, `remove_project_member`) — no auth; **anyone can make themselves owner of any project**, which opens every other gate | Owner-or-admin gate via `_require_project_owner` for add/update; same gate OR self-removal for remove (so members can leave). Foundational privilege escalation | `main.py:16127, 16171, 16184` | 0.5d |
| **H4** | `POST /api/sudo/requests/{id}/{approve,deny,approve-upgrade,resume-without-vm}` — no caller check; any authenticated user can approve any job's privileged shell command. Job owners must NOT self-approve | All four endpoints gated by `_require_sudo_request_authority` (admin or project-owner of the related job) | `main.py:5427, 5443, 5456, 5475` | 0.25d |
| **H5** | `POST /api/projects` (no auth, accepts arbitrary `body.user_id`), `PATCH /api/projects/{id}` (no auth), `DELETE /api/projects/{id}` (no auth, cascade-deletes repos + KC groups + cloud folders), `POST /api/users` (weak auth) — same-class findings missed by the original audit | create_project: `require_approved_user` + bind `owner_id` to caller (admins can specify); update/delete: `_require_project_owner`; create_user: `_require_admin`. Subsumes F4 | `main.py:16014, 16158, 16189, 15734` | 0.5d |

### P0 live verification (2026-05-16)

Verified end-to-end on the dev deployment (`api.superhuman-remote-worker.com`) against orchestrator image `sha-987575b`. Method: logged in as `knaeckebrothero` (admin) for setup + positive paths; registered a fresh `srw-hotfix-test` non-admin via Keycloak self-registration for the cross-user attack paths.

| ID | Attack (non-admin → admin's resource) | Result | Admin path | Result |
|---|---|---|---|---|
| **H1** | `GET /api/ide/{id}/proxy/` | 403 *"IDE access denied"* | n/a (no live IDE session) | — |
| **H2** | `DELETE /api/users/{admin_id}` | 403 *"Admin access required"* | Admin `DELETE` test user | 200 *"deleted"* |
| **H3 add** | `POST .../members` adding self as owner | 403 *"Project owner role required"* | Admin add (deferred to UI smoke) | — |
| **H3 update** | `PATCH .../members/{admin}` demote admin | 403 *"Project owner role required"* | n/a | — |
| **H3 remove** | `DELETE .../members/{admin}` kick admin | 403 *"Project owner role required"* | Self-remove (member): 200 *"removed"* | ✓ carve-out |
| **H3 self** | `DELETE .../members/{self}` from project not member of | 404 *"Member not found"* | — | ✓ gate let through; lookup failed correctly |
| **H4** | sudo approve | (skipped — no live pending sudo request) | — | — |
| **H5a** | (unauth baseline already 401) | n/a | Admin `POST /api/projects` with arbitrary `user_id` | 200 (admin bypass branch works) |
| **H5b** | `PATCH /api/projects/{admin_proj}` | 403 *"Project owner role required"* | Admin owner `PATCH`: 200 *"updated"* | ✓ |
| **H5c** | `DELETE /api/projects/{admin_proj}` | 403 *"Project owner role required"* | Admin owner `DELETE`: 200 *"deleted"* | ✓ |
| **H5d** | `POST /api/users` | 403 *"Admin access required"* | — | — |

The H3 self-removal carve-out is worth highlighting: the 404 from `self_remove_from_project_not_member` proves the gate path is correct — if the carve-out were broken we'd see 403 (gate blocks) instead of 404 (gate passes, lookup fails).

H4 and the H1 live happy path weren't exercised because they need real fixtures (a pending sudo request and a running IDE session, respectively). The negative paths verify the gate fires; happy-path smoke tests should ride with whatever PR introduces the F1 three-user fixture in CI.

### Adjacent findings (not P0 work)

Surfaced while preparing the non-admin tester via Keycloak self-registration. Neither blocks H1-H5; both make new-user onboarding via the registration page silently broken today:

1. **`VERIFY_EMAIL` required action with no SMTP configured.** The realm has registration enabled, but a fresh registration triggers an email-verify required-action and Keycloak's "failed to send email" error page. New users can never finish registering on their own. Workaround used: flip `email_verified=true` and clear `user_required_action` in Keycloak DB.
2. **New registrants do not receive the `user` realm role.** `default-roles-srw` is a composite that only carries client-roles (`view-profile`, `manage-account`, `offline_access`, `uma_authorization`) — not the realm-level `user` role. Without it, `is_approved` resolves to `false` and `require_approved_user()` 403s every endpoint. Workaround: insert into `user_role_mapping` and restart Keycloak to clear its user cache.

Suggested follow-ups (separate ticket from multi-tenancy): either configure SMTP and add `user` to the composite default role, or replace self-registration with admin-approval flow. The auth doc already notes that approval lives in Keycloak roles (no DB column); these two issues make that path actually usable.

### P1 live verification (2026-05-16)

Verified end-to-end on the dev deployment against orchestrator image `sha-74709d8` (commit `74709d8`). Method: logged in as `knaeckebrothero` (admin) and exercised the admin-positive paths + unauthenticated baseline + gate-path-correctness checks. Cross-user negative paths were not re-tested live because the `srw-hotfix-test` user was already cleaned up after H1-H5 verification; the doc's two adjacent Keycloak findings (above) still block trivial creation of a non-admin fixture. The 191 unit tests (`test_security_access`, `test_builder_session_access`, `test_datasource_access`, `test_knowledge_access`, `test_sudo_events_sse`, `test_mcp_scope`) cover every cross-user attack path against the helpers themselves.

| ID | Live check | Result |
|---|---|---|
| **Baseline** | Unauthenticated `GET` on 8 representative endpoints (datasources / builder sessions / knowledge / sudo SSE) | All 401 ✓ |
| **F2 ownership** | `POST /api/builder/sessions` with `body.user_id` = naimroca10's id | 200; session created with admin's id (impersonation blocked) ✓ |
| **F2 get-by-id** | `GET /api/builder/sessions/<deadbeef>` | 404 (gate let through, lookup failed) ✓ |
| **F2 list cross-user** | `GET /api/builder/sessions?user_id=naimroca10` as admin | 200 with naimroca10's 2 sessions (admin override allowed) ✓ |
| **F3 list redaction** | `GET /api/datasources` as admin | 200 with 9 rows, **no `credentials` field on any row** ✓ |
| **F3 get redaction** | `GET /api/datasources/d9924443-…` (a `repository` ds with 431 bytes of stored credentials in DB) | 200, response has 12 keys, none of them `credentials` ✓ |
| **F5 admin path** | `GET /api/projects/<my_pid>/knowledge/summary` | 200 with summary payload ✓ |
| **F5 missing-project 404** | `GET /api/projects/<bogus>/knowledge/summary` | 404 *"Project '…' not found"* — confirms the gate runs before any vector-DB query ✓ |
| **F6 SSE auth** | `GET /api/sudo/events` with admin cookie | 200 + `text/event-stream` + `: keepalive` frame ✓ |
| **F7** | Live MCP-scope test deferred — requires an MCP token issued for a non-admin user with `project:<uuid>` scope, and the same Keycloak fixture pain as the F2/F3/F5 cross-user attacks. Covered by 45 unit tests including the auth-side header plumbing. | deferred |

Negative cross-user paths for F2/F3/F5 are also deferred until a non-admin Keycloak fixture is available (or the registration flow is fixed). The unit tests prove the gate logic; live verification would only confirm "the request actually reaches the gate," which the missing-project 404 from F5 above already establishes for the gate-path layer.

### P2 live verification (2026-05-16)

Verified end-to-end on dev cluster image `sha-811a8e9` (commit `811a8e9` — the deploy that landed all of G1-G5 together). Method: logged in as `knaeckebrothero` (admin) via the BFF cookie at `https://superhuman-remote-worker.com/`, then drove `fetch()` from the browser against `https://api.superhuman-remote-worker.com/`. 20 checks total — 14 in the main matrix (admin happy paths + 401 baseline + gate-path 404s) plus 6 G1 sub-endpoints exercised against a real `job_id`.

| ID | Live check | Result |
|---|---|---|
| **Baseline** | Unauthenticated `GET /api/agents`, `/api/stats/jobs`, `/api/snapshots/stats`, `/api/me/active-jobs` | All 401 ✓ |
| **G1** | Admin `GET /api/jobs?limit=5` | 200 ✓ |
| **G1** | Admin `GET /api/jobs/{bogus-uuid}` | 404 *"Job '…' not found"* — gate path correct ✓ |
| **G1 subs** | Admin `GET /api/jobs/{real_id}/{audit,citations,workspace,todos,progress}` | All 5 200 ✓ |
| **G2** | Admin `GET /api/projects` | 200 ✓ |
| **G2** | Admin `GET /api/projects/{bogus-uuid}` | 404 *"Project '…' not found"* — gate path correct ✓ |
| **G3** | Admin `GET /api/sources?limit=5` (no `?job_id=`) | 200 ✓ (admin-only branch passes) |
| **G3** | Admin `GET /api/sudo/requests?limit=5` | 200 ✓ |
| **G3** | Admin `GET /api/sudo/requests/{bogus-uuid}` | 404 *"Sudo request '…' not found"* — gate path correct ✓ |
| **G4** | Admin `GET /api/agents?limit=5` | 200 ✓ |
| **G4** | Admin `GET /api/me/active-jobs` projection | 200; response keys = `[assigned_agent_id, branch_name, config_name, created_at, description, id, merge_status, parent_job_id, priority, project_id, repo_name, snapshot_status, status, user_id]` — confirmed **no `pod_ip`, no `hostname`** ✓ |
| **G5** | Admin `GET /api/stats/jobs` | 200 — `{total_jobs: 39, completed: 13, failed: 7, cancelled: 10}` ✓ |
| **G5** | Admin `GET /api/stats/daily?days=7` | 200 — per-date rows ✓ |
| **G5** | Admin `GET /api/stats/stuck` | 200 — `[]` (no stuck jobs) ✓ |
| **G5** | Admin `GET /api/stats/agents` | 200 — `{total: 8, ready: 2, offline: 6}` ✓ |
| **G5** | Admin `GET /api/snapshots/stats` | 200 — `{total_snapshots: 3, total_size_bytes: 153 MB}` ✓ |

Cross-user negative paths for G1-G3 + G5 visibility-scoped endpoints are deferred for the same reason F2/F3/F5 deferred theirs: the two Keycloak self-registration gaps surfaced during H1-H5 still block trivial creation of a non-admin test user. The 161 new unit tests across G1-G5 (and the 293-test access-area suite as a whole) cover every cross-user attack path against the helpers themselves. The live admin-positive + gate-path 404 checks above prove the gates are actually reached by the requests, which together with the unit tests fully validates the new code paths.

### P1 — Critical (ship within ~1-2 weeks)

Authenticated exploit with high blast radius. F1 must land first; F2-F7 can fan out in parallel after.

| ID | Finding | Fix | File:line | Est. |
|---|---|---|---|---|
| ~~**F1**~~ | ~~No `access.py`, no `require_project_member` / `require_job_access` deps, no shared visibility helpers. Every existing scope check is inline~~ | **Implemented + tested 2026-05-16 ✓ (40 unit tests, helpers exercised in 191-test access-area suite).** `orchestrator/security/access.py` ships the full sketched API (`user_visible_project_ids`, `user_visible_jobs_clause`, `require_project_member`, `require_project_owner`, `require_job_access`, `require_builder_session_owner`, `user_can_access_ide_entity`, `require_sudo_request_authority`, `apply_mcp_scope`). H1-H5 helpers moved over without behavior changes. Migration `0012_threads_user_id_index.notx.sql` lands the index. 3-user fixture (`user_a`, `user_b`, `user_admin` + `fake_db`) in `tests/conftest.py`. 40 unit tests in `tests/test_security_access.py`. | `orchestrator/security/access.py`, `tests/conftest.py`, `tests/test_security_access.py` | 1d |
| ~~**F2**~~ | ~~Builder sessions: 3 endpoints with zero ownership check; POST also accepts `active_job_id`/`active_project_id` from body without verification~~ | **Implemented + tested 2026-05-16 ✓ (16 unit tests + dev cluster: ownership-force confirmed, bogus session → 404 via gate path).** Scope expanded from 3 → 5 endpoints (same bug class on `list_builder_sessions` `?user_id=` and `create_builder_session` `body.user_id`). `require_builder_session_owner` gates `get`/`messages`/`send`; `send-message` also validates `active_job_id` via `require_job_access` and `active_project_id` via `require_project_member` (open-Q #6 resolved: **fail closed**). `create_builder_session` forces `user_id = caller.id`; `list_builder_sessions` rejects cross-user `?user_id=` with 403 (admin override allowed). `get_builder_session` SELECT now returns `user_id` so the owner gate has the field it needs. 16 unit tests in `tests/test_builder_session_access.py`. | `orchestrator/main.py:17500-17645`, `orchestrator/database/postgres.py:6111` | 0.5d |
| ~~**F3**~~ | ~~`GET /api/datasources/{id}` returns decrypted plaintext credentials with no auth check~~ | **Implemented + tested 2026-05-16 ✓ (36 unit tests + dev cluster: `GET /api/datasources` returns 9 rows with no `credentials` field; DB still stores 431-byte creds for the repository ds).** Scope expanded from 2 → 11 endpoints (same bug class across the surface). **Open-Q #2 decided: strip always (no `?include_credentials=true`, no separate endpoint).** New helpers in `access.py`: `redact_datasource`, `redact_datasources`, `user_can_access_datasource`, `require_datasource_access`, `require_datasource_owner`. Endpoints: list/get/create/update/delete/test plus 4 project-link endpoints all gated; credentials stripped from every response. `update_datasource` preserves stored credentials when body's `credentials` is null/empty (cockpit edit-form pattern). Cockpit edit form (`datasource-list.component.ts`) no longer pre-fills credentials; `buildCredentials()` returns `undefined` when editing with blank fields. 36 unit tests in `tests/test_datasource_access.py`. | `orchestrator/main.py:8568-8815, 16412-16517`, `orchestrator/security/access.py`, `cockpit/src/app/views/datasources/datasource-list.component.ts` | 1d |
| ~~F4~~ | ~~`create_project` accepts arbitrary `body.user_id`~~ | **Subsumed by H5** — fixed in the create_project hotfix (admins can still specify on behalf of others; regular users bound to themselves) | — | — |
| ~~**F5**~~ | ~~Knowledge endpoints (read/note/search/export) — no membership check on the project~~ | **Implemented + tested 2026-05-16 ✓ (15 unit tests + dev cluster: admin own-project 200, missing-project 404 via gate path).** Scope expanded from 4 → 7 endpoints (summary + list + get-note + search + patch-note + delete-note + export). All seven now call `require_project_member` (viewer-minimum). Export decision: viewer is sufficient — the bulk dump is equivalent to scraping list+get note-by-note, so a tighter gate wouldn't close a real gap. 15 tests in `tests/test_knowledge_access.py` (cross-user 403, missing-project 404, member-passes-gate, admin bypass). | `orchestrator/main.py:17120-17531` | 0.5d |
| ~~**F6**~~ | ~~`SSE /api/sudo/events` — no auth, broadcasts all sudo requests to any subscriber~~ | **Implemented + tested 2026-05-16 ✓ (10 unit tests + dev cluster: admin cookie → 200 `text/event-stream` + keepalive frame; unauthenticated → 401).** Chose per-user filter over admin-only (matches H4's job-access authority model so project members get real-time notifications for their own jobs). New `user_can_access_job` bool helper in `access.py`. SSE handler now requires `require_approved_user` and filters each event by job access before yielding; orphan events (no `job_id`) are admin-only. `sudo_gate._broadcast_sse('request_decided', …)` payloads now carry `job_id` (3 sites: approve/deny/sweep_expired; the sweep SQL also `RETURNING` the column). 10 tests in `tests/test_sudo_events_sse.py` + helper coverage in `tests/test_security_access.py`. | `orchestrator/main.py:5375`, `orchestrator/services/sudo_gate.py`, `orchestrator/security/access.py` | 0.4d |
| ~~**F7**~~ | ~~MCP server: 97 of 99 tools ignore `X-MCP-Scope`. Token with `scope='user'` is effectively godmode~~ | **Implemented + tested 2026-05-16 ✓ (45 unit tests across all 9 visibility helpers × scope shapes; live MCP-token testing deferred — needs `project:<uuid>`-scoped token fixture).** Open-Q #3 resolved: ``scope='all'`` = admin-equivalent for THIS user (no extra grant if user isn't admin). ``security/auth.py:_get_user_from_mcp_headers`` now reads `X-MCP-Scope` and stashes it in `user['scopes']`. Three scope-guard helpers in `access.py`: `_scope_project_id`, `_scope_permits_project`, `_scope_permits_personal`. All nine visibility helpers (`user_visible_project_ids`, `require_project_member` / `_owner`, `require_job_access` / `user_can_access_job`, `user_can_access_ide_entity`, `require_builder_session_owner`, `require_sudo_request_authority`, `user_can_access_datasource` / `require_datasource_owner`) consult the scope on top of identity. ``project:<uuid>`` narrows visibility to one project AND restricts admin powers to that project; personal resources (threads, builder sessions) become inaccessible for project-scoped tokens. Malformed `project:<bad>` scopes fail closed via a sentinel UUID. 45 unit tests in `tests/test_mcp_scope.py`. | `orchestrator/security/access.py`, `orchestrator/security/auth.py` | 1d |

**P1 total: ~3.5d** after F1 lands. **All P1 work (F1, F2, F3, F5, F6, F7) shipped 2026-05-16. ~3.4 engineering days actual. Phase 1 keystone complete — P2/P3 are now incremental improvements on top.**

### P2 — High value (ship next sprint)

Authenticated info disclosure across the rest of the API. Each bundle is independent and parallelizable.

| ID | Finding | Fix | File:line | Est. |
|---|---|---|---|---|
| ~~**G1**~~ | ~~Jobs read family — list_jobs, get_job, /audit, /llm-requests, /citations, /memories. The MongoDB-backed endpoints (audit, llm-requests) inherit this~~ | **Implemented + tested 2026-05-16 ✓ (36 unit tests + dev cluster `sha-811a8e9`: admin `/api/jobs?limit=5` 200; missing-job UUID 404 via gate path; 6 sub-endpoints (`/jobs/{id}`, `/audit`, `/citations`, `/workspace`, `/todos`, `/progress`) all 200 against a real job_id).** Scope expanded from 6 → 30 endpoints (same bug class across the full job-read surface: workspace, repo, todos, bulk caches, audit timerange/bulk, chat, snapshot, frozen, progress, version, shell-state, message threads, sources/search, citations stats, memory stats). New `get_visible_jobs` postgres helper (OR-style for non-admins); `get_jobs` kept for the admin AND-style path with optional `scope_project_id`. `list_jobs` uses `require_approved_user` + visibility model: admin sees all (optionally narrowed by `?user_id=` or MCP project scope); non-admin sees `(user_id = caller OR project_id ANY caller's projects)` plus scope intersection. Cross-user `?user_id=` from non-admin → 403 (matches F2 pattern). `get_job` and all 28 other get-by-id endpoints call `require_job_access` before any work. New `mcp_scope_project_id` public accessor in `access.py`. The old inline `_get_mcp_scope` helper deleted (~15 lines). 36 unit tests in `tests/test_job_access.py`. | `orchestrator/main.py` (30 endpoints), `orchestrator/database/postgres.py:528-666`, `orchestrator/security/access.py:78-99` | 1.5d |
| ~~**G2**~~ | ~~Projects read family — list_projects, get_project, /members (read), /datasources~~ | **Implemented + tested 2026-05-16 ✓ (33 unit tests + dev cluster `sha-811a8e9`: admin `/api/projects` 200; missing-project UUID 404 via gate path).** Scope expanded from 4 → 15 endpoints (`/datasources` already done by F3; same bug class across the rest of the project surface). All 9 unscoped reads gated with `require_project_member` (viewer): list_projects, get_project, members, contacts, memory/stats, experts, experts/{name}, repositories, jobs. 6 unscoped mutations gated by role: contacts POST/DELETE (editor), repositories POST/PATCH/DELETE (owner), jobs POST (editor). `list_projects` semantics: admin sees full list (or `?user_id=` cross-user); non-admin auto-restricted to `get_projects_for_user(caller)`; cross-user `?user_id=` from non-admin → 403; MCP `project:<uuid>` scope filters the result post-fetch. `get_project` gates BEFORE `_ensure_project_cloud_resources` side effects fire (test asserts side-effect not invoked when gate blocks). 33 unit tests in `tests/test_project_access.py`. | `orchestrator/main.py` (15 endpoints across the `/api/projects*` surface) | 1d |
| ~~**G3**~~ | ~~Sources, citations, threads orphan bug, sudo requests list/get~~ | **Implemented + tested 2026-05-16 ✓ (33 unit tests + dev cluster `sha-811a8e9`: admin `/api/sources?limit=5` 200 (no-job admin-only branch); admin `/api/sudo/requests?limit=5` 200; missing-sudo UUID 404 via gate path).** **Threads (orphan fix):** all 9 endpoint sites + 1 private helper now use `require_thread_owner` (new helper). The pre-G3 inline check `if thread.get("user_id") and X != Y` silently allowed orphan threads (user_id IS NULL) to be enumerated by UUID; replaced with fail-closed `str(thread.get("user_id") or "") != str(user["id"])` plus admin bypass + MCP-project-scope refusal (threads have no project). **Sources:** `list_sources?job_id=` gated by `require_job_access`; `list_sources` without job_id is admin-only (cross-job source enumeration would need a vector_db ⇆ postgres_db join we deliberately don't do). `get_source_detail` visible if the caller can access at least one linked job via `job_sources`. **Sudo:** `list_sudo_requests?job_id=` gated by `require_job_access`; without job_id admins see all, non-admins receive only requests whose underlying job they can access (post-fetch filter). `get_sudo_request` checks job access (admin bypass unless MCP project-scoped). **Citations get-by-id:** `/api/jobs/{id}/citations` + stats already gated by G1's `require_job_access`; the standalone `GET /api/citations/{citation_id}` retains its current behavior (followup in P3). New helpers in `access.py`: `require_thread_owner`, `user_can_access_any_job`. 33 unit tests in `tests/test_thread_access.py` (12) + `tests/test_sources_sudo_access.py` (21). | `orchestrator/main.py` (sources, sudo, 10 thread sites), `orchestrator/security/access.py` (2 new helpers) | 1d |
| ~~**G4**~~ | ~~Agents — full fleet visible incl. pod IPs / hostnames~~ | **Implemented + tested 2026-05-16 ✓ (13 unit tests + dev cluster `sha-811a8e9`: admin `/api/agents?limit=5` 200; admin `/api/me/active-jobs` 200; projection shape verified — response keys = `[assigned_agent_id, branch_name, config_name, created_at, description, id, merge_status, parent_job_id, priority, project_id, repo_name, snapshot_status, status, user_id]` — confirmed no `pod_ip`, no `hostname`).** 4 agents endpoints (`GET /api/agents`, `GET /api/agents/{id}`, `GET /api/agents/{id}/system-info`, `DELETE /api/agents/{id}`) now gated by `_require_admin`. The agent self-registration endpoints (`POST /api/agents/register`, `POST /api/agents/{id}/heartbeat`) deliberately remain ungated — they belong to Track B (agent ↔ orchestrator auth). New endpoint `GET /api/me/active-jobs` gives non-admins a safe per-user projection: caller's visible jobs (via G1 OR-clause) filtered to in-flight statuses (created / processing / paused / pending_review). No pod IPs, hostnames, or agent metadata (the underlying `get_visible_jobs` SELECT already excludes them). Respects MCP `project:<uuid>` scope. **Resolves Open Q #7** ("Agents API for non-admin users"): admin-only + stripped projection. 13 unit tests in `tests/test_agents_admin_access.py`. | `orchestrator/main.py` (4 admin gates + 1 new endpoint) | 0.5d |
| ~~**G5**~~ | ~~Stats — system-wide aggregates returned to anyone~~ | **Implemented + tested 2026-05-16 ✓ (17 unit tests + dev cluster `sha-811a8e9`: admin `/api/stats/jobs` returned `total_jobs: 39 / completed: 13 / failed: 7 / cancelled: 10`; admin `/api/stats/daily?days=7` returned per-date rows; admin `/api/stats/stuck` returned `[]`; admin `/api/stats/agents` returned status counts `{total: 8, ready: 2, offline: 6}`; admin `/api/snapshots/stats` returned `{total_snapshots: 3, total_size_bytes: 153 MB}`).** 5 stats endpoints scoped: **visibility-scoped (admin sees all, non-admin OR-clause)**: `GET /api/stats/jobs`, `GET /api/stats/daily`, `GET /api/stats/stuck`. **Admin-only (infra metrics)**: `GET /api/stats/agents` (fleet status counts), `GET /api/snapshots/stats` (storage totals). Postgres methods (`get_job_statistics`, `get_daily_statistics`, `detect_stuck_jobs`) extended with optional `owner_user_id` / `visible_project_ids` / `scope_project_id` kwargs that compose into the same OR-clause as G1's `get_visible_jobs` — implemented via a new private `_visibility_clause` SQL-fragment builder (kept inside `postgres.py` for column-name proximity to the SELECTs). Endpoints dispatch through a `_visibility_kwargs_for_stats(user)` helper in main.py. MCP `project:<uuid>` scope still narrows (admins included). No separate `/api/admin/stats/*` endpoints added — the same routes serve both admin and non-admin views since the underlying methods now take visibility args. **Resolves Open Q #8** ("Stats endpoints — scope-to-user view or admin-only?"): scoped + admin-only for infra-level metrics. 17 unit tests in `tests/test_stats_access.py` (including direct `_visibility_clause` coverage). | `orchestrator/main.py` (5 endpoints + 1 helper), `orchestrator/database/postgres.py:1564-1700` | 0.5d |

**P2 total: ~4.5d est. — DONE 2026-05-16.** All bundles (G1-G5) shipped same day with 161 new unit tests. See P2 rows above for per-bundle details.

### P3 — Cleanup

| ID | Finding | Fix | Est. |
|---|---|---|---|
| ~~**C1**~~ | ~~Cockpit "mine" filter is UI-only; no route guards for ownership-sensitive screens~~ | **Implemented 2026-05-16 ✓.** Removed the `'mine'` `StatusFilter` branch from `job-list.component.ts` (filter/UI/computed all gone) and dropped the `jobs.filter.mine` keys from `en.json` / `de-DE.json`. Added `projectAccessGuard` (`core/guards/project-access.guard.ts`) wired onto `/projects/:id` — pre-fetches via `getProject()`, redirects to `/projects` with a `projects.error.noAccess` toast when null (covers 403 and 404 alike). Sidebar admin nav (`/admin/llm`, `/admin/users`) already wrapped with `@if (currentUser()?.is_admin)`. `/debug` audit confirmed: Timeline + RequestService + MemoryPanel + GraphService all hit gated endpoints (`/api/jobs/{job_id}/llm-requests`, `/api/jobs/{job_id}/memory/stats`, `/api/jobs/{job_id}/memories`, `/api/requests/{doc_id}`). IDE happy-path live verification skipped — needs a real running job. **Bonus security fix:** discovered `GET /api/requests/{doc_id}` was completely unauthenticated, allowing anonymous fetch of any LLM request (full prompts + responses) by MongoDB ObjectId. Now gated via `require_job_access` on the embedded `job_id` (admin-only for legacy docs without `job_id`). | `cockpit/src/app/views/jobs/job-list.component.ts`, `cockpit/src/app/core/guards/project-access.guard.ts`, `cockpit/src/app/app.routes.ts`, `cockpit/src/assets/i18n/{en,de-DE}.json`, `orchestrator/main.py:7830-7860` | 0.5d |
| ~~**C2**~~ | ~~No regression net to catch new unscoped endpoints~~ | **Implemented 2026-05-16 ✓ (2 unit tests).** New `scripts/check_endpoint_auth.py` walks `orchestrator/main.py` with `ast`, classifies every `@app.{get,post,put,patch,delete}("/api/...")` decorator by access gate (resource-bound `require_*` > `user_can_access_*` helpers > `_require_admin` > `require_approved_user`), and supports `# nosec: public <reason>` opt-out comments. Snapshot committed at `docs/security/endpoint_inventory.txt` (232 endpoints; 55 unscoped grandfathered). `tests/test_endpoint_inventory.py` re-runs the walker and (1) diffs against the committed manifest with a unified-diff failure message, (2) asserts the unscoped count does not increase. Auto-picked-up by the existing `pytest tests/ -x -q` CI step — no workflow YAML edit needed. New endpoints either gate themselves, mark themselves public, or fail the build. | `scripts/check_endpoint_auth.py`, `docs/security/endpoint_inventory.txt`, `tests/test_endpoint_inventory.py` | 0.5d |

**P3 total: ~1d est. — DONE 2026-05-16.** Both bundles shipped same day with the bonus `/api/requests/{doc_id}` auth fix that the C2 lint immediately surfaced as a working example of the regression net's value. Track A is now fully done.

### P4 — Unscoped-endpoint backlog (surfaced by C2)

The C2 inventory grandfathered **55 endpoints** with no detectable gate (see `docs/security/endpoint_inventory.txt`). They split into three groups: annotations-only (publicly intentional), Track B (already deferred), and real gating work (~22 endpoints, ~3d). Each bundle below is independently shippable. None is high-severity enough to be a P0 hotfix — the highest-risk one (`/api/requests/{doc_id}` anonymous LLM-prompt disclosure) was already fixed during the C1 audit.

**Progress (2026-05-17):**
- P4a ✓ shipped (55 → 51 unscoped: 3 endpoints annotated public, 1 admin-gated after verification).
- P4c ✓ shipped (51 → 37 unscoped: 14 user-only mutation endpoints gated; 8 reclassified to P4b as agent-shared per the Track-B-blocked criterion).
- P4d ✓ shipped (37 → 29 unscoped: 8 admin-only infra endpoints gated with `_require_admin`).
- P4e ✓ shipped (29 → 24 unscoped: 4 plain `require_approved_user` + 1 `user_can_access_any_job` for citations. Pending-actions also now per-user-filtered at the DB layer, closing a global-counts + sudo-command-leak hole).
- P4f ✓ shipped (24 → 21 unscoped: 3 VM lifecycle endpoints gated via `require_job_access`; DELETE adds inline owner-or-admin check).
- **P4b ✓ shipped (21 → 0 unscoped):** Track B agent ↔ orchestrator boundary. **Layer 2 (app-layer, always on):** new helpers `is_internal_call` / `require_internal` / `require_internal_or_job_access` in `orchestrator/security/access.py`, gating 16 pure-internal endpoints (register, heartbeat, threads + 7 sub-routes, jobs/{id}/{complete,agent-release,subjob-merge,messages/send}, internal/mcp-token-*) and 5 dual-callable endpoints (POST /api/jobs + cancel/pause/resume/approve — hybrid: key bypasses user auth, falls through to `require_job_access` otherwise). Agent's 3 HTTP clients send `X-Internal-Key` from `MCP_INTERNAL_KEY`. **Layer 1 (edge-layer, per-deployment):** Helm chart ships an opt-in Traefik middleware behind `ingress.internalPathBlock.enabled` (default `false`); deployments behind a tunnel that bypasses the ingress (like ours, with Cloudflare Tunnel) configure the equivalent at the tunnel — the cloudflared ConfigMap in `HomeLab/deployments_managed/cloudflare-tunnel/cloudflare-tunnel_configmap.yaml` returns 403 for `/api/(agents|internal)/.*` before the request ever reaches the cluster. 20 new tests in `tests/test_internal_auth.py`.
- **Total today: 55 → 0 unscoped, 55 endpoints actually gated, 6 of 6 P4 bundles shipped. Track A + P4 follow-up both done.** Live-verified Layer 1 (cloudflared edge → 403) + Layer 2 (orchestrator → 401) on the dev cluster.

#### ~~P4a~~ — Annotate public-by-design endpoints (~5 min) — **Implemented 2026-05-17 ✓**

3 endpoints annotated public, 1 reclassified admin after verification. Inventory: 55 → 51 unscoped. Tests + ruff green.

| Endpoint | Action taken |
|---|---|
| `GET /api/health` | Annotated `# nosec: public k8s-liveness-probe` |
| `GET /api/auth/me` | Annotated `# nosec: public auth-bootstrap (Bearer-required, intentionally serves pending-approval users)` |
| `GET /api/system/readiness` | Annotated `# nosec: public auth-bootstrap (Bearer-required, intentionally pre-approval — onboarding first paint)` |
| `GET /api/workspace/status` | **Admin-gated** with `_require_admin`, not annotated. Verification found zero callers (no cockpit/agent/probe references) and the response leaks job UUIDs + filesystem paths + env-var values, so making it permanently public was the wrong call. Now classifies as `admin:_require_admin`. |

#### ~~P4b~~ — Track B agent ↔ orchestrator boundary — **Implemented 2026-05-17 ✓**

**Two-layer defense shipped — but Layer 1 is now per-deployment** after a topology audit found that the dev cluster's Cloudflare Tunnel bypasses Traefik entirely (cloudflared routes `api.superhuman-remote-worker.com` straight to the orchestrator Service, never through the Ingress controller). The framing below reflects what landed in the chart vs. what's deployment-specific.

**Layer 2 — application-layer, always on, shipped in the chart's code.**
New helpers in `orchestrator/security/access.py`:
- `is_internal_call(request)` — bool predicate on the `X-Internal-Key` header
- `require_internal(request)` — raises 401 without a valid key (pure-internal endpoints)
- `require_internal_or_job_access(request, db, job_id)` — hybrid: returns `(None, job)` for internal callers, falls through to `require_job_access` otherwise (dual-callable endpoints)
- Key reads from `MCP_INTERNAL_KEY` env (already wired to orchestrator + MCP + agent pods via the `srw` secret).
- Fail-closed: if `MCP_INTERNAL_KEY` is empty, every internal call fails 401 — a misconfigured cluster breaks loudly instead of letting traffic through.

This is the canonical defense and works for **every** deployment regardless of edge topology.

**Layer 1 — edge-layer, opt-in per deployment.** Three setups:

1. **Tunnel/edge that bypasses the ingress controller (this cluster, with Cloudflare Tunnel).** Configure the path block at the tunnel. For cloudflared, add a path-filtered rule **before** the catch-all in the tunnel ConfigMap:

   ```yaml
   ingress:
     - hostname: api.superhuman-remote-worker.com
       path: ^/api/(agents|internal)(/.*)?$
       service: http_status:403
     - hostname: api.superhuman-remote-worker.com
       service: http://srw-orchestrator.superhuman-remote-worker.svc.cluster.local:8085
   ```

   Live on this cluster via `HomeLab/deployments_managed/cloudflare-tunnel/cloudflare-tunnel_configmap.yaml`. Verified: `POST /api/agents/register` returns 403 from cloudflared, never reaches the orchestrator.

2. **Traefik (or compatible) at the edge.** Set `ingress.internalPathBlock.enabled: true` in your values. The chart renders a Traefik `IPWhiteList` middleware + a high-priority `Ingress` matching `/api/agents/*` and `/api/internal/*` that 403s every external client. The orchestrator Service stays reachable via in-cluster DNS, so agent traffic is unaffected. Default is `false` because most deployments either use a tunnel or want to configure their ingress controller their own way.

3. **Other ingress controllers (nginx-ingress, Istio, Envoy Gateway, etc.).** Translate the same intent: deny `/api/agents/*` and `/api/internal/*` from external clients at your edge. The chart doesn't ship templates for every ingress controller — pull-requests welcome.

**Why per-path mutations stay Layer-2-only.** Paths under `/api/jobs/{id}/{complete,agent-release,subjob-merge,messages/send}` can't be stripped at the edge because the cockpit uses sibling `/api/jobs/{id}/*` paths that must remain reachable. The app-layer `require_internal` covers them.

**Agent client wiring** — added to all three places that talk to the orchestrator:
- `src/api/orchestrator_client.py:connect()` — `httpx.AsyncClient` carries `X-Internal-Key` headers
- `src/tools/orchestrator/jobs.py:_get_client()` — same
- `src/tools/communication/messaging.py:198` — ad-hoc client also carries the header

**Endpoint-by-endpoint:**

| Endpoint | Gate applied | Notes |
|---|---|---|
| `POST /api/agents/register` | `require_internal` | Agent bootstrap. |
| `POST /api/agents/{agent_id}/heartbeat` | `require_internal` | Agent liveness. |
| `POST /api/jobs/{job_id}/complete` | `require_internal` | Agent → orchestrator job-done. Body shadowed `request` renamed → `body`. |
| `POST /api/internal/mcp-token-create` / `verify` | `require_internal` | Replaced the inline manual `X-Internal-Key` check that already existed there. |
| `POST /api/agents/threads` + 7 sub-routes (`PATCH /config`, `GET /lifecycle`, `POST /messages`, `POST /release-agent`, `PUT /status`, `POST /upgrade-to-vm`, `GET /workspace`) | `require_internal` (each) | Confirmed agent-only (zero cockpit callers grep'd). Several had `request: ...Request` body shadows that were renamed → `body`. |
| `PUT /api/jobs/{job_id}/agent-release` | `require_internal` | Agent shutdown path; no cockpit caller. |
| `POST /api/jobs/{job_id}/subjob-merge` | `require_internal` | Agent subjob-completion path. |
| `POST /api/jobs/{job_id}/messages/send` | `require_internal` (via `req` alias) | Agent-only outbound-message path; body kept its historical `request` name with FastAPI Request aliased to `req` to avoid churning 15 ref sites. |
| **Dual-callable:** `POST /api/jobs` | inline: `if not is_internal_call(): require_approved_user + force user_id + require_project_member(editor) if body.project_id` | Cockpit creates + agent delegates. Cockpit path forces `body.user_id = caller.id` (F2 pattern); agent path trusts the body. |
| **Dual-callable:** `PUT /api/jobs/{job_id}/cancel`, `PUT /api/jobs/{job_id}/pause`, `POST /api/jobs/{job_id}/resume`, `POST /api/jobs/{job_id}/approve` | `require_internal_or_job_access` | Cockpit users get normal `require_job_access`; agent gets a bypass. The 4 endpoints also drop the redundant in-body `get_job` lookup (the gate already loaded it). |

**Tests:** new `tests/test_internal_auth.py` — 20 tests covering the helpers (`is_internal_call`, `require_internal`, `require_internal_or_job_access` × 4 scenarios) plus integration on representative endpoints (pure-internal 401 without key; dual-callable internal-key-bypass; dual-callable cross-user 403; create_job force-user-id on user path).

**Inventory:** **21 → 0 unscoped** endpoints. `scripts/check_endpoint_auth.py` learned two new classifications (`internal:require_internal`, `gated:require_internal_or_job_access`) and a new priority slot for them. The committed manifest now shows every endpoint with an explicit gate label.

#### ~~P4c~~ — Job mutation gates (~1.5d → **~0.7d actual**) — **Implemented 2026-05-17 ✓**

14 of the originally-planned 22 endpoints shipped. Implementation/verification surfaced that **8 endpoints are agent-callable today** (the agent's `_get_client()` in `src/tools/orchestrator/jobs.py` and `src/api/orchestrator_client.py` is bare httpx with no auth header) — gating them would break agent tools, so they moved to P4b as Track-B-blocked work.

**Shipped (14 endpoints):**

| Endpoint | Implemented gate | Notes |
|---|---|---|
| `DELETE /api/jobs/{job_id}` | `require_job_access` + inline owner-or-admin role check | Mirrors G3 sudo pattern. Plain project members can't delete; only the job's user, the project owner, or admin. |
| `POST /api/jobs/{job_id}/assign/{agent_id}` | `_require_admin` | Manual dispatch override — admin-only. |
| `POST /api/jobs/{job_id}/promote` | `require_job_access` + force `body.user_id = caller.id` | F2 pattern. Caller can't promote into a project owned by someone else. |
| `POST /api/jobs/{job_id}/upgrade-to-vm` | `require_job_access` | |
| `POST /api/jobs/{job_id}/messages/{thread_id}/reply` | `require_job_access` | Renamed shadowed `request` body param → `body`. |
| `DELETE /api/jobs/{job_id}/snapshot` / `PUT /api/jobs/{job_id}/snapshot/pin` | `require_job_access` | |
| `GET /api/jobs/{job_id}/sources/{source_id}/annotations` / `/tags` | `require_job_access` | |
| `PUT /api/jobs/{job_id}/workspace/{path:path}` | `require_job_access` | Write to job workspace. |
| `GET /api/jobs/{job_id}/logs` | `require_job_access` | |
| `GET/POST/DELETE /api/jobs/{job_id}/ide` | `require_job_access` | All three IDE-session endpoints. POST renamed shadowed `request` body param → `body`. |

**Tests:** new `TestJobMutationGates` class in `tests/test_job_access.py` — 17 tests (12 plain cross-user-403 + delete-cross-user + delete-non-owner-member + assign-non-admin-blocked + assign-admin-passes-gate + promote-forces-caller-user-id). Full job-access suite: **162 passing** (was 145). Inventory: **51 → 37 unscoped**.

**Moved to P4b (agent-shared, blocks on B1):** the following 8 are called by the agent (citations below) and need Track B's auth boundary before they can be gated. P4b's table should incorporate them:

| Endpoint | Agent call site |
|---|---|
| `POST /api/jobs` | `src/api/orchestrator_client.py:862` (delegation child job creation) |
| `PUT /api/jobs/{job_id}/cancel` | `src/tools/orchestrator/jobs.py:370` (`cancel_worker_job` tool) |
| `PUT /api/jobs/{job_id}/pause` | `src/tools/orchestrator/jobs.py:390` (`pause_worker_job` tool) |
| `PUT /api/jobs/{job_id}/agent-release` | `src/api/orchestrator_client.py:755` (agent shutdown) |
| `POST /api/jobs/{job_id}/resume` | `src/api/orchestrator_client.py:664` + `src/tools/orchestrator/jobs.py:345` |
| `POST /api/jobs/{job_id}/approve` | `src/api/orchestrator_client.py:914` + `src/tools/orchestrator/jobs.py:316` |
| `POST /api/jobs/{job_id}/subjob-merge` | `src/api/orchestrator_client.py:713` (subjob completion) |
| `POST /api/jobs/{job_id}/messages/send` | `src/tools/communication/messaging.py:198` (`send_message_to_user` tool) |

#### ~~P4d~~ — Admin-only infra endpoints (~30 min) — **Implemented 2026-05-17 ✓**

All 8 endpoints gated with `_require_admin`; no edge cases. Inventory: 37 → 29 unscoped.

| Endpoint | Gate |
|---|---|
| `GET /api/tables` / `GET /api/tables/{name}` / `GET /api/tables/{name}/schema` | `_require_admin` (raw postgres rows) |
| `GET /api/sudo/rules` / `POST /api/sudo/rules` / `DELETE /api/sudo/rules/{rule_id}` | `_require_admin` (global pattern rules) |
| `POST /api/experts/reload` | `_require_admin` (reloads YAML from disk) |
| `GET /api/vms` | `_require_admin` (lists all VMs cross-user; per-job lifecycle endpoints stay in P4f) |

**Tests:** new `TestAdminInfraGates` class in `tests/test_admin_infra_access.py` — 9 tests (8 non-admin-403 + 1 admin-pass-gate). All pass; ruff clean.

#### ~~P4e~~ — Expert + datasource + misc reads (~30 min) — **Implemented 2026-05-17 ✓**

All 5 endpoints gated. Inventory: 29 → 24 unscoped.

| Endpoint | Gate applied |
|---|---|
| `GET /api/experts` / `GET /api/experts/{expert_id}` | `require_approved_user` |
| `POST /api/datasources/ssh-keys/generate` | `require_approved_user` |
| `GET /api/actions/pending` | `require_approved_user` + **per-user filter**. Pre-fix this was anonymous and returned global counts + the most-urgent sudo's command string. `get_pending_action_counts(owner_user_id, visible_project_ids)` now narrows sudo/jobs by `(j.user_id = $1 OR j.project_id = ANY($2))`. Cache is segmented per caller (`__admin__` vs user id) so admin and non-admin views can't leak across slots. |
| `GET /api/citations/{citation_id}` | `user_can_access_any_job` against the citation's `job_id`. Returns **404** (not 403) when the caller can't see the linked job — keeps the probe surface symmetric with "citation missing". |

**Tests:** new `TestPendingActions`, `TestCitationDetail`, `TestExpertsGated`, `TestSshKeyGenerateGated` classes in `tests/test_p4e_misc_access.py` — 11 tests (gate-fires checks + admin-vs-non-admin filter assertions + 404-on-stranger for citation). All pass; ruff clean.

#### ~~P4f~~ — VM lifecycle (~30 min) — **Implemented 2026-05-17 ✓**

All 3 endpoints gated. Inventory: 24 → 21 unscoped.

| Endpoint | Gate applied |
|---|---|
| `POST /api/vms` | `require_job_access(body.job_id)` — body model renamed `request → body` to free the FastAPI Request param; redundant in-body `get_job` lookup removed (the gate already 404s on missing). |
| `GET /api/vms/{job_id}` | `require_job_access` — returns `(_, job)` tuple so the in-body `vm_ctx` lookup reuses the gate's fetch. |
| `DELETE /api/vms/{job_id}` | `require_job_access` + inline owner-or-admin role check (mirrors `DELETE /api/jobs/{job_id}` from P4c). Plain editor-level project membership is not enough. |

**Tests:** `TestVmLifecycleGates` class appended to `tests/test_job_access.py` — 4 tests (cross-user 403 for all three + non-owner-member 403 for delete). All pass; ruff clean.

**P4 total: ~3d est.** (P4a 5min + P4b 10min + P4c 1.5d + P4d 30min + P4e 30min + P4f 30min). Independent bundles; P4c is the only one that needs more than a day. **Severity:** P3-level (cleanup, defense-in-depth) — no individual endpoint is a P0/P1, but the aggregate is meaningful information disclosure and an attack surface that the C2 lint now permanently blocks from growing.

### Track A total

**~10.9 engineering days** (2 hotfixes + 3.4 P1 + 4.5 P2 + 1 P3), reviewable in 12 independent PRs. F1 is the keystone for P1+; H1-H5 land independently as hotfixes. **All bundles shipped 2026-05-16.** P4 (~3d, 6 bundles) is queued as follow-up work surfaced by P3/C2.

### Track B — Agent ↔ orchestrator boundary

Doesn't block current self-hosted single-org use. Pick an option, then schedule. Estimates assume option chosen.

| ID | Finding | Fix | Est. |
|---|---|---|---|
| **B1** | `POST /api/agents/register` and `/heartbeat` have no auth | **Option 1:** NetworkPolicy + bootstrap secret + per-agent token at registration. Closes external-host attack | 1d |
| **B2** | Credentials shipped in plain JSON at dispatch; no proof receiving agent is the intended one | **Option 2:** Per-job dispatch tokens; agent must ACK with token to receive credentials. Builds on B1 | 2d |
| **B3** | (Optional eventual destination) | **Option 3:** mTLS between agents and orchestrator | 3-5d |
| **B4** | Chromium profile (`/tmp/agent-chromium-cdp-profile`) persists across jobs on the same agent | Per-job profile dir + cleanup hook in workspace teardown | 0.5d |
| **B5** | Dispatch payload travels in plaintext over HTTP between orchestrator and agent | Encrypt credentials in the dispatch envelope; decrypt only after job ACK | 0.5d |

**Track B total: 4-9d depending on chosen option.** B5 sudo approval scoping is already handled by H4 in Track A.

### Phase 2 — Multi-tenancy & beyond

Only when ambition demands it.

| ID | Finding | Trigger | Est. |
|---|---|---|---|
| **2.1** | No `organizations` layer above projects | Multi-tenant ambition | 3-5d |
| **2.2** | Cloud storage uses one shared service account across all users — largest single blast radius for hosted product | Hosted/subscription path | 1-2 weeks |
| **2.3** | MongoDB `llm_requests` and `agent_audit` have no TTL — sensitive prompts persist indefinitely | Hygiene / compliance | 0.5d |

### Suggested timeline

A pragmatic landing order:

1. **This week:** H1, H2, H3, H4, H5 as five separate small PRs (all parallelizable, ~2d total).
2. **Next week:** F1 (the foundation). Once it merges, F2-F7 can fan out across whoever is available (~3.5d).
3. **Following sprint:** G1-G5 in parallel (~4.5d).
4. **Wrap-up:** C1, C2 (~1d). Track A done.
5. **Decision point:** Track B option, or formally defer it with a date tied to hosted-product readiness.
6. **Phase 2:** when organizations or hosted-product work actually starts.

## Phase 2 sketch (historical — superseded by M1.B-E + M2 above)

> The substance of this section was re-rolled into the new milestone structure: the **organizations layer** is now [M2.A](#m2a--organizations-schema-35-days), and the **cross-cutting infrastructure isolation** items are now [M1.B](#m1b--cross-job-infrastructure-hygiene-quick-wins-1-day-total) (quick-win hygiene) and [M1.C](#m1c--cloud-storage-per-user-oauth-12-weeks) (cloud storage). Kept here as a historical pointer.

Phase 2 originally had two distinct tracks: the **organizations layer** (a schema-and-helper change that's cheap because Phase 1 centralized visibility) and **cross-cutting infrastructure isolation** (concrete shared-resource fixes that the API gates can't solve).

### Organizations layer (cheap because Phase 1 centralized visibility)

- New table `organizations(id, name, created_at, ...)`.
- `projects.organization_id` FK, `NOT NULL` after backfill.
- New table `organization_members(org_id, user_id, role)`.
- `user_visible_project_ids(user)` augments to include projects in orgs the user is a member of.
- Keycloak: groups in the existing realm; realm-per-tenant only if hard identity isolation is required.

This stays cheap because **Phase 1 centralized visibility in `access.py`** — adding org membership is a single change to one helper.

### Cross-cutting infrastructure isolation (the real remaining work)

The API gates close the application-layer leaks. The following still share state across users in ways the API can't fix:

| # | Gap | Estimated effort | Notes |
|---|---|---|---|
| 1 | **Cloud storage per-user OAuth.** Replace the single OpenCloud/Nextcloud service account with per-user OAuth tokens. Largest single blast-radius issue. | 1-2 weeks | Touches `src/services/cloud_sync/*`, OpenCloud OIDC, token storage. Hosted-product blocker. |
| 2 | **Chromium profile per-job, not per-agent.** Use `--user-data-dir=/tmp/agent-chromium-cdp-profile-{job_id}` (or a tmpfs mount per job) and tear down at job end. | ~2h | One-line change in `src/tools/research/browser.py:101` plus a teardown hook. |
| 3 | **MongoDB TTL on `llm_requests` and `agent_audit`.** Add a TTL index (90 days? 1 year?) so old prompts/responses age out automatically. | ~1h | Config-only; pick a retention window first. |
| 4 | **Audit log of cross-user 403s.** Emit a structured log line (or a Mongo `security_audit` entry) when any gate raises 403. Enables forensics and rate-based alerting. | ~3h | Centralize in the access helpers so it's one log per failed gate. |

### Test gap (blocking proof, not code)

| | Gap | Effort |
|---|---|---|
| 5 | **Keycloak self-registration fixes** — wire SMTP for `VERIFY_EMAIL` (or skip it for dev) and add the realm-level `user` role to `default-roles-srw` so registrants land approved by default. Without this, we can't provision real non-admin test users for E2E cross-user verification of everything Track A + P4 + Track B shipped. | ~2h | Two Keycloak realm-config edits. |

---

## Open questions

1. ~~**404 vs 403** for "you don't own this resource"~~ — **Decided in practice 2026-05-17:** mixed policy reflecting probe-resistance value. Read endpoints that could leak existence (e.g., `GET /api/citations/{id}`) return **404** when the caller can't see the linked job. Action endpoints with explicit ownership checks (`require_job_access`, `require_*`) return **404 if the resource doesn't exist, 403 if it exists but the caller lacks access** — the existence signal is acceptable on action paths because the caller already has a UUID they likely got from a legitimate channel. Centralized in `access.py` helpers.
2. ~~**Datasource credentials in the response**~~ — **Decided 2026-05-16: strip always.** No `?include_credentials=true` flag, no separate `/credentials` endpoint. The orchestrator never returns credentials over REST; the agent reads them via internal dispatch. The cockpit edit form uses a "leave blank to keep existing" UX and the orchestrator preserves stored credentials when `body.credentials` is null/empty. Industry-standard pattern (matches Stripe, GitHub, GCP secrets).
3. ~~**`scope='all'`**~~ — **Decided 2026-05-16: admin-equivalent for the user, not a global override.** A non-admin holding an `'all'` token still sees only their own data — the `'all'` semantic doesn't grant admin powers. Implemented as a no-op at the access.py layer: admin status comes from the realm role, not from the scope. See `_scope_permits_project` / `_scope_project_id` in `security/access.py`.
4. ~~**Track B scope**~~ — **Decided + implemented 2026-05-17 (P4b):** option 1 (shared bootstrap secret) at the app layer (`require_internal` / `require_internal_or_job_access` checking `X-Internal-Key`), plus optional edge-layer path strip (Helm toggle for Traefik-edge users, cloudflared path rule for tunnel-edge users). Defers per-agent tokens (option 2) and mTLS (option 3) to hosted-product readiness; they're additive on top of the current design.
5. ~~**Sudo approval**~~ — **Decided + implemented 2026-05-16 (H4):** project owner OR admin. Job owners cannot self-approve their own sudo requests — that would defeat the gate. Orphan requests (no job or no project) are admin-only. Implemented in `require_sudo_request_authority` at `orchestrator/security/access.py:451`.
6. ~~**Builder session `active_job_id` / `active_project_id`**~~ — **Decided 2026-05-16: fail closed (403).** Implemented in F2: `send_builder_message` now calls `require_job_access` / `require_project_member` for each non-null active_* before constructing the system prompt. The fields end up in the LLM prompt and steer inspection tools, so silently scoping was rejected as a data-leak surface.
7. ~~**Agents API for non-admin users**~~ — **Decided 2026-05-16 (G4): admin-only + stripped per-user projection.** The four `/api/agents*` read/delete endpoints require `_require_admin`. New `GET /api/me/active-jobs` returns the caller's in-flight jobs (no pod IPs / hostnames / agent metadata) so non-admin UIs can show "what's running for me."
8. ~~**Stats endpoints**~~ — **Decided 2026-05-16 (G5): scope-to-user for job-derived stats; admin-only for infra.** `GET /api/stats/jobs`, `/daily`, `/stuck` use the G1 visibility OR-clause (admin sees all, non-admin sees own + project-member). `GET /api/stats/agents` and `GET /api/snapshots/stats` admin-only (infra metrics with no per-user shape). No separate `/api/admin/stats/*` routes — same endpoints, dispatched server-side via `_visibility_kwargs_for_stats(user)`.
9. **Sources metadata sharing across users** — accept that user A's source title/description is visible to user B when they ingest the same content (current behavior, due to dedupe by `content_hash`)? Recommend yes, document it. *Status: still open — low-priority, accept-as-designed candidate.*
10. **Cloud storage per-user OAuth** — Phase 2 or later? *Still open as a scheduling question;* the design is enumerated under [Phase 2 sketch §Cross-cutting infrastructure isolation](#phase-2-sketch-deferred) (~1-2 weeks). It's the largest blast-radius issue but also the biggest refactor; gates Phase 2 / hosted-product readiness.
11. ~~**Orphan handling**~~ — **Decided 2026-05-16 (G3): admin-only by default.** Thread `require_thread_owner` fails closed for `user_id IS NULL` (only admins pass). Source `get_source_detail` fails closed for sources with zero linked jobs (only admins pass). No "orphans" project — leave for an explicit cleanup tool if needed.
12. **MongoDB retention** — add a TTL on `llm_requests` and `agent_audit`? How long? *Still open;* listed as Phase 2 cross-cutting gap #3 (~1h once retention window is picked).

---

## Out of scope (this doc, both milestones)

What this doc covers: **data isolation and abuse prevention** for individual subscribers (M1) and organizations (M2). Everything else is deliberately someone else's problem:

- **Payments / billing / metering / Stripe integration.** Subscription product surface, not isolation.
- **ToS, AUP, cookie banners, privacy-policy text.** Legal, not engineering.
- **GDPR DPA, SOC2, ISO27001 paperwork.** The engineering primitives (user self-deletion, data export, retention, audit log) are in M1.E + M1.B; the paperwork around them is not.
- **Marketing / pricing pages / signup funnel UI.** Cockpit's `/auth/login` flow is what we ship.
- **Customer support workflows / "view as <specific user>" impersonation.** Architected for via `X-Admin-View-As: user:<uuid>` ([view-as design § Future extensions](features/admin_view_as_user.md#future-extensions-post-v1)), but actual implementation is post-M1.
- **Per-resource fine-grained ACLs** ("share this specific thread with user X"). Project membership is the share unit — anything finer is a product feature, not an isolation gap.
- **Per-user OAuth for non-cloud-storage services** (BYO LLM keys, BYO datasource OAuth). Existing patterns work; not a SaaS-readiness blocker. M1.C is specifically the OpenCloud/Nextcloud service-account replacement.
- **Full mTLS between agents and orchestrator** (Track B option 3). The current Layer 2 shared-secret is sufficient until a real cluster-internal-adversary threat model emerges.
- **Replacing the Keycloak realm-roles approval flow with an in-app one.** Current flow (admin assigns the `user` role) is fine; the broken-self-reg fix is M1.B #1.

---

## Next steps

**M1.A (API isolation) is done.** Everything else for M1 is the work this doc tracks. Concrete ordered plan:

1. **M1.B #1 — Keycloak self-registration (~2h).** Smallest fix, biggest unblock. Wire SMTP for `VERIFY_EMAIL` (or skip-verify in dev) + add `user` to `default-roles-srw`. Every later step benefits from being able to provision a real second user.
2. **M1.B #2–#5 — remaining quick-win hygiene (~5h).** Chromium per-job, MongoDB TTL, 403 audit log, view-as PR 4. Independent; parallelizable.
3. **M1.C — Cloud storage per-user OAuth (~1–2 weeks).** The actual hosted-product blocker. Don't open signups beyond invited beta users until this lands.
4. **M1.D — Abuse prevention designs + implementations (~6–9d).** Rate limiting, K8s ResourceQuota/LimitRange per pod, workspace egress NetworkPolicy. Three independent designs, parallelizable; writing the designs is the bottleneck.
5. **M1.E — User account lifecycle (~3–5d).** Self-deletion + data export.
6. **M1 readiness gate clears** — open signups to strangers.
7. **M2 only when demand is real** — organizations layer, per-org primitives, realm strategy.

**Quickest credibility moves (today):** M1.B #1 + #2 + #3 + #4 + #5 are a single short sprint (~1 day total). Doing them now closes 5 of the 10 remaining-gap rows in [Where we are now](#where-we-are-now-2026-05-18) and unblocks live cross-user testing for everything else.

**The big one:** M1.C (cloud storage per-user OAuth). Until that lands, every user-file read is one bug away from cross-tenant exposure. Plan it as the next initiative after M1.B.
