# Hardcoded model defaults in agent YAMLs

## Problem

The agent's base config YAMLs ship a hardcoded model ID
(`RedHatAI/gemma-4-31B-it-FP8-Dynamic`) as the `llm.model` and
`auxiliary.model` defaults. When a thread is created without an explicit
model pin (request body, user prefs, or system default chat pin), the
agent's `_session.config.llm.model` falls through to this string —
which carries no transport. The OpenAI factory then constructs a client
with `base_url=None` and `api_key='not-needed'`, silently routing every
turn to `https://api.openai.com/v1/chat/completions` and 401-ing on the
fake key.

The orchestrator's catalog enrichment closes most of this gap:

- Worker dispatch: `orchestrator/main.py:1056-1070` injects the system
  `default_chat_model` pin when `config_override.llm.model` is unset.
- Persistent thread create: `orchestrator/main.py:9586-9608` does the
  same for cockpit-initiated sessions.
- Runtime config swap: the WebSocket `config.update` path now PATCHes
  the orchestrator first and uses the enriched override (with resolved
  `base_url` + `api_key`) before rebuilding the LLM.

But the YAML default is still load-bearing in three escape hatches:

1. **Standalone agent** (`agent.py` without an orchestrator) reads the
   YAML directly — `config/defaults.yaml`, `persistent_defaults.yaml`,
   `interactive.yaml`.
2. **`agent_create_thread`** (`orchestrator/main.py:9119`) — the agent
   self-registering its own thread on startup. No user context, no
   default-pin injection.
3. **The auxiliary LLM** (`auxiliary.model` in `defaults.yaml:230` and
   `persistent_defaults.yaml:159`). The catalog only resolves the chat
   slot; auxiliary falls through to whatever the YAML says.

If the readiness gate's chat-model pin isn't set (or the system has no
catalog row for `RedHatAI/...`), boot succeeds but the first turn 401s.

## Where the defaults live

| File | Line | Field |
|---|---|---|
| `config/defaults.yaml` | 8 | `llm.model` |
| `config/defaults.yaml` | 230 | `auxiliary.model` |
| `config/persistent_defaults.yaml` | 15 | `llm.model` |
| `config/persistent_defaults.yaml` | 159 | `auxiliary.model` |
| `config/interactive.yaml` | 8 | `llm.model` |

## What the fix should look like

Two shapes worth considering:

1. **Sentinel + lazy resolution**. Replace the model strings with `null`
   (or a sentinel like `__resolve_from_db__`). Make `create_llm` /
   `_create_openai_llm` raise a clear `ModelNotConfigured` error when
   it's invoked with no model. That forces every callsite to inject a
   model explicitly — the orchestrator does this for jobs and threads;
   the standalone agent path would need to read `default_chat_model`
   from `system_settings` at startup. Trade-off: standalone-agent dev
   loop now requires a populated DB.
2. **Keep the strings, document the dependency**. Mark them as
   placeholders that only work when the matching catalog row exists
   (helm seed creates one for `RedHatAI/...`). Add a startup probe that
   warns if the configured model has no catalog row. Trade-off: the
   silent-route-to-OpenAI failure mode stays possible if the operator
   removes the seed row.

Option 1 is the long-term fix — the auth-resolution refactor
(`docs/llm_routing_issues.md`) was meant to make this structurally
impossible, and the YAML defaults are the last unstructured hole.

## Auxiliary slot

`auxiliary.model` doesn't have a catalog-pin equivalent today. The
auxiliary LLM is used for memory extraction, knowledge curation, and
title generation — failures there are non-fatal but observable
(``WARNING - Title generation error: 401`` in the orchestrator logs of
the bug report). Either:

- Add an `auxiliary` capability to the readiness defaults panel and pin
  it the same way as `chat`, or
- Have the auxiliary fall back to the chat pin when it's unset (the
  existing chat catalog row already serves auxiliary thanks to the
  `capabilities[]` array work — just need to wire the resolution).

### Confirmed: auxiliary is never hot-swapped at session attach

Live repro on 2026-05-01 confirmed the auxiliary slot is the load-bearing
path that's still broken even after the chat-side credentials patch.

Sequence from the agent log of thread `33ddf570-…`:

1. **Boot** (`agent.py --port 8001`) — `UniversalAgent.from_config`
   reads `config/defaults.yaml`, builds both LLMs from the YAML:
   ```
   src.agent - INFO - Created single LLM for all phases:
       RedHatAI/gemma-4-31B-it-FP8-Dynamic
   src.agent - INFO - Created auxiliary LLM:
       RedHatAI/gemma-4-31B-it-FP8-Dynamic
   src.core.loader - INFO - Created OpenAI LLM:
       model=RedHatAI/..., base_url=default, ..., keys=1 key
   src.llm.key_ring - DEBUG - KeyRing[openai]: single key mode (not-need...)
   ```
