# Persistent graph — "Memory/Knowledge retrieval failed (Connection error)" logged on every turn even when the embedding endpoint works

## Symptom (observed 2026-05-11)

In agent test session `7d845b7e-…` running against a local
`qwen3-embedding-8b` server at `http://localhost:8090/v1`, every user
turn reproducibly logs the same six-line block before the LLM call:

```
2026-05-11 10:03:46 - openai._base_client     - INFO    - Retrying request to /embeddings in 0.387 seconds
2026-05-11 10:03:46 - openai._base_client     - INFO    - Retrying request to /embeddings in 0.792 seconds
2026-05-11 10:03:47 - src.persistent_graph    - WARNING - Memory retrieval failed (non-fatal): Connection error.
2026-05-11 10:03:47 - openai._base_client     - INFO    - Retrying request to /embeddings in 0.473 seconds
2026-05-11 10:03:47 - openai._base_client     - INFO    - Retrying request to /embeddings in 0.850 seconds
2026-05-11 10:03:48 - src.persistent_graph    - WARNING - Knowledge retrieval failed (non-fatal): Connection error.
```

Same fingerprint on turn 1, 2, 3, and again after session resume —
four times in a 17-minute session. About 1.5 s of latency is added to
every turn and both memory and knowledge injection silently no-op.

**But the embedding endpoint actually works.** Spot-checked directly,
`curl http://localhost:8090/v1/embeddings` with the same model name
returns 200 with a valid vector. So either:

1. The embedding *retrieval* path is broken in some way that doesn't
   affect the embedding *write* path (the observer that extracts and
   stores memories at end of turn would hit the same endpoint and is
   logged elsewhere as succeeding)
2. There's a transient connection glitch on the very first call after
   each turn, and the retry budget (default `max_retries=2` for
   `AsyncOpenAI`) isn't enough to ride it out
3. The two `EmbeddingService` consumers — `RecallStore.retrieve()` and
   `KnowledgeStore.hybrid_search()` — are racing the singleton's
   connection pool somehow

## Root cause (likely candidates, unverified)

The `WARNING` log line drops the original exception type — it just
reports `e` which `openai.APIConnectionError` formats as
"Connection error." with no detail. We don't know whether it's
ECONNREFUSED, ECONNRESET, a timeout, a TLS handshake failure, or
something downstream of `httpx.ConnectError`. That's the first thing
to fix.

Code path:

- `src/persistent_graph.py:350-363` — memory retrieval, wraps
  `recall_store.retrieve(context_text)` in `asyncio.wait_for(...)` and
  catches the bare `Exception`
- `src/services/recall_store.py:899` — calls
  `self.embedding_service.embed(context_text)` (the failing call)
- `src/services/embedding_service.py:73` — single shared `AsyncOpenAI`
  client constructed at module load with `base_url`+`api_key`
- `src/persistent_graph.py:380-398` — knowledge retrieval, same
  pattern but via `knowledge_store.hybrid_search(...)` which also
  embeds the query string

Both consumers use the *same* `EmbeddingService` singleton, so it's
not two different clients with different config. One singleton client,
two consecutive calls per turn, both fail with the same
`APIConnectionError`.

Possibilities worth ruling out in order:

1. **Per-call connection pool exhaustion.** `AsyncOpenAI` builds an
   `httpx.AsyncClient` with default pool limits. If the agent's other
   concurrent work (the cloud-sync poller making WebDAV calls every
   15 s, the orchestrator heartbeat every 60 s, the streaming chat
   completion) keeps the loop saturated, the embedding client's pool
   may briefly stall. Logged as "Connection error" rather than
   "PoolTimeout" because that's how `httpx.PoolTimeout` translates
   through the openai SDK retry layer.
2. **DNS resolution flake on `localhost`.** Unlikely on bare metal,
   but if the agent runs in any container/network namespace where
   `localhost` is interpreted differently between the embedding
   client and the test command, the discrepancy is real. Worth
   inspecting `/etc/hosts` and `getent hosts localhost` inside the
   agent process.
