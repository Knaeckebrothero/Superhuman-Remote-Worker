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
  - "[[vm_backend]]"
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

**Status:** **Slice 0 ✅ shipped + k3d-verified 2026-06-15** (structured JSON
logging + correlation IDs + secret redaction). Slices 0.5–3 (vm-controller
parity, Loki + Alloy + Grafana, retention, OTel) **not started** — the
continuous-shipping pipeline itself is still the open work, so this doc stays
in `features/`.
**Revised 2026-07-09** (design review before Slice 1): the VM backend became a
first-class production path after this doc was written, so the vm-controller /
second cluster is now a fourth log stream in the problem table; the pipeline
moved **out of the SRW app chart into cluster-infra Fleet bundles** (decision
11); Slice 0.5 (controller structured logging) was added; the original six
open questions were pinned into the decisions table and replaced with the five
genuinely-open ones.
**Triggered by:** Logs that matter for post-mortems live in ephemeral places
tied to lifecycle events, so the highest-signal data is lost exactly when a
failure occurs. The triggering question was "shouldn't we snapshot container
logs to S3 before deprovision?" — the answer is *yes to durability, no to the
teardown-pull mechanism*; this doc designs the continuous-shipping alternative
that industry converged on.

## The problem (grounded in our four pod lifecycles, across two clusters)

We have four pod classes across **two clusters** (the main K3s cluster and the
VMS agent cluster), each losing high-signal logs in a different way:

| Pod | Where its logs go today | When they die | What's lost |
|---|---|---|---|
| **Orchestrator** (`replicas: 1`, stdout only, no log volume) | container stdout; `uvicorn.access` is **disabled** (`main.py:63`) | **every Fleet rollout / restart** — and on `develop` that's constant (image bumps) | dispatch decisions, reconciler/reap actions, the **agent reap-log tails** (`agent_provisioner.py:680`), `completion.py` final-status calls, snapshot success/"keeping workspace alive" failures, startup migrations, NATS/sudo. The single highest-signal stream in the system. |
| **Agent** (per-job/session, reaped) | last **500 lines** pulled on reap → orchestrator stdout (`_capture_reap_logs`) | with the orchestrator pod that captured them | the full orchestration log (>500 lines); anything when the orchestrator itself restarts; anything on hard node-eviction where the reap hook never runs |
| **Workspace** (emptyDir, SSH) | filesystem → S3 snapshot of `/home/agent-host` (happy path, reachable only); container stdout = sshd | with the pod when unreachable; stdout always lost | `browser-exec` log lives at `/tmp/browser-exec.log` and `/tmp/*` is **excluded from the snapshot** (`snapshot_service.py:291`) — the one workspace log we'd reach for (CDP failures) is both excluded and dies with the pod |
| **vm-controller** (pod on the **VMS cluster**, Fleet bundle `deployment-vms/srw-vm-controller/`) | container stdout on the *other* cluster — and still plain-text `logging.basicConfig` (`vm/controller/controller.py:42`); never got Slice 0 | every controller rollout — frequent right now (golden image, rootdisk, reap-parity work all touch it) | the VM-side half of every provision / upgrade / reap timeline. The SSH-readiness and golden cold-import post-mortems (2026-07) were exactly this stream, reconstructed by hand. |

A fifth stream — the **VM guest itself** (sshd, cloud-init/boot, browser-exec
when the workspace is a VM) — lives in no `/var/log/pods/*` on any cluster and
is explicitly **out of scope** (decision 12): guest stdout is low-signal, and
the high-signal half of VM debugging is the controller + orchestrator + agent
correlation, which this design does capture. Revisit only if in-guest
CDP/browser-exec debugging starts to dominate post-mortems.

