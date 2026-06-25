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
  - "[[high_availability_setup]]"
  - "[[headless_persistent_sessions]]"
  - "[[direct_session_websockets]]"
  - "[[job_auto_assign]]"
  - "[[sudo_permissions]]"
---

# Orchestrator HA & Scaling

> One orchestrator pod is a single point of failure and a single point of throughput. Today the system tolerates the failure mode — pause + re-dispatch — but it can't survive a multi-minute outage gracefully and can't scale past one CPU. This feature plans the move from `replicas: 1` to `replicas: N` in two phases: fast failover first, true horizontal scale-out second.

**Status:** Partially de-risked. Track 1 (active-passive hardening) is **unstarted**. Track 2's *coordination patterns are mostly already shipped* in narrower domains, but the **leader-election primitive that actually unblocks `replicas: 2` is not built**. See the reality-check matrix below.
**Filed:** 2026-05-12
**Refreshed:** 2026-06-24 — reconciled against current code (`main.py` grew ~15.5k → ~23.7k lines; deployment moved to Helm/Fleet; WS proxy removed; several coordination primitives shipped). All line references in this revision are current as of the refresh.

## Refresh 2026-06-24 — reality check

The original draft said "no implementation yet." That is no longer true, and the gap is lopsided. Three of the four Track-2 coordination patterns now exist as working, shipped code — just applied to narrower problems than full multi-replica. Meanwhile the cheap Track-1 win is still entirely undone, and the one new primitive that gates `replicas: 2` (leader election) is greenfield.

| Work item | Original status | **Current status (2026-06-24)** | Evidence |
|---|---|---|---|
| **Track 1** active-passive hardening (probes / `preStop` / grace / PDB) | not started | **SHIPPED (chart); chaos test operator-pending** | `preStop` drain + `terminationGracePeriodSeconds` + `startupProbe` + tuned probes + orchestrator PDB landed in `helm/` (M0). Behavioral chaos test is operator-run on dev — see `docs/operations/orchestrator_failover.md`. |
| **Layer 1** leader election for singleton loops | not started | **GREENFIELD — the one true unlock** | No `with_leader_lock`, no session-scoped advisory lock, no `leader_election.py`. All existing advisory locks are *xact*-scoped. **Design refined by research → `docs/researches/orchestrator_leader_election.md`** (single leader lock; keepalive tuning; dispatcher CAS folded into M1). |
| **Layer 2** DB-level dispatch (`SKIP LOCKED`) | not started | **PATTERN PROVEN, dispatcher not ported** | `cron_dispatcher` is a complete, documented multi-replica-safe `SKIP LOCKED` queue. Job dispatch still uses the in-process `_dispatch_lock`. `thread_advisory_lock` already covers per-thread serialization. |
| **Layer 3** cross-replica fan-out (`LISTEN/NOTIFY`) | not started | **TRANSPORT SHIPPED + WS problem evaporated** | `notify_channel()` helper + a reconnecting `LISTEN` loop ship for cloud-config reload. The WS half is moot: `persistent_ws_proxy` is gone (direct-to-agent-pod), stream is `thread_events` SSE. Remaining: DB-back the 2 SSE channels + drop `_pending_msgs`. |
| **Layer 4** NATS queue groups | not started | **GREENFIELD but trivial** | No `queue=` on any subscription. Subjects are now `orchestratorId`-scoped (separates installs, not replicas). |
| **Data tier** prerequisite (Postgres/NATS HA) | out of scope here | **NOT STARTED** (see [[high_availability_setup]]) | NATS single-replica with clustering *explicitly disabled*; no CloudNativePG. |

**The practical read:** for the open-source "scales from a mini-PC to a thousand-agent cluster" claim, the critical path is (1) ship Track 1 so a single replica survives its own eviction (~1 week, no new primitives), then (2) build the leader-election helper and wrap the loops — that alone makes `replicas: 2` *correct*, reusing the proven patterns for the rest. The scary-sounding parts (WS coordination, dispatch races) are largely already solved.

## Roadmap

A milestone view sequenced for the open-source release, reconciling this doc with the data-tier work in [[high_availability_setup]]. Milestones are ordered by **dependency, not calendar** — no target dates are set; sequence them against whatever the release timeline turns out to be. Each is independently shippable and leaves the system strictly better. The granular task breakdown is in the **Implementation phases** section below; the data-tier milestones live in the umbrella doc. Effort is rough one-person engineering-time, mostly-testing included.

### What gates what

```
M0 Active-passive hardening ─────────────┐
   (Track 1, stays replicas:1)           │
                                          ├──> M2 Active-active scale-out
M1 Leader election ──> replicas:2 SAFE ───┘    (Layers 2-4)
   (Layer 1)          = orchestrator no
                        longer a hard SPOF
                              │
M3 Data-tier HA ──────────────┴──> meaningful END-TO-END HA
   (NATS cluster, Postgres)          + active-active at scale
   [umbrella doc]
```

The non-obvious relationship: **M1 alone makes the orchestrator HA for failover** — run `replicas: 2`, lose one, and the load balancer plus ~30s loop-failover absorb it — *even over a single Postgres*. But the **stack** isn't HA until M3, because an HA orchestrator in front of a single-Postgres still goes dark when Postgres does. M0 and M3 can run in parallel with the M1 → M2 line.

> This refines the umbrella doc's "suggested execution order," which lumped all multi-replica work into one post-data-tier "Track 2." Separating M1 (failover HA, no data-tier dependency) from M2 (scale-out, wants data-tier HA) lets the orchestrator stop being a SPOF *before* the heavier Postgres migration lands.

### Milestones

| # | Milestone | Unlocks | Depends on | Effort | Detail |
|---|---|---|---|---|---|
| **M0** | Active-passive hardening (Track 1) | Bounded, tested ~15-30s failover on eviction; backs the "survives node drain / image roll / OOM" claim. Stays `replicas: 1`. | drain discipline (umbrella doc — operational, zero-cost) | ~1 week (mostly chaos-testing) | Phase 0 |
| **M1** | Leader election (Layer 1) | **`replicas: 2` becomes correctness-safe → orchestrator is no longer a hard SPOF.** Zero REST/SSE blackout on eviction (peers stay up); singleton loops fail over in ~30s. Works over single Postgres. | none (ship after M0 for the cheap win first) | ~3-5 days + tests | Phase 1 |
| **M2** | Active-active scale-out (Layers 2-4) | Horizontal CPU/connection scale per orchestrator; removes residual cross-replica UX divergence + double-work. Mostly porting patterns already proven in-repo (cron `SKIP LOCKED`, `notify_channel`). | M1 | ~1-1.5 weeks | Phases 2-4 |
| **M3** | Data-tier HA | Removes the Postgres/NATS SPOFs → meaningful end-to-end HA + headroom for active-active at scale. | independent of M0-M2 (run in parallel); **required before the "thousand-agent cluster" claim is honest** | NATS ~2-3 days; Postgres→CloudNativePG ~1-2 weeks incl. migration | [[high_availability_setup]] P1-P2 |
| **M4** | Operational polish (Phase 5) | DB-aware probes, NOTIFY-on-insert dispatch wake; flip `orchestrator.replicas: 2`, watch a deploy cycle, declare done. | M1-M3 | ~2-3 days | Phase 5 |

