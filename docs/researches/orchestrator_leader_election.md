# Research — Leader Election for the Orchestrator (M1)

**Date:** 2026-06-25
**Question:** What's the best way to run the orchestrator's ~10 singleton background loops on exactly one of N replicas, so we can safely go `replicas: 2+`? (Milestone M1 in `docs/features/orchestrator_ha_scaling.md`.)
**Method:** five parallel research agents — Postgres advisory-lock mechanics, Kubernetes Lease, how big projects do it, distributed-systems theory (fencing/split-brain), and our own codebase. Sources at the bottom.

## TL;DR

- **Our chosen primitive — a session-scoped Postgres advisory lock — is the right call** for a Python + Postgres-heavy orchestrator that runs no Redis/etcd. The main alternative (Kubernetes `Lease`) is **not cleanly available in async Python** (the official client ships ConfigMap-only leader election; the Lease PR was closed unmerged 2025-11-21; `kopf` uses its own CRDs). Reuse Postgres.
- **The make-or-break precondition is satisfied:** SRW talks to Postgres **directly via asyncpg** (no PgBouncer/pgcat/RDS-Proxy). Transaction-mode poolers silently break session-scoped advisory locks; we don't have one. *(Caveat: the chart supports external Postgres — add a guard/doc note so an operator doesn't point us at a transaction-mode managed pooler.)*
- **Three corrections to the current M1 design** (details below): (1) tune Postgres TCP-keepalives or failover takes **~2 hours**, not 30s; (2) use **one leadership lock, not one-per-loop** (connection budget); (3) leader election is an **efficiency** mechanism, **not** correctness — the dispatcher needs `SKIP LOCKED` + a CAS guard *regardless*, because two leaders can briefly coexist.

---

## 1. Decision: Postgres advisory lock vs Kubernetes Lease

**Verdict: Postgres advisory lock.** Both options have the *same* correctness ceiling (neither fences — see §4), so the tiebreaker is operational cost, and Postgres wins decisively for us:

