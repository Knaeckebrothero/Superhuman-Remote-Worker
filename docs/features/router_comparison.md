# LLM Router / Gateway Comparison — LiteLLM and Alternatives

**Status:** Decision doc / evaluation · **Date:** 2026-07-01 · **Owner:** platform
> **Outcome (2026-07-01):** the decision landed on **none of these** — we're **removing the gateway/proxy concept entirely** and metering in-process (`docs/issues/remove_litellm_proxy_and_gateway_concept.md`). This doc remains the **evaluation record + the shortlist to revisit** *if* enterprise-grade org governance (SSO/RBAC/audit/self-serve customer keys) ever forces a gateway back — in which case **Kong** (most battle-tested) or **agentgateway** (lightest), never LiteLLM.

**Trigger:** the in-cluster LiteLLM gateway (`srw-litellm`, v1.90.0, DB mode) OOM-crashloops on the homelab cluster (Python memory leak in its DB-mode background jobs — it exceeds its 2 Gi limit and is OOMKilled *even at idle*, ~110 restarts/15 h), which takes down agent/loop jobs with `Connection error.` See `project_litellm_oom_crashloop_blocks_loop` memory + the litellm limitation docs.

> **This is not a feature gap.** A prior evaluation (`docs/issues/system_provider_models_bypass_gateway_unmetered.md:143`) explicitly concluded *"no replacement gateway warranted — LiteLLM does what SRW needs."* We are re-opening the question on **reliability** grounds, plus a long tail of documented LiteLLM sharp edges. So the bar for a replacement is: **do the same job on a runtime that doesn't fall over**, and ideally clear the deferred features we couldn't get from LiteLLM.

---

## 1. TL;DR / Recommendation

1. **There is no clean drop-in replacement.** LiteLLM's DB-minted virtual keys + queryable per-key spend ledger + daily-$ budget freeze is an *application* feature set that none of the reliable alternatives fully reproduces. Every candidate gives you a **reliable runtime + OpenAI-compatible routing + per-tenant token/cost telemetry** that you *finish* into a metering layer. We already own that layer (the `usage_events` ledger + Cockpit usage view + orchestrator-side quota), so for most candidates this is **integration work, not greenfield.**

2. **The temporal-key requirement is real but was mis-framed** (see §4). LiteLLM *natively* supports runtime TTL keys (`/key/generate` + `duration`); our problem was reliability + an architectural pivot away from per-job keys, not a missing feature. And the *durable* answer is **Keycloak-issued short-lived JWTs validated at the gateway** (we already run Keycloak) — which most candidates support and which is arguably better than any gateway-minted key. So temporal keys should **inform**, not **decide**, the choice.

3. **The real discriminators are: reliability, Responses-API + reasoning fidelity (our codex/gpt-5.x pain point), metering-integration cost, and maturity/license.**

4. **Recommended path:**
   - **Now:** stabilize LiteLLM (raise mem, `disable_reset_budget`, worker recycling / timed restart) to unblock the loop — it's down today. Cheap, reversible, and LiteLLM already has the temporal-key feature.
   - **Spike two, in order:** **(1) agentgateway** — Rust, single-container, the *closest operational match* to LiteLLM with OSS virtual keys + per-request USD cost + custom pricing; **(2) Kong AI Gateway** — the *most battle-tested* runtime with OSS TTL keys + custom pricing + Responses, if agentgateway's youth is disqualifying.
   - **Seriously weigh option (c): collapse the gateway into the orchestrator's own LLM factory** (`docs/features/agent_llm_factory_collapse.md`). Since we're building metering glue *either way* and already do quota + `usage_events` ourselves, dropping the external gateway may be the lowest-total-cost, max-control path.
   - **Adopt Keycloak short-lived JWTs** for tenant credentials regardless of which gateway wins.

**Ruled out:** Helicone AI Gateway (feature-frozen, Mintlify-acquired, GPL/Apache license conflict), LangDB/vLLora (ELv2 source-available — *not* OSS — keys delegated to cloud, pivoted to an agent-debugging tool), One-API (unmaintained since Feb 2025, no Responses API).