### The open-source release bar

- **Minimum honest "HA orchestrator" claim = M0 + M1 + the NATS-cluster slice of M3.** A single replica failure is tolerated, `replicas: 2` is safe, and the coordination plane isn't a SPOF. Postgres HA can be a *documented bring-your-own posture* rather than shipped code: the chart already supports pointing at an external/managed Postgres (`databases.*.externalHost`), so "use managed/HA Postgres in production" is a legitimate OSS stance consistent with this doc's "Database HA out of scope" decision — and the cheapest path to making the claim true.
- **Full "mini-PC → thousand-agent cluster" claim = + M2 + the CloudNativePG slice of M3.** Horizontal scale per orchestrator *and* batteries-included data-tier HA for self-hosters who don't bring their own managed Postgres.
- **Hard blocker today:** the chart ships `replicas: 1` and that is the *only* safe value — `replicas: 2` has live correctness bugs (dispatch double-assign, IMAP double-poll) until M1. README/marketing must not imply a multi-replica orchestrator before M1 lands.

## Motivation

`helm/values.yaml:95` sets `orchestrator.replicas: 1` (templated at `helm/templates/orchestrator/deployment.yaml:12`; the live deploy is the Helm chart via Fleet GitOps — `deployment/fleet.yaml`). There is exactly one process making dispatch decisions, listening on NATS, sweeping stuck jobs, expiring sudo approvals, polling IMAP, and reconciling the agent pool. When that pod gets evicted or OOMs:

- Inbound REST traffic dies until Kubernetes re-rolls (15-60s on a healthy cluster, multiple minutes on a degraded one).
- Any NATS reply landing during the gap is dropped — the durable consumer is recoverable but the in-process `_pending_msgs` map is not.
- Background sweepers don't run; orphaned-job recovery stalls.
- Persistent-session SSE streams drop, but the agent pod keeps running and keeps writing `thread_events`; a reconnecting cockpit replays from `Last-Event-ID` after the bounce (this part already degrades gracefully — see "What already works").

The system *does* survive — agents heartbeat-time-out cleanly, jobs auto-pause, persistent sessions reattach. The user-visible damage is a multi-minute UI blackout, not data loss. But the failure mode is unnecessarily disruptive for what's structurally a stateless web service, and the single-process ceiling caps throughput when we want to run thousands of concurrent agents.

Two motivations stacked together:

1. **High availability.** Survive a pod eviction with sub-second user-visible impact. Everyday case (node drain, image roll, OOM).
2. **Horizontal scaling.** Run multiple orchestrator pods to spread CPU and connection load. Future case (more users, more persistent sessions).

Both want `replicas: N`. The work that gets us to `replicas: 2` is the same work that gets us to `replicas: 8`.

## What blocks `replicas: 2` today

The DB layer is fine — migrations coordinate via `pg_advisory_xact_lock` (`orchestrator/database/migrate.py:157`), Postgres handles concurrent writers, and JSONB merges use atomic SQL (`merge_job_context`, `postgres.py:1402`). The blockers are *runtime* singletons in the orchestrator process. The refresh re-verified each and split them into "still a correctness bug," "UX divergence," and "already safe."

### Correctness blockers (must fix before `replicas: 2`)

| What | Where (current) | What breaks with 2 replicas |
|---|---|---|
| `_dispatch_lock` (asyncio.Lock) | `main.py:559`, acquired in `_try_dispatch_pending_jobs` `main.py:3733` | Each replica has its own lock; the dispatch query `get_dispatchable_jobs` (`postgres.py:2449`) has **no `FOR UPDATE SKIP LOCKED`**, so both replicas can assign the same `created` job to two agents. **The Layer-2 fix is unbuilt** (cron already demonstrates the pattern — see below). |
| `imap_poll_loop` | `main.py:981` → `services/imap_poller.py` (`poll_once:118`, `_process_email:275`) | **Both replicas read the same mailbox.** Dedup (`message_exists_by_email_id`) is checked at the *start* of `_process_email`, and the message is marked seen only *after* the reply handler fires, with no row lock → two replicas both pass dedup and both route the email. Duplicate sudo approvals / duplicate thread replies. |
| `delegation_timeout` handler | loop `main.py:8950`, logic `_check_delegation_timeouts` `main.py:8807` | Bare `SELECT ... WHERE status='waiting'`, no row lock/CAS before resuming the parent. Both replicas re-resume the parent (double-unblock). Child `cancel_job` is harmless; the parent resume is not. (Original draft flagged this "needs investigation" — **confirmed racy**.) |
| `_thread_turn_locks: dict[(thread_id,turn_id), Lock]` | `main.py:15860` (helper `_ensure_thread_turn_lock` `main.py:15864`) | Per-turn input serialization. Multi-tab POSTs to different replicas both win the local lock → double-turn. The code comment at `main.py:15857` still explicitly assumes "Single-instance orchestrator." `thread_advisory_lock` does **not** cover this (it guards provisioning, not per-turn input). |
| `_thread_turn_inflight: dict[thread_id,int]` | `main.py:15861` | Same problem; the "is this turn already running" 409 check is per-process. |
| `_pause_pending_job_ids: set[str]` | `main.py:562` | Pause requests in flight on replica A are invisible to replica B; B can re-preempt a job A is already pausing. (Low severity.) |
| `_over_quota_projects: frozenset[str]` | `main.py:1249`, rebound by the quota poll loop `_quota_poll_tick` `main.py:1336`, read by `is_project_over_quota` `main.py:1252` | **New since the original draft.** Only the replica running the quota poll loop has a fresh set; other replicas read a stale/empty frozenset and dispatch jobs for over-quota projects. Same class as `_thread_vm_ids`. Fixed by Layer 1 (singleton loop) + reading the gate from DB or via NOTIFY. |
| `sudo_gate._pending_msgs: dict[req_id, nats.Msg]` | `services/sudo_gate.py:37` (set `:147`, popped `:186`/`:232`/`:290`) | **Downgraded from the original "agent waits forever."** The NATS reply subject is now persisted on the row (`sudo_approval_requests.nats_reply_subject`). Resolvers try the in-memory `respond()` fast-path first, then fall back to publishing on the persisted subject (`_nats_reply`, `sudo_gate.py:666-684`). On the wrong replica the in-memory lookup misses but the decision **still reaches the daemon** via the fallback publish. So this is a reliability/latency degradation, not a hang. Layer-3 cleanup = drop `_pending_msgs`, always publish on the subject. |

### UX divergence (degrades experience, no data corruption)

