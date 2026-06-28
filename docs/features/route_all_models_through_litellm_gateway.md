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

**Status:** Proposed, **research-backed 2026-06-28** (6 subagents: 4 codebase traces + 2 web). Empirically de-risked first: see `docs/issues/system_provider_models_bypass_gateway_unmetered.md` §§ "Empirical addendum" + "Codex-proxy follow-up" — we proved a real LiteLLM proxy (v1.83.7 local / v1.89.3 k3d) preserves reasoning + meters for every model family we run, including the Responses-API-only codex-proxy. This doc is the implementation plan to make the gateway the **single path for all LLM traffic**.

**One-line goal:** every model (minimax, gemini, gemma, gpt-5.x/codex — and any future provider) routes through `srw-litellm:4000`, so we get unified **metering + rate-limiting + quota + reasoning capture + key management** in one chokepoint, instead of the current half-in/half-bypassed state.

---

## 1. Why now

Today the gateway meters only *some* models; the paid external ones (minimax/OpenRouter, gemini/Google) **bypass it entirely** and codex was **deliberately bypassed** to keep its reasoning. Both gaps are documented in `system_provider_models_bypass_gateway_unmetered.md`. We've now shown the bypasses are unnecessary: LiteLLM carries reasoning + cost for all of them. Routing everything through closes the metering gap, fixes the inverted coverage (we currently meter the *free* self-hosted lane and miss the *paid* lane), and removes per-provider drift in the agent.

## 2. Current state (what routes where)

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
- The agent can **collapse to a single `_create_openai_llm`** (all factories already build the same `ReasoningChatOpenAI` wrapper) and **drop** `langchain_google_genai`, `langchain_groq`, `langchain_anthropic`, and the client-side Responses-API codex path.
- **Keep** (provider-agnostic, load-bearing): the `ReasoningChatOpenAI` wrapper — its SSE reasoning tap (LangChain drops `reasoning_content` from CC deltas), Layer-0 context-overflow guard, granular timeout, KeyRing; and the request-param policy (`max_tokens`/`_resolve_max_output_tokens`, `reasoning_effort`, `top_k`/`extra_body`).
- **Two traps that need a new home before deleting the Google factory:** the **gemini-3 temperature-0 loop floor** (`_create_google_llm:3070`, forces `temperature≥1.0`; no gateway equivalent → relocate to `model_config_matrix.yaml` + apply pre-send) and the **`parallel_tool_calls` suppression** (`supports_parallel_tool_calls:2675`, keyed on `provider=="google"` — once Google routes as `provider="openai"` this stops firing → re-key on the model, or rely on LiteLLM `drop_params`). Also relocate `include_thoughts`.
- **`reasoning_chat.py`:** keep `reasoning_content` primary + the SSE tap (mandatory); demote the `.reasoning`/`.reasoning_details` shapes to a documented safety net (for the bypass lane); the Responses-API content-block extractor + codex raw-dump diagnostics can be removed once the client-side Responses path is retired.

### 4.4 Metering / cost — **the subtle one (corrects an earlier assumption)**
The issue doc says "Gap B fixes itself for the paid lane." That is **true only for LiteLLM's own view** (`x-litellm-response-cost`, `/spend/logs.spend`). It is **false for SRW's `usage_events.cost_usd`**: `materialize_llm_usage` (`litellm_gateway.py:923`) **deliberately ignores LiteLLM's `spend`** ("dollar-only, 0 for unpriced homelab") and re-prices token quantities from the app-DB **`usage_rates`** table — which **ships empty** (`migrations/app/0033`). So routing through the gateway alone leaves `cost_usd` NULL. To actually populate it, either:
- **(preferred)** seed `usage_rates` rows for the paid models (minimax/OpenRouter, gemini/Google) — the materializer snapshots them at write time (history-immutable); leave free gemma NULL (its cost is `category='compute'`), or
- change `materialize_llm_usage` to consume LiteLLM's computed `response_cost`/`spend` when present, falling back to `usage_rates`.

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