2. **Attach** (`POST /session/attach`) — `_attach_session` reads the
   orchestrator-enriched `config_override.llm` and rebuilds the **chat**
   LLM via `create_llm`. Auxiliary is passed through untouched:
   ```python
   # src/api/persistent_app.py:415-418
   await _session.setup(
       llm=llm,
       auxiliary_llm=_agent._auxiliary_llm,   # <-- still the YAML default
       ...
   )
   ```
   Log confirms the swap only touched the chat slot:
   ```
   src.core.loader - INFO - Created OpenAI LLM:
       model=gemma-4-moe-strix, base_url=https://ai.h4ll.app/v1, ...
   src.api.persistent_app - INFO - Config override applied:
       model=gemma-4-moe-strix, temperature=0.3
   ```
3. **First turn completes**. `_auto_title_after_first_turn` calls
   `_generate_title(messages, _session.auxiliary_llm)`, which still
   points at the boot-time auxiliary client → request goes to
   api.openai.com with `not-needed`:
   ```
   httpx - HTTP Request: POST https://api.openai.com/v1/chat/completions
       "HTTP/1.1 401 Unauthorized"
   src.api.persistent_app - WARNING - Title generation error:
       Error code: 401 - {'error': {'message': 'Incorrect API key
       provided: not-needed. ...'}}
   ```
4. The session in the cockpit stays titled "Untitled Session" because
   the title-update path silently swallowed the 401.

The chat side still works (`POST https://ai.h4ll.app/v1/chat/completions
"HTTP/1.1 200 OK"`), so the bug is invisible to anyone looking only at
session messages. The symptoms surface in three quieter ways:

- **Title generation never succeeds** — every session shows "Untitled
  Session" until the user types one manually.
- **Memory extraction at session end** silently fails. Memories
  promised by the design (cross-session recall) never get written.
- **Knowledge curation** never runs, so promotion of facts into the
  knowledge graph is skipped.

### Why the chat-side fix doesn't carry over

The fix in `_handle_config_update` and the `create_thread` injection
both target `_session._llm` only. The agent's `auxiliary_llm` is built
once in `UniversalAgent.initialize()` from `config.auxiliary.model` and
held on `_agent._auxiliary_llm` for the lifetime of the process. Per-
session attach reuses that singleton without rebuilding it. The
orchestrator never enriches the auxiliary section because there's no
auxiliary pin in the readiness panel and no callsite that fetches one.

### Fix shapes (deferred)

1. **Pin auxiliary the same way as chat.** Add `default_auxiliary_model`
   to the readiness defaults panel (i18n + service + UI dropdown — the
   shape already exists for embedding/vision/whisper/tts). At thread
   create, resolve it the same way as `default_chat_model` and inject
   `config_override.auxiliary = {model, base_url, api_key}`. The agent's
   `_attach_session` then rebuilds `_session.auxiliary_llm` from the
   override the same way it rebuilds the chat LLM. Mirrors the chat
   path exactly. Slight cost: one more pin for the operator to set.
2. **Auto-fall-back to the chat pin when auxiliary is unset.** Capability
   array work (`capabilities[]`) already says a chat row implicitly
   serves auxiliary; just wire that into the resolver so when no
   auxiliary pin exists, `resolve_default_for_capability("auxiliary")`
   falls through to the chat row. Cheaper for the operator, but loses
   the design intent of auxiliary as a separable budget — every
   memory-extraction call would burn the chat-tier model's quota.
3. **Hybrid.** Pin auxiliary in the readiness panel but default it (in
   the UI) to the chat pin's value. Operator opts into a separate
   model only when they want one. Keeps the design intent and the
   ergonomics.

Option 3 is the recommended one. It also gives a natural place to put
the embedding pin's adjacent fix — embedding has the same singleton
problem (built at boot from `config.embedding`, never re-bound at
attach).

## Repro

1. Wipe the DB, do not pin a default chat model.
2. Boot orchestrator + persistent agent.
3. Create a session via the cockpit without overriding the model.
4. Send any message → 401 from `api.openai.com` with `not-needed`.

## Related

- `docs/llm_routing_issues.md` — original write-up of the
  silent-route-to-OpenAI failure mode.
- `docs/api_key_resolution.md` — current credential resolution chain.
- `src/core/model_registry.py:23` — comment claiming the failure mode
  is "structurally impossible". It almost is — only the YAML defaults
  remain.