| What | Where (current) | What breaks |
|---|---|---|
| `notification_feed._user_queues: dict[user_id, list[Queue]]` | `services/notification_feed.py:23`; SSE endpoint `main.py:7304` | SSE clients on replica B don't receive events emitted on replica A. **New consumer:** `session.lifecycle` startup-progress events now ride this same channel (`services/session_lifecycle.py:38`), so the session-startup card stalls cross-replica too. |
| `sudo_gate._sse_queues: list[Queue]` | `services/sudo_gate.py:35`; SSE endpoint `main.py:7521` | Same problem for the sudo-approval admin stream. |
| `nats_bridge._thread_vm_ids: set[str]` | `services/nats_bridge.py:82` (populated `:246`, read `:437`/`:441`/`:467`/`:562`/`:567`) | Thread-vs-job routing memory; populated only on the replica that called `request_vm_create`. A VM-lifecycle message handled by a different replica mis-routes context to the jobs table instead of threads. |
| NATS subscriptions without queue groups | `services/nats_bridge.py:163-192` (six subjects) | No `queue=` on any subscription → every replica receives every message and runs the handler. Subjects are now `orchestratorId`-scoped (`_subj`, `nats_bridge.py:98`), which separates distinct *installs*, not *replicas* of one install. |
| Admin "reload" caches (`_experts_cache` `main.py:17763`, `_skills_cache` `main.py:18126`, `_settings_matrix_cache` `main.py:17829`) | per-process, busted via `POST .../reload` | A reload endpoint only busts the cache on the replica that served the request; other replicas serve stale catalogs until restart. Fan-out via NOTIFY (Layer 3) or they look broken at 2+. |

### Already safe (no action needed)

- **`persistent_ws_proxy` — REMOVED.** The original draft's biggest WS concern is gone. The cockpit WebSocket now terminates **directly on the agent pod** via a per-session K8s Service+Ingress (`/p/{thread_id}/ws`, URL minted at `routers/sessions.py:390`, built in `services/session_router.py`; see [[direct_session_websockets]]). The orchestrator is no longer in the WS data path, so there is no in-process WS fan-out state to coordinate. The only remaining `@app.websocket` route is the IDE proxy (`main.py:10190`).
- **`_knowledge_graph_db` (Neo4j client cache)** `main.py:22976` — just a duplicated driver per replica. Not a correctness issue.
- **`_project_heal_locks`** `main.py:21941` — two replicas can race `ensure_project_folder` for the same project, but the callsite re-reads `get_project` inside the lock (`main.py:22015`), so same-process doubles are caught; cross-replica is a rare, mostly-idempotent duplicate cloud call. (Track-2 Layer-2 cleans it up with a TTL column; not a `replicas: 2` blocker.)
- **`_threads_suspending: set[str]`** `main.py:3347` — de-dupes concurrent suspend triggers; small cross-replica double-suspend window, low blast radius. Worth one line in the eventual leader-election pass.
- **Per-service caches/locks** — `_pending_actions_cache` (5s TTL, `main.py:7100`), OpenCloud token/role caches, TTS single-flight locks, HTTP client pools. All correctly per-process.

### Background loops — double-fire audit (regenerated)

The lifespan handler is now at `main.py:4866`; task registrations run `main.py:5191-5323` (shutdown awaits `5329-5355`), starting **27** long-lived tasks. The encouraging delta: **new loops were largely built HA-aware.**

**Still double-fire (need Layer 1 leader election):**

| Loop | Where | Harm |
|---|---|---|
| `auto_assign_dispatcher` | `main.py:4069` (core `:3720`) | Double-assigns jobs. **Critical.** |
| `imap_poll_loop` | `main.py:981` / `imap_poller.py` | Duplicate email processing. **Critical.** |
| `delegation_timeout_sweeper` | `main.py:8950` | Double parent-resume. |
| `quiet_hours_digest_loop` | `main.py:933` (300s) | Double-sends digest emails. |
| `stale_agent_detector` | `main.py:565` (60s) | The `UPDATE` is idempotent, but it calls `_trigger_dispatch()` → feeds the unguarded dispatcher. |
| `agent_pool_reconciler` | `main.py:673` (60s) | Over-provisions pods / double-reaps. |
| `lifecycle_reconciler_loop` | `main.py:702` (60s; managers `:5280-5314`) | Double drift-detect / double-drain. |
| `thread_permission_notify_sweeper` | `main.py:16941` (30s) | Tight double-send window for permission emails (smaller IMAP-shaped race). |

**Idempotent / noisy-but-safe** (wrap for log clarity, not correctness): `workspace_idle_sweeper` (`main.py:779`, now reconcile-only), `snapshot_gc_sweeper` (`main.py:906`), `sudo_expiration_sweeper` (`main.py:730`), `thread_events_prune_sweeper` (`main.py:16342`), `quota_poll_loop` (`main.py:1376`, freeze writes idempotent), `litellm_sync_loop` (`litellm_gateway.py:984`), `code_server_settings_sweeper` (`main.py:818`), `ide_session_ttl_sweeper` (`main.py:754`), `attention_sleep_sweeper` (`main.py:17062`, CAS-guarded), `security_events_prune_sweeper` (`main.py:16394`), `cleanup_expired_tokens` (`main.py:5192`), `cleanup_expired_sessions` (`main.py:5195`).

**Already HA-safe by construction** (the proof that the patterns work in-repo):

| Loop | Where | Guard |
|---|---|---|
| `cron_dispatcher_loop` | `services/cron_dispatcher.py:63` (claim `postgres.py:9335`) | `FOR UPDATE SKIP LOCKED`; docstring states "safe to run on multiple orchestrator replicas." |
| `project_loop_sweeper` | `services/project_loop_sweeper.py:40` (claim `postgres.py:9295`) | Conditional-UPDATE CAS (`WHERE ... current_job_id=$2 AND status='running'`). |
| audit `maintenance_loop` | `services/audit_partitions.py:436` | `pg_advisory_xact_lock(MAINT_LOCK_ID)` (`audit_partitions.py:167`). |
| `workspace_metering_loop` | `services/workspace_metering.py:242` | Ledger `ON CONFLICT ... DO NOTHING` dedupe keys. |
| `llm_usage_poll_loop` | `main.py:1414` | Same ledger dedupe key. |

**Not a bug — intentional fan-out:** `main_cloud_listen_task` (`main.py:5321` → `services/cloud/reload.py:50`) is a `LISTEN/NOTIFY` config-reload task that is *meant* to run on every replica. The original draft wrongly listed it as a double-fire risk. It is the reference example of correct multi-replica behavior.

## What already works (Track 1 safety, verified)

These mechanics are why a single orchestrator already survives its own death without data loss — and why Track 1 is "harden + chaos-test" rather than "build."

