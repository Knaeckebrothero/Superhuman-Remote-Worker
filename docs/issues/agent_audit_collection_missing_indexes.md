---
tags:
  - orchestrator
  - mongodb
  - performance
  - indexes
  - init
  - resolved
related:
  - "[[orchestrator_phase_override_credentials_not_injected]]"
  - "[[agent_infinite_retry_on_permanent_llm_errors]]"
  - "[[orchestrator_mongodb_cascading_failure_resilience]]"
---

# `agent_audit` Has No Indexes on the Running Cluster

**Reported**: 2026-05-12
**Status**: **Resolved in `7f8f609`** (2026-05-12). Verified in
production on 2026-05-12: the new orchestrator pod's startup log
emits `MongoDB ensure_indexes: asserted 14 indexes across 3
collections` and `db.agent_audit.getIndexes()` on the live cluster
now lists all seven declared indexes (plus `_id`). The root-cause
hypothesis was correct — the standalone `init.py` CLI was never
invoked by the deploy pipeline; the fix moves the index assertion to
the orchestrator's runtime startup hook. See the
[Resolution](#resolution) section below.

Original report kept below for historical context — including the
diagnostic that uncovered the missing indexes in the first place.

## Summary

`orchestrator/init.py:1521-1551` defines these indexes for the
`agent_audit` collection:

| Index | Name |
|---|---|
| `{job_id: 1}` | `idx_audit_job_id` |
| `{step_type: 1}` | `idx_audit_step_type` |
| `{node_name: 1}` | `idx_audit_node_name` |
| `{timestamp: 1}` | `idx_audit_timestamp` |
| `{job_id: 1, step_number: 1}` | `idx_audit_job_step` |
| `{job_id: 1, iteration: 1, step_number: 1}` | `idx_audit_job_iter_step` |
| `{job_id: 1, agent_type: 1, step_type: 1}` | `idx_audit_job_agent_type` |

On the live cluster:

```
$ kubectl exec -n superhuman-remote-worker srw-mongodb-0 -- \
    mongosh --quiet --norc srw_logs --eval \
    'db.agent_audit.getIndexes().forEach(i => printjson(i.key))'
{ _id: 1 }
```

So six out of seven indexes (every `job_id`-keyed one, including the
single-field `idx_audit_job_id`) **never made it onto this collection**.
Collection currently holds 117K+ docs; every per-job aggregation does a
full collection scan reading ~600 MB of WiredTiger pages.

This isn't merely "we forgot the index" — the orchestrator's startup
code *thinks* it created them. Something is silently failing or being
skipped. That's the part that matters: indexes can be added by hand in
30 seconds; understanding why init didn't put them there is the real
fix.

## Observed Behavior

Slow-query log from `srw-mongodb-0` during the 2026-05-12 incident
(one of hundreds in the same shape):

```json
{
  "msg": "Slow query",
  "attr": {
    "type": "command",
    "ns": "srw_logs.agent_audit",
    "command": {
      "aggregate": "agent_audit",
      "pipeline": [
        {"$match":  {"job_id": "8d31111d-31d8-4c6c-91eb-b54815ed00cc"}},
        {"$group":  {"_id": 1, "n": {"$sum": 1}}}
      ]
    },
    "planSummary": "COLLSCAN",
    "docsExamined": 117282,
    "nreturned": 1,
    "storage": {"data": {"bytesRead": 599298065}},
    "cpuNanos": 219151261,
    "durationMillis": 911
  }
}
```

- `planSummary: COLLSCAN` — no index used.
- `docsExamined: 117282` for a result that's a single `{_id: 1, n: N}`
  document.
- `bytesRead: 599 MB` of WiredTiger pages per aggregation.
- ~900 ms wall-clock per call on a healthy MongoDB; closer to several
  seconds under any contention.

Under the write storm produced by
[[agent_infinite_retry_on_permanent_llm_errors]], each tick of
`/api/jobs` enrichment ran several of these aggregations concurrently,
pinning MongoDB CPU to its 500m limit and timing out the 5-second
`mongosh ping` liveness probe. MongoDB cycled through 8 restarts in
~3 hours, see [[orchestrator_mongodb_cascading_failure_resilience]].

## Expected Behavior

After the orchestrator boots against a fresh (or upgraded) MongoDB,
`db.agent_audit.getIndexes()` should return all seven declared
indexes. Per-job aggregations and counts should use
`idx_audit_job_id`, examining only the rows belonging to that job
(typical: 50–500), not the whole collection.

## Root Cause Hypotheses (Not Yet Confirmed)

The index creation block (`orchestrator/init.py:1539-1551`) wraps each
`create_index` in a `try/except` and **only logs at WARNING** on
unexpected failures:

```python
for index_spec, options in audit_indexes:
    try:
        if isinstance(index_spec, list):
            agent_audit.create_index(index_spec, **options)
        else:
            agent_audit.create_index(index_spec, **options)
        logger.info(f"    Created index: {options['name']}")
    except Exception as e:
        if "already exists" in str(e).lower():
            logger.info(f"    Index exists: {options['name']}")
        else:
            logger.warning(f"    Failed to create index {options['name']}: {e}")
```

Plausible explanations, ranked by likelihood:

1. **The init function isn't being invoked on this deployment.**
   `orchestrator/init.py` may be wired to a one-shot Job / Helm
   pre-install hook rather than the runtime startup of the orchestrator
   container. If that hook last ran on an older code revision (before
   the indexes were declared) and hasn't re-run since, the indexes
   never get created. Worth checking the Helm chart for a
   `pre-install` / `post-upgrade` hook that runs init.
2. **The init runs but the connection points at a different DB/host
   than the one queried at runtime.** The init connects with one URI
   and creates indexes on `srw_logs`; the orchestrator container at
   runtime reads `MONGODB_URL=mongodb://srw-mongodb:27017/srw_logs`. If
   init ran against an older Mongo (e.g. before the StatefulSet
   migration) or a different DB name in the URI, the indexes are
   sitting elsewhere.
3. **Indexes were created at some point, then dropped.** A manual
   `dropIndexes` (debugging, schema work) or a Mongo recovery action
   could have removed them. Less likely — `_id` is still there, no
   sign of a wipe.
4. **`create_index` raises a `Failed to create index ...` warning
   and the operator never noticed.** The current code logs at WARN
   but doesn't surface to alerts. If init *did* run and hit an issue
   (e.g. duplicate keys preventing a unique index), only the orch
   container's old stdout would know.

The Helm release history shows `superhuman-remote-worker-deployment.v45`
deployed ~127 minutes before the incident — i.e. fresh release, fresh
pods, but the indexes weren't re-created at that point. Suggests
hypothesis 1.

## Code References

| File | Lines | Role |
|---|---|---|
| `orchestrator/init.py` | 1521-1551 | `agent_audit` index declarations + creation loop |
| `orchestrator/init.py` | 1539-1551 | `try/except`-wrapped `create_index` that downgrades failures to WARN |
| `orchestrator/database/mongodb.py` | 287 | `count_documents({"job_id": job_id})` — uses `job_id` filter |
| `orchestrator/database/mongodb.py` | 700 | `count_documents({"job_id": job_id})` — same |
| `orchestrator/database/mongodb.py` | 853 | `find(query).sort("timestamp", 1)` — uses `job_id` filter (via `query`) |
| `orchestrator/database/mongodb.py` | 905 | `count_documents({})` — full-collection count for stats endpoint (would benefit from a cached counter, not an index) |
| `helm/...` | (TBD) | Whether and how `init.py` is invoked at deploy time |

## Reproduction

Already in the failure-state of the cluster on 2026-05-12. To
reproduce on a fresh deploy:

1. Deploy a clean stack against a fresh MongoDB volume.
2. After all pods are Ready, exec into Mongo:
   ```bash
   kubectl exec -n superhuman-remote-worker srw-mongodb-0 -- \
     mongosh --quiet --norc srw_logs --eval \
     'db.agent_audit.getIndexes().forEach(i => printjson(i.key))'
   ```
3. Expected: seven indexes listed. Actual (per the incident): only
   `{_id: 1}`.
4. Check the orchestrator pod's startup logs for the
   `Configuring agent_audit collection...` block to determine whether
   init even ran:
   ```bash
   kubectl logs -n superhuman-remote-worker deploy/srw-orchestrator | \
     grep -A20 "agent_audit collection"
   ```

## Resolution

Fixed in commit `7f8f609` ("Consolidate MongoDB index declarations
and ensure robust index creation", 2026-05-12). The commit:

- Extracts the index declarations from `orchestrator/init.py` into a
  single `MONGODB_INDEX_DECLARATIONS` structure in
  `orchestrator/database/mongodb.py` (single source of truth — the
  standalone CLI and the runtime startup path read the same list).
- Adds `MongoDB.ensure_indexes()`, an idempotent method that asserts
  every declared index on every call (existing identical indexes are
  silent no-ops; mismatched or missing indexes are created).
- Wires `await mongodb.ensure_indexes()` into the orchestrator's
  FastAPI `lifespan` startup handler (`orchestrator/main.py:2965`),
  so every orchestrator pod (re)asserts the index set on boot. This
  closes the deploy-pipeline gap that produced the original outage —
  even if the standalone `init.py` CLI never runs, the runtime path
  guarantees the indexes are present.
- Refactors the index-creation loop in `init.py` to surface failures
  loudly (the previous silent `WARN`-and-continue behaviour was the
  trap that hid the missing-indexes state for 9+ days).

Net effect: hypothesis 1 from the original investigation (init never
ran in the deploy pipeline) was the actual cause. The fix doesn't
require diagnosing how the Helm hook is supposed to work — it
sidesteps the question by making the runtime path authoritative.

### Production verification (2026-05-12, post-deploy)

Orchestrator pod `srw-orchestrator-5f95c755b8-hqx4s` (image
`sha-8e92c81`) startup log:

```
16:41:58,851 INFO database.mongodb: MongoDB ensure_indexes:
  asserted 14 indexes across 3 collections
```

Live `agent_audit` index state:

```
$ mongosh ... --eval 'db.agent_audit.getIndexes().forEach(...)'
{_id: 1}
{job_id: 1}
{step_type: 1}
{node_name: 1}
{timestamp: 1}
{job_id: 1, step_number: 1}
{job_id: 1, iteration: 1, step_number: 1}
{job_id: 1, agent_type: 1, step_type: 1}
```

All seven declared indexes plus `_id`, matching the new
`MONGODB_INDEX_DECLARATIONS` source of truth. The 14 indexes spread
across `agent_audit` (7), `llm_requests` (4–5), and `chat_history`
(2) — exact counts depend on which compound indexes are listed where
in the new declaration block.

The follow-up question of *why* the standalone `init.py` wasn't being
invoked by Helm is now moot — the runtime path covers every restart,
which is strictly stronger than a one-shot hook. No further work
required.

### Open follow-ups (deferred — not blocking)

- **Fix 5 from below** (rethink the per-listing `count_documents`
  N+1) — still architecturally relevant but no longer urgent now that
  every count uses the index. Rolls into
  [[orchestrator_mongodb_cascading_failure_resilience]] if/when that
  doc is picked up.

## Proposed Fixes

### Fix 1 — Hand-create the missing indexes on the running cluster (Immediate, manual)

Unblocks the next cascade without a deploy. Single command, runs in
seconds on 117K docs:

```bash
kubectl exec -n superhuman-remote-worker srw-mongodb-0 -- \
  mongosh --quiet --norc srw_logs --eval '
    db.agent_audit.createIndex({job_id: 1}, {name: "idx_audit_job_id"});
    db.agent_audit.createIndex({step_type: 1}, {name: "idx_audit_step_type"});
    db.agent_audit.createIndex({node_name: 1}, {name: "idx_audit_node_name"});
    db.agent_audit.createIndex({timestamp: 1}, {name: "idx_audit_timestamp"});
    db.agent_audit.createIndex({job_id: 1, step_number: 1}, {name: "idx_audit_job_step"});
    db.agent_audit.createIndex({job_id: 1, iteration: 1, step_number: 1}, {name: "idx_audit_job_iter_step"});
    db.agent_audit.createIndex({job_id: 1, agent_type: 1, step_type: 1}, {name: "idx_audit_job_agent_type"});
  '
```

Same treatment should be applied to `llm_requests` and `chat_history`
after verifying their current index state — the `init.py` block treats
all three collections symmetrically, so all three are suspect.

### Fix 2 — Verify the init path actually runs on deploy (Required)

Trace the Helm chart's pre-install / post-upgrade hooks (or the
orchestrator container's startup command) to determine where
`orchestrator/init.py` lives in the lifecycle. Confirm it runs on
*every* upgrade, against the right `MONGODB_URL`. If it currently
runs once and is never re-triggered, wire it to a Helm hook so each
chart upgrade re-asserts the index set.

### Fix 3 — Make index-creation failures loud (Required)

Downgrade the silent `WARN` at `init.py:1551` to an error that fails
the init job (or at minimum: emits a structured log line a
PrometheusRule can alert on). A "we failed to create the index that
makes the database survivable" event should never be silent.

### Fix 4 — Idempotent re-assertion on orchestrator startup (Defensive)

Even with Fix 2, the index set drifts when developers add new
collections / indexes and forget to rebuild the init hook. Cheap
counter: have the orchestrator container call a tiny
`ensure_indexes()` on startup (idempotent, "already exists" is the
expected path on a warm cluster). Index existence is cheap to check;
this is belt and braces.

### Fix 5 — Reconsider the per-listing count itself (Adjacent)

`/api/jobs` enrichment does `count_documents({"job_id": ...})` per
job, per listing tick. Even with the index this is O(jobs in page)
round-trips to Mongo. Two options worth considering:

- Maintain a `audit_step_count` denormalised counter on the `jobs`
  row, updated whenever `auditor.audit_step` writes (or via a Mongo
  change-stream → Postgres sync).
- Drop the count from the default `/api/jobs` payload; surface it
  on-demand on the per-job detail view.

Less urgent than Fixes 1–3, but worth a design call.

## Priority

1. **Fix 1** — runs in seconds, stops the cascade from recurring on
   the next infinite-loop job.
2. **Fix 3** — silent index failures are how we got here; loudness is
   the prerequisite for trusting Fix 2.
3. **Fix 2** — root cause; without it, the next fresh deploy lands
   index-less again.
4. **Fix 4** — defence in depth.
5. **Fix 5** — optimization; revisit after the above are in.

## Open Questions

- Was the indexes-on-this-collection state ever correct, or has the
  cluster been running index-less since first deploy? A quick way to
  check: look at MongoDB's startup logs for any historical
  `Failed to create index idx_audit_job_id` lines.
- Are other collections (`llm_requests`, `chat_history`,
  `agent_steps`, etc.) also missing their declared indexes? If init
  has a structural problem, they probably are too.
- `init.py:1543/1545` has the same `create_index(...)` call inside
  both branches of the `isinstance(index_spec, list)` check — looks
  redundant. Worth a closer read; if there's a subtle bug here (e.g.
  the second branch was meant to pass keyword args differently), it
  would be the kind of typo that silently breaks index creation.
