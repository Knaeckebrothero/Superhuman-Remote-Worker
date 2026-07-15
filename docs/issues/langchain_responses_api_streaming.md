# LangChain Responses API — Tool Call Bugs

## Status: Streaming/parallel-tool bugs resolved for our version; non-streaming worker failure still OPEN

> **2026-07-15 update.** The streaming tool-call bugs (Bugs 1 & 2) are no longer
> a practical problem on the `langchain-openai==1.1.12` we run, so
> `parallel_tool_calls` was **re-enabled** for `gpt-5`/`gpt-5.6`/`codex`/
> `codex-spark`. See [`docs/done/parallel_tool_calls_reenabled_gpt5_codex.md`](../done/parallel_tool_calls_reenabled_gpt5_codex.md)
> and the live-test plan in [`tests/parallel_tool_calls_validation.md`](../../tests/parallel_tool_calls_validation.md).
> **This doc stays in `issues/`** because the non-streaming worker-graph
> empty-response failure (below) is still unresolved.

## Problem

LangChain's `ChatOpenAI` Responses API has multiple bugs that break tool calling, affecting models routed through `/v1/responses` (`gpt-5.*`, `o3`, `o4`, and all Codex proxy models). The streaming bugs were known first and only affected `persistent_graph.py`; they are now resolved for our pinned version (see Bugs 1 & 2). We have also confirmed a **non-streaming** failure mode that affects the worker graph (`graph.py`) when run against the Codex proxy — this remains open; see "Non-streaming failure mode" below.

### Streaming bugs (affect `persistent_graph.py`)

#### Bug 1 — Streamed tool call args arrive as `{}` — RESOLVED for langchain-openai ≥ 1.1.12

