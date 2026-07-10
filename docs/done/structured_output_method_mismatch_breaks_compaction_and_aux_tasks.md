# Structured output is requested over one hardcoded channel — models that don't honor it silently break compaction, memory, citations, and triage

**Status:** ✅ RESOLVED 2026-07-10 — implemented in `194cdf22` (all four parts: per-family `structured_output_method` matrix key, `src/llm/structured_recovery.py` lenient recovery, `IdentityAnchor` strict-safe schema + `tests/test_aux_schema_strictness.py` CI lint, fail-fast classification + triage hardening; doc committed in `1fcbc068`). **Verified live same day:** the wedged 431.9k-token gpt-5.6-sol session was resumed on the new image, manual `/compact` succeeded on the first attempt (SESSION RESUMED → CONTEXT SUMMARIZED), and the session returned to normal turns at ~260k input — the rescue path below worked exactly as designed.
**Severity:** high — persistent-session compaction (manual `/compact` and auto) fails 3/3 whenever the aux model is MiniMax M-series, AND the main-model fallback is rejected on every strict OpenAI endpoint by our own schema. On MiniMax-main loop jobs the entire aux stack (memory extraction, assembly, citation verification) is dead because the fallback is the same model. Orchestrator message triage silently degrades to always-queue on the same class of failure.
**Component:** `src/services/auxiliary.py` (`chain()`/`agent()` → `with_structured_output`), `src/core/context.py` (`ConversationSummary`), `src/core/summarizer.py` (retry loop), `orchestrator/services/message_triage.py`, `config/model_config_matrix.yaml` (fix home).
**Related:** `docs/issues/codex_proxy_context_window_cap.md` (the wedged session this was found in), `docs/issues/web_search_full_page_content_bloats_session_context.md` (what inflated it), memory `project_persistent_compaction_double_failure`.

## TL;DR

"Give me JSON" can travel over four channels — `response_format: json_schema` (server-enforced grammar), `response_format: json_object` (JSON mode), tool/function calling, or prompt instructions. Support varies **per model and per transport**, and unsupported parameters are typically **dropped silently** — the request succeeds and free-form prose comes back. We hardcode one channel everywhere:

- The aux stack calls `with_structured_output(schema, include_raw=True)` with the langchain-openai 1.1.12 default `method="json_schema"` (`auxiliary.py:1123`, `:1237`).
- Message triage sends raw `response_format: {"type": "json_object"}` + bare `json.loads(content)` (`message_triage.py:160`).

When the serving path ignores the parameter, the model — never having seen any constraint — answers helpfully in prose or ```json-fenced``` markdown, and our parser rejects it (`Invalid JSON: expected value at line 1 column 1`). **Most captured failures contained perfectly valid JSON inside markdown fences.** The model did its job; we dropped the result.

The main agent path is NOT exposed — the graph uses `bind_tools` throughout, which is why MiniMax-M3 runs entire loop jobs with complex tool calls 800k-deep while simultaneously "failing" every aux task.

## Evidence (session pod `srw-agent-j-031b9945`, 2026-07-10 14:13Z, manual `/compact` at 431.9k tok)

Every summarize attempt fails twice, deterministically:

1. **Aux leg — MiniMax-M3, direct to `https://api.minimax.io/v1`** (endpoint `33d789eb`, "Minimax", ctx 524288):
   `ValidationError: Invalid JSON: expected value at line 1 column 1, input_value='You are absolutely right…'` — plain prose. Sibling pods show `ExtractMemoriesTask` / `AssembleMemoriesTask` failing with `input_value='```json\n[\n  {…}\n]\n```'` — **valid JSON, fenced**. MiniMax's API docs state `response_format` is only supported by MiniMax-Text-01; on M-series it is silently ignored (confirmed: [MiniMax-AI/MiniMax-M2.5#4](https://github.com/MiniMax-AI/MiniMax-M2.5/issues/4), [platform.minimax.io text-post docs](https://platform.minimax.io/docs/api-reference/text-post)).

2. **Fallback leg — gpt-5.6-sol via codex proxy**:
   `400 invalid_json_schema — "Invalid schema for response_format 'ConversationSummary': In context=('properties','identity_anchor','anyOf','0'), 'additionalProperties' is required to be supplied and to be false"` — `ConversationSummary.identity_anchor: dict | str | List[str]` (`context.py:101`); the bare `dict` renders as `{"type":"object"}` with no `properties`/`additionalProperties`, which OpenAI strict mode rejects **for the whole request**. Irony: that loose union was added to *tolerate* MiniMax quirks. Audited all other aux schemas — none carries a bare dict, which is why citation/memory fallbacks to strict endpoints work and only compaction's cannot.

The fold engine then retries 3× with 5s/15s backoff (`summarizer.py`, `MAX_ATTEMPTS=3`) — both legs deterministic, so ~70s of retry theater, then `SummarizationFailed(aux_unavailable)` → "Compaction aborted … keeping 52 messages uncompacted" (correct behavior: never compact behind a placeholder). The cockpit shows "retry 3/3" then nothing; the log says "AUX MODEL UNREACHABLE", which is wrong — it's reachable, its output is structurally unusable.

