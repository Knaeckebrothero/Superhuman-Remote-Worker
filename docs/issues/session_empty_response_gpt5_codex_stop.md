# Persistent session: gpt-5.x via Codex proxy returns empty `stop` completion

**Status:** **Mitigation implemented 2026-06-23** (develop, branch `fix/session-empty-response-retry`) · root cause **isolated, not synthetically reproducible** (≈1026 calls, every layer incl. full prod LiteLLM path clean) — a rare non-deterministic upstream/condition-dependent event with no single layer to patch · fix = agent-side **bounded auto-retry + live reasoning-replace** (see "Recommended mitigation" below)
**Found:** 2026-06-23, investigating session `6810288e` on the main cluster
**Component:** `persistent_graph.py` streaming turn loop · Codex proxy (`srw-codex-proxy`, CLIProxyAPI v7.2.27) · LiteLLM gateway
**Related:** [[langchain_responses_api_streaming]] (same failure family, non-streaming worker-graph variant) · [[litellm_streaming_usage_not_surfaced]] (explains the `token_usage: {}` / stale usage bar seen here)

## Symptom

In a persistent session on a `gpt-5.x` model routed through the Codex proxy,
a turn occasionally renders the placeholder:

> ⚠ The model returned an empty response. Please try again or switch models.

The session is otherwise healthy — the turn is recorded, the agent stays up,
and the next message works. It is **intermittent** and not tied to a specific
session or prompt.

## Concrete occurrence

Session `6810288e-1cd5-4572-a5f3-4cb3eb839980`, model `gpt-5.5`
(`reasoning_effort=high`, `temp=0.0`), routed agent → LiteLLM (`srw-litellm:4000`)
→ codex-proxy (`/v1/responses`).

- Turn 1 ("check rare earth metal prices") succeeded fully: web_search ×3,
  extract, cite ×4, 1299-char answer.
- Turn 2 ("Okay, what are the prices 10 years ago?") → empty.

Chain for turn 2 (all timestamps UTC):

| Time | Event |
|---|---|
| 09:18:55 | user input received |
| 09:18:57 | memory reranker → LiteLLM `/v1/rerank` **403** (separate issue, see [[litellm_reranker_model_unregistered]]) — non-fatal |
| 09:19:20.705 | codex-proxy `POST /v1/responses` → **200 in 22.1 s** |
| 09:19:20.717 | LiteLLM `POST /v1/chat/completions` → **200 OK** (23,259 ms) |
| 09:19:20.720 | `persistent_graph.py` → "Streaming produced empty content (type=str, has_tool_calls=False, additional_kwargs=[])" → placeholder shown |

Audit row (`srw_audit.llm_requests`, `job_id`=thread, `iteration`=2):

```
model: gpt-5.5   call_type: main   finish_reason: "stop"   latency_ms: 23259
metrics: {"tool_calls": 0, "input_chars": 31369, "token_usage": {}, "output_chars": 0}
response: {"type": "AIMessageChunk", "content": "",
           "response_metadata": {"model_name": "gpt-5.5",
                                 "finish_reason": "stop", "model_provider": "openai"}}
```

The response is an **`AIMessageChunk` (streaming path)** with a valid
`finish_reason: "stop"` but **zero content deltas** — the proxy streamed a
`response.completed` (hence the finish_reason) but no `response.output_text.delta`
events. Contrast with the same session's healthy calls: the answer was
`stop` + 1299 chars; the tool step was `tool_calls` + 4 calls. So this is **not**
an agent-side parse drop of otherwise-present content — the aggregated stream
genuinely carried no text.

## Frequency (cluster-wide, last 7 days)

`gpt-5.5` = **4 / 609** main calls (~0.7 %), `gpt-5.4-mini` = **2 / 223** (~0.9 %).
All six: `call_type=main`, `output_chars=0`, `tool_calls=0`, `finish_reason=stop`.

| ts (UTC) | thread | model | iter | latency | input_chars |
|---|---|---|---|---|---|
| 06-19 07:29:58 | 1be66c2f | gpt-5.5 | 75 | 3.1 s | 193,560 |
| 06-19 07:30:02 | 1be66c2f | gpt-5.5 | 76 | 3.5 s | 194,027 |
| 06-20 21:24:07 | f9d1a276 | gpt-5.4-mini | 8 | 16.6 s | 58,330 |
| 06-21 07:02:05 | 9708ce16 | gpt-5.5 | 11 | 18.1 s | 43,969 |
| 06-21 07:54:57 | 46c6129e | gpt-5.4-mini | 5 | 1.8 s | 30,447 |
| 06-23 09:19:20 | 6810288e | gpt-5.5 | 2 | 23.3 s | 31,369 |

