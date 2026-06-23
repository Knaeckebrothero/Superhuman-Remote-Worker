# Idle session swallows the typed message on send (BFF idle-expiry → hard re-login reload)

**Status:** Fix implemented on `fix/bff-idle-session-message-swallow` (2026-06-23) · root cause **confirmed + isolated to the idle branch** + **reproduced end-to-end on k3d** · pending k3d e2e verification of the fix → merge to `develop`
**Found:** 2026-06-23, reproduced live on session `6810288e` (main cluster)
**Severity:** High annoyance, data loss — a carefully typed message (often minutes of work) is silently lost; no crash, no error the user can read.
**Component:** cockpit auth interceptor + composer · orchestrator BFF session validator (`security/auth.py`) · Angular service worker
**Related:** [[persistent_chat_silent_disconnect]] · [[persistent_session_empty_chunk_history_corruption]] · README "Service Worker hijack" troubleshooting note

## Symptom (user-reported, then reproduced)

Leave a persistent session open and idle for a while (≈30 min — see config
below). The page still *looks* live: green "Connected" dot, composer accepts
typing. You type a message (sometimes several minutes of work), hit Enter, and:

- a red error flashes for ~200 ms — too fast to read,
- the page **refreshes**,
- the typed message is **gone** — never sent, not in history, not in the
  composer. It is silently swallowed.

After the refresh the session works normally again, as if nothing happened.
Browser Network shows the `/api/.../input` request returning **401**.

## Network evidence (DevTools, session `6810288e`)

Captured with Network "Preserve log" on (console was lost to the refresh):

| Name | Status | Type | Initiator |
|---|---|---|---|
| `input` | **401** | fetch | (ServiceWorker) |
| `input` | 200 | preflight (OPTIONS) | Preflight |
| `input` | **401** | fetch | `ngsw-worker.js:1081` |
| `login?return_to=%2Fsessions%2F6810288e-1cd5-45…` | 302 | document | `api.superhuman-remote-worker…` |
| `auth?response_type=code&client_id=cockpit-bff&…` | 302 | document | `api.superhuman-remote-worker…` |
| `callback?state=…` | 302 | document | `auth.superhuman-remote-worker…` |
| `6810288e-1cd5-4572-a5f3-4cb3eb839980` | 200 | document | `api.superhuman-remote-worker…` |
| `env.js` | 200 | script | (ServiceWorker) |

The `POST /api/input` 401s, which immediately triggers the BFF OIDC re-login
(`login → auth → callback`), all **302s with no password prompt** (the Keycloak
SSO browser session is still alive), ending in a fresh document load — i.e. a
full SPA reload.

## Root cause

It is the **REST `POST /api/input`**, not the websocket. The orchestrator's BFF
session **idle-expired**, the cockpit turned the resulting 401 into a full-page
re-login, and the reload destroyed the in-memory draft.

Causal chain:

1. **Idle clock only advances on `/api` HTTP requests.** Each authenticated
   request fire-and-forgets `touch_srw_session_last_seen`
   (`orchestrator/security/auth.py:183`). The streaming control websocket and the
   agent→orchestrator heartbeats do **not** touch the *user's* BFF session, so a
   session page that's just sitting open makes no qualifying requests.
2. **After the idle window, the validator deletes the session and 401s.** On the
   next request, `_resolve_from_cookie` finds
   `last_seen_at + idle_timeout <= now`, **deletes the session row**, and returns
   `None` (`auth.py:153-155`) → `get_current_user` raises **401**. The
   `srw_session` cookie is still valid (30-day Max-Age) — it is the *server-side*
   session that is gone.
3. **The cockpit hard-redirects on any 401.** `authInterceptor` catches the 401
   and calls `session.login()`
   (`cockpit/src/app/core/interceptors/auth.interceptor.ts:48-50`), which sets
   `window.location.href = {api}/auth/login?return_to=…`
   (`cockpit/src/app/core/services/session.service.ts:47-51`) — a full page
   navigation.
4. **The reload eats the draft and abandons the POST.** The composer holds the
   text only in component state (`inputText`); the navigation unloads the SPA
   before anything can persist it, and the original POST is never retried.
   Re-login is instant/prompt-less because the **Keycloak SSO session is still
   alive**, so the app returns to the session looking fine — minus the message.