## Exposure map

| Path | Channel support | Result today |
|---|---|---|
| MiniMax M-series direct (`api.minimax.io`) | `response_format` silently dropped | Broken — all aux tasks fail parse |
| OpenRouter-routed models | Forwarded, but many upstream providers unsupported; OpenRouter **silently drops** params unless `require_parameters: true` | Nondeterministically broken (varies per route) |
| gemma-4-31B on local vLLM (default aux) | vLLM enforces json_schema via guided decoding | Works — why the aux stack seemed fine pre-MiniMax |
| Strict OpenAI endpoints (codex, API) as main/fallback | Enforced, but **rejects non-strict schemas** | 400 on `ConversationSummary` only (the `dict` union member) |
| Reasoning models on unenforced paths | `<think>` leaks into content ahead of the JSON | Parse fails even when JSON follows |
| `message_triage.py` (any model ignoring `json_object`) | Silent drop → prose → `json.loads` throws → caught | Triage silently degrades to always-"queue" |

Note on fallback wiring: `AuxiliaryLLM` nulls the fallback only on object **identity** (`fallback_llm if fallback_llm is not llm else None`, `auxiliary.py:984`), but session/job wiring constructs aux and main as separate instances — so a MiniMax-main loop job "falls back" from MiniMax-M3 to a second MiniMax-M3 client and fails identically twice.

## Fix (proper solution)

Four parts, ordered by leverage. Parts 1–2 are the reliability fix; 3–4 prevent recurrence and stop the stall.

### 1. Per-family structured-output method in `config/model_config_matrix.yaml`

The family-centered wiring already resolves prompts/params per family; add a key, e.g.:

```yaml
minimax-m3:
  structured_output_method: function_calling   # response_format silently dropped by api.minimax.io; tool calling is the channel this model is proven at
gpt-5.6:
  structured_output_method: json_schema        # default; explicit for clarity
gemma-4:
  structured_output_method: json_schema        # enforced by vLLM guided decoding; gemma is a weak tool-caller
```

Thread it through `resolve_model_settings()` → the `AuxiliaryLLM` constructor → both `with_structured_output(...)` call sites (`auxiliary.py:1123`, `:1237`), defaulting to `json_schema` when the family doesn't specify. Remember the hyphen-key rule for matrix families. This routes each model over the channel it actually supports — MiniMax gets the tool-calling muscle it already flexes on the main path.

**Critical wiring detail — the aux and fallback legs need DIFFERENT methods.** `_ainvoke_fallback` currently applies the *same* `build_runnable` lambda to `self.llm` and `self.fallback_llm`. With per-family methods that's wrong: aux=MiniMax needs `function_calling` while fallback=gpt-5.6-sol needs `json_schema` (strict). Change the builder contract to receive the method per leg — e.g. `build_runnable(llm, method)` with `AuxiliaryLLM` storing `structured_output_method` (for `self.llm`) and `fallback_structured_output_method` (for `self.fallback_llm`), each resolved from its own model's family. A single method bound to both legs would quietly re-break whichever leg it doesn't match.

**Construction sites that must resolve + pass the method(s)** — all five, each already knows (or can resolve) both model names:
- `src/agent.py:498` (boot-time aux), `:543` (config-hydrated aux), `:595` (citation-verify aux)
- `src/api/persistent_app.py:1463` (session aux override — already calls `resolve_model_settings(aux_cfg.model, ...)`; add the same for the main model to get the fallback method), `:4447` (session aux rebuild)

### 2. Universal lenient-recovery layer on parse failure

We already pass `include_raw=True`, so the raw text is available at the failure site. Add one helper (suggested home: `src/llm/structured_recovery.py`):

```
recover_structured(raw_text, schema) -> BaseModel | None
  1. strip <think>…</think> blocks
  2. strip markdown code fences (```json … ```)
  3. extract the first balanced {...} or [...] span
  4. schema.model_validate_json(...) — return instance or None
```

Apply it in `AuxiliaryLLM._ainvoke_fallback`/`chain()` before declaring the aux leg failed, and reuse it in `message_triage.py` in place of the bare `json.loads`. This is the backstop for every unenforced path (including OpenRouter roulette) and would have recovered the majority of captured failures outright.

**Hook BOTH failure surfaces.** Parse failure shows up two ways depending on langchain's path: (a) a `ValidationError` **raised** from inside `ainvoke` (what the 2026-07-10 logs show — it propagates to `_ainvoke_fallback`'s `except`), and (b) a returned `include_raw=True` dict with `parsed=None` + `parsing_error` set and no exception. Recovery must run in both places. The raw text lives at `raw_result["raw"].content` in case (b); in case (a) the cleanest seam is to catch in `_ainvoke_fallback` per leg, re-fetch raw via a plain `ainvoke`-shaped call — or simpler, wrap the runnable so the parser step itself falls back to `recover_structured` before raising. Write regression fixtures for both shapes.