No single trigger (iteration 2–76, input 30 k–194 k chars). But latency splits
the events into two apparent **sub-modes**, which probably have different root
causes:

- **Fast-empty** (1.8 s, 3.1 s, 3.5 s) — too quick for real generation; reads
  like a transient upstream hiccup, no message item produced at all. *(The
  initial guess of a proxy/parse bug was later ruled out — proxy and parse both
  tested clean across ≈1026 calls.)* The 06-19 pair is two in a row at iter 75/76
  with ~194 k input chars.
- **Slow-empty** (16.6 s, 18.1 s, 23.3 s) — the model spent real time
  (reasoning) and then emitted no `final` message and no tool call.

## Candidate layers (hypotheses — all subsequently tested & cleared)

> **Resolved 2026-06-23:** every layer below was load-tested (≈1026 calls) and
> came back clean — see "k3d / LiteLLM isolation" below. None reproduced the
> empty under synthetic traffic. Kept here as the reasoning that drove the hunt.

The agent received an empty streamed completion. The emptiness could, a priori,
originate at three layers:

1. **Model** genuinely ends the turn in the reasoning/analysis channel without
   committing a `final`-channel message or a tool call (plausible for the
   slow-empty sub-mode, esp. with `reasoning_effort=high` right after a
   tool-heavy turn).
2. **Codex proxy** (harmony → Responses SSE translation) drops the final
   message / its deltas (plausible for the fast-empty sub-mode).
3. **OpenAI SDK / langchain** streaming Responses parser loses the content.
   There is documented precedent for the *client* side dropping real proxy
   output on the **non-streaming** path — see [[langchain_responses_api_streaming]]
   "Non-streaming failure mode" (job `bf805890`, `gpt-5.4`): every `ainvoke`
   returned empty `content`/`tool_calls` while the proxy logged healthy 200s.

This session case differs from that documented one in two ways: it is on the
**streaming** path (`AIMessageChunk`), and it carries a populated
`response_metadata` with `finish_reason=stop` (the worker-graph case had
`usage_metadata=None` and no `finish_reason`). Same family, different surface.

`token_usage: {}` here is **not** part of this bug — LiteLLM-routed models
don't surface usage without `stream_options:{include_usage:true}`
([[litellm_streaming_usage_not_surfaced]]). It is also why the cockpit usage
footer showed stale turn-1 numbers (`OUTPUT 2,361`) for the empty turn.

## Code path

`src/persistent_graph.py` (streaming turn loop). After the stream aggregates,
the empty-content guard fires (line refs vs `develop`@`18268c8d`; running build
`sha-07b0bd9` is ~46 lines earlier):

- Streaming branch — `~1406-1428`: `if not response_content and not response.tool_calls`
  → logs "Streaming produced empty content", checks `additional_kwargs.refusal`
  (absent here), else sets the placeholder. **No retry.**
- `ainvoke` fallback branch — `~1284-1305`: same handling for the non-streaming
  fallback.

Reasoning deltas stream to a **separate** sink (`on_thinking` / `_reasoning_buf`),
never into `response_content`; an empty `response_content` is therefore exactly
"no final text was streamed."

## Isolation method (executed 2026-06-23)

The approach below was carried out in full — results in "k3d / LiteLLM
isolation" further down. One durable finding surfaced while planning it: the
existing diagnostic hook `DEBUG_CODEX_RAW_RESPONSE=1`
(`src/llm/reasoning_chat.py:852`) only fires on the **non-streaming** path
(`is_responses and not stream`), so it will **not** catch a persistent-session
(streaming) empty as-is (still open — see "Isolation complete").

The steps (cheapest first; what each actually found is in the results table):

1. **Prompt-correlation check (1 call, no code):** resend the same follow-up on
   the still-active session (or any fresh gpt-5.5 session). Empties again →
   strongly prompt-correlated and reproducible on demand; answers → transient
   non-determinism.
