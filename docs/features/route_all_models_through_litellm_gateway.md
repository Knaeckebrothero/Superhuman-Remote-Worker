---
tags:
  - feature
  - llm
  - gateway
  - litellm
  - metering
  - infrastructure
aliases:
  - route all models through litellm
  - all-provider gateway migration
  - litellm gateway migration
related:
  - "[[system_provider_models_bypass_gateway_unmetered]]"
  - "[[litellm_gateway_drops_gpt_codex_reasoning_capture]]"
  - "[[usage_monitoring_and_rate_limiting]]"
  - "[[reasoning_aware_max_output_tokens]]"
---

# Route all models/providers through the LiteLLM gateway — implementation roadmap

**Status:** **In progress — P0–P3 done + committed + LIVE on dev; `*` wildcard added (uncommitted); P4–P5 remaining.** P0–P3 are deployed on dev (`sha-1163fd8`) and metering is working in production (codex GPT-5.5 = $100.79, gpt-5.4-mini = $15.37 tracked in `usage_events`). The openrouter canary is committed + live on dev (`1163fd8d`). Research-backed 2026-06-28 (6 subagents: 4 codebase traces + 2 web); empirically de-risked first (see `docs/issues/system_provider_models_bypass_gateway_unmetered.md` §§ "Empirical addendum" + "Codex-proxy follow-up" — a real LiteLLM proxy preserves reasoning + meters for every model family we run, including the Responses-API-only codex-proxy). This doc is both the implementation plan and the running status to make the gateway the **single path for all LLM traffic**.

**One-line goal:** every model (minimax, gemini, gemma, gpt-5.x/codex — and any future provider) routes through `srw-litellm:4000`, so we get unified **metering + rate-limiting + quota + reasoning capture + key management** in one chokepoint, instead of the current half-in/half-bypassed state.

**Progress — 2026-06-28 (commit hashes are post-rebase `develop`):**
- ✅ **P0 — de-risk** (image pinned `litellm-database:v1.90.0`; gateway-down→direct dispatch fallback; `get_spend_logs` `start_date` bound) — commit `a5bae034`.
- ✅ **P1 — register every model** (`build_desired_models` now syncs system rows + codex `use_responses_api`; per-provider `litellm_params` + `params_json["litellm"]` overrides; `_rev_for_model` re-register fix). gemini/minimax/codex each callable *through* the gateway — commit `3e7b67bf`.
- ✅ **P2 — flip routing** behind a **per-provider canary** (`LITELLM_GATEWAY_ROUTED_PROVIDERS`) with a registered-model fail-loud guard — commit `3bb72235`. **`*` wildcard added 2026-06-28 (uncommitted):** `routedProviders: ["*"]` = route *every* system/codex provider (new providers route the moment they register; guards intact). Live-verified on k3d (value `*` → minimax routed via the wildcard branch).
- ✅ **P3 — metering completeness** (commit `27d026ac`) — the materializer stamps LiteLLM's authoritative per-request `spend` onto a dedicated `unit='request'` cost dimension (token rows stay cost-free; SUM == spend, no double-count), closing **Gap-B §4.4**.
- 🚀 **Deployed + live on dev** (`sha-1163fd8`): metering working in production — codex GPT-5.5 **$100.79**, gpt-5.4-mini **$15.37**, gemma $0, all tracked in `usage_events`. Canary committed at `openrouter` (`1163fd8d`).
- ⏳ **P4 — agent-factory collapse** (scoped: **`docs/features/agent_llm_factory_collapse.md`**) · **P5 — HA**.

**Current routing reality (dev, 2026-06-28):** endpoints (gemma/qwen/codex-GPT) → gateway always; minimax (openrouter canaried) → gateway; **gemini (google) still bypasses** (not canaried → direct, unmetered). Flip dev to `["*"]` to route gemini too — gated by the P5/HA decision (route-all raises the SPOF stakes). `mistral`/`anthropic`/`groq`/`openai-direct` are **not in SRW's catalog** (GPT comes via the codex-proxy *endpoint*, not OpenAI direct). Full per-phase build detail in the **Status** callouts under each phase in §7.

---

## 1. Why now

Today the gateway meters only *some* models; the paid external ones (minimax/OpenRouter, gemini/Google) **bypass it entirely** and codex was **deliberately bypassed** to keep its reasoning. Both gaps are documented in `system_provider_models_bypass_gateway_unmetered.md`. We've now shown the bypasses are unnecessary: LiteLLM carries reasoning + cost for all of them. Routing everything through closes the metering gap, fixes the inverted coverage (we currently meter the *free* self-hosted lane and miss the *paid* lane), and removes per-provider drift in the agent.

