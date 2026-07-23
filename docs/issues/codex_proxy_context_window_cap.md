# Codex-routed models must cap their working context window at the Codex surface limit (~400K), not the model's API window

**Status:** **BUILT 2026-07-10, uncommitted** — code + helm + unit tests + DB re-seed done; needs an agent image rebuild to deploy. Diagnosed from session `4b82e6db` (model `gpt-5.6-sol` via `srw-codex-proxy`).
**Severity:** high — reproducible session dead-end. Every turn returns "⚠ The model returned an empty response" once accumulated context crosses the Codex ceiling, and the user's re-sends fail identically.
**Component:** `src/core/model_registry.py` (`_cap_context_window`), `helm/templates/configmap.yaml` + `helm/values.yaml` (`CODEX_CONTEXT_WINDOW_CAP` / `agent.codexContextWindowCap`), consumed downstream by `src/core/loader.py` (`_apply_settings_matrix`, derives the compaction threshold).
**Related:** `docs/issues/web_search_full_page_content_bloats_session_context.md` (the trigger — deferred), `project_session_web_search_context_bloat` (memory), `project_context_window_derived_limits`, `docs/features/reasoning_aware_max_output_tokens.md`.

## TL;DR

`gpt-5.6-sol` (and every gpt-5.x routed through the system codex proxy) was configured with the model's **true API context window** — the `gpt-5.6` matrix family declares `model_max_context_tokens: 1_000_000`, and the catalog rows had `context_window = NULL` so they inherited it. But the codex proxy (CLIProxyAPI, ChatGPT/Codex **OAuth** backend) does **not** honor that window — it caps context at the Codex product surface limit (~400K) and rejects larger inputs with `context_too_large`. Because the app believed the window was ~1M, its compaction threshold sat at ~840K, so it never trimmed a context that was already over the real ~400K ceiling. Result: the request `context_too_large`s, the codex Responses **streaming** path returns an empty 200 (finish_reason stripped → the generic "empty response" placeholder, not the length-specific one), and the non-streaming retry returns a 400. Every turn re-sends the whole oversized history → permanent wedge.

## Measured ceiling (live probe of `srw-codex-proxy`, 2026-07-10)

Direct `POST http://srw-codex-proxy:8317/v1/responses` (dummy bearer accepted; `truncation:disabled`), payloads tokenized with `tiktoken` `o200k_base`:

| input tokens | result |
|---|---|
| 240,744 | 200 OK |
| 288,896 | 200 OK |
| 356,590 | 200 OK |
| 380,364 | **400 `context_too_large`** |
| 385,174 | 400 |
| 404,440 | 400 |

Error body: `{"code":"context_too_large","message":"Your input exceeds the context window of this model","type":"invalid_request_error"}`. So the effective input ceiling is ~357–380K → a ~400K total window. Note **272K is NOT a cap** — it's only OpenAI's long-context pricing tier (>272K bills 2× input / 1.5× output); requests sail well past it. The wedged session's request was **415,561** tokens — just over the wall.

This is a documented, deliberate Codex-product limit, not our proxy and not the model: OpenAI's own [codex#19464](https://github.com/openai/codex/issues/19464) / [codex#9857](https://github.com/openai/codex/issues/9857) track lifting Codex to 1M; [codex#1999](https://github.com/openai/codex/discussions/1999) confirms plan tier (Plus/Pro/$200) doesn't change the per-session window. The full 1.05M is reachable only via the **paid API** (api.openai.com + key), not this OAuth proxy.

## Fix (as built)

**Provider-keyed clamp, not family-keyed.** `_cap_context_window(provider, context_window)` in `model_registry.py` runs inside the three `ModelMeta` builder sites (`_endpoint_row_to_meta`, both branches of `_catalog_row_to_meta`), keyed on the resolved `provider == "codex"` (the transport identity from `_endpoint_factory_provider`). Semantics: NULL or too-large window → the cap; a deliberately-smaller admin `context_window` is respected (`min`). Keying on transport (not family) means the *same* model over the real API keeps its full 1M while over the codex proxy it's capped — no need to fork a `gpt-5.6-codex` family or touch the matrix.

The capped value flows through the existing dispatch injection (`orchestrator/main.py:1505/1606/3782`, each `if meta.context_window:` → `llm.model_max_context_tokens`) and becomes the authoritative `base` in `loader.py:789`, from which the 80% compaction threshold derives. Cap = 400,000 → threshold ~320K input, safely under the measured ~357K wall, with the output backstop reserving the rest.

**Env override:** `CODEX_CONTEXT_WINDOW_CAP` (helm `agent.codexContextWindowCap`, default `400000`). Set **0** to disable the clamp the day OpenAI ships 1M-for-Codex — no code change, no image rebuild, just `helm upgrade` (Stakater Reloader bounces the agents). A malformed value falls back to the default rather than 0, so a typo can't silently un-cap.

**DB hygiene:** the `gpt-5.6-sol` / `gpt-5.6-terra` catalog rows stored a stale `family = gpt-5` (admin-created before the `gpt-5.6` family shipped; `family_of` drives the matrix so it was cosmetic, but a trap for anything reading `ModelMeta.family`). Re-seeded to `gpt-5.6` (`gpt-5.5` correctly stays `gpt-5`). Note the seeder already does `family or family_of(model_id)`, so new rows won't drift; `ON CONFLICT DO NOTHING` is why the old rows needed a manual `UPDATE`.

## Operational caveats

