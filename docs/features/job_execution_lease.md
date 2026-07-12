# Job execution lease — liveness by renewal, not inference

## Status

Design proposal — 2026-07-12. Motivated by
[`docs/issues/stale_agent_detector_sql_crash_disables_recovery_sweeps.md`](../issues/stale_agent_detector_sql_crash_disables_recovery_sweeps.md)
(two loop jobs permanently wedged in `processing` within 24 h, via two
unrelated agent deaths). Not yet implemented. Complementary to — not competing
with — [`unified_instance_lifecycle.md`](unified_instance_lifecycle.md): the
lease is the truth mechanism for *job execution*, the reconciler is the truth
mechanism for *instance lifecycle*.

## Problem

A job is "running" today because `jobs.status='processing'` and
`assigned_agent_id` points at an agent row. Whether that is *true* is checked
indirectly, through a four-link chain:

1. the agent heartbeats every 5 s → `agents.last_heartbeat`
2. `mark_stale_agents_offline` flips silent agents to `offline` (60 s sweep)
3. `recover_orphaned_jobs` joins `processing` jobs against
   offline/deleted/non-working agents and pauses them
4. the dispatcher re-queues the paused job

Every link, their ordering inside one loop, and their shared `try` block are
load-bearing. The 2026-07-11 incidents broke link 3 (an unrelated sweep in the
same try block crashed) and the result was jobs stuck in `processing` forever —
with a *dispatcher design that explicitly relies on this chain instead of
rolling back failed handoffs* (`main.py:4770` — "a failed dispatch/resume below
self-heals via recover_orphaned_jobs").

Additional structural weaknesses of the inference chain:

- The dead-pod claim race: a one-shot worker heartbeats `ready` then
  `os._exit(0)`s; its row looks dispatchable for up to ~3 min. A job claimed
  for it in that window strands until link 3 notices.
- Recovery depends on the *agents* table being reconciled first — a cross-table
  ordering invariant nobody enforces.
- No single place answers "is this job actually being executed right now?"

## Design

Execution is a **lease** that must be actively renewed; an expired lease *is*
the definition of orphaned. This is the standard mechanism in mature job
systems (SQS visibility timeout, Temporal task tokens, Kubernetes Leases).

### Schema

Migration `orchestrator/database/migrations/app/00XX_jobs_execution_lease.sql`:

```sql
ALTER TABLE jobs ADD COLUMN lease_expires_at timestamptz;
CREATE INDEX jobs_lease_expiry_idx ON jobs (lease_expires_at)
    WHERE status = 'processing';
-- Backfill so in-flight jobs at deploy time either renew or expire cleanly:
UPDATE jobs SET lease_expires_at = NOW() + interval '5 minutes'
    WHERE status = 'processing';
```

(Then regenerate `schema_current.sql` via `scripts/schema-snapshot.sh` —
mandatory, CI fails otherwise.)

Only `processing` holds a lease. `paused`, `waiting`, `created`,
`pending_review` etc. carry `NULL` — the expiry sweep never looks at them.

### Acquire — at claim

`claim_job_for_agent` (`postgres.py:3073`), the single CAS every dispatch path
funnels through, additionally sets a short **pickup lease**:

```sql
SET status = 'processing', assigned_agent_id = $2,
    lease_expires_at = NOW() + interval '180 seconds', ...
```

180 s covers the dispatch/resume POST plus agent init and workspace connect.
If the POST fails, or the pod is already dead, or the agent never comes up —
nothing renews, the pickup lease lapses, the job is recovered. **This makes
the dispatcher's no-rollback design sound again** without adding rollback
logic (inline rollback remains optional hygiene, see the issue doc's F6).

Audit step during implementation: grep for every write of
`status = 'processing'` outside `claim_job_for_agent` — each must set a lease
or be refactored onto the claim path.

### Renew — via the existing agent heartbeat

The heartbeat handler (`POST /api/agents/{id}/heartbeat`) already knows the
agent's current job. When the heartbeat carries a job id:

```sql
UPDATE jobs SET lease_expires_at = NOW() + interval '90 seconds'
 WHERE id = $1 AND assigned_agent_id = $2 AND status = 'processing';
```

The `assigned_agent_id = $2` guard is essential: an agent that lost the job
(re-dispatched elsewhere) cannot extend a lease it no longer owns. 90 s = 18
missed 5 s heartbeats — generous against transient stalls, small against a
wedged loop. Both set and compare use the **database clock** (`NOW()`), so
orchestrator/agent clock skew is irrelevant.

### Expire — one self-contained sweep

```sql
UPDATE jobs SET status = 'paused', assigned_agent_id = NULL,
       lease_expires_at = NULL, updated_at = CURRENT_TIMESTAMP
 WHERE status = 'processing' AND lease_expires_at < NOW()
RETURNING id;
```

No join to `agents`, no dependency on offline-marking having run, no ordering
requirement against any other sweep. CAS semantics make it safe under the
transient dual-leader window. Each recovered id is logged loudly (these are
incidents, not noise) and counted in metrics.

### Fencing the zombie window

An agent can outlive its lease (heartbeat starvation while the event loop
still runs — observed for 60 s in incident 2). Two guards bound
double-execution:

1. **Completion CAS**: `report_completion` handling verifies
   `assigned_agent_id` still matches the reporting agent; a completion from a
   fenced-out agent is rejected and logged (today it would silently win).
2. **Lost-lease intent**: the heartbeat *response* already carries intents
   (`should_drain`). Add `lost_lease`: when an agent heartbeats with a job it
   no longer owns, the response tells it to abort the graph run and reset to
   idle — reusing the drain-intent machinery in `dual_app.py`.

Residual risk — a fenced-out agent's workspace writes racing the replacement's
— is the same risk checkpoint-resume already carries today (resume replays
work against the same workspace); the lease narrows the window, it does not
introduce it.

## What the lease does NOT replace

- `mark_stale_agents_offline` — still wanted for *agent slot* hygiene and
  thread propagation; it just stops being load-bearing for job recovery.
- The graph-progress stall sweep (L3) — catches the *opposite* failure: an
  agent that heartbeats fine (thus renews fine) but makes no progress. Lease
  and graph-progress are complementary detectors.
- `recover_orphaned_jobs` — keep during soak as belt-and-suspenders; demote to
  a consistency assertion (log if it ever finds something the lease missed)
  once the lease has survived a few weeks of prod, then retire.

## Rollout

1. Migration + snapshot regen. Additive, no behavior change.
2. Claim sets pickup lease; heartbeat renews. Deploy — leases now track
   reality but nothing acts on them. Watch the column on live jobs.
3. Enable the expiry sweep (isolated step, own try/except per the incident
   doc's F2). Soak with `recover_orphaned_jobs` still active.
4. Completion CAS + `lost_lease` intent (agent release required — ships with
   the next agent image).
5. After soak: demote `recover_orphaned_jobs` to assertion mode.

## Acceptance criteria

1. k3d: `kubectl delete pod --grace-period=0` on a busy agent → job `paused` +
   unassigned within ≤ 2 sweep ticks and re-dispatched, **with the agents
   table frozen** (temporarily disable offline-marking to prove independence).
2. Failed dispatch handoff (POST to a dead IP) → job recovered by pickup-lease
   expiry within ~3 min, no manual intervention, no rollback code path.
3. A re-dispatched job's original agent cannot renew (unit test on the
   renewal CAS) and its late completion is rejected (completion CAS test).
4. A healthy long-running job (LLM call > 90 s) never loses its lease —
   renewal rides heartbeats, not graph progress.
5. Sweep executes against real Postgres in the test tier (bind-type
   regression guard per incident doc F8).
