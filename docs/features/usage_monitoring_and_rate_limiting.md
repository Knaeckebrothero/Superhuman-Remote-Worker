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

**Status:** **Slices 1 + 2a committed; Slices 2b + 3 + 4 implemented + k3d-verified (2026-06-22), uncommitted on `develop`.** The feature is functionally **complete end-to-end** — measure + attribute + throttle (429) + daily quota stop + durable `usage_events` ledger (LLM tokens + workspace compute) + a Cockpit usage view. The throttle/quota/rate knobs still ship **inert** until real capacity is measured. See **Implementation status** below for what shipped, the spike findings that revised the design, and the gotchas.
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
→ Slice 4 (✅ shipped 2026-06-22).

## Implementation status (updated 2026-06-22)

Built on local k3d (`k3d-srw`), uncommitted on `develop`. Driven **spike-first** —
several LiteLLM assumptions in this doc were **overturned by live testing** and the
design revised accordingly (below).

**✅ Slice 1 — gateway + per-model RPM measurement.** LiteLLM in-chart (own Postgres,
DB mode, Prisma self-migrate). The model catalog is **DB-only** (admin-curated, keys
encrypted in the app DB, no Secret copy), so the orchestrator **syncs the catalog into
LiteLLM via its admin API** (`/model/new`) rather than a static `config.yaml`:
`orchestrator/services/litellm_gateway.py` decrypts each upstream key (as dispatch
already does) and registers endpoint-kind models, reconciling on a 60 s loop. Agent
traffic is pointed at the gateway via the existing dispatch config-swap
(`_inject_dispatch_credentials` / `_inject_model_credentials`). *Verified:* 4 homelab
models registered; real gemma traffic measured per-model in `/spend/logs`. *Scope held:*
endpoint-kind models only (gemini/`system` + embedding/whisper/tts stay direct for now;
codex isn't deployed on this cluster).

**✅ Slice 2a — aggregate upstream backstop (shared fleet key).** The prereq
([[agent_loop_mode_pod_reuse]] item 1) is **resolved**: the agent rebuilds its LLM
clients (strategic/tactical/aux) on every dispatch (`config_dirty` always true →
`_create_phase_llms()` reruns before the graph builds), so minted keys can't bleed
across jobs. The backstop is a **shared, non-admin "fleet" key** (deterministic value =
HMAC of the master key, so routing recomputes it with no persistence) carrying
`model_rpm_limit = capacity`, that all agents use **instead of the admin master key**.
Config knob: `litellm.backstop` (`{model_id|'*':{rpm/tpm}}`, default `{}`). *Verified:*
fleet aggregate 429 at a test cap, cap-removal propagates, agents off the master key,
34 unit tests.

**✅ Slice 2b — per-user / per-project rate limits (scoped keys).** Each job/session
routes through a **per-(user, project) scoped key** instead of the shared fleet key:
deterministic value (HMAC, like the fleet key), bound to a LiteLLM **team** (= project,
carrying per-model `model_rpm_limit`) and an **internal user** (= user, flat `rpm`/`tpm`).
The key itself carries no limits — the **team and user objects do the aggregating**
(team across the project's keys, user across the user's keys), both **in-memory, no
Redis** (verified). *Not per-job keys:* enforcement lives on the shared team/user
objects, so one key per (user, project) gives identical enforcement without per-job
mint/revoke churn — the doc's "per-session to cut churn" steer (per-job *attribution* is
deferred to the Slice-4 ledger). Jobs missing a user or project fall back to the fleet
key (still aggregate-capped). Policy source = `litellm.ratePolicy` value →
`LITELLM_RATE_POLICY` env JSON (`{categories, projects, users}`); the orchestrator
**expands category → model_names with a validation guard** (an entry naming an
unregistered model is dropped + warned, never silently unthrottled — capability gap 1).
Impl: `ensure_scoped_key` + `upsert_team`/`upsert_internal_user`/`upsert_scoped_key` in
`litellm_gateway.py`; routing via `_gateway_routing_target_scoped` wired into both
`_inject_dispatch_credentials` (worker jobs) and `_inject_thread_dispatch_credentials`
(sessions), threaded to every model section via a `gateway_override`. **Hash-gated** so a
repeat dispatch with unchanged policy issues no gateway calls. *Verified live (k3d, real
`ensure_scoped_key` against the gateway):* team created with `model_rpm_limit
{gemma:5}` (ghost model filtered by the guard), key bound to `team_id` + `user_id`,
internal user `rpm_limit=50`, **8 reqs through the scoped key → `[200,200,429×6]`**
(team limit binds); 26 new unit tests (60 total). *Scope:* per-user **per-model** limits
deferred (would need one-key-per-user, which conflicts with per-project teams — a LiteLLM
modeling limit); per-user is flat for now.