---

## 2. Our requirements (the evaluation rubric)

Derived from `orchestrator/services/litellm_gateway.py` + the routing/monitoring docs. **Load-bearing** = something breaks in Cockpit/agent if missing.

**Correction to prior mental model:** Cockpit does **not** read the LiteLLM DB — the orchestrator polls the gateway's `/spend/logs` and materializes rows into SRW's own `usage_events` ledger (in `srw-auditdb`); Cockpit reads that. And **only chat + codex-Responses route through the gateway** today — embeddings/whisper/TTS go direct via env vars. So the coupling is **API-adapter-level** (the ~1200-line `litellm_gateway.py`), swappable without touching Cockpit.

| # | Requirement | Load-bearing? |
|---|---|---|
| R1 | OpenAI-compatible `/v1/chat/completions` as the single normalized inference path | ✅ yes |
| R2 | **Codex `/responses`↔chat bridge + `reasoning_content` (JSON body + SSE `delta.reasoning_content`) + native `reasoning_tokens` + `include_usage`** for gpt-5.x/o-series | ✅ yes (the 40%→0% regression we fixed) |
| R3 | Dynamic model registration at runtime via admin API, persisted across restarts (we ship `model_list: []`, register the catalog at runtime) | ✅ yes |
| R4 | Scoped virtual keys per (user, project) with tenant identity readable in the usage log (not just a hash) | ✅ yes |
| R5 | Per-request spend pollable → materialized into `usage_events` | ✅ yes |
| R6 | Authoritative per-request cost incl. **custom pricing for self-hosted/unpriced models** (MiniMax, gemma, vLLM) | ✅ yes |
| R7 | At-rest encryption of stored upstream provider keys | ✅ yes |
| R8 | `drop_params` / provider-param passthrough; native route translation (OpenAI, OpenRouter, **native Gemini**, self-hosted) | ✅ yes |
| R9 | Split health probes (liveness/readiness), never a `/health` that pings every model | ✅ yes |
| R10 | **Reliable runtime / bounded memory** (the reason we're leaving) | ✅ yes — the whole point |
| R11 | Rate-limit RPM/TPM per key/team + usage read for daily quota (quota itself enforced **orchestrator-side**) | 🟡 ships inert; read is load-bearing |
| R12 | Multi-replica correctness (shared counters) | 🟡 future (single-replica today) |
| R13 | Permissive OSS license, self-hostable, no core features paywalled | ✅ yes (source-available/B2B product) |

**Deferred / "later" features we want** (from `credential_broker.md`, `agent_llm_factory_collapse.md`, `observability_and_quotas.md`, `usage_dashboard.md`, `high_availability_setup.md`):
- **Temporal / short-lived tenant credentials** (see §4).
- **Per-job LLM cost attribution** — tag each request with `job_id` (deferred in 5 places; gateway never sees `job_id` today).
- **Single measurement plane** — route embeddings/whisper/TTS/rerank through the gateway too (today they bypass it).
- **Gateway HA / multi-replica + Redis/Valkey** (current single-replica gateway + its Postgres are SPOFs for all inference).
- **Push-callback ledger feed** (replace `/spend/logs` polling).
- **Factory collapse** — retire the agent's provider-specific LLM factories, leaving one OpenAI-compatible factory + a gateway-down bypass.

**Explicitly NOT requirements** (so we don't over-weight frameworks that lead with them): guardrails / PII redaction / moderation / semantic caching are **not** intended SRW gateway features today.

---

## 3. Master comparison matrix

✅ = yes / native / OSS · 🟡 = partial / DIY / caveated · ❌ = no / paid-gated / missing.
Scores are for the **self-hostable OSS edition** unless noted.

| Framework | R10 Runtime / OOM-class fix | R2 Responses + reasoning | Temporal keys (native mint+TTL) | R5 Per-req spend export | R6 Custom pricing | R3 Dyn model reg (persisted) | R11/R12 RL + budgets (multi-replica) | R13 License | Maturity / risk |
|---|---|---|---|---|---|---|---|---|---|
| **LiteLLM** (baseline, Python) | ❌ **OOM leak** | ✅ (`use_responses_api`) | ✅ native (`duration`) | ✅ `/spend/logs` | 🟡 dollar-only | ✅ `/model/new`+DB | 🟡 needs Redis to 429; $-budget only | MIT | ✅ battle-tested but leak-prone |
| **agentgateway** (Rust, Solo.io/AAIF) | ✅ Rust (~22 MB vendor bench) | 🟡 routed; reasoning-tokens undoc | ❌ static keys (JWT `exp` only) | 🟡 USD cost in logs/OTel/Prom; no query API | ✅ catalog + overrides | 🟡 file/CRD/UI | 🟡 token RL; $-budget not enforced | Apache-2.0 | 🟡 v1.0 Mar'26, no named adopters |
| **Kong AI Gateway** (Lua/OpenResty) | ✅ C/LuaJIT, mature 2015 | ✅ 3.11+ / preserve mode | ✅ `key-auth` `ttl` (Postgres mode) | 🟡 log-plugin push; no OSS poll API | ✅ per-token OSS | ✅ Admin API + Postgres | 🟡 RPM+quota OSS; **TPM/cost = Enterprise** | Apache-2.0 (advanced plugins Enterprise) | ✅ 43.7k★, since 2015 |
| **Bifrost** (Go, Maxim) | ✅ Go (log-bloat bug #1665) | 🟡 bridge; gpt-5.x tokens unverified | ❌ no TTL (maintainer rejected) | ✅ `GET /api/logs` pull + attribution | ✅ API, 6 scopes | ✅ `/api/providers`+SQLite/PG | ❌ **multi-replica broken in OSS** (Enterprise) | Apache-2.0 (open-core) | 🟡 15 mo, 6.2k★, high churn |
| **TensorZero** (Rust) | ✅ Rust | ❌ **no `/v1/responses`** | 🟡 expiry, but CLI/UI mint only | ✅ ClickHouse + `list_inferences` | ✅ `cost_per_million` | ❌ static TOML | ✅ $/token/req; needs PG or Valkey | Apache-2.0 (ungated) | 🟡 11.7k★ active; pin ≥2026.6.0 |
| **Higress** (Envoy/Go, Alibaba) | ✅ Envoy/Go | 🟡 passthrough; reasoning-drop bug #3812 | ❌ static consumers (JWT `exp`) | 🟡 logs/Prom, tokens only | ❌ **tokens only, no cost** | ✅ console+CRD/Nacos hot | ✅ TPM/TPD + quota; needs Redis | Apache-2.0 | ✅ 8.7k★, CNCF Sandbox, big adopters |
| **Envoy AI Gateway** (C++/Go) | ✅ Envoy C++ | ✅ full + reasoning tokens (best infra) | ❌ JWT `exp` only | 🟡 access logs; CEL cost; no API | 🟡 CEL formula (DIY) | 🟡 CRD/GitOps | 🟡 token RL, Redis-backed | Apache-2.0 | 🟡 1.8k★, v1.0 GA Jun'26, CNCF |
| **New-API** (Go) | ✅ Go (one OOM #5698) | ✅ Responses + reasoning stream | ✅ TTL via API, server-enforced | ✅ usage export | ✅ | ✅ channels via API | ✅ quota/budgets OSS; Redis HA | ⚠️ **AGPL-3.0** | 🟡 40.7k★ active, pre-1.0, CN-market |
| **One-API** (Go) | ✅ Go | ❌ no Responses | ✅ TTL keys | ✅ | 🟡 | ✅ channels | 🟡 needs Redis | MIT | ❌ **unmaintained since Feb'25** |
| **Portkey Gateway** (TS/Node) | 🟡 Node, stateless (no RSS data) | ✅ native providers (drop bug #1672) | ❌ OSS (paid control-plane) | ❌ OSS (OTel push experimental) | 🟡 compute yes, manage paid | ❌ OSS stateless | ❌ OSS (paid control-plane) | MIT (heavily gated) | 🟡 12.3k★, **acquired by Palo Alto Networks** |
| **Helicone AI GW** (Rust) | ✅ Rust | 🟡 cloud only, self-host unverified | ❌ (control-plane) | ❌ OSS (OTel push) | 🟡 rebuild+recompile | 🟡 in-mem + Redis | (n/a) | ⚠️ **GPL-3.0/Apache conflict** | ❌ **frozen beta, Mintlify-acquired** |
| **LangDB / vLLora** (Rust) | ✅ Rust | 🟡 | ❌ cloud-delegated | 🟡 | 🟡 | 🟡 | 🟡 in-mem (no HA) | ⚠️ **ELv2 (not OSS)** | ❌ pivoted to debug tool, pre-1.0 |

**Reading the matrix:** the gateways that clear **both** temporal-keys *and* Responses-API in their free edition are the "boring" ones — **Kong, New-API** (+ LiteLLM) — *not* the shiny VC-backed entrants (Bifrost/TensorZero/Portkey-OSS/Helicone). And the gateways strongest on **reliability + cost metering** (agentgateway, Kong, Bifrost) each miss a *different* one of our load-bearing items. This is why no option is a clean win.

---

## 4. The temporal / ephemeral API-key requirement (deep-dive)

This was flagged as the top priority ("didn't work with LiteLLM, so we went back to one key for all requests"). The research reframes it:

**It's two layers, and only one was ever a LiteLLM failure.**
- **Per-session ephemeral keys** (the `credential_broker.md` idea) were **never built** — status *"idea, not building."* Our own doc argues their security value is **bounded**: for a live, prompt-injectable agent *"access is the asset,"* so a broker defends against **key exfiltration / cross-tenant reuse**, not in-session abuse.
- **Per-job keys** (minted at dispatch, revoked on completion — the original Slice-2 design) were **dropped for two reasons**: (a) *architectural* — enforcement lives on shared team/user objects and `--loop` pod-reuse made per-job keys pointless (they add attribution granularity but no enforcement); (b) *LiteLLM mechanics* — rpm doesn't 429 without Redis, the master key bypasses limits, DB-mode per-model limits enforce unreliably (#10052), the `team_ids`-plural footgun, etc.

**Current state:** deterministic **permanent** keys (`HMAC(master_key, label)`), scoped per (user, project) for **attribution**, with enforcement shipped **inert**. So "one shared key" is true in *effect* (all derived from one master, permanent, inert), though technically per-tenant. (There's even a stale `main.py:1232` docstring still describing per-job keys that never shipped — clean this up.)

**What the alternatives actually offer on temporal keys:**

| Gateway | Native gateway-minted TTL keys | Notes |
|---|---|---|
| **LiteLLM** | ✅ `POST /key/generate` `duration` `"30m"/"30d"`, server-enforced | We *have* this; couldn't operationalize enforcement reliably |
| **Kong** | ✅ `key-auth` `ttl` (seconds), server-side expiry, runtime Admin API | **Postgres mode only** (DB-less Admin API is read-only) |
| **New-API** | ✅ token TTL via API, server-enforced | AGPL, pre-1.0 |
| **One-API** | ✅ token TTL | unmaintained |
| **TensorZero** | 🟡 server-side `expires_at`, but mint via **CLI/UI only** (no REST) | you'd script the CLI / write to Postgres |
| **Bifrost** | ❌ maintainer explicitly rejected short-lived keys | static dual-key rotation only |
| **Portkey-OSS / Helicone / LangDB** | ❌ | keys live in a paid/cloud control plane |
| **Envoy / Higress / agentgateway** | ❌ *mint* — but ✅ **validate external JWT `exp`** | see below |

**The durable answer — Keycloak short-lived JWTs.** We already run Keycloak. The soundest architecture on *any* modern gateway is: **Keycloak mints short-lived, per-tenant JWTs → the gateway validates `exp` server-side and attributes usage to the `sub`/tenant claim.** This gives real, centrally-revocable expiry without the gateway being a key-minting/secret-holding SPOF — arguably *better* than LiteLLM's minted keys, and it's the only "temporal" path the infra-grade gateways (Envoy/Higress/agentgateway) offer. **Recommendation: adopt this pattern regardless of gateway choice**, and treat gateway-side TTL minting as a nice-to-have, not a hard gate.

---

## 5. Per-framework notes (advantages / disadvantages)

### agentgateway — *closest operational match; youngest*
- **Pros:** Rust, single standalone container (no K8s/Redis/DB needed for basic use) — the closest ops model to one LiteLLM pod; **OSS virtual keys + per-request USD cost + custom pricing catalog** (handles MiniMax/gemma/self-hosted); native Gemini + arbitrary self-hosted; built-in UI + OTel/Prometheus/Langfuse; Apache-2.0, core un-paywalled; vendor benchmark ~22 MB vs LiteLLM ~11.8 GB.
- **Cons:** **static keys (no TTL mint)**; **no queryable spend-ledger API** (build from telemetry); **no native $-budget** enforcement (token/request only; global quotas need an external rate-limit service); **reasoning-token capture undocumented** — the exact soft spot we were burned on; v1.0 only March 2026, **no named production adopters** (maturity risk).
- **Migration:** Low–Medium. Drop-in container + cost catalog in an afternoon; real work is Keycloak-JWT tenancy + building the spend ledger from its OTel/log output + validating codex reasoning.

### Kong AI Gateway — *most battle-tested; app-layer is partly Enterprise*
- **Pros:** mature C/LuaJIT/nginx runtime (since 2015) — the direct antidote to the Python OOM; **OSS TTL `key-auth` keys**, **OSS custom per-token pricing**, per-request token+cost+consumer log records, Responses API (3.11+) + preserve-mode passthrough, native Gemini, runtime persisted config; Apache-2.0.
- **Cons:** **token/TPM rate-limiting + per-key cost budgets are Enterprise** (`ai-rate-limiting-advanced`); **unified multi-provider fallback is Enterprise** (`ai-proxy-advanced`); **no pollable spend API in OSS** (Konnect is paid — rebuild as a log-sink into `usage_events`); TTL keys need Postgres mode + Redis for correct multi-replica limits; reasoning-*content* in translation mode needs validation (`preserve` recommended).
- **Migration:** Low for a single-provider OSS proxy; Moderate–High for full LiteLLM-DB parity (budgets + spend store are Enterprise/rebuild).

### Bifrost — *strong metering, but two hard misses*
- **Pros:** Go; **genuine `GET /api/logs` pull API with tenant attribution**; custom pricing (6 scopes); runtime provider registration persisted to SQLite/Postgres; Responses bridging; no-Redis single-node limits; Apache-2.0 core.
- **Cons:** **no temporal/TTL keys, and the maintainer rejected them**; **multi-replica governance is broken in OSS** — per-process counters leak caps N×, and correct cluster-wide counters are **paid Enterprise-only** (maintainer on record refusing Redis-backed budgeting in OSS); gpt-5.x reasoning-token capture unverified; young (15 mo), open Postgres log-bloat bug (#1665); memory is *bounded* but not "tiny" (1.3–3.3 GB at 5k RPS by its own docs).
- **Migration:** Low–moderate (dedicated `/litellm` compat path), but no `config.yaml` auto-import.

### TensorZero — *fully-OSS Rust, but blocked by no-Responses*
- **Pros:** Apache-2.0, **fully ungated**; real server-side key expiry + $/token/request budgets + custom pricing; native Gemini/OpenRouter; **ClickHouse optional** (Postgres-only viable); Rust; active cadence; vendor benchmark explicitly beats LiteLLM (which "fails at 1k QPS").
- **Cons:** **no `/v1/responses` endpoint** (Chat-Completions only → our codex-Responses agents can't just repoint; full reasoning fidelity needs the *native* API); **temporal keys mint via CLI/UI only** (no REST); **no dynamic model registration** (static TOML, edit-and-restart); tenancy is tag-based (no first-class user/project keys); no published RSS, no named adopters.

### Higress — *most production-proven, but no cost tracking*
- **Pros:** most mature/adopted (Alibaba, CNCF Sandbox; Ant/Ctrip/DJI/Kuaishou/etc.); **broadest providers incl. native Gemini and native MiniMax** + self-hosted `openaiCustomUrl`; hot runtime config; TPM/TPD rate-limits + per-consumer token quotas with a runtime admin API; console + OTel; Apache-2.0.
- **Cons:** **zero cost tracking (tokens only)** — the biggest miss vs our metering; **no temporal keys**; per-request records only in logs/Prom with opt-in identity, no export API; **open Responses/reasoning-capture bugs (#3812)** mirroring the LiteLLM problem we're escaping; Redis hard dependency; production HA = Helm + Envoy/Istio stack.

### Envoy AI Gateway — *best reasoning fidelity, heaviest ops, no app layer*
- **Pros:** best **Responses-API + reasoning-token accounting** of the infra tier (directly relevant to our pipeline); CNCF/Envoy pedigree (Tetrate + Bloomberg); Envoy data-plane reliability; OTel/OpenInference-native; CEL cost hook allows custom pricing math; Apache-2.0.
- **Cons:** **no app-level virtual keys, no monetary cost/spend layer** — it's an *infra* gateway; per-tenant metering is DIY header→attribute plumbing; **no temporal key minting** (JWT `exp` only); **heaviest ops** (Envoy Gateway + Kubernetes Gateway API + CRDs); OpenRouter not first-class; Gemini via Vertex.
- **Migration:** High — you stand up a control plane and rebuild the entire keys+cost+ledger layer.

### New-API — *full-featured OSS hit, but AGPL + pre-1.0*
- **Pros:** clears **TTL keys + Responses + reasoning streaming + usage export + custom pricing** — all OSS; Go runtime; very active (~40.7k★, ~3–4 day cadence); channel/token/quota dashboard.
- **Cons:** **AGPL-3.0** (network-copyleft — needs legal review for our FSL/B2B-deploy product); pre-1.0 RC; primarily China-market (English-docs gaps); one open workload OOM (#5698) on the `/v1/responses` path.

### Ruled out
- **One-API** (MIT, Go): has TTL keys + usage export but **unmaintained since Feb 2025** and **no Responses API**. New-API is its living fork.
- **Helicone AI Gateway** (Rust): **feature-frozen public beta**, **acquired by Mintlify** (maintenance mode, ~11-month release gap), keys/metering require the Helicone cloud control plane, and a **GPL-3.0 (committed LICENSE) vs Apache-2.0 (README)** conflict. Do not build on it.
- **LangDB / vLLora** (Rust): **ELv2 — source-available, not OSS**; key provisioning delegated to LangDB cloud; rebranded to an agent-debugging tool; pre-1.0.

---

## 6. The three strategic options

### (a) Stabilize LiteLLM — cheapest, keeps the sharp-edge tail
Raise mem 2 Gi→4 Gi (litellm's own recommended floor is 4 Gi/worker), `general_settings.disable_reset_budget: true` (we enforce quota orchestrator-side), `disable_error_logs: true`, `LITELLM_LOG=ERROR`, worker recycling (`--max_requests_before_restart`) + a time-based `rollout restart` backstop for the idle leak. Also worth trying: upgrade past the aiohttp-pool leak fix (PR #17388). **Keeps** all our feature work and the native TTL-key capability; **inherits** LiteLLM's whole documented sharp-edge tail and the underlying leak.

### (b) Swap the gateway — solves reliability, costs an adapter rewrite + re-validation
Rewrite the ~1200-line `litellm_gateway.py` adapter against the new gateway; re-point the `/spend/logs` poll at its equivalent; re-validate codex reasoning capture. Best candidates: **agentgateway** (lightest ops) or **Kong** (most reliable). **Solves** the OOM structurally; **costs** integration + codex re-validation + adopting Keycloak-JWT tenancy.

### (c) Collapse the gateway into the orchestrator LLM factory — max control, biggest build
Per `docs/features/agent_llm_factory_collapse.md`, meter in-process and drop the external gateway. We're **already ~70% there**: non-chat traffic routes direct, quota is orchestrator-side, `usage_events` is our ledger, and a gateway-down→direct fallback exists. **Because every swap option (b) requires building metering glue anyway**, collapsing may be the lowest-total-cost, no-external-SPOF, no-third-party-leak path. **Loses** the central virtual-key isolation + choke point (mitigate with Keycloak JWTs + the per-job credential injection we already do).

---

## 7. Recommendation + spike plan

1. **Immediately:** stabilize LiteLLM (option a) to unblock the loop.
2. **Spike agentgateway** (2–3 days) with hard acceptance criteria:
   - **(i)** codex/gpt-5.x call through it surfaces `reasoning_content` **and** `reasoning_tokens` on a *streamed* response (its softest spot + our known regression). If this fails, agentgateway is out.
   - **(ii)** per-request cost + tenant identity land in our `usage_events` from its OTel/log output.
   - **(iii)** Keycloak short-lived JWT validated at the gateway, usage attributed to the tenant claim.
3. **If agentgateway fails (i) or maturity is disqualifying → spike Kong** with the same criteria (accepting Enterprise/rebuild for token budgets + spend API).
4. **In parallel, cost option (c)** — estimate the factory-collapse metering work; if it's ≤ the swap+integration cost, prefer it (no external dependency, no leak, max control).
5. **Adopt Keycloak short-lived JWTs** for tenant credentials regardless — this is the real resolution of the temporal-key requirement.

**Decision owners to weigh:** AGPL (New-API) and ELv2 (LangDB) are disqualifying-or-not depending on our source-available/FSL posture — legal call. Maturity risk (agentgateway youth vs Kong's Enterprise gating vs Higress's no-cost-tracking) is the core trade.

---

## 8. Sources

Primary sources per framework (docs, GitHub, release notes, benchmarks) are captured in the research transcripts. Highest-value:
- **LiteLLM:** docs.litellm.ai `/proxy/virtual_keys`, `/troubleshoot/memory_issues`, `/proxy/prod`; issues #12685, #15128, #13210, #15933; PR #17388.
- **agentgateway:** agentgateway.dev/docs (llm/virtual-keys, cost-tracking, providers/custom); benchmark blog 2026-06-26 (vs LiteLLM); aaif.io/projects/agentgateway.
- **Kong:** developer.konghq.com (ai-proxy, ai-rate-limiting-advanced, key-auth ttl, prometheus); Kong-authored gateway benchmark.
- **Bifrost:** docs.getbifrost.ai (governance/virtual-keys, budget-and-limits, logs API); issues #3220, #3547, #1665; getmaxim.ai benchmarks.
- **TensorZero:** tensorzero.com/docs (set-up-auth, enforce-custom-rate-limits, call-the-openai-responses-api, deployment/clickhouse, benchmarks).
- **Higress:** higress.ai/en/docs (ai-proxy, ai-token-ratelimit, ai-quota, ai-statistics); CNCF Sandbox announcement 2026-03-25; issues #2483, #3812.
- **Envoy AI Gateway:** aigateway.envoyproxy.io (release-notes/v1.0, capabilities/{security,observability,usage-based-ratelimiting}).
- **New-API / One-API:** github.com/QuantumNous/new-api, github.com/songquanpeng/one-api; New-API issue #5698.
- **Portkey:** github.com/Portkey-AI/gateway (LICENSE=MIT, issue #1672); paloaltonetworks.com press (acquisition completed 2026-05-29).
- **Helicone:** github.com/Helicone/ai-gateway (LICENSE=GPL-3.0, PR #182); mintlify.com/blog acquisition 2026-03-03.
- **LangDB:** github.com/langdb/ai-gateway (ELv2).

> All performance/memory numbers cited above are **vendor self-benchmarks** unless noted; none is independently third-party-validated. The one **primary-sourced, non-vendor** reliability fact in this whole comparison is that **LiteLLM's DB-mode memory leak is real** (its own issue tracker + maintainer roadmap + official memory-troubleshooting doc) — which is exactly why we're here.
