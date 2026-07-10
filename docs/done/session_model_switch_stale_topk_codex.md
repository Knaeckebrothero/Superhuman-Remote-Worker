# Session model switch leaks stale `top_k` onto gpt-5.x/codex → Responses-API 400

**Status:** ✅ DONE 2026-07-01 (develop) — fix shipped as
`5163238e refactor(loader): drop unsupported top_k parameter for Codex proxy`
(`src/core/loader.py` + `tests/test_loader_routing.py`). Live-confirmed working on a
real gpt-5.5/codex session (2026-07-10). Investigated 2026-06-30 (dev thread
`e496d293`). **Severity:** medium — a session switched from a `top_k`-bearing family
(gemma/gemini) to a gpt-5.x/codex model hard-fails on the *next* turn with a 400, and
the stale value persists for the rest of the session. Gateway-routed models are
unaffected (see §"Why the gateway path is immune").

**Deferred follow-up (not fixed; low priority):** the underlying leak is broader than
codex. `_apply_settings_matrix` is *additive-only* and the session-switch path
re-resolves on a *dirty* base, so **any** family sampling param the new family omits
can survive a switch. This is masked in practice by the LiteLLM gateway's
`drop_params` (every gateway-routed model) plus this codex-lane guard. The only
remaining uncovered sliver is a gateway-down direct fallback for a *non-codex*
OpenAI-family model. Not worth chasing until it bites — chasing it via the matrix
would fight the "tolerate params, sanitize at the gateway" strategy (see §"Why not
the matrix"). 

**Related:** `docs/done/litellm_gateway_drops_gpt_codex_reasoning_capture.md` (why
codex deliberately bypasses the gateway — the direct lane this bug rides on) ·
`docs/done/context_budget_uses_base_model_not_phase_models.md` (documents the same
`gemma top_k=64` value as "wrong, but dropped by LiteLLM"; also the worker phase-pin
path that already clears it) · `docs/done/session_model_switch_stale_context_manager_empty_response.md`
(sibling: same trigger — session model switch — different stale-state symptom) ·
`docs/features/route_all_models_through_litellm_gateway.md` ·
`docs/features/agent_llm_factory_collapse.md` (item 4: retire `_create_codex_llm` once
codex routes through the gateway — the real end-state).

## Repro (observed)

Dev thread `e496d293-e0be-4bf6-999f-043cee2eee5e`, 2026-06-30. Session started on the
`persistent_defaults` default model `RedHatAI/gemma-4-31B-it-FP8-Dynamic` (gemma
family).

- **Turn 1** ("um was geht es in dem compliance cloud Ordner?") — **succeeded**, full
  answer.
- User **switched the model** to `gpt-5.5` (`provider: codex`, `base_url:
  http://srw-codex-proxy:8317/v1`).
- **Turn 2** ("Geht das auch etwas genauer?") — **failed**:

> Error code: 400 - {'detail': 'Unsupported parameter: top_k'}

(The `{'detail': ...}` is the upstream ChatGPT/Codex Responses-API error body relayed
verbatim through the Gin proxy — the proxy's *own* errors are `{'error': {...}}`.)

## Root cause chain

1. **The default model sets `top_k`.** gemma family →
   `config/model_config_matrix.yaml:482` `top_k: 64` (Google's official Gemma
   sampling). Applied to the session config by `_apply_settings_matrix`, so
   `llm.top_k = 64` is baked in from turn 1.
2. **The switch re-resolves on a DIRTY base.** `_handle_config_update`
   (`src/api/persistent_app.py:4305`) rebuilds config from
   `base_dict = dataclasses.asdict(_session.config)` (`:4368`) — the *already-resolved*
   config, gemma's `top_k=64` included — then re-runs `_apply_settings_matrix` (`:4376`).
   This is the only `_apply_settings_matrix` caller that starts from a resolved config;
   every other site (e.g. `persistent_app.py:1121`, initial load) starts from a clean
   file base.
3. **The matrix is additive-only — it never clears the stale value.**
   `_apply_settings_matrix` (`src/core/loader.py:723`) only *writes* keys present in the
   new family's settings (`:776`, `if key not in expert_llm_keys: data["llm"][key] =
   value`). The `gpt-5`/`codex` families define no `top_k`, so nothing overwrites the
   stale `64` → it survives onto the gpt-5.5 config.
4. **codex is dispatched DIRECT to the proxy, bypassing the gateway.** The dispatcher
   routes `provider == "codex"` straight to `srw-codex-proxy` (Responses API) instead of
   through the LiteLLM gateway — deliberately, because the gateway normalizes to Chat
   Completions and strips the gpt-5.x reasoning summary (see
   `litellm_gateway_drops_gpt_codex_reasoning_capture.md`). So codex is the one lane with
   **no `drop_params` safety net**.
5. **The codex factory forwarded `top_k`.** `_create_codex_llm` (`src/core/loader.py:3590`)
   put `extra_body["top_k"] = config.top_k` → the OpenAI SDK sent it to
   `/v1/responses`. `top_k` is not a valid Responses-API parameter → the ChatGPT/Codex
   backend rejects it with 400 `Unsupported parameter: top_k`.

## Why the gateway path is immune (and turn 1 worked)

The LiteLLM gateway ran `drop_params: true` (+ per-model `n: true`) at incident time,
so it silently drops params the target doesn't support. gemma is gateway-routed to
vLLM (which *accepts* `top_k`), so turn 1's identical `top_k=64` sailed through.
Verified on k3d 2026-06-30: `gemma-4-moe` + `top_k` through the gateway → **HTTP 200**.
`context_budget_uses_base_model_not_phase_models.md` documents this same value as
"wrong, but dropped by LiteLLM." The worker/phase-pin path is also unaffected — it
re-derives each phase's family params (clears `top_k` for gpt-5); only the session
model-switch path leaked.

## Fix (implemented 2026-07-01 — `5163238e`)

`_create_codex_llm` no longer forwards `top_k`. Removed:

```python
if config.top_k is not None:
    extra_body["top_k"] = config.top_k
```

…replaced with a comment (`src/core/loader.py:3638`) explaining the rationale: the
codex bypass lane opted out of the gateway to preserve reasoning capture, so it must
**self-sanitize** and replicate the gateway's param-dropping. The Responses API never
accepts `top_k`, so the forward was always wrong for this lane. (The sibling factories
`_create_openai_llm:3057` etc. still forward `top_k` — correct for OpenAI-compatible
endpoints like vLLM that accept it.)

## Why not "fix the matrix"

The tempting root-cause fix — make `_apply_settings_matrix` clear stale non-pinned
family params on a switch — was **rejected**. It fights the codebase's deliberate
"tolerate sloppy params, sanitize at the gateway chokepoint" strategy, is redundant
for the ~all gateway-routed models, needs a hand-maintained list of "family params"
(rots when a family adds `min_p` etc.), and rests on an incomplete pin set (the switch
handler only knows the current patch's keys, not accumulated user intent). The
architecture-aligned fix is: the bypass lane self-sanitizes (this fix); the gateway
handles everything else; the eventual end-state routes codex through the gateway too
and retires `_create_codex_llm` (`agent_llm_factory_collapse.md` item 4, blocked on the
gateway's reasoning-summary stripping).

## Verification

- **Unit (TDD):** `test_top_k_not_forwarded`
  (`tests/test_loader_routing.py::TestCodexLLMCreation`) — a `top_k=64` codex config
  produces no `top_k` in `extra_body`/`model_kwargs`. Watched it fail first
  (`assert 'top_k' not in {'top_k': 64}`), then pass. Full loader-routing suite green
  (61), adjacent LLM/dispatch suites green (180).
- **Diagnosis proof:** the live transcript above (turn 1 gemma ✅ → turn 2 codex ❌).
- **k3d:** gemma+`top_k` via the gateway → 200 (why turn 1 worked). A full local codex
  E2E was initially blocked (local codex-proxy had no ChatGPT OAuth account); once a
  real account was connected, the fix was confirmed working on a live gpt-5.5/codex
  session (2026-07-10). The `top_p`/`temperature` the lane still forwards did **not**
  trip a follow-on rejection — this Codex backend tolerates them.
