# OpenRouter auxiliary silently misrouted to api.openai.com → 401 → aux degraded

**Status:** ROOT-CAUSED + FIXED (uncommitted on `develop`); unit-tested; live
verification pending.
**Found:** 2026-07-03, dev cluster (`--context main`, ns
`superhuman-remote-worker`), session `182a8fc1-625c-4112-a9cc-0c2439506f1e`.
**Sibling:** [openrouter_auxiliary_crashes_session_via_memory_reranker.md](openrouter_auxiliary_crashes_session_via_memory_reranker.md)
— same trigger (OpenRouter as the auxiliary), different mechanism. That one was
the memory reranker borrowing the aux `base_url`; this is the auxiliary LLM
itself.

## Symptom

A session with an **OpenRouter-direct auxiliary**
(`auxiliary: {model: openrouter/minimax/minimax-m3, provider: openrouter}`)
booted and answered turns fine, but **never generated a title** ("Untitled
Session"). The agent log showed the real damage:

```
Auxiliary override applied: model=openrouter/minimax/minimax-m3, base_url=default
...
AUXILIARY MODEL DEGRADED: model=openrouter/minimax/minimax-m3 — 3 consecutive
  auxiliary failures (latest task 'citation_verification':
  AuthenticationError: 401 - Incorrect API key provided: sk-or-v1***...***fc51.
  You can find your API key at https://platform.openai.com/account/api-keys).
  Memory extraction, knowledge curation and session titles are silently
  disabled until the auxiliary model is reachable again.
```

Read the error carefully: the key is an **OpenRouter** key (`sk-or-v1…`) but the
error is **OpenAI's** ("platform.openai.com") — the request went to
`api.openai.com`, which 401s a non-OpenAI key. So the auxiliary ran for 15
minutes with **no titles, no memory extraction, no knowledge curation, no
citation verification**, and the only signal was one agent-log line. The
`agents.aux_degraded` DB flag was still `false`.

## Root cause — the auxiliary's `provider` is dropped, so it defaults to OpenAI

Four links, all confirmed in code:

1. Orchestrator injects `auxiliary: {model, provider: "openrouter"}` into the
   session config override.
2. `AuxiliaryConfig` (`src/core/loader.py`) **had no `provider` field**, and
   `_parse_auxiliary_config` didn't read it → the injected `provider` was
   **dropped on parse**.
3. All three aux build sites — `src/agent.py` (worker),
   `src/api/persistent_app.py` attach + `config.update` — construct
   `LLMConfig(model, base_url, api_key, …)` **without `provider`**.
4. `create_llm` did `provider = config.provider.lower() if config.provider else
   "openai"` — **no auto-detect** from the `openrouter/` prefix (the docstring's
   "auto-detect from model name" was aspirational). So `provider="openai"` →
   `base_url=None` → `api.openai.com`, carrying the `sk-or-v1` key → 401.

`_create_openrouter_llm` is itself correct — it strips the `openrouter/` prefix,
defaults `base_url` to `https://openrouter.ai/api/v1`, and reads
`OPENROUTER_API_KEY`. It just never got called for the auxiliary.

### Why only OpenRouter, and why it surfaced now

Every *custom* endpoint (e.g. `gemma-4-moe` → `ai.h4ll.app`) stores an explicit
`base_url`, so the default `openai` class + that base_url works regardless of
provider. **OpenRouter is the only provider whose `base_url` is meant to be
resolved *from* `provider`.** While the short-lived **LiteLLM proxy** was in
place (added then reverted a few days prior — commits `57fbd436` /
`81479875` / `dbfb04db`), every model funnelled through the proxy with a
`base_url` set, so the missing aux `provider` never mattered. Ripping the proxy
out exposed it. This is a LiteLLM-removal fossil: the aux path was never
rewired to carry `provider`.

## Fix

### Part A — thread the provider (correctness)

- `AuxiliaryConfig` gains a `provider` field; `_parse_auxiliary_config` reads
  `data.get("provider")`.
- `provider=aux_cfg.provider` threaded into `LLMConfig(...)` at all three aux
  build sites (`src/agent.py`, `src/api/persistent_app.py` ×2).
- **Safety net:** `create_llm` auto-detects the `openrouter/` model prefix when
  `provider is None` (honours the documented contract; protects any un-threaded
  path, e.g. the `CITATION_LLM_*` verifier, present or future).

### Part B — main-model fallback + never-silent failure (resilience)

The auxiliary is **load-bearing**: it drives context compaction/summarization,
so a dead aux model eventually **crashes** the session, not just degrades it.
Silent degradation is therefore doubly wrong. Per the "prefer a loud error over
a half-working agent" principle:

- `AuxiliaryLLM` gains a `fallback_llm` (the **main session/worker model**, which
  is always present and working). A new `_ainvoke_fallback` chokepoint wraps
  every call shape — `chain()` (structured), `agent()` (tool loop), and a new
  public `ainvoke()` for the title-gen raw call. On aux-model failure it
  **LOUD-logs**, retries on the main model, and returns — the session keeps
  running. It raises only when there is **no** fallback (aux already *is* the
  main model) or the fallback **also** fails.
- **Halt-on-true-dead-end is preserved:** when both fail, `chain()` raises →
  the summarizer's existing `SummarizationFailed("aux_unavailable")` fails the
  turn/session loud (no silent context overflow).
- **Surfacing:** `AuxHealth` tracks aux-model reachability
  (`mark_aux_unreachable`/`mark_aux_reachable`) *independently* of the
  caller-driven per-task health — so a fallback *success* (caller records
  success, clearing the legacy `_degraded`) can't mask that the aux model is
  down. `heartbeat_summary()`/`snapshot()` now report `degraded` (incl.
  `on_fallback`) + `last_fallback_error`, so `agents.aux_degraded` and the admin
  badge light up while on fallback.
- Fallback wired at the three build sites: worker →
  `self._summarization_llm` (only when a *dedicated* aux model is configured);
  persistent attach → the main session `llm`; `config.update` →
  `_session._llm`.

## Tests

- `tests/test_auxiliary_fallback.py` (new, 14 tests): provider parsing +
  `create_llm` prefix auto-detect + explicit-provider-wins; fallback
  success/loud-degrade/no-fallback-raises/both-fail-raises/recovery; and the
  key **"fallback success does not mask aux down"** heartbeat guarantee.
- Full touched-area suite green (975 passed); `ruff check` + `ruff format`
  clean.

## Follow-ups / not done

- Live verification on a real cluster (k3d can't reproduce the OpenRouter path —
  no OpenRouter model in the registry; the fallback path can be forced with a
  deliberately-broken aux endpoint). The cleanest real proof is re-running
  session `182a8fc1` on dev after deploy: expect it to route to
  `openrouter.ai` (title generates), or fall back loudly to the main model
  (`gpt-5.5`) with `aux_degraded=true`.
- Investigate why `agents.aux_degraded` stayed `false` during the incident
  despite the agent logging `DEGRADED` (heartbeat aux-health may not be wired
  for dual-mode session agents) — Part B's reachability flag should now drive
  it, but confirm the heartbeat→orchestrator→DB path fires.
- Consider a session-level (not just admin-badge) "running on fallback model"
  banner in the cockpit.
