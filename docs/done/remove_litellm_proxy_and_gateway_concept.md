# Remove the LiteLLM proxy — and the gateway/proxy concept entirely (for now)

**Date:** 2026-07-01

**Closed by the 2026-08-06 doc-truth sweep (batch #3):** Removal + replacement shipped (`cce858e4`/`8119a5c4`/`81479875`/`10479d56`/`d1ec4189`) — zero litellm references left in helm/ or orchestrator code; audit-based metering, OpenRouter pricing sync and codex usage bars live.

**Status:** **REMOVAL IMPLEMENTED.** We removed the self-hosted LiteLLM proxy and, with it, the "route all LLM traffic through a self-hosted gateway" concept — **for now** (see § "When we'd bring a gateway back"). This doc supersedes the "stabilize LiteLLM" and "swap to another gateway (Bifrost/Kong/agentgateway)" directions explored on the way here.
**Progress (2026-07-08):** P0 **done** (homelab/local routing direct). **P1 (restore monitoring in-process) — Slices 1–3 DONE + k3d E2E-verified**. In-process metering (audit `llm_requests` → priced `usage_events`) is live on k3d; the live run caught + fixed a critical id-vs-timestamp cursor bug. **P3 cleanup done:** the Helm chart and local/experimental values no longer render LiteLLM or its dedicated DB; the orchestrator no longer starts sync/quota loops; the app-side gateway service, dispatch branches, health gate, and gateway-only tests are removed. See § Plan / § k3d E2E.
**Severity:** **High (reliability).** The gateway OOM-crashloops and takes down agent/loop jobs; it is the current blocker to the continuously-running self-improvement loop (the north star). Removal unblocks it immediately.
**Decision owners:** platform (Niklas). This is an architecture-direction decision, not just a bugfix.
**Component / blast radius:**
- Gateway deployment — removed from Helm and local/experimental values.
- Dispatch routing — direct endpoint/provider credential injection only; gateway routing helpers removed.
- Metering pipeline — auditdb `llm_requests` → `usage_events` via `orchestrator/services/audit_usage.py`; Cockpit "By Model" view (`GET /api/usage`) reads the ledger as before.
- Independent token source that survives — MongoDB `llm_requests` audit (the agent records per-request `usage` token counts regardless of routing; e.g. job `b17d63bc` shows `tokens=[16536/267/16803]`).
**Related:**
- `docs/features/router_comparison.md` — the 11-alternative evaluation that concluded none is a clean drop-in (produced 2026-07-01, same investigation).
- `docs/features/route_all_models_through_litellm_gateway.md` — the roadmap this decision **reverses**.
- `docs/features/agent_llm_factory_collapse.md` — aligned; the single OpenAI-compatible factory is the vehicle for in-process metering.
- `docs/features/credential_broker.md` — the temporal-key idea (deferred; → Keycloak JWTs).
- `docs/features/usage_dashboard.md`, `docs/features/observability_and_quotas.md` — the usage view + deferred per-job cost attribution.
- `docs/done/litellm_gateway_drops_gpt_codex_reasoning_capture.md` — why codex already bypasses the gateway.
- `docs/issues/reasoning_capture_regressions_on_routing_and_factory_changes.md` — the recurring class of "routing change silently drops reasoning"; this removal is the fix-direction (codex bypasses entirely), and that doc proposes encoding the bypass as a guard so a stray `routedProviders` can't silently reintroduce the drop (as it did on k3d 2026-07-01).
- `docs/issues/system_provider_models_bypass_gateway_unmetered.md`, `docs/issues/codex_session_gateway_baseurl_401.md`, `docs/issues/litellm_reranker_model_unregistered.md` — the LiteLLM sharp-edge rap sheet.
- Memory: `project_litellm_oom_crashloop_blocks_loop`, `reference_usage_view_gateway_metering_routing`.

---

## Decision (one paragraph)

**Turn off the LiteLLM proxy and stop routing LLM traffic through a self-hosted gateway.** Route cloud models through **OpenRouter** (which already gives us OpenAI-wire + genuine reasoning-parameter normalization), self-hosted/codex/gemini **direct** (as most already are), and **meter in-process** by stamping the token counts we already capture into the `usage_events` ledger. Do **not** replace LiteLLM with another gateway (Bifrost/Kong/agentgateway) — for our provider set a gateway is redundant middleware whose jobs we already do elsewhere. Defer tenant-credential isolation to **Keycloak-issued short-lived JWTs** if/when we want it.

---

## How this surfaced

Started as an incident: loop iter-2 job `b17d63bc` (CRITIC, MiniMax-M3) failed with `Connection error.` on 5 consecutive LLM calls → circuit breaker froze it. Investigation traced it to the `srw-litellm` gateway pod **OOM-crashlooping**, and the question escalated from "fix the OOM" → "stabilize vs swap vs remove the gateway" → **"remove the proxy concept entirely."** The reasoning that got us here is recorded below because each step killed an option.

## Root cause (why "stabilize" is a treadmill, not a fix)

`srw-litellm` (image `ghcr.io/berriai/litellm-database:v1.90.0`, DB mode, single worker, `2Gi` limit) **OOM-crashloops**: boots → serves 3–10 min → balloons `512Mi→2Gi` in ~60s with CPU pegged → **OOMKilled (exit 137)** → ~5 min backoff → repeat (~113 restarts / 15 h).

- **It is NOT request-driven.** Proven while the loop was dead: the pod's whole access log was `6× /health/readiness + 2× /model/info`, **zero `chat/completions`**, and it still OOMed (restart count ticked up mid-investigation). Node memory was fine (22–50% used) — it's the pod's own cgroup limit.
- **Cause = a known LiteLLM DB-mode background-scheduler leak** that runs on timers regardless of traffic: the budget-reset job loads whole key tables into memory (GH #13210: *"~500 MB → >16 GB in seconds, then connection errors"*) + a general FastAPI-path leak (#15128). Maintainers acknowledge it (Perf Roadmap #15933; they're hiring a reliability engineer). **v1.90.0 (newest) still leaks; there is no fixed version to upgrade to.**
- **Blast radius:** every gateway-routed model (MiniMax via OpenRouter lane, codex, embeddings, whisper) returns `Connection error.` whenever the gateway is in an OOM/backoff window. Our config is clean (`model_list: []`, `drop_params`, `telemetry: false`, no callbacks) — this is LiteLLM's bug, not our setup.

Mitigations exist (4 Gi + `disable_reset_budget` + time-based restart) but they **manage** a chronic leak class; they don't remove it. Stabilize = keep renting a Python service that leaks, on our critical path.

## Why not swap to another gateway

We ran a 6-agent deep evaluation of **11 alternatives** vs a 15-point rubric built from our requirements (full detail + matrix in `docs/features/router_comparison.md`). Headline: **no clean drop-in exists.** LiteLLM's DB-minted virtual keys + queryable spend ledger + daily-$ freeze is an *application* feature set; every candidate gives a reliable runtime + OpenAI routing + telemetry you *finish* into a metering layer — which we already own. Each strong option also misses a *different* load-bearing item:

- **Bifrost** (was the presumed front-runner): **no temporal/TTL keys** (maintainer rejected them) + multi-replica governance is paid-Enterprise-only.
- **TensorZero**: **no `/v1/responses`** (codex blocker) + CLI-only key mint.
- **Kong**: OSS TTL keys + reliable, but token/cost budgets + spend API are Enterprise/rebuild.
- **agentgateway**: closest ops match (Rust, single container, OSS keys + USD cost), but young/no adopters + reasoning-token capture undocumented.
- **Higress**: most production-proven, but **zero cost tracking**.
- **Ruled out:** Helicone (frozen/acquired), LangDB (ELv2, not OSS), One-API (unmaintained).

Swapping = a full adapter rewrite + metering integration + codex re-validation, to end up **more** dependent on an external service, not less — for a feature set we already have. Not worth it.

## The two reframes that made "remove entirely" the answer

### Reframe 1 — temporal keys were never a missing LiteLLM feature
The stated top reason for a gateway was security: pods hold short-lived keys the router swaps for the real provider key, valid only for this job/pod/window. But:
- LiteLLM **natively** supports runtime TTL keys (`/key/generate` + `duration`, server-enforced). Our problem was *reliability* + a deliberate architectural pivot away from per-job keys (enforcement lives on shared team/user objects; `--loop` pod-reuse made per-job keys pointless) — **not a missing feature**.
- Its security value is **bounded**: for a live, prompt-injectable agent *"access is the asset,"* so key-swapping defends against exfiltration/cross-tenant reuse, not in-session abuse (our own `credential_broker.md` says this).
- The **durable** answer is **Keycloak-issued short-lived JWTs** validated wherever the call is made (`exp` enforced, revocable centrally) — we already run Keycloak, and it doesn't require any gateway to mint/hold secrets. **→ Defer; solve with Keycloak, not a proxy.**

### Reframe 2 — "unify all providers into one schema" is largely redundant for *our* provider set
The reason that surfaced *after* adoption (and the one I'd argued for) was schema unification — avoid hand-parsing every provider's format. On inspection, LiteLLM does almost **no** real translation for us, and what it does is shallow:

| Model / lane | How it routes | Who does the translation |
|---|---|---|
| codex / gpt-5.x | Responses API via codex-proxy | **bypasses LiteLLM** entirely |
| gemini | native Google `generateContent` | **bypasses LiteLLM** (direct) |
| MiniMax-M3 | via **OpenRouter** | **OpenRouter** unifies; LiteLLM just forwards OpenAI-wire |
| gemma-4 | self-hosted **OpenAI-compatible** endpoint | nothing to translate |
| embeddings (qwen3) / whisper | direct | never touched the gateway |

So LiteLLM was mostly **forwarding OpenAI-wire traffic**. The formats it's genuinely good at (Anthropic Messages, Bedrock, Gemini native) are ones we bypass or don't use. And critically, its unification is **envelope-deep, not semantic**: it wraps a call in OpenAI shape but `reasoning_effort: high` (OpenAI) vs `thinking: {budget_tokens}` (Anthropic) vs gemma's own thinking knob **leak through** — it does *not* collapse them into one parameter. **OpenRouter does** (its unified `reasoning: {effort | max_tokens}` maps to each provider's native mechanism). So the one place real, deep unification is worth having, **OpenRouter already provides — and we already use it.** A self-hosted translation layer (LiteLLM as server *or* as a library) is not load-bearing for us.

### Net: what the gateway uniquely did for us ≈ nothing
- **Metering** → we already materialize `usage_events` ourselves; Cockpit reads that, not the gateway DB. In-process we also get **per-job attribution** (the gateway structurally never sees `job_id`).
- **Cost** → LiteLLM's was dollar-only and **0 for our self-hosted models**; we'd compute it *better* in-process.
- **Key isolation** → ships **inert**; durable answer is Keycloak JWTs + the per-job credential injection we already do at dispatch.
- **Rate-limit / quota** → already enforced **orchestrator-side**.
- **Schema unification** → OpenRouter (cloud) + OpenAI-wire (self-hosted) + existing codex/gemini paths.
- **Single choke point / egress** → the orchestrator already injects creds per job (it *is* the choke point); embeddings/whisper/TTS already bypass, so the "single plane" was already not single.

## What removal means (consequences)

**Works / unaffected:** inference (models route direct via the existing `gateway_is_healthy() → None` fallback), the loop, job stats, audit trail, agent activity, MongoDB `llm_requests` token capture, everything not the LLM cost view.

**The one real loss:** the Cockpit **"By Model" usage/cost view goes dark for new traffic**, because that pipeline is fed *only* by polling the gateway's `/spend/logs` → `usage_events`. Historical rows remain. **But the raw per-request token data survives** in MongoDB `llm_requests` (agent-captured, gateway-independent). So the fix is small: compute cost from those tokens × a price map and write `usage_events` rows in-process (§ P1). Until then, that view shows no new data.

**Also lost (accepted / deferred):** centralized virtual-key isolation (→ Keycloak later), a single gateway measurement plane, LiteLLM's per-request `spend` cost number.

## Target architecture (gateway-free)

```
cloud models (MiniMax, future)   → OpenRouter (OpenAI-wire + reasoning normalization)
self-hosted (gemma, vLLM, …)     → direct OpenAI-compatible endpoint
codex / gpt-5.x                  → Responses API via codex-proxy (unchanged)
gemini                           → existing direct path (or OpenRouter if OpenAI-wire wanted)
metering                         → in-process: llm_requests tokens × price map → usage_events
tenant credentials               → per-job injection now; Keycloak short-lived JWTs later
```
No self-hosted gateway. No translation library. No DB-mode governance server → no OOM class.

## Plan / work items

- **P0 — Disable the proxy. ✅ DONE (disabled on homelab; loop routes direct).** `deployment/values-experimental.yaml`: `litellm.enabled: false`, `routedProviders: []`; `databases.litellm.enabled: true` kept (litellmdb + spend history survive a re-enable). Values-only → Fleet applied without a chart rebuild. *(Still uncommitted on `develop` — commit together with this doc + the Slice-1 pricing module.)*
- **P1 — Restore monitoring in-process** (the one real gap). **Design (decided):** the materializer emits **cost-free** `prompt-token` / `completion-token` `usage_events` rows keyed by model id; `UsageLedger.record_events` (`usage_ledger.py:179-184`) already auto-prices any token row via `UsageRates.resolve(category, resource, unit, ts)` against the effective-dated `usage_rates` table (`migrations/app/0033`). So "cost" = **seed `usage_rates`** — no per-request cost row, no gateway `spend`. Source = the auditdb **`llm_requests`** SQL table (`audit_writer.py:102`), written by **both** job agents and sessions (`persistent_app.py:3304`), same Postgres as `usage_events` — no Mongo. Free wins vs the gateway: **per-job attribution** (`job_id`) + **session coverage** + backfillable.
  - **Slice 1 — OpenRouter pricing → `usage_rates`. ✅ BUILT + unit-tested (15 tests pass, `ruff` clean; uncommitted `develop`).** New `orchestrator/services/openrouter_pricing.py`: fetches OpenRouter's public catalog (`/api/v1/models`, no auth) → seeds per-model `prompt-token`/`completion-token` `$/token`; effective-dated + **change-only** (re-run inserts a row only on a price move → no table bloat, no history rewrite); non-fatal. Tests: `tests/test_openrouter_pricing.py`.
  - **Slice 2 — audit materializer. ✅ BUILT + unit-tested (13 tests pass, `ruff` clean; uncommitted `develop`).** New `orchestrator/services/audit_usage.py` — `materialize_llm_usage_from_audit(audit_pool, app_pool, ledger, *, since_id, min_age_s)`: reads `llm_requests` above the cursor → emits cost-free `prompt-token`/`completion-token` `UsageEvent`s (ledger auto-prices from `usage_rates`). Tests: `tests/test_audit_usage.py`. **Facts pinned during build:**
    - **Token home is source-dependent** (two SELECT fallbacks, both extracted server-side as text via `->>` so it's jsonb-codec-independent): worker rows carry them in `metrics.token_usage` (`prompt_tokens`/`completion_tokens`, or `input_tokens`/`output_tokens` on Anthropic-wire); **session** rows (`agent_type='persistent'`) carry the reliable counts in `metadata.input_tokens`/`metadata.output_tokens` (`persistent_app.py:3323` — streaming leaves `response_metadata.token_usage` empty). `.response` is *not* a token source.
    - **Reasoning tokens are a subset of completion** (billed at the completion rate) → carried in `details` only, never a separate priced row (would double-count).
    - **Attribution:** `agent_type` discriminates — `≠persistent` → `jobs` table, `=persistent` → `threads` table (app DB); both give `user_id`/`project_id`. Soft (no FK): a deleted job's tokens still meter, and `ref_kind`/`ref_id` (`job`/`thread`) give per-entity cost.
    - **Cursor = `llm_requests.timestamp`** (NOT `id`). ⚠️ Originally built on an id cursor assuming `id` is monotone with time; **k3d proved it is not** — the audit sequence was reset (frozen high-id band + active low-id band), so a max-id anchor silently meters nothing. Switched to `timestamp` (monotone at write); `id` stays the unique dedupe `source_id` (bands don't overlap). Advances only over a *contiguous* run of rows older than `min_age_s` (default 60s) — the first too-fresh row halts the tick. Idempotent regardless (ledger dedupes on `source='audit'`, `source_id=str(id)`, `unit`, `ts`).
    - **Pools:** reads `llm_requests` from **auditdb** (`audit_db.pool`, jsonb codec present) + resolves attribution against the **app DB** (`postgres_db.pool`); writes `usage_events` via the ledger (already holds both).
  - **Slice 3 — re-point + pricing wiring. ✅ DONE + k3d E2E-verified.** `llm_usage_poll_loop` now calls `materialize_llm_usage_from_audit(audit_db.pool, postgres_db.pool, usage_ledger)` — gated on the two pools + ledger. **Forward-only anchor = `SELECT max(timestamp)`** at startup (not max-id; see cursor note), so re-pointing never re-meters history under `source='audit'`. New `pricing_sync_task` runs `llm_pricing_sync_loop` (openrouter_pricing.py: enumerate ALL catalog rows via `postgres_db.list_models` → `(model_id, params_json.pricing_id)` pairs → `sync_llm_rates`, 6 h + on startup). Gateway sync/materialization imports are removed.
    - **Pricing-id home:** the catalog's `params_json.pricing_id` (JSONB, no migration). ALL rows enumerated (not just enabled) — a disabled `system` row whose `model_id` matches a recorded string still needs pricing (e.g. `openrouter/minimax/minimax-m3`). No UI field yet → set via DB (follow-up: Admin → Models field).

- **k3d ground-truth verification (read-only, 2026-07-01).** The modules have NOT been *run* on a cluster (un-wired until Slice 3); this is a read-only probe of the inferred assumptions against live k3d DBs (`srw-auditdb-0` = `srw_audit`, app DB `srw-postgres-0`).
  - **CONFIRMED:** (a) Slice 2 token home split holds on real rows — worker (`default`/`scholar`/`critic`/… ≈26k `gemma-4-moe`) → `metrics.token_usage.{prompt,completion}_tokens` populated, `metadata.*` empty; `persistent` sessions → `metrics.token_usage` **empty** (streaming), tokens in `metadata.{input,output}_tokens`. The `_first_int(...)` fallback chain extracts both. (b) `id` is a monotone single-sequence BIGINT (`~1.0e9`); `agent_type='persistent'` cleanly discriminates. (c) Slice 1 fetch works against the real OpenRouter API (334 models; `minimax/minimax-m3`, `openai/gpt-5.5`, `google/gemini-3.5-flash` all present + parse).
  - **FINDINGS (fold into Slice 3 wiring):**
    1. **Resource-key alignment = the pricing risk.** `usage_rates.resource` must match the *recorded* `llm_requests.model`, which varies by dispatch path. Priced pairs to seed (`resource → pricing_id`): `gpt-5.5 → openai/gpt-5.5`, `openrouter/minimax/minimax-m3 → minimax/minimax-m3`, `gemini-3.5-flash → google/gemini-3.5-flash`. Self-hosted/unpriced (`pricing_id=""`): `gemma-4-moe`, `gemma-4-31b`, `RedHatAI/gemma-4-31B-it-FP8-Dynamic` (same model, two recorded strings). **`gpt-5.3-codex-spark` has NO clean OpenRouter id** → manual rate or unpriced.
    2. **Token pricing has never worked:** every existing `source='litellm'` token row has `cost_usd=NULL` (usage_rates empty); only gateway `request` rows are priced. Slice 1 is the fix; no token-pricing regression risk exists to protect.
    3. **k3d litellm is still ENABLED** — P0 disabled it only in homelab `values-experimental.yaml`, not local k3d. A clean Slice 3 E2E on k3d needs the anchor (avoid summing `litellm`+`audit` overlap) or a local litellm-disable.
- **k3d E2E verification — ✅ DONE (2026-07-01).** Deployed Slices 1–3 to k3d (Tilt/uvicorn reload) and drove the full path against live DBs.
  - **🔴 Critical bug caught (would have made metering silently dead):** `llm_requests.id` is **not monotonic with insertion time** on real data — the audit sequence reset around 2026-06-19 (43.5k-row frozen high-id band `≥1e9`; active band climbing from `last_value≈1531`), so recent rows carry ids *far below* `max(id)`. The forward-only **max-id anchor matched zero new rows** → metering would meter nothing forever. **Fixed:** cursor + anchor switched to `timestamp` (monotone at write); `id` kept only as the unique dedupe key (bands don't overlap). Unit tests + read-only shape probes had both passed — only the live run surfaced this.
  - **Slice 1 live:** set `params_json.pricing_id` on `gpt-5.5`/`openrouter/minimax/minimax-m3`/`gemini-3.5-flash`; the pricing loop logged `inserted 6 new usage_rates row(s)` with the correct OpenRouter rates keyed by the exact recorded strings.
  - **Slice 2/3 live:** ran the deployed `materialize_llm_usage_from_audit` against the real pools + seeded rates → 62 `usage_events` rows from real `llm_requests`: correct token quantities, `user_id`/`ref_kind`/`ref_id` attribution (incl. **session `ref_kind=thread` rows**, proving the `metadata.*_tokens` path on real data), and `record_events` pricing exact (`quantity × rate = cost_usd`: minimax 210393 prompt × $3e-7 = $0.063118). Test rows + a back-dated proof rate cleaned up afterward (ledger restored: 6 rates, 0 audit `usage_events`).
  - **Confirmed correct-by-design:** effective-dating is not retroactive — a rate stamped `effective_from=<sync time>` does **not** price rows older than it (my backfill of pre-sync rows stayed unpriced until I added a back-dated rate). Forward organic traffic prices automatically (its `ts` > the rate's `effective_from`). The live loop is deployed + anchored forward; observing it meter a *fresh organic* row is the only step not directly watched (cluster was idle) — mechanically identical to the in-pod run it calls.
  - **Residual / follow-ups:** forward-only anchor skips the homelab disable-gap (backfill = a one-off lower anchor); no `pricing_id` UI yet (DB-only).

- **Codex-proxy / subscription models — DECISION (2026-07-01): treat as regular priced models (option B), no special quota dimension.** All OpenAI models on the cluster route through the codex proxy (`eceasy/cli-proxy-api` = **CLIProxyAPI**) backed by a ChatGPT **Codex subscription** (no per-token OpenAI key), so their "cost" is flat-rate, not $/token. Options: **A** = leave unpriced ($0); **B** = price from OpenRouter like any model (cockpit cost = *estimate at API rates*, not actually billed). **Chose B:** (1) zero special-casing — the existing `params_json.pricing_id` mechanism; (2) **A silently breaks billing for the real product case** — a customer on their own OpenAI key *is* billed per-token, and pricing lives on the model, not the auth method; (3) under a subscription the estimate is still useful (API-equivalent value of the sub). **Rejected** a dedicated Codex-quota poller (no value: companies use real keys not shared subs; homelabbers' real variable cost = embeddings + the fixed sub). `pricing_id` set on homelab: `gpt-5.5→openai/gpt-5.5`, `gpt-5.3-codex-spark→openai/gpt-5.1-codex` (est), `gpt-5.4-mini→openai/gpt-5-mini` (est), `gemini-3.5-flash→google/gemini-3.5-flash`, `MiniMax-M3→minimax/minimax-m3` — pre-staged (inert until the metering code deploys to homelab). **Codex subscription quota — capacity bars BUILT (2026-07-01, uncommitted `develop`, k3d-verified).** Distinct from cost/metering: this is an *operational capacity display* in the Codex Proxy admin panel (Settings → Codex), NOT a `usage_events` dimension — so the "no cost/quota metering dimension" decision above still stands. New admin endpoint `GET /api/codex/usage` (`main.py`, beside `/api/codex/status`) fetches the ChatGPT 5-hour + weekly rate-limit windows: management-API `auth-files` → download the active account's token → `GET https://chatgpt.com/backend-api/wham/usage` (Bearer + `ChatGPT-Account-Id` from the id_token JWT). The OAuth token stays server-side — only `used_percent`/`reset_after_seconds`/`plan_type`/`limit_reached` cross to the UI. Non-fatal → `{available:false}` (proxy down / no account / no chatgpt.com egress). Cockpit renders two Claude-style bars (session 5h + weekly) in `settings.component.ts`, colour-banded ok/warn/crit. **Live path proven on k3d** (real `wham/usage` 200: `plan_type:prolite`, `primary_window` 18000s, `secondary_window` 604800s); normalization unit-tested (`tests/test_codex_usage.py`); cockpit build clean. **Deploy note:** needs orchestrator→`chatgpt.com` egress (present on k3d; verify homelab netpol). Data source is ChatGPT's private/unofficial `wham/usage` (subject to change; `x-codex-primary/secondary-used-percent` response headers are an alternative).
  - **⚠️ KNOWN LIMITATION (verified 2026-07-01): the bars do NOT track SRW's proxy usage.** Fired 22 real completions through the proxy (14× gpt-5.5 + 8× gpt-5.3-codex-spark, all HTTP 200) → every `wham/usage` window stayed **0%** and `credits.approx_local_messages` stayed `[0,0]`. CLIProxyAPI's OAuth-proxied `/v1/responses` requests meter as **"extra usage"**, which ChatGPT does not count in the plan windows (CLIProxyAPI #2599) — only genuine Codex-CLI/app sessions increment them. So the bars are *accurate to ChatGPT's number* but read ~0% no matter how hard SRW uses the subscription. **Decision (user):** keep the bars + add an in-UI **disclaimer** (`settings.codex.usage.disclaimer`, en+de) stating the proxy doesn't expose usage and these are "extra usage" windows that don't reflect proxy traffic at this time. The proxy's own `recent_requests` (per-account success/fail per 10-min window) is the only signal that DOES reflect proxy traffic — a possible future pivot (throughput, not "% of limit"; the proxy has no per-client quota, #3467).
- **P2 — Confirm cloud-model routing via OpenRouter** (OpenAI-wire) and that per-model reasoning params are handled in our catalog (`model_config_matrix` / DB catalog), since LiteLLM's partial reasoning mapping goes away. Validate reasoning_content/tokens still captured (codex already independent; check gemma/minimax paths).
- **P3 — Cleanup. ✅ DONE (2026-07-08).** Deleted the Helm proxy/DB templates, chart/overlay values, chart config/env, the orchestrator gateway sync/quota startup tasks, the app-side gateway service, gateway routing/health/quota branches, and gateway-only tests.
- **Deferred (own issues later):** Keycloak short-lived JWTs for tenant creds; `agent_llm_factory_collapse.md` (single OpenAI factory — the natural home for in-process metering); egress NetworkPolicy review (direct routing means agent pods need outbound to providers/OpenRouter — the gateway had been a single egress point).

## When we'd bring a gateway back ("at least for now")

The decision is "no proxy concept," not "never." **One condition flips it:** if the **B2B product** needs *gateway-grade org governance* — self-serve customer API keys, SSO/RBAC/audit trails on LLM access, per-customer budget dashboards — that's cheaper to **buy than build**, and we'd adopt a **reliable** gateway (**Kong** = most battle-tested, or **agentgateway** = lightest, both per `router_comparison.md`) — **never LiteLLM again**. Absent a concrete enterprise-governance requirement, in-process wins.

## Open questions

- **Price-map source of truth** — **RESOLVED:** the effective-dated `usage_rates` table (per-token, `category='llm'`), seeded from OpenRouter (Slice 1, built). Remaining sub-decision: model→OpenRouter-id mapping via `params_json.pricing_id` (rec) vs a dedicated column.
- **reasoning_tokens on non-codex paths** — does `llm_requests` capture `completion_tokens_details.reasoning_tokens` for gemma/minimax, or only codex (which already surfaces it)? Affects the REASONING chip in the usage bar.
- **OpenRouter margin vs BYOK** — accept OpenRouter's markup on cloud models for the unification, or route BYOK through it? (Self-hosted models never go through OpenRouter.)
- **litellmdb / chart-template deletion timing** — keep the DB until in-process metering has soaked; then remove.
- **Egress posture** — direct routing changes the outbound surface from one gateway pod to all agent pods; revisit the agent-egress NetworkPolicy.
