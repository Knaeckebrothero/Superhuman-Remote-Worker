---
tags:
  - issue
  - mcp
  - orchestrator
  - auth
  - reliability
---

# MCP client: 30s timeout + retry reports slow mutations as failed, and auth headers live on a shared global client

**Status:** Filed 2026-08-01; PARTIALLY FIXED since (sweep-verified at HEAD
2026-08-06). The client relocated to `src/shared/orch_surface/client.py`
(commit `99b87008`, the shared-orch-surface extraction — the doc's
`orchestrator/mcp/client.py` path is gone). Defect 3 (shared mutable auth
headers) FIXED via per-request `_RequestScopeAuth` ContextVar auth; defect 2
(retry of non-idempotent mutations) FIXED via exactly-once
`_mutation_request()` + `MutationOutcomeUnknown` (~25 mutation sites routed
through it; retries now decorate reads only). Defect 1 (flat 30.0s client
timeout vs the ~30s teardown budget) NOT fixed — softened by defect 2's
ambiguous-outcome semantics, but the root timeout mismatch stands.

**One line:** `AsyncCockpitClient` gives every orchestrator call a 30.0s budget
and retries timeouts up to 3×; a mutation that takes >30s server-side (observed:
`DELETE /api/persistent/threads/{id}` at **30.8s**) is reported to the MCP user
as failed even though it committed — and the retry can go out **without auth
headers**, because per-user scope headers are mutated in place on one shared
global httpx client.

## Evidence (thread `b35346cf`, 2026-07-31 22:55 UTC)

| Request | What happened |
|---|---|
| `f2319609fac5` | MCP DELETE attempt 1 — permanent teardown, **200 in 30798ms** |
| `0435596b7d53` | tenacity retry ~1s after client timeout — **401 in 1ms** ("Not authenticated") |
| `a1f7e481ff72` | operator re-issued delete after the false failure — 404 (row already gone) |

The tool surfaced: `Action 'end_thread' failed ... Not authenticated` — for a
deletion that had committed 200ms earlier.

Why attempt 1 was slow: the DELETE's own detach poke made the agent PUT its
status; the *other* replica ran suspend-on-release (`e74683827b81`) and deleted
the agent pod under the DELETE's ~30s pre-teardown detach
(`RemoteProtocolError` → "proceeding"). Client budget (30.0s) ≤ server detach
budget (~30s) means the client always times out first on a slow detach.

## Defects

1. **Nested equal timeouts.** `httpx.AsyncClient(..., timeout=30.0)`
   (`orchestrator/mcp/client.py:401`) vs an endpoint whose internal detach
   timeout alone is ~30s. Any slow-but-successful mutation is reported as
   failed. Client budget must exceed the endpoint's worst-case internal budget
   (or the endpoint should 202 + poll).
2. **Retry of non-idempotent mutations.** `_create_retry_decorator()`
   (`client.py:22`) retries `TimeoutException` on **every** method, including
   creates/deletes. After a timeout the first attempt may still commit —
   double-fire for creates, false-conflict for deletes.
3. **Shared mutable auth headers.** `_get_client()`
   (`orchestrator/mcp/server.py:75-99`) sets/clears `X-MCP-User-Id` /
   `X-MCP-Scope` / `X-Internal-Key` on the **global** client's default headers
   per tool call. Concurrent MCP activity (another session, an unauthenticated
   request hitting the `clear` path) strips or **swaps** the identity between a
   call's attempts — observed as the retry's 401; in the worst case a request
   executes under a *different user's* identity (cross-user bleed —
   security-relevant, not just cosmetic).

## Fix sketch

- Pass scope headers per-request (`headers=` kwarg), never mutate the shared
  client's defaults.
- Raise the client timeout above the slowest endpoint budget, or make slow
  teardowns async (202 + status poll).
- Restrict the retry decorator to read-only methods, or make mutations
  idempotent-safe (idempotency keys / treat 404-after-delete as success).

## Related

- `docs/done/session_vm_backend_never_attaches.md` — re-gate 2026-08-01 where
  this fired (finding 2), including the suspend-on-release race that made the
  DELETE slow.
- `docs/issues/orchestrator_mcp_query_surface_too_coarse_for_investigation.md` —
  sibling MCP-surface quality issues.
