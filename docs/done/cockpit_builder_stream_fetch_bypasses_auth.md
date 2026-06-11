# Cockpit builder-stream `fetch()` bypasses auth interceptor

## Symptom (observed 2026-05-14 during auth-layer audit)

The cockpit's `BuilderStreamService.streamMessage()` uses a raw `fetch()` call to consume the builder AI's SSE stream. It does not go through Angular's `HttpClient`, which means it bypasses `authInterceptor` and **does not attach an `Authorization` header**:

```ts
// cockpit/src/app/core/services/builder-stream.service.ts:136
const response = await fetch(
  `${this.baseUrl}/builder/sessions/${sessionId}/message`,
  {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: this.abortController.signal,
  },
);
```

The receiving orchestrator endpoint also has no auth check:

```python
# orchestrator/main.py:17319
@app.post("/api/builder/sessions/{session_id}/message")
async def send_builder_message(
    session_id: str,
    body: BuilderMessageRequest,
) -> StreamingResponse:
    # Verify session exists
    session = await postgres_db.get_builder_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # ... no require_approved_user / get_current_user ever called
```

This makes the builder family of endpoints **anonymously accessible** in any deployment where they're reachable.

## Affected endpoints

All under `/api/builder/`:

| Verb | Path | Auth check today | Notes |
|---|---|---|---|
| POST | `/api/builder/sessions` | none | Creates a builder session |
| GET | `/api/builder/sessions` | none | Lists |
| GET | `/api/builder/sessions/{id}` | none | Reads |
| GET | `/api/builder/sessions/{id}/messages` | none | Reads history |
| POST | `/api/builder/sessions/{id}/message` | none | Sends message, returns SSE stream |

A `grep` of the orchestrator shows no `require_approved_user`, `get_current_user`, or `_require_admin` calls anywhere in the `/api/builder/*` handler bodies. The endpoints have been live since the builder feature shipped (see `docs/features/builder.md`).

## Root cause

Two independent gaps stacked:

1. **Cockpit side.** The builder stream was implemented with a raw `fetch()` because the developer needed `response.body.getReader()` to consume the SSE chunks — Angular's `HttpClient` doesn't expose `ReadableStream` directly. `fetch()` works, but bypasses the interceptor chain, so the Keycloak Bearer token never gets attached.
2. **Orchestrator side.** The receiving endpoint was never wired through `require_approved_user`. Probably an oversight from when the builder feature was prototyped — every other endpoint family in `main.py` calls `require_approved_user` inline at the top of each handler.

Either gap alone would fail safely (the cockpit can't reach an authenticated endpoint with no token; or the orchestrator would 401 a tokenless request). Both together produce a working-but-unauthenticated channel.

## Impact

- Anyone who can reach `api.superhuman-remote-worker.com` and knows or guesses a `builder_session_id` (UUID — high entropy, not enumerable in practice) can send messages to the builder LLM. With LLM token budget attached, this is the typical "unauthenticated LLM proxy" risk.
- Session creation is unauthenticated — an attacker can mint a new session UUID at will. Doesn't even need to guess.
- Cluster blast radius: production is behind Cloudflare and HTTPS but not behind any IP allowlist; the API is publicly reachable. Anyone on the internet who finds the API host can spend LLM tokens on the builder.
- Severity: **medium–high.** Genuine unauthenticated LLM access. Mitigated by the API host not being publicly advertised, but that's security-through-obscurity.

## Resolution

Two independent fixes; both are needed.

### A. Orchestrator: gate every `/api/builder/*` endpoint

Add `await require_approved_user(request, postgres_db)` at the top of all five handler bodies in `orchestrator/main.py`. Three lines per endpoint; ~15 lines total. Mechanical change; mirrors the pattern used in every other endpoint family.

Owner-scope each session: when a user creates a builder session, store `user_id` on the session row. Every subsequent handler checks `session["user_id"] == current_user["id"]` and 403s otherwise (mirrors the persistent-thread ownership check at `main.py:10892`).

### B. Cockpit: attach Authorization to the `fetch()`

Two options, easy:

1. **Inline.** Call `await this.keycloakService.getToken()` and add `Authorization: Bearer <token>` to the fetch headers manually. ~5 lines.
2. **Once the auth refactor lands** ([auth_bff_and_api_tokens.md](../features/auth_bff_and_api_tokens.md)), add `credentials: 'include'` to the fetch and let the `srw_session` cookie ride along. Same shape as `EventSource`.

Path 2 is the cleaner long-term answer because it stops the cockpit from holding the token at all. Path 1 is a 5-line stopgap if we need to land this before the auth refactor.

## Why this is a separate doc, not part of the auth refactor

The auth refactor (cookie BFF + API token consolidation) closes the cockpit-side gap as a side effect — once the cockpit uses cookies, `credentials: 'include'` on the fetch will carry them. But the orchestrator-side gap (`/api/builder/*` having no auth check) is independent: it's a missing `require_approved_user` call, not anything to do with the auth mechanism. That fix is small, mechanical, and can ship at any time.

Tracking this separately so it doesn't get rolled into the auth refactor PRs and lost.

## References

- `cockpit/src/app/core/services/builder-stream.service.ts:136` — the raw `fetch()` call
- `orchestrator/main.py:17212-17400` — the unauthenticated builder endpoint family
- `docs/features/builder.md` — feature design
- `docs/features/auth_bff_and_api_tokens.md` — long-term cockpit-side fix (cookies)
