# Orchestrator HA — Background-Loop Replica-Safety Sweep

**Feature:** `docs/features/orchestrator_ha_scaling.md` — Milestone M2 follow-on (replica-safety completeness).
**Related:** M1 (leader election), M2-L4 (`docs/tests/orchestrator_m2_l4_nats_replica_safety_verification.md`).

**Status (2026-06-29): Sweep complete; 2 gaps found and fixed (leader-gate, `main.py`), `ruff` clean, UNCOMMITTED on develop; LIVE-VERIFIED on local k3d with two replicas (leader runs both sweepers, follower runs neither). Dev-cluster deploy pending.**

> Note: `main.py` line references below are anchored to the 2026-06-29 working tree. The file is volatile (it grew ~230 lines vs the snapshot used during the sweep) — anchor by function name; the fix site is `attention_sleep_task`/`ide_sweeper_task` at `main.py:5641-5646`, just after `thread_permission_notify_sweeper` (5633) in the leader-gated cluster.

## Why this sweep

The chart default is now `orchestrator.replicas: 2`, so **every** self-hoster runs two replicas. Each replica independently runs its own copy of every background loop spawned at boot (`main.py` lifespan). M1 closed the cron/scheduled side-effect paths and M2-L4 closed the NATS fan-out paths — but those fixed the handlers *we knew to look for*. This sweep is the systematic pass: enumerate **every** always-on background loop and confirm each is leader-gated, claim/CAS-guarded, or genuinely idempotent under two replicas. The deliverable is confidence to declare orchestrator HA done with no lurking double-fire bug.

## Scope

- **28 background loops** spawned at boot (`main.py:5386–5538`).
- **6 NATS subjects** — already closed in M1 + M2-L4 (`nats_bridge.py`); the only `subscribe(` sites in the codebase.
- **Request-scoped `asyncio.create_task`** (auth notifications, session provisioning, etc.) — the K8s Service load-balances HTTP to **one** replica, so these are not fan-out / not double-fire surface. Out of scope by construction.

