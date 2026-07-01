# Remove the LiteLLM proxy — and the gateway/proxy concept entirely (for now)

**Date:** 2026-07-01
**Status:** **DECISION MADE.** We are removing the self-hosted LiteLLM proxy and, with it, the "route all LLM traffic through a self-hosted gateway" concept — **for now** (see § "When we'd bring a gateway back"). Step 0 (disable on homelab) is **staged, uncommitted on `develop`** in `deployment/values-experimental.yaml` (`litellm.enabled: true→false`, `routedProviders: []`); `databases.litellm.enabled` kept `true` so the litellmdb (registered models + spend history) survives a re-enable. This doc supersedes the "stabilize LiteLLM" and "swap to another gateway (Bifrost/Kong/agentgateway)" directions explored on the way here.
**Progress (2026-07-01):** P0 **done** (litellm disabled on homelab; routing direct). P1 (restore monitoring in-process) **in progress** — Slice 1 (OpenRouter→`usage_rates` pricing) **built + unit-tested**; Slices 2–3 (audit materializer + re-point poll) pending. See § Plan.
**Severity:** **High (reliability).** The gateway OOM-crashloops and takes down agent/loop jobs; it is the current blocker to the continuously-running self-improvement loop (the north star). Removal unblocks it immediately.
**Decision owners:** platform (Niklas). This is an architecture-direction decision, not just a bugfix.
**Component / blast radius:**
- Gateway deployment — `helm/templates/litellm/{deployment,service,config-configmap}.yaml`; toggled by `.Values.litellm.enabled`; `LITELLM_BASE_URL` (helm configmap `:49`) unset when disabled.
- Dispatch routing — `orchestrator/main.py` `_inject_dispatch_credentials` (`:1651`), `_gateway_routing_target(_scoped)` (`:1246`/`:1252-1300`); when the gateway is absent/unhealthy these already fall through to **direct** provider credentials (`gateway_is_healthy() → None`).
- Metering pipeline — `orchestrator/services/litellm_gateway.py` (`get_spend_logs` `:1064-1077`, `materialize_llm_usage` `:1144-1223`) polls the gateway's `/spend/logs` → writes `category='llm'` rows into the `usage_events` ledger (audit DB); Cockpit "By Model" view (`GET /api/usage`) reads *that*. **This is the one thing that goes dark on removal.**
- Independent token source that survives — MongoDB `llm_requests` audit (the agent records per-request `usage` token counts regardless of routing; e.g. job `b17d63bc` shows `tokens=[16536/267/16803]`).
**Related:**
- `docs/features/router_comparison.md` — the 11-alternative evaluation that concluded none is a clean drop-in (produced 2026-07-01, same investigation).
- `docs/features/route_all_models_through_litellm_gateway.md` — the roadmap this decision **reverses**.
- `docs/features/agent_llm_factory_collapse.md` — aligned; the single OpenAI-compatible factory is the vehicle for in-process metering.
- `docs/features/credential_broker.md` — the temporal-key idea (deferred; → Keycloak JWTs).
- `docs/features/usage_dashboard.md`, `docs/features/observability_and_quotas.md` — the usage view + deferred per-job cost attribution.
- `docs/done/litellm_gateway_drops_gpt_codex_reasoning_capture.md` — why codex already bypasses the gateway.
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
  - **Slice 2 — audit materializer** (`materialize_llm_usage_from_audit`): poll new `llm_requests` since a watermark → pull `{prompt, completion, reasoning}` tokens from the `metrics` JSONB → resolve `job_id → user/project` → emit the cost-free token `UsageEvent`s (dedupe `source='agent'`, `source_id=llm_requests.id`). *Pin first:* the exact JSONB key for tokens in `llm_requests.metrics` vs `.response`.
  - **Slice 3 — re-point + backfill**: swap the `main.py:1581` poll from `materialize_llm_usage` (litellm) to the audit materializer; backfill **forward of the disable timestamp only** (avoid double-count with residual `litellm`-source rows).
  - **Wiring decisions (recommended, pending confirm):** (a) model→OpenRouter-id mapping via the catalog's existing **`params_json.pricing_id`** (no migration; `""` = explicitly unpriced/free for self-hosted) over a new column; (b) run `sync_llm_rates` from the **repurposed `litellm_sync_loop` slot** (slow periodic, 6–24 h + on startup) — prices move slowly and the sync is cheap + change-only.
- **P2 — Confirm cloud-model routing via OpenRouter** (OpenAI-wire) and that per-model reasoning params are handled in our catalog (`model_config_matrix` / DB catalog), since LiteLLM's partial reasoning mapping goes away. Validate reasoning_content/tokens still captured (codex already independent; check gemma/minimax paths).
- **P3 — Cleanup.** Remove the stale `main.py:1232` docstring (describes per-job keys that never shipped). After P1 lands + a soak: delete the `helm/templates/litellm/*` templates + `LITELLM_BASE_URL`, and decide whether to drop `litellmdb` (keep until we're sure we won't re-enable). Simplify the dispatch routing to drop the gateway branch.
- **Deferred (own issues later):** Keycloak short-lived JWTs for tenant creds; `agent_llm_factory_collapse.md` (single OpenAI factory — the natural home for in-process metering); egress NetworkPolicy review (direct routing means agent pods need outbound to providers/OpenRouter — the gateway had been a single egress point).

## When we'd bring a gateway back ("at least for now")

The decision is "no proxy concept," not "never." **One condition flips it:** if the **B2B product** needs *gateway-grade org governance* — self-serve customer API keys, SSO/RBAC/audit trails on LLM access, per-customer budget dashboards — that's cheaper to **buy than build**, and we'd adopt a **reliable** gateway (**Kong** = most battle-tested, or **agentgateway** = lightest, both per `router_comparison.md`) — **never LiteLLM again**. Absent a concrete enterprise-governance requirement, in-process wins.

## Open questions

- **Price-map source of truth** — **RESOLVED:** the effective-dated `usage_rates` table (per-token, `category='llm'`), seeded from OpenRouter (Slice 1, built). Remaining sub-decision: model→OpenRouter-id mapping via `params_json.pricing_id` (rec) vs a dedicated column.
- **reasoning_tokens on non-codex paths** — does `llm_requests` capture `completion_tokens_details.reasoning_tokens` for gemma/minimax, or only codex (which already surfaces it)? Affects the REASONING chip in the usage bar.
- **OpenRouter margin vs BYOK** — accept OpenRouter's markup on cloud models for the unification, or route BYOK through it? (Self-hosted models never go through OpenRouter.)
- **litellmdb / chart-template deletion timing** — keep the DB until in-process metering has soaked; then remove.
- **Egress posture** — direct routing changes the outbound surface from one gateway pod to all agent pods; revisit the agent-egress NetworkPolicy.
