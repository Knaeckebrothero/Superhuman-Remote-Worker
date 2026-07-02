---
tags:
  - feature
  - rate-limiting
  - quotas
  - cost-control
  - subscription-plans
  - observability
related:
  - "[[remove_litellm_proxy_and_gateway_concept]]"
  - "[[usage_monitoring_and_rate_limiting]]"
  - "[[observability_and_quotas]]"
  - "[[usage_dashboard]]"
  - "[[llm_outage_pause_and_backoff_redispatch]]"
  - "[[project_self_improvement_loop]]"
  - "[[saas_billing_and_metering]]"
aliases:
  - rate limiting v2
  - plan quotas
  - subscription plan limits
  - orchestrator rate limiting
  - routerless rate limiting
---

# Rate limiting & subscription-plan quotas — routerless, orchestrator-enforced

> Design doc — 2026-07-01, from the "rate limiting for individual users, models,
> projects" planning conversation. Designed natively for the **routerless** LLM
> architecture (no proxy/gateway on the request path — see § Background for how we
> got here). Rewritten same day to stand on the routerless system as ground truth.

**Status:** DESIGN — not built. **Hard prerequisite:** the in-process metering
pipeline (audit `llm_requests` → `usage_events`, built + k3d-verified in
[[remove_litellm_proxy_and_gateway_concept]] P1) must be committed and cut over on
the homelab — it is this feature's entire data plane.

