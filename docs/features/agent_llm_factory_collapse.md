---
tags:
  - feature
  - llm
  - gateway
  - litellm
  - agent
  - cleanup
aliases:
  - phase 4 agent simplification
  - agent factory collapse
  - llm factory collapse
related:
  - "[[route_all_models_through_litellm_gateway]]"
  - "[[system_provider_models_bypass_gateway_unmetered]]"
---

# Agent LLM factory collapse — Phase 4 of the gateway migration

**Status:** **Proposed / scoped 2026-06-28** (sharpened from a live codebase trace, not yet built). This is **Phase 4** of `route_all_models_through_litellm_gateway.md` — the agent-side cleanup that makes the **code** uniform, now that P0–P3 + the `*` wildcard make the **runtime path** uniform.

**One-line goal:** retire the six provider-specific LLM factories from the agent's hot path so every model is built by one OpenAI-compatible factory, captured one way — while keeping a minimal direct **bypass lane** for the gateway-down fallback.

## 1. Why now — the distinction P0–P3 exposed

"Everything routes through LiteLLM" is **two** claims, and only one was true after P3:

- **Transport-uniform (true):** any model that *routes through the gateway* comes back as OpenAI `/chat/completions` — `reasoning_content`, `tool_calls`, `usage` normalized by LiteLLM.
- **Code-uniform (NOT true yet):** the agent's `create_llm` (`src/core/loader.py:2701`) still dispatches on `config.provider` to **seven** factories. Routed traffic only *happens* to converge on one of them because the dispatch canary forces `provider="openai"`.

Phase 4 closes the gap: make the code match the runtime.

## 2. Current state (the factory inventory)

`create_llm(config, limits)` — `src/core/loader.py:2701`, dispatches on `config.provider` (defaults to `openai` when unset, loader.py:2730):