| Criterion | Postgres advisory lock | K8s Lease |
|---|---|---|
| New dependency / RBAC | none (reuse Postgres) | k8s API + `coordination.k8s.io/leases` RBAC |
| Python maturity | excellent (one `asyncpg` call) | **poor** — official client is ConfigMap-only; [Lease PR #2314 closed unmerged 2025-11-21](https://github.com/kubernetes-client/python/pull/2314); `kopf` = own CRDs + framework |
| Async/FastAPI fit | native | awkward (sync/threaded) |
| Survives Postgres outage | no — *but the orchestrator is down anyway, so moot* | yes |
| Fencing / unique-leader | no | **no** (client-go: *"does not guarantee that only one client is acting as a leader"*) |

This matches our ADR; the research strengthens it with the concrete Python-ecosystem finding.

## 2. The make-or-break precondition — connection pooler (we're safe)

A session-scoped advisory lock binds to one backend connection. A **transaction-mode pooler** (PgBouncer marks session advisory locks **"Never"**) reassigns backends per-transaction, so the unlock lands on the wrong backend, returns false silently, and **leaks the lock** (which then pins `xmin` and blocks autovacuum cluster-wide — a real outage class).

**Our verdict (verified): no pooler.** Orchestrator → `asyncpg.create_pool` → ClusterIP Service `srw-postgres` → single Postgres StatefulSet. No pgbouncer/pgcat/pgpool/RDS-Proxy anywhere in `helm/`. Session locks are safe here.

**Owed:** the chart allows `databases.postgres.internal: false` (external Postgres). If an operator points that at a managed **transaction-mode** pooler, session locks break silently. Ship either a startup self-check (acquire, re-check from the same logical call, fail loud if it doesn't hold) or a prominent doc/`values` warning: *leader election requires a session/direct Postgres connection, not a transaction-pooled one.* (RDS Proxy is subtler — it *pins* rather than breaks, killing the pooling benefit; and it explicitly does **not** pin on the `_xact_` variants.)

## 3. Failover latency — the 2-hour footgun (must tune)

A session lock releases only when Postgres reaps the holder's backend.
- **Clean pod shutdown** (SIGTERM → TCP FIN): lock releases **instantly** (`pg_advisory_unlock_all` fires at session end). Good — this covers rolling deploys.
- **Hard kill / OOM / network partition** (no FIN): Postgres notices only via TCP keepalive, and **vanilla Postgres `tcp_keepalives_*` default to the OS value — ~2 hours on Linux.** So a partitioned leader's lock can stay held for ~2h → no failover. This is the single most-missed operational detail.

**Required tuning (server-side** — these govern the server's socket toward the client, which is what releases the lock):

```
tcp_keepalives_idle = 10
tcp_keepalives_interval = 10
tcp_keepalives_count = 3        # → dead-holder detected in ~40s
idle_session_timeout = 60000   # ms; backstop for a dead *idle* backend
```

Detection ≈ `idle + interval×count`. **Trap:** `client_connection_check_interval` does **not** help here — it only fires *while a query is running*, not for an idle lock-holder sitting between ticks. Verify on our cluster (`SHOW tcp_keepalives_idle;`) — self-hosted Postgres does **not** ship Aurora's non-zero defaults. This means the realistic failover window is **clean-shutdown ≈ instant, hard-failure ≈ 40s** (with tuning), vs the doc's flat "~30s".

## 4. The safety model — leader election is for *efficiency*, not *correctness*

This is the load-bearing insight from the theory + the big-project precedents, and it changes how we build M1.

**Every lease/lock has a "two leaders" window:** a partitioned-but-alive leader keeps working until its lock is reaped (the ~40s above), and a **Postgres primary→replica failover wipes all advisory locks instantly** (they're never WAL-logged), so all replicas briefly re-acquire. There is **no fencing.** Kleppmann's canonical rule: *a lock alone is not safe for correctness; you need fencing tokens or idempotent/CAS-guarded operations.* Antirez disputes the details but agrees the **resource** must guard the operation. AWS's leader-election guidance and Kubernetes' own docs stop at "make it idempotent."

**Therefore the design rule:** the advisory lock decides *which replica does the redundant work* (efficiency); **correctness must come from the resource** — idempotency, a unique constraint, or `FOR UPDATE SKIP LOCKED`/CAS. The industry agrees on a clean split (graphile-worker, river, pgmq, SolidQueue, **and Airflow's own multi-scheduler — which deliberately rejected ZooKeeper/Consul leader election in favor of `FOR UPDATE SKIP LOCKED` to minimize operational surface**):

- **Queue / dispatch work → `SKIP LOCKED` on all replicas, no leader.** Replicas grab disjoint rows; a crash auto-releases.
- **Periodic "tick" emitters → leader-only, or run-everywhere + `UNIQUE(key, time)` dedup** (GoodJob/SolidQueue migrated cron *off* advisory locks to a unique index).
- **Stateful continuous loops** (a poller holding a cursor; reconcilers) → leader election is the clean fit.

**The smoking gun in our code** (why this matters now): `get_dispatchable_jobs` has **no `SKIP LOCKED`**, *and* the assignment write is `UPDATE jobs SET status='processing', assigned_agent_id=$X WHERE id=$job` with **no CAS guard** (`postgres.py:1029`), and the agent HTTP POST fires *before* the write. So during the dual-leader window, two leaders genuinely **send the same job to two agents**. Leader election fixes the steady state but **not** the partition window. The fix (`SKIP LOCKED` + `WHERE assigned_agent_id IS NULL` CAS) is what the rest of the industry uses as the *primary* dispatch mechanism — so it belongs **in M1**, not deferred wholesale to M2.

### Per-loop hardening (from the codebase audit)

| Loop | Brief double-run safe? | Resource-side guard to add (besides leader election) |
|---|---|---|
| `auto_assign_dispatcher` | **No — sends job to 2 agents** | `SKIP LOCKED` on `get_dispatchable_jobs` + `WHERE assigned_agent_id IS NULL` CAS on the assign write. **Bundle into M1.** |
| `imap_poll_loop` | **No — double reply / double sudo** | `UNIQUE(email_message_id)` + `ON CONFLICT DO NOTHING` (today the index is non-unique). |
| `quiet_hours_digest_loop` | **No — double-send email** | leader-only is the fix; or `SKIP LOCKED` the pending rows before send. |
| `delegation_timeout_sweeper` | **No — double parent-resume** | status CAS on resume (+ `SKIP LOCKED` scan). |
| `thread_permission_notify_sweeper` | **No — same-tick double email** | insert-the-marker-then-send, or leader-only. |
| `stale_agent_detector` | Yes (CAS-guarded writes) — but pokes the dispatcher | wrap for cleanliness; dispatcher guard above covers the poke. |
| `agent_pool_reconciler`, `lifecycle_reconciler_loop` | Mostly (idempotent K8s ops); double-provision risk | leader-only (no DB queue applies). |
| quota poll (`_over_quota_projects`) | Writes idempotent; **in-memory gate is per-process** | leader-only loop **+** followers must read the gate from DB/NOTIFY (M2) or they dispatch over-quota jobs. |

**Already HA-safe — do NOT wrap:** `cron_dispatcher` (`SKIP LOCKED`), `project_loop_sweeper` (CAS), audit maintenance (xact lock), metering (`ON CONFLICT`), and `main_cloud_listen_task` (intentional per-replica fan-out).

## 5. Connection budget → one leadership lock, not one-per-loop

Our asyncpg pool defaults to `max_size=10`; Postgres is stock `max_connections=100` (shared across orchestrator + migrate + audit + vector + MCP + jobs). A session lock **pins a connection for the loop's life**. The doc's sketch (`with_leader_lock(name, loop_id, …)` per loop from the shared pool) would pin ~10 connections → **starve the leader replica's request pool.**

**Recommendation: a single leadership lock.** One `pg_advisory_lock(LEADER_ID)` on **one dedicated connection** (the `services/cloud/reload.py` out-of-pool + reconnect-with-backoff pattern is the template); the elected leader then starts all singleton loops locally. Cost: 1 connection. Simpler, one failover unit, matches "M1 = failover HA." Per-loop locks (to spread loops across replicas) is an **M2** optimization and would need its own dedicated lock-pool + bumped `max_connections` — not worth it for M1.

## 6. Implications for the M1 design (changes to fold into the spec)

1. **Single leadership lock** on a dedicated, reconnecting, out-of-pool connection — not per-loop. (Revises the doc's pseudocode.)
2. **Bundle the dispatcher's `SKIP LOCKED` + CAS into M1** (it's the correctness floor for the dual-leader window, not optional M2 polish). Optionally also the `imap_poll_loop` unique-index and the delegation/digest/notify guards — cheap, and they remove the "two-leaders → user-visible duplicate" footguns.
3. **Failover tuning is a required sub-task:** set `tcp_keepalives_*` + `idle_session_timeout` on the Postgres deployment (chart/values), and document the realistic windows (instant on clean shutdown, ~40s on hard failure).
4. **Graceful step-down:** on lost leadership or lock-connection death, **stop the loops** (don't keep running); release the lock in a `finally`/guard (a cancelled asyncio task does **not** release the lock — GreptimeDB war story).
5. **External-pooler guard** (startup self-check or doc warning) for `internal: false`.
6. **Keep the lock-ID registry** (extend the existing packed-ASCII int64 style: `LOCK_ID`/`MAINT_LOCK_ID`) — but it's now just `LEADER_ID` plus any future per-loop ids.
7. **Test harness:** the `tests/test_audit_store.py` `PostgresContainer` fixture is the base — open two asyncpg sessions against one DB, assert exactly one acquires, kill the leader's connection, assert failover. No k8s needed. Manual two-replica test on k3d/tilt: set `orchestrator.replicas: 2` + flip `pdb.minAvailable: 1`.

## 7. Operational checklist (carry into runbooks)

- Tune `tcp_keepalives_*` / `idle_session_timeout` **before** `replicas: 2`.
- Hold the leader lock on a **dedicated** connection; never a pooled/borrowed one.
- Release via explicit guard; treat "can't confirm my lock connection is alive" as loss of leadership and halt work.
- Random startup jitter so co-booting replicas don't thrash the lock.
- Monitor stuck advisory locks (`pg_locks` ⋈ `pg_stat_activity`) — a leaked holder blocks autovacuum; `pg_terminate_backend(pid)` is the escape hatch. Note `pg_locks` can't distinguish session vs xact advisory locks.
- Make every correctness-critical loop step idempotent or DB-guarded; "mostly idempotent" is a yellow flag — audit each.

---

## Sources (curated)

**Postgres advisory locks / failover**
- [PostgreSQL: Advisory Lock Functions](https://www.postgresql.org/docs/current/functions-admin.html) · [Explicit Locking §13.3.5](https://www.postgresql.org/docs/current/explicit-locking.html) · [Hot Standby §26.4.3 (locks never WAL-logged)](https://www.postgresql.org/docs/current/hot-standby.html) · [Connection settings (keepalives, client_connection_check_interval)](https://www.postgresql.org/docs/current/runtime-config-connection.html)
- [AWS RDS: Dead connection handling in PostgreSQL](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.DeadConnectionHandling.html) · [CYBERTEC: TCP keepalive for PostgreSQL](https://www.cybertec-postgresql.com/en/tcp-keepalive-for-a-better-postgresql-experience/)
- [Jeremy D. Miller: PG advisory locks for leader election](https://jeremydmiller.com/2020/05/05/using-postgresql-advisory-locks-for-leader-election/)

**The pooler gotcha**
- [PgBouncer feature map (session advisory locks = "Never" in txn mode)](https://www.pgbouncer.org/features.html) · [AWS: Avoiding RDS Proxy pinning](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy-pinning.html) · [JP Camara: PgBouncer is useful… and fraught](https://jpcamara.com/2023/04/12/pgbouncer-is-useful.html)

**K8s Lease**
- [client-go leaderelection (the "no fencing" warning + defaults)](https://pkg.go.dev/k8s.io/client-go/tools/leaderelection) · [Kubernetes: Leases](https://kubernetes.io/docs/concepts/architecture/leases/) · [kubernetes-client/python PR #2314 — Lease support, closed unmerged](https://github.com/kubernetes-client/python/pull/2314) · [kopf peering](https://docs.kopf.dev/en/stable/peering/) · [cert-manager best practice](https://cert-manager.io/docs/installation/best-practice/)

**Big-project precedents**
- [GoodJob cron + advisory locks (and why they migrated off them)](https://island94.org/2023/01/how-goodjob-s-cron-does-distributed-locks) · [GoodJob README](https://github.com/bensheldon/good_job)
- [Airflow AIP-15: multi-scheduler via SELECT FOR UPDATE (rejected consensus)](https://cwiki.apache.org/confluence/pages/viewpage.action?pageId=103092651) · [Airflow scheduler HA docs](https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/scheduler.html)
- [River: leader election (TTL row + LISTEN/NOTIFY)](https://riverqueue.com/docs/leader-election) · [River periodic jobs](https://riverqueue.com/docs/periodic-jobs) · [graphile-worker cron](https://worker.graphile.org/docs/cron) · [Sidekiq Ent leader election](https://github.com/sidekiq/sidekiq/wiki/Ent-Leader-Election) · [RedBeat #208 (swallowed lock error → dup schedulers)](https://github.com/sibson/redbeat/issues/208)

**Theory / fencing**
- [Kleppmann: How to do distributed locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) · [antirez: Is Redlock safe?](https://antirez.com/news/101) · [AWS Builders' Library: Leader election](https://aws.amazon.com/builders-library/leader-election-in-distributed-systems/) · [Surfing Complexity: Locks, leases, fencing tokens](https://surfingcomplexity.blog/2025/03/03/locks-leases-fencing-tokens-fizzbee/)
