# `web_search` silently reports "No web results found" when Tavily returns an error (quota/432, auth, rate-limit)

**Status:** Filed — root cause confirmed on the live main cluster. The *quota* instance was fixed operationally (Tavily key usage limit raised, 2026-06-26); the **error-masking code defect is open**.
**Found:** 2026-06-26, during session `7692637b-9c60-4698-9875-b57ec34e66a6` (the agent said "the websearch tools don't work").
**Severity:** Medium. A hard tool failure (quota exhausted, bad key, rate-limit) is reported to the LLM as an ordinary empty result, so the agent can't tell "the tool is broken" from "there genuinely are no results." It burns retries on query variants and misleads the user; the real cause (an over-quota key) stays invisible.
**Component:** `src/tools/research/web.py` — `_direct_web_search` and the `Extract` / `Crawl` / `Map` siblings (same `response.get(...)`-only pattern).
**Related:** same "exhausted external capacity surfaced as a benign-looking empty/timeout" family as `loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md` (Defect C) and the gate-timeout-as-denial in `resumed_session_dead_stream_and_supervised_gate_timeout_as_denial.md` · `docs/done/tavily_implementation.md` (the web_search implementation).

---

## Symptom

`web_search` returned **`No web results found for: <query>`** for *every* query in the session — including trivially-populated ones like `"Michelstadt" "Vereine"`. That is not a plausible empty result; a real search returns hundreds of hits. The agent retried ~a dozen query variants (~5 min, ~30 LLM calls) before giving up on `web_search` and pivoting to `run_command` scraping.

## Root cause

The Tavily API key was **over its usage limit**. Probing Tavily directly from the agent pod with the configured key:

```
HTTP 432  {"detail":{"error":"This request exceeds this API key's set usage limit.
           You can increase its limit on the Tavily dashboard."}}
```

`langchain_tavily` 0.2.18 catches the 432 and returns a dict **`{"error": "Error 432: …usage limit…"}` with no `results` key** (reproduced through the real `TavilySearch.invoke` in the pod: `keys=['error']`, `results_len=0`). Then `_direct_web_search` (`src/tools/research/web.py:295-328`) does:

```python
response = search.invoke(invoke_kwargs)
results = response.get("results", [])
if not results:
    return f"No web results found for: {query}"   # :328  ← masks the real error
```

It checks **only** `response.get("results")` and never inspects `response.get("error")`, so a hard API error (432 quota, 401 bad key, 429 rate-limit) is flattened into the benign "No web results found." An *exception* would have been surfaced (`except Exception → "Error searching web: …"`, `:409-411`), but the wrapper doesn't raise — it returns the error in-band, which this code drops.

## Fix

In `_direct_web_search` — and the `_direct_*` Extract/Crawl/Map siblings that share the `response.get(...)`-only pattern — check the error field **before** the empty-results check and surface it:

```python
response = search.invoke(invoke_kwargs)
if isinstance(response, dict) and response.get("error"):
    return f"Error searching web: {response['error']}"   # e.g. "Error 432: … usage limit …"
results = response.get("results", [])
if not results:
    return f"No web results found for: {query}"
```

Then the agent sees "usage limit exceeded / rate-limited / bad key" and can stop, tell the user, or wait — instead of futile retries — and the operator gets a real signal that the key is out of quota. Optionally alert/log when Tavily returns an `error` so a spent key is visible without reading agent logs.

## Reproduce

1. Point `TAVILY_API_KEY` at a key that is over its usage limit (or otherwise erroring).
2. Call `web_search` with any query.
3. Observe: the tool returns `No web results found for: <query>` for everything, with no indication the key is over quota; the agent retries variants pointlessly.
