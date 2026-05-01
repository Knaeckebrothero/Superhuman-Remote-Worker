# LLM Routing & API Key Issues

Captured 2026-04-30 while debugging a persistent-session 401 against the local model orchestrator at `https://ai.h4ll.app/v1`.

## Symptom

A persistent session attached against a custom model orchestrator (`https://ai.h4ll.app/v1`, model `gemma-4-moe-strix`, key `sk-PASTE-980c6c2862deebb2aa5963b1c72379c6-admin`) failed every turn:

- Turn 1 (before hot-swap, model `RedHatAI/gemma-4-31B-it-FP8-Dynamic`):
  `httpx.ConnectError: All connection attempts failed` against `http://localhost:8080/v1`.
- Turn 2 (after hot-swap to `gemma-4-moe-strix`):
  `openai.AuthenticationError: 401 — Incorrect API key provided: your_ope************here.` — the request actually went to `https://api.openai.com/v1/chat/completions`, not to the user's orchestrator.

## Root cause

Three separate issues that compounded.

### Issue 1 — `.env` vs `.env.example` drift: stale `LLM_BASE_URL` leftover

The architecture moved off env-var-based runtime LLM routing in favor of `user_llm_endpoints` (DB-backed, configured via Admin → Providers → Endpoints, dispatched per-job/session). `.env.example` reflects this:

```env
# .env.example, lines 79–83
# Custom OpenAI-compatible endpoints (vLLM, Ollama, llama.cpp, private gateways)
# are configured via Admin → Providers → Endpoints, not via env vars. Each row
# in user_llm_endpoints carries its own base_url + api_key + model list and is
# resolved by the model registry at dispatch time. See
# docs/features/custom_llm_endpoints.md.
```

The user's `.env` was not migrated cleanly. Three concrete drifts vs `.env.example`:

| Section | `.env.example` | User's `.env` | Status |
|---------|----------------|---------------|--------|
| `LLM_BASE_URL` | Removed entirely; replaced with the "configured via Admin → Providers" note. | **Still active** at line 100: `LLM_BASE_URL=http://localhost:8080/v1`, plus commented variants on line 99 (`https://ai.h4ll.app/v1`, `http://vpn-cluster:8080/v1`). | Stale, must remove. |
| `OPENAI_API_KEY` block | Single block, lines 66–73, with placeholder uncommented (`OPENAI_API_KEY=your_openai_api_key_here`). | Two near-identical blocks (lines 64–77 + lines 84–99), both with `OPENAI_API_KEY` commented out by the user. | User has commented the key correctly, but the duplicated guidance block is leftover from pre-migration. |
| `SEED_*_API_KEY` | All commented placeholders. | Populated with real values (`SEED_OPENAI_API_KEY`, `SEED_ANTHROPIC_API_KEY`, etc.). | Correct — this is the new seeding path. |

The active `LLM_BASE_URL=http://localhost:8080/v1` is what produced Turn 1's `ConnectError`: the registry inherits it onto `Local`-group built-ins (`src/core/model_registry.py:131`), so `RedHatAI/gemma-4-31B-it-FP8-Dynamic` got pointed at a port nothing's listening on. Removing the line — matching `.env.example` — fixes Turn 1.

### Issue 1b — `.env.example` itself ships an active placeholder for `OPENAI_API_KEY`

Even after removing the env-var routing, `.env.example` line 73 still has:

```env
OPENAI_API_KEY=your_openai_api_key_here
```

