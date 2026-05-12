---
tags:
  - architecture
  - resilience
  - orchestrator
  - mongodb
  - incident-2026-05-12
related:
  - "[[orchestrator_phase_override_credentials_not_injected]]"
  - "[[agent_infinite_retry_on_permanent_llm_errors]]"
  - "[[agent_audit_collection_missing_indexes]]"
---

# Orchestrator/MongoDB Path Has No Backpressure or Circuit Breaker

**Reported**: 2026-05-12
**Status**: Architectural gap. Surfaced during the 2026-05-12 outage
when a single misconfigured Scholar job cascaded into a full API
outage (MongoDB CrashLoopBackOff, orchestrator OOMKilled, `/api/jobs`
returning 500/504, cockpit unusable). This is the meta-issue tying
together the three concrete bugs that participated in the cascade.

## Summary

On 2026-05-12 the cluster went from "one job is stuck" to "the entire
API is unreachable" with no human intervention required. The chain:

```
1 job pinned to a non-existent model
  → agent gets 404, retries 3× then iterates again
    → infinite write loop into agent_audit
      → agent_audit grows; every /api/jobs enrichment counts rows per job
        → COLLSCAN on 117K docs (no index — see [[agent_audit_collection_missing_indexes]])
          → MongoDB CPU pegged at 500m limit
            → mongosh-ping liveness probe (5s) starves; pod killed
              → kubelet restarts; orchestrator reconnects, hammers Mongo immediately
                → loop: 8 Mongo restarts in ~3h
                  → orchestrator queues responses; OOMKilled after 48 min (1Gi memory limit)
                    → /api/jobs returns 500/504; cockpit blank
```

Each link is a known and individually-fixable bug
([[orchestrator_phase_override_credentials_not_injected]],
[[agent_infinite_retry_on_permanent_llm_errors]],
[[agent_audit_collection_missing_indexes]]). What's *not* a known bug
is the **absence of any structural defence between them**: no
backpressure on the agent ↔ orchestrator path, no circuit breaker on
the orchestrator ↔ MongoDB path, no degraded-mode response from the
API when its data store is misbehaving, no shedding on the cockpit
poll loop.

This doc is the design-discussion home for fixing the *class* of
failure, not any specific link. The other three issues are the
"trigger and amplifiers"; this one is "stop the next misconfiguration
from doing the same thing."

## Observed Cascade (2026-05-12)

Timing reconstructed from `kubectl describe` + container logs.

| Time (CEST) | Event |
|---|---|
| ~10:41 | Helm release v45 rolls out fresh `superhuman-remote-worker` pods. All Running. |
| ~10:42 | User submits Scholar-preset jobs pinned to `gpt-5.3-codex-spark`. |
| 10:42–11:30 | Agent loops on 404 model-not-found; each iteration writes one audit row. Iteration counter climbs past 60. |
| ~11:30 | `srw-orchestrator-7ff55dfddd-l52j8` **OOMKilled** (Exit 137) after 48 min. Memory limit 1 Gi. |
| 11:30:31 | Orchestrator pod restarts (kept running thereafter). |
| 12:45 | First MongoDB liveness-probe failure (`mongosh ping` >5 s). Kubelet kills pod. |
| 12:45–~13:15 | MongoDB restarts 5× in 30 min, each time hammered immediately by the orchestrator's outstanding aggregations. |
| ~13:00 | Cockpit users see `504 Gateway Timeout` / `500 Internal Server Error` on `/api/jobs?limit=100`. |
| ~13:03 | MongoDB enters `CrashLoopBackOff` (8 restarts). |
| (recovery) | Manual recovery — cluster eventually stabilized; details in chat thread. |

CPU/memory at peak (from `kubectl top`):

- `srw-mongodb-0`: **501m / 500m CPU** (at limit), 516 Mi memory.
- `srw-orchestrator-...`: 22m CPU (already cooled), 264 Mi memory
  (post-restart). Pre-OOM, it would have been pegging memory.

Slow-query log shape on Mongo:

```
"planSummary": "COLLSCAN", "docsExamined": 117282, "durationMillis": ~900-1700
"ns": "srw_logs.agent_audit"
"command": {"aggregate": ..., "pipeline": [{"$match": {"job_id": "..."}},
                                            {"$group": {"_id": 1, "n": {"$sum": 1}}}]}
```

## Architectural Gaps

### Gap 1 — Agent has no backpressure signal

The agent's iteration loop runs at the speed of the LLM endpoint
(plus retry backoff). Nothing in the loop reads orchestrator
health/load; nothing rate-limits audit writes based on downstream
pressure. A degraded orchestrator/Mongo manifests as 30-second `POST
/api/.../audit` calls, which the agent silently waits on — but the
*next* iteration's audit write is queued the moment the LLM
responds. There's no "the database is overloaded, slow down."

### Gap 2 — Orchestrator has no circuit breaker on Mongo

`mongodb.py`'s `count_documents` / `aggregate` / `find` calls are
direct, unguarded. When Mongo is slow or unavailable, every inflight
HTTP request to `/api/jobs` (and any other endpoint that reads from
Mongo) blocks until its Mongo call returns or times out — typically
~30 s server-side, longer at the gateway. The orchestrator's worker
pool fills up; new requests queue; memory grows; OOMKilled follows.

A circuit breaker (e.g. `pybreaker`, or hand-rolled with the same
shape) would fail fast after N consecutive Mongo errors and let the
API return a structured "audit data temporarily unavailable" instead
of timing out.

### Gap 3 — `/api/jobs` enrichment is N+1 against a hot path

The job-list endpoint enriches each job with an audit count
(`mongodb.py:287`, `mongodb.py:700`). Cockpit polls this every few
seconds. Even on a healthy cluster that's O(jobs × polls) Mongo
hits/min. Under load it's the first thing to drown.

Options:

- Denormalise the count onto the `jobs` row (counter incremented at
  audit-write time).
- Drop the count from the list payload and surface it on the detail
  view only.
- Cache the count per `job_id` for N seconds — cheap when audits are
  bursting from one job.

### Gap 4 — Resource limits are sized for the happy path

| Component | Request | Limit | Observed peak |
|---|---|---|---|
| Orchestrator | 100m / 256 Mi | **500m / 1 Gi** | OOMKilled at 1 Gi |
| MongoDB | 100m / 256 Mi | **500m / 1 Gi** | Pegged at 500m CPU, 516 Mi |

Both hit limits during the cascade. The fix isn't "always size for
disaster" (that's expensive and hides bugs), but the current limits
leave **zero headroom**: any sustained anomaly is a kill. Specific
followups belong in this doc rather than their own:

- Orchestrator memory 1 Gi → 2 Gi (current usage during incident
  suggested response-buffering was the OOM cause).
- MongoDB CPU 500m → 1 (allows the existing slow queries to still
  hit the liveness probe in time during anomalies).
- Tune liveness/readiness probe timeouts: a 5 s `mongosh --eval` is
  fragile under any load. Either bump the timeout or use the cheaper
  `mongosh --quiet --eval 'db.runCommand({ping:1})'` against a
  socket-level health command.

### Gap 5 — Cockpit polls aggressively with no shedding

The cockpit refreshes `/api/jobs?limit=100` on every navigation,
visibility change, and N-second interval. When the API is slow, the
cockpit just stacks parallel pending requests — the user sees a hung
spinner *and* the backend gets pounded harder. A shed-on-slow policy
(cancel inflight request before issuing a fresh one; back off polling
when responses are >5 s) would damp the feedback loop from the UI
side.

## Design Direction (Discussion)

These are *options to consider*, not a settled plan. The PR for this
issue should pick a subset.

### Direction A — Defensive plumbing in the existing services

Smallest code change. In-process additions:

1. Circuit breaker around every Mongo call in `mongodb.py` (one
   shared breaker keyed by collection).
2. Per-route timeout budgets in FastAPI: any request that exceeds
   `request_budget_ms` returns 503 + a degraded-mode payload, not a
   hang.
