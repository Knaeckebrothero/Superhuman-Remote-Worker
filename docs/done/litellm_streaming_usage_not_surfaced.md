# Session usage bar empty on gemma turns — NOT a streaming-usage gap (live-verified healthy)

**Status:** closed (misdiagnosed) · 2026-06-24
**Found:** 2026-06-22 during the persistent-session token-UI work.
**Resolved:** 2026-06-24 — the original "LiteLLM drops usage" hypothesis is
**wrong**. The streaming-usage pipeline is healthy end-to-end and now
**live-verified in the cockpit**. The empty bar that started this was an
*errored turn* (stale-key 401), not a missing-usage gap.

## TL;DR

`stream_options.include_usage` reaches the wire, `usage_metadata` comes back,
`_execute_turn` captures it, the cockpit renders the bar. Nothing to fix in the
usage plumbing. An errored turn (e.g. a 401) captures no usage, so the bar
doesn't render — that's the symptom originally misread as a LiteLLM gap.

## Live verification (k3d cockpit, 2026-06-24)

New `gemma-4-moe` session (`f1eab88e`), prompt "explain why the sky is blue in
3 sentences". Turn completed; the redesigned usage bar rendered above the
composer:

```
INPUT 11.5k   OUTPUT 1.9k   REASONING 1.8k   │   CTX ▱ 9%
```

The `REASONING 1.8k` chip carries **no `~`** → a **native** provider count, not
the estimate.

## Routing (corrected) — the session DOES go through LiteLLM

Earlier I inferred the session bypassed the gateway. The live agent dispatch log
disproves that:

```
Created OpenAI LLM: model=gemma-4-moe, base_url=http://srw-litellm:4000/v1, ...
```

So on this k3d cluster the **gateway is enabled** (matches dev since
`ac211a52`): the agent talks to `srw-litellm:4000`, which forwards to the
endpoint's `ai.h4ll.app` upstream (registry `models.provider_kind='endpoint'` →
`llm_endpoints` `f475b8e1`, `base_url=https://ai.h4ll.app/v1`). The endpoint URL
is the *upstream* LiteLLM forwards to, **not** the agent's dispatch URL.

## Evidence the plumbing is sound (in-pod probes, 2026-06-24)

Probed from the orchestrator pod (`langchain_openai 1.3.3`,
`/app/src/llm/reasoning_chat.py`):

1. **`include_usage` is on the wire** — openai-SDK DEBUG logs show
   `stream_options:{'include_usage':True}` on every request. langchain converts
   `stream_usage=True` (loader.py:2769/3123/3211 → base.py:1381-1383).
2. **The SSE tap is a pure pass-through** — `_SSEReasoningTap.aiter_bytes`
   (reasoning_chat.py:433-439) yields every byte unchanged; plain `ChatOpenAI`
   and `ReasoningChatOpenAI` return **identical** `usage_metadata`.
3. **Usage (incl. reasoning) comes back through LiteLLM** — a streamed
   `gemma-4-moe` turn via `srw-litellm:4000` returns
   `{input_tokens:21, output_tokens:20, output_token_details:{reasoning:16}}`.
4. **Real turns persist correct metrics** — `thread_messages.metrics` for a live
   gemini thread: `{input:15624, output:65, reasoning:35}`.
   (`threads.total_tokens` is an unused rollup, always 0 — ignore it.)

## Why the original curl looked like a gap

The 2026-06-22 curl against the gateway *without* `stream_options` returned no
usage — true of any OpenAI-compatible stream, and exactly why langchain sends
`include_usage`. It only proved the param is required, not that SRW omits it.
SRW sends it.

## Real cause of the empty bar

The gemma session that triggered this (`579acfce`) used **`gemma-4-moe-strix`**
(now `enabled=false`), which 401s on a stale upstream key. A 401 turn errors
before `turn_metrics` is built → no `usage.updated` frame → no bar. Correct
behavior, just with no friendly "turn failed" affordance.

## Note on reasoning detail (route-dependent)

The **LiteLLM route returns native `reasoning_tokens`** (live: 1.8k; probe: 16).
The **direct `ai.h4ll.app` upstream** returns none (`output_token_details:{}`).
So the `_maybe_estimate_reasoning_tokens` estimate (context_summarization_rework
§10.2) is the fallback for any route/model that omits the count; on the current
gateway route it isn't needed because the count is real.

## Residual (optional, low priority)

- No usage/error affordance when a turn errors (the original confusion). A small
  "turn failed" chip would avoid the "is it broken?" read. Not a plumbing bug.