A loop is **safe** under `replicas:2` iff one holds: (a) **leader-gated** via `run_when_leader` (only the leader's copy acts); (b) **claim/CAS-guarded** (`ON CONFLICT … DO NOTHING`, `FOR UPDATE SKIP LOCKED`, or `UPDATE … WHERE <expected> RETURNING`); or (c) **genuinely idempotent** (set-based `DELETE … WHERE`, pure upsert, read-only poll, or convergent write). It is a **risk** if it does a read-then-side-effect with no per-row claim, or an un-deduped external side-effect (SSH, snapshot, pod/VM teardown, email/NATS-reply, job create).

## Result

**2 gaps — both the same class (singleton side-effecting sweeper: `SELECT` idle rows → snapshot/teardown, no claim, not leader-gated). 26 loops + all NATS subjects + all request-scoped tasks: safe.**

### Findings (fixed)

| # | Loop | Severity | Race under `replicas:2` |
|---|---|---|---|
| 1 | `attention_sleep_sweeper` (boot `main.py:5641`; def `main.py:17686`) | **Harmful** | Both replicas `SELECT` awaiting-user threads past TTL (deterministic `ORDER BY … LIMIT 50`, `main.py:17720-17743`) → `suspend_thread_workspace` → `capture_vm_snapshot` (SSH+S3) + teardown. Sole dedup is a **non-atomic** TOCTOU: read `ws_status` (`workspace_suspension.py:457,477`), then `merge_thread_workspace_context(status=suspending)` — a bare `UPDATE … WHERE id=$2`, **no** `WHERE status='ready'` precondition (`postgres.py:1681-1696`). Both pass, both snapshot to the same per-thread S3 key, both tear down. The thread-level CAS (`main.py:17759`) runs *after* the side-effect. Loser's snapshot SSH dies when the winner deletes the pod mid-stream → hits the `if not ok` revert (`workspace_suspension.py:514-517`), flipping status back to `ready` pointing at a deleted pod (wedged-workspace state) and threatening the resume snapshot's integrity. |
| 2 | `ide_session_ttl_sweeper` (boot `main.py:5644`; def `main.py:762`) | Low (wasteful) | `check_ttl_all` plain `SELECT … WHERE status IN ('active','idle')` (`ide_session.py:325-331`) → `stop_session` deletes IDE VM/pod, then merges `status='expired'` with no CAS. Both replicas delete the same session. **No snapshot** in `stop_session`; deletes are name-targeted (loser 404s). Harm: redundant delete API call + a thin ABA hazard (a stale delete could hit a freshly-recreated session). |

### Fix

Leader-gate both — two `run_when_leader` wraps at `main.py:5641-5646`, mirroring the **lifecycle reconciler** (`main.py:5760`) that already owns the parallel idle workspace-teardown path (note `workspace_idle_sweeper` is now reconcile-only — its old snapshot-before-delete was moved into that leader-gated reconciler; docstring `main.py:787-799`). This is a *complete* fix: only one replica runs the loop, and each loop is sequential within a replica, so the TOCTOU cannot fire. Zero throughput cost — both are singleton housekeeping loops. No internal-CAS hardening needed once concurrency is gone (YAGNI).

```python
# main.py:5641-5646 (after)
attention_sleep_task = asyncio.create_task(
    run_when_leader(attention_sleep_sweeper, _shutdown_event)
)
ide_sweeper_task = asyncio.create_task(
    run_when_leader(ide_session_ttl_sweeper, _shutdown_event)
)
```

Behavioral guarantee is provided by `run_when_leader` itself (already unit-tested in M1: it runs the wrapped coro only while `is_leader` is set and cancels it on leadership loss). No new test added — the change is a mechanical application of that proven primitive; `ruff check orchestrator/main.py` passes.

## Full safety ledger (28 boot loops)

**Leader-gated — `run_when_leader` (11, incl. the 2 fixed):** `stale_agent_detector`, `auto_assign_dispatcher`, `thread_permission_notify_sweeper`, `imap_poll_loop`, `quiet_hours_digest_loop`, `delegation_timeout_sweeper`, `agent_pool_reconciler`, `quota_poll_loop`, `lifecycle_reconciler_loop`, **`attention_sleep_sweeper`** (this fix), **`ide_session_ttl_sweeper`** (this fix).

**Claim/CAS-guarded (5):**
- `cron_dispatcher_loop` — `FOR UPDATE SKIP LOCKED` (`postgres.py:9581`).
- `workspace_metering_loop` — `ON CONFLICT (owner_kind,owner_id) WHERE ended_at IS NULL DO NOTHING` (`workspace_metering.py:87`).
- `llm_usage_poll_loop` — ledger `ON CONFLICT (source,source_id,unit,ts) DO NOTHING` (`usage_ledger.py:153`); cursor is per-replica in-memory only, no shared watermark.
- `sudo_expiration_sweeper` — `UPDATE … SET status='expired' WHERE status='pending' … RETURNING` (`sudo_gate.py:285-291`) claims each row before its NATS deny reply / SSE.
- `project_loop_sweeper_loop` — `claim_project_loop_advance` CAS `UPDATE … WHERE current_job_id=$2 AND status='running'` (`postgres.py:9551-9557`) gates job spawn.

**Idempotent / convergent (10):**
- `cleanup_expired_tokens` (`postgres.py:4619`), `cleanup_expired_sessions` (`postgres.py:4959,5017`), `thread_events_prune_sweeper`, `security_events_prune_sweeper` (`postgres.py:7138`) — pure set-based `DELETE … WHERE` of expired rows.
- `stale_verification_sweeper_loop` — set-based `UPDATE jobs SET status='cancelled' WHERE status IN ('created','paused') …` (`postgres.py:2542-2548`).
- `workspace_idle_sweeper` — reconcile-only; deterministic pod name + 409→adopt no-op (`container_provisioner.py:1213-1219`); teardown moved to the leader-gated lifecycle reconciler.
- `code_server_settings_sweeper` — newest-wins pull (mtime/version gates `ide_settings.py:957,1012`), deterministic per-user S3 keys, no pod mutation (write-back `seed_ide_config` not called here).
- `snapshot_gc_sweeper` — S3 copy/delete idempotent, per-object `try/except` (`snapshot_service.py:942-981`); wasteful under concurrency, not harmful.
- `litellm_sync_loop` — drift-gated reconcile keyed by deterministic `model_info.id`; steady state is a read-only `GET` (`litellm_gateway.py:414-417`). Caveat: a concurrent *catalog edit* can momentarily double-`POST /model/new`; self-heals (collapsed by id), traffic-transparent.
- `audit_maintenance_loop` — partition DDL serialized by `pg_advisory_xact_lock` + existence checks (`audit_partitions.py:150-152`); alarms are log-only (`:377-378`).

**Per-replica by design — fan-out wanted (2):**
- `run_listen_loop` (main_cloud config) — LISTEN → in-memory backend pointer swap only; no DB write-back, no re-NOTIFY (callback `_main_cloud_reload_callback`). Each replica needs its own fresh copy.
- `run_as_leader` — the leader-election mechanism itself; all replicas contend for the lock (the point).

## Live-verified on local k3d — two replicas (2026-06-29)

Verified on the local `k3d-srw` two-replica orchestrator (`srw` namespace, deploy `srw-orchestrator` 2/2). Tilt live-synced the fix into `/app/main.py`; presence confirmed in **both** pods via in-pod `grep` before reading behavior.

| Check | Result |
|---|---|
| Fix synced to both pods | **PASS** — `run_when_leader(attention_sleep_sweeper, …)` / `…(ide_session_ttl_sweeper, …)` present in `/app/main.py` on both replicas. |
| Leader runs both sweepers | **PASS** — leader `bqzl9` logged `leader_election: acquired leadership (lock 6003957320051409220` = `0x5352575F4C454144` = "SRW_LEAD"`)` at 13:40:25.759, then `Attention-sleep sweeper started` + `IDE session TTL sweeper started` ~0.26s later. |
| Follower runs neither | **PASS** — follower `nc72z` logged **no** leadership and **neither** sweeper "started" line — identical to the established leader-gated loops (IMAP / pool-reconciler / delegation-timeout, also absent on the follower). |
| Follower is healthy, not half-booted | **PASS** — follower logged all three **non-gated** sweepers (`Snapshot GC sweeper started`, `Workspace idle sweeper started (reconcile-only)`, `Code-server settings sweeper started`), proving its silence on the gated pair is the gate working, not a dead pod. |

Pre-fix, both sweepers would have appeared on the follower (the double-fire). They now sit in the same leader-only cluster as the M1-gated loops. The leader-gate is the M1 mechanism, already unit-tested; this confirms it composes correctly for the two newly-wrapped loops under two real replicas.

## Conclusion

The background-loop surface is now fully replica-safe: every always-on loop is leader-gated, claim/CAS-guarded, idempotent, or fan-out-by-design, each with cited evidence. Combined with M1 (leader election / scheduled side-effects) and M2-L4 (NATS fan-out), the orchestrator has no remaining known double-fire path under `replicas:2`. **The fix is leader-gate-verified on local k3d with two replicas; HA is ready to declare done for the OSS release once it deploys to the dev cluster.**
