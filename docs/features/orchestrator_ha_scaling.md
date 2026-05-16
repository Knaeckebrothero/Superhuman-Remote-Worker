---
tags:
  - feature
  - orchestrator
  - infrastructure
  - architecture
  - reliability
aliases:
  - orchestrator HA
  - multi-replica orchestrator
  - orchestrator scaling
  - leader election
related:
  - "[[unified_instance_lifecycle]]"
  - "[[headless_persistent_sessions]]"
  - "[[stuck_agent_recovery]]"
  - "[[job_auto_assign]]"
  - "[[sudo_permissions]]"
---

# Orchestrator HA & Scaling

> One orchestrator pod is a single point of failure and a single point of throughput. Today the system tolerates the failure mode — pause + re-dispatch — but it can't survive a 30-minute outage gracefully and can't scale past one CPU. This feature plans the move from `replicas: 1` to `replicas: N` in two phases: fast failover first, true horizontal scale-out second.

**Status:** Design / brainstorm. No implementation yet.
**Filed:** 2026-05-12

## Motivation

`deployment/legacy/20-orchestrator.yaml:9` pins `replicas: 1` for the orchestrator. There is exactly one process making dispatch decisions, holding the WebSocket fan-out for every persistent session, listening on NATS, sweeping stuck jobs, expiring sudo approvals, and reconciling the agent pool. When that pod gets evicted or OOMs:

- Inbound REST traffic dies until Kubernetes re-rolls (15-60s on a healthy cluster, multiple minutes on a degraded one).
- All in-flight WebSocket connections drop and the cockpit shows the "silent disconnect" failure mode tracked in `docs/issues/persistent_chat_silent_disconnect.md`.
- Any NATS reply landing during the gap is dropped — the durable consumer is recoverable but the in-process `_pending_msgs` map is not.
- Background sweepers don't run; orphaned-job recovery stalls.

The system *does* survive — agents heartbeat-time-out cleanly, jobs auto-pause, persistent sessions reattach after the bounce. The user-visible damage is a multi-minute UI blackout, not data loss. But the failure mode is unnecessarily disruptive for what's structurally a stateless web service, and the single-process ceiling caps throughput when we eventually want to run more than a few hundred concurrent persistent sessions.

This feature has two motivations stacked together:

1. **High availability.** Survive a pod eviction with sub-second user-visible impact. This is the everyday case (node drain, image roll, OOM).
2. **Horizontal scaling.** Run multiple orchestrator pods to spread CPU and connection load. This is the future case (more users, more persistent sessions, longer event-log fan-out work).

Both want `replicas: N`. The work that gets us to `replicas: 2` is the same work that gets us to `replicas: 8`.

## What blocks `replicas: 2` today

The DB layer is fine — migrations already coordinate via `pg_advisory_xact_lock` (`orchestrator/database/migrate.py:157`), Postgres handles concurrent writers, and JSONB merges use atomic SQL. The blockers are all *runtime* singletons in the orchestrator process.

### In-process locks and sets

| What | Where | What breaks with 2 replicas |
|---|---|---|
| `_dispatch_lock` (asyncio.Lock) | `orchestrator/main.py:388` | Each replica has its own lock — both can run `auto_assign_dispatcher` concurrently and assign the same `created` job to two different agents. |
| `_pause_pending_job_ids: set[str]` | `orchestrator/main.py:391` | Pause requests in flight on replica A are invisible to replica B; B can re-preempt a job A is already pausing. |
| `_thread_turn_locks: dict[(thread_id, turn_id), Lock]` | `orchestrator/main.py:10693` | Per-turn input serialization. Multi-tab POSTs to different replicas both win the local lock; double-turn races. |
| `_thread_turn_inflight: dict[thread_id, int]` | `orchestrator/main.py:10694` | Same problem; the "is this turn already running" check is per-process. |
| `_project_heal_locks: dict[project_id, Lock]` | `orchestrator/main.py:14623` | Two replicas can race on `ensure_project_folder` for the same project (mostly idempotent but logs warnings + duplicate cloud calls). |
| `_knowledge_graph_db` (Neo4j client cache) | `orchestrator/main.py:15567` | Each replica gets its own driver. Not a correctness issue; just doubles the Neo4j connection pool. |

### Push/pull channels with in-memory subscribers