uncommented. Anyone copying `.env.example → .env` and forgetting to either delete the line or fill in a real key will end up with `os.environ["OPENAI_API_KEY"] == "your_openai_api_key_here"`, which the agent then hands to OpenAI verbatim and gets 401 against. The line should either be removed entirely (the new architecture doesn't need it for runtime; `SEED_OPENAI_API_KEY` is the only `OPENAI_*` env the user should set) or commented out by default.

This is also what produced the `your_ope...` substring in the Turn 2 401: the placeholder reached the process environment somehow (whether from a still-uncommented copy at the time the agent was started, from a parent-shell export, or from `.env.example` getting read — the user has since commented out their `.env` copy). The exact source matters less than the fact that the example file ships a foot-gun.

### Issue 2 — Hot-swapping to a model not in `config/models.yaml` silently drops `base_url`

This one is independent of `.env` and is a real code bug.

The cockpit's model picker (`cockpit/src/app/views/agent-settings/model-group.component.ts:328-343`) sends only `{ llm: { model: <id> } }` in the `config.update` payload — no `base_url`, no `api_key`, no provider hint.

When `_handle_config_update` (`src/api/persistent_app.py:1429`) deep-merges that fragment and calls `create_llm`, the factory in `src/core/loader.py:1765-1769` resolves `base_url` like this:

```python
if config.base_url:
    base_url = config.base_url
else:
    meta = resolve_builtin(config.model)
    base_url = meta.base_url if meta is not None else None
```

`gemma-4-moe-strix` is not in `config/models.yaml`, so `resolve_builtin` returns `None`. `base_url` falls to `None`, and `ChatOpenAI` defaults to `https://api.openai.com/v1`. Combined with whatever stray `OPENAI_API_KEY` was in env (Issue 1b), the OpenAI SDK then attempts authentication against real OpenAI → 401.

Confirmed by the contrast in the logs:

| Phase | Model | Logged `base_url` |
|-------|-------|-------------------|
| Startup | `RedHatAI/gemma-4-31B-it-FP8-Dynamic` (in catalog, `Local` group) | `http://localhost:8080/v1` (inherited from `LLM_BASE_URL` via `model_registry.py:131`) |
| After hot-swap | `gemma-4-moe-strix` (not in catalog) | `default` (i.e. `None` → falls through to `api.openai.com`) |

The right fix is for the cockpit to either:
- Carry the `endpoint_id` of the `user_llm_endpoints` row that contributed the model (so the agent re-resolves on swap), or
- Have `_handle_config_update` re-run model registry resolution and refuse to swap if the target model is unresolvable.

### Issue 3 (latent) — `LLM_BASE_URL` only inherits onto `Local`-group built-ins

`src/core/model_registry.py:131` only mutates `meta.base_url` for entries whose YAML provider is `local`. So even adding `gemma-4-moe-strix` to `config/models.yaml` under a non-`Local` group would not pick up `LLM_BASE_URL` automatically. This isn't a bug per se — the deprecation comment in the registry calls it out — but it's a sharp edge that interacts with Issue 2.

## Fix paths

### For the user (immediate unblock)

1. **Clean up `.env` to match `.env.example`.** Specifically:
   - Delete line 100 (`LLM_BASE_URL=http://localhost:8080/v1`) and the commented variants on line 99. The new architecture does not use this env var at runtime.
   - Optionally collapse the two duplicated `OPENAI_API_KEY` guidance blocks (lines 64–77 + 84–99) into one. Cosmetic; doesn't affect behavior since the user has commented out the active assignments.
2. **Register the custom orchestrator via Cockpit.** Settings → LLM Endpoints → Add: base_url `https://ai.h4ll.app/v1`, key `sk-PASTE-980c6c2862deebb2aa5963b1c72379c6-admin`, click Discover. `gemma-4-moe-strix` will appear in the model picker with proper routing. This is the architecturally-blessed path; backed by `POST /api/settings/llm-endpoints` (`orchestrator/main.py:11494`) and `/discover` (line 11584).
3. **Restart the agent process** so it re-reads the cleaned `.env`.

### For the codebase (real fixes)

1. **Fix `.env.example` line 73.** Either remove the `OPENAI_API_KEY=your_openai_api_key_here` line entirely or comment it out. Document that the only `OPENAI_*` env var users should set is `SEED_OPENAI_API_KEY` (one-time, on `python init.py`).
2. **Detect duplicate keys / placeholder values at startup.** A one-line warning when `OPENAI_API_KEY` looks like a template (`*_here`, `your_*`, `<placeholder>`, etc.) would short-circuit hours of debugging. The agent's `KeyRing` constructor is the natural place.
3. **Refuse to hot-swap to an unresolvable model.** When `_handle_config_update` rebuilds the LLM and `resolve_builtin(model) is None` *and* there is no `base_url` in the merged config *and* there is no matching `user_llm_endpoints` row, fail loudly via `error` WebSocket frame instead of silently defaulting to `api.openai.com`. The current behavior turns a config bug into a confusing 401.
4. **Have the cockpit carry endpoint context on model swap.** When the chosen model came from a `user_llm_endpoints` row, send `{ llm: { model, endpoint_id } }` (or equivalent) so the agent re-resolves base_url + api_key. Today the dispatcher handles this at session *creation* time but not for in-session hot-swaps.
5. **Drop or relabel `LLM_BASE_URL`.** The registry comment already calls it deprecated and `.env.example` has removed it. Either remove the inheritance path entirely (forcing everyone onto `user_llm_endpoints`) or rename it to make the `Local`-group-only scope obvious.
6. **Sanity-check the user-supplied API key.** `sk-PASTE-980c6c2862deebb2aa5963b1c72379c6-admin` contains the literal substring `PASTE`. If the user's gateway generates keys with descriptive infixes, fine — but worth confirming it isn't a half-filled template.

## Files referenced

- `.env` (root) — stale `LLM_BASE_URL`, duplicated OPENAI guidance block.
- `.env.example` (root) — line 73 ships an active placeholder `OPENAI_API_KEY`.
- `config/models.yaml` — catalog; `gemma-4-moe-strix` missing (intentionally — should come from `user_llm_endpoints`, not the static catalog).
- `src/core/loader.py:1685-1840` — `create_llm` / `_create_openai_llm`, base_url resolution.
- `src/core/model_registry.py:118-180` — `LLM_BASE_URL` inheritance for `Local` group.
- `src/api/persistent_app.py:1429-1510` — `_handle_config_update` (hot-swap entry point).
- `cockpit/src/app/views/agent-settings/model-group.component.ts:328-343` — `getOverrides()`, what the cockpit sends on model change.
- `orchestrator/main.py:11486-11600` — `/api/settings/llm-endpoints` CRUD + test/discover.
