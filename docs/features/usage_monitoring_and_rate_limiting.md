---
tags:
  - feature
  - observability
  - rate-limiting
  - llm-gateway
  - cost-control
  - credentials
related:
  - "[[observability_and_quotas]]"
  - "[[saas_billing_and_metering]]"
  - "[[credential_broker]]"
  - "[[agent_loop_mode_pod_reuse]]"
  - "[[custom_llm_endpoints]]"
  - "[[codex_proxy]]"
aliases:
  - usage monitoring
  - rate limiting
  - llm gateway
  - rpm limits
  - throughput limiting
---

# Usage Monitoring & Rate Limiting — LLM gateway for RPM measurement + throttling

> Design doc — captured 2026-06-20 from the "user usage monitoring + rate limits"
> planning conversation, **reframed** mid-discussion: this is **not** a dollar
> cost-brake. Per-key dollar caps are set at the **provider** (and must be). What
> this builds is (1) **measurement** — see real requests-per-minute per model, so
> limits can be calibrated — and (2) **rate limiting** — throttle usage to stay
> under provider API limits and subscription-coding-plan rate limits, so the fleet
> stops generating constant 429 errors.

**Status:** Design — ready to build.
**Triggered by:** Putting the system into real operation. Multiple agents call
the same providers with **zero coordination** today — each discovers limits by
hitting a 429 and backing off independently (`src/graph.py:177-290` classify +
`:1969-1998` retry). Against rate-limited **subscription coding plans** (codex /
Claude-subscription via CLIProxyAPI) and **provider API tiers**, an uncoordinated
fleet produces constant 429 storms. There is also **no** live view of actual
RPM/TPM consumption, so limits can't even be set sensibly. Dollar cost is handled
at the provider key, so the need is **throughput control + visibility**, not a
spend gate.
**Scope:** A central LLM gateway all agent calls traverse, providing
**per-model-category rate limits** (admin-configurable), a longer-window quota
stop, and **live RPM/TPM/usage monitoring**.
**Explicitly NOT this round:** dollar budget caps (set provider-side); wallet /
markup / billing → [[saas_billing_and_metering]]; capacity-aware admission
(derived concurrency) → Deferred §; the custom usage ledger + workspace metering
→ Slice 4 (later).

## Why a gateway (the one mechanism that does all of it)

A central proxy in front of all LLM traffic is the only place that can do the two
things asked, because both are inherently **cross-agent**:

- **Aggregate visibility — the measurement need.** "How many RPM am I using on
  gpt-5.5?" is unanswerable from any single agent; only a chokepoint sees the sum
  across every job/session. The gateway *is* the RPM meter.
- **Aggregate throttling — the coordination need.** A provider/subscription rate
  limit is on the **key/account**, shared by all jobs using that upstream. To stay
  under it you must cap the **aggregate** rate, which requires one coordinator —
  N agents each backing off on their own 429s cannot collectively stay under a
  shared ceiling (they all retry into it together). One token bucket in one place
  does.
- **"Agents wait and honor" comes nearly free.** The gateway returns
  `429 + Retry-After` on a rate breach; the agent's **existing** reactive backoff
  already honors exactly that — `_classify_llm_error` (`src/graph.py:177-290`,
  status-code at `:213`) → `_extract_rate_limit_delay` (`:103-151`, reads the
  header) → retry loop (`:1969-1998`, sleeps & resumes). We feed backpressure the
  agent already implements; we build no new agent behavior.
- **Identity without re-plumbing.** `jobs.user_id` is available at the dispatcher
  but **stripped at the agent wire boundary** (`JobStartRequest`,
  `src/api/models.py:243`, has no `user_id`). A **per-job virtual key** carries
  job_id + user_id; the gateway maps key → job/user for per-user limits and
  per-job attribution. `user_id` never has to reach the agent.
- **It reuses a pattern already in the stack** — the codex proxy (CLIProxyAPI)
  already brokers one LLM path. This is [[credential_broker]] note 1, realized.

**Integration seam (near-zero agent change).** The orchestrator already injects
`base_url` + `api_key` into a job's `config_override` at dispatch
(`_inject_dispatch_credentials`, `main.py:1141-1144`) and the agent consumes them
(`loader._create_openai_llm`, `:2557-2614`). Pointing an agent at the gateway is
injecting `base_url = <gateway>` + `api_key = <virtual key>` there — identical to
the codex path. *(Prerequisite: per-job client rebuild, so a reused looped agent
doesn't carry a prior job's client/key — [[agent_loop_mode_pod_reuse]] item 1.)*

