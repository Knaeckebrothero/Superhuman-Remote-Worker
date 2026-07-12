# Cockpit file uploads fail (`net::ERR_FAILED`) — the service worker re-issues the multipart POST and the body doesn't survive

**Status:** Fix **IMPLEMENTED on `develop`** (2026-07-11, TDD, uncommitted) · unit tests green (6 new interceptor specs + full 884-test suite) · prod-build + SW-controlled browser harness verified the fixed path end-to-end (multipart intact through CORS preflight; GETs fine; trimmed `ngsw.json` boots clean) · **local control could NOT reproduce the original failure** (SW re-issue survives on plain HTTP/1.1-to-localhost; original repro was HTTPS/`api.srw.works`) → **final confirmation = live dev-cluster upload after deploy, still pending**
**Found:** 2026-07-07, live session `02a59ae2-e118-433d-90bc-0a46a83c0c26` (dev cluster, `api.srw.works`). Investigated 2026-07-07 → 2026-07-10.
**Severity:** High — **every file upload through the cockpit is broken whenever the service worker is active** (i.e. all production builds; the SW is `enabled: !isDevMode()`). Blocks attaching files to a persistent session *and* the job-creation upload paths. No readable error — surfaces as a generic "Network error — check your connection", which wrongly implicates the user's connectivity.
**Component:** cockpit Angular service worker (`@angular/service-worker` / `ngsw-worker.js`) · `cockpit/ngsw-config.json` · `cockpit/src/app/core/interceptors/auth.interceptor.ts` · orchestrator upload endpoint (`orchestrator/main.py:18882` → `orchestrator/services/thread_uploads.py`)
**Related:** [[resumed_session_dead_stream_and_supervised_gate_timeout_as_denial]] (item 3 "SW `/api/**` freshness-cache audit" — **this doc corrects that doc's prescribed fix**) · [[persistent_session_idle_expiry_message_swallow]] · [[session_reliability_investigation_index]] · README "Service Worker hijack" note

## Symptom (user-reported, then traced)

Attach a file in a persistent session and send. The message text sends fine, but the file upload fails. The composer shows **"Upload failed (HTTP 504) — try again"** (offline) or **"Network error — check your connection"** (online), and the attachment is never delivered to the agent. The session itself is healthy the whole time (thread `active`, workspace `ready`, `POST /input` accepted, agent replies).

The user first hit this on a train (bad mobile network) and reasonably assumed connectivity. It is **not** connectivity — it reproduces on a good connection, and only the *upload* fails while every other API call succeeds.

## Network + console evidence (DevTools)

Two distinct signatures depending on connectivity, both with the service worker in the initiator chain:

| Condition | `POST …/uploads` | Companion row | Timing |
|---|---|---|---|
| **Offline** (train) | `504` (synthetic) — initiator `ngsw-worker.js` | `(failed) net::ERR_…` | ~6–10 ms (not a real gateway timeout) |
| **Online** (good link) | `net::ERR_FAILED`, `status: 0`, `statusText: "Unknown Error"` | — | instant |

Console (online):
```
The FetchEvent for "https://api.srw.works/api/persistent/threads/02a59ae2…/uploads"
  resulted in a network error response: the promise was rejected.
Uncaught (in promise) TypeError: Failed to fetch
    at DataGroup.safeFetch (ngsw-worker.js:1081:27)
    at DataGroup.handleFetch (ngsw-worker.js:913:23)
    at async AppVersion.handleFetch (ngsw-worker.js:1202:20)
    at async Driver.handleFetch (ngsw-worker.js:1845:19)
POST https://api.srw.works/api/persistent/threads/02a59ae2…/uploads net::ERR_FAILED
```
Meanwhile `POST /input`, `POST /citations`, `POST /interrupt` (all JSON, same origin, same cookie + `X-CSRF`) return **200**. The only request that fails is the one with a **multipart/form-data** body.

## Root cause

Two facts, both verified against the shipped `@angular/service-worker` source in `cockpit/node_modules`.

### 1. The service worker re-issues **every** request through `scope.fetch()` — group config only controls *caching*, not *handling*

Once the SW controls the page, `Driver.onFetch` calls `event.respondWith(this.handleFetch(event))` for every request that isn't explicitly bypassed (`ngsw-worker.js:1633`). When no asset/data group matches, `AppVersion.handleFetch` returns `null` (`:1221`), and the driver falls through to:

```js
// Driver.handleFetch, ngsw-worker.js:1858-1860
if (res === null) {
  return this.safeFetch(event.request);   // → Driver.safeFetch (:2257) → return await this.scope.fetch(req)
}
```

So **narrowing or excluding `/api/**` from a data group does NOT take a request out of the worker** — an unmatched request is *still* re-issued via `scope.fetch(req)`. The group config decides only whether the *response* is cached.

The **only** mechanism that keeps a request out of the SW is the `ngsw-bypass` header or `?ngsw-bypass` query param — the sole early `return` in `onFetch` that skips `respondWith` (`ngsw-worker.js:1611`):
```js
if (req.headers.has("ngsw-bypass") || /[?&]ngsw-bypass(?:[=&]|$)/i.test(requestUrlObj.search)) {
  return;   // browser handles it natively — the normal, no-SW path
}
```

### 2. A multipart body doesn't survive the SW's `scope.fetch(req)` re-issue; a JSON body does

The failing request enters via the `/api/**` `api-cache` data group, but as established above the group is incidental — the SW would re-issue it regardless. For **JSON POST** bodies the re-issue succeeds (`/input`, `/citations` work). For a **multipart/form-data** body it rejects with `TypeError: Failed to fetch` → `net::ERR_FAILED` / `status: 0`. The multipart streamed body does not survive being handed to `scope.fetch` from inside the worker.

The server side is **symmetric** between the working and failing calls and is therefore *not* the differentiator: `auth.interceptor.ts:40-44` stamps `withCredentials` + `X-CSRF: 1` on all non-safe methods (uploads included, via `HttpClient`), and orchestrator CORS (`main.py:6212`) is uniform. The upload endpoint (`orchestrator/main.py:18882` → `services/thread_uploads.py`, SFTP into the workspace) never even runs — the request fails before reaching it.

### Aggravating detail: the error surface is uglier than it should be

`DataGroup.safeFetch` does `return this.scope.fetch(req)` **without awaiting inside its `try`** (`ngsw-worker.js:1079-1088`), so an async rejection escapes the `catch`. For POSTs (which skip the freshness/timeout path and hit `safeFetch` directly at `:913`) this means the rejection propagates as *"the promise was rejected"* instead of a graceful fallback — so a plain failed upload reads as a hard network error.

## Why the previously-prescribed fix is wrong (correction to [[resumed_session_dead_stream_and_supervised_gate_timeout_as_denial]] item 3)

That doc's follow-up #1 recommended replacing the per-URL `?ngsw-bypass=true` allowlist with an **`ngsw-config.json` `dataGroups.urls` `!`-negation exclusion**. That is wrong on two independent counts:

1. **ngsw does not support `!`-negation in `dataGroups.urls`.** The config generator only strips `!` for `assetGroups.resources.files` (`globListToMatcher`, `ngsw-config.js:175-180`) and `navigationUrls` (`:159-161`). `dataGroups.urls` is mapped straight through `urlToRegex` with no negation branch (`ngsw-config.js:144`), so `!/api/**/uploads` compiles to a regex that simply never matches — a silent no-op.
2. **Even a working exclusion would not fix uploads.** Per root-cause #1, excluding a URL from a data group only stops *caching*; the SW still re-issues it via `Driver.safeFetch`. The multipart re-issue would break exactly the same way.

The correct lever for "must reach the network untouched" is `ngsw-bypass`, which this codebase already uses for its SSE streams (`persistent-chat.service.ts:935`, `api.service.ts:1175`). **File uploads are the same category as SSE streams / WebSocket handshakes / binary downloads: requests that cannot survive being re-issued by the worker.**

## The fix (approved direction — keep the PWA service worker)

Product decision 2026-07-10: the installable/offline-load PWA experience is wanted, so we keep the SW (`assetGroups` app-shell caching untouched) and systematically bypass the request class that must reach the network raw.

**Change 1 — centralized mutation bypass (`auth.interceptor.ts`).** Where it already stamps `X-CSRF` on non-safe methods, also set the bypass header:
```ts
if (!isSafe) {
  modified = modified.clone({
    headers: modified.headers.set('X-CSRF', '1').set('ngsw-bypass', '1'),
  });
}
```
Rationale: mutations are *never* cached by ngsw (data groups cache GET/HEAD only), so re-issuing them through the worker is pure liability. Bypassing all non-safe methods fixes uploads **and every future POST/PUT/DELETE/PATCH** by construction, in one place. The request goes out natively (the normal no-SW path) — CORS/preflight/credentials behave as usual (mutations already carry the non-safelisted `X-CSRF` header, so they were already preflighted; `allow_headers=["*"]` covers `ngsw-bypass`).

**Change 2 — stop blanket-caching the API (`ngsw-config.json`).** Delete the `api-cache` `/api/**` data group. It stales stateful GETs (the `/api/sessions/{id}/connection` handshake, binary downloads — item 3's original concern) and buys ~nothing for a live-agent UI (1 h stale cache behind a 5 s timeout). GETs continue to work as uncached passthrough. The `assetGroups` (PWA app-shell) and the `runtime-config` (`env.js`) data group are left intact.

Net: ~1 line added + ~10 lines of config removed.

## Verification plan (non-optional — two prior code-only rounds looked right and weren't)

The SW only runs in production builds (`app.config.ts:120`, `enabled: !isDevMode()`), so a dev-mode check proves nothing.

1. Production `ng build` of the cockpit (needs `npm i --no-save @monaco-editor/loader`; watch the `persistent-chat.scss` 32 kB budget ceiling). Confirm the regenerated `ngsw.json` no longer contains the `api-cache` data group.
2. Serve the prod build, let the SW register, drive a **real** multipart upload. Confirm: `…/uploads` returns **200**, initiated by native network (not `ngsw-worker.js`), and the file lands in the workspace `uploads/` dir.
3. Regression: confirm chat `POST /input` and history GETs still work with the SW active.
4. Deploy: push `develop` → CI (`sha-XXX`) → dev cluster; re-confirm on a live session.

## Follow-ups (out of scope for this bug)

- **Binary-download GETs** (`/api/uploads/*/files/*`, `/api/citations/*/snapshot`, `/api/skills/*/export`) still pass through the SW uncached. Harmless now that nothing caches them, but if a raw bypass is ever wanted they need the query-param form (a custom header on a GET would newly force a CORS preflight). Small, tracked, not part of this fix.
- **Migrate the SSE `?ngsw-bypass=true` allowlist** toward the same centralized philosophy so a newly-added streaming route isn't silently broken (the original spirit of item 3 follow-up #1 — just via the mechanism that actually works).
- Consider filing the `DataGroup.safeFetch` non-awaited `try/catch` (`ngsw-worker.js:1079`) upstream to `@angular/service-worker` — it turns any failed non-GET into an unhandled rejection rather than a graceful fallback.

## Investigation log

- **2026-07-07** — Reported on live session `02a59ae2` (screenshots: offline `504`, then online `net::ERR_FAILED`). Confirmed thread healthy via orchestrator MCP (`active`, workspace `ready`). Initial hypothesis (offline SW synthetic 504) corrected once online evidence showed the backend and other POSTs were fine.
- **2026-07-10** — Reviewed cockpit changes made in the interim (SSE reliability / outbox / composer / mobile). None touched the two fix-sites; `registrationStrategy` changed to `registerImmediately`, which makes the SW engage *sooner*. Traced the ngsw source end-to-end: established that (a) the SW re-issues all requests, (b) `dataGroups.urls` negation is unsupported, (c) `ngsw-bypass` is the only real opt-out. Product decision to keep the PWA → approach (a). Fix designed; implementation + live verification pending.
- **2026-07-11** — **Implemented** (TDD: new `auth.interceptor.spec.ts`, watched the 2 `ngsw-bypass` cases fail, then the one-line interceptor change + `api-cache` dataGroup deletion). Full vitest suite 884/884. Prod `ng build` clean; generated `ngsw.json` has dataGroups `[runtime-config]` only, zero `/api/**` patterns. **Browser-harness verification** (Playwright against the prod build on `localhost:4000`, real activated SW controlling the page, cross-origin stub API on `:8085`): multipart POST **with** `ngsw-bypass` + `X-CSRF` → 200, 307,382 bytes byte-intact through the CORS preflight; plain GET through the SW → 200. **Caveat:** the control POST *without* the header also succeeded locally — the SW's `scope.fetch` re-issue survives plain HTTP/1.1-to-localhost, so the original `net::ERR_FAILED` (seen on HTTPS/`api.srw.works`) did not reproduce in the rig. The harness proves the fixed path works and nothing regressed; it cannot prove the header is the load-bearing difference. Live dev-cluster upload after deploy remains the final arbiter.