## Confirmed reproduction (k3d, 2026-06-23)

Reproduced end-to-end on the local k3d cluster, driving the prod-built cockpit
(`https://localhost`, login `test/test`) with Playwright and lowering
`SRW_SESSION_IDLE_TIMEOUT_S` (20s, then 120s) to shrink the idle window. The bug
fired in **three** independent ways, all the same mechanism:

**1. New Session POST swallowed (idle 20s, accidental).** Logged in, spent ~45s
filling the New-Session form (no `/api` calls), clicked Create. Orchestrator:

```
10:20:19.799  POST /api/persistent/threads  401   ← create swallowed
10:20:20.028  GET  /api/persistent/threads  200   ← after silent re-auth
```

The session was never created; the SPA bounced `/sessions/_creating → /sessions`
with console `AbortError: Transition was skipped`.

**2. Navigation after idle (deliberate, deterministic).** Idle >20s, then clicked
the Jobs nav link:

```
10:23:06.488  GET /api/jobs       401
10:23:06.504  GET /auth/login     302
10:23:06.549  GET /auth/callback  302
10:23:06.654  GET /api/auth/me    200
10:23:06.785  GET /api/jobs       200   ← succeeds after reload
```

Full chain in **~300 ms** — exactly matching the user's screenshot
(`401 → login 302 → auth 302 → callback 302 → 200`) and the "~200 ms flash".

**3. Literal composer-message swallow (idle 120s).** Resumed a live session
(gemma-4-moe, "Connected"), typed a 140-char message, idled 130s, pressed Enter:

```
10:27:52.225  POST /api/persistent/threads/330d4d73…/input  401   ← the send, REJECTED
10:27:52.239  GET  /auth/login                              302
10:27:52.346  GET  /auth/callback                           302
10:27:52.480  GET  /api/auth/me                             200
10:27:52.554  GET  …/messages                               200   ← reloaded, re-fetches history
10:27:52.589  GET  …/stream                                 200   ← reconnects
```

Client-side after the reload (Playwright `evaluate`): `composer_value: ""`,
`message_in_history: false` — **the message was swallowed**. Console (captured
across the reload with preserve-log) showed the chain end to end:

```
[ERROR] Failed to load resource: 401 @ .../threads/330d4d73…/input   ← the send
[ERROR] AbortError: Transition was skipped @ …/@angular_router.js:3516 ← the ~200ms flash
[ERROR] 401 @ /api/auth/me, /api/models, /api/persistent/threads, /api/projects, /api/jobs …
[ERROR] 425 (Too Early) @ /api/sessions/330d4d73…/connection (×9)     ← WS reconnect racing re-auth
```

**Key finding:** the unreadable ~200 ms error the user reported is
**`AbortError: Transition was skipped`** — Angular's in-flight router transition
being aborted by the interceptor's `window.location.href` redirect. It is the
visible signature of this bug.

### Cause isolated to the idle branch (`auth.py:153-155`)

To rule out the *other* 401 sources, a controlled test at the **real 1800s
timeout** (not the lowered repro value): take the browser's live session row and
backdate **only** `last_seen_at` to 31 min ago, leaving the access token and
absolute lifetime valid. Then issue one request.

```
before:  idle_for 00:31:00 | access_still_valid = t | absolute_still_valid = t
10:35:26.442  GET /api/auth/me  401   ← session rejected on first call
10:35:26.470  GET /auth/login   302   ← interceptor redirect → reload
10:35:26.526  GET /auth/callback 302
10:35:26.874  GET /api/jobs     200   ← succeeds after re-auth
after:   SELECT count(*) FROM srw_sessions WHERE id=<that row> → 0   (DELETED)
         a fresh session row (different id) appears from the re-auth
```

The row was **deleted while the access token and absolute lifetime were both
still valid** — the unique signature of `_resolve_from_cookie`'s idle branch
(`last_seen_at + _idle_timeout() <= now` → `delete_srw_session` → `None` → 401,
`auth.py:153-155`). This **confirms the trigger is idle expiry specifically**,
not access-token expiry (`auth.py:158`), absolute-lifetime (`:149`), or a refresh
failure (`:160`). The controlled-variable runs above corroborate it: failure
timing tracked `SRW_SESSION_IDLE_TIMEOUT_S` exactly (20s → ~20s, 120s → ~120s).