3. **Eager per-turn client recreation.** If something is recreating
   the `EmbeddingService` singleton between turns and the new client's
   first connection always fails (e.g. cached DNS warming), retries
   wouldn't help because they reuse the same broken connection. Seems
   unlikely given the singleton pattern, but worth confirming.
4. **HTTP/2 or keepalive issue on the embedding server.** Some local
   model servers (vLLM, llama.cpp's server) close idle connections
   after N seconds; the agent reuses a stale socket on the next
   embedding call and the first attempt fails. Retries within the
   same `AsyncOpenAI.embeddings.create()` call may retry on the same
   stale connection rather than a fresh one.

## Impact

- Memory injection (`RecallStore`) — the agent loses access to its
  TTL-pinned memories and prior-conversation hybrid search every turn.
  Defeats the entire memory-light feature.
- Knowledge injection (`KnowledgeStore`) — project-scoped knowledge
  notes are not surfaced. Agent operates with only the system prompt
  and the message history.
- ~1.5 s of dead latency per turn (the SDK does its full retry budget
  before the wrapping `try/except` catches and gives up).
- Silent in the cockpit. The user has no signal that semantic memory
  is degraded; the agent just appears occasionally forgetful.

## Suggested investigation steps

1. **First thing — log the exception type and message.** One-line
   change in `src/persistent_graph.py:362` and `:397`:

   ```python
   except Exception as e:
       logger.warning(
           "Memory retrieval failed (non-fatal): %s: %s",
           type(e).__name__, e,
       )
   ```

   This turns "Connection error." into something diagnosable
   ("APIConnectionError: All connection attempts failed",
   "PoolTimeout: …", etc.). Cheap and immediately actionable.

2. **Wrap the embedding call with a one-shot probe at startup.**
   `EmbeddingService.__init__` could async-fire a tiny embedding
   request and log success/failure once — would tell us whether the
   first per-turn call has a different failure mode than steady-state.

3. **Increase `max_retries` and add explicit `httpx.Timeout`.** The
   default `max_retries=2` with default exponential backoff caps total
   retry time at ~1 s. If the local embedding server warm-up is
   slower than that, every cold call fails. Bump to 4 with a
   `connect=2.0, read=10.0` timeout and see if the warnings vanish.

4. **Add a circuit breaker.** Once we *do* know it's failing, stop
   spending 1.5 s per turn on the retry budget. Track recent failures
   in `EmbeddingService` and short-circuit calls for ~5 minutes after
   N consecutive errors — log once on circuit-open and once on
   circuit-close.

## Cockpit-side surfacing (separate concern)

Memory and knowledge retrieval failing should not be silent. The
persistent_graph already gracefully degrades, but a cockpit indicator
("Semantic memory unavailable — using session messages only") would
let the user understand why the agent forgets things across turns.

A small WS event on first failure of a session would do it — agent
emits `{"method":"capability.degraded","feature":"memory","reason":"…"}`
once, cockpit shows a discreet badge near the session title until it
recovers.

## Related code

- `src/persistent_graph.py:350-398` — both retrieval blocks (memory + knowledge)
- `src/services/embedding_service.py` — singleton + `embed()`
- `src/services/recall_store.py:857-911` — `retrieve()` calls `embed()`
- `src/services/knowledge_store.py:314-370` — `hybrid_search()` calls `embed()`
- `src/api/persistent_app.py` — `EmbeddingService` initialization log
  at line referenced by 10:03:16 (`Embedding override applied …`)

## Decision

**Step 1 shipped 2026-05-11** — `src/persistent_graph.py` now logs
`type(e).__name__: <message>` for both memory and knowledge retrieval
failures. Next time the warnings recur in agent logs, the actual
exception type (`APIConnectionError`, `PoolTimeout`, `ReadTimeout`,
etc.) will be visible and the right next step from the suggestion list
above (probe at startup, retry/timeout tuning, circuit breaker, cockpit
indicator) becomes obvious. Steps 2-4 are deliberately deferred until
we have that data — picking a fix without knowing the failure mode is
guesswork.

Re-trigger the bug (any persistent session against the embedding
endpoint) and grep `agent_log.txt` for "Memory retrieval failed" or
"Knowledge retrieval failed" — the line will now name the exception
class.