**Driver (concrete):** run the fleet — project loops
([[project_self_improvement_loop]]) first — on a **portfolio of flat-rate
subscription plans** instead of pay-per-token. The portfolio (2026-07-01, ~$440/mo):
Anthropic Claude Max **$200**, OpenAI **$100** (the codex-proxy's plan), MiniMax
Token Plan **Ultra $120**, Google **$20**, plus z.ai GLM Coding Plan **Max
(~$160)** under consideration. Every one of these enforces **rolling-window
quotas** (typically 5-hour + weekly), account-global — an uncoordinated fleet
burns a window blind and hits provider lockout mid-iteration. The **MiniMax plan
is the first lane** (loops run MiniMax today); the design must be portfolio-shaped
from day one (§3.2). Secondary drivers: per-user / per-project / per-model limits
have never actually been enforced in SRW; limit policy has never lived anywhere an
admin can touch; and nothing alerts when metering or a subscription plan silently
degrades.

**Explicitly NOT this doc:** dollar budget caps / wallets / markup
(→ [[saas_billing_and_metering]]); any proxy or gateway on the request path
(architecture decision, § Background); per-minute RPM smoothing as a v1 goal
(§ Deferred — provider 429s + agent backoff are the burst regulator).

---

## 1. The routerless system this builds on

How LLM traffic flows today, and where control is possible:

```
worker/session agent ── direct, per-dispatch-injected creds ──> provider
   │      cloud (MiniMax, …)  → OpenRouter (OpenAI-wire, reasoning normalization)
   │      self-hosted (gemma) → OpenAI-compatible endpoint (strix box)
   │      gpt-5.x / codex     → Responses API via codex-proxy (subscription)
   │      gemini              → direct Google
   │
   └─ every request audited in-process → auditdb `llm_requests`
        (model, tokens, user_id, project_id, ref_kind/ref_id = job|thread, ts)
                │
                └─> materializer (`materialize_llm_usage_from_audit`, min_age 60s)
                      → priced, attributed `usage_events` ledger
                      → Cockpit usage dashboard ([[usage_dashboard]])
```

**The control points that exist** (and are enough):

1. **Dispatch admission** — the orchestrator decides which jobs start and where
   (`_try_dispatch_pending_jobs`). It can defer or refuse dispatch per any
   criterion it can compute. This is the *pace* lever.
2. **The freeze path** — `pause_job` + `freeze_data` + `release_workspace` +
   cascade-to-children: stop running work, release compute, leave the job cleanly
   re-dispatchable. Proven by the shipped daily-quota stop. This is the *stop* lever.
3. **Agent-side reactive backoff** — `_classify_llm_error` → retry-with-backoff in
   `src/graph.py`, plus [[llm_outage_pause_and_backoff_redispatch]] (repeated LLM
   failure → job pauses and re-dispatches later instead of failing). This is the
   *burst/failure* floor that exists with zero coordination.

**The control point that deliberately does NOT exist:** nothing sits on the
request path, so SRW cannot 429 an individual request in-band. Consequences owned
up front:

- **Per-minute rate smoothing is out of scope for this mechanism.** Bursts hit the
  provider, the provider 429s, the agent backs off — self-regulating, uncoordinated,
  and acceptable. If a shared upstream (the strix box) ever needs *coordinated*
  smoothing, the answer is admission control at dispatch (§8), not a proxy.
- **Enforcement is poll-cadence, not per-request.** Audit rows are written
  per-request; the materializer holds them `min_age_s=60`; the enforcement loop
  polls ~120 s → worst-case **~3–4 minutes of overshoot** at burst rate. Against
  the hour/day/week windows this doc targets, that is noise — and it is exactly
  *why* per-minute limits are not this mechanism's job.

**What the ledger gives enforcement for free** (each of these was impossible or
unreliable in earlier gateway-based designs): per-request timestamps → **any
window shape** (rolling 5 h, daily, weekly) is a SQL sum; full attribution →
**account / project / user / model / job / session** scopes without any external
object model; per-user-per-model is just a GROUP BY; session traffic meters into
every scope; and enforcement reads and dispatch decisions live **in the same
process**, so there is no bypass lane to fail open through — the failure mode
shifts to *metering stall*, which gets a watchdog (§3.4).

## 2. Decisions

| # | Decision | Value |
|---|---|---|
| 1 | Enforcement point | **Orchestrator only.** Dispatcher admission gate (don't start work) + freeze path (stop running work). No proxy; no agent-side coordination protocol. |
| 2 | Usage source | **`usage_events`** (attributed + priced, shared with the dashboard — one source of truth). The raw `llm_requests` tail is the freshness fallback if the 60 s materializer lag ever matters. |
| 3 | Scopes | **`account`** (an upstream credential row — system-provider key or endpoint; the subscription-plan scope), **`project`**, **`user`** — each × a model selector (`model_id` \| category \| `*`). Tightest-wins when several match. The `account` scope is deliberately attribution-independent: it catches *all* traffic on a credential, including rows with no user/project. |
| 4 | Windows | `rolling-Nh` (first-class — the plan needs `rolling-5h`), `daily-utc`, `weekly`. |
| 5 | Limit units | `requests` and `tokens` per window (native ledger sums). Dollars stay out of scope. |
| 6 | Two thresholds | **`pace_at`** (default ~80%): dispatcher stops *starting* new work in the scope; running jobs finish. **`freeze_at`** (100%): running jobs in the scope are frozen (`freeze_data.type=quota_exceeded`) + workspaces released + dispatch gated, with a **computed `resume_at`** (the moment enough window-oldest usage ages out to drop back under the cap). |
| 7 | Plan exhaustion behavior | **v1 = freeze-until-window-frees** (pause is already the loop-safe failure mode). Auto-fallback of new dispatches to a pay-per-token lane is designed but deferred (§8, open question 3). |
| 8 | Policy home | Phase 1 ships env/values-JSON (zero UI dependency); Phase 2 moves policy to an app-DB table + Cockpit Admin UI and removes the values path. |
| 9 | Plan-side truth | Prefer the **provider's own usage read** where one exists (MiniMax `GET /v1/token_plan/remains`) — a subscription is account-global and may be consumed outside SRW. Ledger self-accounting is the always-available floor and the calibration check. |
| 10 | Cost display for plan traffic | Price at public API rates via `params_json.pricing_id` (same call as the codex decision): an *estimate of API-equivalent value*, clearly not a bill. |

## 3. Architecture

### 3.1 Quota engine (the core)

```
policy (env JSON → later rate_policies table)     usage_events
        │                                              │
        └──> quota_poll_loop (~120 s) <────────────────┘
                 │ per enabled policy: SUM(requests|tokens) over its window,
                 │ scoped by account/project/user × model selector
                 ├─ ≥ pace_at   → scope → "pacing" set → dispatcher defers NEW dispatches
                 ├─ ≥ freeze_at → freeze running jobs in scope (pause + quota_exceeded
                 │                + release_workspace) + dispatch gate + resume_at
                 └─ window slid under cap / resume_at reached → scope clears,
                    paused jobs re-dispatch via the existing auto-assign path
```

- **Reuses verbatim:** `pause_job`, `freeze_data`, `_cascade_pause_to_children`,
  `release_workspace`, and the dispatcher-gate shape from the shipped daily-quota
  stop (`is_project_over_quota` generalizes to `is_scope_limited(job)`), including
  the atomic rebind-not-mutate in-memory scope set the dispatcher reads.
- **New:** windowed scoped sums over `usage_events` (indexed, partition-pruned —
  the same query family the dashboard runs); the `account`-scope resolver (job's
  resolved model → its upstream credential row, known at dispatch); `resume_at`
  computation for rolling windows (from the window-oldest event timestamps).
- **Freshness:** the poll may additionally sum the un-materialized `llm_requests`
  tail (rows younger than `min_age_s`) to shave a minute off reaction time —
  decide at build whether the complexity pays.

### 3.2 Subscription-plan lanes — the portfolio

Each plan is **one `account`-scope policy on one credential row**. The engine
(§3.1) is plan-agnostic; what differs per plan is the lane (how SRW reaches it),
the window semantics, and whether the provider exposes a usage read:

| Plan | Windows | SRW lane today | Plan-usage read | Notes |
|---|---|---|---|---|
| **MiniMax Token Plan Ultra ($120)** | 5 h rolling + weekly, consumption-deducted | ❌ **new lane = Phase 0.** Today MiniMax rides OpenRouter pay-per-token; the plan key is a *direct* MiniMax credential | `GET /v1/token_plan/remains` (probe — auth caveat below) | The driving lane; detail below |
| **OpenAI $100** (ChatGPT/Codex sub) | 5 h + weekly (`wham/usage`) | ✅ **live** — codex-proxy (CLIProxyAPI) Responses lane | capacity bars shipped 2026-07-01 | **Quirk (verified):** proxied traffic bills as ChatGPT "extra usage" and does *not* consume the plan windows (bars read ~0%) — so the binding constraint here isn't the window at all; **verify what "extra usage" actually costs/limits** before adding a quota policy |
| **Anthropic Claude Max ($200)** | 5 h rolling + weekly | ❌ **no Anthropic lane exists in SRW** (catalog has none) | Claude usage surfaces (probe) | CLIProxyAPI — the same binary as the codex-proxy — supports Claude-subscription OAuth, so the lane is the proven proxy pattern, **not** a new architecture; Anthropic wire format / thinking-signature caveats apply (route-all research). Biggest untapped capacity in the portfolio |
| **z.ai GLM Coding Plan Max (~$160, considering)** | ~1,600 prompts/5 h + ~8,000/wk; **time-of-day multipliers** (GLM-5.2 draws 3× peak / 2× off-peak) | ❌ new lane — z.ai exposes an OpenAI-compatible coding-plan endpoint → slots in exactly like MiniMax | quota query (probe) | The multipliers mean self-accounted "requests" must be weighted by time-of-day (or calibrated to the worst case) — the one plan where a raw request count materially misestimates |
| **Google $20** (consumer AI plan) | consumer-surface limits | ⚠️ current gemini lane uses an **API key** (pay-per-token / free tier) — a consumer plan is likely **not reachable** via plain API key; the plausible bridge is Gemini-CLI OAuth via CLIProxyAPI (verify) | ? | Lowest value; investigate last or leave as-is |

Portfolio consequences for the engine: **nothing new is required in the core**
(N plans = N `account` policies), but pacing headroom matters more (several plans
are also consumed by the user's own IDE/desktop use outside SRW — decision 9), and
exhaustion behavior gets a better option than "wait": **rotate to another plan
with window headroom** (open question 3 / §8 — deferred, but the portfolio is why
it exists).

**The MiniMax lane (Phase 0) in detail:**

- **Routing:** the plan key is a MiniMax-direct credential (OpenAI-compatible
  endpoint on the MiniMax platform — pin the exact base URL at build), a normal
  catalog row. It does **not** ride OpenRouter — that is the pay-per-token lane,
  which stays available as the potential exhaustion fallback. Verify reasoning
  capture on the direct path (the removal doc's P2 covers the same check for
  cloud lanes generally).
- **Plan-side usage probe:** `GET /v1/token_plan/remains`. Known caveat: the
  older `coding_plan/remains` endpoint rejected API-key auth and wanted a console
  cookie (MiniMax-M2 issue #88); reports differ for the current hosts. **Probe at
  build.** If it authenticates → Cockpit **capacity bars** (5 h + weekly windows)
  mirroring the codex `wham/usage` bars shipped 2026-07-01 (provider panel in
  Settings; token stays server-side; non-fatal `{available:false}` degradation).
  Unlike the codex bars — which read ~0% because proxied traffic bills as "extra
  usage" — SRW's plan calls *are* plan usage, so these bars should actually move.
- **Quota policy:** one `account`-scoped policy on the plan credential:
  `rolling-5h` + `weekly`, request+token caps calibrated to the purchased tier
  (open question 1), `pace_at` ~80% / `freeze_at` ~95% — headroom for the user's
  own out-of-SRW plan usage; tighten once `remains` gives ground truth.
- **Lockout handling (belt + suspenders):** if MiniMax rejects for plan
  exhaustion despite pacing, the error must land in **pause, not fail** —
  [[llm_outage_pause_and_backoff_redispatch]] already pauses loop jobs on repeated
  LLM failure; add the plan-limit error signature to `_classify_llm_error` so
  lockout is distinguishable from a transient outage (a lockout has a computable
  `resume_at`; an outage doesn't).

### 3.3 Policy in the DB + Admin UI (Phase 2)

Sketch (finalize at build; ships as a normal `migrations/app/NNNN` file):

```sql
CREATE TABLE rate_policies (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_kind     TEXT NOT NULL,       -- 'account' | 'project' | 'user'
  scope_id       TEXT,                -- NULL = default for the kind
  model_selector TEXT NOT NULL DEFAULT '*',  -- model_id | category | '*'
  window_kind    TEXT NOT NULL,       -- 'rolling' | 'daily-utc' | 'weekly'
  window_seconds INT,                 -- for 'rolling'
  limit_requests BIGINT,              -- NULL = uncapped axis
  limit_tokens   BIGINT,
  pace_pct       SMALLINT NOT NULL DEFAULT 80,
  enabled        BOOLEAN NOT NULL DEFAULT true,
  note           TEXT,
  created_by     UUID,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- CRUD under `/api/admin/rate-policies` (admin-gated, same auth family as
  Admin → Models); the poll loop re-reads policies each tick — no restart to apply.
- **Cockpit Admin → Limits:** policy CRUD + a live per-policy consumption bar
  (current window sum / cap — the same aggregates the [[usage_dashboard]] view
  reads, filtered by the policy's scope). Per-user and per-user-per-model limits
  are just rows here.

### 3.4 Alerting / failure posture (Phase 3)

Two silent failure modes, both made loud:

1. **Metering stall** — enforcement reads the ledger; if the audit materializer
   wedges (cursor stops advancing while `llm_requests` keeps growing — the class
   of failure the id-vs-timestamp cursor bug in the metering build would have
   caused), every limit silently stops binding. Watchdog in the poll loop:
   `max(llm_requests.timestamp) − materializer cursor > threshold` → structured
   error log + Cockpit banner on the Usage/Limits views.
2. **Approaching-limit / lockout events** — a scope crossing `pace_at` /
   `freeze_at`, or the provider `remains` probe reporting near-exhaustion or
   flipping unavailable: structured log + surfaced on the affected project page
   and the Limits view (freeze events already land visibly as `quota_exceeded`
   freeze_data on the jobs).

## 4. Phases

**Phase 0 — MiniMax plan lane + visibility.** Register the plan credential +
model row(s), route direct, run a real loop-role job end-to-end on the plan key
(reasoning captured, `usage_events` rows attributed, `pricing_id` estimate set).
Probe `token_plan/remains`; ship the capacity bars if it authenticates (else file
the finding and let the bars degrade to ledger self-accounted sums).
*Acceptance:* a loop iteration runs on the plan; Cockpit shows its usage; the
`remains` question is answered empirically either way.

**Phase 1 — Quota engine.** Windowed scoped sums over `usage_events`; `account`
scope resolver; pace/freeze thresholds + `resume_at`; dispatcher gate generalized
from project-only to scope-set; policy via env JSON — with the plan policy as the
first real entry (the first non-inert limit numbers SRW has ever had).
*Acceptance:* a synthetic over-window scope freezes its running jobs
(`quota_exceeded`) + gates dispatch + auto-resumes when the window slides; the
pace threshold defers new dispatch while running jobs finish; a loop survives
plan exhaustion as a pause, not a failure; unit + k3d E2E on the real primitives.

**Phase 2 — Policy in DB + Admin UI.** `rate_policies` migration + admin CRUD
API + Cockpit Admin → Limits with live consumption; env-JSON path removed.
Per-user / per-user-per-model / per-project policies expressible and enforced.
*Acceptance:* an admin sets a per-user daily token cap in the UI and it visibly
binds (paces, then freezes) without a deploy; **browser-render-verified** (the
sessions-page dead-dialog lesson — test the control the user actually clicks).

**Phase 3 — Alerting + lockout classification.** Metering-stall watchdog,
approaching-limit events, plan-lockout error signature → pause-with-`resume_at`.
*Acceptance:* killing the materializer raises the banner within one poll
interval; a synthetic 80% crossing surfaces on the project page; a simulated
plan-lockout error pauses (not fails) the job with `resume_at` set.

**Phase 4 — deferred backlog (§8), pulled in on demand.**

## 5. Relationship to existing docs

- **[[remove_litellm_proxy_and_gateway_concept]]** — established the routerless
  architecture and built this doc's data plane (in-process metering, its P1);
  commit + homelab cut there is this doc's step-zero. Its P3 cleanup deletes the
  legacy `litellm.*` helm knob surface — nothing from it migrates here (those
  knobs were never set to real values).
- **[[usage_monitoring_and_rate_limiting]]** (done) — the predecessor feature.
  Its orchestrator-side pieces carry over (freeze path, dispatcher gate, the
  two-limit-types insight: *slow-down* vs *stop* must be distinguishable, and the
  midnight-flood analysis that motivates rolling windows). Its proxy-enforced
  layer is decommissioned. This doc absorbs its deferred items: rolling windows,
  per-user quota, per-user-per-model limits, DB+UI policy source.
- **[[llm_outage_pause_and_backoff_redispatch]]** — the reactive floor under
  Phases 0–1: until pacing binds, plan exhaustion degrades to the outage path
  (pause + backoff re-dispatch) instead of failed loop iterations.
- **[[usage_dashboard]] / [[observability_and_quotas]]** — same ledger,
  read-side; the Limits view reuses their aggregates. The alert-only posture of
  `observability_and_quotas` is superseded on the quota axis by enforcement here.
- **[[project_self_improvement_loop]] / [[loop_repo_compounding]]** — the
  customer: loops are the workload this keeps inside the plan window.

## 6. Accepted gaps (v1)

- **Sessions meter but aren't force-stopped.** Session usage counts toward every
  scope (thread rows are in the audit source), and a paced scope stops *job*
  dispatch — but an active session's turns aren't blocked at `freeze_at`.
  Hard-stopping sessions needs its own UX (block new turns with a visible reason
  vs end-thread) — deferred.
- **~3–4 min enforcement latency** (audit write → materializer `min_age` → poll
  tick). Fine for hour/day/week windows; anything per-minute is Phase-4
  admission-control territory.
- **Self-accounting undercounts the plan if it's consumed outside SRW** (the
  user's IDE etc. share the account). Mitigated by `freeze_at` headroom and, if
  the `remains` probe works, by preferring provider-side truth (decision 9).

## 7. Open questions

1. ~~Which Token Plan tier~~ → **ANSWERED 2026-07-01: the portfolio in §3.2**
   (MiniMax = Ultra $120; plus Anthropic Max $200, OpenAI $100, Google $20, z.ai
   GLM Max under consideration). Remaining sub-questions: does the MiniMax plan
   key already exist as a catalog row; is the z.ai purchase happening; and in
   what order do the non-MiniMax lanes get built (recommendation: MiniMax P0 →
   Claude Max next — biggest idle capacity — → z.ai if purchased → Google last).
2. **`token_plan/remains` auth** — does it accept the plan API key on the current
   hosts (the M2-era issue said cookie-only)? Empirical Phase-0 probe.
3. **Exhaustion behavior beyond freeze** — v1 freezes until the window frees. The
   portfolio opens a better option: **rotate new dispatches to another plan with
   headroom** (MiniMax window gone → loop continues on GLM or Claude), with
   pay-per-token OpenRouter as the last resort. That is capacity-aware model
   selection — a real feature (per-loop/per-job model becomes a *policy outcome*,
   not a fixed setting) — deferred to §8; revisit once ≥2 plan lanes exist and
   pacing data shows how often windows actually exhaust.
4. **Does the strix box need coordinated per-minute smoothing at all**, or do its
   own 429s + agent backoff suffice? Decides whether Phase-4 admission control
   ever gets pulled forward.
5. **Self-accounting calibration per plan** — MiniMax deducts by opaque "actual
   resource consumption", not raw tokens; z.ai applies 3×/2× time-of-day
   multipliers; ChatGPT counts proxied traffic as "extra usage" outside the
   windows entirely. Each lane's first cap numbers are estimates to calibrate
   against the provider's own read / observed lockouts — conservative (worst-case
   multiplier) until calibrated.

## 8. Deferred (parked, not lost)

- **Admission-control RPM smoothing** (derived concurrency:
  `sustainable_agents = rate_budget / demand_per_agent`, two-timescale control —
  full design in the predecessor doc's Deferred §). It plugs into the dispatcher,
  not a proxy, so it composes with this doc unchanged if a shared upstream ever
  needs coordinated burst control.
- **Portfolio rotation / capacity-aware model selection** (open question 3) —
  dispatcher picks the model/lane by plan-window headroom: drain flat-rate
  capacity across the portfolio before touching pay-per-token; freeze only when
  every lane is dry. Needs ≥2 plan lanes + Phase-1 window accounting first; the
  per-loop model override is the natural actuation point.
- **New plan lanes beyond MiniMax** — Claude Max via CLIProxyAPI's Claude-OAuth
  support (the codex-proxy pattern; Anthropic-wire caveats), z.ai GLM
  coding-plan endpoint (MiniMax-shaped), Google consumer-plan bridge (unclear —
  verify it's reachable at all).
- **Session hard-stop** (§6).
- **Per-key BYOK limits** — a user's own provider key as an `account`-scope row
  (the mechanism already fits; just policy rows).
- **Keycloak short-lived JWT credential isolation** — the security thread owned
  by the removal doc's deferred list; orthogonal here.

## 9. Background — why routerless, and what it changed for rate limiting

SRW previously ran a self-hosted LiteLLM gateway on the request path
([[usage_monitoring_and_rate_limiting]], shipped 2026-06; extended by
`route_all_models_through_litellm_gateway.md`). It enforced per-minute limits
in-band (virtual keys / teams / internal users → 429 + Retry-After) and fed a
daily per-project quota read. On 2026-07-01 the gateway — and the proxy concept —
was removed ([[remove_litellm_proxy_and_gateway_concept]]): a chronic DB-mode OOM
leak put it on the critical path as a reliability liability, an 11-alternative
evaluation found no drop-in, and inspection showed its unique value for our
provider set was ≈ nothing (OpenRouter already does the real schema unification;
metering moved in-process and got *better* — per-job attribution and session
coverage the gateway structurally couldn't see; its limit knobs had shipped inert
and were never set).

For rate limiting, the removal traded away exactly one thing — in-band per-minute
429s — and that trade is fine: burst pressure is handled reactively by provider
429s + agent backoff, while every limit anyone actually asked for (subscription
windows, per-user/project/model budgets) is a *long-window quota*, which the
orchestrator enforces better from its own ledger than a proxy ever did from
per-minute counters. The proxy-era limits also carried a structural fail-open
hole (gateway unhealthy → traffic silently routed direct, unmetered and
unthrottled) that the routerless design removes by construction: the process
that enforces is the process that dispatches.

## 10. Sources / evidence

- Routerless architecture + in-process metering build (incl. the id-vs-timestamp
  cursor lesson and the codex `wham/usage` capacity-bar precedent):
  `docs/issues/remove_litellm_proxy_and_gateway_concept.md` (2026-07-01).
- Enforcement-primitive provenance (freeze path, dispatcher gate, slow-vs-stop):
  `docs/done/usage_monitoring_and_rate_limiting.md`.
- MiniMax Token Plan mechanics (tiers, 5 h + weekly windows, consumption-based
  deduction, exhaustion options): platform.minimax.io Token-Plan FAQ/overview +
  pricing pages (fetched 2026-07-01/02 — Starter $10/1,500 req per 5 h, Plus
  $20/4,500, Max $50/15,000, **Ultra $120**, Highspeed variants above);
  `remains` endpoint + auth caveat: MiniMax-AI/MiniMax-M2 GitHub issue #88;
  third-party plan write-ups (Kilo Code, Flowith, BuyGLM/mclist tier tables).
- z.ai GLM Coding Plan (Max ≈ $160/mo, ~1,600 prompts/5 h + ~8,000/wk, GLM-5.2/
  GLM-5-Turbo/GLM-4.7 only, 3× peak / 2× off-peak quota multipliers, OpenAI-
  compatible coding-plan endpoint): z.ai subscribe/devpack FAQ + third-party
  guides (fetched 2026-07-02).
- Subscription portfolio (which plans, prices): user, 2026-07-01.