| Factory | loader.py | Reached today by |
|---|---|---|
| `_create_openai_llm` (`ReasoningChatOpenAI`) | 2777 | **every gateway-routed model** (canary forces `provider="openai"`) + native OpenAI-compatible endpoints |
| `_create_anthropic_llm` | 2937 | direct/bypass only (SRW runs no Anthropic → effectively dead) |
| `_create_google_llm` | 3026 | **gemini direct** (google not canaried on dev) — holds the gemini-3 temp floor + `parallel_tool_calls`/`include_thoughts` traps |
| `_create_groq_llm` | 3092 | direct/bypass only (no groq in catalog today) |
| `_create_openrouter_llm` | 3152 | direct/bypass only (minimax is canaried → uses openai factory) |
| `_create_mistral_llm` | 3298 | direct/bypass only (no mistral in catalog) |
| `_create_codex_llm` (client-side Responses API) | 3377 | codex **bypass** only (codex-through-gateway uses the openai factory + the gateway's Chat↔Responses bridge) |

**The convergence mechanism:** the dispatch canary branch (`orchestrator/main.py` `_inject_dispatch_credentials` / `_inject_model_credentials`) sets `llm_over["provider"] = "openai"` + the gateway `base_url`/virtual key whenever `_should_route_via_gateway` is true. So a routed gemini/minimax/codex all land on `_create_openai_llm`. With `routedProviders: ["*"]`, **all** system/codex models route → the six non-openai factories are reached **only** via the direct fallback.

**Auxiliary is already collapsed.** Aux LLMs are built by the same `create_llm` (`src/agent.py:494` worker, `src/api/persistent_app.py:1381` / `4300` sessions) and pass **no** `provider`, so they always default to `_create_openai_llm`. Aux routing rides `aux_config.base_url`, which dispatch injects with the gateway URL on the same canary path as the main model. So aux needs **no** factory work in Phase 4 — it's already on the uniform path (see §5 for the metering caveat).

## 3. Scope — five work items

1. **Collapse to the single openai factory.** Once `*` is the standing config everywhere, the six non-openai factories are dead on the routed path. Retire them from `create_llm`'s dispatch, leaving `_create_openai_llm` as the sole hot-path factory. **Constraint:** the gateway-down fallback (P0) still needs a direct path — see item 5.

2. **Relocate the two gemini traps.** The gemini-3 temperature floor + `include_thoughts` live in `_create_google_llm`; when gemini routes through the gateway it hits `_create_openai_llm`, which never applies them. Move them to the **gateway entry** (per-model `litellm_params` set in `build_desired_models`) so they apply regardless of factory. *Partly done:* P1 already sets `drop_params: true` on system rows, which covers the `parallel_tool_calls` guard — so this item is mostly the **temp floor**. Verify the `drop_params` path actually neutralizes `parallel_tool_calls` for gemini-through-gateway.

3. **Demote the multi-shape reasoning tap to a safety net.** The agent's reasoning extraction handles several shapes (`reasoning_content` / `thinking_blocks` / chat-template native). LiteLLM normalizes everything to `reasoning_content`, so make that the **primary** path and keep the other shapes only as a fallback for the bypass lane. Simplification, not a correctness fix.

4. **Retire the client-side Responses path.** `_create_codex_llm` (client-side `/responses`) is dead now that codex routes through the gateway's Chat↔Responses bridge (confirmed live on dev — codex GPT-5.5 metered through the gateway). Remove it from the hot path; keep only as bypass (item 5).

5. **Keep a minimal bypass lane.** The P0 gateway-down fallback routes **direct** with provider creds — so we cannot delete *all* direct factories. Phase 4 must define the retained fallback path. **Open decision:** (a) keep the provider factories solely for the fallback, or (b) route the fallback *also* through `_create_openai_llm` against each provider's OpenAI-compatible base_url (works for openrouter/groq/mistral/openai-compat; **does not** work for native Google/Anthropic SDKs). Recommendation: (b) for OpenAI-compatible providers + retain `_create_google_llm` only if a native-Gemini fallback is required. This is what bounds how much actually gets deleted.

## 4. Sequencing

- **Phase 4 follows the `*` rollout.** While any provider is non-canaried (gemini-direct on dev today), its factory is live and cannot be collapsed. The standing config must be `routedProviders: ["*"]` everywhere before the factories are provably dead on the routed path.
- **Independent of P5 (HA)**, but the bypass-lane design (item 5) is the same fallback that mitigates the gateway SPOF until HA lands — so design them with P5 in mind.

## 5. Adjacent — metering refinements (P3 follow-ups, not factory work)

Surfaced by the same trace; track here so they aren't lost:
- **Aux is metered but not labeled.** Aux calls land in the spend log → `usage_events` by model + scoped-key user/project, **blended with main** (no aux/main flag). e.g. dev's 24k-row `gemma` line mixes main + aux gemma calls. Add an aux marker if per-channel cost is ever needed.
- **Per-job attribution deferred.** The gateway carries user/project (scoped key) but not `job_id`; `usage_events` LLM rows attribute to user/project, not job. Tag requests with `job_id` via gateway metadata to close this.

## 6. Verification plan

- **Full regression** incl. a **tool-using gemini turn** and a **reasoning-heavy codex turn**, both *through the gateway*. Assert: `reasoning_content` captured, `tool_calls` captured, the gemini temp floor honored (now via the gateway entry, item 2), no `parallel_tool_calls` 400.
- **Bypass lane:** simulate gateway-down (scale `srw-litellm` → 0, as in the P0 fallback test) and confirm each retained provider class still serves via the direct path.
- Confirm aux still builds + runs (memory extraction / summarization) on the openai factory unchanged.

## 7. Risks & open questions

- **The bypass-lane tension (item 5)** is the crux: native Google/Anthropic are not OpenAI-compatible without the gateway, so a true "delete all but one factory" is only safe if the fallback for those providers is acceptable to drop (SRW runs no Anthropic; gemini's need for a *native* fallback is the open question).
- **Residual provider quirks** LiteLLM doesn't fully normalize (beyond the two known gemini traps) could surface only under real load — hence the tool-using gemini turn in the regression.
- **Don't collapse before `*` is standing everywhere**, or a non-canaried provider hits a deleted factory.