| What | Where | What breaks |
|---|---|---|
| `notification_feed._user_queues: dict[user_id, list[asyncio.Queue]]` | `orchestrator/services/notification_feed.py:23` | SSE clients on replica B don't receive events emitted on replica A. Whichever replica handles the trigger event keeps the message to itself. |
| `sudo_gate._sse_queues: list[asyncio.Queue]` | `orchestrator/services/sudo_gate.py:35` | Same problem for sudo-approval streams. |
| `sudo_gate._pending_msgs: dict[req_id, nats.Msg]` | `orchestrator/services/sudo_gate.py:37` | A NATS reply landing on replica B can't find the original `respond()` handle stored on replica A. The agent waits forever. |
| `persistent_ws_proxy` WS fan-out (`orchestrator/main.py:11370`) | (per-process) | The cockpit's persistent-chat WebSocket terminates on whichever replica it hit; if the agent pod's events route through a different orchestrator replica, the WS sees nothing. (Less of an issue once [[headless_persistent_sessions]] ships SSE: stream replay is from `thread_events` in Postgres, no in-process fan-out.) |

### Background loops that would double-fire

All started in the `lifespan` handler at `orchestrator/main.py:2973`, registered around `3158-3222`:

| Loop | File / line | Double-fire harm |
|---|---|---|
| `auto_assign_dispatcher` | `main.py:2269` | Double-assigns jobs. Critical. |
| `stale_agent_detector` | `main.py:394` | Idempotent DB writes (mark offline twice = same row), but `recover_orphaned_jobs` is racy with itself across replicas — two replicas could re-dispatch the same orphan to different agents. |
| `agent_pool_reconciler` | `main.py:468` | Over-provisions pods (each replica thinks the pool is short by N). |
| `lifecycle_reconciler_loop` | `main.py:497` | Double drift-detection; can double-drain. |
| `workspace_idle_sweeper` | `main.py:574` | Double-suspend attempts. Largely idempotent but logs noise. |
| `snapshot_gc_sweeper` | `main.py:606` | Idempotent if guarded by S3 ETag, but worth confirming. |
| `imap_poll_loop` | `main.py:681` | **Both replicas read the same IMAP mailbox.** Each inbound email gets processed twice → duplicate sudo approvals / duplicate thread replies. |
| `sudo_expiration_sweeper` | (registered `main.py:3163`) | Idempotent DB deletes. Safe. |
| `thread_events` prune sweeper | (registered `main.py:3164`) | Idempotent. Safe. |
| `delegation_timeout` handler | (registered `main.py:3172`) | Needs investigation; likely racy. |
| `quiet_hours_digest_loop` | (registered `main.py:3171`) | Double-sends digest emails. |
| `main_cloud_listen_task` | (registered `main.py:3222`) | Both replicas subscribe to the cloud-events stream and double-process. |

### NATS subscriptions

`nats_bridge.py` subscribes to `vm.lifecycle.status`, `agent.vm.*.register`, `agent.vm.*.heartbeat`, `agent.vm.*.status`. Without queue groups, every replica receives every message and runs the handler. The handlers are partially idempotent (DB updates) but `_thread_vm_ids: set[str]` (`nats_bridge.py:68`) diverges between replicas, breaking the thread-vs-job routing decision.

### Summary of the blast radius

The dispatch double-assignment and the IMAP poll double-process are the two correctness bugs. Everything else is either idempotent (annoying but safe), or it's a "messages go to the wrong replica" issue that degrades UX without corrupting data. That asymmetry shapes the decision below.

## Decision: active-passive first, active-active as the roadmap

There are two viable end-states. They differ by a lot of engineering effort.

**Active-passive (Track 1).** Single replica running, second replica is hot standby (or no standby; K8s replaces fast). The active pod runs every loop, holds every WS connection, owns the dispatch lock. On crash, Kubernetes spins up a replacement; the existing system mechanics (heartbeat-driven offline detection, orphan auto-pause, WS reconnect) absorb the gap. **Trade-off:** brief blackout during failover (~15-30s with tightened probes), throughput capped at one CPU.

**Active-active (Track 2).** Multiple replicas, all serving traffic. Background loops run on exactly one replica via leader election. Dispatch uses DB-level row locks. Cross-replica fan-out for SSE/WS/sudo via Postgres LISTEN/NOTIFY. NATS queue groups for one-message-one-consumer. **Trade-off:** real horizontal scale + zero-blackout failover, multi-week refactor of a dozen call sites.