## Why "refresh the token and retry" can NOT recover the message

The obvious fix — intercept the 401, silently refresh, replay the request — does
**not** work here, because the idle branch **deletes the session row**
(`auth.py:154`) before returning. A follow-up `POST /auth/refresh` would then hit
`get_srw_session → None → 401 "Session not found"` (`orchestrator/auth/bff.py:357-360`).
The full re-login redirect is genuinely required to re-establish a session via
the live KC SSO. (Server-side transparent refresh at `auth.py:158-161` only
covers access-token-near-expiry on an *otherwise-live* session; it never runs
once the session is idle-dead.)

Consequently the message can only be preserved by **(a) preventing idle expiry**
or **(b) persisting the draft across the reload** — not by client-side
refresh-retry.

## Existing partial mitigation that fails for this case

`persistent-chat.component.ts` (~1948-1962) already tries to protect the draft:
it captures `text`, clears `inputText` immediately, calls `chat.sendMessage(text)`,
and **restores** the draft only if the call resolves `ok === false` and the user
hasn't typed something new. This survives a *soft* failure (a send that returns
false without navigating) but **not** a `window.location` re-login: the page
unloads before the `.then()` runs, and component state doesn't survive a reload
regardless.

## Service worker note

`cockpit/ngsw-config.json` registers a `dataGroups` entry caching `/api/**`
(`strategy: freshness`), so `ngsw-worker.js` is the initiator on the `input`
fetch and relays the 401. The SW is **not** the root cause — it neither generates
the 401 nor forces the reload — but it is in the request path and explains the
`(ServiceWorker)` / `ngsw-worker.js:1081` initiators in the trace. (See also the
README's separate "Service Worker hijack of WebSocket handshakes" gotcha.)

## Verified configuration (main cluster, `srw-orchestrator`)

```
SRW_SESSION_IDLE_TIMEOUT_S       = 1800       # 30 min  ← the idle window
SRW_SESSION_ABSOLUTE_TIMEOUT_S   = 2592000    # 30 days (cookie Max-Age)
SRW_ACCESS_TOKEN_REFRESH_SKEW_S  = 60         # transparent access-token refresh window
SRW_COOKIE_DOMAIN                = .superhuman-remote-worker.com
SRW_COOKIE_SAMESITE              = lax
SRW_COOKIE_SECURE                = 1
```

The user's "probably more than 10 minutes" maps to the real **30-minute idle
timeout**. (Code defaults are identical: `auth.py:45` idle=1800,
`bff.py:67` absolute=2592000.)

## Key code references (develop @ `18268c8d`; line numbers may drift)

- `orchestrator/security/auth.py:44-45` — `_idle_timeout()` ← `SRW_SESSION_IDLE_TIMEOUT_S` (1800)
- `orchestrator/security/auth.py:149-155` — absolute + **idle** expiry → delete session → `None`
- `orchestrator/security/auth.py:158-161` — transparent access-token refresh (live sessions only)
- `orchestrator/security/auth.py:183` — `touch_srw_session_last_seen` (the only idle-clock reset)
- `orchestrator/auth/bff.py:111-116` — `srw_session` cookie, `max_age = absolute lifetime` (30 d)
- `orchestrator/auth/bff.py:345-364` — `/auth/refresh` (401s if session already gone)
- `cockpit/src/app/core/interceptors/auth.interceptor.ts:46-54` — 401 → `session.login()` (no retry)
- `cockpit/src/app/core/services/session.service.ts:44-51` — `login()` → `window.location.href`
- `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts:~1948-1962` — send + restore-on-soft-fail
- `cockpit/src/app/core/services/persistent-chat.service.ts:1040-1092` — separate WS `4401` close → reopen with fresh token
- `cockpit/ngsw-config.json` — `dataGroups` `/api/**` (SW in request path)

## Fix implemented (2026-06-23)

Shipped the **root-cause** server change plus the **draft-persistence net**, and
**dropped the keep-alive heartbeat** — the server fix makes it redundant (it was
treating the symptom, not the cause).

**A. Server — idle re-validates instead of deleting** (`orchestrator/security/auth.py`).
`_resolve_from_cookie` no longer blind-deletes an idle session. The idle check now
*falls into* the existing `_refresh_session_in_place` (the same refresh the
near-expiry path already uses): while Keycloak's SSO session is alive the BFF
session is renewed in place — a *renewable lease* over the KC SSO session — so the
request succeeds with **no 401, no redirect, nothing to recover**. Only a genuine KC
rejection (SSO ended / refresh revoked) or the absolute-lifetime cap ends it. Also
made `_refresh_session_in_place` keep the in-memory `sess` authoritative (it wrote
new tokens to the DB but left a stale `id_token` in `sess` for the downstream
identity-claim merge). Covered by `tests/test_bff_session_auth.py` — 9 cases:
idle+live renews silently, idle+dead deletes, absolute hard-stops with no refresh,
near-expiry still refreshes, fully-valid no-ops, missing/invalid/no-sub paths, and
the id_token write-back.

> **Concurrency invariant:** a tab refocus can fan out concurrent refreshes of the
> same refresh token. This is safe only while Keycloak refresh-token rotation is OFF
> (`revokeRefreshToken=false` — verified in the `home` and `srw` realms + the helm
> default). Pinned in a code comment at the refresh branch; an `asyncio.Lock` keyed
> by session id is the documented follow-up if rotation is ever enabled or the
> orchestrator goes multi-replica. Bonus: the WebSocket cookie path
> (`resolve_ws_user` → IDE/code-server proxy) inherits the same renewal for free.

**B. Client — draft persistence** (`cockpit/.../persistent-chat.component.ts`).
The composer text is persisted to `sessionStorage` (key `cockpit:draft:<threadId>`)
on every keystroke, restored when a thread loads (empty-guarded so it never clobbers
in-progress text), and cleared once a send is in flight (re-persisted on a hard send
failure). This is the un-loseable net for a *genuine* expiry — KC SSO truly dead, a
server restart, a network drop mid-send — where a re-login is unavoidable: the
redirect still happens, but the typed text returns after the reload. Exported
`saveDraft` / `loadDraft` / `clearDraft` / `draftKey` helpers are unit-tested in
`persistent-chat.component.spec.ts`.

**Dropped — keep-alive heartbeat.** With (A) in place there's nothing to keep warm:
an idle session renews on its next request. A ping-to-stay-alive timer would have
been symptom treatment, and it can't cover a slept/discarded tab anyway.

The "Fix options" below are the **original menu**, retained for context — note (A)
was *not* among them; the root-cause reorder emerged from re-reading the validator's
ordering.

## Fix options (prioritized — original menu, superseded by the above)

- **P0 — Keep the session warm (prevent idle expiry).** While a cockpit tab is
  open, heartbeat an authenticated endpoint (e.g. `GET /auth/me`) every
  ~`idle/2` and on `visibilitychange → visible`. Each ping resets `last_seen_at`
  (`auth.py:183`), so an open session never idles out. Eliminates the bug for the
  "left it open" case. Caveat: a slept laptop / long-suspended tab (timers
  paused) can still expire — hence the next item.
- **P0 — Persist the draft across reloads.** Stash the composer text to
  `sessionStorage`/`localStorage` keyed by thread id as the user types (and
  defensively right before the interceptor redirects); restore on session load.
  Makes the message un-losable regardless of auth state — the real safety net.
- **P1 — Soften the 401 UX.** Before the hard redirect, save the draft and show a
  brief "session expired — signing you back in…" toast, so the reload is not a
  silent yank. Optionally write a breadcrumb to `localStorage` so the cause is
  visible post-reload.
- **Stopgap (not a fix).** Raising `SRW_SESSION_IDLE_TIMEOUT_S` widens the window
  but only delays the loss.

## Reproduction & diagnostics

- **Deterministic repro (local k3d):** set `SRW_SESSION_IDLE_TIMEOUT_S=30` on
  `srw-orchestrator`, log in `test/test`, open a session, idle 30 s, then
  type + Enter. Drive with Playwright / Chrome-DevTools-MCP to capture the
  console error + network without losing them to the reload. Same code paths as
  prod.
- **Capture the flashing console error manually:** DevTools → Console → ⚙ →
  enable **"Preserve log"** (separate from Network's), reproduce, copy the error.
  Or Network → right-click → "Save all as HAR with content".
