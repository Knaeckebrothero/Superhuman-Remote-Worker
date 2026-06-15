# Gemini 3.x thinking models — temperature-0 degenerate loop → empty responses

**Date:** 2026-06-15
**Status:** Partial fix landed on `develop` (code-only, lint-clean). **NOT yet
verified end-to-end** — the k3d session test is the gate and has not run.
Hardening + diagnostics follow-ups open (see checklist).
**Component:** Agent LLM factory — `src/core/loader.py` (`_create_google_llm`
and the `_is_gemini_*` helpers). Touches the persistent-session path
(`src/persistent_graph.py` thinking-block handlers, `src/api/persistent_app.py`
persistence) and the audit trail (`src/core/archiver.py`).
**Severity:** High for Gemini 3.x sessions — the user-visible failure is a turn
that does a run of silent tool calls and then returns
"⚠ The model returned an empty response." with no answer.

## Symptom

Reported on session `91ae13f5` (main/dev cluster), model `gemini-3.5-flash`,
autonomous persistent session. The user sent "Hey, how are you today?"; the
agent made ~15 tool calls with **no visible text and no thinking**, then the
final synthesis turn returned **empty content** and the cockpit showed the
empty-response placeholder.

## Root cause (corrected after investigation)

**Not** "the model legitimately spent 16k tokens reasoning about a greeting" —
that framing was wrong. The real cause is a **degenerate token-filling loop that
`temperature: 0.0` (our stack-wide default) triggers in Gemini 3.x thinking
models.** The model burns the entire `max_output_tokens` budget producing
reasoning/garbage and returns `finish_reason=MAX_TOKENS` with empty `content`.

### Evidence

- **Wire truth (MongoDB `llm_requests`, last call of the turn):**
  `content: ""`, `finish_reason: "MAX_TOKENS"`, `tool_calls: []`; the persisted
  `thread_messages.metrics` showed `output_tokens == reasoning_tokens == 16376`
  (≈ the `min(16384, ctx//4)` derived cap). 64s latency → ~16k tokens really
  were generated, just none of them visible answer.
- **Baseline (direct Gemini API, bare prompt):** "Hey, how are you today?" →
  **239–305 thought tokens**, `STOP`, clean answer, every time. So 16k is **not
  genuine reasoning** — the user's skepticism was correct.
- **Reproduction (direct API, the real 64k context, only temperature varied):**
  at **temp 0.0** it reproducibly filled the entire 16,382-token budget with
  **empty output** (2/3 runs byte-identical — temp 0 is ~deterministic); at
  **temp 1.0** the catastrophic 16k runaway did not occur. *Caveat:* the context
  had to be flattened to replay (see thought-signature item below), which baits
  function-call emission and inflates the absolute meltdown rate — the trustworthy
  parts are the **relative** temperature effect and the 16k signature.
- **External corroboration:** Google staff document a Gemini 3 Flash infinite
  reasoning loop (~15,356 thoughts / 16,233 total — near-exact match) and state
  that **`temperature: 0.0` traps the model in the loop; `temperature: 1.0`
  mitigates it.** `max_output_tokens` on Gemini 3 is a *combined* cap over
  thinking + answer, so a loop starves the answer entirely.

## Fix applied (`src/core/loader.py::_create_google_llm`)

Minimal and root-cause-aligned — two behavioral changes for Gemini thinking
models:

1. **Floor `temperature` to `1.0`** for `gemini-3.x` (incl. 3.5) — the dominant
   loop trigger. Constant `_GEMINI_THINKING_MIN_TEMPERATURE`; only lifts a value
   below the floor, leaves explicit higher values alone.
2. **`include_thoughts=True`** for all Gemini thinking models (2.5 + 3.x) — so a
   future loop is *visible* instead of silent, and so the existing thinking
   capture chain (`persistent_graph.py` `type=="thinking"` handlers →
   `_extract_thinking` → `thread_messages.thinking`) finally populates. The
   capture machinery already existed; `include_thoughts` was the missing switch.

### Deliberately NOT done (these were premised on the wrong "budget exhaustion" cause)

- **No forced `thinking_level`.** Mapping our `reasoning_level` default ("high")
  → `thinking_level="high"` is counterproductive: "high" *worsens* looping and
  3.5-flash's own default is "medium". We leave thinking depth at the model
  default.
- **No inflated `max_output_tokens`.** A loop fills whatever cap it is given;
  a 32k headroom just wastes ~2× the tokens/latency before the guard fires.
  Legit thinking is ~250 tokens, so the default 16k cap is ample.
- **No dedicated `gemini-3` family.** The single `gemini` family block
  (`config/model_config_matrix.yaml`) already supplies 1M context etc.; the only
  generation-specific setting is temperature, kept in code beside the rest of the
  thinking logic. Detection is by model name (`_is_gemini_3_or_later` =
  `"gemini-3"` substring), covering 3 / 3.1 / 3.5.

## Remaining hardening / verification work

- [ ] **End-to-end verify on k3d (the gate).** Run a real `gemini-3.5-flash`
      session — especially an autonomous, tool-using one resembling the failing
      context — and confirm: (a) it answers instead of `MAX_TOKENS`-empty, and
      (b) thinking is captured + displayed. Nothing is "fixed" until this passes.
- [ ] **`temperature: 1.0` is not a 100% guarantee.** The repro still degenerated
      1/3 even at temp 1.0 (likely inflated by the flat reconstruction). The
      empty-response guard (`persistent_graph.py:1248` / `:1358`) remains the
      backstop. Consider detecting `finish_reason=MAX_TOKENS` + empty content and
      auto-retrying once (e.g. a "stop thinking, answer now" nudge or a one-shot
      temperature bump) instead of surfacing a dead turn. Watch for recurrence
      now that loops are observable.
- [ ] **Persist `finish_reason` / `response_metadata` on `thread_messages`.**
      Currently NULL for every session AI row — this entire investigation needed
      the MongoDB audit trail because the row didn't carry `MAX_TOKENS`.
      `save_thread_message` already accepts `response_metadata`;
      `_persist_one_message` (`src/api/persistent_app.py` ~3713) just doesn't
      populate it from the message.
- [ ] **Audit serializer drops `usage_metadata`.** `_message_to_dict`
      (`src/core/archiver.py`) stores `usage_metadata: {}` and
      `metrics.token_usage: {}` in `llm_requests`, so per-call reasoning-token
      breakdowns aren't recoverable from the audit trail.
- [ ] **Thought-signature not persisted → Gemini 3 + tools breaks on resume.**
      Replaying the persisted tool-calling history 400s with *"Function call is
      missing a thought_signature in functionCall parts"* (reproduced against
      `91ae13f5`'s context). Live turns work (signatures held in-memory); resume
      does not. Needs capture + persist + echo of thought signatures, or a
      strip/repair strategy. **Separate bug — may warrant its own doc.**
- [ ] **Metrics mis-attribution (separate, deferred by decision).**
      `_save_turn_ai_messages` broadcasts the *final* LLM call's `turn_metrics`
      onto every AI message of a multi-call turn (all 16 rows of `91ae13f5`
      showed identical `16376/16376`). Per-generation token accounting is wrong.
- [ ] **Tuning / coverage.** Decide whether to expose a Gemini thinking-depth
      knob (we no longer map `reasoning_level`) and/or lower the default for flash
      models. Extend explicit handling to Gemini 2.5 (`thinking_budget`) if those
      models get used. Confirm the temp floor takes effect after deploy on the
      main (dev) and prod clusters (the model is registered identically there).
