---
tags:
  - feature
  - observability
  - metering
  - cost-tracking
  - quotas
  - usage-ledger
related:
  - "[[saas_billing_and_metering]]"
  - "[[multi_tenancy]]"
  - "[[high_availability_setup]]"
  - "[[config_matrix_db_overrides]]"
  - "[[workspace_network_isolation]]"
aliases:
  - usage metering
  - cost tracking
  - usage ledger
  - soft quotas
  - infra monitoring
---

# Observability & Quotas (self-owned usage metering + ops monitoring)

> Design doc — captured 2026-06-02 from the "quotas + basic monitoring" planning
> conversation. This is the **observability-and-soft-quota** layer: it meters and
> surfaces cost, and *alerts* when a user/project crosses a threshold. It deliberately
> does **not** block anything. Hard-stop enforcement and the pre-pay wallet are the
> documented follow-on in [[saas_billing_and_metering]] — this doc builds the ledger
> they will read and debit from.

**Status:** **Ledger spine + LLM + workspace-compute metering + a Cockpit usage view are
IMPLEMENTED + k3d-verified (2026-06-22)** as **Slice 4 of
[[usage_monitoring_and_rate_limiting]]** (see its Implementation status for the verification
detail). This doc stays the **schema + decisions source of truth** (`usage_events`,
`usage_rates`, the rate table, the open-interval reconciler) — the implementation note just
below records what's built, what's deferred, and the one design change.

**Successor state (2026-08-07):**
[`infrastructure_resource_metering.md`](infrastructure_resource_metering.md)
Slices 0–3 are implementation-complete in the repository through app migration
`0114`. In addition to the typed v2 ledger/read model, workspace-Pod path, and
separate PVC/PV lifecycles, Slice 3 adds gated agent/IDE Pod intervals,
authenticated VM/VMI lifecycle capture, exact root-storage attribution, and
per-source activation/recovery controls. Tilt now bakes metering-package changes
atomically, both orchestrator Dockerfiles enforce a build-time Slice 3 import,
and the repeated dark k3d rollout is healthy. Configured
collection/publication gates remain off, although prior local tests left durable
activation rows in `shadow`. The compatibility ledger remains authoritative.
The Slice 3 source is merged on `develop`, while observed main dev remains on
`sha-a4d1fab` with migrations through `0102` and Pod inventory only; its
agent/IDE rollout and shadow approval remain pending. Live VM objects have been
normalized read-only, but the VM cluster's old controller and absent collector
leave VMI/root-storage rollout and shadow approval pending. Shared-platform
coverage is Slice 4; provider adapters are incomplete Slice 5 work; utilization
is Slice 6. This infrastructure Slice 3 must not be confused with this
document's still-deferred **Slice 3 query /
utility metering**.

**Triggered by:** Approaching enterprise/SaaS readiness. Before plans/billing exist we
need (a) per-user cost attribution that works on *any* cluster (homelab today, AWS
later), and (b) basic infra-ops monitoring. Locked in conversation that this round is
**all read-side** — metering + dashboards + soft-quota *alerts*, no enforcement gates.

## Implementation note (2026-06-22) — what the rate-limiting Slice 4 built

The ledger this doc designed now exists, built as Slice 4 of
[[usage_monitoring_and_rate_limiting]]. Later removal of the proxy/gateway and
the audit-based materializer changed where LLM cost is sourced — see below:

- **`usage_events`** — shipped verbatim from this doc's schema as
  `migrations/audit/0002_usage_events.sql` (monthly-partitioned on `ts` in `srw-auditdb`,
  via the audit-store partition machinery). **`usage_rates`** shipped as
  `migrations/app/0033` (effective-dated $/unit rates).
  > **As-built update (gateway removed, `remove_litellm_proxy_and_gateway_concept.md` P1;
  > rollup added Phase 6, 2026-07-03).** The LiteLLM gateway is gone. LLM cost is
  > priced from `usage_rates` again, whose **LLM rates (prompt/completion-token) are
  > now auto-seeded** from OpenRouter's catalog by
  > `openrouter_pricing.llm_pricing_sync_loop` — not admin-seeded, not empty.
  > Canonical compute rates (vcpu/gib-hour) stay unseeded, so those rows meter
  > quantity with `cost_usd` NULL until a rate exists; an unpriced (category,
  > resource, unit) resolves to no rate and the quantity still meters immediately.
  > **Added 2026-08-05:** [[cloud_equivalent_usage_pricing]] keeps separate,
  > effective-dated STACKIT/AWS/Azure rate cards and reprices those quantities at
  > read time for planning. It never writes the comparison estimate back onto
  > `usage_events` or presents it as canonical customer spend.