2. **Direct proxy SSE dump (no agent):** curl LiteLLM (`/v1/chat/completions`,
   `stream:true, stream_options:{include_usage:true}`, master key in Secret
   `srw` / `LITELLM_MASTER_KEY`) and/or codex-proxy `/v1/responses` directly with
   the reconstructed turn-2 messages; dump every SSE event. If
   `response.completed.response.output` contains a message item with text but
   the deltas were absent → proxy/client streaming bug; if it contains only a
   reasoning item → model.
3. **Extend the raw-capture hook to streaming:** tee the streamed bytes (mirror
   `_install_streaming_reasoning_tap`) and dump when the aggregated content is
   empty. Then drive empties via option 1/2.

## k3d / LiteLLM isolation (2026-06-23) — every layer clean, not synthetically reproducible

Once the Codex login was set up on k3d, we hammered `gpt-5.5` to try to catch an
empty, sweeping outward layer by layer. The first batches hit the codex-proxy
**directly** (`http://srw-codex-proxy:8317/v1/responses`, raw `urllib` + our own
parse) to test the proxy/model in isolation; later batches added the openai SDK,
langchain streaming, big context, and finally the **main-cluster LiteLLM
`chat/completions`** path — the exact route the failing session took.

| Batch | Calls | Shape | Empties | Notes |
|---|---|---|---|---|
| 1 | 20 | single-turn, no tools, reasoning=high, non-stream | 0 | normal `['reasoning','message']` |
| 2 | 36 | multi-turn + `web_search` tool, non-stream | 0 | model reliably tool-called (36/36) |
| 3 | 40 | streaming, mixed tool/text | 0 | — |
| 4 | 400 | streaming, mixed tool/text | 0 | 200 tool-calls + 200 text |
| 5 | 80 | **openai-SDK** `with_raw_response`, parse-vs-raw | 0 | 0 raw/parse mismatches |
| 6 | 150 | **langchain** `ChatOpenAI.stream()` + chunk-merge; tool/text/**big-ctx** | 0 | 0/50 each mode; big-ctx ≈50k chars |
| 7 | 300 | **main-cluster LiteLLM** `chat/completions` streaming (exact prod path) | 0 | 0/100 each mode; tool/text/big-ctx |
| **Total** | **≈1026** | | **0** | every layer clean, including the full prod path |

Findings:

- **Proxy + model are reliable in isolation.** 0 empties in ≈496 direct calls.
  Against the prod 0.7 % rate, `P(0 | n=496) ≈ 3 %` — i.e. the synthetic
  direct-proxy rate is **materially lower** than prod. The proxy is not the
  source under clean, small-context load.
- **The streaming path is faithful** (tests the leading "proxy drops streamed
  deltas" hypothesis). For every text response, summed `response.output_text.delta`
  **exactly equalled** the final `response.completed` message (e.g. 514==514,
  583==583); tool responses correctly carried a `function_call`. **0 delta-drops.**
  So "the proxy silently drops streamed content" is **not** supported — when
  there is content, the deltas carry it.
- **The openai-SDK Pydantic parse is also clean.** Batch 5 used the SDK's
  `with_raw_response` and compared the **raw body** to the **parsed `Response`**
  on every call: 0 mismatches (msg/tool counts identical). This **refutes** the
  [[langchain_responses_api_streaming]] "Non-streaming failure mode" hypothesis
  (SDK deserializing a shape-mismatched body into `Response(output=[])`) — for
  these shapes the SDK parses faithfully.
- **langchain `ChatOpenAI` streaming + big context are also clean** (batch 6).
  150 calls through the agent's *actual* client stack (`use_responses_api=True`,
  `.stream()` with chunk-merge — the exact code that emits the user-visible
  "Streaming produced empty content"), split across tool / text / **big-context
  (≈50k-char, 13-message)** modes: **0 empties in all three** (0/50 each).
  langchain aggregated tool-calls and text faithfully even under large context.
  This **clears** suspect (b) langchain-aggregation **and** (c) context-scale.
- **LiteLLM — the full prod path — is also clean** (batch 7). 300 calls through
  the **main cluster's** registered `gpt-5.5` LiteLLM entry via `chat/completions`
  streaming — the exact `agent → LiteLLM → proxy` path the failing session took,
  including LiteLLM's `responses → chat/completions` response translation — across
  tool/text/big-context: **0 empties** (0/100 each).
- **Final conclusion: the empty is NOT a reproducible single-layer bug.** Across
  **≈1026 gpt-5.5 calls** — proxy, model, openai-SDK parse, streaming-delta
  fidelity, langchain aggregation, big-context, **and the full prod LiteLLM
  path** — **zero** empties. At the prod 0.7 % rate, `P(0 | 1026) ≈ 0.08 %`, so the
  prod empties depend on conditions synthetic traffic does **not** replicate: the
  real (large) system prompt + accumulated real tool results + specific model
  reasoning states, and/or **transient ChatGPT-backend hiccups** (the prod
  fast-empties at 1.8–3.5 s read like momentary upstream failures, not a parse
  bug). It is rare, non-deterministic, and **not pin-downable to one layer to
  patch**, which makes the agent-side **bounded auto-retry** the correct fix.

Harness (session scratchpad): `codex_hammer.py` / `codex_hammer2.py` (direct
proxy, non-stream), `codex_stream.py` (direct proxy streaming, delta-vs-final),
`codex_sdk.py` (openai-SDK raw-vs-parse), `codex_langchain.py` /
`codex_langchain2.py` (langchain streaming + big-context), `codex_litellm.py`
(main-cluster LiteLLM `chat/completions`). Each dumps any anomaly's raw payload.

### Isolation complete — no single-layer culprit

Every reachable layer was tested and cleared (batches 1–7, ≈1026 calls): codex
proxy, model, streaming deltas, openai-SDK parse, langchain aggregation,
big-context, **and the full main-cluster LiteLLM `chat/completions` path**. The
empty was **not reproduced synthetically**. Treat it as a rare (~0.7 %),
non-deterministic, condition-dependent upstream event — the fix is the
agent-side retry below, not an upstream-layer patch.

The only way to capture a true positive is **in situ on a real occurrence**. The
existing `DEBUG_CODEX_RAW_RESPONSE` hook (`src/llm/reasoning_chat.py:852`) only
fires on the **non-streaming** path; extend it to the **streaming** path (tee the
SSE, dump when the aggregated content is empty) and leave it armed on a real
gpt-5.x session to catch the next one.

## Recommended mitigation — implemented 2026-06-23

Independent of root cause: a **single bounded auto-retry** when a `main` call
streams empty content **and** no tool calls **and** no refusal, gated to
reasoning/codex model families (`getattr(llm_with_tools, "reasoning", None)`).
At ~0.7 %, one retry almost always succeeds and the user never sees the
placeholder. The retry is **non-streaming** (`ainvoke`) on purpose — it reuses
the proven empty-tool-args retry path and hits a different proxy/SDK translation
path. The placeholder remains the terminal fallback when the retry is also
empty/refuses. Single attempt — a straight-line call, not a loop, so it is
hard-bounded.

What shipped:

- **Server** (`src/persistent_graph.py`): the empty-content terminal guard
  (`~1418`) now retries via `ainvoke` before the placeholder. On success it
  re-streams the retry's reasoning + answer via the new
  `_stream_response_blocks` helper. In the **slow-empty** sub-mode (the model
  streamed *reasoning* then emitted no answer) it first emits a new
  `thinking.reset` frame so the dead-end reasoning bubble is **replaced** rather
  than left stale with the retry's answer appended under it — draining the
  in-flight reasoning broadcasts (`_reasoning_tasks`) first so a late delta can't
  repaint after the reset. New optional callback
  `PersistentLoopCallbacks.on_thinking_reset` → `_loop_on_thinking_reset`
  (`src/api/persistent_app.py`) broadcasts `thinking.reset` (free-form frame, the
  `message_id` coerced via `_coerce_row_id` to match the reasoning frames).
- **Client** (`cockpit/.../persistent-chat.service.ts` + `turn-reducer.ts`): a
  `thinking.reset` SSE frame maps to a `thinking_reset` reducer action whose
  `resetThought` helper drops the active turn's streaming `ThoughtEvent`(s) for
  the message id — idempotent and replay-safe (no active turn ⇒ no-op; never
  seeds a placeholder turn). The persisted row is already the retry's response,
  so a reload is coherent regardless; the reset frame fixes the *live* render.
- Tests: `tests/test_persistent_graph.py::TestEmptyResponseRetry` (6 cases) +
  `turn-reducer.spec.ts` (4) + `persistent-chat.service.spec.ts` (1).

Still open (separate, optional): the only way to capture a true positive is **in
situ** — `DEBUG_CODEX_RAW_RESPONSE` (`src/llm/reasoning_chat.py:852`) fires only
on the non-streaming path; extend it to tee the streaming SSE and dump when the
aggregated content is empty, then leave it armed on a real gpt-5.x session.
