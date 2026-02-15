# KeyRing Rotation Issues

Investigation date: 2026-02-14
Job examined: `c429302c-a79a-4eec-a9c0-f066364c8af7` (gpt-5.2-pro, 3 keys configured)

## Summary

The KeyRing rotation mechanism is **completely dead code in production**. The root cause is an async/sync mismatch: the custom `ReasoningCapturingClient` (an `httpx.Client`) is only wired to the **sync** OpenAI client, but the graph exclusively uses `ainvoke()` which goes through the **async** OpenAI client — a default `httpx.AsyncClient` that has no key rotation, no Layer 0 token validation, and no reasoning content capture.

## Root Cause (Bug #0)

### The async client bypasses all custom HTTP logic

**Files:** `src/llm/reasoning_chat.py:393-399`, LangChain's `BaseChatOpenAI` init

```python
# reasoning_chat.py — only sets http_client (sync), NOT http_async_client (async)
reasoning_client = ReasoningCapturingClient(...)
kwargs["http_client"] = reasoning_client   # ← sync only
# kwargs["http_async_client"] is NEVER set
super().__init__(**kwargs)
```

LangChain's `BaseChatOpenAI` creates two separate OpenAI SDK clients:

```python
# langchain_openai/chat_models/base.py (lines 479-512)

# Sync client — gets our ReasoningCapturingClient ✓
sync_specific = {"http_client": self.http_client, ...}
self.root_client = openai.OpenAI(**client_params, **sync_specific)

# Async client — gets a DEFAULT httpx.AsyncClient ✗
async_specific = {"http_client": self.http_async_client or _get_default_async_httpx_client(...), ...}
self.root_async_client = openai.AsyncOpenAI(**client_params, **async_specific)
```

The graph (`src/graph.py:653`) uses:
```python
response = await llm_with_tools.ainvoke(prepared_messages)
```

This routes through `_agenerate()` → `self.async_client.create()` → the **async** OpenAI client → the **default** `httpx.AsyncClient`.

**Runtime proof:**
```
>>> Sync client:  root_client._client type = ReasoningCapturingClient  ✓
>>> Async client: root_async_client._client type = _AsyncHttpxClientWrapper  ✗
>>> http_async_client = None
```

### What this means

ALL of the following features are dead code during normal graph execution:
1. **Key rotation** — `KeyRing.rotate()` is never called
2. **Layer 0 context overflow protection** — token counting before send never runs
3. **Reasoning content capture from HTTP** — `_last_reasoning_content` is always None
4. **Key injection** — `current_key` header override never happens

The `_agenerate` override in `ReasoningChatOpenAI` still runs (post-processing), but it relies on `_last_reasoning_content` which is never set. The Responses API reasoning extraction still works because it reads from LangChain's parsed message objects, not from HTTP interception.

### Why the KeyRing init message is missing from job logs

The `KeyRing[openai]: initialized with 3 keys...` INFO message is logged exactly once (singleton pattern). In API server mode (`--port 8001`), the first `get_or_create_key_ring()` call happens during initial agent startup — before any job-specific file handler exists. Subsequent calls for new jobs return the cached instance without logging. The init message goes to stdout/stderr only.

## Additional Bugs (Even After Fix)

### 1. `_rotate_and_retry` only retries once per `send()` call

**File:** `src/llm/reasoning_chat.py:343-358`

```python
def _rotate_and_retry(self, request, reason, **kwargs):
    new_key = self._key_ring.rotate(reason)
    if new_key is None:
        return None
    request.headers["authorization"] = f"Bearer {new_key}"
    return super().send(request, **kwargs)  # ONE retry, no loop
```

If key A fails, it rotates to B and retries once. If B also fails, the failed response is returned — key C is never tried in that pass. It relies on the OpenAI SDK's next retry (`max_retries=3`) to eventually reach C. This is fragile and couples KeyRing behavior to the SDK's internal retry count.

### 2. Singleton KeyRing doesn't detect key changes

**File:** `src/llm/key_ring.py:266-290`

```python
def get_or_create_key_ring(keys, provider="openai", ...):
    if provider in _registry:
        return _registry[provider]  # Returns cached KeyRing, ignores new keys
```

In API server mode (`--port 8001`), the KeyRing is created once and cached forever. If `OPENAI_API_KEY` changes between jobs (e.g., keys added or removed), the cached KeyRing keeps the old keys. The init message would also only appear in the first job's log, making debugging harder for subsequent jobs.

### 3. Race between SDK retries and KeyRing cooldowns

The retry layers interact poorly:

| Layer | Retries | Delay | Scope |
|-------|---------|-------|-------|
| KeyRing (`_rotate_and_retry`) | 1 per `send()` | None | Rotates to next key |
| OpenAI SDK (`max_retries=3`) | 4 total | SDK-controlled | Rebuilds request, calls `send()` again |
| graph.py (`tool_retry_count=3`) | 4 total | ~90s | Calls `llm.invoke()` again |

After 3 SDK retries, all 3 keys are on cooldown (1800s = 30 min). The graph.py retries (90s apart) then fail repeatedly because no keys are available. The cooldown window is too long relative to the graph.py retry interval.

### 4. Shared billing account makes rotation pointless for quota errors

OpenAI quotas are per-organization, not per-API-key. If all 3 keys belong to the same billing account, rotating between them has zero effect on `insufficient_quota` errors. Rotation only helps for per-key rate limits (requests/min), not billing exhaustion.

### 5. No key rotation for non-OpenAI providers

The KeyRing is only created in `_create_openai_llm`. Anthropic, Google, and Groq LLMs have no key rotation support. This is a limitation, not a bug, but worth noting for feature parity.

## Retry Flow (Current Behavior)

The retry flow documented below is **theoretical** — it would happen if the sync path were used. In practice, none of the KeyRing rotation logic fires because the async path bypasses it entirely.

For a job with 3 keys (A, B, C) all hitting `insufficient_quota`, the **actual** behavior is:

```
ainvoke() → AsyncOpenAI._request():
  send() #1 via default httpx.AsyncClient: key A → 429
    SDK sees 429 → sleep → retry
  send() #2 via default httpx.AsyncClient: key A → 429  (SAME key, no rotation)
    SDK sees 429 → sleep → retry
  send() #3 via default httpx.AsyncClient: key A → 429  (SAME key)
    SDK sees 429 → sleep → retry
  send() #4 via default httpx.AsyncClient: key A → 429  (SAME key)
    SDK gives up → raises RateLimitError
graph.py catches error → waits 90s → retries ainvoke()
  → same key A → same result → fails again
  → repeats 3 more times → gives up
```

Key A is used for ALL attempts. Keys B and C are never tried.

## Fix Required

Create an `AsyncReasoningCapturingClient` (subclass of `httpx.AsyncClient`) that mirrors the sync client's key injection and rotation logic. Pass it as `http_async_client` to `ChatOpenAI`:

```python
# In ReasoningChatOpenAI.__init__:
sync_client = ReasoningCapturingClient(...)
async_client = AsyncReasoningCapturingClient(...)  # NEW
kwargs["http_client"] = sync_client
kwargs["http_async_client"] = async_client  # NEW
```
