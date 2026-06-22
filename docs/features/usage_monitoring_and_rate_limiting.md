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

**Status:** **Slices 1 + 2a committed; Slice 2b implemented + k3d-verified (2026-06-22), uncommitted on `develop`.** Slices 3–4 pending. See **Implementation status** below for what shipped, the spike findings that revised the design, and the gotchas.
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

## Implementation status (updated 2026-06-21)

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

**Decision on the aggregate backstop (2a) vs scoped keys (2b):** an agent presents one
key, so the fleet-key bucket and a scoped key are mutually exclusive per request. The
fleet key stays the **fallback** (no user/project, or gateway blip); for scoped traffic
the aggregate ceiling is **`Σ(project team caps) ≤ capacity`, an admin invariant** (a true
global ceiling needs Redis at multi-replica — Scaling §). Honest for single-replica v1.

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

**Next:** commit 2b; set a real `backstop` + `ratePolicy` capacity (or measure the strix
box's safe RPM); Slice 3 (long-window quota stop); Slice 4 (ledger + Cockpit view).
*Deferred within 2b:* per-user **per-model** limits; a DB-table + Cockpit-UI policy source
(file/values-driven for now); full job-dispatch E2E through `_gateway_routing_target_scoped`
(the helper is verified live; the dispatch glue is unit + inspection-proven, same caveat as
Slice 1's "real JOB through `_inject_dispatch_credentials`").

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