### 3. Make `ConversationSummary` strict-mode safe + CI schema lint

Replace the bare `dict`:

```python
class IdentityAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_role: str = ""
    current_task: str = ""
    active_constraints: List[str] = Field(default_factory=list)

identity_anchor: IdentityAnchor | str | List[str] = ""
```

Keep `format_structured_summary` (`summarizer.py:83-98`) and the `coerce_all_fields` validator in sync — they currently `isinstance(..., dict)`; accept the submodel (or coerce via `model_dump()`).

Then add a CI unit test (e.g. `tests/test_aux_schema_strictness.py`) that runs **every** aux output schema through the openai SDK's `openai.lib._pydantic.to_strict_json_schema()` — it raises on strict-incompatible schemas, so the next `identity_anchor`-class bug fails at commit time, not in production at 431k tokens. The inventory (note the first lives in `context.py`, not `auxiliary.py` — don't glob one file): `ConversationSummary` (`src/core/context.py:66`), and from `src/services/auxiliary.py`: `ExtractedMemory`, `ExtractedMemories`, `CurationResult`, `KnowledgeAssemblyResult`, `AssemblyAction`, `AssemblyResult`, `IngestionVerdict`, `KnowledgeVerdict`, `CitationVerdict`. Better: derive the list dynamically from every `AuxTask.output_schema` property so new tasks are covered automatically.

### 4. Fail fast + honest reporting

- In the fold loop's error classification (alongside `is_overflow_error`): treat `BadRequestError` with `code: invalid_json_schema` and pydantic `ValidationError` as **deterministic** → no retry (or one retry only after the lenient-recovery layer, which changes the odds). Kills the 70s stall.
- Fix the no-op fallback: compare **model names**, not object identity, when nulling `fallback_llm`; when fallback == same model, skip the second call and say so.
- Reword "AUXILIARY MODEL UNREACHABLE" → distinguish transport failure ("unreachable") from output failure ("returned non-conforming output for <task>") — they need different operator responses.
- Surface a distinct `compaction.failed` reason in the cockpit (schema-rejected vs model-unavailable) instead of the spinner dying silently after "retry 3/3".

## Implementation touch list

| File | Change |
|---|---|
| `config/model_config_matrix.yaml` | `structured_output_method` per family (minimax-m3 → `function_calling`; others explicit `json_schema`) |
| `src/core/loader.py` | `resolve_model_settings()` returns the new key |
| `src/services/auxiliary.py` | ctor takes `structured_output_method` + `fallback_structured_output_method`; `build_runnable(llm, method)` per-leg; recovery hook in `chain()`/`_ainvoke_fallback` (both surfaces); fallback null-check by model name not identity; "UNREACHABLE" rewording |
| `src/llm/structured_recovery.py` (new) | `recover_structured(raw_text, schema)` — think-strip, fence-strip, first-JSON-span, validate |
| `src/agent.py:498,543,595` + `src/api/persistent_app.py:1463,4447` | resolve + pass method(s) at all five `AuxiliaryLLM(...)` sites |
| `src/core/context.py` | `IdentityAnchor` submodel replaces bare `dict`; `coerce_all_fields` sync |
| `src/core/summarizer.py` | `format_structured_summary` accepts submodel; deterministic-error classification (no retry on `invalid_json_schema` / `ValidationError`) |
| `orchestrator/services/message_triage.py` | replace bare `json.loads` with `recover_structured` (or shared equivalent — note orchestrator can't import from `src/`; small copy or shared util) |
| `tests/test_aux_schema_strictness.py` (new) | strict-lint all `AuxTask.output_schema` + `ConversationSummary` via `to_strict_json_schema()` |
| `tests/` | recovery fixtures (fenced/prose/think-prefixed, both failure surfaces); per-leg method unit tests; retry classification |
| Cockpit (optional, small) | distinct `compaction.failed` reason display |

## Rescue path for an already-wedged session

Compaction runs agent-side, so after the fix ships: roll the agent image, let the session land on a fresh pod (end + resume works cross-pod via the Postgres checkpointer), then manual `/compact`. The fold input is tiny (~1.9k tokens for 36 messages — observation masking truncates old tool results to placeholders), so a single working summarize call drops the history far below the codex ~400K wall. Until then there is no in-place rescue: both legs are deterministically broken on the running image.

## Verification plan

1. Unit: matrix resolution → `method` reaches both `with_structured_output` call sites; recovery helper on fenced/prose/think-prefixed fixtures; strict-lint test over all aux schemas; retry classification.
2. k3d: session with aux=MiniMax-M3 → `/compact` succeeds on attempt 1 via function_calling; kill the aux key → fallback to main succeeds (strict schema now accepted); check `aux_degraded` heartbeat wording.
3. Live: loop job on MiniMax main — memory extraction/assembly produce rows again (currently 100% ValidationError); citation verification verdicts return.
