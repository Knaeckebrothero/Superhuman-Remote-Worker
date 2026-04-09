# LangChain Responses API Streaming — Tool Call Bugs

## Status: Mitigated (workaround in place)

## Problem

LangChain's `ChatOpenAI` Responses API streaming has multiple bugs that break tool calling, specifically affecting models routed through the Responses API (`gpt-5.*`, `o3`, `o4`, and all Codex proxy models).

### Bug 1: Streamed tool call args arrive as `{}`

**Issue:** [langchain-ai/langchain#34660](https://github.com/langchain-ai/langchain/issues/34660) (OPEN)

When streaming with the Responses API, `response.function_call_arguments.delta` events are never extracted into `AIMessageChunk.tool_call_chunks`. The `response.completed` chunk contains metadata but the `tool_calls` from the aggregated message are missing. Result: tool calls arrive with empty args `{}`.

### Bug 2: `merge_dicts()` corrupts parallel tool calls

**Issue:** [langchain-ai/langchain#34807](https://github.com/langchain-ai/langchain/issues/34807)

When streaming produces multiple parallel tool calls, `merge_dicts()` concatenates tool call fields (name, id, type) across different tool calls, merging them into a single malformed entry.

### Bug 3: Malformed tool calls silently dropped

**Issue:** [langchain-ai/langchain#35782](https://github.com/langchain-ai/langchain/issues/35782)

When any single tool call in a batch lacks `function` or `index` keys, the entire batch is discarded without warning due to a broad `try-except KeyError` around the list comprehension.

## Why Codex is especially affected

The Codex proxy (`CLIProxyAPI` at `srw-codex-proxy:8317`) only supports the Responses API endpoint (`/v1/responses`). It does **not** support Chat Completions (`/v1/chat/completions`). This means we cannot use `use_responses_api=False` as a workaround for Codex models, unlike standard OpenAI models.

## `parallel_tool_calls=False` is a soft switch

Setting `parallel_tool_calls: false` reduces but does **not prevent** parallel tool calls. The parameter influences model behavior but is not structurally enforced — if the user instructs the model to make parallel calls, it will.

References:
- [sgl-project/sglang#9696](https://github.com/sgl-project/sglang/issues/9696) — models still output multiple tool calls with `parallel_tool_calls=False`
- [openai/openai-agents-python#762](https://github.com/openai/openai-agents-python/issues/762) — SDK default mismatch
- [OpenAI community thread](https://community.openai.com/t/responses-api-parallel-tool-calls-not-happening/1226942) — Responses API has additional quirks with this parameter

## Current mitigations

### 1. `parallel_tool_calls: false` in settings matrix

Set for `gpt-5`, `codex`, and `codex-spark` families in `config/settings_matrix.yaml`. Prevents the model from voluntarily making parallel calls in normal operation, but does not structurally block it.

### 2. Chat Completions for standard OpenAI models

`_create_openai_llm` in `src/core/loader.py` sets `use_responses_api=False`, forcing Chat Completions API which handles streaming + tool calls correctly. This does **not** apply to Codex (proxy doesn't support it).

### 3. Streaming workaround + ainvoke retry

In `src/persistent_graph.py`, the `_execute_turn` function detects streamed tool calls with empty args and retries with `ainvoke`. This works for single tool calls but may still fail for parallel calls via the Responses API.

### 4. Empty response safety net

Also in `src/persistent_graph.py`, if the LLM returns empty content with no tool calls (after streaming or ainvoke retry), the user sees a warning message instead of silence.

## TODO: Runtime enforcement

The soft `parallel_tool_calls=false` flag doesn't cover edge cases where the user or prompt instructs the model to make parallel calls. A runtime enforcement layer could:

- After receiving a response with multiple tool calls from a Responses API model, only execute the first and re-prompt for the rest sequentially
- Or strip all but the first tool call and log a warning

## When to revisit

Monitor [langchain-ai/langchain#34660](https://github.com/langchain-ai/langchain/issues/34660) for a fix. Once merged and released in `langchain-openai`, re-enable `parallel_tool_calls: true` in `config/settings_matrix.yaml` and remove the streaming workaround in `persistent_graph.py`. Current installed version: `langchain-openai==1.1.6`, latest: `1.1.12` (still unfixed).