- **New sessions only.** A persistent session freezes its resolved config (incl. `model_max_context_tokens`) at session start on a long-lived `srw-agent-s-*` pod, and agents read env at pod start. So neither the code rule nor the env change nor a DB edit retro-heals an already-wedged session — it needs a **fresh session** (or an agent-pod restart). The fix prevents recurrence.
- **The trigger is separate and still open.** The context only ballooned because `web_search(include_raw_content=True)` inlines full page bodies (see the web_search issue doc). Even inside a correct 400K window, one 88K-token search result is reckless. Cap the window *and* fix the tool.

## Verification done

`pytest tests/test_model_registry.py` (57 passed incl. 8 new `TestCodexContextWindowCap`: NULL→cap, oversized→cap, smaller-respected, non-codex-untouched, catalog path, env override, env=0 disables, malformed env→default). `ruff check` + `ruff format` clean. `tests/test_dispatch_phase_credentials.py` window tests green. Live probe above. **Remaining:** image rebuild + `helm upgrade`, then confirm a new gpt-5.6-sol session compacts before ~357K and no longer `context_too_large`s.

---

## Follow-up: `NULL -> cap` inflated sub-cap models (regression, fixed 2026-07-23)

**Status:** fixed on `develop` (code + data), unpushed. Diagnosed from job `9a99f433-85fe-4e50-8bb0-d7f82c372c1e` (Loop iter 1 · SCHOLAR, model `gpt-5.3-codex-spark`).
**Severity:** high — deterministic job kill, same `context_too_large` signature as the original bug but the opposite cause.

The clamp above was written for models whose true window is **above** the cap (gpt-5.6: 1M → 400K), where `NULL -> cap` *narrows*. `gpt-5.3-codex-spark` shipped 2026-07-15 as the first codex-routed model whose true window is **below** the cap — a distilled **128K** model. Its catalog row had `context_window = NULL`, so the same rule handed back **400,000** and *widened* the window 3.1× past reality.

That value is not advisory. Dispatch injects it (`orchestrator/main.py:1712`) into `llm.model_max_context_tokens`, where `loader._apply_settings_matrix:804` treats any truthy value as authoritative and **never falls back** to the family matrix — so `codex-spark`'s correct `model_max_context_tokens: 128000` (`config/model_config_matrix.yaml:429`) was masked. The 80% threshold landed at **320K** against a **128K** wall.

Observed on the failed job: prompt tokens grew monotonically 42,477 (13:02) → **123,796** (13:08:41, last 200 OK); the next turn crossed the wall and returned `400 context_too_large` six identical times, tripping the deterministic-rejection classifier. Agent boot log confirms the whole chain in one line:

```
Created Codex LLM: model=gpt-5.3-codex-spark, ..., max_context_tokens=400000
```

### Fix

**Code — the cap is a ceiling, never a floor.** `_cap_context_window(provider, context_window, model_id)` now resolves the effective window as `context_window or _family_context_window(model_id)` and `min`s *that* with the cap; the bare cap stands in only when neither is known. `_family_context_window` reads the matrix via a deferred `from src.core.loader import resolve_model_settings` (loader imports `family_of` from model_registry, so the dependency can only run in this direction at call time) and degrades to None on any failure, preserving the old behaviour. Registry and matrix now agree instead of the registry overriding it.

Behaviour preserved for every prior case — gpt-5.6 NULL still yields 400K, because the family declares 1M and `min(1M, 400K) = 400K`. Only sub-cap families change. An unrecognised model now resolves via the matrix `default` family (128000) rather than the cap, which is both consistent with what the loader would derive alone and the safe direction.

**Data — dev catalog re-seed.** The row was created without a window (`seeded_from` NULL; the 07-10 re-seed predates the model). `ON CONFLICT DO NOTHING` means only an UPDATE lands it:

```sql
UPDATE models SET context_window = 128000 WHERE model_id = 'gpt-5.3-codex-spark';
```

This is the **immediate** remedy: it fixes the deployed image with no rebuild, because `min(128000, 400000) = 128000` already holds under the old code. The code fix prevents the next sub-cap codex model from repeating it. `model_registry` has no caching, so the UPDATE takes effect on the next dispatch with no orchestrator restart.

### Verification done

`pytest tests/test_model_registry.py` (61 passed, incl. 4 new: sub-cap family via endpoint row + catalog row, sub-cap still clamped by a smaller env cap, unknown family → matrix default). Related suites green: `test_dispatch_phase_credentials`, `test_settings_matrix`, `test_config_resolver`, `test_loader_routing`, `test_admin_models_api`, `test_phase_model_budget`, `test_context_overflow`, `test_context_safety`, `test_context_methods`, `test_model_swap_hardening`, `test_prompt_matrix`, `test_tool_vocabularies` (359 passed). `ruff check` + `ruff format` clean.

Live-verified against the **deployed** dev orchestrator image (`srw-orchestrator-74c896f7f`), which proves the matrix is readable there — the load-bearing assumption of the deferred import:

```
gpt-5.3-codex-spark | family= codex-spark | window= 128000
gpt-5.6-sol         | family= gpt-5.6     | window= 1000000
some-unheard-of-model | family= default   | window= 128000
```

and post-UPDATE resolution through the deployed `_catalog_row_to_meta`: `provider = codex | context_window = 128000` → threshold **102,400**, comfortably under the 128K wall.

**Blast radius:** one job, cluster-wide — `SELECT status, count(*) FROM jobs WHERE error_message LIKE '%context_too_large%'` returned exactly `failed | 1`. gpt-5.6-* were never exposed (explicit 400000).

### Contributing factor (not causal, still open)

From iter ~300 the scholar was in a repetitive git-inspection loop — `git_tags, git_log, git_diff, git_show, list_files, read_file×3` on nearly every turn — driving 42K → 124K in six minutes. A correct 128K window would have compacted at ~102K and the job would have survived, so this is not the root cause, but it is why the wall was reached inside a single iteration.