- **LLM rows — DESIGN CHANGE.** The current implementation materializes token
  usage from the `llm_requests` audit trail through
  `orchestrator/services/audit_usage.py`; it no longer depends on a proxy or
  gateway. Worker job IDs and persistent-session thread IDs resolve to
  user/project attribution and are retained as `ref_kind` / `ref_id`, so
  per-job/thread ledger queries are supported. The ledger snapshots rates from
  the OpenRouter-seeded `usage_rates` table.
- **Workspace compute** — the active compatibility writer uses
  `workspace_intervals` (`app/0034`) and requests × wall-clock to emit
  `vcpu-hour` + `gib-hour`. Its typed successor is implemented through Slice 1,
  but remains behind the durable cutover/publication gates. Agent/IDE Pod and
  VM/VMI/root-storage capture is now implemented by infrastructure-metering
  Slice 3, but it is not operationally shadow-approved, published, or visible on
  this compatibility serving path.
- **Cockpit "Usage" view** (this doc's Slice 4) — shipped, reads `GET /api/usage`.
- **Deferred:** query metering (**Slice 3 in this document**), soft-quota
  *alerts* (Slice 5 here), operational agent/IDE/VM and PVC/PV shadow approval
  and publication, provider pricing adapters, shared-platform coverage, and
  utilization overlays. The legacy
  `usage_daily` rollup and typed `usage_daily_v2` machinery are built; the latter
  is gated with the infrastructure-v2 path. The `quota_limits` table is unbuilt;
  note the **rate-limiting doc already ships a different,
  enforcing daily quota** (its Slice 3, orchestrator-driven freeze) — distinct from this
  doc's planned soft-alert quota.

## Why self-owned metering (not cloud billing)

Cloud billing (AWS CUR / Cost Explorer, GCP billing export) is **account-level and
~24h delayed**. It cannot see our `user → job → pod` mapping and is far too slow to
feed real-time quotas or a wallet. Per-user, near-real-time cost attribution *must* be
metered at the application layer — and once it is, it's cloud-agnostic by construction:
the same ledger works on homelab or a customer's own AWS/GCP, only the **rate rows**
change. This is the cost-isolation equivalent of the data-isolation work in
[[multi_tenancy]] M1.

## Locked decisions

| # | Decision | Value |
|---|---|---|
| 1 | Metering ownership | App-layer, self-owned. Cloud-agnostic. Never derive per-user cost from cloud billing APIs. |
| 2 | This round's posture | Read-side only: meter + surface + **alert**. No dispatch blocking, no wallet. |
| 3 | Storage of record | **One append-only `usage_events` ledger** in the product's observability store **`srw-auditdb`** (in-chart plain Postgres, partition machinery from the audit-store work). *Amended 2026-06-11 — was: homelab `analytics` TimescaleDB. Reasons: shippability (the ledger is product billing substrate; `analytics` is homelab infra outside the chart) + TSL licensing for redistributed charts. See `database_architecture.md`. The homelab Timescale keeps ops analytics + $/vcpu-hour rate calibration.* |
| 4 | Cost taxonomy | Single ledger, `category ∈ {llm, compute, query, storage}` ("utility costs" = compute + query). |
| 5 | Compute cost basis | **Requests × wall-clock** (reserved capacity), not sampled utilization, not limits. |
| 6 | Rate config | One DB-backed, effective-dated rate table keyed by `(category, resource, unit)`, in the **app DB**. |
| 7 | Build order | Compute first (workspace → agent pods), then query/utility, then storage. LLM folded in early (data already exists). |
| 8 | Ops monitoring | Prometheus/Grafana for cluster health + *utilization* only — **not** the cost system of record. Resume the half-finished kube-prometheus-stack migration item. |
| 9 | Surfaces | Cost → Cockpit (product). Ops → Grafana (ops). |
| 10 | Enforcement / wallet | Out of scope → [[saas_billing_and_metering]]. This doc builds the ledger it reads. |

## Architecture

### The spine: one usage ledger

Every metered thing — an LLM call, a workspace pod's lifetime, a Neo4j query — becomes
a row in a single append-only hypertable. **One row per resource *dimension*** (so a
pod emits a vCPU-hours row and a GiB-hours row; an LLM call emits a prompt-tokens row
and a completion-tokens row), which keeps `quantity/unit/rate/cost` scalar and maps 1:1
to the rate table.

```
-- srw-auditdb (observability store; amended 2026-06-11, was analytics TimescaleDB),
-- schema owned by migrations/audit/ (lands as 0002 with Slice 1)
usage_events  -- monthly range-partitioned on ts (audit-store partition machinery)
  id           bigint
  ts           timestamptz        -- when the usage occurred / interval end
  user_id      uuid               -- attribution target (nullable for system events)
  project_id   uuid
  ref_kind     text               -- 'job' | 'thread'
  ref_id       uuid               -- job_id or thread_id
  category     text               -- 'llm' | 'compute' | 'query' | 'storage'
  resource     text               -- 'workspace_pod'|'agent_pod'|'neo4j'|'pgvector'|'<model_id>'
  quantity     numeric            -- e.g. 1.83 (vcpu-hours), 4096 (tokens)
  unit         text               -- 'vcpu-hour'|'gib-hour'|'prompt-token'|'completion-token'|'query'|'gib-month'
  rate_usd     numeric            -- snapshot of the rate applied (audit: never recompute history)
  cost_usd     numeric            -- quantity * rate_usd, stored
  details      jsonb              -- {pod_name, started_at, ended_at, cpu_millicores, mem_bytes, model, ...}
```

- **`rate_usd` is snapshotted onto the row.** History is immutable; changing a rate
  never rewrites past cost. (Same principle as storing `cost_usd` rather than joining
  at read time.)
- An **orchestrator-timer rollup** (plain SQL over recent partitions; same
  pattern as the audit store's maintenance loop) maintains
  `usage_daily(day, user_id, project_id, category, resource, unit, quantity,
  cost_usd, events)` in the app DB — **BUILT** (`migrations/app/0047`,
  `services/usage_rollup.py`; watermarked by `rollup_state`). Cheap to read; the
  raw events are the rollup SOURCE and are **never auto-deleted** (no-auto-deletion
  policy — manual export-first only). *(Amended 2026-07-03 — built; dims are the
  full superset; retention → no-deletion policy. Was TimescaleDB caggs, 2026-06-11.)*

### Where it lives, and the cross-DB join

| Data | Store | Why |
|---|---|---|
| `usage_events` (high volume, time-series) | **`srw-auditdb`** (in-chart observability store) | Keeps the firehose off the control plane, ships with the product, reuses the audit store's pool/partition machinery (retention is a no-auto-deletion policy). *(Amended 2026-06-11 — was `analytics` TimescaleDB; see `database_architecture.md`.)* |
| `usage_daily` rollup mirror (small, joinable) | **App DB** | The Cockpit dashboard must join usage with user/job/project names. The rollup task (`services/usage_rollup.py`) full-replace upserts closed days here + advances the `rollup_state` watermark; `/api/usage` serves it for closed days, raw `usage_events` for the open tail. |
| `usage_rates` (config) | **App DB** | Small relational config; LLM rates auto-seeded from OpenRouter (`openrouter_pricing`), compute rates admin-settable. Fits the [[config_matrix_db_overrides]] DB-override pattern. |
| `usage_rate_cards` + `usage_rate_card_rates` (comparison config) | **App DB** | Effective-dated public-cloud list-price cards used only to revalue measured quantities in `GET /api/usage`. AWS/Azure refresh from official machine-readable sources; STACKIT is source-labelled from its public PDF. See [[cloud_equivalent_usage_pricing]]. |
| `quota_limits` (config) | **App DB** | **Planned, not built** — the rate-limiting v2 concern (`rate_limiting_and_plan_quotas.md`), scope-polymorphic `(scope_kind, scope_id, period, limit_usd, …)`. |

New wiring required: **none beyond the audit-store foundation** *(amended
2026-06-11)* — the orchestrator's auditdb pool, the `migrations/audit/` family,
and the partition machinery all come from `postgres_audit_store_implementation.md`
PR 1; `usage_events` is an additive `0002` migration. The old plan's
`datawarehouse-lb` gotcha (Patroni labels the leader `role=primary`, so the
chart's `timescaledb` ClusterIP has no endpoints) now matters only for
rate-calibration reads of pdu data, not for the ledger.

### The rate table

```
-- app DB
usage_rates
  category       text       -- 'compute' | 'llm' | 'query' | 'storage'
  resource       text       -- specific ('gpt-5.5') or '*' (all compute shares vcpu rate)
  unit           text       -- 'vcpu-hour' | 'gib-hour' | 'prompt-token' | ...
  rate_usd       numeric    -- price per unit
  effective_from timestamptz
  -- PK (category, resource, unit, effective_from); newest <= ts wins
```

- **Homelab rates can be measured, not guessed.** The `pdu-scraper` already logs real
  rack power per phase into this same `analytics` DB. Idle-vs-loaded watts ÷ schedulable
  capacity → an empirical `$/vcpu-hour` and `$/gib-hour` (× €/kWh, already configured at
  `0.32`). On AWS, swap in EC2/EKS list prices. Same ledger, different rate rows.

### Compute cost: requests × wall-clock

The cost basis is **requested** resources (what the scheduler reserves and what drives
node count → the bill), known at provision time — *not* sampled utilization (that's an
ops/right-sizing signal, see Track B). Workspace pods already declare requests today:
`container_provisioner.py:158-161` → workspace `cpu=500m / mem=1Gi` (limits `2000m/4Gi`),
IDE pods `250m/512Mi`. So **no precursor work** — the numbers are in the pod spec.

```
duration_h = (ended_at - started_at) / 3600
vcpu_hours = (cpu_millicores / 1000) * duration_h     -> usage_events row, unit='vcpu-hour'
gib_hours  = (mem_bytes / 2^30)     * duration_h       -> usage_events row, unit='gib-hour'
cost       = quantity * rate_usd (per row)
```

Lifecycle seam: `workspace_lifecycle.py` (`ensure_workspace` / `WorkspaceOwner`, which
already unifies `job` and `thread`/session). Emit an **open** event on create, **close**
it on teardown.

### Reliability: open-interval reconciler (design in from day one)

A start event with no stop (pod OOM-killed, node lost, teardown hook skipped) silently
mis-counts. The orchestrator already reconciles this class of failure for orphaned jobs
(agent-offline → auto-pause). The ledger needs the same janitor: a periodic sweep that
compares live pod ages (`metadata.creationTimestamp`) against open `usage_events`
intervals and closes stragglers. **Acceptance test:** delete a workspace pod out-of-band
→ reconciler closes its event with a bounded `ended_at`, no orphaned open interval, no
double-count on the next sweep.

## Slices (each independently shippable)

> **Implementation status (updated 2026-08-07):** the ledger foundation,
> audit-sourced LLM usage, legacy workspace compute, both rollup models, and the
> Cockpit view are built. The repository's typed successor is implemented
> through infrastructure-metering Slice 3/app `0114`: workspace and agent/IDE
> Pods, separate PVC/PV assets, VM/VMI lifecycles, and exact root storage. Its
> repaired dev and production images pass the Slice 3 packaging/import contract,
> and the repeated dark k3d rollout is healthy; no durable cutover occurred.
> Main dev remains on `sha-a4d1fab`/`0102` with Pod inventory only, while
> agent/IDE and live-VM source rollout/shadow approval remain pending.
> **Deferred here:**
> this document's query metering, soft-quota alerts, infrastructure source
> rollout and publication, Slice 4 shared-platform coverage, incomplete Slice 5
> provider adapters, and utilization overlays. The slice text below is the
> original design record; its Slice 3 name refers to query/utility metering, not
> the successor's agent/VM Slice 3.

Each metering slice ships with at least a raw read endpoint so numbers can be verified;
the polished dashboard is its own slice.

### Slice 1 — Ledger foundation + workspace-pod compute  *(the starting point)*
- Stand up `analytics` connection + `usage_events` hypertable + continuous aggregate + `usage_daily` app-DB mirror.
- `usage_rates` table + seed compute rates (measured from pdu data or a placeholder).
- Emit open/close compute events from `workspace_lifecycle.py` for **jobs and sessions**.
- Open-interval reconciler.
- Raw read endpoint: `GET /api/usage?user=&project=&from=&to=` (G5 visibility-scoped).
- **Acceptance:** a completed job and a closed session each produce vcpu-hour + gib-hour rows with non-zero `cost_usd`; reconciler test passes; `/api/usage` returns per-user totals matching hand-computed `requests × duration × rate`.

### Slice 2 — Agent-pod compute + LLM (the freebie)
- Agent pods: same pattern via `agent_provisioner.py` (own requests/limits).
- LLM: tap the **token-capture point** — `src/core/archiver.py:393-403` already reads `token_usage` from `response.response_metadata` *before* it writes Mongo's `llm_requests`. Emit a `category='llm'` ledger row (prompt + completion) from that same source → rate × tokens. This sources from the LLM response, **not** by reading Mongo — so it adds no MongoDB coupling (see "Relationship to MongoDB retirement"). Optional transitional backfill from existing `llm_requests`.
- Finally populate the dead `jobs.total_tokens_used` / `total_requests` columns from the ledger.
- **Acceptance:** a job's total cost = compute (workspace + agent) + LLM, reconciled against the captured `token_usage` (cross-checked vs `llm_requests` during transition).

### Slice 3 — Query / utility metering
- Instrument the expensive shared-infra call sites: Neo4j graph queries, pgvector searches. Emit `category='query'` events (unit `query` or `query-second`; `pg_stat_statements` is already enabled on `analytics`, and could inform a Neo4j-side analogue).
- Rates per `resource` (`neo4j`, `pgvector`).
- **Acceptance:** a graph-heavy job shows a non-trivial `category='query'` line item ("the agent did lots of expensive graph queries").

### Slice 4 — Cost surfacing (Cockpit, product surface)
- Admin "Usage & Cost" view: per-user/project/day, broken down by category (LLM / compute / query / storage). Reuses the G5 visibility model already on the stats endpoints (`/api/stats/*`, `main.py:9855+`).
- Per-job cost line on the existing job detail/list — from **raw `usage_events` per `ref_id`** (indexed by `usage_events_ref_idx`), NOT `usage_daily` (`ref_id` is deliberately not a rollup dim; `/api/usage?ref_id=…` serves it).
- **Bonus value today:** even with zero external users this answers "which of *my* jobs/models/sessions burn my budget."

### Slice 5 — Soft quotas (alert, do not block)
- `quota_limits(scope_kind ∈ {user,project}, scope_id, period ∈ {day,month}, limit_usd, …)`, admin-set.
- On each `usage_daily` rollup refresh, compare rolling spend vs limit. On crossing a threshold (e.g. 80% / 100%), fire an **alert only** via the existing SSE + email notification path (`headless_notifications.py` / the automation-auto-disable pattern). No dispatch gate, no suspension.
- Admin UI to set limits; user-facing "you've used X% of your Y" banner.
- **Acceptance:** a user crossing 100% of a `day` limit gets exactly one SSE + email alert; jobs continue to run (this round is non-blocking by design).

## Track B — Infra-ops monitoring (parallel, ops-only)

Explicitly **separate** from the cost ledger. Prometheus/Grafana answer "is the cluster
healthy / am I over-provisioned," never "what does this user owe." (Prometheus
downsamples, expires, isn't transactional — wrong for a billing system of record.)
Utilization data here is still useful to the cost work — as the signal for *right-sizing*
the requests we bill on.

- **Resume the existing migration item:** `HomeLab/cluster_migration.md:109` still has
  `[ ] Install Rancher monitoring chart` unchecked, with values already saved at
  `HomeLab/rancher_cluster/OLD_rancher-monitoring_values.yaml`. Installing
  kube-prometheus-stack gives Prometheus + Grafana + Alertmanager + node/pod/cluster
  metrics out of the box.
- **Note — a Grafana already exists** (the `pdu-scraper` drives power/cost dashboards over
  `analytics`). Decide: consolidate onto the stack's bundled Grafana, or keep the
  existing one and add Prometheus as a datasource.
- **Instrument the orchestrator:** add `prometheus_client` + a real `/metrics` endpoint
  (request latency/error rate — there's already `time.perf_counter()` timing at
  `main.py:3662` to build on; job-pipeline gauges; dispatch-loop health). Make the
  agent's stub `/metrics` (`src/api/app.py:649`) real.
- **ServiceMonitor(s)** in `helm/` so Prometheus scrapes orchestrator + agent.
- A couple of starter Grafana dashboards (orchestrator throughput, job pipeline, agent fleet).
- *Optional convergence:* once Alertmanager is up, a soft-quota breach can also raise an
  Alertmanager alert — but the user/admin SSE+email notification (Slice 5) stays primary.

## Storage metering — successor design

> Originally captured here as a deferred note. The allocation design and its
> completed Slice 0–3 substrate now lives in
> [`infrastructure_resource_metering.md`](infrastructure_resource_metering.md).
> PVC/PV collection, lifecycle, activation, read/seal, and publication code is
> implementation-complete behind Slice 2 dark-launch gates, while Slice 3 adds
> exact VM root-storage capture behind independent source gates. The repaired
> image packaging and repeated dark k3d rollout passed. Main dev remains on
> `sha-a4d1fab`/`0102` with every storage gate off, live VM rollout/shadow
> validation is pending, and no storage event has become billing-authoritative.
> Whether storage becomes a customer charge remains conditional on the hosting
> model; truthful allocation visibility does not.

The implementation keeps logical PVC demand (`claim-requested`) non-additive
with provisioned PV/backend assets (`volume-provisioned`). CSI handles are HMAC'd
inside the collector and never persisted. Physical tiers come only from exact,
immutable operator-owned cluster/StorageClass/CSI-driver/volume-mode rules; the
collector does not read StorageClass parameters or CSIDriver objects, and
`unmapped_block_volume` is always unpriced. Any PV disappearance freezes at the
last proof and opens a backend-unverified gap until an audited fleet-admin
operator assertion closes it. Automated provider evidence, non-CSI reimport
deduplication, and provider rate cards are not implemented yet; a scheduled
activation also requires an orchestrator rollout after its UTC boundary so the
fixed enabled-resource set is rebuilt.

- ~~**Workspace storage today ≈ free.**~~ **No longer true (2026-08-04).** emptyDir
  is still the chart default, but `workspace.pvcEnabled` PVC-backs both jobs and
  sessions on `longhorn-ephemeral` (default `10Gi` per claim), and it is already
  ON in k3d dev and the homelab soak. Two things make this meterable in a way the
  old optional-PVC framing was not: a **session consumes two claims** (workspace
  pod + agent pod), and **session claims are released only when the thread row is
  hard-deleted** — an `ended` thread is resumable and keeps its volumes. So
  workspace storage no longer tracks concurrency; it tracks *retained threads*,
  and it accumulates. That is a real retained-storage allocation even before
  hosted storage is decided, and it is the first workspace-storage number worth
  putting on the dashboard (PVC count, current requested GiB, and GiB-hours by
  owner kind).
- **The real persistent storage is elsewhere:** OpenCloud, object stores (`minio` /
  `garage`), and PVCs. For PVC allocation, the
  [`infrastructure_resource_metering.md`](infrastructure_resource_metering.md)
  design records logical PVC demand as raw `storage/gib-hour` plus
  `claim-hour`, and physical PV/CSI assets separately as `storage/gib-hour`
  plus `volume-hour`. It derives GiB-month only for display and keeps claim,
  volume, and pod lifetimes independent. Other storage services need their own
  resource taxonomy before entering the ledger.
- **The BYO-cloud question (open):** if a customer brings their own cloud (their
  OpenCloud / Nextcloud / S3), the storage bill sits on *their* account — not ours to
  charge. In that mode storage metering is at most for the *user's own visibility*, not
  our cost recovery. If we host their storage, it's our cost and we bill it. So how much
  storage metering matters for cost recovery depends on whether hosted storage is
  part of the product or always BYO. That policy does not block allocation
  metering; it decides whether the result is customer visibility, our internal
  cost input, or eventually a billable measure.

## Relationship to MongoDB retirement

Resolves the "don't build more on MongoDB" concern *without* gating monitoring on a DB migration. Today Mongo holds three worker-job observability collections (`llm_requests`, `agent_audit`, `chat_history`) — all agent-side fire-and-forget, non-fatal, no transactional/product-critical writes; the interactive path already persists to Postgres `thread_messages`.

- **This layer is Mongo-independent.** LLM cost rows are emitted at the token-capture point (`src/core/archiver.py:393`, from `response.response_metadata`) — the *same upstream source* that feeds `llm_requests`, **not** a read of it. The `usage_events` ledger is in effect a better-typed, HA-capable `llm_requests`, so this work is the **first step of replacing** Mongo, not weight on top of it.
- **It also stands up the foundational store any Mongo retirement needs:** the orchestrator↔TimescaleDB connection, hypertable schema management, and the non-blocking event-write path. (Whether agents write the ledger directly — as they write Mongo today — or report through the orchestrator is a design call this layer settles; the migration of `agent_audit`/`llm_requests` then reuses whichever path we pick.) Monitoring-first de-risks and pays for that foundation; migrating-first builds the same foundation with no user-facing payoff in between.

**Amended 2026-06-11 — the consolidation initiative now exists and the
dependency inverted.** `postgres_audit_store_implementation.md` (validated
DDL, adapter spec, 7-PR plan) stands up `srw-auditdb` and migrates all three
collections there with wire parity; **this metering layer reuses *its*
foundation** (pool, migration family, partition machinery) rather than
building its own. Two deltas vs the sketch below as originally written:
- `agent_audit` + `llm_requests` → **`srw-auditdb` plain-Postgres partitioned
  tables** (not Timescale hypertables) — co-located with `usage_events` as
  envisioned, engine per `database_architecture.md` (TSL/shippability).
- `chat_history` → **stays a separate auditdb table with wire parity** for
  the migration; the `thread_messages` convergence ("one message store",
  G5 visibility model) is explicitly decoupled as a later *product*
  initiative — the byte-parity migration keeps that door open.

Cost driver: whether existing audit history must be preserved (dual-write + backfill) or a **forward-only cutover** is acceptable (far cheaper; fine on dev/thesis). Independent win that justifies eventual retirement: it removes today's single-replica, un-HA'd Mongo SPOF — the HA roadmap invests in CloudNativePG for Postgres but has no Mongo HA story. Tracked separately (issue doc TBD).

## Out of scope (this doc)

- **Hard-stop enforcement** (block at dispatch, suspend sessions at limit) → [[saas_billing_and_metering]] Slice 2.
- **Pre-pay wallet, deposits, debit transactions, Stripe** → [[saas_billing_and_metering]].
- **Storage metering implementation** → successor feature
  [`infrastructure_resource_metering.md`](infrastructure_resource_metering.md).
- **Per-org / per-tenant billing** → M2 in [[multi_tenancy]].
- **Right-sizing automation** from utilization (Track B produces the signal; acting on it is separate).

## Open questions

1. ~~Compute cost basis edge~~ **Resolved 2026-08-05:** canonical allocation uses
   admitted effective scheduler requests; observed utilization is a separate
   optional overlay. See
   [`infrastructure_resource_metering.md`](infrastructure_resource_metering.md).
2. ~~Analytics schema management~~ **Resolved 2026-06-11**: the `migrations/audit/` family owns the schema (`usage_events` = `0002`); same runner, one source of truth.
3. ~~`usage_daily` mirror transport~~ **Resolved 2026-07-03:** the orchestrator
   timer pulls and full-replaces the app-DB rollup; no cross-DB trigger is used.
4. Soft-quota thresholds: fixed (80/100%) or admin-configurable per limit?
5. Homelab compute rate: derive empirically from pdu power now, or ship a placeholder and calibrate later?
6. Do we meter LLM cost for **BYOK** users (user supplies their own API key)? Then there's no LLM cost *to us* — meter for their visibility only, or skip?

## References

- [[saas_billing_and_metering]] — the enforcement/wallet follow-on that reads this ledger.
- [[multi_tenancy]] §M1 — cost isolation is the M1 counterpart to data isolation.
- [[config_matrix_db_overrides]] — the DB-override pattern the rate/quota config follows.
- `HomeLab/deployments_managed/datawarehouse/` — the `analytics` TimescaleDB (chart, init, lb service).
- `HomeLab/deployments_unmanaged/pdu-scraper/` — the existing `analytics` writer + power data for rate calibration.
- `HomeLab/cluster_migration.md:109` — the unchecked "Install Rancher monitoring chart" item Track B resumes.
- OpenRouter markup model: https://openrouter.ai/docs/use-cases/byok