**Chosen: ship Track 1, build Track 2 incrementally.** Track 1 is mostly probe tuning, a `PodDisruptionBudget`, and confirming the existing recovery paths actually work end-to-end under chaos. Track 2 lands as a sequence of independent PRs (leader election, then DB-level dispatch, then fan-out, then NATS) where each one improves correctness even before the full active-active goal is reached.

**Why not just skip to Track 2.** Track 1 captures 80% of the operational benefit (no all-day outages from a wedged pod) for 10% of the work. It also de-risks Track 2: by the time we have a working leader election and DB-level dispatch, we already have a battle-tested active-passive deployment, and flipping from `replicas: 1` to `replicas: 2` becomes a config change, not a feature.

## Track 1 — Active-passive (P0)

Single replica with fast, well-understood failover. Most of the work is verifying existing recovery paths under chaos, not writing new code.

### What's already working

- **Job auto-pause on agent offline.** `recover_orphaned_jobs` (`postgres.py`) flips processing jobs whose agent missed three heartbeats back to `paused`. Dispatch picks them up on next loop. So a crashed orchestrator + the agents heartbeat-timing out + the new orchestrator dispatching = automatic recovery.
- **Persistent session reattach.** [[headless_persistent_sessions]] (Phase 2) writes the wire-level event stream to `thread_events` before broadcasting. A reconnecting cockpit replays from `Last-Event-ID`. The orchestrator pod restarting drops the WS but the agent pod keeps running and keeps writing; reconnect after the bounce catches up.
- **Migration safety.** `pg_advisory_xact_lock` (`migrate.py:157`) means concurrent orchestrator startups can't double-apply migrations. Two replicas during a rolling deploy is already safe at that layer.
- **NATS reconnect.** `nats-py` reconnects on its own; durable consumers (where used) replay missed messages. The non-durable ones still drop messages during a bounce, but that's a Track 2 fix.

### What needs to change

| Change | Where | Effort |
|---|---|---|
| Tighten readiness probe to drain in-flight requests before pod termination. | `deployment/legacy/20-orchestrator.yaml:549` | 1h |
| Add `terminationGracePeriodSeconds` + a `preStop` hook that sleeps long enough for the load balancer to deregister the endpoint. Pattern: 30s sleep, 60s grace. | same | 1h |
| Add a `PodDisruptionBudget` with `minAvailable: 0` (since we run 1 replica, this is mostly documentation, but flips to `minAvailable: 1` for Track 2 trivially). | new file in `deployment/legacy/` | 30m |
| Confirm liveness probe doesn't kill the pod during a long startup (migrations on a populated DB can take seconds). `initialDelaySeconds: 30` is current; verify with a populated DB. | `deployment/legacy/20-orchestrator.yaml:543` | 30m + test |
| Move the four module-level Neo4j / DB caches behind `lifespan` startup so a SIGTERM cleanly closes them (currently they leak briefly during pod termination, which is harmless but ugly in logs). | `main.py:15567` and similar | 2h |
| Chaos-test the failover end-to-end: `kubectl delete pod srw-orchestrator-...` while (a) a job is dispatching, (b) a sudo prompt is open, (c) a persistent session is mid-turn. Measure observable downtime. | manual / scripted | 1d |

Track 1 should land in under a week of focused work, mostly testing.

### What Track 1 explicitly does *not* fix

- Per-pod connection load. One replica still holds every WebSocket; if the persistent-session count grows past a few hundred concurrent, the single pod starts to feel it.
- IMAP double-poll if anyone ever sets `replicas: 2`. Track 1 keeps `replicas: 1` so this isn't a problem in production, but it's a foot-gun.
- The 15-30s failover blackout. Acceptable for everyday operation; not acceptable for "zero-downtime deploy" or for users sensitive to dropped WS connections.

## Track 2 — Active-active (P1, multi-phase)

Three layers of work, each independently shippable. The order matters: leader election first, then DB-level coordination, then cross-replica fan-out.

### Layer 1: Leader election for singleton loops

The cheapest, most-impactful change. Most of the background loops are not stateless — they shouldn't run on every replica even if we wanted to. Wrap each loop in a leader-election guard.