## Locked decisions

| # | Decision | Value |
|---|---|---|
| 1 | Driver | **Measure real RPM/TPM + rate-limit to honor provider & subscription-plan limits.** Avoid constant 429 storms. **Dollar cost is out of scope** — capped at the provider key. |
| 2 | Mechanism | One central **self-hosted LiteLLM** gateway all agent LLM calls traverse. |
| 3 | Enforcement | **Rate limits (RPM/TPM), not dollar caps.** No spend gate. |
| 4 | Enforcement **subject** | **Per-user and per-project** — *not* application-wide aggregate. |
| 4a | Model **categories** | A **config convenience** for the admin: group models (`large`/`medium`/`small`) to set one limit across many, with per-model override (gpt-5.5 ≠ opus-4.8 if wanted). Categories are **our** abstraction — LiteLLM has no model-group rate limits (see capability check), so the orchestrator **expands category → individual model_names** when writing limits. |
| 5 | Limit responses | **RPM/TPM breach → 429 → agent backoff (slow down), in-band.** **Longer-window quota breach → hard stop + pod teardown, orchestrator-driven (out-of-band).** |
| 5a | Upstream backstop | **REVISED — k3d-verified 2026-06-21.** Per-deployment `rpm`/`tpm` in `config.yaml` does **NOT** 429 in our setup (it's router/load-balancing metadata; hard pre-call enforcement needs **Redis + `enable_pre_call_check`/`enforce_model_rate_limits`**, and the **admin master key bypasses all limits**). What **does** enforce in-memory (no Redis) is **key/team `model_rpm_limit`** (proven 429 + Retry-After). So the backstop is a **shared "fleet" virtual key** — deterministic value (HMAC of the master key, no persistence), `model_rpm_limit = upstream capacity`, scoped to registered models — that all agents use **instead of the admin master key** (security win). Fleet aggregate → one bucket → 429 → backoff. Capacity = the `litellm.backstop` knob (`{model_id\|'*':{rpm/tpm}}`). **Implemented (Slice 2a).** |
| 6 | Identity | Per-**job** virtual key minted at dispatch, **revoked on completion**; carries job_id + user_id (+ team_id = project). |
| 7 | Monitoring (v1) | LiteLLM's **native dashboard** (RPM/TPM/usage per key/user/model). Build no custom UI for Slices 1–3. |
| 8 | Store of record (later) | Slice 4 only: canonical rows in **`usage_events`** (option A); LiteLLM's internal DB = enforcement-only. |
| 9 | Metered scope | LLM via gateway. **Workspace** compute → Slice 4 (cost-picture completion, decoupled from rate-limiting). Agent pods / query / storage → ignored. |
| 10 | BYOK | Route the user's own key **through the gateway too** (rate-limited + measured); later, let the user set custom limits on their own key. |
| 11 | Subscription plans | Codex / subscription models route **LiteLLM → CLIProxyAPI → provider** so they're rate-limited too (the plans this is most needed for). |

## Architecture

### Two limit types, two enforcement paths

The crux: a *short-window rate* limit and a *long-window quota* must produce
**different, distinguishable** responses, or a quota stop degrades into an
infinite slow-retry loop (the agent backs off a 429 forever instead of stopping).

| Limit | Window | Gateway/orch response | Agent sees | Net |
|---|---|---|---|---|
| **RPM / TPM** (per category, per key) | per-minute | gateway `429 + short Retry-After` | classified `rate_limit` → backoff & retry | **throttle** — agent slows down, keeps working |
| **Quota** (per user, daily / N-hour) | rolling hours | **orchestrator** sees over-quota → freeze + **teardown** | job frozen by orchestrator | **hard stop**, pod released |

- **RPM throttle is in-band** — gateway → 429 → existing agent backoff. Fully
  self-regulating; no orchestrator involvement. Verify the existing rotate-vs-
  backoff split (`reasoning_chat.py:664-699`, `:694-696`) treats a LiteLLM
  rpm-429 (short retry-after, no quota signal) as **backoff, not key rotation**.
- **Quota stop is out-of-band** — the agent can't cleanly tear down its own pod,
  and shouldn't have to distinguish "slow down" from "stop" off an ambiguous 429.
  Instead the **orchestrator** polls LiteLLM spend/usage (or a webhook) for users
  over their rolling quota and drives the existing freeze + workspace-teardown
  path. Lifecycle stays where it belongs.
- **Known issue, accepted for now:** a daily window means quota-stopped jobs could
  all become resumable at the same reset instant → a "midnight flood" of restarts.
  Mitigate later with a **rolling N-hour window (4–8 h)** or a **weekly** cap, or
  staggered resume. Filed as a follow-up, not a v1 blocker.

### Enforcement subject: per-user / per-project (categories are config sugar)

Limits are enforced **per user** and **per project**, not application-wide. The
admin sets a limit per **model category** (a grouping of models) purely so they
don't configure 5000 models one by one; individual models can still differ. The
category is *our* abstraction — the orchestrator expands it to the underlying
`model_name`s when it writes limits to the gateway (LiteLLM keys limits by exact
model name; see capability check).

**Residual the subject choice doesn't cover:** per-user/project limits don't by
themselves bound the **shared upstream key** — Σ(active users × their limit) can
exceed what the provider key actually allows, which would still 429. Covered by
decision 5a: the per-deployment `rpm`/`tpm` in `config.yaml` (the upstream's real
capacity) sits underneath as a hard ceiling. That is *not* an "application limit
on users" — it's the gateway knowing the provider's physical throughput.

### LiteLLM capability check (verified 2026-06-20)

The matrix (user × project × model-category) **maps onto LiteLLM**, via:

| Our concept | LiteLLM primitive | Per-model limits? |
|---|---|---|
| **project** | **team** (`/team/new`, `/team/update`) | ✅ `model_rpm_limit` / `model_tpm_limit` = dict `{model_name: int}` |
| **user** | **internal user** (`/user/new`, `/user/update`) | flat `rpm_limit`/`tpm_limit`/`max_parallel_requests` (per-model rides on the key) |
| **job** | **virtual key** (`/key/generate`, tagged `user_id`+`team_id`+metadata) | ✅ same `model_rpm_limit` dict in key metadata; **resolution key > team** |

**Confirmed supported:** per-model rpm/tpm dicts at team **and** key level; per-job
key minting with TTL (`duration`) + revoke (`/key/delete`|`/key/block`); model
allow-list (`models=[]`); multi-instance limit sharing via Redis.

**Gaps / footguns — these define our thin orchestrator-side layer:**
1. **No model-group rate limits.** Limits key by **exact `model_name`**; a dict
   key that doesn't match the request's model string is **silently skipped** (no
   error). → Orchestrator owns the category→model_names expansion **and** a
   validation guard that every intended model actually gets its limit (a silent
   miss = an unthrottled model).
2. **Long-window quota is dollar-only.** `max_budget`+`budget_duration` is **USD**
   with a **fixed cron reset** (not rolling). There is **no native request-count
   or token-count daily quota.** → The Slice-3 request/token quota stop is
   **necessarily orchestrator-enforced** (poll usage → freeze+teardown). Confirms
   both the out-of-band design and the midnight-flood risk.
3. **Enforcement is not free-trust.** Open issues report key-level per-model
   limits enforcing unreliably in DB mode (#10052) and `enforce_model_rate_limits`
   may be required to make rpm/tpm **hard-block** vs merely inform routing. → A
   load-test that a set limit actually returns 429 in our deployment mode is an
   acceptance gate, not an afterthought. (Per-model 429 also skips fallback,
   #24152 — for us that's *desired*: we want the 429, not a silent model swap.)
4. **Usage polling:** spend/usage is tracked (end_user, team, model_group,
   tokens); confirm the exact read path at build (spend API vs reading LiteLLM's
   Postgres spend table directly) for the Slice-3 poll.

### Metering ownership (Slice 4) — event-driven vs time-driven

When the custom ledger lands: meter each category at its **point of ground
truth**. LLM cost is **event-driven** → the gateway (only place that sees tokens
+ identity live). Compute is **time-driven** → a pod existing t0→t1 costs money
with zero LLM calls, unreconstructable from an event stream → the orchestrator
(owns pod lifecycle). Gateway + orchestrator are **emitters into one
`usage_events` ledger**; LiteLLM's internal spend stays **enforcement-only**, and
both emitters need **at-least-once** delivery (gateway buffers+retries; orch runs
the open-interval reconciler). Attribution is clean for both metered resources:
LLM self-attributes per call via the virtual key (robust to agent-pod reuse);
workspaces are single-owner (worker one-shot `container_provisioner.py:291`;
session one-thread, billed per active interval).

## Infrastructure

> The deployment delta is small because LiteLLM brings its own persistence:
> **+1 app container, +1 small Postgres LiteLLM fully owns, ~0 new tables on our
> side for Slices 1–3.** The chart already deploys one small single-replica
> Postgres StatefulSet per workload class (`helm/templates/databases/postgres.yaml`,
> `-vector`, `-audit`, `-keycloak`), gated by `databases.<name>.enabled` — LiteLLM
> drops straight into that mold.

### New chart components (Slices 1–3)

| Component | K8s objects | Chart location | Notes |
|---|---|---|---|
| **LiteLLM gateway** | Deployment + ClusterIP Service | new `helm/templates/litellm/` (mirror `orchestrator/`) | image `ghcr.io/berriai/litellm`; port **4000**; agents reach `http://<release>-litellm:4000` |
| **LiteLLM Postgres** | StatefulSet + PVC + Service | new `databases/postgres-litellm.yaml` (copy `postgres-audit.yaml` near-verbatim) | gated by `databases.litellm.enabled`; **required** — DB mode is what unlocks virtual keys + per-user/team limits + spend tracking |
| **Secrets** | `LITELLM_MASTER_KEY`, `LITELLM_POSTGRES_USER/PASSWORD` | existing `secret.yaml` (+ ESO/Vault for prod) | mirror the `AUDIT_POSTGRES_*` keys |
| **Config** | `LITELLM_POSTGRES_DB` + a mounted `config.yaml` (model list, upstreams, per-deployment rpm/tpm backstop) | existing `configmap.yaml` + a new ConfigMap | upstreams incl. homelab router + LiteLLM→CLIProxyAPI for codex |

### Databases

- **+1 new DB: LiteLLM's own.** Ownership note that matters operationally:
  **LiteLLM self-migrates its schema via Prisma on startup** — it creates/owns its
  `LiteLLM_*` tables (keys, teams, users, spend logs). This is **not** driven by the
  orchestrator's `migrations/{app,audit,vector}/` runner. New DB, but we don't author
  its tables.
- **No new DB for the ledger.** Slice 4's `usage_events` lands in the **existing
  `srw-auditdb`** as `migrations/audit/0002` — the audit template already earmarks it
  ("and, later, the usage-metering ledger", `postgres-audit.yaml:3-7`). Slice 4 =
  **0 new DBs, 1 new table.**

### New tables in *our* schema

- **Slices 1–3:** effectively **none required** — keys/limits/spend live in LiteLLM's
  DB. Only candidate: **one small config table** in the app DB for the category→model
  map + per-user/project limit policy (Open Q4) — and that can start as **YAML**, so
  possibly zero.
- **Slice 4:** **+1** `usage_events` (audit DB) + workspace-metering emit code + a
  Cockpit view.

### Not needed (for v1)

- **No Redis** — only required to share rate-limit counters across *multiple* LiteLLM
  replicas; v1 is single-replica → in-memory. (Un-defers at scale — see below.)
- **No new DB server** for metering — reuses `srw-auditdb`.
- **No agent-side infra change** — agents are pointed at the gateway via the existing
  dispatch config-swap (`_inject_dispatch_credentials`).

### Side effect: centralized provider egress

Today every agent pod egresses directly to providers. With the gateway, **only the
LiteLLM pod needs outbound provider egress** — agents reach it in-cluster. Shrinks the
egress surface (composes with the agent-egress NetworkPolicy work).

## Slices (each independently shippable)

### Slice 1 — Gateway + RPM/TPM visibility *(the measurement need; starting point)*
- Stand up LiteLLM in-chart; route all agent LLM traffic through it via the
  dispatch config-swap. **No enforcement yet** — pass-through.
- Include codex/subscription models via LiteLLM → CLIProxyAPI so their usage is
  visible too.
- **Deliverable:** LiteLLM native dashboard shows live **RPM/TPM per model / key /
  user**. This alone answers "how much am I actually using?" — the input needed to
  set sane limits.
- **Acceptance:** normal traffic flows to providers unchanged; the dashboard shows
  non-zero per-model RPM under load; homelab router (`ai.h4ll.app`) + external
  providers + codex all visible as upstreams.

### Slice 2 — Per-user / per-project rate limits *(the headline enforcement)*
- Admin config (ours): model → **category** map + a **RPM/TPM per category**,
  settable per **user** and per **project**, with per-model override.
- Map to LiteLLM: **project → team** (`model_rpm_limit` dict), **user → internal
  user**, **job → virtual key** (tagged user_id+team_id) minted at dispatch,
  revoked on completion. Orchestrator **expands category → exact model_names**
  when writing the dicts + validates none silently skip. *(Prereq: per-job client
  rebuild, [[agent_loop_mode_pod_reuse]].)*
- Set per-deployment `rpm`/`tpm` in `config.yaml` to each provider key's real
  capacity (decision 5a backstop).
- Breach → `429 + short Retry-After` → agent's existing backoff slows it down.
- **Acceptance (enforcement is a gate, not faith — gap 3):** a configured per-user
  limit on a model **actually returns 429** under load in our deployment mode
  (verify `enforce_model_rate_limits` / DB-mode enforcement); agents wait via
  429+retry rather than erroring; raising/lowering the limit visibly changes
  throughput; a deliberately-misnamed category entry is caught by the validation
  guard, not silently unthrottled.

### Slice 3 — Longer-window quota stop *(orchestrator-enforced)*
- Per-user / per-project **rolling N-hour or daily request/token quota**.
  **Necessarily orchestrator-side** — LiteLLM's only long-window cap is
  dollar-denominated (capability gap 2), and we want request/token counts.
- Orchestrator polls LiteLLM usage (confirm read path — gap 4) via **one aggregate
  `GROUP BY user` query, never a per-user loop** (see Scaling); over-quota → freeze
  the user's jobs + tear down pods (reusing the freeze/teardown path).
- **Acceptance:** a user crossing the quota has in-flight jobs frozen and
  workspaces released; throttle (Slice 2) and quota-stop are distinguishable (one
  slows, one stops); midnight-flood mitigation (rolling window) applied or filed.

### Slice 4 — Unified ledger + workspace metering + Cockpit view *(later)*
- Adopt **option A**: gateway per-request callback writes canonical
  `category='llm'` rows into `usage_events`; LiteLLM DB → enforcement-only.
- Orchestrator emits `category='compute'` workspace open/close intervals
  (`requests × wall-clock`) + open-interval reconciler.
- Minimal admin "Usage" view joining both per job/user/day (G5 visibility model).
- **Acceptance:** one query gives job cost = LLM + workspace, reconciled against
  the gateway's own number.

> Slices 1–3 are the operational win (measure + throttle + quota), all on
> LiteLLM-native surfaces. Slice 4 stands up the durable ledger billing will later
> read and completes the cross-resource cost picture.

## Relationship to the existing docs

- **[[observability_and_quotas]]** — shares the eventual `usage_events` spine
  (Slice 4), but this is **enforcing throughput**, where that doc was read-side /
  alert-only. Its soft *cost* alerts remain a later add-on; the *dollar* posture
  here is "set at provider, not metered for a gate."
- **[[saas_billing_and_metering]]** — disjoint for now: that's dollar wallets +
  markup; this is rate limits. Slice 4's ledger is the substrate it will reuse.
- **[[credential_broker]]** — this realizes note 1 (broker the normal LLM path)
  via LiteLLM, led by the operational framing the broker doc named as its
  justification. Deferred from here: pod-binding (tailnet), per-session policy,
  broker-as-SPOF hardening.
- **[[codex_proxy]]** — CLIProxyAPI stays for codex OAuth; it sits **behind**
  LiteLLM so subscription-plan traffic is rate-limited too (decision 11).
- **[[agent_loop_mode_pod_reuse]]** — per-job virtual keys (Slice 2) need the
  per-job-client-rebuild audit; prerequisite.

## Deferred (parked, not lost)

**Dollar budget caps / billing** — set at the provider key today; wallet/markup/
debit → [[saas_billing_and_metering]].

**Capacity-aware admission (derived concurrency).** The efficiency layer that
stops one user holding 30 starved pods. Concurrency isn't an arbitrary knob —
it's derived from the rate budget this doc enforces:

```
sustainable_agents(user) = rate_budget(user) / demand_per_agent(cohort)
```

Gateway *enforces* rate, ledger *measures* per-agent demand, dispatcher *derives*
the pod count and actuates via the existing priority-preemption in
`_try_dispatch_pending_jobs`. Captured constraints for that future build:
two-timescale control (429-backoff = fast loop absorbs bursts; pause/resume =
slow loop, act only on *sustained* over-subscription); **measure demanded rate,
incl. 429'd attempts, not served rate** (throttling suppresses the signal);
per-cohort demand (expert × model × phase, bursty/bimodal, cold-start prior);
bound per-user under per-endpoint capacity (Σ user budgets can exceed physical
GPU rpm); policy knob priority-preemptive (less churn) vs fair-share (fairer,
hits the fragile resume path).

**Other cost centers** — query (Neo4j/pgvector), storage, agent-pod compute:
ignored now; the ledger's `category` taxonomy reserves them.

## Scaling to N users

> Verdict: the **shape holds** — a central gateway is how rate-limiting/metering is
> done at *any* scale, not something you outgrow. What upgrades at ~1000 users is the
> single-instance v1 *deployment*, in a predictable order. **Every lever below is
> already a deferred design item; scaling = un-deferring, not rethinking.** The real
> ceilings (provider tier capacity, cluster nodes) are things the gateway *rations
> fairly* but can't *create*.

### Un-defer order (each bites roughly in turn)

| # | Pressure at scale | Fix (already in the design) |
|---|---|---|
| 1 | One LiteLLM pod won't carry aggregate RPS, and it's now **everyone's critical path** | **Multi-replica LiteLLM + Redis** for shared rate-limit counters, + Redis HA. The Redis skipped at v1 becomes **mandatory** the moment replicas > 1. |
| 2 | LiteLLM writes a **spend-log row per request** + hot-row spend-counter updates → DB firehose (not proxy CPU) | Lean on the **partitioned `usage_events`** ledger (Slice 4 — audit-store machinery is built for firehose volume + retention) as the durable store; keep LiteLLM's own DB **enforcement-only, short-retention**, with batched spend writes. |
| 3 | Orchestrator added load: key mint/revoke per job (2 calls) + the quota poll | Quota poll = **one aggregate query**, never a per-user loop; consider **per-session keys** instead of per-job to cut key churn (trades attribution granularity). |
| 4 | Hundreds of concurrent agent + workspace pods | **Derived-concurrency admission** (Deferred §): `sustainable_agents = rate_budget / demand_per_agent` — stops provisioning thousands of starved pods. |
| 5 | Gateway holds **every** provider key → compromise = total; real external users | **Credential-broker hardening** ([[credential_broker]]): per-session least-privilege + pod-binding (tailnet). |

### The honest ceiling

- **Homelab:** finite cluster nodes cap concurrent agents *well before* 1000 users —
  the cluster is the first wall, not the gateway.
- **Cloud:** the walls are provider **tier limits** (1000 users share a finite set of
  keys → per-user limits get small unless you buy capacity) and the autoscaled pod
  fleet.
- The gateway makes scarcity **fair and visible**; "it holds at 1000 users" means "it
  throttles everyone correctly," not "everyone runs at full speed." Creating capacity
  is a provisioning/procurement question, not an architecture one.

## Resolved by the capability check (2026-06-20)

- ~~Aggregate vs per-user~~ → **per-user + per-project** (decision 4); categories
  are config sugar (4a); upstream ceiling handled by per-deployment `config.yaml`
  rpm (5a), not a user-facing app cap.
- ~~LiteLLM expresses the matrix?~~ → **yes**, via team/user/key (capability
  check), with three gaps now owned as orchestrator-side work (category
  expansion + validation, request-count quota, enforcement load-test).

## Open questions

1. **Limit unit:** **requests** (RPM) only, or also **tokens** (TPM)? Both are
   native. *Lean: RPM first (matches the "requests per minute" framing); add TPM
   for a token-heavy category when needed.*
2. **Quota window:** daily (simple, midnight-flood) vs rolling N-hour (4–8 h,
   smoother) vs weekly (matches some subscription allowances)? *Lean: configurable
   per category; default rolling to dodge the flood.* Orchestrator-enforced either
   way (gap 2).
3. **User vs project precedence:** a job has both a `user_id` and a `project_id`
   (= team) — when both carry a limit, which wins? *Lean: the more restrictive
   (min) applies; LiteLLM resolves key > team, and the user limit is a separate
   independent ceiling, so effectively all three bind and the tightest wins.*
4. **Category definition home:** where does the model→category map live — a new
   admin-editable table (mirrors the DB-override pattern), or config YAML? Ties to
   how the orchestrator expands categories before writing LiteLLM limits.

## References

- [[observability_and_quotas]] — the `usage_events` ledger spine (Slice 4).
- [[saas_billing_and_metering]] — the dollar-billing follow-on (disjoint now).
- [[credential_broker]] — the gateway as note-1, operationally led.
- [[codex_proxy]] / [[agent_loop_mode_pod_reuse]] — CLIProxyAPI placement; per-job
  client-rebuild prerequisite.
- LiteLLM Proxy — virtual keys, per-key/user/model RPM/TPM, usage tracking.