## 2. Current state (what routes where)

> **As-built note (post-P2).** The table below is the **original pre-implementation baseline**, kept for context — its `main.py` line numbers predate the P0–P2 edits and have shifted. What changed since: **P1** registers system + codex rows in the gateway (so "In gateway sync?" is now **Yes** for every enabled model), and **P2** adds a default-off per-provider canary (`LITELLM_GATEWAY_ROUTED_PROVIDERS`) that routes system + codex models *through* the gateway when their provider is opted in. With an empty allowlist (the default) the routing still matches this table. See the §7 **Status** callouts for the as-built behaviour.

The gateway-vs-direct decision lives in **two functions** in `orchestrator/main.py`, with duplicated gate logic, and both the legacy (`config_override`) and blob (`resolved_config`) delivery paths funnel through them (so one edit covers both — `config_resolver.py:202` `inject_blob_credentials` calls the same injector):

| Model shape (`ModelMeta`) | Examples | Routing today | In gateway sync? |
|---|---|---|---|
| `provider_kind='endpoint'` (`endpoint_id` set) | gemma "Local Router" (vLLM) | **Through gateway** (`main.py:1551` gate `and meta.endpoint_id`) | Yes |
| `provider_kind='endpoint'` + codex | gpt-5.x via codex-proxy | **Force-bypassed** (`main.py:1567` `_gw = ... if meta.provider != "codex" else None`) | Registered but dead |
| `provider_kind='system'` (`endpoint_id=None`, `api_key_ref`=slug) | minimax/openrouter, gemini/google | **Direct, never gateway** (`elif resolved_keys` `main.py:1590`) | **No** (`litellm_gateway.py:322` syncs `provider_kind="endpoint"` only) |

Key structural facts found by the trace:
- Only the **`models` catalog** is wired (`register_catalog_lookup` at `main.py:5214`); the `custom`/`system` endpoint-lookup branches in `model_registry.py` are **dormant code** (`register_custom_lookup`/`register_system_lookup` never called).
- `models.params_json` (JSONB) is **fully plumbed but unused for routing** — a ready-made, admin-editable, zero-migration carrier for per-model LiteLLM params.
- The gateway is **DB-backed** (`store_model_in_db: true`, `config.yaml` ships `model_list: []`); the orchestrator is the source of truth, syncing via `/model/new` every 60s (`litellm_sync_loop`, `litellm_gateway.py:984`). Keys live encrypted in the app DB (no Secret copy), so static config is a non-starter — the runtime sync stays.

## 3. Target architecture

- **Every model is a gateway deployment.** `build_desired_models` registers system + endpoint + codex rows with the right `litellm_params` (per-provider route prefix + decrypted key; codex gets the Responses-API flag).
- **Dispatch always points the agent at `{LITELLM_BASE_URL}/v1` with a virtual key**; the agent's OpenAI-compatible factory (`_create_openai_llm`) speaks `/v1/chat/completions` and captures reasoning as `reasoning_content` (the field `reasoning_chat.py` already reads). Codex uses the gateway's Responses bridge.
- **A direct lane remains as fallback** (gateway-down) — see §6.
- **Metering** flows from `/spend/logs` → `materialize_llm_usage` → `usage_events`, now covering 100% of traffic.

## 4. Change-set by component

### 4.1 Gateway registration (`orchestrator/services/litellm_gateway.py`, `build_desired_models:314-375`)
- **Widen the query** from `list_models(provider_kind="endpoint")` (`:322`) to also include `"system"`.
- **Per-provider `litellm_params`** (mirrors `_factory_provider`’s slug→route map):
  - minimax: `{"model": "openrouter/<id>", "api_key": <system openrouter key>}` (id already carries `openrouter/` prefix) — optional OpenRouter provider-pin via `extra_body`/`provider.order`.
  - gemini: `{"model": "gemini/<id>", "api_key": <system google key>}` (Google AI Studio; the only non-OpenAI-wire provider — LiteLLM translates).
  - codex: `{"model": "openai/<id>", "api_base": <codex-proxy>/v1, "api_key": <key>, "use_responses_api": true}` (**see §4.5 on the exact knob**).
  - gemma: unchanged (`openai/<id>` + `api_base` + key).
  - System keys decrypted via `get_system_api_key(provider_ref)` (`postgres.py:5386`).
