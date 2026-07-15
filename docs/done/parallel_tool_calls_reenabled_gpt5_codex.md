# Parallel tool calls re-enabled — gpt-5 / gpt-5.6 / codex / codex-spark

## Status: Implemented (config flip); live validation deferred

Shipped 2026-07-15 to `develop` (uncommitted at time of writing). Flipped
`parallel_tool_calls: false → true` for the four Responses-API/codex-proxy
families in `config/model_config_matrix.yaml`: `gpt-5`, `gpt-5.6`, `codex`,
`codex-spark`.

Companion docs:
- Outstanding test plan: [`tests/parallel_tool_calls_validation.md`](../../tests/parallel_tool_calls_validation.md)
- Broader (still-open) bug tracker: [`docs/issues/langchain_responses_api_streaming.md`](../issues/langchain_responses_api_streaming.md)

## Why this is now safe

These families were gated by a conflation of **two different** LangChain bugs:

- **`langchain#34807`** — `merge_dicts()` concatenated parallel tool-call fields
  (`name`/`id`/`type`) during streaming, mangling N calls into one. **Fixed**
  (PR #35281 in `langchain-core`), already pulled in via the
  `langchain-openai>=1.1.12` pin in `requirements.txt`. This was the bug that
  actually broke parallel calls.
- **`langchain#34660`** — nominally still OPEN, and the old config comment /
  tracker blamed it for "corrupting parallel tool call args" (args arriving
  `{}`). That symptom was real at **langchain-openai 1.1.6** but is **gone in
  the 1.1.12 we run**: the `response.function_call_arguments.delta` handler now
  extracts streamed args into `tool_call_chunks` with the correct index
  (`langchain_openai/chat_models/base.py:4789-4796`). The only residual of
  #34660 is that the final `response.completed` chunk omits the aggregated
  `tool_calls` (`base.py:4697-4714`) — which our graph never relies on, because
  it reconstructs calls from the incremental stream chunks. Verified the
  `response.completed` block is byte-identical on upstream `master`, so this is
  the settled behaviour, not a version we're waiting to bump past.

Net: the corruption bug is fixed and the empty-args bug doesn't apply to our
version, so parallel calls aggregate correctly on both graph paths. Empirically
confirmed by the user on `gpt-5.6`.

## Changes

| File | Change |
|---|---|
| `config/model_config_matrix.yaml` | `parallel_tool_calls: true` for `gpt-5` (~316), `gpt-5.6` (~351), `codex` (~376), `codex-spark` (~397); stale `#34660` TODOs replaced with accurate rationale |
| `tests/test_settings_matrix.py` | gpt-5.6 assertion flipped to `is True` |
| `tests/parallel_tool_calls_validation.md` | new — outstanding live-test plan + recommended automated additions |

Bind path unchanged: `supports_parallel_tool_calls()` (`src/core/loader.py`)
still gates the kwarg (suppressed for google + o-series); worker binds it in
`src/agent.py`, persistent in `src/api/persistent_session.py`.

## Explicitly left OFF (not a LangChain gate)

`deepseek` (model-unreliable), `glm` (untested / start-safe), `gpt-oss` (vLLM
provider support), `gemma` (`vllm#39392` `<pad>` tokens), and the base `default`
(conservative floor). These are model/provider issues, not #34660/#34807.

## Verification done

- `pytest tests/test_settings_matrix.py tests/test_loader_routing.py` → 161 pass.
- `ruff check` / `ruff format --check` on the touched test file → clean.
- YAML parses; the four families read `true`, the OFF families read `false`.
- Code inspection of installed `langchain-openai==1.1.12` +
  `langchain-core==1.2.28` vs upstream `master` (streaming args extraction /
  `response.completed` handling).

## Verification deferred (reopen criteria)

Live parallel-tool turns per family × graph path are **not** done — the user
will run them later. Plan and pass criteria (≥2 independent calls in one turn,
args-integrity not just count) are in `tests/parallel_tool_calls_validation.md`.

**If any live test fails:** revert the four `parallel_tool_calls` flips (and the
gpt-5.6 test assertion), move this doc back to `docs/issues/`, and re-open the
"re-enable when validated" note in the tracker.

## Related, still open (not blocking this)

The non-streaming worker-graph empty-response failure (Codex proxy, job
`bf805890`) is a **separate** bug affecting the same families on the worker
path, tracked in `docs/issues/langchain_responses_api_streaming.md`. Its
circuit breaker (`_check_empty_response_streak` in `src/graph.py`) and the
pending raw-response-capture diagnostic remain in place; this flip neither fixes
nor worsens it.
