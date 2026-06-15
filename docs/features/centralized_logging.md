---
tags:
  - feature
  - observability
  - logging
  - log-aggregation
  - retention
  - debugging
related:
  - "[[observability_and_quotas]]"
  - "[[high_availability_setup]]"
  - "[[database_architecture]]"
  - "[[postgres_audit_store_implementation]]"
aliases:
  - centralized logging
  - log aggregation
  - log shipping
  - log retention
  - container log durability
---

# Centralized Logging (continuous shipping + durable retention)

> Design doc — captured 2026-06-15 from the "store container logs before
> deprovisioning" conversation. This is the **log-side companion to Track B** of
> [[observability_and_quotas]] (which covers Prometheus/Grafana *metrics*). It
> makes pod logs **durable, queryable, and correlatable** after the pod that
> produced them is gone — without depending on a teardown hook or on the pod
> being reachable at teardown time.

**Status:** Design / not started.
**Triggered by:** Logs that matter for post-mortems live in ephemeral places
tied to lifecycle events, so the highest-signal data is lost exactly when a
failure occurs. The triggering question was "shouldn't we snapshot container
logs to S3 before deprovision?" — the answer is *yes to durability, no to the
teardown-pull mechanism*; this doc designs the continuous-shipping alternative
that industry converged on.

## The problem (grounded in our three pod lifecycles)

We have three pod classes, each losing high-signal logs in a different way:

| Pod | Where its logs go today | When they die | What's lost |
|---|---|---|---|
| **Orchestrator** (`replicas: 1`, stdout only, no log volume) | container stdout; `uvicorn.access` is **disabled** (`main.py:63`) | **every Fleet rollout / restart** — and on `develop` that's constant (image bumps) | dispatch decisions, reconciler/reap actions, the **agent reap-log tails** (`agent_provisioner.py:680`), `completion.py` final-status calls, snapshot success/"keeping workspace alive" failures, startup migrations, NATS/sudo. The single highest-signal stream in the system. |
| **Agent** (per-job/session, reaped) | last **500 lines** pulled on reap → orchestrator stdout (`_capture_reap_logs`) | with the orchestrator pod that captured them | the full orchestration log (>500 lines); anything when the orchestrator itself restarts; anything on hard node-eviction where the reap hook never runs |
| **Workspace** (emptyDir, SSH) | filesystem → S3 snapshot of `/home/agent-host` (happy path, reachable only); container stdout = sshd | with the pod when unreachable; stdout always lost | `browser-exec` log lives at `/tmp/browser-exec.log` and `/tmp/*` is **excluded from the snapshot** (`snapshot_service.py:291`) — the one workspace log we'd reach for (CDP failures) is both excluded and dies with the pod |

The recurring shape: **a high-signal log lives in an ephemeral place coupled to
a lifecycle event** (orchestrator-on-redeploy, agent-on-reap,
workspace-on-delete). Three different events, three different loss modes, one
root cause.

### Why "capture at deprovision" is the wrong mechanism

A teardown-time pull only succeeds on a *clean* exit — the case you least need
to debug — and fails on exactly the cases you do:

- The workspace-reaper spec already documents pods stuck **17–24 days,
  unreachable**; a deprovision-time pull captures nothing from those (same SSH
  dependency the snapshot path has).
- On **OOM-eviction or node failure**, the orchestrator's teardown hook never
  runs at all.
- By deprovision time the **kubelet may have already rotated** the container log
  (10Mi/file default; a container restart wipes the prior stream).
- The orchestrator pod **doesn't deprovision per-job at all** — its lifecycle is
  "restart on deploy," so a per-job teardown hook can't help it.

The industry answer is to **decouple log durability from the pod lifecycle
entirely**: ship each line off the node as it is emitted. However the pod dies,
the logs already left.

## How this is done at scale (distilled research)

The pattern is remarkably consistent from homelab to hyperscaler:

- **Node-level collector as a DaemonSet** is the standard Kubernetes pattern: one
  agent per node tails `/var/log/pods/*`, enriches with k8s metadata (pod,
  namespace, labels), and forwards to a central store — **no application
  changes, low per-node overhead**. (Sidecar-per-pod is the alternative, used
  when an app can't log to stdout; heavier, avoid by default.) — *Graylog,
  Sematext, Tigera guides.*
- **Index-light store for cost.** Grafana **Loki** indexes only *metadata*
  (labels) and stores compressed log chunks in object storage — far cheaper than
  ELK/Elasticsearch, which indexes full content. Loki saw ~80% YoY adoption
  growth into 2026, driven by teams already on Prometheus/Grafana wanting one
  stack. ELK still wins for deep ad-hoc search / security forensics. — *Wallarm,
  OpsVerse, Grafana.*
- **Big-company pipelines are tiered hot/cold over object storage.** Netflix:
  lightweight sidecars → ingestion → **S3 + Kinesis** → ClickHouse (hot, query
  in seconds) + Apache Iceberg (cold, long-term); retention **2 weeks to 2
  years** per service; 5 PB/day. Uber: Kafka **tiered storage** (local hot tier,
  remote tier retained days–months). Cloudflare: request bodies to **R2** with
  a small DB as the **index**, extending retention cheaply. The shared lesson —
  *match storage tier to access pattern; tiering cuts cost 80%+ while keeping
  recent data fast.* — *ClickHouse/Netflix, Uber, Cloudflare blogs.*
- **Collector choice has consolidated.** Grafana **Promtail is feature-complete
  / maintenance-only**; the go-forward is **Grafana Alloy** (a branded
  OpenTelemetry Collector distribution). **Fluent Bit** (C) has a negligible
  footprint; **Vector** (Rust) leads raw throughput. — *Grafana, VictoriaMetrics
  2026 benchmark.*
- **Structured logs + correlation IDs are what make it useful.** JSON logs with
  `trace_id`/`span_id` (OpenTelemetry log data model) let you pivot from a log
  line to the exact trace and back, and filter/redact per-field. "Don't log
  everything at INFO." — *OpenTelemetry docs, OneUptime.*
- **Retention is policy-driven and compliance-aware.** Hot (full index,
  expensive) / warm (reduced index) / cold (archive, 10× compression). Loki's
  compactor performs retention-driven deletion; bucket lifecycle rules are a
  safeguard. Classify by value — debug logs ≠ security logs. — *Last9, Graylog,
  Cloudflare.*

## Locked decisions

| # | Decision | Value |
|---|---|---|
| 1 | Mechanism | **Continuous node-level shipping**, not teardown-time pull. Durability is decoupled from pod lifecycle and reachability. |
| 2 | Collector | **Grafana Alloy DaemonSet** (OTel-based, Grafana's go-forward; Promtail is EOL). Fluent Bit is the lighter fallback if Alloy's footprint is too high for homelab nodes. |
| 3 | Store | **Grafana Loki**, single-binary/monolithic mode, **backed by our existing MinIO/S3** (new bucket, e.g. `srw-logs`; reuses the snapshot store's S3 wiring). Metadata-only index keeps it cheap on homelab. |
| 4 | Query/UI | **Reuse the existing Grafana** (pdu-metrics) — add Loki as a datasource. LogQL. Correlates with the Prometheus metrics from [[observability_and_quotas]] Track B → one observability pane. |
| 5 | Log format | **Structured JSON** from orchestrator + agent, with correlation fields (`job_id`, `thread_id`, `agent_id`, `phase`, `trace_id`). This is the keystone — it makes "everything for job X, across orchestrator + agent + workspace" a single LogQL query. |
| 6 | Everything logs to **stdout** | Sidecars/daemons that log to files (`browser-exec` → `/tmp`) switch to stdout so the DaemonSet captures them. Removes the bespoke per-file capture problem. |
| 7 | Retention | Tiered: hot (Loki/S3, queryable, short — e.g. 14–30d) + cold (S3 lifecycle to cheaper class / longer archive for any compliance need). Compactor-driven deletion; bucket lifecycle as safeguard. |
| 8 | Secret redaction | **Mandatory, in the collector pipeline** — non-negotiable (see below). |
| 9 | Cloud-agnostic | Same posture as the metering ledger: works on homelab MinIO today, customer's S3 later — only the bucket/endpoint changes. |
| 10 | Relationship to teardown hooks | Continuous shipping makes the per-pod log captures **non-load-bearing**. The workspace FS snapshot stays — but for **state restore/resume**, not log aggregation. |

## Architecture

```
                       ┌──────────────────────────────────────────┐
  every node:          │  Grafana Alloy (DaemonSet)               │
  /var/log/pods/* ────►│   tail + k8s-metadata enrich + REDACT     │
  (orchestrator,       │   + parse JSON → labels                   │
   agent, workspace,   └───────────────────┬──────────────────────┘
   cockpit, mcp…)                          │ push (Loki API / OTLP)
                                           ▼
                          ┌────────────────────────────────┐
                          │  Loki (monolithic)              │
                          │   index: labels only            │
                          │   chunks: → MinIO/S3 (srw-logs) │
                          │   compactor: retention deletion │
                          └───────────────┬─────────────────┘
                                          │ LogQL
                                          ▼
                          ┌────────────────────────────────┐
                          │  Grafana (existing)             │
                          │   Loki datasource + dashboards  │
                          │   correlate w/ Prometheus (TrkB)│
                          └────────────────────────────────┘
```

### Labels (the query surface)

Cheap, low-cardinality labels do the routing: `namespace`, `app`
(`orchestrator|agent|workspace|cockpit|mcp`), `pod`, `node`, `level`.
**High-cardinality IDs** (`job_id`, `thread_id`, `agent_id`, `trace_id`) stay in
the **log line** (structured JSON), queried with LogQL line filters — *not*
labels (Loki cardinality rule). That still gives the killer query:
`{app=~"orchestrator|agent"} | json | job_id="<uuid>"` → the full cross-pod
timeline for one job, after the pods are gone.

### Correlation IDs

Thread a request/job context through the logging layer so every line carries
`job_id` / `thread_id` / `agent_id` / `phase`. The agent already has these in
state; the orchestrator already has them per-request. This is the difference
between "grep a wall of text" and "reconstruct one job's life across three
pods." OTel `trace_id`/`span_id` is the eventual upgrade (Slice 3) once tracing
exists.

### Secret redaction — a hard requirement for *us* specifically

We have a documented history of **cleartext credential leaks into
persisted/returned data** (`config_override` keys in thread metadata; `api_key`
in GET-thread responses). Centralized logging **amplifies the blast radius** of
any secret that reaches a log line — it becomes durable, indexed, and broadly
readable. So the collector pipeline **must** carry a redaction stage (regex/deny
patterns for `api_key`, `Authorization`, `*_SECRET`, bearer tokens, JWTs,
provider key prefixes) *before* anything is shipped, and the orchestrator/agent
loggers should never log raw `config_override` / headers. Treat this as a Slice
1 gate, not a follow-up.

## Slices (each independently shippable)

### Slice 0 — Structured logging + log-to-stdout *(prereq; ships value alone)*
- Switch orchestrator + agent to **JSON structured logging** (a `JsonFormatter`;
  nothing structured today — it's greenfield) with correlation fields.
- Route file-logging daemons to **stdout**: `browser-exec`
  (`BROWSER_EXEC_LOG` → stdout, or fix the `/tmp` snapshot exclusion as interim),
  any sidecar.
- Add the redaction deny-list at the logger layer as defense-in-depth.
- **Value even without Loki:** `kubectl logs` becomes machine-parseable and
  greppable by `job_id`. **Acceptance:** a job emits JSON lines carrying its
  `job_id` across orchestrator and agent; no secret appears in any line.

### Slice 1 — Loki + Alloy + Grafana datasource *(the core)*
- Loki (monolithic) in the chart, chunks → MinIO `srw-logs` bucket (reuse
  snapshot S3 creds pattern).
- Alloy DaemonSet: tail `/var/log/pods/*`, k8s enrich, **redact**, parse JSON,
  push to Loki.
- Loki datasource in the existing Grafana.
- **Acceptance:** kill an agent pod and a workspace pod, then in Grafana query
  `{app=~"orchestrator|agent|workspace"} | json | job_id="<uuid>"` and get the
  **full timeline after the pods are gone** — including the orchestrator lines
  that previously vanished on the next rollout.

### Slice 2 — Retention, tiering, dashboards
- Compactor retention (hot window, e.g. 14–30d); S3 lifecycle rule on `srw-logs`
  as cold archive / safeguard. Per-namespace overrides if needed.
- Starter dashboards: **Job timeline** (one job across pods), **Agent-fleet
  errors** (error-rate by `app`/`level`), **Orchestrator dispatch/reconciler**.
- **Acceptance:** logs older than the hot window are pruned by the compactor;
  a saved "Job timeline" dashboard reconstructs a completed job end-to-end.

### Slice 3 — Correlation + alerting *(converge with Track B)*
- Adopt OTel `trace_id`/`span_id` once orchestrator tracing exists; wire
  log↔trace↔metric correlation in Grafana (logs from Loki, metrics from the
  Track B Prometheus).
- Optional: Loki **ruler** → Alertmanager for log-based alerts (error-rate
  spike, repeated codex 401, reap-storms) — complements, doesn't replace, the
  metric alerts.

## Relationship to existing work

- **[[observability_and_quotas]] Track B** plans to resume the dormant
  kube-prometheus-stack (`HomeLab/rancher_cluster/OLD_rancher-monitoring_values.yaml`)
  for *metrics*. This doc is its **log-side companion** — same Grafana, same
  "ops-not-billing" posture, deployed together as the observability stack. Track
  B is explicitly *not* the system of record for cost; likewise Loki is *not*
  the system of record for the agent's semantic audit (that's
  Mongo→[[postgres_audit_store_implementation]] `srw-auditdb`). Loki is for
  **operational/infra debugging** — the raw stdout firehose — not product data.
- **Workspace-reaper spec / snapshot service:** the FS snapshot stays for
  **state restore** (resume support), unchanged. Once logs ship continuously,
  the snapshot no longer needs to be the log-durability path, and the
  `browser-exec` `/tmp` exclusion stops mattering for logs (it logs to stdout).
- **Agent reap-tail (`_capture_reap_logs`):** can remain as a convenience
  (inline "why did this pod die" in orchestrator logs) but is **no longer
  load-bearing** — the full agent log is in Loki regardless.

## Out of scope / deferred

- **ELK / full-text search.** Loki's label+grep model fits our debugging. If
  deep ad-hoc/security forensics ever dominates, revisit ELK or ClickHouse
  (Netflix's choice) as a parallel store — not now.
- **Tracing (full OTel spans).** Slice 3 assumes trace IDs exist; standing up
  distributed tracing is its own initiative.
- **Long-horizon compliance retention (years).** We have no regulatory mandate
  today (thesis/homelab). The `srw-logs` bucket + S3 lifecycle makes adding a
  cold archive additive when a customer requires it.
- **BYO-cloud customer mode.** If a customer brings their own logging backend,
  Alloy can fan out to it; designed-for, not built.

## Open questions

1. **Collector:** Alloy (unifies logs+metrics+traces, Grafana-native, heavier)
   vs Fluent Bit (tiny C footprint, logs-only). Lean Alloy for convergence with
   Track B; fall back to Fluent Bit if node overhead bites on homelab.
2. **Loki topology:** monolithic single-binary (simplest, fine at our scale) vs
   SSD/microservices. Lean monolithic until volume forces a split.
3. **Hot retention window:** 14d? 30d? Driven by debugging need, not compliance.
4. **Multi-tenancy:** single-tenant Loki vs per-namespace tenants (matters more
   if customer log isolation becomes a requirement — ties to [[multi_tenancy]]).
5. **Redaction completeness:** deny-list (fast, may miss novel secret shapes) vs
   structured-only logging that never puts secrets in a line in the first place.
   Lean: both — never-log at the source *and* a collector backstop.
6. **Do we ship workspace stdout at all,** or only orchestrator+agent? Workspace
   sshd noise is low-signal; `browser-exec`-to-stdout is the part worth keeping.

## References

### Internal
- [[observability_and_quotas]] — Track B (metrics) this is the log companion to.
- `docs/superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md` — the
  snapshot-then-delete teardown path and its reachability failure mode.
- `orchestrator/services/snapshot_service.py` — existing MinIO/S3 wiring to reuse.
- `orchestrator/services/agent_provisioner.py:680` — `_capture_reap_logs`.
- `HomeLab/rancher_cluster/OLD_rancher-monitoring_values.yaml` — dormant
  kube-prometheus-stack values (Track B + this deploy together).

### External (best-practice sources)
- Kubernetes node-level DaemonSet logging — Graylog, Sematext, Tigera guides.
- Loki vs ELK (index-light, cost) — Wallarm, OpsVerse, Grafana Labs.
- Netflix petabyte-scale logging (sidecar→S3/Kinesis→ClickHouse/Iceberg, tiered)
  — clickhouse.com/blog/netflix-petabyte-scale-logging.
- Uber Kafka tiered storage; Cloudflare R2 log retention — uber.com, blog.cloudflare.com.
- Collector benchmark (Alloy/Vector/Fluent Bit), Promtail EOL — VictoriaMetrics
  2026 benchmark, Grafana Labs.
- OpenTelemetry logs / correlation IDs / structured logging — opentelemetry.io, OneUptime.
- Loki S3 backend + compactor retention — grafana.com/docs, Last9.
- Log retention tiering / cost / compliance — Last9, Graylog, Cloudflare.
</content>
</invoke>