> **Status — 2026-06-28, mostly DONE on develop (uncommitted).**
> - ✅ **Image pinned** `litellm-database:main-stable` → `v1.90.0` (digest `sha256:b4b2eb22…`), `helm/values.yaml` (overlays don't override → covers dev/local/prod). Helm-template-rendered.
> - ✅ **Salt key** — already wired as a required `secretKeyRef` + verified on dev: present and **distinct from master** (Vault-backed via ESO `dataFrom: extract`, so already "backed up"). No change needed.
> - ✅ **Gateway-down→direct fallback** — cached `_gateway_healthy` flag in `litellm_gateway.py` (`mark_gateway_health`/`gateway_is_healthy`), set by the sync loop's `is_ready()` probe **and** the per-dispatch scoped-key probe; `_gateway_routing_target()` returns None when down → both dispatch injectors fall to the endpoint's direct creds (stale-gateway-base_url cleanup made health-independent). Optimistic cold-start preserves happy-path behavior. **k3d-verified:** scaled `srw-litellm`→0, orchestrator logged `health transition: up -> down` (sync probe), then auto-recovered `down -> up` on scale-back — exactly two sync ticks apart.
> - ✅ **`get_spend_logs` start_date** — new `start_date`/`end_date` params; `materialize_llm_usage` threads its cursor day (−1d tz margin; dedupe makes over-scan free) so a high-rate window can't silently drop unreached rows. First call / post-restart stays unbounded to backfill.
> - ⏳ **Deferred:** the `CustomLogger` **push callback** (gateway config + new authed orchestrator endpoint + a callback module) — larger build, better validated with real traffic; folded into **Phase 3** (metering completeness). Also deferred from §6: `proxy_batch_write_at`/`LiteLLM_SpendLogs` retention tuning. `get_spend_logs` pagination not added (LiteLLM's `/spend/logs` is list-shaped on the pinned version; `start_date` is the load-bearing bound).
> - Tests: +5 unit (`TestGatewayHealth`, `TestSpendLogsStartDate`, materializer-passes-cursor) + 2 (`TestGatewayHealthFallback` in `test_thread_config_persistence.py`); touched-path suites green (226 + 124). Lint clean.

**Phase 1 — Register every model (no routing change).** Widen `build_desired_models` to system rows + codex `use_responses_api`; per-provider `litellm_params` + `params_json` overrides; fix `srw_rev` to include `models`/`system_api_keys` `updated_at`. *Verify:* gateway `/v1/models` lists minimax/gemini/codex; agents still bypass (no behavior change yet).

**Phase 2 — Flip routing through the gateway.** Broaden the dispatch gate to system rows (force `provider="openai"` + gateway creds); remove the codex bypass + rework the stale-base_url cleanup; add the fail-loud unregistered-model guard. Roll out **per-provider behind a flag / weighted canary**. *Verify (k3d):* a real session/job on minimax + gemini + codex captures `reasoning_content` AND produces `usage_events` rows; `x-litellm-response-cost` present.

**Phase 3 — Metering completeness.** Seed `usage_rates` for paid models (or switch the materializer to LiteLLM's `response_cost`). *Verify:* `usage_events.cost_usd` non-null for minimax/gemini/codex; gemma stays 0/NULL (correct).

**Phase 4 — Agent simplification (cleanup).** Collapse to the single openai factory; relocate the gemini-3 temp floor + `parallel_tool_calls`/`include_thoughts`; demote the multi-shape reasoning tap to safety net; retire the client-side Responses path; keep a bypass lane. *Verify:* full regression incl. a tool-using gemini turn and a reasoning-heavy codex turn.

**Phase 5 — HA & production hardening.** Stand up Redis/Valkey → wire `router_settings.redis_*` → scale to ≥2 replicas + PDB + HPA + probes; DB sizing (pool limit, PgBouncer, `use_redis_transaction_buffer` at scale); Prometheus `/metrics` (auth it) + alerts (`litellm_deployment_state`, budget-remaining, a drop in `/v1/models` count). Separately: close the **session quota-freeze gap** (sessions consume quota but aren't stoppable — `quota_poll_loop` freezes jobs only).

*Dependency order:* 1 must precede 2 (else routing → hard failures); 3 follows 2; 4 is independent cleanup; 5 is required before multi-replica but the Phase-0 fallback covers the single-replica SPOF interim.

## 8. Risks & open questions
- **SPOF interim** (Phases 2-4 run on a single-replica gateway). Mitigated by the Phase-0 fallback; full HA is Phase 5. Decide risk tolerance.
- **Unregistered-model hard failure** — the fail-loud guard (§4.2) is mandatory, not optional.
- **Passthrough is NOT metered** — LiteLLM #24204 is "closed" only by a label-fix PR; the real spend fix (#24205) is **unmerged**. So **never** use passthrough routes (`/v1/messages`, `/anthropic`) for metered traffic; use `/chat/completions` + native `/responses` only. (This is why Anthropic interleaved-thinking *signatures*, if ever needed, stay a genuine exception — but SRW runs no Anthropic.)
- **Gemini-via-LiteLLM less battle-tested** than OpenRouter in our testing — validate reasoning + usage on the `gemini/` route specifically.
- **Codex knob** (§4.5) — confirm `use_responses_api` vs `mode: responses` on the pinned version.
- **Cost in the SRW ledger** needs Phase 3; don't assume routing fixes it.
- **Provider-pin / OpenRouter niceties** (`reasoning_details`, `reasoning_split`, `provider.order`) move into the gateway entry if wanted — reasoning *capture* survives without them.

## 9. Best-practices checklist (condensed, from web research)
- Normalize everything to `/chat/completions`; native `/responses` for codex only; **avoid passthrough for metered traffic** (#24204/#24205).
- Pin `1.90.0`; ≥ v1.86.0. Integration-test the chat↔responses bridge (known edge bugs: #21331 parallel tool-calls on index 0, #25429 empty gpt-5.4 output).
- Reasoning: `reasoning_content` (all) + `thinking_blocks`+`signature` (Anthropic only); `reasoning_tokens` at `usage.completion_tokens_details.reasoning_tokens`. OpenRouter streaming-reasoning bug is fixed (≥ v1.63.x). Set `modify_params: true` as an Anthropic-thinking safety net (future).
- Custom pricing for self-hosted via `model_info.input_cost_per_token` (LiteLLM view) — but remember SRW's ledger prices from `usage_rates` (§4.4).
- Routing: `simple-shuffle` (avoid `usage-based-routing-v2`, #16060); `num_retries`/`allowed_fails`/`cooldown_time`; cross-provider `fallbacks`.
- HA: Redis before replicas; `database_connection_pool_limit` formula + PgBouncer; `use_redis_transaction_buffer` at 1000+ RPS; pod = 1 CPU / 4 Gi per worker.

## 10. Sources
Codebase trace (file:line throughout) — 4 read-only subagents, 2026-06-28. Web research — 2 subagents, 2026-06-28:
- LiteLLM docs: response_api, providers/openai, reasoning_content, pass_through/intro+openai_passthrough, proxy/{cost_tracking,custom_pricing,prod,deploy,health,db_deadlocks,virtual_keys,security_encryption_faq,prometheus}, routing, load_balancing.
- GitHub: #24204 + PR #26248 (label-only close) + PR #24205 (real spend fix, open); #8631/#9094 (OpenRouter streaming reasoning, fixed); #28146 (v3 limiter regression, fixed v1.86.0); #16060 (usage-based-routing-v2); #21331/#25429 (bridge bugs); releases (v1.90.0).
- Background: `docs/issues/system_provider_models_bypass_gateway_unmetered.md`, `docs/done/litellm_gateway_drops_gpt_codex_reasoning_capture.md`.
