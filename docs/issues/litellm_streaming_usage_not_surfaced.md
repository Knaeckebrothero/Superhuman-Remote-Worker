# Session usage bar empty on gemma turns — investigated, NOT a streaming-usage gap

**Status:** closed (misdiagnosed) · 2026-06-24
**Found:** 2026-06-22 during the persistent-session token-UI work.
**Resolved:** 2026-06-24 — the original "LiteLLM drops usage" hypothesis below
is **wrong**. The streaming-usage pipeline is healthy end-to-end; the empty bar
was an *errored turn* (stale-key 401), not a missing-usage gap.

## TL;DR

`stream_options.include_usage` reaches the wire, `usage_metadata` comes back,
`_execute_turn` captures it, and real turns persist correct per-turn metrics.
There is nothing to fix in the usage plumbing. An errored turn (e.g. a 401)
captures no usage, so the bar simply doesn't render — that's the symptom that
was originally misread as a LiteLLM gap.

## Evidence (k3d, 2026-06-24)

Probed from inside the orchestrator pod (`langchain_openai 1.3.3`,
`/app/src/llm/reasoning_chat.py`, network to both routes):

1. **`include_usage` is on the wire.** openai-SDK DEBUG logs show
   `stream_options: {'include_usage': True}` on every request — for both plain
   `ChatOpenAI` and `ReasoningChatOpenAI`. langchain's `_astream` converts
   `stream_usage=True` → `stream_options` (base.py:1381-1383); loader sets
   `stream_usage=True` (loader.py:2769/3123/3211).

2. **The tap does NOT eat the usage chunk.** `_SSEReasoningTap.aiter_bytes`
   (reasoning_chat.py:433-439) `yield`s every byte unchanged and only
   *additionally* parses for reasoning. Plain `ChatOpenAI` and
   `ReasoningChatOpenAI` return **identical** `usage_metadata`.

3. **Usage comes back on the actual session route.** The persistent session for
   `gemma-4-moe` routes **direct to `https://ai.h4ll.app/v1`** (registry
   `models.provider_kind='endpoint'` → `llm_endpoints` row `f475b8e1`,
   `base_url=https://ai.h4ll.app/v1`). LiteLLM (`srw-litellm:4000`) is **not**
   in the path on this cluster. With the correctly-decrypted endpoint key
   (`security/crypto.py` AES-GCM), a streamed gemma turn returns
   `usage_metadata = {input_tokens:21, output_tokens:20, total_tokens:41}`.

4. **Real turns persist correct metrics.** `thread_messages.metrics` for a live
   `gemini-2.5-flash` thread holds
   `{input_tokens:15624, output_tokens:65, reasoning_tokens:35,
   model:'gemini-2.5-flash'}`. End-to-end capture works.

## Why the original curl looked like a gap

The 2026-06-22 curl against `srw-litellm:4000` *without* `stream_options`
returned no usage — true of any OpenAI-compatible stream, and exactly why
langchain sends `include_usage`. It only proved the param is required, not that
SRW omits it. SRW sends it.

## Real cause of the empty bar

The gemma session that triggered this (`579acfce`) used **`gemma-4-moe-strix`**,
the variant that 401s on the stale/invalid upstream key (and is now
`enabled=false` in the registry). A 401 turn errors before
`turn_metrics` is built → no `usage.updated` frame → no bar. Correct behavior,
just with no friendly "turn failed" affordance.

## Side-finding (validates the reasoning estimate)

The **direct `ai.h4ll.app` route reports no reasoning detail**
(`output_token_details: {}`), whereas the **same model through LiteLLM** returns
`{reasoning: 16}` (LiteLLM normalizes `completion_tokens_details`), and gemini
reports it natively. So on the real gemma route, the shipped
`_maybe_estimate_reasoning_tokens` estimate (context_summarization_rework.md
§10.2) is the *only* source of a reasoning count — its intended purpose.

## Residual (optional, low priority)

- No usage/error affordance when a turn errors (the original confusion). A small
  "turn failed" chip or keeping the prior turn's bar would avoid the "is it
  broken?" read. Not a usage-plumbing bug.
- If native reasoning counts for gemma are wanted, routing it *through* LiteLLM
  would surface `reasoning_tokens` without the estimate — but the gateway is
  dark on dev by design (cost-monitoring overlay), so the estimate stands.
