# LiteLLM-routed models surface no token usage → empty session usage bar

**Status:** open · characterized, not fixed
**Found:** 2026-06-22, during the persistent-session token-UI work.

## Symptom

For a persistent session on a **LiteLLM-routed** model (`gemma-4-moe`,
`base_url=http://srw-litellm:4000/v1`), the cockpit's live token bar
(`.usage-panel`) never appears — no input/output/ctx **and** therefore no
reasoning estimate. The turn otherwise completes normally (answer streams,
reasoning is captured). Direct vLLM models (e.g. `gemma-4-moe-strix`) **do**
show the bar, so it is route-specific, not a cockpit bug.

## Root cause (narrowed, not closed)

The bar is driven by `usage.updated` frames, emitted only when
`persistent_graph._execute_turn` builds `turn_metrics`, which requires
`response.usage_metadata` (or `response_metadata.token_usage`) to be populated.
For the LiteLLM route that dict is empty, so no frame is sent.

Established by curling the gateway directly (master key in Secret `srw`,
`LITELLM_MASTER_KEY`) from the orchestrator pod:

- `stream:true` **without** `stream_options` → stream ends at `data: [DONE]`
  with **no usage chunk**.
- `stream:true` **with** `stream_options:{include_usage:true}` → final chunk
  carries `usage:{prompt_tokens, completion_tokens, total_tokens,
  completion_tokens_details:{reasoning_tokens:0}}`.

So the LiteLLM/vLLM route **requires** `include_usage`, and SRW's turn isn't
getting it onto the wire — even though:

- `loader.py` sets `llm_kwargs["stream_usage"] = True` (3 sites).
- langchain_openai `_astream` (verified in installed source, lines ~1376-1383)
  turns `stream_usage=True` into `kwargs["stream_options"]={"include_usage":True}`.
- `ReasoningChatOpenAI.__init__` passes `**kwargs` straight to `super()`, and
  its `_astream` override (`src/llm/reasoning_chat.py:1105`) is a pure
  pass-through; chunk accumulation in `_execute_turn` (`:1113-1117`) sums all
  chunks via `+`, so a usage chunk *would* aggregate if present.

**Prime suspect:** the custom httpx tap `AsyncReasoningCapturingClient`
(`reasoning_chat.py:721`) that intercepts the raw SSE to extract reasoning may
be dropping the final usage-only chunk before the OpenAI SDK aggregates it; or
`include_usage` isn't reaching the wire for the custom `base_url`.

## Next step to close

1. Capture the **actual outbound request** to LiteLLM (LiteLLM request log, or a
   one-line debug log of the payload in `reasoning_chat`) to confirm whether
   `stream_options.include_usage` is present.
2. If present on the wire but usage still absent in `response.usage_metadata`,
   the tap is eating the final chunk → fix `AsyncReasoningCapturingClient` to
   forward the usage-only chunk.
3. If absent on the wire, force it: `model_kwargs["stream_options"] =
   {"include_usage": True}` in `loader._create_openai_llm`.

## Relevance

Blocks the live usage bar (and the new reasoning-token estimate) for every
LiteLLM-routed model — i.e. the common case once models are registered through
the gateway. The reasoning-estimate feature itself is correct and unit-tested;
it simply can't render until a `usage.updated` frame arrives.