**Issue:** [langchain-ai/langchain#34660](https://github.com/langchain-ai/langchain/issues/34660) — issue still **OPEN** upstream (linked PR #35624 was closed/rejected), but the **empty-args symptom no longer applies to the version we run.**

The description below was accurate at **langchain-openai 1.1.6**: `response.function_call_arguments.delta` events weren't extracted into `AIMessageChunk.tool_call_chunks`, so streamed tool-call args arrived empty (`{}`).

As of **1.1.12** (our pin), the delta handler **does** extract args into `tool_call_chunks` with the correct index — `langchain_openai/chat_models/base.py:4789-4796`:

```python
elif chunk.type == "response.function_call_arguments.delta":
    _advance(chunk.output_index)
    tool_call_chunks.append(
        {"type": "tool_call_chunk", "args": chunk.delta, "index": current_index}
    )
```

The only residual of #34660 is that the terminal `response.completed` chunk still omits the aggregated `tool_calls` (`base.py:4697-4714`) — but our graph reconstructs calls from the incremental stream chunks and never depends on the completed chunk for them. The `response.completed` block is byte-identical on upstream `master`, so this is settled behaviour, not something a version bump changes. **Net: streamed parallel tool calls aggregate correctly on 1.1.12**, which is why `parallel_tool_calls` was re-enabled (2026-07-15). The `persistent_graph.py` streaming→`ainvoke` workaround (mitigation #3) is now belt-and-suspenders rather than load-bearing.

#### Bug 2 — `merge_dicts()` corrupts parallel tool calls

**Issue:** [langchain-ai/langchain#34807](https://github.com/langchain-ai/langchain/issues/34807) — **CLOSED, fixed in PR #35281 (`langchain-core`).** Picked up by bumping `langchain-openai` to `>=1.1.12` (which transitively pulls a fixed `langchain-core`).

When streaming produced multiple parallel tool calls, `merge_dicts()` concatenated tool call fields (`name`, `id`, `type`) across different tool calls, merging them into a single malformed entry. The fix added these fields to the skip-concatenation guard.

#### Bug 3 — Malformed streamed tool calls silently dropped (chat completions only)

**Issue:** [langchain-ai/langchain#35782](https://github.com/langchain-ai/langchain/issues/35782) — **OPEN, unfixed.** Two community fix PRs (#35813 and #36662) were submitted but **both auto-closed by the langchain bot** because the contributors weren't pre-assigned to the issue. The most recent one (#36662) was killed on 2026-04-11.

When any single tool call chunk in `_convert_delta_to_message_chunk` lacks `function` or `index` keys, a broad `try-except KeyError` around the list comprehension drops the entire batch silently. **This bug is in the chat completions streaming path, not the Responses API parser** — it does not directly explain the worker-graph failure described below, despite the symptoms looking similar.

### Non-streaming failure mode (affects `graph.py` worker against Codex proxy)

We do **not** yet have a langchain issue number for this. Symptoms in job `bf805890-31be-4413-a3d8-573e6d242c31` (cancelled after 337 iterations against `codex/gpt-5.4`):

- Every `ainvoke` returned an `AIMessage` with `content == ""`, `tool_calls == []`, `usage_metadata is None`, and no `finish_reason` in `response_metadata`.
- The `srw-codex-proxy` pod logs show 1171 successful `200 OK` POSTs to `/v1/responses` with 2–12s latencies during the same window — i.e. the upstream call worked. The proxy is delivering real payloads.
- The non-streaming Responses API parser `_construct_lc_result_from_responses_api` in `langchain_openai/chat_models/base.py:4187` (at version 1.1.6) is clean — there is **no** silent-drop bug in that function. It iterates `response.output`, has explicit `function_call` / `custom_tool_call` / `message` branches, and the only `try/except` puts failed `json.loads` results into `invalid_tool_calls` (not silently dropped).

That means the content is being lost **before** the langchain parser sees it — most likely in the OpenAI Python SDK's Pydantic deserialization of the proxy's JSON into the `Response` model (`openai==2.14.0`). If the proxy returns JSON whose shape doesn't exactly match the SDK's `Response` schema, Pydantic can construct `Response(output=[], usage=None, ...)` silently and `_construct_lc_result_from_responses_api` then dutifully produces an empty `AIMessage`.

**Next diagnostic step (not yet done):** capture the raw `httpx.Response.text` for one Codex `/v1/responses` call before Pydantic touches it. The cheapest place to do this is in `AsyncReasoningCapturingClient.send` in `src/llm/reasoning_chat.py` — gate it behind an env var like `DEBUG_CODEX_RAW_RESPONSE=1` and dump the body to a temp file when the request URL contains `/responses`. With one captured payload, we can decide whether to (a) file an upstream issue against `openai-python`, (b) patch CLIProxyAPI's response shape, or (c) bypass langchain's parser entirely for codex by parsing the raw JSON ourselves in `_create_codex_llm`.

## Why Codex is especially affected

The Codex proxy (`CLIProxyAPI` at `srw-codex-proxy:8317`) only supports the Responses API endpoint (`/v1/responses`). It does **not** support Chat Completions (`/v1/chat/completions`). This means we cannot use `use_responses_api=False` as a workaround for Codex models, unlike standard OpenAI models.

## `parallel_tool_calls=False` is a soft switch

Setting `parallel_tool_calls: false` reduces but does **not prevent** parallel tool calls. The parameter influences model behavior but is not structurally enforced — if the user instructs the model to make parallel calls, it will.

References:
- [sgl-project/sglang#9696](https://github.com/sgl-project/sglang/issues/9696) — models still output multiple tool calls with `parallel_tool_calls=False`
- [openai/openai-agents-python#762](https://github.com/openai/openai-agents-python/issues/762) — SDK default mismatch
- [OpenAI community thread](https://community.openai.com/t/responses-api-parallel-tool-calls-not-happening/1226942) — Responses API has additional quirks with this parameter

## Current mitigations

### 1. ~~`parallel_tool_calls: false` in settings matrix~~ — REVERTED 2026-07-15

Was set for `gpt-5`, `codex`, `codex-spark` (and later `gpt-5.6`) as a soft
suppressant while Bugs 1 & 2 were live. **Now `parallel_tool_calls: true`** for
all four families in `config/model_config_matrix.yaml` (the file that replaced
the old `config/settings_matrix.yaml`), since Bug 2 is fixed and Bug 1 no longer
applies to 1.1.12. See `docs/done/parallel_tool_calls_reenabled_gpt5_codex.md`.

### 2. Chat Completions for standard OpenAI models

`_create_openai_llm` in `src/core/loader.py` sets `use_responses_api=False`, forcing Chat Completions API which handles streaming + tool calls correctly. This does **not** apply to Codex (proxy doesn't support it).

### 3. Streaming workaround + ainvoke retry

In `src/persistent_graph.py`, the `_execute_turn` function detects streamed tool calls with empty args and retries with `ainvoke`. This works for single tool calls but may still fail for parallel calls via the Responses API.

### 4. Empty response safety net

Also in `src/persistent_graph.py`, if the LLM returns empty content with no tool calls (after streaming or ainvoke retry), the user sees a warning message instead of silence.

### 5. Empty-response circuit breaker in worker graph

In `src/graph.py`, the worker graph fails the job after 3 consecutive empty responses (`content == "" and tool_calls == []`) with `error.type == "empty_response"`. This is a defensive stop-the-bleed measure for the non-streaming failure mode described above — it prevents the 337-iteration spin observed in job `bf805890`. The decision logic is in `_check_empty_response_streak()` (`src/graph.py`); the trigger lives in the `execute` node retry loop. Tests: `tests/test_graph_helpers.py::TestCheckEmptyResponseStreak`.

This is **not** a fix for the underlying empty-response bug — see "Non-streaming failure mode" above for the actual root cause and the diagnostic step still needed.

## TODO: Runtime enforcement

The soft `parallel_tool_calls=false` flag doesn't cover edge cases where the user or prompt instructs the model to make parallel calls. A runtime enforcement layer could:

- After receiving a response with multiple tool calls from a Responses API model, only execute the first and re-prompt for the rest sequentially
- Or strip all but the first tool call and log a warning

## When to revisit

- **#34807 — shipped & closed.** The `merge_dicts` parallel-tool-call corruption is fixed (in via the `langchain-openai>=1.1.12` pin).
- **#34660 — resolved for our version (2026-07-15).** The empty-args symptom no longer applies to 1.1.12 (see Bug 1). `parallel_tool_calls: true` is already re-enabled for `gpt-5`/`gpt-5.6`/`codex`/`codex-spark`. The GitHub issue is still nominally open (terminal `response.completed` chunk omits `tool_calls`), but that residual doesn't affect our graph. The `persistent_graph.py` streaming→`ainvoke` workaround can be removed once the live parallel-tool tests (`tests/parallel_tool_calls_validation.md`) pass; keeping it for now is harmless.
- **#35782** (chat-completions streaming silent drop) — still open, two community PRs auto-killed by the langchain bot for not having a pre-assigned issue. To unblock this, either get an issue assignment from a maintainer and resubmit, or vendor the fix in our `ReasoningChatOpenAI` subclass. **This bug does not directly affect us today** — our streaming path uses the Responses API (not chat completions) and our worker uses non-streaming.
- **Non-streaming failure mode in the Codex worker** — needs the raw-response capture diagnostic described in the section above before any fix can be designed.

Versions as of 2026-04-11: `langchain-openai` latest on PyPI was **1.1.12** (released 2026-03-23). Repo pin bumped from `>=0.0.5` to `>=1.1.12,<2.0` to pick up the #34807 fix. `openai` SDK at 2.14.0.

Versions as of 2026-07-15: installed `langchain-openai==1.1.12`, `langchain-core==1.2.28`; latest on PyPI is **1.3.5**. We remain on 1.1.12 (pin allows `<2.0`); a bump is **not** required for the parallel-tool re-enablement — #34660's `response.completed` handling is unchanged through `master`, so no newer release changes the relevant behaviour.