3. Rate-limit `auditor.audit_step` writes per job: cap at e.g.
   1 row/sec; aggregate excess into a single "N errors in window"
   row. Bounds the write storm regardless of agent behaviour.
4. Resource-limit bumps from Gap 4.

Pros: small, no new infrastructure. Cons: doesn't address the cockpit
side or the architectural N+1 in `/api/jobs`.

### Direction B — Make the agent/orchestrator boundary explicit

Add a small "health hint" header to orchestrator responses (`X-
Backpressure: high`) when Mongo or its own queue is degraded. The
agent reads it and dials back: longer audit-write intervals,
batched writes, no per-iteration audit during pressure. Cheap on the
happy path, materially changes the cascade dynamics under load.

### Direction C — Move audit writes off the synchronous path

Audit rows don't need to be durable before the next agent iteration
proceeds. Two patterns worth weighing:

- Agent writes to a local ring buffer; a background task flushes to
  the orchestrator in batches.
- Agent writes directly to Mongo (the orchestrator already exposes
  it via its API; the audit path doesn't enrich much). Trades a
  network hop for direct DB load — only sensible alongside Gap 3's
  N+1 cleanup.

### Direction D — Move audit storage off Mongo entirely

`agent_audit` is append-mostly time-series data with one read pattern
(`{job_id: ..., timestamp: ...}`). Postgres + a JSONB column would
fit, with the bonus of cheaper joins to `jobs`. Or a dedicated
time-series store. Larger lift, not appropriate for the immediate
post-incident PR, but worth noting if Mongo continues to be a hot
spot.

## Acceptance Criteria (suggested)

This doc closes when:

- A single misconfigured job cannot drive Mongo CPU above its limit.
- A 30-second slow `count_documents` does not cause `/api/jobs` to
  return 504 — it returns a degraded payload with the count omitted
  or stale.
- Resource limits have ≥30 % headroom on the observed peak.
- A runbook exists for "Mongo is in CrashLoopBackOff": scale
  orchestrator → 0, let Mongo stabilize, re-create missing indexes,
  scale orchestrator back up. (We did this by hand on 2026-05-12;
  it should be a script.)

## Code References

| File | Lines | Role |
|---|---|---|
| `orchestrator/database/mongodb.py` | 287, 700, 853, 905 | Mongo call sites with no breaker / timeout budget |
| `orchestrator/main.py` | (TBD) | `/api/jobs` handler, per-job audit count enrichment |
| `src/graph.py` | 1684-1702 | Per-iteration audit write at agent retry-exhaustion |
| `cockpit/.../jobs.service.ts` (or similar) | (TBD) | Cockpit poll loop |
| `helm/.../orchestrator-deployment.yaml` | (TBD) | Resource limits |
| `helm/.../mongodb-statefulset.yaml` | (TBD) | Resource limits + probe config |

## Priority

This is the long-tail design doc; ship it after the three concrete
fixes are merged. Sequencing:

1. Land [[orchestrator_phase_override_credentials_not_injected]] (kills
   the trigger).
2. Land [[agent_infinite_retry_on_permanent_llm_errors]] (kills the
   amplifier on the agent side).
3. Land [[agent_audit_collection_missing_indexes]] Fixes 1–3 (kills
   the amplifier on the data side).
4. Then revisit this doc, pick a direction (A is the most pragmatic
   starting point), and design.

Doing the resilience work before fixing the trigger/amplifiers risks
covering up the underlying bugs.

## Open Questions

- What's the actual SLO for `/api/jobs`? A circuit-breaker that
  returns 503 after 5 s of Mongo unavailability is reasonable for an
  internal cockpit, possibly too aggressive for an externally-facing
  API. Worth nailing down before designing the breaker.
- Is `agent_audit` actually the right granularity? If many of its
  reads are "give me the total count per job", a denormalized counter
  changes the calculus on every other recommendation here.
- Does the `bedrockconnect` SVCLB pending pod (see
  `kubectl get pods -A | grep Pending`) interact with this at all?
  It's pre-existing noise, but worth checking that nothing in the
  blast radius is blocked on it.