- **Per-model overrides** ride `models.params_json` — shallow-merge `params_json` (or a `params_json["litellm"]` sub-key) on top of the structural params. `drop_params: true` means stray keys won't error. Zero schema change.
- **Drift/rev fix:** `_rev_for_endpoint` keys `srw_rev` off `endpoint.updated_at` only (`:76`); system rows have no endpoint, so fold `models.updated_at` + the `system_api_keys` row `updated_at` into the rev so `params_json` edits and key rotations actually re-register.

### 4.2 Dispatch routing (`orchestrator/main.py`, both `_inject_dispatch_credentials:1551-1609` and `_inject_model_credentials:3597-3647`)
- **Broaden the gate** `... and meta.endpoint_id` → `... and (meta.endpoint_id or meta.api_key_ref)` so system rows enter the gateway block.
- For system rows when the gateway is on: set `base_url=_gw[0]`, `api_key=_gw[1]`, and **force `provider="openai"`** (use `_create_openai_llm` → reasoning captured as `reasoning_content`). Ensure the gateway branch wins over the `elif resolved_keys` block so the upstream key never reaches the gateway (it 401s on non-`sk-` keys — `codex_session_gateway_baseurl_401.md`).
- **Remove the codex bypass** (`:1567` + `:3608`): `_gw = _gw_scoped` unconditionally; keep `provider=codex` is **not** needed if codex is fronted by the gateway with `use_responses_api` — the agent can use the openai factory + the `/chat/completions` bridge (simpler). Rework the stale-gateway-base_url cleanup (`:3618-3636`), which assumed codex bypasses.
- **Fail-loud guard (new):** if dispatch forces `base_url=gateway` for a model the sync hasn't registered, the agent gets a `400 model not found` — *worse* than today's silent-but-working direct path, and `unrouted_model_slots` won't catch it (base_url is present). Gate the gateway branch on a known-registerable allowlist (`openrouter`, `google`, codex) and/or check against the gateway's managed-model set; ensure the sync ran before first dispatch.

### 4.3 Agent-side simplification (`src/core/loader.py`) — optional follow-on
> **This is Phase 4 — full scope in `docs/features/agent_llm_factory_collapse.md`.** The notes below are the original change-set sketch; the dedicated doc has the sharpened plan, sequencing, and the auxiliary/bypass-lane analysis.

- The agent can **collapse to a single `_create_openai_llm`** (all factories already build the same `ReasoningChatOpenAI` wrapper) and **drop** `langchain_google_genai`, `langchain_groq`, `langchain_anthropic`, and the client-side Responses-API codex path.
- **Keep** (provider-agnostic, load-bearing): the `ReasoningChatOpenAI` wrapper — its SSE reasoning tap (LangChain drops `reasoning_content` from CC deltas), Layer-0 context-overflow guard, granular timeout, KeyRing; and the request-param policy (`max_tokens`/`_resolve_max_output_tokens`, `reasoning_effort`, `top_k`/`extra_body`).
- **Two traps that need a new home before deleting the Google factory:** the **gemini-3 temperature-0 loop floor** (`_create_google_llm:3070`, forces `temperature≥1.0`; no gateway equivalent → relocate to `model_config_matrix.yaml` + apply pre-send) and the **`parallel_tool_calls` suppression** (`supports_parallel_tool_calls:2675`, keyed on `provider=="google"` — once Google routes as `provider="openai"` this stops firing → re-key on the model, or rely on LiteLLM `drop_params`). Also relocate `include_thoughts`.
- **`reasoning_chat.py`:** keep `reasoning_content` primary + the SSE tap (mandatory); demote the `.reasoning`/`.reasoning_details` shapes to a documented safety net (for the bypass lane); the Responses-API content-block extractor + codex raw-dump diagnostics can be removed once the client-side Responses path is retired.

### 4.4 Metering / cost — **the subtle one (corrects an earlier assumption)**
The issue doc says "Gap B fixes itself for the paid lane." That is **true only for LiteLLM's own view** (`x-litellm-response-cost`, `/spend/logs.spend`). It is **false for SRW's `usage_events.cost_usd`**: `materialize_llm_usage` (`litellm_gateway.py:923`) **deliberately ignores LiteLLM's `spend`** ("dollar-only, 0 for unpriced homelab") and re-prices token quantities from the app-DB **`usage_rates`** table — which **ships empty** (`migrations/app/0033`). So routing through the gateway alone leaves `cost_usd` NULL. To actually populate it, either:
- **(preferred)** seed `usage_rates` rows for the paid models (minimax/OpenRouter, gemini/Google) — the materializer snapshots them at write time (history-immutable); leave free gemma NULL (its cost is `category='compute'`), or
- change `materialize_llm_usage` to consume LiteLLM's computed `response_cost`/`spend` when present, falling back to `usage_rates`.

