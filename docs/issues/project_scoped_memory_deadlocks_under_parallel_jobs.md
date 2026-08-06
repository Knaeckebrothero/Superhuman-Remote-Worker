---
tags:
  - issue
  - memory
  - postgres
  - concurrency
  - jobs
  - scholar
related:
  - "[[agent_memory_overhaul]]"
  - "[[phase_model_overhead_amnesia_loop]]"
  - "[[overnight_minimax_m3_scholar_batch_2026-08-03]]"
aliases:
  - concurrent project memory deadlocks
  - recall store write-on-read deadlock
---

# Project-scoped memory deadlocks under concurrent jobs

**Filed:** 2026-08-04 from the five-job main-cluster overnight Scholar batch.

**Status:** **CONTAINMENT TIER SHIPPED 2026-08-06 (batch #2); the semantic
per-consumer model below remains OPEN.** What shipped (src/services/
recall_store.py):
- `decrement_ttl()` and the access-stat write both lock their target rows via
  an id-ordered `SELECT … FOR UPDATE` CTE feeding the UPDATE — concurrent
  consumers acquire overlapping tuple locks in one deterministic order, which
  removes the lock-order cycles behind the 138 observed deadlocks.
- The access-stat write moved into `_record_access_stats()`: ids sorted,
  bounded deadlock-only retry (`_ACCESS_STAT_RETRY_DELAYS`, 3 attempts), and
  EVERY failure contained — a lost access-count bump can no longer abort an
  otherwise-successful retrieval (previously an uncaught deadlock here threw
  away the already-fetched rows; that was the bulk of the count).
- Contained-error counters (`MemoryHealth` singleton: ttl_decrement_deadlock,
  access_stats_deadlock, access_stats_error, retrieval_deadlock) ride the
  agent heartbeat as `metrics["memory"]` into `agents.metadata` — operator
  telemetry, not just pod logs (acceptance criterion 6).
Tests: `TestDeadlockContainment` (tests/test_recall_store.py, mock-level SQL
shape/retry/containment) + tests/test_recall_store_concurrency.py (real
pgvector-Postgres via testcontainers + the real vector migrations: two
concurrent stores over one project, zero unhandled errors, exact access-count
accounting under opposite-order hammering). Heartbeat wiring pinned for all
three app variants.
Still OPEN (out of batch scope, deliberately): the correct per-consumer
delivery model — acceptance criterion 3 (one job's turns must not decrement
another's remaining injection lifetime) is NOT met by containment; shared TTL
still ages ~N× faster under N parallel consumers. Criterion 5 (stable
relevance-budget share under pinned-pool pressure) also remains open (P-3).

**Originally:** OPEN. P1 concurrency / context-quality defect. The failures are
contained, so all five jobs completed, but affected turns silently lose the
shared memory retriever and collectively place heavy avoidable write pressure on
Postgres.

## Summary

Five jobs in the same project started within 20 seconds of one another. Their
archived pod logs recorded 138 `recall_two_tier` retrieval deadlocks while they
read the same project-scoped memory set. The same runs also recorded TTL-update
deadlocks/timeouts and failed retrieval-message writes.

| Job | Retrieval deadlocks | TTL deadlocks | TTL timeouts | Retrieval-message write failures | Pinned-budget truncations |
|---|---:|---:|---:|---:|---:|
| control | 26 | 1 | 0 | 1 | 3 |
| 10-turn readers | 32 | 1 | 1 | 3 | 3 |
| 24-turn readers | 31 | 1 | 2 | 6 | 3 |
| paper review | 32 | 1 | 0 | 1 | 3 |
| web comparison | 17 | 1 | 1 | 6 | 3 |
| **Total** | **138** | **5** | **4** | **17** | **15** |

Representative worker warning:

```text
Memory retriever 'recall_two_tier' failed (contained):
DeadlockDetectedError: deadlock detected
```

The manager's containment preserved job execution, which is correct. It also
means the model simply continued without that retriever's context on every
affected turn. The jobs' terminal success must not be misread as memory-system
success.

## Source-level cause

The current project memory read path performs multiple synchronous writes on
shared rows for every consumer turn:

1. `RecallTwoTierRetriever.retrieve()` calls `RecallStore.decrement_ttl()`.
2. For a project-scoped store, `decrement_ttl()` updates **every** active memory
   in the project:

   ```sql
   UPDATE memories
      SET remaining_turns = remaining_turns - 1
    WHERE <project scope> AND remaining_turns > 0 AND valid_to IS NULL
   ```

3. `RecallStore.hybrid_search()` selects overlapping top memories and then
   updates their `access_count` and `last_accessed` in one `id = ANY($1)` write.
4. Five workers execute those two write shapes concurrently over nearly the
   same row set. PostgreSQL can acquire the overlapping tuple locks in different
   orders, producing the observed cycles.

There is also a semantic ownership defect independent of locking: a shared
memory's `remaining_turns` is decremented once per **consumer job turn**. Five
parallel jobs therefore age project memory roughly five times as quickly as one
job. A field on the shared memory row cannot represent per-consumer injection
lifetime correctly.

The 15 identical pinned-budget warnings confirm the pre-existing P-3 pressure:
each job's first turns encountered almost 10,000 tokens of TTL-active memory and
truncated the tier before relevance retrieval could receive a dependable share.

## Consequences

- Project concurrency makes shared context availability nondeterministic.
- TTL lifetime depends on unrelated sibling-job traffic.
- Postgres spends substantial work detecting/rolling back deadlocked
  transactions.
- Contained retrieval failures are visible only in archived pod logs, not in the
  normal job/audit summary.
- The missing memory may change model decisions, but this batch cannot attribute
  a specific report error or finalization loop to any one deadlock.

## Fix direction

### Correct model

Move consumer-specific delivery state off the shared memory row. For example,
store TTL/pin/injection state keyed by `(memory_id, consumer_id)` where the
consumer is a job or persistent session. A project memory can remain shared;
its per-consumer remaining lifetime cannot.

Access tracking should not make the critical retrieval response depend on
updating the selected memory rows. Prefer an append-only/buffered access event or
an asynchronous best-effort aggregate. Losing one access counter update is
better than losing the retriever result.

### Safe interim containment

If the larger data-model change is deferred:

- acquire overlapping memory locks in a deterministic ID order;
- bound and retry `DeadlockDetectedError` for the access-stat write only;
- separate TTL mutation from retrieval and prevent one project-wide update per
  worker turn; and
- expose contained-memory-error counts in job telemetry.

Ordering/retry reduces deadlocks but does not correct the shared-TTL semantics,
so it is not the complete fix.

## Acceptance criteria

1. Run at least five concurrent jobs against the same project memory corpus.
2. No retrieval or TTL transaction deadlocks occur.
3. One job's turns do not decrement another job's remaining injection lifetime.
4. Access-stat persistence cannot suppress an otherwise-successful retrieval.
5. Every job receives a stable relevance-budget share even when the pinned pool
   is larger than the configured memory budget.
6. Contained memory degradation is visible in operator/job telemetry, not only
   pod logs.

This issue is separate from runtime loop containment. The correct response to a
memory deadlock is to make shared-memory concurrency safe, not to give the model
fewer turns.
