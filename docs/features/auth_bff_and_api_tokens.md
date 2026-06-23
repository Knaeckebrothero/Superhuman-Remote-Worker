---
tags:
  - feature
  - auth
  - security
  - orchestrator
  - cockpit
  - architecture
aliases:
  - cookie BFF
  - session auth
  - API tokens
  - PAT
  - personal access tokens
related:
  - "[[headless_persistent_sessions]]"
  - "[[sso_and_cloud_storage]]"
  - "[[mcp_oauth_bridge]]"
  - "[[mcp]]"
  - "[[orchestrator_ha_scaling]]"
---

# Auth: Cookie BFF + Consolidated API Tokens

> Replace the SPA-Bearer-token model with a server-side session cookie issued by the orchestrator (BFF pattern), so EventSource auth works natively, tokens leave JavaScript memory, and XSS can't lift a session. Simultaneously fold the existing `mcp_tokens` table into a single `auth_tokens` system that issues Personal Access Tokens (PATs) for automation tools like n8n alongside the MCP tokens for Claude Code.

**Status:** PR 1 (orchestrator-side BFF foundation) shipped 2026-05-13. PR 2 (cockpit cutover) shipped 2026-05-14. PR 3 (PAT consolidation + UI) shipped 2026-05-14. PR 4 (scope decorators + audit + log redaction) outstanding, optional. The "sessions decoupled from the browser" feature is fully shipped end-to-end — the SSE 401 trigger is fixed, the WS auth hole is closed, and automation tooling (n8n) can authenticate via PATs.
**Triggered by:** §P7.1 of `docs/tests/headless_sessions_smoke.md` — `GET /api/persistent/threads/{id}/stream` returns 401 in the cluster because `EventSource` cannot attach an `Authorization: Bearer` header and there is no cookie on the API subdomain. The cockpit's session UI hangs in "Starting session → Provisioning agent" with an Error badge.

## Decisions locked (2026-05-14)

| # | Decision | Locked value | Notes |
|---|---|---|---|
| 1 | Keycloak client mode | **Confidential** — implemented as a **separate `cockpit-bff` client** alongside the existing public `cockpit` | Realm config + new `KC_CLIENT_SECRET` in Vault/ESO. See "Implementation log" below — the original plan to flip the existing `cockpit` client to confidential was reverted during PR 1 testing because the SPA PKCE flow breaks without a client secret; the BFF runs on its own client so the cockpit's old login path stays intact during cutover and rollback. |
| 2 | Session store | **Postgres** | Reuses existing infrastructure; sub-ms indexed UUID lookup |
| 3 | Pre-auth state | **DB table** (`srw_pre_auth_states`) | No signing-key management |
| 4 | Session lifetimes | **30 min idle / 30 days absolute** | Both env-tunable |
| 5 | Cookie name | **`srw_session`** | Vendor-prefixed |
| 6 | builder-stream `fetch()` interceptor bypass | **Separate issue doc** | See `docs/done/cockpit_builder_stream_fetch_bypasses_auth.md` |
| 7 | PR 3 split | **Don't split** | Consolidation + UI ship together |
| 8 | PAT default expiry | **1 year** | Internal-app threat model |
| 9 | API key UI location | **Separate page** at `cockpit/src/app/views/settings/api-keys/` | Per `automations_v0.md` |
| 9b | Table model | **Consolidate `mcp_tokens` → `auth_tokens`** with `kind` column | Overrides `automations_v0.md`'s Open Decision #1, see §3.6 |
| 10 | Audit logging | **Every authenticated request** | MongoDB graceful-degrade path |

The format reasoning for the API token prefix was also revised: keep `ak_<32-byte-urlsafe>` from `automations_v0.md` rather than introducing `srw_pat_v1_…`. Divergence without strong reason is just churn.

## Implementation log

PR 1 and PR 2 are landed. Two material deviations from the original design above; everything else matches.

### PR 1 — Cookie BFF foundation (2026-05-13)

Landed scope matches §1 + §2 of the design with these adjustments:

1. **Separate `cockpit-bff` confidential client (deviation from Decision #1).**
   The first PR-1 attempt flipped the existing `cockpit` Keycloak client from public to confidential per the literal design. The cockpit SPA still runs the PKCE-without-secret flow against that client, so its token-endpoint exchange immediately broke ("Invalid client credentials"). The user reverted the realm flip; we then added a *separate* `cockpit-bff` client (`access_type=confidential`, `KC_CLIENT_SECRET` from Vault/ESO) and left the original `cockpit` client public. The orchestrator BFF authenticates at the token endpoint as `cockpit-bff`; the SPA's old login path keeps working unchanged. This is also what makes PR 2 a clean cutover — both clients coexist in the realm; rollback is "stop using `cockpit-bff`".
   Realm-import JSON and `kcadm` post-start hook are wired in `helm/templates/services/keycloak.yaml` and the external-KC bootstrap path; the `KC_CLIENT_SECRET` is synced via the same Vault path as other client secrets (`MCP_OIDC_CLIENT_SECRET`, `OPENCLOUD_KEYCLOAK_CLIENT_SECRET`).

2. **`sub` claim missing from KC 24+ access tokens — defensive id_token merge in the validator.**
   Keycloak 24 removed the implicit `sub` mapper from access tokens by default; only the id_token reliably carries `sub` per OIDC. The first BFF callback against `cockpit-bff` 500'd with `KeyError: 'sub'`. Two options: (a) configure a per-client `sub` mapper on `cockpit-bff` so the access token carries the claim, or (b) decode the id_token in the callback and merge `sub`/`email`/`preferred_username`/`name`/`email_verified` into the claim bag before user resolution. We took (b) because it's robust to future KC default changes and avoids per-client realm configuration: see `_merge_identity_claims` in `orchestrator/security/auth.py`, called from the BFF callback and the cookie validator path.

Other PR-1 work that landed as designed: `0009_srw_sessions.sql` (with the dormant pre-KC `sessions` table dropped in the same migration), `orchestrator/auth/bff.py`, `orchestrator/security/csrf.py`, `orchestrator/security/kc_client.py`, the cookie-first validator dispatch in `security/auth.py` (preserving the Bearer + MCP-internal fallbacks), the WS auth fix on `/ws/persistent/{thread_id}`, the session-cleanup background task, and Helm env wiring (`KEYCLOAK_CLIENT_ID=cockpit-bff`, `KC_CLIENT_SECRET` from `srw-secrets`, six `SRW_*` config knobs).

### PR 2 — Cockpit cutover (2026-05-14)

Landed scope matches the cockpit half of §1 with no deviations. Specifically:

- **New `core/services/session.service.ts`** — small signal-based surface (`login` / `logout` / `forceRefresh` / `authenticated`). Replaces `keycloak.service.ts`, which is deleted.
- **Rewritten `core/interceptors/auth.interceptor.ts`** — every request to the API origin now sends `withCredentials: true`; non-safe methods carry `X-CSRF: 1`; 401 triggers a single redirect to `/auth/login?return_to=…` (suppressed only for `/auth/logout` to avoid racing the KC RP-initiated logout).
- **`app.config.ts` APP_INITIALIZER** — drops `keycloak-js` init, replaces with a `GET /auth/me` bootstrap that pre-populates `UserService.currentUser` before the first component renders. On 401 the initializer catch is a no-op; the interceptor already kicked off the BFF login redirect, the bootstrap promise resolves so Angular can finish booting before the page unloads to KC.
- **Guards** (`auth.guard.ts`, `admin.guard.ts`) — drop `KeycloakService`; check `userService.currentUser()`; await one effect tick + 1.5s fallback before falling through to `session.login()`.
- **SSE consumers** — `notification.service.ts` and `sudo.service.ts` `EventSource` constructors now pass `{withCredentials: true}` so the cross-origin SSE handshake carries the cookie. `persistent-chat.service.ts` already had it (the original PR 1 acceptance probed it manually); the only change was a comment correction.
- **`project-list.component.ts`** — `keycloak.forceRefreshToken()` (used after project create so the new `project-{id}` group claim propagates) replaced by `session.forceRefresh()`, which POSTs `/auth/refresh` and writes new tokens into the server-side session row.
- **Drop `keycloak-js`** — removed from `package.json`/`package-lock.json` (~150 KB off the bundle), `keycloakUrl`/`keycloakRealm`/`keycloakClientId` removed from `core/environment.ts`, `assets/silent-check-sso.html` deleted. The Helm cockpit-env ConfigMap (`helm/templates/cockpit/deployment.yaml`) no longer writes those env vars into the served `env.js`. Real-user browsers that cached the previous `env.js` (`max-age=14400`) will still have stale `window.env.keycloakUrl` until cache expires; the SPA no longer reads it, so this is cosmetic.
- **Small orchestrator follow-up that shipped with PR 2**: `/auth/login` now forwards a sanitised `ui_locales` query param to Keycloak so the cockpit's language preference propagates to the KC login form (matches the previous keycloak-js behavior).

### PR 3 — API token consolidation + UI (2026-05-14)

Landed scope matches §3 of the design with one runtime deviation surfaced during testing (see "PR 3 hotfix" below). Specifically:

- **`0010_auth_tokens_consolidation.sql`** — drops the dormant pre-Keycloak `auth_tokens` table to free the name, renames `mcp_tokens` → `auth_tokens`, adds `kind` (`CHECK IN ('mcp','api')`), `scopes TEXT[]`, `last_four CHAR(4)`, `last_used_ip INET`, `superseded_by UUID` columns plus two new partial indexes. Backfills `kind='mcp'` for the 5 existing rows via a transient `DEFAULT 'mcp'`, then drops the default so new inserts must specify. Migration ran in 14 ms; no row rewrite.
- **DB-layer split** — existing public method names (`create_mcp_token`, `list_mcp_tokens`, `revoke_mcp_token`, `get_mcp_token_by_hash`, `update_mcp_token_last_used`, `cleanup_expired_mcp_tokens`) are preserved so init-seeding and the MCP server's `TokenVerifier` keep working unchanged; each now filters/writes `kind='mcp'`. New kind=`api` helpers added: `create_api_key`, `list_api_keys`, `revoke_api_key`, `rotate_api_key`, cross-kind `get_auth_token_by_hash`, and `touch_auth_token(token_id, ip)`. The pre-Keycloak verification/password-reset methods (`create_auth_token`, `get_auth_token`, `mark_auth_token_used`, `delete_auth_tokens_by_email`, `delete_expired_auth_tokens`, `get_latest_auth_token_time`) had zero callers and were deleted alongside the table drop.
- **Bearer dispatcher in `security/auth.py`** — `get_current_user` now shape-routes the Authorization header: `ak_*` → `_resolve_pat` (new), `srw_*` → `_resolve_legacy_mcp_token` (new), three-dot JWTs → existing `validate_token` flow, anything else → 401 "Unrecognized token format". Both new resolvers `sha256` the token, look up the row cross-kind, then enforce `row["kind"]` matches the prefix (so a hash collision across kinds cannot impersonate). Each fires a non-blocking `touch_auth_token(token_id, client_ip)` so the UI can show last-used + last-used-IP per row. Client IP is taken from `X-Forwarded-For` leftmost entry (single hop, matching the nginx ingress) with `request.client.host` as fallback. Both resolvers force `is_approved=True` — a token can only be issued by an already-approved user, and a later role revocation should revoke the token, not silently leave it usable.
- **`/api/api-keys` endpoint family** — `POST` (create with name + scopes + expiry; admin scope gated on `is_admin`), `GET` (list, no plaintext), `DELETE /{id}` (soft revoke), `POST /{id}/rotate` (issue successor, set `old.superseded_by = new.id`, both stay valid for 24h grace; cleanup loop revokes the old). Default expiry per design = 1 year; allowed `[30, 90, 365, null-with-warning]`. PAT format = `ak_<43-char urlsafe>` = `f"ak_{secrets.token_urlsafe(32)}"`. Validator stays in permissive mode for scope checks until PR 4 wires per-endpoint `@require_scope` decorators.
- **Cockpit `ApiKeysService`** + standalone page at `/settings/api-keys` (route guarded by `authGuard`). Page lifts the existing settings-page table aesthetic: sorted active-keys list with scope chips, `prefix…last_four` hint, stale-row highlight (90+ days unused), rotate/revoke per row, inline create form, one-shot reveal banner with mandatory acknowledge checkbox. A link card from the existing settings page sends users over. Full i18n in `en` and `de-DE`.
- **Cleanup loop now covers two responsibilities** — `cleanup_expired_mcp_tokens` (kept name for back-compat) now also revokes any `kind='api'` row whose `superseded_by` successor is older than 24 hours, implementing the rotation grace window without a separate scheduled job.

### PR 3 hotfix (2026-05-14)

Two bugs surfaced during PR-3 acceptance testing and were fixed in a follow-up commit:

1. **PAT resolver passed an asyncpg native UUID to `db.get_user`.** asyncpg returns `pgproto.UUID` instances for UUID columns; `db.get_user(...)` normalizes its argument by constructing `uuid.UUID(arg)`, which calls `arg.replace('urn:', '')` — fine for `str`, fatal for an already-typed UUID (`AttributeError: 'asyncpg.pgproto.pgproto.UUID' object has no attribute 'replace'`). PAT-authenticated `/api/auth/me` 500'd; PAT-authenticated `/api/jobs?limit=2` happened to work because the jobs endpoint serializes its own response. Fix: `str(row["user_id"])` + `str(row["id"])` in both `_resolve_pat` and `_resolve_legacy_mcp_token` before passing into DB helpers. No-touch on the existing JWT/cookie paths because those resolve user_id from claims (already a string).

2. **Latent PR-1 regression: CSRF middleware was blocking every agent-pod → orchestrator-pod write.** The CSRF middleware enforced `X-CSRF: 1` on all non-safe methods, with bypasses for Bearer-auth, `X-Internal-Key`, and a path allowlist. Agent traffic (`POST /api/agents/register`, `POST /api/jobs/{id}/complete`, etc.) carries none of those — agent endpoints are documented as "no auth, agent-facing" because the trust boundary is the cluster network, and the agent client doesn't set any of the bypass markers. Net effect since PR 1 deployed 2026-05-13: every new-agent registration and every job completion call 403'd silently; only stale already-registered agents kept the cluster running. Discovered in PR-3 testing when log triage for the `/api/auth/me` 500 caught the parallel stream of `CSRF rejection: csrf:missing-header` warnings tied to `POST /api/agents/register 403`. Fix in `orchestrator/security/csrf.py`: short-circuit before any header check if the request has no `srw_session` cookie. The cookie *is* the CSRF vector — without it, an attacker has no browser-mediated session to forge against. The header-based exemptions for Bearer and `X-Internal-Key` are kept as defense-in-depth for hybrid requests that carry both a cookie and a Bearer (we trust the Bearer in that case). After the hotfix, agent heartbeats and registrations went from 100% 403 to 100% 200 within the new pod's first minute.

### PR-2 acceptance run (2026-05-14, dev cluster `sha-e933414`)

All 10 acceptance probes passed end-to-end via Playwright:

1. Fresh visit: cockpit boots → `/auth/me` 401 → BFF login redirect → KC login form → `/auth/callback` → cookie set → `/sessions` rendered.
2. `srw_session` cookie is HttpOnly (`document.cookie` returns `""` while authenticated).
3. `/api/auth/me` 200 with full user payload via cookie.
4. `/api/users`, `/api/models`, `/api/system/readiness`, `/api/settings/preferences` — all 200 via cookie.
5. `GET /api/notifications/events` — 200 `text/event-stream; charset=utf-8`, body starts `: keepalive\n\n`. (Previously 401 — the trigger for this whole refactor.)
6. `GET /api/sudo/events` — 200 `text/event-stream`. (Same.)
7. `WebSocket(wss://api/ws/persistent/{id})` — handshake reaches `readyState=1` with cookie auto-attached; closes the previous zero-auth hole.
8. CSRF: POST without `X-CSRF` → 403 `CSRF check failed`; POST with `X-CSRF: 1` → 200.
9. Logout: POST `/auth/logout` → 200 with `kc_logout_url` (containing `id_token_hint`); subsequent `/auth/me` → 401.
10. Re-login: navigate guarded route → 401 → `/auth/login` (PKCE, `client_id=cockpit-bff`, `ui_locales=en` honored) → KC form → callback → `/sessions` with fresh cookie.

### PR-3 acceptance run (2026-05-14, dev cluster `sha-603ed59` after hotfix)

All 12 PR-3 probes passed end-to-end via Playwright + raw fetch from the cockpit page:

1. Migration `0010` applied cleanly (14 ms); 5 existing rows backfilled `kind='mcp'`; new columns + indexes present.
2. `POST /api/api-keys` with `[jobs:read, chat:read]` + 30d expiry → 200; plaintext `ak_xX2AydhHs…Bz6k` returned exactly once.
3. `GET /api/api-keys` → 200, lists the new row, never includes a `token` field.
4. Bearer `ak_…` on `/api/jobs?limit=2` → 200 (PAT dispatcher resolves to user).
5. Bearer `ak_…` on `/api/auth/me` → 200 after the UUID hotfix (was 500 with `UUID.replace` traceback before).
6. Bearer bogus `ak_<garbage>` → 401 "Invalid token".
7. Bearer malformed (no recognized prefix) → 401 "Unrecognized token format" (proves the new dispatcher's else-branch).
8. `POST /api/api-keys/{id}/rotate` via cookie → 200; list now shows old row with `superseded_by = new.id`, new row with `superseded_by = null`.
9. Rotated token works as Bearer on `/api/auth/me` → 200.
10. `DELETE /api/api-keys/{id}` → 200; revoked-token Bearer → 401 "Invalid token".
11. UI at `/settings/api-keys` renders the superseded row with the "Rotating" badge, hides the Rotate button while keeping Revoke, displays the `prefix…last_four` hint, and surfaces stale-row warning logic.
12. Agent heartbeats (`POST /api/agents/{id}/heartbeat`) returned to 200 every 60s after the CSRF hotfix — zero CSRF rejections in the new pod's first 17 minutes (vs. constant `csrf:missing-header` 403 every minute pre-hotfix).

### What is NOT yet shipped

- **PR 4** — scope-enforcement decorators on remaining endpoints (lock down the transitional permissive mode), `auth_audit` MongoDB rows per authenticated request, log redaction filter for plaintext token leaks. Optional post-v1.
- **`builder-stream.service.ts:136` raw `fetch()` bypass** — out of scope per Decision #6, tracked separately in `docs/done/cockpit_builder_stream_fetch_bypasses_auth.md`. The builder was already broken pre-PR-2 (no auth attached) and PR 2 neither regresses nor fixes it. Now that PAT-Bearer works orchestrator-side, a one-line `Authorization: Bearer …` addition would also work; cookie credentials (with `credentials: 'include'` + `X-CSRF: 1`) would be the cleaner fix.

## Motivation

### The proximate trigger

The persistent-chat WebSocket→SSE migration landed in `166d54e` (`Migrate persistent chat service from WebSocket to Server-Sent Events`). It works in localhost dev where the orchestrator and cockpit share an origin, but the cluster splits them across subdomains: cockpit on `superhuman-remote-worker.com`, orchestrator on `api.superhuman-remote-worker.com`. The cockpit holds the Keycloak access token in JS memory (`keycloak.service.ts:90-99`); a normal `fetch()` call gets the token from `authInterceptor` and adds `Authorization: Bearer …`; but `EventSource` is restricted to a URL — no headers — and `{withCredentials: true}` only carries cookies, of which there are none on the API subdomain. The orchestrator's `require_approved_user` (`orchestrator/security/auth.py:233`) requires the header, so the SSE handshake 401s and the UI is wedged.

Three SSE consumers hit the same gap, in increasing visibility:
- `notification.service.ts:90` (`/notifications/events`) — broken but invisible (sets `isConnected=false`, no UI gating)
- `sudo.service.ts:155` (`/sudo/events`) — same pattern
- `persistent-chat.service.ts:340` (`/persistent/threads/{id}/stream`) — UI hard-depends on it, so its failure is visible

The persistent-chat control WebSocket at `/ws/persistent/{thread_id}` *also* has no auth check — `await ws.accept()` runs before anything else. Today it's protected only by "you have to know the thread UUID," which is not auth. The cockpit's WS open code (`persistent-chat.service.ts:443-464`) doesn't even try to pass a token. This is a latent hole that the proposed cookie design closes by giving the WS handshake real auth via the cookie.

### The structural opportunity

The proximate fix has two competing solutions:

1. Quick fix: `fetch()` + `ReadableStream` + a manual SSE parser. ~80 LOC in `persistent-chat.service.ts` (and similar in the other two SSE callers). Keeps the in-memory-token model. Lets bearer tokens carry on the SSE handshake. Doesn't change the structural auth posture — XSS still lifts a session, and the WS hole stays open.
2. Structural fix: switch the cockpit from in-memory Bearer tokens to server-issued HttpOnly cookies. Bigger change. Eliminates the SSE problem natively (`EventSource` already ships cookies). Closes XSS-via-token-theft. Authenticates the WS handshake for free. Sets up the auth layer for the next consumer: programmatic API tokens.

This document picks (2), because:
- The cockpit's auth interceptor (`auth.interceptor.ts`) has no 401 handling at all — a stale token doesn't trigger re-auth, requests just go out unauthenticated. The current architecture is already fragile under realistic conditions.
- The user has flagged a coming need for programmatic API tokens (n8n integration, automation scripts). The existing `mcp_tokens` table is structurally identical to what a PAT system needs — keeping them as two parallel systems doubles the surface for no benefit.
- The cookie path lets us address the WS auth gap as a side effect, instead of leaving it as a separate later project.

The work to land the cookie path is genuinely bigger (~3-4 days vs. ~half a day for the polyfill route), but the polyfill route would need to be replaced if we ever ship to real users. This refactor lets us do the structural fix once.

## Goals

1. Cockpit authenticates to the orchestrator via an HttpOnly `srw_session` cookie on `.superhuman-remote-worker.com`.
2. `EventSource` works natively (no polyfill, no header hacks, no token-in-URL).
3. The persistent-chat WS, sudo SSE, and notification SSE all authenticate via the same cookie.
4. Access-token refresh is transparent and happens server-side; the SPA never holds a JWT.
5. Logout coordinates clearing the orchestrator session + Keycloak's SSO session (`id_token_hint`) + optional back-channel logout from Keycloak.
6. CSRF protection is in place for state-changing endpoints, with a defense-in-depth posture (not relying on `SameSite=Lax` alone).
7. `mcp_tokens` becomes `auth_tokens` and supports two kinds: `mcp` (legacy compatibility with Claude Code), `pat` (new, for automation).
8. Users can self-serve PATs from the cockpit settings page, with scopes, expiry, copy-once display, last-used tracking, and audit logging.
9. `Authorization: Bearer <token>` becomes the canonical PAT header, with `X-MCP-Token` kept as a legacy alias.
10. Migration is reversible: rollback at each phase boundary.

## Non-goals

- Replacing Keycloak with something else.
- OAuth 2.0 device flow, service-account JWTs, or other non-PAT machine-to-machine patterns (out of scope for v1; `mcp` kind remains for Claude Code).
- Multi-tenant or organization-scoped tokens. We have 5 users; per-user PAT is sufficient.
- Fine-grained per-resource scopes (the GitHub fine-grained PAT model). Two-tier (named role-style scopes) is right-sized for now.
- Removing the existing `auth_tokens` table for verification/password_reset. That dormant table has a naming collision with what we're proposing; we'll rename the new table to avoid it (see §6.1).
- Fixing the `builder-stream.service.ts:136` `fetch()` bypass of the interceptor. That's a separate latent bug to file.

## Background: what's there today

Mapped end-to-end in subagent runs; key facts only.

### Orchestrator auth

- `orchestrator/security/auth.py` — three documented paths:
  1. `Authorization: Bearer <keycloak_jwt>` → `oidc.validate_token` → JIT user provisioning + `realm_access.roles`-driven `is_approved`.
  2. `X-MCP-User-Id` + `X-Internal-Key == MCP_INTERNAL_KEY` → trusted MCP-server-resolved user.
  3. *Documented* `X-MCP-Token` path that **does not actually exist** in `main.py`. The raw token is consumed by `orchestrator/mcp/auth.py:25-53` (FastMCP's `TokenVerifier`) which posts to `/api/internal/mcp-token-verify` and then calls back as path 2. The orchestrator never sees the raw token.
- `oidc.py` — PyJWKClient with internal caching, RS256, `verify_aud=False`, no refresh-token handling (we're a pure resource server).
- Auth is called **inline at the top of every endpoint body**: `await require_approved_user(request, postgres_db)` × ~42 endpoints, plus `_require_admin` × ~39, plus `get_current_user` × 5. Not via `Depends()`. Touching every site is mechanical but unavoidable for some refactors.
- CORS (`main.py:3288-3301`) already has `allow_credentials=True` and a permissive `allow_origins` list extendable via `CORS_ORIGINS` env. No `expose_headers` set.
- No cookie code anywhere. Zero hits on `request.cookies`, `set_cookie`, `Set-Cookie`, `delete_cookie` across the orchestrator tree.

### Cockpit auth

- `keycloak.service.ts` wraps `keycloak-angular`. `init({onLoad: 'check-sso', pkceMethod: 'S256', checkLoginIframe: false})`. `getToken()` refreshes within 30s of expiry.
- One HTTP interceptor: `auth.interceptor.ts` (28 lines). Adds `Authorization: Bearer …` if `authenticated`. **No 401 handling.**
- `ApiService` wraps `HttpClient`; all other services use `HttpClient` directly. None set `withCredentials` except `mcp-token.service.ts` (vestigial).
- One WebSocket (`persistent-chat.service.ts:443`, `/ws/persistent/{id}`, no auth).
- Three EventSource sites (above).
- `builder-stream.service.ts:136` uses raw `fetch()` and bypasses the interceptor — no `Authorization` attached. Filed as flag, not in scope.

### `mcp_tokens` today

Schema (`migrations/app/0001_initial.sql:145-160`, snapshot `schema.sql:134-149`):

```sql
CREATE TABLE IF NOT EXISTS mcp_tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix VARCHAR(12) NOT NULL,
    scope TEXT NOT NULL DEFAULT 'user',
    origin TEXT,
    expires_at TIMESTAMP WITH TIME ZONE,
    revoked_at TIMESTAMP WITH TIME ZONE,
    last_used_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

Endpoints: `POST /api/mcp-tokens`, `GET /api/mcp-tokens`, `DELETE /api/mcp-tokens/{id}`, `POST /api/internal/mcp-token-verify`, `POST /api/internal/mcp-token-create`. Cockpit settings page already has a full create/copy-once/revoke UI (`settings.component.ts:591-726`) — directly reusable.

### Dormant infrastructure (relevant)

- `sessions` table (`schema.sql:56-66`) with full CRUD in `postgres.py:3080+`. **Zero live callers.** Pre-Keycloak leftover. Comment at `auth.py:9` explicitly says "Session-based auth has been replaced by Keycloak OIDC." We can reuse this table or drop it; the existing schema (`session_key TEXT PRIMARY KEY`, `email`, `csrf_token`) is *almost* what we want but not quite — see §1.2.
- `auth_tokens` table (`schema.sql`) for `verification`/`password_reset` types. Also dormant. Creates a naming collision with our proposed `auth_tokens` consolidation; we'll either drop it in the same migration or pick a different name (see §6.1).
- `magic_link_tokens` (`migrations/app/0006_headless_notifications.sql`) — fresh, in active use for the headless-session magic-link approve flow. Pattern is reusable: opaque random → SHA-256 store → GET-confirms-POST-consumes single-use CAS. We mirror this pattern for PATs and (less directly) for session issuance.

### Sibling code source: `Advanced-LLM-Chat/backend/` (frozen prior project)

A separate FastAPI + Postgres + Angular project lives at `Advanced-LLM-Chat/` in the repo root — a 2025 university chatbot, frozen as a reference. It implements a working cookie BFF *without* OIDC (login is a mock-email stub plus an IP-rate-limited guest path). The cookie/session/cleanup mechanics are production-quality and directly portable; the OIDC layer is absent. Audit results 2026-05-14:

**Lift verbatim or with light adaptation** (replaces ~0.5d of greenfield work in PR 1):

| Source file | Lines | Ports to |
|---|---|---|
| `Advanced-LLM-Chat/backend/security/auth.py:60-79` (`create_session` + `secrets.token_urlsafe(32)`) | session-key generation | `orchestrator/auth/bff.py` `/auth/callback` |
| `Advanced-LLM-Chat/backend/security/auth.py:100-135` (`validate_session`, including expiry coercion and `last_activity` touch) | validator shape | `security/auth.py` cookie branch |
| `Advanced-LLM-Chat/backend/security/auth.py:196-214` (`cleanup_expired_sessions` background task) | session-row cleanup loop | orchestrator lifespan |
| `Advanced-LLM-Chat/backend/api/auth.py:90-109` (cookie-set flag combo: `httponly=True, secure=True, samesite="lax", path="/"`) | cookie issuance | `/auth/callback` + `/auth/refresh` |
| `Advanced-LLM-Chat/backend/api/auth.py:243-274` (logout cookie-clearing block) | logout response shape | `/auth/logout` |
| `Advanced-LLM-Chat/backend/security/auth.py:60-63` (`regenerate_from=` session-fixation defense — see §1.4) | re-login fixation guard | `/auth/callback` |
| `Advanced-LLM-Chat/backend/database/queries/schema.sql:44-53` (sessions table primary shape) | structural sanity check for `0009_srw_sessions.sql` | migration design |
| `Advanced-LLM-Chat/src/app/auth/auth.service.ts:51-86` (`provideAppInitializer` bootstrap waiting on `initializeAuth()` Promise) | cockpit auth bootstrap | new `session.service.ts` |
| `Advanced-LLM-Chat/src/app/auth/auth.guard.ts:17-37` (await-init-Promise-then-check-state race fix) | functional `CanActivateFn` | replaces `cockpit/.../guards/auth.guard.ts` |
| `Advanced-LLM-Chat/src/app/services/api.service.ts:39-51` (cookie parser — 6 LOC) | client-side cookie read helper | cockpit (if surfacing CSRF or session state to UI) |

**Explicitly don't lift** (see §2 for the CSRF rationale, §1.4 for the callback shape):

- The CSRF synchronizer / double-submit-cookie mechanism (`Advanced-LLM-Chat/backend/security/csrf.py` + `middleware/middleware.py:61-102`). OWASP-2017 model. Our design adopts the OWASP-2025 `Sec-Fetch-Site` + custom-header pattern.
- The client-mediated callback (`Advanced-LLM-Chat/src/app/auth/auth-callback/auth-callback.component.ts`). Their SPA reads `code`+`state` from the URL and POSTs them to the backend. Our design has the BFF consume the auth code directly via a server-side 302 — the cockpit never sees the code.
- The in-app `/login` page (`src/app/login/`). Mock-login UI. We use a BFF `/auth/login` redirect instead.
- The guest-login + IP-rate-limit infrastructure (`autoGuestLogin`, `guest_usage` table). No anonymous concept in the orchestrator.
- The `Authorization`-free, single-cookie-path dispatch. We need the multi-path validator (cookie → Bearer → MCP).
- The in-memory `sessions: Dict[str, dict]` at `security/auth.py:17`. Dead code in the source.
- `TIMESTAMP` without timezone in their `sessions` schema. Bug — use `TIMESTAMPTZ` from migration day one.
- `datetime.utcnow()` (deprecated in Python 3.12). Use `datetime.now(UTC)`.
- Their explicit `/refresh-session` endpoint. Our design refreshes transparently inside the validator; one mechanism, not two.

**Missing entirely** (must build fresh — no Advanced-LLM-Chat equivalent): all Keycloak OIDC integration (PKCE, code exchange, JWKS verification beyond what `orchestrator/security/oidc.py` already has, refresh-token flow, `id_token_hint` logout, back-channel logout), the PAT system (`auth_tokens` consolidation + `/api/api-keys` + Bearer dispatch + UI), and the multi-path validator dispatch.

The `user_management_implementation.md` doc in that project is a separate guest-experience UX redesign — not an auth-design doc, irrelevant here.

## Design 1 — Cookie BFF for the cockpit

### 1.1 Architecture

```
Browser                  Orchestrator (api.srw.com)              Keycloak
───────                  ────────────────────────────             ────────
  │
  │ 1. user clicks login
  ├──────────────────────► GET /auth/login
  │                        ◄── 302 to KC /authorize?code_challenge=…
  │                        (sets srw_pre_auth cookie with PKCE verifier)
  │
  ├─────────────────────────────────────────────────────────────► /authorize
  │ ◄────────────────────────────────────────────────── 302 to /auth/callback?code=…
  │
  │                        GET /auth/callback?code=…
  │                        ─── reads srw_pre_auth, exchanges code ──► /token
  │                                                           ◄────── {access, refresh, id_token}
  │                        INSERT INTO sessions (…)
  │                        Set-Cookie: srw_session=<uuid>
  │ ◄───────────────────── 302 to cockpit /
  │
  │ 2. all subsequent API calls
  │ fetch(api/…, credentials: include) — cookie rides along
  │ EventSource(api/…/stream, {withCredentials: true}) — same
  │ WebSocket(api/ws/…) — same (cookies sent on WS handshake)
  │
  │ 3. logout
  ├──────────────────────► POST /auth/logout
  │                        DELETE FROM sessions; clear cookie
  │ ◄───────────────────── { kc_logout_url: "…?id_token_hint=…" }
  ├──────────────────────────────────────────────────────────► /logout
  │ ◄────────────────────────────────────────── 302 back to cockpit
```

The SPA never sees the Keycloak access or refresh token. The orchestrator holds them server-side keyed by session UUID. When the access token expires (5 min default), the orchestrator's session dependency refreshes server-side using the stored refresh token, writes new tokens back to the session row, and the cookie keeps working unchanged. This is the key property that makes SSE just work — long-lived streams don't break on token expiry because the cookie identifies a session, not a JWT.

### 1.2 Session model

New `srw_sessions` table (renamed from the dormant `sessions` to avoid touching it accidentally; we drop the old one in the same migration). Postgres-backed because we already run it, transactional revocation is free, and the load profile (~5 users × maybe 10 active sessions = O(50) rows) is trivial. Redis is reserved for later if perf demands it; the cookie payload is the same opaque UUID either way.

```sql
-- migrations/app/0009_srw_sessions.sql
CREATE TABLE srw_sessions (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id                  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kc_sub                   TEXT NOT NULL,
    kc_sid                   TEXT,                       -- KC session ID, for back-channel logout
    access_token             TEXT NOT NULL,              -- KC JWT, refreshed in place
    refresh_token            TEXT NOT NULL,
    id_token                 TEXT NOT NULL,              -- needed for RP-initiated logout
    access_expires_at        TIMESTAMPTZ NOT NULL,
    absolute_expires_at      TIMESTAMPTZ NOT NULL,       -- = now() + KC refresh TTL at creation
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_agent               TEXT,
    created_ip               INET,
    revoked_at               TIMESTAMPTZ
);

CREATE INDEX srw_sessions_user_idx       ON srw_sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX srw_sessions_kc_sid_idx     ON srw_sessions (kc_sid)  WHERE kc_sid IS NOT NULL;
CREATE INDEX srw_sessions_absolute_idx   ON srw_sessions (absolute_expires_at);

-- Drop the dormant pre-Keycloak table; zero callers, zero data we care about.
DROP TABLE IF EXISTS sessions;
```

Lifetime knobs (env-tunable; defaults below):

| Knob | Default | Env var |
|---|---|---|
| Idle timeout (refreshed on each request) | 30 min | `SRW_SESSION_IDLE_TIMEOUT_S` |
| Absolute lifetime (anchored to KC refresh TTL) | 30 days | `SRW_SESSION_ABSOLUTE_TIMEOUT_S` |
| Access-token refresh skew | 60 s | `SRW_ACCESS_TOKEN_REFRESH_SKEW_S` |

The idle timeout is *enforced by the session validator*, not by the cookie's `Max-Age` — the cookie's Max-Age matches absolute lifetime so the browser doesn't drop it early.

> **Update (2026-06-23) — idle re-validates instead of deleting.** The validator
> originally *deleted* an idle session (`last_seen_at + idle ≤ now()` → drop row →
> 401), which force-logged-out idle-but-still-valid users and, on the ensuing
> full-page redirect, destroyed any unsent cockpit chat draft. It now treats idle
> as a *re-validation checkpoint*: an idle session is refreshed in place against
> Keycloak (the same `_refresh_session_in_place` the near-expiry path already
> uses), so it survives as long as KC's SSO session is alive — the BFF session is a
> *renewable lease* over the KC SSO session. Only a genuine KC rejection (SSO ended
> / refresh revoked) or the absolute cap ends it. **Concurrency invariant:** the
> refresh fan-out a tab refocus can trigger is safe only while Keycloak
> refresh-token rotation is OFF (`revokeRefreshToken=false` — the realm default and
> our config); enabling rotation requires per-session refresh serialization first.
> Full write-up: `docs/issues/persistent_session_idle_expiry_message_swallow.md`.

A pre-auth state table for PKCE verifier + return-to URL between `/auth/login` and `/auth/callback`:

```sql
CREATE TABLE srw_pre_auth_states (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    state           TEXT NOT NULL,                 -- OAuth state parameter
    pkce_verifier   TEXT NOT NULL,
    return_to       TEXT NOT NULL,                 -- post-login redirect inside cockpit
    expires_at      TIMESTAMPTZ NOT NULL,          -- now() + 5 min
    consumed_at     TIMESTAMPTZ
);
CREATE INDEX srw_pre_auth_states_state_idx ON srw_pre_auth_states (state);
```

(Could be a signed cookie instead — but the PKCE verifier is long and the value here of having server-side state for the OAuth exchange outweighs the simplicity of cookies.)

### 1.3 Cookie design

```
Set-Cookie: srw_session=<uuid>; Domain=.superhuman-remote-worker.com;
            Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000

Set-Cookie: srw_pre_auth=<uuid>; Domain=.superhuman-remote-worker.com;
            Path=/auth; HttpOnly; Secure; SameSite=Lax; Max-Age=300
```

| Flag | Value | Rationale |
|---|---|---|
| `HttpOnly` | yes | XSS can't read the cookie via JS. Entire point. |
| `Secure` | yes | Required for `SameSite=None`/HTTPS-only. |
| `SameSite` | `Lax` | Required: the OAuth callback is a top-level GET navigation from Keycloak's domain back to ours; `Strict` would strip the pre-auth cookie there. Lax is the consensus pick (OWASP, Clerk, Duende BFF). |
| `Domain` | `.superhuman-remote-worker.com` | Required so a cookie set by `api.` rides on subsequent requests to `api.`. Side effect: every subdomain on this parent shares cookie visibility — don't host untrusted user content on a subdomain. Mitigation: production deployment must not have a `user-uploads.srw.com` subdomain serving HTML. |
| `Path` | `/` for session; `/auth` for pre-auth | Pre-auth is scoped tighter for hygiene. |
| `Max-Age` | 30 days for session; 5 min for pre-auth | Matches absolute lifetime / OAuth turnaround. |

A separate `srw_dev` cookie variant **is not** issued for localhost. Local dev keeps cookie `Domain` unset (host-only `localhost`) — same logic, just narrower scope.

### 1.4 BFF endpoints (orchestrator)

All under `/auth/*` prefix on the API host. Not under `/api/`, because `/api/` carries the implication of "client API"; `/auth/` is more accurate for BFF endpoints that the browser navigates to directly.

| Verb | Path | Purpose | CSRF |
|---|---|---|---|
| GET | `/auth/login` | Generate state+PKCE, set `srw_pre_auth`, 302 to Keycloak `/authorize` | n/a (GET) |
| GET | `/auth/callback` | Consume `srw_pre_auth`, exchange code, INSERT session, set `srw_session`, 302 back to cockpit | n/a (GET; protected by state+PKCE) |
| GET | `/auth/me` | Return current user (replaces `/api/auth/me`) | n/a (GET) |
| POST | `/auth/refresh` | Force refresh of access token (rare; usually transparent) | CSRF required |
| POST | `/auth/logout` | Revoke session, clear cookie, return KC logout URL | CSRF required |
| POST | `/auth/backchannel-logout` | Keycloak posts signed logout token here when user logs out at IDP | n/a (signed payload) |

Implementation outline:

```python
# orchestrator/auth/bff.py (new)
router = APIRouter(prefix="/auth")
SESSION_COOKIE = "srw_session"
PRE_AUTH_COOKIE = "srw_pre_auth"
COOKIE_DOMAIN = os.environ.get("SRW_COOKIE_DOMAIN") or None  # None = host-only for dev
COOKIE_SECURE = os.environ.get("SRW_COOKIE_SECURE", "1") == "1"

@router.get("/login")
async def login(request: Request, return_to: str = "/"):
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    pre_auth_id = await postgres_db.create_pre_auth(state=state, pkce_verifier=verifier,
                                                   return_to=return_to, ttl=300)
    challenge = pkce_s256_challenge(verifier)
    url = f"{KC_AUTHORIZE_URL}?{urlencode({...})}"  # client_id, redirect_uri, scope, state, code_challenge
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(PRE_AUTH_COOKIE, str(pre_auth_id), max_age=300, path="/auth",
                    domain=COOKIE_DOMAIN, secure=COOKIE_SECURE, httponly=True, samesite="lax")
    return resp

@router.get("/callback")
async def callback(request: Request, code: str, state: str):
    pre_id = request.cookies.get(PRE_AUTH_COOKIE)
    pre = await postgres_db.consume_pre_auth(pre_id)
    if not pre or pre["state"] != state:
        raise HTTPException(400, "invalid state")
    tokens = await kc_exchange_code(code, pre["pkce_verifier"])
    claims = oidc_validator.decode_unverified_id_token(tokens["id_token"])  # we issued the redirect, KC verifies code

    # Session-fixation defense (lifted from Advanced-LLM-Chat/backend/security/auth.py:60-63):
    # if the user already has a session cookie when they hit /callback (re-login flow
    # without explicit logout), kill the old session before issuing the new one. Without
    # this, an attacker who captured a session ID before login could keep using it after
    # the user re-authenticates.
    old_sess_id = request.cookies.get(SESSION_COOKIE)
    if old_sess_id:
        await postgres_db.delete_session(old_sess_id)

    sess_id = await postgres_db.create_session(
        user_id=…,    # JIT-provisioned same as today
        kc_sub=claims["sub"],
        kc_sid=claims.get("sid"),
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        id_token=tokens["id_token"],
        access_expires_at=now() + tokens["expires_in"],
        absolute_expires_at=now() + tokens["refresh_expires_in"],
        user_agent=request.headers.get("user-agent"),
        created_ip=request.client.host,
    )
    resp = RedirectResponse(SPA_BASE + pre["return_to"], status_code=302)
    resp.set_cookie(SESSION_COOKIE, str(sess_id), …)
    resp.delete_cookie(PRE_AUTH_COOKIE, path="/auth", domain=COOKIE_DOMAIN)
    return resp
```

### 1.5 Validator changes

`require_approved_user(request, db)` learns the cookie path first, falls back to Bearer (for PAT + MCP-internal + transitional cockpit support):

```python
# orchestrator/security/auth.py — extended require_approved_user
async def get_current_user(request: Request, db) -> dict:
    # 1. Cookie path — preferred for cockpit
    session_id = request.cookies.get("srw_session")
    if session_id:
        sess = await db.get_session(session_id)
        if sess and sess.revoked_at is None \
                and sess.absolute_expires_at > now():        # absolute cap = only hard stop
            # Idle OR access-token near expiry → re-validate with Keycloak by
            # refreshing in place. Idle no longer deletes the row: while KC's SSO
            # session is alive the refresh renews it (renewable lease); a genuine
            # KC rejection inside refresh_session deletes the row and 401s.
            idle = sess.last_seen_at + IDLE_TIMEOUT <= now()
            if idle or sess.access_expires_at - now() < REFRESH_SKEW:
                sess = await refresh_session(sess, db)   # None on KC reject → falls through → 401
                if sess is None:
                    return await _bearer_or_mcp(request, db)
            await db.touch_session_last_seen(session_id)
            return await db.get_user(sess.user_id)

    # 2. Bearer path — JWT or PAT
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        if token.startswith("srw_"):
            return await _resolve_pat(token, request, db)   # see §3
        return await _resolve_jwt(token, db)                 # current logic, unchanged

    # 3. MCP-internal trust (unchanged)
    return await _get_user_from_mcp_headers(request, db)
```

This shape preserves the ~86 inline `await require_approved_user(…)` call sites unchanged. The function gains paths; signature is identical.

Refresh logic posts to Keycloak's token endpoint with `grant_type=refresh_token`, updates the session row atomically, and on failure (token revoked, user disabled, KC down) raises 401 — the cockpit's interceptor sees that and triggers a re-login. If KC issues a new refresh token (rotation enabled), we store it; otherwise we keep the existing one.

### 1.6 Mid-stream refresh for SSE

The SSE endpoints don't need anything special — the session is established by the cookie at handshake time, and the validator runs only once. The access token stored in the session row may expire mid-stream, but the orchestrator doesn't *use* the access token to serve SSE chunks (it's a resource server, not a proxy). So the stream just keeps running.

The only place the access token matters mid-stream is if the SSE handler itself calls a downstream that requires the JWT (e.g. the orchestrator calling an upstream OIDC-protected service). Today this doesn't happen on the persistent-chat stream path. If it ever does, the pattern is:

```python
async def event_stream():
    async for evt in source:
        if sess.access_expires_at - now() < REFRESH_SKEW:
            sess = await refresh_session_in_place(sess, db)
        yield format_sse(evt)
```

— check on each yield, refresh if needed. Cheap; the check is a single `<` comparison and the refresh hits KC at most once per ~5 min.

### 1.7 Single sign-out

Two paths:

**Front-channel** (user clicks logout in cockpit):

```python
@router.post("/logout")
async def logout(sess = Depends(current_session)):
    id_token = sess.id_token
    await postgres_db.delete_session(sess.id)
    qs = urlencode({"id_token_hint": id_token,
                    "post_logout_redirect_uri": SPA_BASE})
    resp = JSONResponse({"kc_logout_url": f"{KC_LOGOUT_URL}?{qs}"})
    resp.delete_cookie(SESSION_COOKIE, domain=COOKIE_DOMAIN, path="/")
    return resp
```

The cockpit then redirects `window.location.href` to `kc_logout_url`. KC clears its SSO cookie and redirects back to the cockpit. `id_token_hint` is essential to avoid Keycloak's confirmation screen (KC 19+).

**Back-channel** (user clicks logout in Keycloak account console, or admin force-revokes):

Configure the `cockpit` client in Keycloak with a Back-Channel Logout URL of `https://api.superhuman-remote-worker.com/auth/backchannel-logout`. Keycloak POSTs a signed logout token; we verify it via JWKS (same key infrastructure as `oidc.py`) and `DELETE FROM srw_sessions WHERE kc_sid = $1` (or `kc_sub` if no `sid`). The user's next request to the orchestrator from that browser gets a 401 and the cockpit interceptor (now with proper 401 handling) redirects to `/auth/login`.

## Design 2 — CSRF protection

### 2.1 Approach

Layered, no server-side token store:

1. **No-cookie short-circuit** (landed shape, hardened in PR-3 hotfix). If the request has no `srw_session` cookie, CSRF doesn't apply — the cookie *is* the vector, and without it there is no browser-mediated session to forge against. This implicitly bypasses CSRF for: Bearer-auth callers (PAT, MCP, transitional JWT), `X-Internal-Key` MCP-server traffic, and in-cluster agent-pod → orchestrator-pod HTTP. The original design listed Bearer + X-Internal-Key as the only exemptions; this turned out to miss the agent path (see "PR 3 hotfix" in the Implementation log) which carries neither marker but is also not cookie-authenticated.
2. `SameSite=Lax` on the session cookie. Blocks most cross-site request hijacking by default.
3. `Sec-Fetch-Site` header check on non-safe methods (POST/PUT/DELETE/PATCH). Reject if `cross-site`. This header is unforgeable from JS and is sent by every browser shipping since March 2023. OWASP's Dec 2025 cheatsheet upgrades this from "fallback" to a primary mechanism.
4. Custom-header requirement (`X-CSRF: 1`) on non-safe methods, set by the cockpit's HTTP interceptor. Non-safelisted custom headers force a CORS preflight, which an attacker can't satisfy. This is the Duende BFF pattern.
5. Origin check on the same non-safe methods: reject if `Origin` is set and not in the allowlist (`https://superhuman-remote-worker.com` + dev origins).

Layers 2–5 only fire on requests that DID present a session cookie. The Angular interceptor adds `X-CSRF: 1` on every non-GET. SSE GETs don't need CSRF — they're GETs, and SameSite=Lax already prevents cross-site EventSource attachment.

```python
# orchestrator/security/csrf.py (landed shape)
SAFE = {"GET", "HEAD", "OPTIONS"}

class CSRFMiddleware:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in SAFE:
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path in EXEMPT_EXACT or path.startswith(EXEMPT_PREFIXES):
            return await self.app(scope, receive, send)
        request = Request(scope)
        # Short-circuit: no session cookie → no CSRF vector. Covers Bearer,
        # X-Internal-Key, AND in-cluster agent traffic in one rule.
        if "srw_session" not in request.cookies:
            return await self.app(scope, receive, send)
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return await self.app(scope, receive, send)
        if request.headers.get("x-internal-key"):
            return await self.app(scope, receive, send)
        sfs = request.headers.get("sec-fetch-site")
        if sfs == "cross-site":
            return await _send_403(send, "csrf:cross-site")
        if not request.headers.get("x-csrf"):
            return await _send_403(send, "csrf:missing-header")
        if sfs is None:
            origin = request.headers.get("origin")
            if origin and origin not in ALLOWED_ORIGINS:
                return await _send_403(send, "csrf:bad-origin")
        return await self.app(scope, receive, send)
```

PAT-authenticated requests skip CSRF — both via the no-cookie short-circuit (typical) and the Bearer fallback (hybrid case where a Bearer call happens to ride a cookie too).

### 2.2 What this is not

We are not generating per-session synchronizer CSRF tokens, not double-submitting, not storing CSRF state anywhere. The `sessions.csrf_token` column on the dormant pre-Keycloak `sessions` table — we drop it. Modern best practice for JSON-only APIs has moved past synchronizer tokens for this exact scenario.

We also explicitly **do not lift the CSRF mechanism from `Advanced-LLM-Chat/backend/security/csrf.py`** despite it being a working, tested reference implementation. That codebase uses the classic OWASP-2017 double-submit-cookie pattern: a non-HttpOnly `csrf_token` cookie that JS echoes into `X-CSRF-Token` on each non-safe request, compared server-side via string equality. It works, but porting it would move us backwards relative to the OWASP Dec-2025 cheatsheet revision, which now treats `Sec-Fetch-Site` + a custom-header preflight forcer as a primary mechanism rather than a fallback. Concretely:

- Their model needs a CSRF cookie set on every login/refresh path, a non-HttpOnly storage choice (so JS can read it), a cookie-parser in the SPA, an interceptor that injects the echoed token on every non-GET, and a per-session DB column to store the canonical value. ~150 LOC total.
- Our model needs middleware that checks `Sec-Fetch-Site` and the `X-CSRF: 1` flag header. No CSRF cookie, no DB column, no SPA parsing, no echo round-trip. ~30 LOC total.

The structural middleware shape (OPTIONS bypass, exempt-path allowlist, JSON-403 response with CORS headers patched) IS worth reading from `Advanced-LLM-Chat/backend/middleware/middleware.py:61-102` as a reference for how to wire the FastAPI middleware properly. The *validation algorithm* inside should be ours, not theirs.

## Design 3 — Consolidated API tokens

### 3.1 Format

```
ak_<43-char base64url>
```

Example: `ak_kx7T3pQ9mZvF2nR8sY1bL4cH6jW0eU5aI8oN3xM7vC2`

| Segment | Why |
|---|---|
| `ak` | Namespace for API keys. Same convention `automations_v0.md` already commits to: `ak_` for full-access keys; future `ar_` for read-only, `as_` for service keys, etc. coexist via prefix-sniff. |
| `_` | Boundary char not in base64url; double-click selects the whole token cleanly. |
| 32 random bytes (43 chars base64url) | 256 bits of entropy. Generated by `secrets.token_urlsafe(32)`. Comfortably above OWASP's 128-bit minimum. |

Generated by `f"ak_{secrets.token_urlsafe(32)}"`. No CRC32 checksum — GitHub-scale infrastructure; for 5 users it's maintenance liability with negligible payoff.

Existing MCP tokens at `srw_<32-char>` (`main.py:13286` — `"srw_" + secrets.token_urlsafe(32)`) keep their existing format. The validator distinguishes `ak_…` (API key) from `srw_<bare>` (legacy MCP) by prefix-sniff. Both kinds live in the same `auth_tokens` table (see §3.6).

### 3.2 Storage

```sql
-- migrations/app/0010_auth_tokens_consolidation.sql

-- Drop the dormant pre-Keycloak table to free the name.
DROP TABLE IF EXISTS auth_tokens;

-- Rename mcp_tokens and extend.
ALTER TABLE mcp_tokens RENAME TO auth_tokens;

ALTER TABLE auth_tokens
    ADD COLUMN kind         TEXT NOT NULL DEFAULT 'mcp'
        CHECK (kind IN ('mcp', 'api')),
    ADD COLUMN last_four    CHAR(4),                   -- displayed in UI: '…vC2'
    ADD COLUMN last_used_ip INET,
    ADD COLUMN superseded_by UUID REFERENCES auth_tokens(id) ON DELETE SET NULL;

ALTER TABLE auth_tokens
    ALTER COLUMN kind DROP DEFAULT;

CREATE INDEX auth_tokens_kind_user_idx
    ON auth_tokens (user_id, kind) WHERE revoked_at IS NULL;
```

The `DEFAULT 'mcp'` on the `ADD COLUMN` populates all pre-existing rows correctly (every current token in the table came from the MCP flow), then `DROP DEFAULT` forces new rows to set `kind` explicitly.

`token_hash` stays TEXT (existing schema uses hex). All new API keys hash with SHA-256 and store the hex digest. Legacy MCP tokens already do the same. `last_four` is the last 4 characters of the plaintext token, displayed in the UI as `ak_…vC2`.

**Hash-only, no encrypted-at-rest.** GitHub, Stripe, Anthropic all do this. Reasoning: a 256-bit random token can't be brute-forced from its hash, so SHA-256 is fast enough for per-request validation while still being unrecoverable. bcrypt/argon2 would be wrong here — those are for low-entropy human passwords. Plain SHA-256 is the right hash for high-entropy tokens.

Existing MCP tokens carry on unchanged with `kind='mcp'`. Their endpoints (`/api/mcp-tokens`, `/api/internal/mcp-token-verify`) stay wired and work identically — backwards-compat for Claude Code.

### 3.3 Scopes

Stored as `TEXT[]`. Two-tier model:

| Scope | Grants |
|---|---|
| `jobs:read` | List/get jobs, projects, audit |
| `jobs:write` | Create/cancel/resume jobs, send messages, attach files |
| `chat:read` | Read persistent threads, message history |
| `chat:write` | Create threads, send messages, interrupt, slash-commands |
| `knowledge:read` | Read knowledge notes, search, list sources |
| `knowledge:write` | Create/update/delete knowledge notes, citations |
| `admin` | Sudo approvals, user/project admin, datasource admin |

MCP tokens (`kind='mcp'`) keep their existing implicit super-scope semantics, mediated by the existing MCP-server validator (which already does its own scope-checking via the `scope` column). We don't change the MCP path.

PAT scopes default to `['jobs:read', 'chat:read']` in the create UI. User selects more explicitly. `admin` scope is only offered to admin users.

The endpoint-to-scope mapping is added incrementally — start with the most-used endpoints and add a `@require_scope("jobs:write")` decorator (or equivalent inline check). Endpoints that don't have a scope mapping yet are accessible to PATs with any scope (transitional permissive mode), with a TODO to lock down. Phase 3 (post-v1) closes the remaining endpoints.

### 3.4 Lifetime & UX

**Default expiry: 90 days.** Allowed options in the create UI: `30 days`, `90 days`, `1 year`, `never` (with a warning). This mirrors GitHub's classic-PAT model. We don't enforce required expiry like GitHub fine-grained does, because our user base is small enough that "your token expired" pain outweighs the marginal security gain.

**Default expiry: 1 year.** Allowed options in the create UI: `30 days`, `90 days`, `1 year`, `never` (with a warning). The 1-year default reflects an internal-app threat model — short defaults like GitHub's 90 days create token-rotation pain for set-and-forget n8n flows.

UI lives in a **separate page** at `cockpit/src/app/views/settings/api-keys/` (per `automations_v0.md`, not as a section inside the existing settings page):

1. **List page** (`api-keys-page.component.ts`): `name | scopes | created | last_used | expires | hint (ak_…vC2)`, sorted by `last_used_at DESC NULLS LAST`. Stale tokens (90+ days unused) get a yellow row.
2. **Create dialog** (`api-key-create-dialog.component.ts`): modal with name (required), scopes (checkboxes, default jobs:read+chat:read), expiry (dropdown, default 1 year). On submit, server generates token, returns plaintext once.
3. **Display-once banner**: full token in monospace `<input readonly>` with copy-to-clipboard button, prominent warning ("This is the last time you'll see this token"). Acknowledge checkbox required to dismiss.
4. **Revoke**: per-row "Revoke" button. Confirmation modal requires typing the token name. Soft-delete (`revoked_at = now()`).
5. **Rotate**: per-row "Rotate" button. Generates new token with same name+scopes, displays once, sets `old_token.superseded_by = new_id`, keeps old valid for 24-hour grace window. Auto-revoke job runs daily via existing `cleanup_expired_tokens` loop.

The existing MCP token UI at `settings.component.ts:591-726` stays in place untouched. Two settings surfaces, one underlying table — different `kind` filter on the API request.

### 3.5 Header & validator dispatch

Canonical: `Authorization: Bearer <token>`. Works with `requests.auth`, axios, n8n's "Header Auth" credential type, every HTTP client.

`X-MCP-Token` stays accepted as a legacy alias for `kind='mcp'` tokens, routed through the existing `mcp-token-verify` flow. New PATs only work via `Authorization: Bearer`.

Validator dispatch in `require_approved_user` (landed shape — split into two resolvers since the legacy MCP path needs slightly different post-processing than the new PAT path):

```python
# orchestrator/security/auth.py
async def get_current_user(request: Request, db) -> dict:
    # ... cookie path tried first ...
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return await _get_user_from_mcp_headers(request, db)
    token = auth[7:]
    if token.startswith("ak_"):
        return await _resolve_pat(token, request, db)
    if token.startswith("srw_"):
        return await _resolve_legacy_mcp_token(token, request, db)
    if token.count(".") == 2:
        claims = oidc_validator.validate_token(token)
        if not claims:
            raise HTTPException(401, "Invalid or expired token")
        return await _resolve_user_from_claims(claims, db)
    raise HTTPException(401, "Unrecognized token format")

async def _resolve_pat(token: str, request: Request, db) -> dict:
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    row = await db.get_auth_token_by_hash(digest)
    if not row or row["kind"] != "api":
        raise HTTPException(401, "Invalid token")
    user = await db.get_user(str(row["user_id"]))   # str() — asyncpg UUID
    if not user:
        raise HTTPException(401, "Invalid token")
    asyncio.create_task(db.touch_auth_token(str(row["id"]), _client_ip(request)))
    user["auth_method"] = "pat"
    user["scopes"] = list(row.get("scopes") or [])
    user["token_id"] = str(row["id"])
    user["is_approved"] = True
    return user

# _resolve_legacy_mcp_token has the same shape but enforces kind='mcp',
# returns scope as a single-element list ('user' / 'all' / 'project:<uuid>'),
# and sets auth_method='mcp'.
```

JWT vs API key vs cookie distinction is shape-based:
- Cookie present → cookie path.
- `Authorization: Bearer ak_…` → PAT (`kind='api'` in `auth_tokens`). Sets `auth_method='pat'`, `scopes=[…]`.
- `Authorization: Bearer srw_…` → legacy MCP token (`kind='mcp'`). The MCP server's own `TokenVerifier` path through `/api/internal/mcp-token-verify` keeps working unchanged; the direct-Bearer path landed in `_resolve_legacy_mcp_token` for callers that hit the orchestrator without the MCP server in the middle. Sets `auth_method='mcp'`, `scopes=[legacy_scope]`.
- `Authorization: Bearer <three-segment JWT>` → Keycloak JWT (transitional during cockpit cutover; PR 2 means the cockpit no longer uses this path itself, but direct API consumers still can).
- No credential → 401.

Three dots in the token = JWT. Prefix-sniff for the others. No KID-lookup-then-fallback; failure modes are clean.

**Important `str()`-coercion detail** (fixed in the PR-3 hotfix): `row["user_id"]` and `row["id"]` are asyncpg native `pgproto.UUID` instances, not strings. `db.get_user(...)` normalizes its argument via `uuid.UUID(arg)`, which assumes `arg` is a string and calls `.replace('urn:', '')`. Passing a native UUID raises `AttributeError`. Both resolvers wrap with `str(...)` before any DB helper call.

### 3.6 Consolidation strategy

We rename `mcp_tokens` → `auth_tokens` in `0010_auth_tokens_consolidation.sql`. Same migration drops the dormant pre-Keycloak `auth_tokens` (verification/password_reset) which has no callers. Rollback is `DROP COLUMN kind, last_four, last_used_ip, superseded_by` + rename back; safe because none of the new columns are FK'd from anywhere.

**This consolidation overrides `automations_v0.md`'s Open Decision #1** ("new `api_keys` table — clean separation is worth it"). That decision was filed before the BFF refactor was on the table; at the time, "add a parallel table" was the simpler-to-reason-about choice. With the wider auth refactor consolidating session, JWT, and token handling, having two structurally identical token tables doubles the helper surface for no benefit. The schema of `mcp_tokens` is completely generic (`id, user_id, name, token_hash, token_prefix, scope, origin, expires_at, revoked_at, last_used_at, created_at`) — nothing in it is MCP-specific. "MCP session semantics" lives in the *validator path* (FastMCP's TokenVerifier pre-resolves the token before it reaches the orchestrator), not in the data shape. Consolidation keeps the validator paths separate (prefix-sniff) while sharing helpers, audit, and rotation. `automations_v0.md` is being updated in the same PR to match.

Existing `/api/mcp-tokens` endpoints stay live. They write rows with `kind='mcp'`. The cockpit's MCP token UI keeps working. A new `/api/api-keys` endpoint family is added for API keys (path name preserved from `automations_v0.md`):

| Verb | Path | Returns |
|---|---|---|
| POST | `/api/api-keys` | `{token: "ak_…", id, name, …}` |
| GET | `/api/api-keys` | `[{id, name, scopes, created_at, last_used_at, …}]` (filtered to `kind='api'` for current user) |
| DELETE | `/api/api-keys/{id}` | revokes |
| POST | `/api/api-keys/{id}/rotate` | `{token: "ak_…", id_old, id_new}` |

These endpoints are scoped to API keys only. The DB layer methods get a `kind` parameter (`db.list_auth_tokens(user_id, kind="api")`) so MCP and API-key routes don't cross-pollinate.

### 3.7 Audit

Each authenticated request writes a row to MongoDB `auth_audit` (uses the existing graceful-degradation MongoDB path — failures non-fatal). Fields modeled on GitHub's audit-log schema:

```
@timestamp, request_id, actor_id, actor_email,
auth_method ('cookie' | 'jwt' | 'pat' | 'mcp'),
token_id (if pat/mcp), token_kind, token_prefix,
hashed_token_first_16,    # never the full hash, never plaintext
scopes_granted, route, method, status_code, ip, user_agent
```

Plus lifecycle events on the token:
- `auth.token.created` — `{token_id, kind, scopes, expires_at, actor}`
- `auth.token.revoked` — `{token_id, reason, actor}`
- `auth.token.rotated` — `{old_token_id, new_token_id, grace_until}`

Plaintext tokens are never logged. The orchestrator's logging config gets a redaction filter that masks any string matching `^srw_[a-z]+_v\d+_\S+`.

## Surfaces to change

### Orchestrator

| File | Change |
|---|---|
| `orchestrator/auth/bff.py` *(new)* | BFF endpoints (`/auth/login`, `/auth/callback`, `/auth/logout`, `/auth/backchannel-logout`, `/auth/refresh`) |
| `orchestrator/security/auth.py` | `get_current_user` gains cookie path; `_resolve_pat` added; `require_approved_user` unchanged |
| `orchestrator/security/oidc.py` | Add `decode_unverified_id_token` for the callback flow; add `verify_logout_token` for back-channel |
| `orchestrator/security/csrf.py` *(new)* | The middleware in §2.1 |
| `orchestrator/security/kc_client.py` *(new)* | `exchange_code(code, verifier)`, `refresh(refresh_token)`, `revoke_session_kc_side(refresh_token)` — small `httpx` wrapper |
| `orchestrator/database/postgres.py` | `create_session`, `get_session`, `touch_session_last_seen`, `refresh_session_tokens`, `delete_session`, `delete_sessions_by_kc_sid`; `create_pre_auth`, `consume_pre_auth`; `get_auth_token_by_hash` (cross-kind), `touch_auth_token`; `list_auth_tokens(kind=…)`, `rotate_auth_token` |
| `orchestrator/database/migrations/app/0009_srw_sessions.sql` *(new)* | Session + pre-auth tables, drop dormant `sessions` |
| `orchestrator/database/migrations/app/0010_auth_tokens_consolidation.sql` *(new)* | Rename mcp_tokens→auth_tokens; drop dormant `auth_tokens`; new columns |
| `orchestrator/main.py` | Mount BFF router, mount CSRF middleware, add `/api/auth-tokens` endpoints (~60 lines). Existing `~86` auth-using endpoint bodies stay unchanged — they call `require_approved_user(request, postgres_db)` which gains the cookie path transparently |
| `orchestrator/main.py` | Add `await require_approved_user(...)` to `/ws/persistent/{thread_id}` handler (closes the WS auth hole) |
| `orchestrator/main.py` | Cookie path equally protects `/notifications/events` and `/sudo/events` SSE — those already require approved user inline, they just gain cookie auth for free |

### Cockpit

| File | Change |
|---|---|
| `src/app/core/services/keycloak.service.ts` | Replace with `src/app/core/services/session.service.ts` — small Signal-based service that wraps `/auth/me`, `/auth/logout`. Remove `keycloak-angular` dependency. *Or* keep `keycloak.service.ts` thin (just for the redirect to `/auth/login`) and add session service alongside |
| `src/app/core/interceptors/auth.interceptor.ts` | Rewrite. Set `withCredentials: true` on every request; add `X-CSRF: 1` on non-GET; on 401, redirect `window.location.href = '${apiUrl}/auth/login?return_to=${current_path}'` |
| `src/app/core/services/persistent-chat.service.ts:340` | Already uses `{withCredentials: true}`; the stale comment about cookies becomes accurate. No code change needed beyond removing the misleading comment. |
| `src/app/core/services/persistent-chat.service.ts:443` | Add `withCredentials: true` to WebSocket — wait, WS doesn't have that option. But browsers DO send cookies on cross-origin WS handshake automatically when the URL is `wss://api.…`. So no change in cockpit; the orchestrator's WS endpoint just needs to read `request.cookies` |
| `src/app/core/services/notification.service.ts:90` | Add `{withCredentials: true}` |
| `src/app/core/services/sudo.service.ts:155` | Add `{withCredentials: true}` |
| `src/app/core/services/mcp-token.service.ts` | Already has `withCredentials: true` (vestigial); now load-bearing |
| `src/app/core/services/api-keys.service.ts` *(new)* | List/create/revoke/rotate API keys. Path per `automations_v0.md`. |
| `src/app/views/settings/api-keys/api-keys-page.component.ts` *(new)* | Separate page (sibling to existing settings page), per `automations_v0.md` |
| `src/app/views/settings/api-keys/api-key-create-dialog.component.ts` *(new)* | Create dialog + copy-once banner |
| `src/app/app.routes.ts` | Add `/settings/api-keys` route (auth-guarded) |
| `src/app/core/guards/auth.guard.ts` | Replace `keycloak.login()` redirect with `window.location.href = '${apiUrl}/auth/login?return_to=…'` |
| `src/app/app.config.ts` | Drop the Keycloak `APP_INITIALIZER`; replace with a `loadCurrentUser` initializer that GETs `/auth/me` and 302s to `/auth/login` if 401 |
| `src/assets/env.js` | Drop `keycloakUrl`/`keycloakRealm`/`keycloakClientId` (cockpit no longer talks to KC directly). Or keep for the dev fallback path |
| `package.json` | Optionally remove `keycloak-angular`, `keycloak-js`. Saves ~150 KB of bundle |

### Database

Two new migration files in `orchestrator/database/migrations/app/`:
- `0009_srw_sessions.sql` — session + pre-auth tables, drop dormant `sessions`
- `0010_auth_tokens_consolidation.sql` — rename mcp_tokens, drop dormant `auth_tokens`, add columns

Per `docs/db_migration.md`: transactional (no `.notx.sql` needed). Migration runner picks them up at orchestrator startup. Squawk CI gate will check them for unsafe operations — both should pass (no concurrent indexes, no NOT NULL adds without defaults, no destructive renames of in-use columns).

### Helm / config

| File | Change |
|---|---|
| `deployment/legacy/20-orchestrator.yaml` (or wherever the orchestrator deployment lives) | Add env: `SRW_COOKIE_DOMAIN=.superhuman-remote-worker.com`, `SRW_COOKIE_SECURE=1`, `SRW_SESSION_IDLE_TIMEOUT_S=1800`, `SRW_SESSION_ABSOLUTE_TIMEOUT_S=2592000`, `KC_CLIENT_SECRET=<from-vault>` |
| Keycloak realm config | Add the new client secret to `cockpit` client (today it's public — needs to switch to confidential). Configure Back-Channel Logout URL = `https://api.superhuman-remote-worker.com/auth/backchannel-logout` |
| Vault (ESO) | New secret entry for `KC_CLIENT_SECRET` |
| `deployment-local/` | Local-cluster equivalents |

The Keycloak client switch from public→confidential is the most fragile piece — it changes the OIDC flow's token-endpoint authentication. Worth a manual test in dev cluster before rolling to prod.

## Phasing

Three PR-sized chunks. Each is independently reviewable and revertable.

### PR 1 — Cookie BFF foundation (orchestrator side) — ✅ shipped 2026-05-13

Scope: `0009_srw_sessions.sql`, `auth/bff.py`, `security/csrf.py`, `security/kc_client.py`, validator changes in `security/auth.py`, WS auth fix, Helm env additions. See "Implementation log" above for the two design deviations (separate `cockpit-bff` client; id_token sub-merge for KC 24+).

Acceptance: orchestrator running this PR + a hand-crafted curl flow (login → callback → cookie-authenticated `/auth/me`) works against dev Keycloak. Existing Bearer-auth path unchanged; cockpit untouched and still works on develop branch unchanged.

Actual effort: ~1.5 days, matching the estimate. Cookie mechanics + session table + cleanup loop + logout response + session-fixation defense are lifted from `Advanced-LLM-Chat/backend/` (see Background §"Sibling code source"), saving ~0.5d of greenfield work. The OIDC client and validator dispatch are new.

### PR 2 — Cockpit cutover — ✅ shipped 2026-05-14

Scope: drop `keycloak-js` (cockpit uses keycloak-js directly, not keycloak-angular as the original draft said), rewrite `auth.interceptor.ts`, update two EventSource sites (notifications + sudo; persistent-chat already had `withCredentials`) + WS site, swap auth guard to BFF redirect, drop Keycloak `APP_INITIALIZER`, add `/auth/me` bootstrap.

Acceptance: see "PR-2 acceptance run" in the Implementation log — all 10 probes passed against dev cluster `sha-e933414`. The notification and sudo SSE attach successfully (they didn't before); the WS handshake is now cookie-authenticated.

Actual effort: ~half a day of focused work, well under the 1.5-day estimate — the bulk was rewriting the interceptor, replacing the auth bootstrap, and a mechanical sweep of `KeycloakService` callers.

### PR 3 — API token consolidation + UI — ✅ shipped 2026-05-14

Scope: `0010_auth_tokens_consolidation.sql`, `/api/api-keys` endpoints, `_resolve_pat` + `_resolve_legacy_mcp_token` validator branches, `api-keys.service.ts`, standalone settings page at `/settings/api-keys`. See "Implementation log §PR 3" for the landed shape and the two hotfix bugs.

Acceptance: A user can create an API key from `/settings/api-keys`, copy it, use it with `curl -H "Authorization: Bearer ak_…" https://api.…/api/jobs`, see `last_used_at` + `last_used_ip` update, revoke it, rotate it. n8n's "Header Auth" credential with `Authorization`/`Bearer …` works against our API. All 12 probes passed in the dev cluster — see "PR-3 acceptance run" above.

Actual effort: ~1 day matching the estimate, plus a few hours of hotfix work that retroactively cleaned up a PR-1 agent-CSRF regression as well.

### PR 4 — Cleanup (optional, post-v1)

Scope: scope-enforcement decorators on remaining endpoints (lock down permissive PAT mode), removal of `keycloak.service.ts` dead code if PR 2 didn't, addition of `auth_audit` MongoDB rows, redaction filter on logging.

## Open decisions

All 10 decisions are locked as of 2026-05-14. See the "Decisions locked" table at the top of the doc for the resolved values.

Discussion summary preserved for posterity:

1. **Pre-auth state.** DB table. Avoids signing-key management; PKCE verifier is long.
2. **Session store.** Postgres. No new dependency.
3. **Cookie name.** `srw_session`. Vendor-prefixed.
4. **Session timeouts.** 30 min idle, 30 days absolute.
5. **Keycloak client mode.** Confidential — required for BFF pattern (orchestrator authenticates at the token endpoint when exchanging a code).
6. **`builder-stream.service.ts` interceptor bypass.** Filed as `docs/done/cockpit_builder_stream_fetch_bypasses_auth.md`. Out of scope for this refactor.
7. **PR 3 split.** Don't split — consolidation + UI ship together to avoid a half-shipped state.
8. **Token expiry default.** 1 year. Internal-app threat model; set-and-forget automation flows don't want quarterly rotation pain.
9. **API key UI location.** Separate page at `views/settings/api-keys/` per `automations_v0.md`.
9b. **Table model.** Consolidate `mcp_tokens` → `auth_tokens` with `kind` column. Overrides `automations_v0.md` Open Decision #1; rationale in §3.6.
10. **Audit logging.** Every authenticated request to MongoDB graceful-degrade audit path.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Keycloak realm config switch (public→confidential) breaks dev login | **Realised 2026-05-13.** First PR-1 attempt flipped the existing `cockpit` public client to confidential per the literal design. SPA login broke immediately — the PKCE-without-secret flow gets rejected at the token endpoint. Resolved by adding a separate `cockpit-bff` confidential client alongside the unchanged public `cockpit`. The two clients coexist in the realm; the SPA's old login keeps working through PR 1, then PR 2 cuts it over. Rollback = "stop using `cockpit-bff`". See Implementation log §"PR 1". |
| KC 24+ removed `sub` claim from access tokens — callback 500'd `KeyError: 'sub'` | **Realised 2026-05-13.** Resolved with a defensive `_merge_identity_claims` helper in `security/auth.py` that decodes the id_token (which always carries `sub` per OIDC) and merges identity fields into the access-token claim bag before user resolution. Used by both the BFF callback and the cookie validator. Avoids per-client KC realm config and is robust to future default changes. |
| 86 inline `require_approved_user` calls have subtle variations that break with cookie path | The function signature stays identical; cookie path is purely additive. Pre-PR-1 grep audit confirms all call sites use the canonical shape. |
| Cookie scoped to `.superhuman-remote-worker.com` exposes other subdomains | Audit current subdomain inventory: `cockpit`, `api`, `auth`, `dozzle`, `git`, `mcp`, `mongo`, `cloud`, `pgadmin`. Confirm none serve untrusted user-uploaded HTML. The `cloud` subdomain (OpenCloud) is the highest-risk — review its content-security headers. |
| Refresh token rotation enabled in Keycloak breaks long-lived sessions | If KC issues a new refresh token on each exchange, we store it. If we drop one due to a race, the session dies and user re-logs in. Test with KC's rotation policy enabled. **Amplified by idle-refresh (2026-06-23):** a tab refocus can fan out concurrent refreshes of the same token, so rotation must stay OFF (`revokeRefreshToken=false`) until per-session refresh serialization exists. |
| Back-channel logout URL not reachable from KC (network policy) | Verify the orchestrator's pod is reachable from the KC pod's namespace before relying on back-channel. Front-channel logout works regardless. |
| MCP tokens kept working but accidentally locked out by stricter validator | The `kind='mcp'` path is explicitly preserved. Add a test that an existing pre-migration MCP token round-trips. |
| CSRF middleware overreached and blocked in-cluster agent traffic | **Realised 2026-05-13–14, fixed 2026-05-14.** The middleware as designed enforced `X-CSRF: 1` on all non-safe methods with only a path allowlist + Bearer/X-Internal-Key bypasses. Agent-pod → orchestrator-pod calls have none of those, so every `POST /api/agents/register` and `POST /api/jobs/{id}/complete` 403'd silently for two days. Only stale, already-registered agents kept the cluster running. Fixed by short-circuiting CSRF before any header check when the request has no `srw_session` cookie — the cookie *is* the CSRF vector, so without it there's no browser-mediated session to forge. Header-based exemptions for Bearer and X-Internal-Key kept as defense-in-depth for hybrid requests. **Retro lesson**: the CSRF design's threat model named "cookie-authenticated browsers" as the target, but the implementation acted on "all non-safe requests by default" — those are not the same thing, and the implementation should follow the threat model. The CSRF tests covered the cockpit happy path but not the agent path; a smoke test that exercises a no-cookie POST should have caught this. |
| PAT scope-enforcement is permissive in v1; a PAT can do more than its scopes claim | Acknowledge in release notes. Phase 4 closes this. Until then, PATs should not be issued to untrusted automation. |
| Cookie domain on dev cluster (`localhost`) needs different handling than prod | The `SRW_COOKIE_DOMAIN` env defaults to unset (host-only) — works for localhost dev. Prod sets it explicitly. |
| CSRF middleware breaks legitimate state-changing requests with unusual user-agent | `Sec-Fetch-Site` is universally supported in browsers since March 2023; the only callers without it are non-browser clients which authenticate via PAT (Bearer header) and skip the CSRF check by design. |

## References

### Internal

- `docs/tests/headless_sessions_smoke.md` §P7 — the failing test that triggered this
- `docs/db_migration.md` — migration runner, advisory locks, file conventions
- `docs/features/mcp_oauth_bridge.md` — existing OAuth bridge for the MCP server
- `docs/features/sso_and_cloud_storage.md` — Keycloak SSO context
- `docs/features/headless_persistent_sessions.md` — the feature whose SSE migration uncovered this
- `CLAUDE.md` — note that auth is called inline at endpoint sites, not via FastAPI `Depends`

### External

- OWASP CSRF Prevention Cheat Sheet (Dec 2025 revision) — https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
- Duende BFF Security Framework — https://docs.duendesoftware.com/bff/
- oauth2-proxy session storage — https://oauth2-proxy.github.io/oauth2-proxy/configuration/session_storage/
- Skycloak: Keycloak BFF Pattern — https://skycloak.io/blog/keycloak-backend-for-frontend-bff-pattern/
- Auth.js Session Strategies — https://authjs.dev/concepts/session-strategies
- Keycloak RP-initiated logout (`id_token_hint`) — https://forum.keycloak.org/t/rp-initiated-logout-what-id-token-to-use-as-id-token-hint/15509
- GitHub: Behind GitHub's new authentication token formats — https://github.blog/engineering/platform-security/behind-githubs-new-authentication-token-formats/
- GitHub: Token expiration and revocation — https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/token-expiration-and-revocation
- Stripe API keys — https://docs.stripe.com/keys
- OWASP Password Storage Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- Permit.io: JWT vs Opaque Tokens — https://www.permit.io/blog/a-guide-to-bearer-tokens-jwt-vs-opaque-tokens
- Carbon Design: Generate an API key — https://carbondesignsystem.com/community/patterns/generate-an-api-key/
- Miguel Grinberg: CSRF protection without tokens — https://blog.miguelgrinberg.com/post/csrf-protection-without-tokens-or-hidden-form-fields