- **Job auto-pause on agent offline.** `recover_orphaned_jobs` (`postgres.py:2377-2435`) flips `processing` jobs whose agent is offline/missing back to `paused` and clears the assignment; the next dispatch picks them up. Invoked from `stale_agent_detector` (`main.py:649`). (The original draft loosely said "back to `paused`/`created`" — code sets `paused`.)
- **Stale-agent detection.** `mark_stale_agents_offline` (`postgres.py:2350`) on a 60s loop marks agents offline after a **3-minute** heartbeat cutoff (the draft's "three heartbeats" is approximate). This is the mechanism the whole "survives a pod death" story rests on.
- **Persistent session reattach (headless Phase 2 — SHIPPED).** The wire-level stream is written to the `thread_events` table (migration `0004_thread_events.sql`) **by the agent pod** (`src/api/persistent_app.py:2524`), not the orchestrator. The SSE replay endpoint `thread_event_stream` (`main.py:15993`) parses `Last-Event-ID` (`main.py:16017-16031`) and polls `thread_events` by `seq` (`main.py:16118-16147`) — it holds **no in-process subscriber list**. A reconnecting cockpit catches up after a bounce. (This checks off the original Layer-3 action item "confirm the SSE handler reads from DB.") One residual: the agent's `thread_events` write is best-effort fire-and-forget (`persistent_app.py:2552`), so a lost write means one missing `seq` in replay.
- **Migration safety.** `pg_advisory_xact_lock` (`migrate.py:157`, constant `LOCK_ID`) means concurrent orchestrator startups can't double-apply migrations. Rolling `replicas: 2` is already safe at that layer.
- **Atomic JSONB merges.** `merge_job_context` (`postgres.py:1424`) does `context = COALESCE(context,'{}'::jsonb) || $1::jsonb` in a single UPDATE; nested-key variants use `jsonb_set(...)`. Concurrent-writer-safe, as the draft claimed.

## What already ships toward Track 2 (the patterns are proven)

The single most important correction to the original draft: **most of the coordination machinery already exists**, applied to narrower problems. A `replicas: N` push is mostly generalizing proven code, not inventing it.

| Primitive | Where | Lock scope | Maps to | Status |
|---|---|---|---|---|
| `thread_advisory_lock(thread_id)` | `postgres.py:2568-2586` (`pg_advisory_xact_lock`, blake2b-8 key); call sites `sessions.py:177`, `main.py:13080`, `main.py:15621`, `provision_or_assign.py:72` | xact | Layer 2 (per-thread serialization) | **Shipped.** Serializes provisioning/binding so `/prepare`, `/resume`, agent `/register` can't double-provision. Already cross-replica-correct. |
| `FOR UPDATE SKIP LOCKED` work-queue | `cron_dispatcher.py` + `postgres.py:9335` (`fetch_next_due_cron_automation`) | xact row lock | Layer 2 (DB dispatch) | **Shipped & documented multi-replica-safe.** A complete working example of the dispatch pattern, applied to cron. Porting `auto_assign_dispatcher` is largely copying this shape. |
| `LISTEN/NOTIFY` fan-out + `notify_channel()` helper | `services/cloud/reload.py` (loop `:50`, `fire_reload:163`); generic helper `postgres.py:8839-8856`; channel const `services/cloud/__init__.py:48` | n/a (pub/sub) | Layer 3 (cross-replica fan-out) | **Transport shipped.** Reconnecting out-of-pool LISTEN loop + a reusable, validated `notify_channel(channel, payload)`. Near-verbatim template for `notifications:<user_id>` / `sudo:<thread_id>`. |
| Maintenance advisory locks | `audit_partitions.py:77` (`MAINT_LOCK_ID`), `migrate.py:21` (`LOCK_ID`) | xact | Layer 1 (singleton guard, partial) | **Shipped** as short DDL critical sections; both deliberately distinct (informal anti-collision). Not the long-lived *session*-scoped lease Layer 1 needs. |

**The one genuinely missing primitive is Layer 1 leader election.** There is no `with_leader_lock`, no session-scoped `pg_advisory_lock`/`pg_try_advisory_lock`, no `leader_election.py`, no k8s Lease anywhere in `orchestrator/`. Every existing advisory lock is **xact-scoped** (released at COMMIT); leader election needs a **session-scoped** lock held for the loop's lifetime — a new pattern. It would build on the `migrate.py` lock template, the dedicated out-of-pool connection idiom already proven in `reload.py:82`, and the `shutdown_event` + `asyncio.wait_for` loop idiom every background loop already uses.

> **Doc correctness fix:** earlier revisions referenced a constant `MIGRATION_LOCK_ID`. That identifier does not exist — the real constant is `LOCK_ID` (`migrate.py:21`).

## Decision: active-passive first, active-active as the roadmap

Unchanged, and the refresh reinforces it.

**Active-passive (Track 1).** Single replica running; K8s replaces it fast on crash. The existing recovery mechanics (heartbeat-driven offline detection, orphan auto-pause, SSE reconnect from `thread_events`) absorb the gap. **Trade-off:** brief blackout during failover (~15-30s with tightened probes), throughput capped at one CPU.

**Active-active (Track 2).** Multiple replicas, all serving traffic. Background loops run on one replica via leader election. Dispatch uses DB row locks. Cross-replica fan-out via `LISTEN/NOTIFY`. NATS queue groups for one-message-one-consumer. **Trade-off:** real horizontal scale + zero-blackout failover; but now mostly *generalizing shipped patterns* rather than the multi-week unknown the original draft assumed.

**Chosen: ship Track 1, build Track 2 incrementally.** Track 1 is probe tuning, a `PodDisruptionBudget`, a `preStop` hook, and chaos-testing the existing recovery paths — still genuinely undone and the cheapest reliability win for the OSS release. Track 2 lands as independent PRs where each improves correctness even before full active-active.

## Track 1 — Active-passive (P0) — NOT STARTED

Single replica with fast, well-understood failover. Most of the work is verifying existing recovery paths under chaos. As of the refresh, **none of the deployment hardening exists.**

| Change | Where (Helm — the live path) | Effort |
|---|---|---|
| Tighten readiness probe to drain in-flight requests before termination. | `helm/templates/orchestrator/deployment.yaml:1042-1047` (readiness: `/api/health:8085`, `initialDelaySeconds: 10`, `periodSeconds: 5`, no `timeout`/`failureThreshold`). | 1h |
| Add `terminationGracePeriodSeconds` + a `preStop` hook (e.g. 30s sleep, 60s grace) so the LB deregisters the endpoint before SIGTERM. **None exists today** (no `lifecycle:` block at all). | `helm/templates/orchestrator/deployment.yaml` | 1h |
| Add a `PodDisruptionBudget` for the orchestrator. **None exists** — mirror the existing `helm/templates/agent/pdb.yaml` pattern into a new `helm/templates/orchestrator/pdb.yaml`. `minAvailable: 0` while `replicas: 1`; flip to `1` for Track 2. | new `helm/templates/orchestrator/pdb.yaml` | 30m |
| Verify the liveness probe doesn't kill the pod during a long startup (migrations on a populated DB). Current: `initialDelaySeconds: 30`, `periodSeconds: 10` (`deployment.yaml:1036-1041`). Note init containers (`deployment.yaml:26-56`: `wait-for-postgres`/`pgvector`/`auditdb`/`mongodb`/`gitea`/`keycloak`) already gate startup on the data tier. | same | 30m + test |
| Move module-level singletons (`_knowledge_graph_db`, the cloud-service HTTP clients, `_ide_http_client`) behind `lifespan` startup so SIGTERM closes them cleanly. | `main.py:22976` and similar | 2h |
| Chaos-test failover end-to-end: delete the orchestrator pod while (a) a job is dispatching, (b) a sudo prompt is open, (c) a persistent session is mid-turn. Measure observable downtime. | manual / scripted | 1d |

Track 1 should land in under a week, mostly testing.

### What Track 1 explicitly does *not* fix

- Per-pod connection load (one replica still holds every SSE stream and REST connection).
- IMAP double-poll / dispatch double-assign if anyone sets `replicas: 2` — Track 1 keeps `replicas: 1` so it's not a production problem, but it's a foot-gun. **Do not bump replicas before Layer 1.**
- The 15-30s failover blackout (acceptable for everyday operation, not for zero-downtime deploys).

## Track 2 — Active-active (P1, multi-phase)

Order unchanged: leader election → DB coordination → fan-out → NATS. The refresh annotates each layer with how much is already built.

### Layer 1: Leader election for singleton loops — GREENFIELD (the unlock)

The cheapest, most-impactful change, and **the single primitive that makes `replicas: N` safe** (Open Question #7). Elect one leader replica that runs the singleton loops; the others stand by.

> **Design refined 2026-06-25 by `docs/researches/orchestrator_leader_election.md`** (5-agent web + codebase research). Verdict unchanged (advisory lock), but four corrections are baked in below: a **single** leadership lock (not per-loop), a **dedicated non-pooled** connection, **mandatory keepalive tuning**, and "**the lock is for efficiency, not correctness**."

**Primitive: a single session-scoped Postgres advisory lock per replica**, held on a **dedicated, long-lived connection** (the `reload.py` out-of-pool + reconnect-with-backoff idiom). Whoever holds `LEADER_ID` is the leader and runs *all* the singleton loops; others retry every ~5-10s (with random startup jitter). On the leader's death its Postgres session closes and the lock releases.

> **Not per-loop.** An earlier sketch wrapped each loop in its own lock acquired from the shared pool — but a session lock pins a connection for life, and ~10 loops would pin ~10 of the pool's 10 connections and starve the leader's request traffic. One leadership lock = one pinned connection. (Per-loop locks, to spread loops across replicas, is an M2 optimization needing its own dedicated lock-pool.)

```python
async def run_as_leader(run_all_loops, shutdown_event: asyncio.Event) -> None:
    while not shutdown_event.is_set():
        conn = await acquire_dedicated_conn()                 # NOT a txn-pooled conn; cf. reload.py:71-150
        try:
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_ID)
            if got:
                logger.info("acquired leadership")
                try:
                    await run_all_loops(shutdown_event)       # runs until shutdown OR conn dies
                finally:
                    await conn.execute("SELECT pg_advisory_unlock($1)", LEADER_ID)
            else:
                await _sleep_or_shutdown(shutdown_event, 10.0)
        except Exception:                                     # conn dropped → step down, reconnect, re-contend
            logger.warning("leader connection lost; stepping down")
        finally:
            await release_dedicated_conn(conn)
```

**Preconditions & properties (research-validated):**
- **No transaction-mode connection pooler in the lock path.** Session advisory locks silently leak behind PgBouncer/pgcat/RDS-Proxy transaction pooling (the unlock lands on a different backend). **Verified: SRW connects direct asyncpg → Postgres, no pooler — safe.** Caveat: external-Postgres mode (`databases.postgres.internal: false`) could point at a pooler — add a startup self-check or a loud `values`/doc warning.
- **Failover needs Postgres keepalive tuning — or it's ~2 hours, not ~30s.** A session lock releases only when Postgres reaps the dead backend. Clean pod shutdown (TCP FIN) releases **instantly** (rolling deploys are fine); a hard kill / partition is noticed only via TCP keepalive, whose Linux default is ~2h. **Required:** set server-side `tcp_keepalives_idle=10`, `tcp_keepalives_interval=10`, `tcp_keepalives_count=3` (→ ~40s) + `idle_session_timeout` as a backstop, on the Postgres deployment. (`client_connection_check_interval` does **not** help an idle lock-holder — common trap.)
- **The lock is for *efficiency*, not *correctness* — there is no fencing.** Two leaders can briefly coexist (the ~40s detection window; and a Postgres primary→replica failover wipes all advisory locks instantly, since they're never WAL-logged). Correctness-critical loops must therefore be guarded **at the resource** (idempotency / CAS / `SKIP LOCKED`), not by the lock. Concretely this **pulls the dispatcher's `SKIP LOCKED` + assign-write CAS into M1** (see Layer 2), and the other non-idempotent loops get cheap resource guards (imap unique-index, digest/delegation/notify) so a brief overlap is harmless.
- **Graceful step-down.** On lost leadership or a dead lock-connection, **stop the loops** and release in a `finally` — a cancelled asyncio task does *not* release the lock (GreptimeDB war story).
- **Stable lock IDs.** Add `LEADER_ID` to a registry module alongside the existing packed-ASCII int64 constants (`LOCK_ID` "SRW_MIG", `MAINT_LOCK_ID` "SRW_AUDT"); session-scoped `pg_advisory_lock` is genuinely new (all current uses are xact-scoped).

The single leader runs: `auto_assign_dispatcher`, `imap_poll_loop`, `delegation_timeout_sweeper`, `quiet_hours_digest_loop`, `stale_agent_detector`, `agent_pool_reconciler`, `lifecycle_reconciler_loop`, the `_over_quota_projects` quota poll, `thread_permission_notify_sweeper`. The already-HA-safe loops (`cron_dispatcher`, `project_loop_sweeper`, audit maintenance, metering) and the intentional per-replica `main_cloud_listen_task` **stay outside** leadership — they run everywhere by design.

**Alternatives considered (not chosen):** Kubernetes Lease — rejected, and the research sharpened why: the Python ecosystem is immature (official client does ConfigMap-only election; the Lease-support PR was closed unmerged 2025-11-21; `kopf` uses its own CRDs), it needs extra RBAC + a k8s-API dependency, and "Postgres down ⇒ orchestrator down" makes its one advantage (DB-independence) moot. Redis `SET NX PX` (no Redis), etcd/Consul (extra infra). Postgres-first.

### Layer 2: DB-level job dispatch — the dispatcher fix moves INTO M1; the rest stays M2

> **Research correction (2026-06-25):** the dispatcher's `SKIP LOCKED` + a CAS guard is **not** optional M2 polish — it's the correctness floor for the dual-leader window (leader election has no fencing). The codebase audit found `get_dispatchable_jobs` (`postgres.py:2449`) has no `SKIP LOCKED` **and** the assign-write `UPDATE jobs SET status='processing', assigned_agent_id=$X WHERE id=$job` (`postgres.py:1029`) has **no CAS** — so two transient leaders genuinely send one job to two agents. **Do this in M1.** The column migrations below (`pause_requested_at`, `in_flight_turn_id`, `heal_in_progress_until`) stay M2.

`_dispatch_lock` becomes unnecessary. Replace the candidate scan with `SELECT ... FOR UPDATE SKIP LOCKED` — **exactly what `fetch_next_due_cron_automation` (`postgres.py:9335`) already does for cron** — and add `WHERE assigned_agent_id IS NULL` (CAS) to the assign-write so a row can't be claimed twice. Copy that shape onto `get_dispatchable_jobs` (`postgres.py:2449`):

```sql
SELECT id, ... FROM jobs
WHERE status IN ('created','paused') AND assigned_agent_id IS NULL
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
-- match an agent, set assigned_agent_id, COMMIT
```

- `_pause_pending_job_ids` → a `jobs.pause_requested_at` column (or `pause_requests` table); the initiator marks the row, dispatch respects the marker.
- `_thread_turn_locks`/`_thread_turn_inflight` → either a `threads.in_flight_turn_id` column with a conditional `UPDATE ... WHERE in_flight_turn_id IS NULL RETURNING ...` (one replica gets the row → 200, the other → 409), **or** reuse the already-shipped `thread_advisory_lock` (it currently guards provisioning, not per-turn input — extend it). The advisory-lock route is less new code.
- `_project_heal_locks` → a `projects.heal_in_progress_until` TTL column.

### Layer 3: Cross-replica fan-out — TRANSPORT SHIPPED, WS half moot

The instance-local queues (`notification_feed._user_queues`, `sudo_gate._sse_queues`, `sudo_gate._pending_msgs`) need to deliver events emitted on any replica to subscribers on any replica. **The mechanism already exists** (`notify_channel()` + the `reload.py` LISTEN loop); the work is generalizing it.

- `notification_feed` (carries `new_message`/`reply_delivered`/`session.lifecycle`) → write to a `user_events` table, `NOTIFY notifications:<user_id>`; SSE handler reads the table on connect, then LISTENs. Generalize `run_listen_loop` from the single hard-coded `RELOAD_CHANNEL` to per-subscriber channels.
- `sudo_gate._sse_queues` → same pattern on `sudo:<thread_id>`.
- `sudo_gate._pending_msgs` → **mostly done.** The `nats_reply_subject` column already exists and the fallback publish already works cross-replica. Remaining: delete `_pending_msgs` and always publish on the persisted subject. (Caveat to verify first: confirm the daemon-side sudo client listens on the deterministic published subject, not a NATS auto-inbox.)
- Persistent-session WS/SSE — **already done.** `thread_events` SSE (`main.py:15993`) is stateless on the orchestrator; the WS proxy is gone (direct-to-agent-pod). No work.

`LISTEN/NOTIFY` scaling caveat (from [[headless_persistent_sessions]]): don't use it for high-volume streams. Fine for these channels (a few hundred sudo prompts/day, a few thousand notifications/user/day). The hot path (`thread_events`) is already DB-write + agent-pod, not orchestrator fan-out.

### Layer 4: NATS queue groups — GREENFIELD but trivial

Each broadcast subscription in `nats_bridge.py:163-192` becomes a queue-group subscription so exactly one replica receives each message:

```python
await nc.subscribe(self._subj("vm.lifecycle.status"), queue="orchestrator", cb=handle_status)
```

All six subjects (`vm.lifecycle.status`, `agent.vm.*.register/heartbeat/status`, `sudo.request.>`, `session.events.>`) join the `orchestrator` queue group. `_thread_vm_ids` (`nats_bridge.py:82`) goes away — replace with a Postgres lookup per message (~5ms, acceptable). Note: the existing `orchestratorId` subject scoping is orthogonal — it separates installs; queue groups separate replicas within one install.

## Coordination primitives summary

| Need | Mechanism | Status |
|---|---|---|
| Singleton background loop | Session-scoped Postgres advisory lock | **Greenfield** (xact-scoped locks exist; session-scoped is new) |
| Distributed job dispatch | `SELECT ... FOR UPDATE SKIP LOCKED` | **Proven** (cron); port the job dispatcher |
| Per-turn / per-thread serialization | `thread_advisory_lock` or a DB column | **Shipped** for provisioning; extend to per-turn |
| Cross-replica fan-out (low volume) | `LISTEN/NOTIFY` + `notify_channel()` | **Transport shipped**; generalize channels |
| Cross-replica fan-out (high volume) | DB write + agent-pod (`thread_events` SSE) | **Shipped** |
| One-message-one-consumer (NATS) | Queue groups | **Greenfield**, one line per subscription |

Still nothing exotic: Postgres + NATS only. No Redis, etcd, Zookeeper, Temporal.

## What stays untouched

- **The REST endpoints.** Stateless requests against the DB; work behind a load balancer today.
- **Migrations.** Concurrency-safe (`migrate.py:157`).
- **Authentication.** Keycloak OIDC stateless per request; cookie-BFF sessions and MCP tokens are DB-backed. No session affinity required.
- **JSONB atomic merges.** Already handle concurrent writers.
- **WebSocket routing.** Already solved by direct-to-agent-pod ingress; no sticky sessions, no orchestrator-side WS state.

## Out of scope

- **Database HA.** Assumed handled separately (CloudNativePG, etc.) — see [[high_availability_setup]] Priority 1. Multi-orchestrator over single-Postgres still has a DB SPOF.
- **NATS HA.** Single-replica today with clustering *explicitly disabled* (`helm/templates/nats/configmap.yaml:32-33`). Matters more once orchestrator goes multi-replica — [[high_availability_setup]] Priority 2.
- **Sharding by tenant/user.** Premature; "more identical replicas in front of one DB" suffices for the foreseeable future.
- **Cross-region / multi-cluster.** Single-cluster only.
- **Worker (agent) HA.** Agents are already horizontally scaled; the dispatcher is their load balancer.
- **MCP server HA.** Separate Deployment (`helm/templates/mcp/deployment.yaml`), already scalable via `mcp.replicas`; stateless per request.
- **vm-controller HA.** Singleton by design (NATS consumer, hardcoded `replicas: 1`); leader election would be the fix, same pattern as Layer 1.

## Open questions

1. **Leader handoff mid-cycle → graceful step-down.** On SIGTERM or a dropped lock-connection, the leader must *stop* the loops and release the lock in a `finally` (a cancelled asyncio task doesn't release it — GreptimeDB war story). The dispatcher's `SKIP LOCKED`+CAS makes a mid-cycle handoff safe; the other loops should commit progress transactionally. *(Research-informed.)*
2. **Failover latency — RESOLVED (config, not design).** Clean shutdown releases the lock instantly; hard-failure detection is governed by Postgres TCP keepalives (Linux default ~2h). Tune `tcp_keepalives_*` (→ ~40s) + `idle_session_timeout` server-side. The doc's old flat "~30s" → "instant on clean shutdown, ~40s on hard failure (after tuning)."
3. **`LISTEN/NOTIFY` payload limit (8000 bytes).** We send IDs only; document so future work doesn't grow the payload.
4. **Probe behavior during DB outage.** A replica that can't reach Postgres can't acquire leadership and should fail readiness so the LB routes around it. Track 2 should make readiness DB-aware.
5. **Failover latency during deploy.** Rolling `replicas: 2`, `maxUnavailable: 0` → zero-downtime steady state; a draining old leader releases the lock on clean shutdown (instant), so the new replica acquires promptly. Document.
6. **Dispatcher wake latency.** With DB-level dispatch, a `NOTIFY`-on-job-insert can wake the dispatcher immediately (`_trigger_dispatch` already exists in-process). Follow-up; doesn't block Track 2.
7. **`replicas: 2` after Layer 1 only.** **Yes for steady state** — leader election makes the loops singleton. **But** the ~40s dual-leader partition window double-dispatches unless the dispatcher also has `SKIP LOCKED`+CAS and the other non-idempotent loops are DB-guarded — both now **folded into M1**. So the honest unlock = leader election **+ the dispatcher/loop resource-guards**.
8. **Daemon-side sudo reply subject.** Before deleting `_pending_msgs`, confirm the sudo daemon listens on the deterministic published `nats_reply_subject`, not a connection-bound `_INBOX.>` auto-inbox.
9. **Single leadership lock, not per-loop — RESOLVED (research).** Connection budget (pool `max_size` 10; a session lock pins a connection for life). Per-loop locks are an M2 load-spreading optimization with their own dedicated lock-pool.
10. **External-Postgres pooler guard — (new, owed in M1).** `internal: false` could point at a transaction-mode pooler that breaks session locks. Add a startup self-check or a loud values/doc warning.

## Implementation phases

Each phase independently shippable, ordered so each improves posture even if the next never lands.

### Phase 0 — Track 1: Active-passive failover hardening — SHIPPED (chart); chaos test operator-pending
- [x] Tighten readiness probe + add `preStop` hook + `terminationGracePeriodSeconds` + `startupProbe` in `helm/templates/orchestrator/deployment.yaml`. (2026-06-24)
- [x] Add `helm/templates/orchestrator/pdb.yaml` (mirror `agent/pdb.yaml`), `minAvailable: 0`. (2026-06-24)
- [ ] Move module-level singletons behind `lifespan` startup for clean SIGTERM. **Deferred** — cosmetic; tracked as a follow-up.
- [ ] Chaos test. **Local k3d mechanics verified 2026-06-24** (startupProbe no crash-loop on a ~5-min cold start, preStop 18s drain, PDB allows drain). **Live multi-node + real-traffic test deferred** to a quiet overnight window after M0 reaches dev — tracked in `docs/tests/orchestrator_m0_failover_verification.md` (runbook: `docs/operations/orchestrator_failover.md`).
- [x] Document failover behavior in `docs/operations/orchestrator_failover.md`. (2026-06-24)

### Phase 1 — Track 2 Layer 1: Leader election — GREENFIELD (design refined by 2026-06-25 research)
- [ ] `orchestrator/services/leader_election.py` — a **single** leadership lock (`run_as_leader`) via **session-scoped** `pg_try_advisory_lock(LEADER_ID)` on a **dedicated, reconnecting** connection (model on `reload.py:71-150`), with random startup jitter + graceful step-down (release in `finally`).
- [ ] Lock-ID registry module: `LEADER_ID` alongside `LOCK_ID`/`MAINT_LOCK_ID` (packed-ASCII int64; never collide).
- [ ] The elected leader starts the singleton loops registered in `lifespan` (confirm the current block at impl time); leave the already-HA-safe loops + `main_cloud_listen_task` running on all replicas.
- [ ] **Dispatcher correctness (folded in from Layer 2):** port `get_dispatchable_jobs` to `FOR UPDATE SKIP LOCKED` + add `WHERE assigned_agent_id IS NULL` CAS to the assign-write (`postgres.py:1029`) — closes the dual-leader double-assign.
- [ ] **Failover tuning:** set `tcp_keepalives_*` + `idle_session_timeout` on the Postgres deployment (chart values); document instant-clean / ~40s-hard.
- [ ] **External-pooler guard:** startup self-check (or loud doc/values warning) that the DB connection isn't transaction-pooled.
- [ ] Harden other non-idempotent loops at the resource: `UNIQUE(email_message_id)`+`ON CONFLICT` (imap), CAS on delegation resume, insert-marker-before-send (notify/digest).
- [ ] Log "leader acquired/lost" at INFO.
- [ ] Tests (pytest + `PostgresContainer`, base = `tests/test_audit_store.py`): two asyncpg sessions / one DB → exactly one acquires; kill the leader's connection → follower acquires within the poll interval.
- [ ] **Unlock:** `replicas: 2` becomes safe here (with the dispatcher/loop guards above).

### Phase 2 — Track 2 Layer 2: DB-level coordination — PORT THE PROVEN PATTERN
- [ ] Port `get_dispatchable_jobs` to `FOR UPDATE SKIP LOCKED` (model on `fetch_next_due_cron_automation`); remove `_dispatch_lock`.
- [ ] Migration: `jobs.pause_requested_at`, `threads.in_flight_turn_id` (or extend `thread_advisory_lock`), `projects.heal_in_progress_until`.
- [ ] Remove `_pause_pending_job_ids`, `_thread_turn_locks`, `_thread_turn_inflight`, `_project_heal_locks`, `_over_quota_projects` (read the gate from DB).
- [ ] Tests: two replicas racing one job → one assignment; racing one turn → one 200 + one 409.

### Phase 3 — Track 2 Layer 3: Cross-replica fan-out — GENERALIZE SHIPPED TRANSPORT
- [ ] Generalize `run_listen_loop`/`notify_channel` to per-subscriber channels; back `notification_feed` (incl. `session.lifecycle`) with `user_events` + NOTIFY.
- [ ] `sudo_gate._sse_queues` → DB-backed with NOTIFY.
- [ ] Delete `sudo_gate._pending_msgs`; always publish on the persisted `nats_reply_subject` (after Open Question #8).
- [ ] (Already done: SSE reads `thread_events` from DB — no work.)
- [ ] Tests: emit on replica A → SSE client on replica B receives; resolve sudo via REST on replica B → NATS-bound agent gets the decision.

### Phase 4 — Track 2 Layer 4: NATS queue groups — GREENFIELD/TRIVIAL
- [ ] Add `queue="orchestrator"` to the six subscriptions in `nats_bridge.py:163-192`.
- [ ] Remove `_thread_vm_ids`; replace with a Postgres lookup per message.
- [ ] Tests: two replicas on NATS; publish a status message; exactly one processes it.

### Phase 5 — Operational polish
- [ ] DB-aware liveness/readiness (Open Question #4).
- [ ] `NOTIFY`-on-insert dispatcher wake-up (Open Question #6).
- [ ] Bump `orchestrator.replicas: 2` in `helm/values.yaml` (or the Fleet `values-experimental.yaml` override). Watch a deploy cycle; declare done.

## ADR: alternatives considered, not adopted

- **Temporal for orchestration.** Requires a separate cluster + worker SDK, multi-week migration. Postgres advisory locks + `SKIP LOCKED` cover the failure modes; the refresh shows both are already in production for cron/provisioning. Revisit only if we outgrow Postgres for coordination.
- **Kubernetes Lease API.** Cleaner conceptually, but we already depend on Postgres for everything; advisory locks have lower failover latency and test cleanly with a Postgres testcontainer.
- **Redis Sentinel/Cluster for fan-out.** We don't run Redis; `LISTEN/NOTIFY` is sufficient at our volume (and already shipped). Add Redis only if NOTIFY throughput becomes a bottleneck.
- **Sticky-session ingress for WebSockets.** Moot — the WS path is direct-to-agent-pod and the stream source-of-truth is `thread_events` ([[direct_session_websockets]], [[headless_persistent_sessions]]).
- **`threads.in_flight_turn_id` column vs. `thread_advisory_lock`.** The shipped advisory lock already serializes per-thread provisioning; extending it to per-turn input is less new code than a new column + conditional UPDATE. Either works; lean on the shipped primitive.

## Related code

- `orchestrator/main.py:559` — `_dispatch_lock`; acquired `main.py:3733`.
- `orchestrator/main.py:562` — `_pause_pending_job_ids`.
- `orchestrator/main.py:565` — `stale_agent_detector` (60s).
- `orchestrator/main.py:673` — `agent_pool_reconciler`.
- `orchestrator/main.py:702` — `lifecycle_reconciler_loop`.
- `orchestrator/main.py:779` — `workspace_idle_sweeper` (reconcile-only).
- `orchestrator/main.py:906` — `snapshot_gc_sweeper`.
- `orchestrator/main.py:981` — `imap_poll_loop` → `services/imap_poller.py`.
- `orchestrator/main.py:1249` — `_over_quota_projects`; quota poll `_quota_poll_tick` `main.py:1336`.
- `orchestrator/main.py:3347` — `_threads_suspending`.
- `orchestrator/main.py:4069` — `auto_assign_dispatcher`; core `_try_dispatch_pending_jobs` `main.py:3720`.
- `orchestrator/main.py:4866` — `lifespan`; task registrations `main.py:5191-5323`.
- `orchestrator/main.py:7304` / `:7521` — notification / sudo SSE endpoints.
- `orchestrator/main.py:8807` — `_check_delegation_timeouts` (loop `:8950`).
- `orchestrator/main.py:10190` — IDE WebSocket proxy (the only remaining `@app.websocket`).
- `orchestrator/main.py:15860-15861` — `_thread_turn_locks`, `_thread_turn_inflight` (comment `:15857` "Single-instance orchestrator").
- `orchestrator/main.py:15993` — `thread_event_stream` SSE (DB-backed, stateless).
- `orchestrator/main.py:21941` — `_project_heal_locks`.
- `orchestrator/main.py:22976` — `_knowledge_graph_db` cache.
- `orchestrator/services/notification_feed.py:23` — `_user_queues`.
- `orchestrator/services/sudo_gate.py:35,37` — `_sse_queues`, `_pending_msgs`; fallback publish `:666-684`.
- `orchestrator/services/nats_bridge.py:82` — `_thread_vm_ids`; subscriptions `:163-192`; subject scoping `_subj` `:98`.
- `orchestrator/services/cron_dispatcher.py` + `postgres.py:9335` — `SKIP LOCKED` work-queue (reference example).
- `orchestrator/services/project_loop_sweeper.py:40` + `postgres.py:9295` — CAS claim.
- `orchestrator/services/audit_partitions.py:77,167` — `MAINT_LOCK_ID` maintenance lock.
- `orchestrator/services/cloud/reload.py:50` + `postgres.py:8839` — `LISTEN/NOTIFY` loop + `notify_channel()` helper.
- `orchestrator/database/postgres.py:2449` — `get_dispatchable_jobs` (no `SKIP LOCKED` yet).
- `orchestrator/database/postgres.py:2377` — `recover_orphaned_jobs`; `:2350` `mark_stale_agents_offline`.
- `orchestrator/database/postgres.py:1402` — `merge_job_context` (atomic JSONB).
- `orchestrator/database/postgres.py:2568` — `thread_advisory_lock`.
- `orchestrator/database/migrate.py:21,157` — `LOCK_ID` + advisory-lock template for leader election.
- `src/api/persistent_app.py:2524` — agent-pod `thread_events` writer (stream source-of-truth).
- `helm/templates/orchestrator/deployment.yaml:12` (`replicas`), `:26-56` (init containers), `:1036-1047` (probes); `helm/values.yaml:95`.
- `helm/templates/agent/pdb.yaml` — PDB template to mirror for the orchestrator.
- `deployment/fleet.yaml` — live deploy mechanism (Helm via Fleet; `legacy/**` ignored).

## Decision log

- **2026-05-12:** Two-track plan: active-passive hardening (Track 1) first, then incremental active-active layers (Track 2).
- **2026-05-12:** Postgres advisory locks chosen for leader election over Kubernetes Leases.
- **2026-05-12:** `SELECT ... FOR UPDATE SKIP LOCKED` chosen for dispatch over a queue service.
- **2026-05-12:** Postgres `LISTEN/NOTIFY` chosen for low-volume cross-replica fan-out.
- **2026-05-12:** NATS queue groups chosen for one-message-one-consumer.
- **2026-05-12:** No new infra (Postgres + NATS only). Database HA out of scope.
- **2026-05-12:** Sticky-session ingress not required.
- **2026-06-24 (refresh):** Reconciled with code. Status corrected from "no implementation yet": Layer 2 (`SKIP LOCKED`) and Layer 3 (`LISTEN/NOTIFY`) patterns are **already shipped** for cron / cloud-reload; `thread_advisory_lock` covers per-thread serialization; headless Phase 2 (`thread_events` SSE) and direct-to-agent-pod WS make the entire WS-fan-out concern moot (`persistent_ws_proxy` removed).
- **2026-06-24:** `_pending_msgs` downgraded from correctness-blocker ("agent waits forever") to reliability/latency degradation — the persisted `nats_reply_subject` fallback already delivers cross-replica.
- **2026-06-24:** `delegation_timeout` upgraded from "needs investigation" to **confirmed racy** (double parent-resume). `_over_quota_projects` added as a new correctness blocker. `main_cloud_listen_task` removed from the double-fire list (intentional per-replica fan-out).
- **2026-06-24:** **Leader election (Layer 1) confirmed as the single greenfield primitive and the gating item for `replicas: 2`.** Must use *session*-scoped `pg_advisory_lock` (all existing locks are xact-scoped). Stale `MIGRATION_LOCK_ID` reference corrected to `LOCK_ID`.
- **2026-06-24:** Deployment references repointed from `deployment/legacy/` (frozen, ignored by Fleet) to `helm/` (the live path).
- **2026-06-24:** Added the **Roadmap** section — milestone sequencing (M0-M4) across both HA docs. Separates M1 (failover HA, no data-tier dependency) from M2 (scale-out), and defines a minimum vs full OSS-release bar with external/managed Postgres as a legitimate posture for the minimum bar. Dependency-ordered, no calendar dates set.
- **2026-06-25 (research):** Web + codebase research (`docs/researches/orchestrator_leader_election.md`, 5-agent) validated advisory lock over k8s Lease (Python Lease ecosystem immature: ConfigMap-only client, Lease PR closed unmerged) and **verified no connection pooler** (session locks safe; external-Postgres mode is the one risk). Four corrections folded into Layer 1 / M1: **single leadership lock** on a dedicated connection (not per-loop — connection budget); **mandatory Postgres TCP-keepalive tuning** (else ~2h hard-failure failover); **leader election = efficiency-not-correctness** → the dispatcher's `SKIP LOCKED` + assign-write CAS (`postgres.py:1029` has none today) moves **into M1**; **graceful step-down** (release in `finally`). New owed item: external-pooler startup guard.

## Sources

- Postgres advisory locks: [PG docs](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS); used in-repo at `migrate.py:157`, `audit_partitions.py:167`, `postgres.py:2584`.
- `SELECT ... FOR UPDATE SKIP LOCKED`: [PG docs](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE); used in-repo at `postgres.py:9335` (cron). Pattern also used by [graphile-worker](https://github.com/graphile/worker), [pgmq](https://github.com/tembo-io/pgmq), Sidekiq's Postgres adapter.
- Postgres `LISTEN/NOTIFY` scaling constraint: [Recall.ai write-up](https://www.recall.ai/blog/postgres-listen-notify-does-not-scale); in-repo at `services/cloud/reload.py`.
- NATS queue groups: [NATS docs](https://docs.nats.io/nats-concepts/core-nats/queue).
- Kubernetes `PodDisruptionBudget`: [k8s docs](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/).
- Kubernetes Lease API (considered, not chosen): [k8s docs](https://kubernetes.io/docs/concepts/architecture/leases/).