> **As-built (P3, 2026-06-28).** Chose the **second** option — but cleaner than "fall back to usage_rates per token". The reasoning: LiteLLM's `spend` is a per-*request* total (tiered/cached pricing already folded in) that does **not** decompose onto a per-token `usage_rates` rate, and a per-component split is provider-specific (only OpenRouter returns `usage_object.cost_details`; gemini/codex don't). So the materializer emits the cost as its **own dimension**: alongside the prompt-token / completion-token quantity rows it appends, for `spend > 0` only, a `unit='request'`, `quantity=1`, `cost_usd=rate_usd=spend` row. Token rows stay cost-free (honest — there's no per-token rate), `SUM(cost_usd)` per request equals `spend` with no double-count, and pre-setting `cost_usd` makes `record_events` skip the (now compute-only) `usage_rates` lookup. Homelab models report `spend=0` → no cost row → cost stays absent (same inert posture). **`usage_rates` is unchanged and still prices `category='compute'`** (vcpu/gib-hour) when seeded. Code: `litellm_gateway.py` `_spend_amount` + the cost-row branch in `materialize_llm_usage`. Consumer-safe: `/api/usage` (`query_usage`) and the breakdown fold (`_fold_breakdown`) sum `cost_usd` generically, so the new unit just adds a clean `(llm, request)` line and the correct headline total.

### 4.5 The codex Responses knob — confirm before building
We verified **`use_responses_api: true`** in `litellm_params` works end-to-end on v1.83.7/v1.89.3 (HTTP `/v1/responses` + the `/chat/completions` bridge both returned reasoning + cost). The web research flags the *documented* knobs as `mode: responses` (model_info), the `openai/responses/<model>` prefix, or global `route_all_chat_openai_to_responses` — i.e. `use_responses_api` may be an alias/older form. **Action:** on the pinned version, confirm which knob is canonical (both may work); use whichever the docs bless and keep the empirical test as the gate.

## 5. Reliability & HA (the biggest new risk)
Routing 100% through the gateway makes it a **hard single point of failure for all inference**, and today it is **single-replica, single-worker, no Redis**:
- **No direct fallback exists.** When the gateway is unreachable, `_gateway_routing_target_scoped` still returns the **gateway** base_url (`main.py:1277`), so the agent is handed a dead gateway URL and the job fails at inference — there is no "gateway-down → hit provider directly" path. **Add one** (dispatch-time fallback to the upstream when the gateway is unhealthy), or accept that a gateway blip = total outage.
- **Multi-replica requires Redis/Valkey first.** Rate-limit/budget/cooldown counters are **in-memory per replica** (verified in `litellm_gateway.py`), so a second replica forks every counter → effective limits ≈ per-replica × replicas, budgets undercount. Redis is the prerequisite that makes HA *correct*, not just available. Configure via `router_settings.redis_host/port/password` (discrete params, ~80 RPS faster than `redis_url`); Valkey is a drop-in.
- Probes: liveness→`/health/liveliness`, readiness→`/health/readiness` (never `/health` — it pings every model). `allow_requests_on_db_unavailable: true` (VPC-only) + `use_shared_health_check: true`.
- DB-backed config propagates **poll-based (~30s), eventual-consistent** across replicas — not instant; for immediate rollback use `config.yaml` + pod roll.

## 6. Operational hardening (do early)
- **Pin an exact image** — the chart runs the **`-database`** variant (pre-baked Prisma client), so pin `ghcr.io/berriai/litellm-database:v1.90.0` (digest `sha256:b4b2eb22cfe218d2c46c0cfe442db0dd4262fdf4025f96b0c74abda3d379ef65`). Stay **≥ v1.86.0** to dodge the v3 rate-limiter regression that 400s every key with rpm/tpm limits. `main-stable` stops publishing 2026-06-30. ✅ done (`helm/values.yaml`).
- **Set an explicit `LITELLM_SALT_KEY`** separate from the master key, **before** routing more models or rotating anything: it encrypts stored provider creds and **falls back to the master key if unset** → rotating the master would make all stored creds undecryptable. Back it up.
- **Spend capture:** make a `CustomLogger` push callback the primary ledger feed (survives `disable_spend_logs`, no poll latency), keep `/spend/logs` polling as reconciliation. Today's `get_spend_logs` has **no time-filter/pagination** (`litellm_gateway.py:306`) — add `start_date` before full volume or high-rate windows silently drop rows. `api_key` in logs is a **hash** (join on it). Raise `proxy_batch_write_at` to 60s; add `LiteLLM_SpendLogs` retention.

## 7. Phased roadmap

**Phase 0 — De-risk (no routing change).** Pin image + version floor; set/back-up `LITELLM_SALT_KEY`; add the gateway-down→direct dispatch fallback; harden `get_spend_logs` (start_date/pagination) and add the push callback. *Verify:* existing traffic unchanged; fallback exercised by killing the gateway pod on k3d.

> **Status — 2026-06-28, DONE + committed (`a5bae034`) on `develop`, deployed to dev. k3d-verified.**
> - ✅ **Image pinned** `litellm-database:main-stable` → `v1.90.0` (digest `sha256:b4b2eb22…`), `helm/values.yaml` (overlays don't override → covers dev/local/prod). Helm-template-rendered.
> - ✅ **Salt key** — already wired as a required `secretKeyRef` + verified on dev: present and **distinct from master** (Vault-backed via ESO `dataFrom: extract`, so already "backed up"). No change needed.
> - ✅ **Gateway-down→direct fallback** — cached `_gateway_healthy` flag in `litellm_gateway.py` (`mark_gateway_health`/`gateway_is_healthy`), set by the sync loop's `is_ready()` probe **and** the per-dispatch scoped-key probe; `_gateway_routing_target()` returns None when down → both dispatch injectors fall to the endpoint's direct creds (stale-gateway-base_url cleanup made health-independent). Optimistic cold-start preserves happy-path behavior. **k3d-verified:** scaled `srw-litellm`→0, orchestrator logged `health transition: up -> down` (sync probe), then auto-recovered `down -> up` on scale-back — exactly two sync ticks apart.
> - ✅ **`get_spend_logs` start_date** — new `start_date`/`end_date` params; `materialize_llm_usage` threads its cursor day (−1d tz margin; dedupe makes over-scan free) so a high-rate window can't silently drop unreached rows. First call / post-restart stays unbounded to backfill.
> - ⏳ **Deferred:** the `CustomLogger` **push callback** (gateway config + new authed orchestrator endpoint + a callback module) — larger build, better validated with real traffic; folded into **Phase 3** (metering completeness). Also deferred from §6: `proxy_batch_write_at`/`LiteLLM_SpendLogs` retention tuning. `get_spend_logs` pagination not added (LiteLLM's `/spend/logs` is list-shaped on the pinned version; `start_date` is the load-bearing bound).
> - Tests: +5 unit (`TestGatewayHealth`, `TestSpendLogsStartDate`, materializer-passes-cursor) + 2 (`TestGatewayHealthFallback` in `test_thread_config_persistence.py`); touched-path suites green (226 + 124). Lint clean.

**Phase 1 — Register every model (no routing change).** Widen `build_desired_models` to system rows + codex `use_responses_api`; per-provider `litellm_params` + `params_json` overrides; fix `srw_rev` to include `models`/`system_api_keys` `updated_at`. *Verify:* gateway `/v1/models` lists minimax/gemini/codex; agents still bypass (no behavior change yet).

> **Status — 2026-06-28, DONE + committed (`3e7b67bf`) on `develop`, deployed to dev. k3d-verified.** All in `orchestrator/services/litellm_gateway.py` (zero `main.py`/dispatch change → agents still bypass, as designed).
> - ✅ **Codex knob confirmed on the pinned v1.90.0** first (§4.5): registered a temp model `openai/gpt-5.5`+`use_responses_api:true` at `srw-codex-proxy`, called via the `/chat/completions` bridge → `reasoning_content` (478 chars) + reasoning_tokens + `x-litellm-response-cost`. `use_responses_api` is the right knob.
> - ✅ **`build_desired_models` widened** — now queries `provider_kind="endpoint"` **and** `"system"`. Endpoint rows unchanged except the Codex proxy now gets `use_responses_api:true` (detected by `_is_codex_endpoint`, label/base `codex-proxy`). System rows → `_LITELLM_SYSTEM_ROUTE` prefix (`google`→`gemini/`, else identity; skip double-prefix) + decrypted `get_system_api_key(provider_ref)` + `drop_params:true` (the gemini `parallel_tool_calls` guard). `params_json["litellm"]` sub-key shallow-merges per-model overrides (zero-migration).
> - ✅ **`srw_rev` fix** — `_rev_for_endpoint`→`_rev_for_model`, sums the catalog row + endpoint + system-key `updated_at`, so a params_json edit or key rotation re-registers (one-time re-register of existing rows applied the codex knob).
> - ✅ **k3d-verified on v1.90.0 — all three provider classes called *through* the gateway:**
>   - **codex** `gpt-5.5` re-registered with `use_responses_api=True` (non-codex endpoints correctly without it); the knob test returned reasoning_content via the bridge.
>   - **gemini** (system/google) → `gemini/gemini-3.5-flash`+`drop_params`; live call HTTP 200, `pong`, metered (`cost=0.000858`), reasoning_tokens=93. (Row restored to disabled; reconcile cleanly de-registered it.)
>   - **minimax** (system/openrouter, `openrouter/minimax/minimax-m3` — already-prefixed id used as-is, no double-prefix) → live call HTTP 200, metered (`cost=5.4e-05`), reasoning_content 74c + reasoning_tokens.
>   - No sync errors throughout.
> - Tests: +12 unit (system openrouter/google routes, no-key skip, codex flag, params_json merge, combined, rev folding). Lint clean.

**Phase 2 — Flip routing through the gateway.** Broaden the dispatch gate to system rows (force `provider="openai"` + gateway creds); remove the codex bypass + rework the stale-base_url cleanup; add the fail-loud unregistered-model guard. Roll out **per-provider behind a flag / weighted canary**. *Verify (k3d):* a real session/job on minimax + gemini + codex captures `reasoning_content` AND produces `usage_events` rows; `x-litellm-response-cost` present.

> **Status — 2026-06-28, DONE + committed (`3bb72235`) on `develop`, deployed to dev. k3d live-verified.** Built **default-off**: empty `LITELLM_GATEWAY_ROUTED_PROVIDERS` = zero behaviour change (gemma already gateway-routed; system direct; codex bypassed). Files: `orchestrator/main.py`, `orchestrator/services/litellm_gateway.py`, `helm/{values.yaml,templates/configmap.yaml,templates/orchestrator/deployment.yaml}`, `tests/test_dispatch_phase_credentials.py`.
> - **`*` wildcard (added 2026-06-28, uncommitted).** `_should_route_via_gateway` now treats `"*"` in the allowlist as "all system/codex providers": `if "*" not in routed and provider not in routed: return False`. So `routedProviders: ["*"]` routes every provider through the gateway and a newly-added provider routes the moment it registers — no per-provider edit. The registered + gateway-health guards still hold (a `"*"` never forces an unregistered model on, or routes when the gateway's down). +3 unit tests. **k3d live-verified:** ConfigMap value literally `*` → minimax dispatched `(canary openrouter)`, which can only match via the wildcard branch. This is the end-state "single path" posture; flipping dev to `["*"]` is gated by the P5/HA SPOF decision.
> - **Dev rollout:** canary committed at `openrouter` (`1163fd8d`/`652dd22c`), live on dev (`sha-1163fd8`) — codex + gemma route + meter; **gemini still bypasses** (google not in the allowlist).
> - **Canary flag** `LITELLM_GATEWAY_ROUTED_PROVIDERS` (comma slugs `google,openrouter,codex`) → helm `litellm.routedProviders` → ConfigMap → orchestrator env. `_gateway_routed_providers()` reads it.
> - **Decision** `_should_route_via_gateway(meta, gw)` — three AND-gates: gateway reachable (covers the P0 health fallback) · provider in the allowlist (`_gateway_canary_provider`: codex, or a system row's `api_key_ref`; gemma/endpoint rows are *not* candidates) · **model registered** (`gateway_registered_models()`, republished by the sync after each reconcile = the fail-loud guard → canaried-but-unregistered routes *direct* with a warning, never 400).
> - **Routing flip** — prepended a canary branch to **both** injectors (`_inject_dispatch_credentials`, `_inject_model_credentials`): force `provider="openai"` + gateway base_url/key (**assigned**, not setdefault, so the upstream key never reaches the gateway and a stale hot-swap key can't shadow it). Codex bypass + the existing endpoint/direct branches stay for the non-canaried cases.
> - **k3d live (canary `openrouter,codex` via ConfigMap + Reloader roll, then reverted):**
>   - **minimax** → dispatch `routed … via LiteLLM gateway (canary openrouter)` + agent `base_url=http://srw-litellm:4000/v1` + gateway spend-log rows (tokens + `spend=0.00498`) + **20 `usage_events` rows materialized** (Gap-A traffic now metered). `usage_events.cost_usd=NULL` = the documented **Gap-B** (priced from the empty `usage_rates`; Phase 3, §4.4).
>   - **codex** → dispatch `(canary codex)` + agent `Created OpenAI LLM: model=gpt-5.5 base_url=…srw-litellm… reasoning=chat_completions(effort=high)` — i.e. the openai factory + the gateway Chat↔Responses bridge, not the client-side codex factory.
>   - gemini = same system path as minimax (disabled on k3d; Phase-1-proven registered + functional).
> - Tests: +8 unit (canary helpers + dispatch integration: codex/system × canaried/not × registered/not). Full suite **7259 passed** (1 unrelated env DB-conn failure). Lint + format clean.
> - **k3d/Tilt gotcha:** the orchestrator runs 2 replicas; a ConfigMap-env change rolls them via Stakater Reloader, and Tilt **re-syncs** the live_update code to the new pods (verify both replicas have env+code before testing). A pod roll alone loses live_update until Tilt re-syncs.

**Phase 3 — Metering completeness.** Seed `usage_rates` for paid models (or switch the materializer to LiteLLM's `response_cost`). *Verify:* `usage_events.cost_usd` non-null for minimax/gemini/codex; gemma stays 0/NULL (correct).

> **Status: ✅ DONE + committed (`27d026ac`) on `develop`, deployed + LIVE on dev** (`orchestrator/services/litellm_gateway.py` + `tests/test_litellm_gateway.py`; zero schema/migration change). Production proof: dev `usage_events` shows codex GPT-5.5 **$100.79** + gpt-5.4-mini **$15.37** costed (gemma $0). Chose **gateway-priced** (not seed-`usage_rates`) — the as-built rationale is in §4.4. `materialize_llm_usage` now appends, for `spend > 0`, a `unit='request'` / `quantity=1` / `cost_usd=spend` dimension row (token rows stay cost-free; `SUM == spend`, no double-count; pre-set cost skips the empty `usage_rates` lookup). `usage_rates` is untouched and still prices `category='compute'`.
> - **Live (k3d, auditdb `usage_events`):** a cursor-reset full backfill materialized **+40** `request` cost rows from the existing paid spend logs. Priced exactly as the gateway computed: minimax-m3 $0.169846 (34 rows), gpt-5.5/codex $0.274610 (3), gemini-3.5-flash $0.000858, codexknob-test/2 $0.002165/$0.007240. **gemma-4-moe / -strix + the zzz-test models emit NO `request` row** (spend=0 → cost absent ✓). Token rows remain `priced=0` for all models.
> - **`/api/usage` headline** (`query_usage` aggregate): `total_cost_usd` **$0 → $0.454719**; `(llm, request)` line carries the cost, `(llm, prompt-token|completion-token)` stay $0, `(compute, *)` stay $0 (unpriced — separate future seeding).
> - **Tests:** +6 unit (`TestMaterializeLlmUsageCost`: paid emits cost row, free/missing/unparseable spend emit none, prompt-only still priced, re-poll idempotent). Existing materializer tests unaffected (they set no `spend`). Lint + format clean.
> - **Deferred (unchanged):** the `CustomLogger` push-callback ledger feed (still poll-based); per-job LLM attribution (gateway lacks `job_id`); seeding `usage_rates` for `category='compute'` (vcpu/gib-hour priced from pdu/power data).

**Phase 4 — Agent simplification (cleanup).** Collapse to the single openai factory; relocate the gemini-3 temp floor + `parallel_tool_calls`/`include_thoughts`; demote the multi-shape reasoning tap to safety net; retire the client-side Responses path; keep a bypass lane. *Verify:* full regression incl. a tool-using gemini turn and a reasoning-heavy codex turn.

> **Scope sharpened 2026-06-28 → `docs/features/agent_llm_factory_collapse.md`** (from a live trace). Key facts: `create_llm` (`loader.py:2701`) still dispatches to **7** factories; routed traffic only converges on `_create_openai_llm` because the canary forces `provider="openai"` — so the other six are dead **only on the routed path**, reached via the direct/bypass fallback. **Auxiliary is already collapsed** (built by the same `create_llm` at `agent.py:494` with no `provider` → always the openai factory; routed + metered on the same canary terms as the main model). Five work items in the doc: (1) retire the six factories from the hot path, (2) relocate the gemini traps to the gateway entry — *`parallel_tool_calls` already covered by P1's `drop_params`; the temp floor remains*, (3) demote the multi-shape reasoning tap, (4) retire `_create_codex_llm` (client-side Responses; codex now uses the gateway bridge), (5) keep a minimal bypass lane (the crux — native Google/Anthropic aren't OpenAI-compatible without the gateway). **Sequencing: Phase 4 follows the `*` rollout** (factories aren't provably dead until every provider routes). Adjacent metering refinements (aux-vs-main label, per-job attribution) tracked in that doc's §5.

**Phase 5 — HA & production hardening.** Stand up Redis/Valkey → wire `router_settings.redis_*` → scale to ≥2 replicas + PDB + HPA + probes; DB sizing (pool limit, PgBouncer, `use_redis_transaction_buffer` at scale); Prometheus `/metrics` (auth it) + alerts (`litellm_deployment_state`, budget-remaining, a drop in `/v1/models` count). Separately: close the **session quota-freeze gap** (sessions consume quota but aren't stoppable — `quota_poll_loop` freezes jobs only).

*Dependency order:* 1 must precede 2 (else routing → hard failures); 3 follows 2; 4 is independent cleanup; 5 is required before multi-replica but the Phase-0 fallback covers the single-replica SPOF interim.

## 8. Risks & open questions
- **SPOF interim** (Phases 2-4 run on a single-replica gateway). Mitigated by the Phase-0 fallback; full HA is Phase 5. Decide risk tolerance.
- **Unregistered-model hard failure** — the fail-loud guard (§4.2) is mandatory, not optional.
- **Passthrough is NOT metered** — LiteLLM #24204 is "closed" only by a label-fix PR; the real spend fix (#24205) is **unmerged**. So **never** use passthrough routes (`/v1/messages`, `/anthropic`) for metered traffic; use `/chat/completions` + native `/responses` only. (This is why Anthropic interleaved-thinking *signatures*, if ever needed, stay a genuine exception — but SRW runs no Anthropic.)
- **Gemini-via-LiteLLM less battle-tested** than OpenRouter in our testing — validate reasoning + usage on the `gemini/` route specifically.
- **Codex knob** (§4.5) — confirm `use_responses_api` vs `mode: responses` on the pinned version.
- ~~**Cost in the SRW ledger** needs Phase 3; don't assume routing fixes it.~~ **Resolved (P3):** LLM `cost_usd` now comes from LiteLLM's per-request `spend` (gateway-priced `unit='request'` row), not `usage_rates`.
- **Provider-pin / OpenRouter niceties** (`reasoning_details`, `reasoning_split`, `provider.order`) move into the gateway entry if wanted — reasoning *capture* survives without them.

## 9. Best-practices checklist (condensed, from web research)
- Normalize everything to `/chat/completions`; native `/responses` for codex only; **avoid passthrough for metered traffic** (#24204/#24205).
- Pin `1.90.0`; ≥ v1.86.0. Integration-test the chat↔responses bridge (known edge bugs: #21331 parallel tool-calls on index 0, #25429 empty gpt-5.4 output).
- Reasoning: `reasoning_content` (all) + `thinking_blocks`+`signature` (Anthropic only); `reasoning_tokens` at `usage.completion_tokens_details.reasoning_tokens`. OpenRouter streaming-reasoning bug is fixed (≥ v1.63.x). Set `modify_params: true` as an Anthropic-thinking safety net (future).
- Custom pricing for self-hosted via `model_info.input_cost_per_token` (LiteLLM view). As of **P3**, SRW's ledger takes **LLM** `cost_usd` from LiteLLM's per-request `spend` (so a self-hosted model with `input_cost_per_token` set in the gateway would flow through to `cost_usd` automatically); `usage_rates` now prices **compute** only (§4.4).
- Routing: `simple-shuffle` (avoid `usage-based-routing-v2`, #16060); `num_retries`/`allowed_fails`/`cooldown_time`; cross-provider `fallbacks`.
- HA: Redis before replicas; `database_connection_pool_limit` formula + PgBouncer; `use_redis_transaction_buffer` at 1000+ RPS; pod = 1 CPU / 4 Gi per worker.

## 10. Sources
Codebase trace (file:line throughout) — 4 read-only subagents, 2026-06-28. Web research — 2 subagents, 2026-06-28:
- LiteLLM docs: response_api, providers/openai, reasoning_content, pass_through/intro+openai_passthrough, proxy/{cost_tracking,custom_pricing,prod,deploy,health,db_deadlocks,virtual_keys,security_encryption_faq,prometheus}, routing, load_balancing.
- GitHub: #24204 + PR #26248 (label-only close) + PR #24205 (real spend fix, open); #8631/#9094 (OpenRouter streaming reasoning, fixed); #28146 (v3 limiter regression, fixed v1.86.0); #16060 (usage-based-routing-v2); #21331/#25429 (bridge bugs); releases (v1.90.0).
- Background: `docs/issues/system_provider_models_bypass_gateway_unmetered.md`, `docs/done/litellm_gateway_drops_gpt_codex_reasoning_capture.md`.
