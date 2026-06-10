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

**Status:** Design / not started.
**Triggered by:** Approaching enterprise/SaaS readiness. Before plans/billing exist we
need (a) per-user cost attribution that works on *any* cluster (homelab today, AWS
later), and (b) basic infra-ops monitoring. Locked in conversation that this round is
**all read-side** — metering + dashboards + soft-quota *alerts*, no enforcement gates.

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
| 3 | Storage of record | **One append-only `usage_events` ledger** in the existing `analytics` TimescaleDB, *not* an ops TSDB. |
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
-- analytics TimescaleDB (datawarehouse), schema owned by orchestrator
usage_events  -- hypertable, time dimension = ts
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
- TimescaleDB **continuous aggregates** roll `usage_events` up to
  `usage_daily(user_id, project_id, category, day, tokens|quantity, cost_usd)` —
  cheap to read, compressed raw retained for audit.

### Where it lives, and the cross-DB join

| Data | Store | Why |
|---|---|---|
| `usage_events` (high volume, time-series) | **`analytics` TimescaleDB** (`datawarehouse-lb…:5432`) | App DB is plain `postgres:15` — no hypertables/compression/continuous-aggregates. Reuse existing infra (pdu-scraper already writes here). |
| `usage_daily` rollup mirror (small, joinable) | **App DB** | The Cockpit dashboard must join usage with user/job/project names. Mirror the daily continuous-aggregate into the app DB so the product reads **one** DB; raw high-res events stay in analytics. |
| `usage_rates` (config) | **App DB** | Small relational config; admin-editable. Fits the [[config_matrix_db_overrides]] DB-override pattern. |
| `quota_limits` (config) | **App DB** | Same. |

New wiring required (none exists today): an orchestrator connection pool to `analytics`
(`datawarehouse-lb.datawarehouse.svc.cluster.local:5432`, DB `analytics`) + a dedicated
`srw_metering` role, following the `pdu_scraper` user pattern. **Gotcha (documented in
pdu-scraper):** use the `datawarehouse-lb` service, not the chart's `timescaledb`
ClusterIP — the latter selects `role=master` but this Patroni/Spilo version labels the
leader `role=primary`, so it has no endpoints. Schema is applied by the orchestrator at
startup (extend `database/migrate.py` with an `analytics` target, or a one-off
init-SQL mirroring pdu-scraper's `10-schema.sql`).

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
- Per-job cost line on the existing job detail/list (from `usage_daily` / per-`ref_id` rollup).
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

## Storage metering — DEFERRED (note only)

> Captured per request; not yet designed. Storage's importance is **conditional on the
> hosting model**, which isn't decided.

- **Workspace storage today ≈ free.** Pods use `emptyDir` by default
  (`container_provisioner.py:191`) — ephemeral, node-local, dies with the pod. PVCs
  (`longhorn-ephemeral`, default `10Gi`) are legacy/optional. So there's little
  workspace storage cost to meter right now.
- **The real persistent storage is elsewhere:** OpenCloud, object stores (`minio` /
  `garage`), and PVCs if re-enabled. These are GB-month on *our* infra → meterable as
  `category='storage'`, `unit='gib-month'` in the same ledger when we get to it.
- **The BYO-cloud question (open):** if a customer brings their own cloud (their
  OpenCloud / Nextcloud / S3), the storage bill sits on *their* account — not ours to
  charge. In that mode storage metering is at most for the *user's own visibility*, not
  our cost recovery. If we host their storage, it's our cost and we bill it. So how much
  storage metering matters depends on whether hosted-storage is part of the product or
  always BYO. **Decide before building storage metering; the ledger already has the
  `storage` category reserved so adding it later is additive.**

## Relationship to MongoDB retirement

Resolves the "don't build more on MongoDB" concern *without* gating monitoring on a DB migration. Today Mongo holds three worker-job observability collections (`llm_requests`, `agent_audit`, `chat_history`) — all agent-side fire-and-forget, non-fatal, no transactional/product-critical writes; the interactive path already persists to Postgres `thread_messages`.

- **This layer is Mongo-independent.** LLM cost rows are emitted at the token-capture point (`src/core/archiver.py:393`, from `response.response_metadata`) — the *same upstream source* that feeds `llm_requests`, **not** a read of it. The `usage_events` ledger is in effect a better-typed, HA-capable `llm_requests`, so this work is the **first step of replacing** Mongo, not weight on top of it.
- **It also stands up the foundational store any Mongo retirement needs:** the orchestrator↔TimescaleDB connection, hypertable schema management, and the non-blocking event-write path. (Whether agents write the ledger directly — as they write Mongo today — or report through the orchestrator is a design call this layer settles; the migration of `agent_audit`/`llm_requests` then reuses whichever path we pick.) Monitoring-first de-risks and pays for that foundation; migrating-first builds the same foundation with no user-facing payoff in between.

The broader Mongo→Postgres/TimescaleDB consolidation is its **own initiative, not a prerequisite here.** When it happens, the clean split is by nature, not "everything into one Postgres":
- `agent_audit` + `llm_requests` → **TimescaleDB hypertables** (time-series, co-located with `usage_events`, reusing this layer's plumbing).
- `chat_history` → **app-DB `thread_messages`**, converging the worker path with the already-Postgres interactive path (one message store, under the G5 visibility model).

Cost driver: whether existing audit history must be preserved (dual-write + backfill) or a **forward-only cutover** is acceptable (far cheaper; fine on dev/thesis). Independent win that justifies eventual retirement: it removes today's single-replica, un-HA'd Mongo SPOF — the HA roadmap invests in CloudNativePG for Postgres but has no Mongo HA story. Tracked separately (issue doc TBD).

## Out of scope (this doc)

- **Hard-stop enforcement** (block at dispatch, suspend sessions at limit) → [[saas_billing_and_metering]] Slice 2.
- **Pre-pay wallet, deposits, debit transactions, Stripe** → [[saas_billing_and_metering]].
- **Storage metering implementation** (deferred, see note above).
- **Per-org / per-tenant billing** → M2 in [[multi_tenancy]].
- **Right-sizing automation** from utilization (Track B produces the signal; acting on it is separate).

## Open questions

1. Compute cost basis edge: bill on `requests` only, or `max(requests, observed)` for pods that burst past requests toward their limit? (Lean: requests only — simpler, defensible, matches node-packing.)
2. Analytics schema management: extend `database/migrate.py` with an `analytics` target, or a standalone init-SQL like pdu-scraper? (Lean: extend `migrate.py` for one source of truth.)
3. `usage_daily` mirror: push from a TimescaleDB continuous-aggregate refresh hook, or pull on an orchestrator timer? (Lean: orchestrator timer — no cross-DB triggers.)
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
</content>
</invoke>