> **⚠️ TODO — measure real capacity before turning the throttle on (deferred 2026-06-22).**
> Both knobs ship **inert** (`litellm.backstop: {}`, `litellm.ratePolicy: {}`): scoped keys
> mint, agents are off the admin master key, and traffic is attributed per user/project —
> but **nothing is actually rate-capped until real numbers are set.** Before enabling
> enforcement, measure the homelab router's (strix box's) safe sustained RPM **and** each
> provider / subscription-plan tier's real limits, then fill in `litellm.backstop`
> (aggregate capacity) + `litellm.ratePolicy` (per-project / per-user caps). Until then this
> feature is **measurement + attribution only**, not throttling. (Same inert-until-measured
> state as 2a's backstop — folded into one task.)

**Decision on the aggregate backstop (2a) vs scoped keys (2b):** an agent presents one
key, so the fleet-key bucket and a scoped key are mutually exclusive per request. The
fleet key stays the **fallback** (no user/project, or gateway blip); for scoped traffic
the aggregate ceiling is **`Σ(project team caps) ≤ capacity`, an admin invariant** (a true
global ceiling needs Redis at multi-replica — Scaling §). Honest for single-replica v1.

**✅ Slice 3 — longer-window quota stop (orchestrator-enforced).** Unlike 1/2 (LiteLLM
enforces in-band), the daily quota is **orchestrator-enforced** because LiteLLM's only
long-window cap is dollar-denominated (gap 2). A `quota_poll_loop` (120 s, beside the
catalog sync) reads each active project's **daily** usage from `/team/daily/activity`
(gap 4 resolved — the non-enterprise per-team endpoint; `/spend/report` is Enterprise-
gated, `/spend/logs` dollar-only, and per-**user** activity didn't populate off
key-ownership, so v1 is **per-project only**). Over-quota → two enforcement points sharing
one in-memory set: the loop **freezes** the project's running jobs (`pause_job` +
`update_job_status(freeze_data=quota_exceeded)` + `release_workspace`, all existing
primitives) and the **dispatcher skips** that project (else a paused job re-dispatches in
~5 s and keeps consuming). The daily UTC reset auto-clears the set → frozen jobs
re-dispatch at 00:00 UTC (the midnight-flood follow-up is **filed**, per the acceptance
criteria's "applied *or* filed"). Window = UTC day (rolling-N-hour deferred). Policy =
`litellm.quota` → `LITELLM_QUOTA` env, default `{}` = inert. *Verified live:* the
over/under decision logic (over@quota=3, under@quota=100, inert at no policy) **and** —
after the `team_ids` read-path fix below — correct **per-team scoping** (a 6-request team
reads back exactly 6, a zero-traffic team 0); poll loop confirmed running in-cluster; 16
new unit tests (76 total). *Deferred:* full job-freeze E2E (needs a real dispatched job —
freeze reuses proven primitives, same bar as Slices 1/2b); per-user quota.

> **Read-path bug caught + fixed in verification (Slice 3).** `/team/daily/activity`'s
> filter param is **`team_ids` (plural)** — the singular `team_id` is **silently ignored**
> and returns the *global* all-team total. The first cut used `team_id`, so every project
> read the same global number (a global quota mislabeled as per-project — one busy project
> would have frozen all of them). **Fixed to `team_ids`** and re-verified live: a team that
> fired 6 requests reads back **exactly 6** (1:1, not inflated — the earlier "~3×" was this
> global leak), a zero-traffic team reads **0** (isolated). Minor caveat that remains: a
> short aggregation **lag** (~tens of seconds) before served requests appear — fine for a
> daily cap. `successful_requests` (not `api_requests`) is summed so our own 429s don't
> count.

**✅ Slice 4 — unified usage ledger + workspace metering + Cockpit view.** The durable,
queryable record of usage (LLM tokens + workspace compute) is the append-only
`usage_events` ledger in **`srw-auditdb`** (the *locked* schema from
[[observability_and_quotas]]); LiteLLM's own DB stays **enforcement-only** (Scaling §2).
Implemented as four independently-shippable sub-slices, **all k3d-verified**:
- **4a — ledger foundation.** `migrations/audit/0002_usage_events.sql` (partitioned on
  `ts`, registered in `audit_partitions.py` via a conservative per-parent partition-column
  map; at-least-once dedupe on `(source, source_id, unit, ts)`), `usage_rates`
  (`app/0033`, effective-dated, ships **empty/inert** — same posture as the throttle
  knobs), a `UsageLedger` writer (snapshots the rate onto each row; **per-row fallback** so
  one uninsertable row can't sink a batch or wedge the poller) + a `UsageRates` resolver,
  and `GET /api/usage` (G5-visibility-scoped aggregate by category/unit). *Verified:*
  migrations applied live on the dev auditdb/app DB; 11 unit tests + the partition
  machinery (testcontainers, real migration runner).
- **4b — workspace compute metering.** `workspace_intervals` (`app/0034`) records a pod's
  open→close (**requests × wall-clock**, decision 5); the container provisioner emits open
  at create + close at delete (the funnel for release *and* suspend, so suspend/restore
  bills only live periods); a loop materializes CLOSED intervals into `vcpu-hour` +
  `gib-hour` ledger rows + reconciles leaked opens (bounded at a cap). *Verified live:* a
  synthetic 2h / 500m·1Gi interval materialized **cross-DB** (app pool → audit pool) into
  exactly `vcpu-hour=1.0` + `gib-hour=2.0`; 6 unit tests. *(Container/sandbox tier only;
  VM-tier emits are an additive follow-up — the table + loop are tier-agnostic.)*
- **4c — LLM materialization.** A poll loop pulls LiteLLM `/spend/logs` → `category='llm'`
  rows (prompt-token + completion-token), **attributed per user/project** by parsing the
  scoped key's `user=srw-user-<uuid>` / `team_id=srw-proj-<uuid>` off each row (the
  `api_key` field is hashed, unusable; a UUID guard rejects non-scoped / test ids so they
  never poison the ledger). Idempotent (ledger dedupe + an in-memory `startTime` cursor).
  *Verified live:* a real-UUID scoped-key gemma request round-tripped spend-log → poll →
  **2 correctly-attributed ledger rows** (prompt 18 + completion 5, carrying user_id **and**
  project_id); 76 historical spend-log rows materialized; 8 unit tests.
- **4d — Cockpit view.** Admin → Usage (read-only, reads `GET /api/usage`): tokens +
  compute by category/unit over a 7/30/90-day window, G5-scoped. *Verified:* prod build
  compiles the component + route + nav; 3 vitest tests. *(The `usage_daily` rollup + the
  per-day / per-user / per-job breakdowns the doc sketches are deferred — the raw indexed,
  partition-pruned query is instant at v1 scale.)*

> **⚠️ v1 limitation — per-job LLM attribution.** Compute rows carry `ref_id` = job/thread,
> but LLM rows do **not** (the gateway never sees `job_id` — it's stripped at the wire
> boundary; only the scoped key's user/project reach the spend log). So a per-**job** cost
> line covers compute fully and LLM only at the user/project level. Closing it needs the
> orchestrator to tag each agent request with `job_id` via LiteLLM request metadata — a
> deferred follow-up. Per-user/project LLM attribution (the headline) works today.

**Spike findings that revised the design:**
- **Per-deployment `rpm`/`tpm` does NOT 429** without **Redis + `enable_pre_call_check`/
  `enforce_model_rate_limits`** (v1 has no Redis) — it's router/load-balancing metadata.
  → **Decision 5a revised:** backstop is the fleet key, not a `config.yaml` number.
- **The admin master key bypasses all rate limits** → it can't be the agent credential
  (hence the fleet key).
- **Key/team `model_rpm_limit` DOES enforce in-memory** (no Redis) → `429 + Retry-After:
  60`. The gap-3 worry (per-model key limits unreliable, #10052) did **not** reproduce —
  no `enforce_model_rate_limits` needed for key-level limits. **2b extends this (verified
  2026-06-22):** team `model_rpm_limit` **and** internal-user flat `rpm` both enforce
  in-memory too, and **compose on one scoped key** (key bound to both). One caveat: the
  **team** limiter trips *stricter* than nominal (rpm=5 cut at ~2) — fine for a protective
  throttle (agents back off), but don't read team caps as exact.
- `/key/generate` accepts a **custom key value** (→ deterministic fleet key); an **empty
  `model_rpm_limit` clears** a cap (so `/key/update` must always send the limit dicts,
  since omitted fields keep old values).

**Gotchas worth keeping:**
- LiteLLM first boot **OOMKilled at 1Gi** (Prisma-migrate + proxy spike) → chart default
  raised to **2Gi + `--num_workers 1`** (each uvicorn worker reloads the whole app).
- LiteLLM serves OpenAI under `/v1`, admin/health at root — routing appends `/v1`, the
  admin client uses the bare base URL.
- Dashboard is ClusterIP-only: `kubectl -n srw port-forward svc/srw-litellm 4000:4000` →
  `/ui` (master-key login).
- If multi-replica is ever needed (shared counters), use **Valkey** (BSD, drop-in);
  Postgres can't sub for LiteLLM's hot-path counters (Redis-hardcoded + hot-row write
  antipattern).

**Next:** commit 2b + 3 + 4; set a real `backstop` + `ratePolicy` + `quota` capacity
(measure the strix box's safe RPM / daily volume — counts are 1:1 now the `team_ids` bug is
fixed) **and** seed `usage_rates` to turn the ledger's $0 costs into real dollars.
*Deferred:* **per-job LLM attribution** (tag agent requests with `job_id` via LiteLLM
request metadata — the gateway never sees job_id today); **VM-tier compute metering**
(additive — the same `workspace_intervals` path); the `usage_daily` rollup + per-day /
per-user / per-job Cockpit breakdowns; a live authed-UI screenshot of the Usage view;
per-user **per-model** rate limits + per-user **quota**; rolling-N-hour quota window; a
DB-table + Cockpit-UI policy source (file/values-driven for now); full job-level dispatch/
freeze E2E (all reuse verified primitives — same bar as Slice 1's "real JOB through
`_inject_dispatch_credentials`").

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
| 5 | Limit responses | **RPM/TPM breach → 429 → agent backoff (slow down), in-band** — *shipped Slice 2b.* **Longer-window quota breach → hard stop + pod teardown, orchestrator-driven (out-of-band)** — *shipped Slice 3 (per-project daily; freeze = `pause_job` + `release_workspace` + dispatcher gate; per-user deferred).* |
| 5a | Upstream backstop | **REVISED — k3d-verified 2026-06-21.** Per-deployment `rpm`/`tpm` in `config.yaml` does **NOT** 429 in our setup (it's router/load-balancing metadata; hard pre-call enforcement needs **Redis + `enable_pre_call_check`/`enforce_model_rate_limits`**, and the **admin master key bypasses all limits**). What **does** enforce in-memory (no Redis) is **key/team `model_rpm_limit`** (proven 429 + Retry-After). So the backstop is a **shared "fleet" virtual key** — deterministic value (HMAC of the master key, no persistence), `model_rpm_limit = upstream capacity`, scoped to registered models — that all agents use **instead of the admin master key** (security win). Fleet aggregate → one bucket → 429 → backoff. Capacity = the `litellm.backstop` knob (`{model_id\|'*':{rpm/tpm}}`). **Implemented (Slice 2a).** |
| 6 | Identity | **REVISED — k3d-verified 2026-06-22 (Slice 2b).** *Not* per-job — a **per-(user, project) scoped key** (deterministic, like the fleet key), bound to a LiteLLM **team** (= project) + **internal user** (= user) that carry the limits. Enforcement aggregates on the shared team/user objects, so one key per (user, project) gives identical limits **without** per-job mint/revoke churn; per-job *attribution* deferred to the Slice-4 ledger. Falls back to the fleet key (5a) when user/project is absent. |
| 7 | Monitoring (v1) | LiteLLM's **native dashboard** (RPM/TPM/usage per key/user/model). Build no custom UI for Slices 1–3. |
| 8 | Store of record | **shipped Slice 4** — canonical rows in **`usage_events`** (`srw-auditdb`): LLM via spend-log materialization, compute via interval emit; LiteLLM's internal DB = enforcement-only. |
| 9 | Metered scope | **shipped Slice 4** — LLM (gateway) + **workspace** compute (container tier; requests × wall-clock). Agent pods / query / storage still ignored (the `category` taxonomy reserves them). |
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
4. **Usage polling:** ~~confirm the exact read path at build~~ → **RESOLVED (Slice 3):
   `GET /team/daily/activity?team_ids=<id>`** (per-team daily `successful_requests` +
   `total_tokens`, non-enterprise). Rejected at build: `/spend/report` (Enterprise-gated),
   `/spend/logs` (dollar-only, `0.0` for unpriced homelab models), per-**user**
   `/user/daily/activity` (didn't populate off key-ownership). **Param gotcha:** the filter
   is `team_ids` (plural); singular `team_id` is silently ignored → returns the *global*
   total (a bug caught in verification — see Implementation status). Counts are 1:1; minor
   aggregation lag.

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

> **Implemented (Slice 4, 2026-06-22) — the design held, one realization detail.** We run
> **stock LiteLLM** (no custom callback), so the gateway doesn't buffer/emit; instead the
> **orchestrator polls `/spend/logs`** and writes the LLM rows. The **idempotent dedupe**
> on `(source, source_id, unit, ts)` provides the at-least-once guarantee in place of
> gateway buffering. Compute is exactly as designed (orchestrator open/close into
> `workspace_intervals` + a materialize/reconcile loop). Attribution caveat: LLM rows
> self-attribute to **user/project** via the scoped key's ids — **not job** (the gateway
> never sees `job_id`); compute rows do carry the job/thread ref. Per-job LLM attribution
> is the one deferred gap.

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
  ("and, later, the usage-metering ledger", `postgres-audit.yaml:3-7`). Slice 4 (shipped)
  = **0 new DBs, 3 new tables** (`usage_events` in `srw-auditdb`; `usage_rates` +
  `workspace_intervals` in the app DB — the two app-side tables were small enough to not
  warrant their own server).

### New tables in *our* schema

- **Slices 1–3 (shipped):** **none** — keys/limits/spend live in LiteLLM's DB, and the
  category→model + per-user/project limit policy stayed **YAML** (`litellm.ratePolicy` /
  `litellm.quota` env, Open Q4 resolved file-driven), exactly as the "possibly zero" call
  predicted.
- **Slice 4 (✅ shipped 2026-06-22):** **+3 tables** — `usage_events` (`audit/0002`, the
  append-only ledger) + `usage_rates` (`app/0033`, effective-dated rate config, ships
  empty/inert) + `workspace_intervals` (`app/0034`, mutable compute open/close
  bookkeeping). Plus the `/spend/logs` materializer, the compute emit + reconcile loop,
  and the Cockpit Usage view.

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

### Slice 1 — Gateway + RPM/TPM visibility — ✅ IMPLEMENTED (k3d-verified 2026-06-21)
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
> **2a (aggregate backstop, shared fleet key) ✅ + 2b (per-user/project, scoped keys) ✅
> IMPLEMENTED + k3d-verified** (2a 2026-06-21, 2b 2026-06-22 — see Implementation status +
> decision 5a). 2a is committed; 2b is uncommitted on `develop`. The mapping below is what
> shipped (project → team, user → internal user) — minus per-job keys, which 2b replaced
> with one scoped key per (user, project) since enforcement lives on the team/user objects.
- Admin config (ours): model → **category** map + a **RPM/TPM per category**,
  settable per **user** and per **project**, with per-model override.
- Map to LiteLLM: **project → team** (`model_rpm_limit` dict), **user → internal
  user**, **job → virtual key** (tagged user_id+team_id) minted at dispatch,
  revoked on completion. Orchestrator **expands category → exact model_names**
  when writing the dicts + validates none silently skip. *(Prereq: per-job client
  rebuild, [[agent_loop_mode_pod_reuse]] — **RESOLVED 2026-06-21**: agent already
  rebuilds its LLM clients per dispatch.)*
- ~~Set per-deployment `rpm`/`tpm` in `config.yaml`~~ — **revised (done in 2a):**
  per-deployment rpm doesn't 429 without Redis; the aggregate backstop is the shared
  fleet key's `model_rpm_limit` instead (decision 5a). 2b adds the per-user/project
  layer on top: **project → team** `model_rpm_limit` (aggregates per-model), **user →
  internal user** (flat), **job → key** tagged user+team.
- Breach → `429 + short Retry-After` → agent's existing backoff slows it down.
- **Acceptance (enforcement is a gate, not faith — gap 3):** a configured per-user
  limit on a model **actually returns 429** under load in our deployment mode
  (verify `enforce_model_rate_limits` / DB-mode enforcement); agents wait via
  429+retry rather than erroring; raising/lowering the limit visibly changes
  throughput; a deliberately-misnamed category entry is caught by the validation
  guard, not silently unthrottled.

### Slice 3 — Longer-window quota stop *(orchestrator-enforced)*
> **✅ IMPLEMENTED + k3d-verified (read path live) 2026-06-22.** Per-**project** daily
> quota (per-user deferred — read path unreliable); window = UTC day (rolling filed).
> See Implementation status for what shipped + the `team_ids` read-path fix.
- Per-project **daily request/token quota** (per-user deferred). **Necessarily
  orchestrator-side** — LiteLLM's only long-window cap is dollar-denominated (capability
  gap 2), and we want request/token counts.
- Orchestrator polls LiteLLM usage via **`/team/daily/activity`** (gap 4 resolved: the
  non-enterprise per-team activity endpoint — `/spend/report` is Enterprise-gated,
  `/spend/logs` is dollar-only, per-user activity didn't populate). One read per active
  project (bounded by active jobs; `SELECT DISTINCT` at scale). Over-quota → freeze the
  project's jobs (`pause_job` + `release_workspace`) **and** the dispatcher skips
  re-dispatching them (an in-memory over-quota set the poll loop owns) — without the gate,
  a paused job re-dispatches in ~5 s and keeps consuming.
- **Acceptance:** a project crossing the quota has in-flight jobs frozen + workspaces
  released *(freeze reuses proven primitives; the read/decision/poll path is live-verified,
  full job-freeze E2E deferred to a real dispatched job — same bar as Slices 1/2b)*;
  throttle (Slice 2) and quota-stop are distinguishable (429-slows vs pause-stops, tagged
  `freeze_data.type=quota_exceeded`); midnight-flood mitigation **filed** (rolling window —
  the daily UTC reset means frozen projects auto-unfreeze + re-dispatch at 00:00 UTC).

### Slice 4 — Unified ledger + workspace metering + Cockpit view
> **✅ IMPLEMENTED + k3d-verified 2026-06-22** (4a–4d — see Implementation status). One
> design change vs the sketch below: LLM rows are **materialized by polling `/spend/logs`**,
> not a LiteLLM per-request callback (we run the stock image) — the same orchestrator-polls-
> the-gateway shape as Slice 3, and exactly the Scaling-§2 "LiteLLM DB enforcement-only,
> `usage_events` durable" design.
- ~~gateway per-request callback~~ → **orchestrator polls `/spend/logs`** → canonical
  `category='llm'` rows into `usage_events` (attributed per user/project via the scoped
  key's deterministic ids); LiteLLM DB → enforcement-only.
- Orchestrator emits `category='compute'` workspace open/close intervals
  (`requests × wall-clock`) + open-interval reconciler. **Done** (container/sandbox tier).
- Admin "Usage" view (by category/unit over a window, G5 visibility model). **Done.**
- **Acceptance:** a window query returns LLM + workspace usage by category, G5-scoped
  (`GET /api/usage`). *Caveat:* a per-**job** total covers compute fully but LLM only at the
  user/project level (job_id never reaches the gateway — see the v1-limitation callout).

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
  are config sugar (4a); upstream ceiling handled by the **shared fleet key**
  (5a, revised — *not* per-deployment `config.yaml` rpm, which doesn't enforce),
  not a user-facing app cap.
- ~~LiteLLM expresses the matrix?~~ → **yes**, via team/user/key (capability
  check), with three gaps now owned as orchestrator-side work (category
  expansion + validation, request-count quota, enforcement load-test).

## Open questions

1. **Limit unit:** **requests** (RPM) only, or also **tokens** (TPM)? Both are
   native. *Lean: RPM first (matches the "requests per minute" framing); add TPM
   for a token-heavy category when needed.*
2. **Quota window:** ~~daily vs rolling N-hour vs weekly~~ → **v1 shipped daily (UTC)**
   (Slice 3); the `/team/daily/activity` read path is day-bucketed, and the daily reset
   gives free auto-unfreeze. **Rolling N-hour deferred** (the midnight-flood mitigation —
   needs finer-grained reads, e.g. raw spend logs or the Slice-4 ledger). Still
   orchestrator-enforced (gap 2).
3. ~~**User vs project precedence**~~ → **RESOLVED (Slice 2b).** All three scopes
   bind independently and the **tightest wins**: the scoped key inherits both its
   **team** (project, per-model) and **internal-user** (user, flat) limits, plus the
   aggregate fleet/team ceiling — verified composing on one key (429 at whichever
   trips first). Per-user is flat for now (per-user per-model deferred — needs
   one-key-per-user, which conflicts with per-project teams).
4. ~~**Category definition home**~~ → **RESOLVED for v1: file/values-driven.** The
   category→model map + per-project/per-user limits live in the `litellm.ratePolicy`
   helm value (`LITELLM_RATE_POLICY` env JSON); the orchestrator expands
   category→model_names with a validation guard. A DB table + Cockpit admin UI can
   replace the *source* later without touching the enforcement plumbing.

## References

- [[observability_and_quotas]] — the `usage_events` ledger spine (Slice 4).
- [[saas_billing_and_metering]] — the dollar-billing follow-on (disjoint now).
- [[credential_broker]] — the gateway as note-1, operationally led.
- [[codex_proxy]] / [[agent_loop_mode_pod_reuse]] — CLIProxyAPI placement; per-job
  client-rebuild prerequisite.
- LiteLLM Proxy — virtual keys, per-key/user/model RPM/TPM, usage tracking.