The recurring shape: **a high-signal log lives in an ephemeral place coupled to
a lifecycle event** (orchestrator-on-redeploy, agent-on-reap,
workspace-on-delete, controller-on-rollout). Four different events, four loss
modes, one root cause.

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
| 2 | Collector | **Grafana Alloy DaemonSet** — **pinned** (was open question; converges with Track B, one collector for logs+metrics+traces). Deploy with explicit memory requests/limits and validate on one node before fleet-wide — this homelab has real OOM history. Fluent Bit stays the documented fallback if Alloy's footprint bites. |
| 3 | Store | **Grafana Loki**, single-binary/**monolithic pinned** (was open question; revisit only if volume forces a split), **backed by our existing MinIO/S3** (new bucket `srw-logs`, in-cluster endpoint `minio.minio.svc.cluster.local:9000`, creds via Vault/ESO). Metadata-only index keeps it cheap on homelab. |
| 4 | Query/UI | **Reuse the existing Grafana** (pdu-metrics) — add Loki as a datasource. LogQL. Correlates with the Prometheus metrics from [[observability_and_quotas]] Track B → one observability pane. |
| 5 | Log format | **Structured JSON** from orchestrator + agent, with correlation fields (`job_id`, `thread_id`, `agent_id`, `phase`, `trace_id`). This is the keystone — it makes "everything for job X, across orchestrator + agent + workspace" a single LogQL query. |
| 6 | Everything logs to **stdout** | Sidecars/daemons that log to files (`browser-exec` → `/tmp`) switch to stdout so the DaemonSet captures them. Removes the bespoke per-file capture problem. |
| 7 | Retention | Tiered: **hot = 14d pinned** (was open question; driven by debugging need, not compliance) via Loki compactor. The **S3 lifecycle rule on `srw-logs` is created in Slice 1, day one** — the bucket never runs unbounded while dashboards wait for Slice 2. |
| 8 | Secret redaction | **Mandatory, in the collector pipeline** — non-negotiable (see below). **Both layers pinned** (was open question): never-log at the source (Slice 0, shipped) *and* the Alloy backstop — and the Alloy stage is tested against the **known past leak shapes** (`config_override` keys, `api_key` in GET-thread responses) as regression fixtures, not just generic patterns. |
| 9 | Cloud-agnostic | Same posture as the metering ledger: works on homelab MinIO today, customer's S3 later — only the bucket/endpoint changes. |
| 10 | Relationship to teardown hooks | Continuous shipping makes the per-pod log captures **non-load-bearing**. The workspace FS snapshot stays — but for **state restore/resume**, not log aggregation. |
| 11 | Deployment home | **Cluster-infrastructure Fleet bundles, NOT the SRW app chart** (revised 2026-07-09; Slice 1 originally said "in the chart"). Log collection is inherently cluster-scoped (hostPath `/var/log/pods`, all-namespaces RBAC), two SRW installs on one cluster would collide on the DaemonSet, and Alloy must run on **both clusters** while the app chart deploys to one. Loki + main-cluster Alloy → `HomeLab/deployments_managed/`; VMS-cluster Alloy → `HomeLab/deployments_managed_vms/`. The SRW chart's only contract stays what it already ships: JSON logs with correlation IDs (`LOG_FORMAT=json`). Also keeps k3d local dev light by default. |
| 12 | Coverage | All SRW-relevant pods on **both clusters**, including the vm-controller. **VM guest logs out of scope** (see problem section). Workspace-pod stdout ships (was open question — it's nearly free and `browser-exec` now logs to stdout per Slice 0; sshd noise is filtered by labels). |
| 13 | Tenancy | **Single-tenant Loki pinned** (was open question). Per-namespace tenants only if customer log isolation materializes — ties to [[multi_tenancy]]. |

## Architecture

```
  MAIN cluster (K3s)                         VMS cluster (agent VMs)
  /var/log/pods/*                            /var/log/pods/*
  (orchestrator, agent,                      (vm-controller, …)
   workspace pods, cockpit, mcp…)
        │                                          │
        ▼                                          ▼
 ┌───────────────────────────┐          ┌───────────────────────────┐
 │ Alloy (DaemonSet)          │          │ Alloy (DaemonSet)          │
 │  tail + k8s enrich         │          │  same config,              │
 │  + REDACT + parse JSON     │          │  label cluster=vms         │
 │  label cluster=main        │          └─────────────┬─────────────┘
 └─────────────┬─────────────┘                        │ push over LAN
               │ push (in-cluster svc)                 │ (open question 1)
               ▼                                       │
 ┌────────────────────────────────┐◄──────────────────┘
 │  Loki (monolithic, main)        │
 │   index: labels only            │
 │   chunks: → MinIO/S3 (srw-logs) │
 │   compactor: 14d hot retention  │
 └───────────────┬─────────────────┘
                 │ LogQL
                 ▼
 ┌────────────────────────────────┐
 │  Grafana (pdu-metrics, interim) │
 │   Loki datasource (ConfigMap)   │
 │   correlate w/ Prometheus (TrkB)│
 └────────────────────────────────┘
```

### Labels (the query surface)

Cheap, low-cardinality labels do the routing: `cluster` (`main|vms`),
`namespace`, `app` (`orchestrator|agent|workspace|cockpit|mcp|vm-controller`),
`pod`, `node`, `level`.
**High-cardinality IDs** (`job_id`, `thread_id`, `agent_id`, `trace_id`) stay in
the **log line** (structured JSON), queried with LogQL line filters — *not*
labels (Loki cardinality rule). That still gives the killer query:
`{app=~"orchestrator|agent"} | json | job_id="<uuid>"` → the full cross-pod
timeline for one job, after the pods are gone. For a VM-backed job, widening to
`{app=~"orchestrator|agent|vm-controller"}` adds the controller's
provision/upgrade/reap lines from the other cluster — the cross-cluster
timeline the 2026-07 VM post-mortems had to reconstruct by hand.

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

### Slice 0 — Structured logging + log-to-stdout  ✅ **DONE (2026-06-15)**
- Switch orchestrator + agent to **JSON structured logging** (a `JsonFormatter`;
  nothing structured today — it's greenfield) with correlation fields.
- Route file-logging daemons to **stdout**: `browser-exec`
  (`BROWSER_EXEC_LOG` → stdout, or fix the `/tmp` snapshot exclusion as interim),
  any sidecar.
- Add the redaction deny-list at the logger layer as defense-in-depth.
- **Value even without Loki:** `kubectl logs` becomes machine-parseable and
  greppable by `job_id`. **Acceptance:** a job emits JSON lines carrying its
  `job_id` across orchestrator and agent; no secret appears in any line.

**Shipped + verified end-to-end on k3d (drove a session + worker job):**
- New independent modules `orchestrator/logging_config.py` +
  `src/core/logging_config.py` (separate images, no shared import path;
  `redact()` kept in sync, both covered by `tests/test_logging_config.py`, 32
  tests). `LOG_FORMAT=json|text` — code default `text`; `helm/values.yaml`=`json`,
  `values-local.yaml.example`=`text` for readable local dev.
- Correlation wired: **`agent_id`** (pod name; `HOSTNAME`/`POD_NAME` fallback) at
  agent startup; **`job_id`** in the worker+dual job entry and the orchestrator
  dispatcher; **`thread_id`** at the persistent loop-start seam — bound before
  `asyncio.create_task` so it **copies into the loop task** (verified: the
  `src.persistent_graph` line carried it); **`request_id`** via a pure-ASGI
  `CorrelationIdMiddleware` (registered outermost → reaches route handlers, not
  just the access line; honors inbound `X-Request-ID`, else generates).
- uvicorn's own access/error logs routed through the JSON root handler
  (`log_config=None`) + its ANSI `color_message` extra excluded.
- `browser-exec` `DAEMON_LOG` moved out of the snapshot-excluded `/tmp`.
- **5 env/wiring bugs the unit tests structurally could not catch — found only on
  k3d** (all fixed + re-verified on the rebuilt image): (1) the orchestrator
  deployment wires config keys individually, so it needed an explicit
  `LOG_FORMAT` env block (agent pods get it via `envFrom`); (2) pods have **no**
  `AGENT_ID` — identity is `HOSTNAME`/`POD_NAME`, session thread is
  `SESSION_BOUND_THREAD_ID`; (3) k8s sets unused env to `""` not unset →
  `bind_log_context` skips `""`; (4) uvicorn logs were plain text; (5) uvicorn's
  `color_message` ANSI leak.
- **Python warnings folded in:** `configure_logging` calls
  `logging.captureWarnings(True)`, so `warnings.warn()` output routes through the
  `py.warnings` logger → root JSON handler (local-verified with the
  `reasoning_effort` UserWarning; deploys on next agent rebuild). All output —
  the `logging` framework, uvicorn, and Python warnings — is now JSON.
- **Ops note:** any change to the shared `srw-config` ConfigMap (incl. the agent
  image tag, which lives there) makes Stakater Reloader bounce the whole stack
  (~7 min, Keycloak cold-boot dominated). Budget for it on config-touching changes.
- Files: the two `logging_config.py` modules, `agent.py`, `orchestrator/main.py`,
  `src/api/{app,dual_app,persistent_app}.py`, `docker/browser-exec`,
  `helm/templates/configmap.yaml`, `helm/templates/orchestrator/deployment.yaml`,
  and the `values*.yaml` files.

### Slice 0.5 — vm-controller structured logging *(Slice 0 parity)*
- `vm/controller/controller.py` still uses plain-text `logging.basicConfig`
  (line 42) — it never got Slice 0. Give it the same JSON formatter,
  correlation fields (`job_id`, `vm_name`), and `redact()`.
- The controller is its own image (`vm/controller/Dockerfile`) with no shared
  import path — same situation as the orchestrator/agent pair, so this is a
  third synced copy of `logging_config.py` (keep `redact()` in sync; cover in
  `tests/test_logging_config.py` alongside the other two).
- Cheap to do **now**: controller.py is already in flight for the golden-import
  and reap-parity work; this rides the next controller rollout.
- **Acceptance:** a VM provision/reap emits JSON lines carrying `job_id` +
  `vm_name` through the controller; no secret (VM SSH keys, NATS creds) in any
  line.

### Slice 1 — Loki + Alloy + Grafana datasource *(the core)*
Ships as **cluster-infra Fleet bundles, not in the SRW chart** (decision 11):
- `HomeLab/deployments_managed/loki/` — Loki monolithic, chunks → MinIO
  `srw-logs` bucket (in-cluster endpoint, creds via Vault/ESO like every other
  bundle). Create the **S3 lifecycle rule the same day** (decision 7).
- `HomeLab/deployments_managed/alloy/` — main-cluster DaemonSet: tail
  `/var/log/pods/*`, k8s enrich, **redact** (with the leak-shape regression
  fixtures from decision 8), parse JSON, push to Loki, label `cluster=main`.
- `HomeLab/deployments_managed_vms/alloy/` — same for the VMS cluster, label
  `cluster=vms`, pushing over the LAN path (open question 1).
- Loki datasource provisioned into the existing pdu-metrics Grafana via
  ConfigMap — that bundle already provisions its TimescaleDB datasource the
  same way (`pdu-metrics/02-datasource.yaml`); copy the pattern.
- **Acceptance:** (a) kill an agent pod and a workspace pod, then in Grafana
  query `{app=~"orchestrator|agent|workspace"} | json | job_id="<uuid>"` and
  get the **full timeline after the pods are gone** — including the
  orchestrator lines that previously vanished on the next rollout; (b) for a
  **VM-backed job**, widening to `app=~"...|vm-controller"` includes the
  controller's provision/reap lines from the VMS cluster in the same timeline.

### Slice 2 — Retention tuning, dashboards
- Compactor retention at the pinned 14d hot window; per-namespace overrides if
  needed. (The S3 lifecycle safeguard already exists — created day one in
  Slice 1.)
- Starter dashboards: **Job timeline** (one job across pods *and clusters*),
  **Agent-fleet errors** (error-rate by `app`/`level`), **Orchestrator
  dispatch/reconciler**. Hold these until the Grafana-home question (open
  question 3) is settled so they aren't built twice.
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
- **Agent reap-tail (`_capture_agent_logs_before_reap`,
  `agent_provisioner.py:671`):** can remain as a convenience (inline "why did
  this pod die" in orchestrator logs) but is **no longer load-bearing** — the
  full agent log is in Loki regardless.
- **Local k3d:** unaffected by default — the pipeline is cluster infra, so
  nothing lands in the daily dev loop. Whether to build an opt-in local overlay
  at all is open question 5.

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

The original six (collector, topology, retention window, tenancy, redaction
depth, workspace stdout) are **pinned** in the decisions table (rows 2, 3, 7,
8, 12, 13). Genuinely open after the 2026-07-09 revision:

1. **Cross-cluster push path** — how the VMS-cluster Alloy reaches Loki on the
   main cluster: LAN-only Traefik ingress (`loki.h4ll.app`, basic-auth secret
   via Vault/ESO, MikroTik DNS only — **no** Cloudflare Tunnel route) vs a
   dedicated MetalLB IP vs the headscale tailnet (the controller already
   carries a headscale client). **Lean: LAN-only ingress + basic-auth** —
   simplest, matches how the clusters already talk, keeps the tailnet out of
   the hot path.
2. **Bundle layout + Track B sequencing** — one `observability/` bundle that
   later absorbs the kube-prometheus-stack, vs separate `loki/` + `alloy/`
   bundles now with Track B landing independently later. **Lean: separate
   bundles, logs ship first** — Track B has its own dormant-values
   resurrection work and shouldn't gate log durability.
3. **Grafana home** — add the Loki datasource to the pdu-metrics Grafana now
   (trivial ConfigMap, but that instance is nominally PDU-scoped) vs waiting
   for Track B's kube-prometheus Grafana as the durable observability pane.
   Building dashboards twice is the risk. **Lean: datasource into pdu-metrics
   now as the interim query surface; hold Slice 2 dashboards until the
   durable-home decision lands with Track B.**
4. **Namespace scope on the main cluster** — it hosts plenty of non-SRW
   workloads (nextcloud, minecraft, teamspeak…). Ship everything vs an
   allowlist (SRW namespaces + nats + keycloak + minio). Cost is trivial
   either way; it's a noise-vs-completeness question. **Lean: allowlist first,
   widen the first time a post-mortem wants a stream we didn't ship.**
5. **k3d parity** — build an opt-in local overlay for the pipeline, or accept
   that Slice 1 acceptance runs on the dev cluster only. **Lean: dev-only** —
   the k3d loop stays light, and the acceptance test doesn't need local
   reproducibility.

## References

### Internal
- [[observability_and_quotas]] — Track B (metrics) this is the log companion to.
- `docs/superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md` — the
  snapshot-then-delete teardown path and its reachability failure mode.
- `orchestrator/services/snapshot_service.py` — existing MinIO/S3 wiring to reuse.
- `orchestrator/services/agent_provisioner.py:671` — `_capture_agent_logs_before_reap`.
- `vm/controller/controller.py:42` — plain-text `logging.basicConfig` (Slice 0.5 target).
- `deployment-vms/srw-vm-controller/fleet.yaml` — the vm-controller's Fleet bundle (VMS cluster).
- `HomeLab/deployments_managed/pdu-metrics/` — the existing Grafana; `02-datasource.yaml`
  is the ConfigMap-provisioning pattern to copy for the Loki datasource.
- `HomeLab/rancher_cluster/fleet-gitrepo.yaml` + `fleet-gitrepo-vms.yaml` — the two
  GitRepos the new bundles land under (`deployments_managed/*` is glob-watched;
  drop the directory in and push).
- `HomeLab/rancher_cluster/OLD_rancher-monitoring_values.yaml` — dormant
  kube-prometheus-stack values (Track B; sequencing is open question 2).

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