**Primitive: Postgres advisory locks.** Already used in `migrate.py`; no new infra. Each singleton loop holds a session-scoped advisory lock on a unique integer key for its lifetime. If the lock is taken, the loop sleeps and re-tries to acquire periodically (say every 30s). On the leader's death, its session closes, the lock releases, a follower takes over within 30s.

```python
async def with_leader_lock(name: str, loop_id: int, run: Callable[[], Awaitable[None]],
                           shutdown_event: asyncio.Event) -> None:
    while not shutdown_event.is_set():
        async with db_pool.acquire() as conn:
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", loop_id)
            if got:
                logger.info(f"[{name}] acquired leadership")
                try:
                    await run()  # runs until shutdown_event or conn dies
                finally:
                    await conn.execute("SELECT pg_advisory_unlock($1)", loop_id)
                    logger.info(f"[{name}] released leadership")
            else:
                logger.debug(f"[{name}] not leader, sleeping")
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass
```

Key properties:

- **Session-scoped lock** (not transaction-scoped): the lock lives as long as the Postgres session does. If the pod is OOM-killed or network-partitioned, the lock releases when Postgres reaps the dead connection (TCP keepalive + `idle_session_timeout`).
- **No fencing token needed** at this layer: the loops are coarse (30-60s tick rate), and the worst-case "two leaders for 30 seconds during a partition" is recoverable because the underlying DB operations are idempotent (Track 1's existing safety carries through).
- **Stable lock IDs.** Allocate a registry of `(name, id)` pairs in a constants module; never reuse, never collide with the migration lock (`MIGRATION_LOCK_ID`).

Loops to wrap:

- `auto_assign_dispatcher` — singleton. (Even with DB-level dispatch from Layer 2, having only one dispatcher running reduces useless DB traffic.)
- `stale_agent_detector`, `agent_pool_reconciler`, `lifecycle_reconciler_loop` — singletons.
- `imap_poll_loop` — singleton (closes the duplicate-email correctness bug).
- `quiet_hours_digest_loop` — singleton.
- `workspace_idle_sweeper`, `snapshot_gc_sweeper`, `thread_events_prune`, `sudo_expiration_sweeper` — all idempotent; wrapping is "cleanliness" not "correctness." Wrap them anyway for log clarity.
- `delegation_timeout` handler — investigate, then wrap.

Time-bounded loops (per-request side effects fired via `asyncio.create_task`) are *not* wrapped; they're per-request work, not background loops.

**Alternatives considered (not chosen):**

- **Kubernetes Lease objects.** The k8s-native pattern, used by cert-manager and similar. Pros: no Postgres dependency for leadership; survives a Postgres outage. Cons: requires RBAC plumbing, an extra k8s client library, and we'd have to thread the lease holder identity through every loop. Postgres advisory locks reuse infra we already have and "Postgres is down" implies the orchestrator is down anyway.
- **Redis with `SET NX PX`.** Same shape as advisory locks but requires Redis, which we don't run. No-go.
- **etcd / Consul.** Same objection: extra infra. We are Postgres-first.

### Layer 2: DB-level job dispatch

`_dispatch_lock` becomes unnecessary. Replace with `SELECT ... FOR UPDATE SKIP LOCKED` on the jobs query.

Today's pattern (`auto_assign_dispatcher` around `main.py:2269`):

```python
async with _dispatch_lock:
    candidates = await postgres_db.list_dispatchable_jobs()
    for job in candidates:
        ...
```

New pattern:

```sql
-- inside a single transaction per dispatch attempt:
SELECT id, ... FROM jobs
WHERE status IN ('created', 'paused')
  AND assigned_agent_id IS NULL
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
-- ... match an agent, update assigned_agent_id ...
COMMIT;
```

`SKIP LOCKED` is the canonical Postgres pattern for distributed work queues (used by `graphile-worker`, `pgmq`, Sidekiq's Postgres adapter). Two replicas running the dispatcher will each grab a different row; neither will see the other's locked row until commit.

Same pattern for `_pause_pending_job_ids`: replace with a `jobs.pause_requested_at` column or a dedicated `pause_requests` table; the pause initiator marks the row, anyone reading dispatch candidates respects the marker.

`_thread_turn_locks` becomes a `threads.in_flight_turn_id` column (nullable, set/cleared atomically with `UPDATE ... WHERE in_flight_turn_id IS NULL OR in_flight_turn_id = $1 RETURNING ...`). Multi-tab POST races land cleanly: one replica's UPDATE returns a row, the other's returns nothing → 409.

`_project_heal_locks` becomes a `projects.heal_in_progress_until` timestamp column with a TTL; nobody else attempts heal until the timestamp passes. Idempotent in practice but cleaner under load.

### Layer 3: Cross-replica fan-out for SSE / WS / sudo

The instance-local queues (`notification_feed._user_queues`, `sudo_gate._sse_queues`, `sudo_gate._pending_msgs`) all need to deliver events emitted on any replica to subscribers on any replica.

**Primary mechanism: Postgres `LISTEN/NOTIFY` for low-volume channels.** Each instance LISTENs on `notifications:<user_id>`, `sudo:<thread_id>`, etc. When any replica needs to fan out, it NOTIFIES. Postgres delivers to all listening sessions.

Important constraint, learned from [[headless_persistent_sessions]] decision log: `LISTEN/NOTIFY` does not scale to high-volume streams (Recall.ai documented the commit-serializing lock). It's fine for the channels here — at most a few hundred sudo prompts per day, a few thousand notification events per user per day. The hot path is `thread_events`, and that one already uses in-pod pub/sub via the agent pod (not the orchestrator), so it's not affected.

**Stream subscribers:**

- `notification_feed` — replace in-process queue with: write the event to a `user_events` table, then `NOTIFY notifications:<user_id>`. SSE handler reads from the table on connect, then LISTENs for new ids.
- `sudo_gate._sse_queues` — same pattern.
- `sudo_gate._pending_msgs` — the NATS reply problem. Resolution: when the agent inserts an open sudo request, store the NATS reply subject on the row (`sudo_approval_requests.nats_reply_subject`). When the request is resolved, *any* replica can publish on that subject, no need for `_pending_msgs`.
- Persistent-session WS / SSE — already structured for this via [[headless_persistent_sessions]] Phase 2 (`thread_events` in Postgres; agent pod is source of truth). The orchestrator-side WS proxy stays a thin forwarder; no in-process state to coordinate. Action item: confirm SSE handler reads `thread_events` from DB rather than holding an in-process subscriber list (Phase 2 design says yes).

### Layer 4: NATS queue groups

Each subscription that today is "broadcast to every consumer" becomes a queue-group subscription so exactly one orchestrator instance receives each message.

```python
# Before:
await nc.subscribe("vm.lifecycle.status", cb=handle_status)
# After:
await nc.subscribe("vm.lifecycle.status", queue="orchestrator", cb=handle_status)
```

The agent.vm.* subjects similarly join the `orchestrator` queue group. Messages that need to fan out to all replicas (rare — none identified today) explicitly skip the queue group.

`_thread_vm_ids` set goes away — it was a routing optimization; now the handler looks up routing in Postgres on every message. ~5ms latency penalty per message, acceptable.

## Coordination primitives summary

A small, opinionated set:

| Need | Mechanism | Notes |
|---|---|---|
| Singleton background loop | Postgres advisory lock (session-scoped) | Reuses migration infra; 30s failover window. |
| Distributed job dispatch | `SELECT ... FOR UPDATE SKIP LOCKED` | Industry-standard for Postgres work queues. |
| Per-turn / per-project serialization | DB column with conditional UPDATE | No lock service needed. |
| Cross-replica fan-out (low volume) | Postgres `LISTEN/NOTIFY` | Don't use for `thread_events` (high volume). |
| Cross-replica fan-out (high volume) | Already done via DB write + agent-pod pub/sub | [[headless_persistent_sessions]] Phase 2. |
| One-message-one-consumer (NATS) | Queue groups | One-line change per subscription. |

Notice what's not on this list: Redis, etcd, Zookeeper, Temporal. We are Postgres-and-NATS-only.

## What stays untouched

- **The 180 REST endpoints.** All stateless requests against the DB. They work today behind a load balancer; nothing to change.
- **Migrations.** Already concurrency-safe (`migrate.py:157`).
- **Authentication.** Keycloak OIDC is stateless per request; MCP tokens are DB-backed. No session affinity required.
- **JSONB atomic merges.** `merge_job_context` and similar (`orchestrator/database/postgres.py`) already handle concurrent writers via `jsonb_set() || $1::jsonb` patterns.

## Out of scope

- **Database HA.** This feature assumes Postgres is HA-managed separately (managed Postgres, Patroni, etc.). Multi-orchestrator with single-Postgres still has a single point of failure at the DB layer; that's a separate problem to solve.
- **Sharding by tenant or user.** Premature at our scale. The system is small enough that horizontal scaling per orchestrator pod (Layer 1-4 above) is sufficient for the foreseeable future.
- **Cross-region / multi-cluster active-active.** Single-cluster only. Multi-region would require careful thought about Postgres write coordination across regions and is well past current need.
- **Per-replica observability isolation.** All replicas log to the same stream; identifying which replica handled a request is via the pod name in log lines. Sufficient.
- **Worker (agent) HA.** Agents are already horizontally scaled (the auto-assign dispatcher is the load balancer for them). This feature is orchestrator-only.
- **MCP server HA.** Bolted into the same process as the orchestrator (`orchestrator/mcp/`). If the orchestrator is HA, the MCP server is HA. No separate work.
- **WebSocket sticky-session routing.** The headless-persistent-sessions design eliminates the need for sticky sessions on the orchestrator side. We do not add a sticky-session ingress as part of this feature.

## Open questions

1. **Leader handoff during loop work.** If `auto_assign_dispatcher` is holding the lock and starts a dispatch cycle, and the pod gets SIGTERM mid-cycle, the lock releases on connection close — but the in-flight dispatch might leave partial state. The DB transaction around `SKIP LOCKED` makes this safe for dispatch specifically; verify the same for the other loops, or add explicit checkpoint logic.
2. **Lease duration for stale-agent detection.** Today the loop runs every 60s; with 30s leader-election failover, a worst-case detection delay grows from 60s to 90s. Probably fine; worth confirming against SLO.
3. **`LISTEN/NOTIFY` payload size limits.** Postgres caps NOTIFY payloads at 8000 bytes. We use them for IDs only, so this is fine, but document the constraint so future work doesn't grow the payload.
4. **Probe behavior during DB outage.** Liveness probe today returns 200 if the HTTP handler runs at all. Should it require a DB ping? Track 2 makes this more sensitive: a replica that can't reach Postgres can't acquire leadership, so it should fail readiness so the load balancer routes around it.
5. **Failover latency during deploy.** Rolling deploy with `replicas: 2` and `maxUnavailable: 0` gives zero-downtime in steady state. But during the deploy, the new replica starts up, tries to acquire leader locks held by the old replica, waits 30s, and finally takes over after the old replica drains. Acceptable; document the expected behavior.
6. **Should the dispatcher have a tighter loop than 30s during high load?** With DB-level dispatch (Layer 2), a NOTIFY-on-job-insert can wake the dispatcher immediately. Worth a follow-up; doesn't block initial Track 2.
7. **Active-active during a Track 2 partial roll-out.** If Layer 1 ships but Layer 2 doesn't, can we run `replicas: 2`? No — `_dispatch_lock` is still per-process. The leader-election wrap makes the dispatcher singleton, so functionally we can: only one replica's dispatcher runs at a time even with `replicas: 2`. Worth being explicit that Layer 1 alone is the unlock for `replicas: N`, with Layer 2-4 as scaling/correctness improvements.

## Implementation phases

Each phase is independently shippable. They're ordered so each one improves operational posture even if the next never lands.

### Phase 0 — Track 1: Active-passive failover hardening

- [ ] Tighten readiness probe + add `preStop` hook + `terminationGracePeriodSeconds` in `deployment/legacy/20-orchestrator.yaml`.
- [ ] Add `PodDisruptionBudget`. Document expected failover latency.
- [ ] Move module-level singletons (`_knowledge_graph_db`, the various dicts) behind `lifespan` startup so SIGTERM closes them cleanly.
- [ ] Chaos test: delete the pod under load; measure user-visible downtime.
- [ ] Document the failover behavior in `docs/operations/orchestrator_failover.md`.

### Phase 1 — Track 2 Layer 1: Leader election

- [ ] `orchestrator/services/leader_election.py` with `with_leader_lock(name, loop_id, run, shutdown_event)`.
- [ ] Lock-ID registry in `orchestrator/constants.py` (or similar) — never collide with `MIGRATION_LOCK_ID`.
- [ ] Wrap every loop registered in `lifespan` at `main.py:3158-3222`.
- [ ] Log "leader changed" events at INFO level for observability.
- [ ] Tests: spin up two test orchestrators against the same DB; verify exactly one runs each loop; kill the leader; verify the follower takes over within 30s.
- [ ] **Unlock:** `replicas: 2` becomes safe at this point (background-loop correctness is preserved; in-process state divergence still exists but is per-process state that doesn't need to be coordinated).

### Phase 2 — Track 2 Layer 2: DB-level coordination

- [ ] Replace `_dispatch_lock` + in-memory job-candidate scan with `SELECT ... FOR UPDATE SKIP LOCKED`.
- [ ] Migration: add `jobs.pause_requested_at`, `threads.in_flight_turn_id`, `projects.heal_in_progress_until`.
- [ ] Migration: add `sudo_approval_requests.nats_reply_subject` (Layer 3 needs this).
- [ ] Remove `_pause_pending_job_ids`, `_thread_turn_locks`, `_thread_turn_inflight`, `_project_heal_locks`.
- [ ] Tests: two replicas racing on the same job → exactly one assignment; two replicas racing on the same turn → one 200, one 409.

### Phase 3 — Track 2 Layer 3: Cross-replica fan-out

- [ ] `notification_feed` reads + LISTENs against `user_events` instead of in-process queue.
- [ ] `sudo_gate._sse_queues` → DB-backed with NOTIFY.
- [ ] `sudo_gate._pending_msgs` → NATS reply subject stored on `sudo_approval_requests`; resolution publishes from any replica.
- [ ] Confirm SSE handler in [[headless_persistent_sessions]] Phase 2 reads `thread_events` from DB (no in-process subscriber list).
- [ ] Tests: emit a notification on replica A; verify SSE client on replica B receives it. Open sudo on replica A; resolve via REST hitting replica B; verify the agent (NATS-bound) gets the response.

### Phase 4 — Track 2 Layer 4: NATS queue groups

- [ ] Audit every `nc.subscribe(...)` in `nats_bridge.py`; add `queue="orchestrator"` where one-of-N delivery is desired.
- [ ] Remove `_thread_vm_ids`; replace with Postgres lookup on each message.
- [ ] Tests: two replicas connected to NATS; publish a status message; verify exactly one replica processes it.

### Phase 5 — Operational polish

- [ ] Per-replica liveness/readiness that includes DB ping (Open Question #4).
- [ ] Tighten dispatcher loop with NOTIFY-on-insert wake-up (Open Question #6).
- [ ] Bump `replicas: 2` in production. Watch for a deploy cycle; declare done.

## ADR: alternatives considered, not adopted

- **Temporal for orchestration.** Battle-tested durable execution. Same argument as [[headless_persistent_sessions]]'s ADR: requires a separate cluster + worker SDK, multi-week migration. Postgres advisory locks + `SKIP LOCKED` cover the failure modes Temporal would address (loop crash mid-cycle, partial state). Worth revisiting only if we outgrow Postgres for coordination.
- **Kubernetes Lease API.** Cleaner conceptually (k8s-native, no Postgres dependency). Rejected because (a) we already have the Postgres dependency for everything else, so adding leases is net-positive complexity not net-negative; (b) advisory locks have lower failover latency (30s vs. lease-renewal interval, typically 15-30s anyway); (c) testing leases requires k8s integration tests, while advisory locks test cleanly with a Postgres testcontainer.
- **Redis Sentinel / Cluster for fan-out.** Common pattern in industry. Rejected because we don't run Redis and Postgres `LISTEN/NOTIFY` is sufficient at our volume. If the orchestrator's notification load ever exceeds what `LISTEN/NOTIFY` can serve (the Recall.ai write-up estimates degradation at thousands of NOTIFY/sec under writer contention), we add Redis as a follow-up — not before.
- **Sticky-session ingress for WebSockets.** A common quick-fix for stateful WS protocols. Unnecessary once the source-of-truth for stream events is `thread_events` in Postgres ([[headless_persistent_sessions]] Phase 2); the orchestrator-side WS proxy is then a thin DB reader, and any replica can serve any subscriber.
- **Sharding by user / tenant.** Premature. The cleanest scaling path for our workload is "more identical replicas in front of one DB" until that ceiling actually shows up in metrics.

## Related code

- `orchestrator/main.py:388` — `_dispatch_lock`.
- `orchestrator/main.py:391` — `_pause_pending_job_ids`.
- `orchestrator/main.py:394-465` — `stale_agent_detector`.
- `orchestrator/main.py:468-495` — `agent_pool_reconciler`.
- `orchestrator/main.py:497-572` — `lifecycle_reconciler_loop`.
- `orchestrator/main.py:574-604` — `workspace_idle_sweeper`.
- `orchestrator/main.py:606-679` — `snapshot_gc_sweeper`.
- `orchestrator/main.py:681-...` — `imap_poll_loop`.
- `orchestrator/main.py:2269-...` — `auto_assign_dispatcher`.
- `orchestrator/main.py:2973-3245` — `lifespan`; every `asyncio.create_task` registration lives here.
- `orchestrator/main.py:10693-10694` — `_thread_turn_locks`, `_thread_turn_inflight`.
- `orchestrator/main.py:11370` — `persistent_ws_proxy`.
- `orchestrator/main.py:14623` — `_project_heal_locks`.
- `orchestrator/main.py:15567-15590` — `_knowledge_graph_db` cache.
- `orchestrator/services/notification_feed.py:23` — `_user_queues`.
- `orchestrator/services/sudo_gate.py:35,37` — `_sse_queues`, `_pending_msgs`.
- `orchestrator/services/nats_bridge.py:68` — `_thread_vm_ids`.
- `orchestrator/database/migrate.py:154-160` — existing advisory-lock pattern (template for leader election).
- `deployment/legacy/20-orchestrator.yaml:9,543-554` — current `replicas: 1` and probe config.

## Decision log

- **2026-05-12:** Two-track plan: ship active-passive hardening (Track 1) first, then incremental active-active layers (Track 2). Track 1 captures the operational benefit at minimal engineering cost; Track 2 unblocks horizontal scale.
- **2026-05-12:** Postgres advisory locks chosen for leader election over Kubernetes Leases. Reuses migration infra; tests cleanly with a Postgres testcontainer; lower-latency failover.
- **2026-05-12:** `SELECT ... FOR UPDATE SKIP LOCKED` chosen for dispatch over a queue service. Postgres-native; battle-tested in the Postgres-work-queue ecosystem.
- **2026-05-12:** Postgres `LISTEN/NOTIFY` chosen for low-volume cross-replica fan-out (notifications, sudo). The high-volume path (`thread_events`) is already DB-write + agent-pod pub/sub per [[headless_persistent_sessions]] Phase 2 — no Redis needed.
- **2026-05-12:** NATS queue groups chosen for one-message-one-consumer delivery. One-line change per subscription.
- **2026-05-12:** No new infra (no Redis, etcd, Zookeeper, Temporal). Postgres + NATS only.
- **2026-05-12:** Database HA is out of scope; assumed handled by the underlying Postgres deployment.
- **2026-05-12:** Sticky-session ingress for WebSockets is *not* required, because the stream source-of-truth moved to `thread_events` with the headless-sessions work.

## Sources

- Postgres advisory locks: [PG docs](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS), used here in the migration system (`orchestrator/database/migrate.py:157`).
- `SELECT ... FOR UPDATE SKIP LOCKED` as the canonical distributed-queue pattern: [PG docs](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE), used by [graphile-worker](https://github.com/graphile/worker), [pgmq](https://github.com/tembo-io/pgmq), and Sidekiq's [Postgres adapter](https://github.com/sidekiq/sidekiq).
- Postgres `LISTEN/NOTIFY` scaling constraint: [Recall.ai write-up](https://www.recall.ai/blog/postgres-listen-notify-does-not-scale) (referenced in [[headless_persistent_sessions]]).
- NATS queue groups: [NATS docs — queue subscribers](https://docs.nats.io/nats-concepts/core-nats/queue).
- Kubernetes `PodDisruptionBudget`: [k8s docs](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/).
- Kubernetes Lease API (alternative considered, not chosen): [k8s docs](https://kubernetes.io/docs/concepts/architecture/leases/), used by [cert-manager](https://github.com/cert-manager/cert-manager) and similar controllers.
