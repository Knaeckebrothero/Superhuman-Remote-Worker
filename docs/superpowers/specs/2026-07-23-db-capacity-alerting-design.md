# DB Capacity Alerting — Design

- **Date:** 2026-07-23
- **Status:** SUPERSEDED IN PART (2026-07-27) — see **Decision** below. Retention shipped in-app; the app-level capacity monitor is dropped in favour of infra-level alerting.
- **Author:** Investigation + design pairing
- **Related incident:** dev thread `accfbc56` wedged 2026-07-23 — Postgres PVC (`srw-postgres-data`, 10 Gi Longhorn) hit 100 %, message/checkpoint saves failed as "non-fatal" WARNINGs, and no one was alerted until a session visibly froze. **Recurred 2026-07-27** (job `e1192a9d`; the 16 Gi PVC refilled in 4 days) because the durable defenses had not yet shipped.

## Decision (2026-07-27) — split by concern; capacity alerting re-homed to infra

The two halves of this design have different natural homes, and we build them accordingly:

- **Retention (prevent the fill) = application layer. ✅ IMPLEMENTED this session.** A data-lifecycle concern the app owns. In-flight keep-last-N sweeper: `PostgresDB.prune_checkpoints_keep_last` + `orchestrator/services/checkpoint_retention.py` (leader-gated) + `main.py` lifespan wiring + `tests/test_checkpoint_retention.py`. Env: `CHECKPOINT_RETENTION_KEEP` (3), `CHECKPOINT_RETENTION_INTERVAL_S` (600). Committed as `9bb24cea` (the repo's auto-committer bundled it with unrelated work). **✅ PUSHED + DEPLOYED 2026-07-28, verified live on dev** — orchestrator logs `Checkpoint retention sweeper started (interval=600s, keep=3)` at 10:07:41Z and `checkpoint retention: pruned 76 rows (keep_last=3)` at 11:07:12Z (leader-gated: only one of the two replicas prunes). This is the **primary** defense — see the *Companion: checkpoint retention* section.
- **Capacity alerting (detect a filling PVC) = infrastructure / observability layer. ❌ DROP the app-level monitor (Components A + B below); do NOT build it in the orchestrator.** Two reasons: (1) an app-level monitor that polls Postgres is **blind exactly when it matters** — a full disk crash-loops Postgres, so the orchestrator can't query it and no alert fires; infra monitoring watches the PVC from *outside* the app and still fires. (2) PVC/disk capacity is a **generic, cross-volume** concern (postgres, pgvector, neo4j, gitea, garage, workspace PVCs + nodes), not app-specific. **Implement with Prometheus + Alertmanager** on `kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes > 0.8` per PVC (or Longhorn `longhorn_volume_actual_size_bytes` vs capacity) → Ntfy/Slack/email; or, if a full stack is too much now, a minimal CronJob hitting the kubelet stats API. This needs a metrics stack stood up/revived (none in the app cluster today — only an OLD kube-prometheus-stack in `HomeLab/`), which is a separate infra decision.

**Everything below (Components A + B — the app-level `db_capacity_monitor` + `critical_admin_alert`) is retained for context only and is NOT to be built in the app.** The *Companion: checkpoint retention* section is the part that shipped.

## Problem

Two distinct observability gaps let a slow-motion outage go unnoticed until it broke a live session:

1. **No proactive capacity signal.** Nothing watches the Postgres data volume approach full. The 8.55 GB of `checkpoint_blobs` bloat that filled the 10 Gi PVC accreted over days with zero warning.
2. **Critical errors are swallowed at WARNING.** When writes finally failed, `No space left on device` was caught and logged as `Incremental message save failed (non-fatal)` (`src/api/persistent_app.py:4533`) — indistinguishable from routine noise. A Sev-1 died in a log line.

This spec covers **gap 1 (proactive)** for v1. Gap 2 (reactive error propagation) is a documented fast-follow (see below).

## Grounding — what already exists (verified 2026-07-23)

- **No metrics stack in the app.** No Prometheus / Grafana / Alertmanager / exporters / `ServiceMonitor` / `PrometheusRule` anywhere in `helm/`, `helm-vm-cluster/`, or `deployment/`. The only Prometheus artifact is an **OLD, dropped** kube-prometheus-stack in the out-of-band `HomeLab/` repo. → Adding an alert *rule* is not cheap; it means standing up a subsystem. **Rejected for v1** (feature-freeze).
- **A full multi-channel notification service already exists:** `orchestrator/services/notification_service.py` — `NotificationService` (class `:35`, singleton `:627`, `dispatch()` `:93`) with email + Ntfy + Slack + Discord + Cockpit SSE transports (`orchestrator/services/webhook_transports.py`, `orchestrator/services/email.py:52`). Per-transport `is_configured` gating (`:59`, `:198`); unconfigured channels no-op. **There is no global `NOTIFICATIONS_ENABLED` flag** (not referenced anywhere in the repo).
- **An admin fan-out helper exists:** `notify_admins_user_registered()` (`:370`) → `db.list_admin_user_ids()` → SSE + email to every admin. This is the pattern to generalize.
- **`NotificationPayload.priority`** (low / default / high / urgent) exists but is never driven by a severity computation.
- **A severity-escalation pattern exists** for the reactive fast-follow to copy: `AuxHealth` (`src/services/auxiliary.py:803`, `ESCALATE_AFTER=3`, `REPEAT_EVERY=20`) → heartbeat → `agents.aux_degraded` (`orchestrator/database/postgres.py:3805,3924`) → Cockpit badge.
- **Live channel config (dev):** only **SMTP is wired** (`SMTP_HOST/USER/FROM` set); Ntfy/Slack/Discord unset. So v1 alerts reach admins by **email + the Cockpit inbox**. Adding a louder channel later is env-only.
- **Health checks never inspect capacity.** Orchestrator `/api/health` (`orchestrator/main.py:7566`) is bare `{"status":"ok"}`; no `pg_database_size` call exists anywhere in the repo.

> File:line anchors are from exploration on 2026-07-23 and should be re-verified during implementation.

## Scope

**v1 (this spec):**
- **A.** A reusable `critical_admin_alert(...)` seam over the existing `NotificationService`.
- **B.** A proactive **DB capacity monitor** that polls Postgres size and alerts admins at configurable thresholds via A.

**Out of scope / non-goals:**
- Prometheus / exporters / Alertmanager (heavy new subsystem; deferred).
- A dedicated Cockpit `admin/health` page (v1 uses email + existing inbox).
- Generalizing to all 40+ `"(non-fatal)"` swallow sites — only the disk-full/critical-DB class, and that is the **reactive fast-follow**, not v1.
- `df`-via-exec exact PVC measurement (see accuracy note).
- The immediate ops remediation (expand PVC / prune `checkpoint_blobs`) — real and urgent, but that's incident cleanup, not this feature.

## Design

### Component A — `critical_admin_alert(kind, title, body, severity)`

New thin module `orchestrator/services/critical_alerts.py`. Fans a `NotificationPayload` out to all admins via the existing service:

- Resolve recipients via `db.list_admin_user_ids()` (reuse the `notify_admins_user_registered` path).
- Map `severity` → `priority`: `warning → high`, `critical → urgent`.
- **Dedup / rate-limit** keyed by `kind` (e.g. `db_capacity:main`): suppress repeats within a window, modeled on `AuxHealth`'s escalate/repeat cadence, so we never spam admins.
- Graceful no-op + logged error if the service is uninitialized or admin lookup fails (never raises into the caller).

This is the generic "Sev-1 → admins" pipe. The capacity monitor is its first consumer; the reactive fast-follow and any future swallowed-critical-error reuse it.

### Component B — DB capacity monitor

New `orchestrator/services/db_capacity_monitor.py`: a background async task started in the orchestrator's lifespan/startup, following the existing background-task/reconciler pattern.

**Targets.** A list of `{name, connection, capacity_source}`. v1 configures **one target — the main `srw` DB** (the volume that broke). The separate audit DB (`databases.audit`, its own PVC — 32Gi in dev, 10Gi chart default), pgvector, and neo4j are trivial additional targets (same code, one config entry each) → documented follow-up, not v1.

**Capacity source (the denominator) — chart-driven, never hardcoded.** The denominator is derived from the *same chart value that sizes the PVC* (`databases.<db>.storageSize`), so a deployment that sets `databases.postgres.storageSize: 500Gi` gets 70/90 % alerts computed against 500Gi automatically, with no code change. Helm renders that quantity string into an orchestrator env (`DB_CAPACITY_<TARGET>_SIZE`, e.g. `"16Gi"`); the monitor parses the K8s quantity → bytes. Precedence per target: (1) an explicit operator override env if set; (2) the chart-rendered `storageSize`; (3) if the DB is **external/managed** (`databases.<db>.internal: false`, no in-cluster PVC) and no override is given, the %-threshold alert is **disabled for that target** (capacity unknown, logged once) and coverage falls to the reactive disk-full escalation (fast-follow). This mirrors the existing `capacityBytes`-under-a-PVC precedent the bundled Garage store already uses (`helm/values.yaml:1574`).

**Measurement (per poll, per target).**
```sql
SELECT sum(pg_database_size(datname)) FROM pg_database;
```
→ logical bytes used for the instance. `ratio = used / capacity_bytes` (capacity from the source above).

- **Accuracy note:** `pg_database_size` excludes `pg_wal` and filesystem overhead, so it **under-reports physical PVC usage by ~5–10 %**. Measured on this incident: logical ≈ 9.3 GB vs. a 10 Gi PVC = **93 % logical while `df` read 100 %**. Thresholds carry enough margin that this is acceptable — a 70 % logical warn still fires with days of runway on a slowly-filling volume. A `df`-via-k8s-exec variant is exact but needs `pods/exec` RBAC and pod-name coupling → **opt-in follow-up, not v1**.
- **Reads survive a full disk** (only writes fail 53100), so the monitor keeps reporting during the very incident it exists to catch.

**Threshold state machine (per target).** Levels `OK / WARN / CRIT`.
- `ratio ≥ WARN_PCT (default 70)` → WARN; `ratio ≥ CRIT_PCT (default 90)` → CRIT.
- Alert (via A) only on **upward** transitions (`OK→WARN`, `WARN→CRIT`, `OK→CRIT`).
- While CRIT, **re-alert every `CRIT_REPEAT_SECONDS` (default 3600)**.
- On drop back to OK, send one `info` "recovered" notification.
- **Anti-flap:** clear a level only below a hysteresis margin (e.g. clear WARN under 67 %, CRIT under 87 %).

**Leader-gating.** The orchestrator runs `replicas: 2` on dev (HA). The poll loop MUST run on the **leader only**, reusing the existing active-passive singleton gate (ref: `docs/superpowers/specs/2026-06-24-orchestrator-m0-active-passive-design.md`), so replicas don't double-alert. Exact gate API to be pinned in the plan.

### Data flow

```
orchestrator (leader) startup
  └─ db_capacity_monitor loop (every POLL_SECONDS)
       └─ SELECT sum(pg_database_size)  →  ratio vs capacity
            └─ threshold state machine (hysteresis + repeat)
                 └─ critical_admin_alert(kind="db_capacity:main", severity=warning|critical, …)
                      └─ NotificationService.dispatch(priority, admins=list_admin_user_ids())
                           └─ email (live) + Cockpit SSE inbox   [+ Ntfy/Slack if later configured]
```

### Configuration (env, with defaults)

| Env | Default | Meaning |
|-----|---------|---------|
| `DB_CAPACITY_MONITOR_ENABLED` | `true` | Master on/off for the loop |
| `DB_CAPACITY_MAIN_SIZE` | = `databases.postgres.storageSize` | Capacity denominator as a chart-rendered K8s quantity (e.g. `16Gi`), parsed to bytes — adapts to deployment size. Optional explicit-bytes override per target. |
| `DB_CAPACITY_WARN_PCT` | `70` | Warn threshold |
| `DB_CAPACITY_CRIT_PCT` | `90` | Critical threshold |
| `DB_CAPACITY_POLL_SECONDS` | `300` | Poll interval |
| `DB_CAPACITY_CRIT_REPEAT_SECONDS` | `3600` | Re-alert cadence while CRIT |

Helm: add these env defaults to the orchestrator Deployment/values and wire `DB_CAPACITY_MAIN_SIZE` from `databases.postgres.storageSize` (single source of truth — the same value sizes the PVC). **No new services, CRDs, or RBAC.**

### Failure modes

- **Postgres unreachable:** poll query fails → log WARNING, skip cycle, retry next interval. Do not alert-storm. (Reachability is already covered by health/readiness.)
- **NotificationService uninitialized / no admins:** log error, no crash (handled in A).
- **Orchestrator restart:** in-memory hysteresis resets → at most one duplicate alert if still breached. Acceptable for v1; persisting last-level is YAGNI (documented).
- **Both replicas somehow active:** prevented by leader-gating; worst case is a duplicate email.

### Testing

- **Unit — threshold state machine:** injected sizes + fake clock; assert OK→WARN→CRIT→OK transitions, CRIT repeat timing, and anti-flap clear margins fire exactly the expected alerts.
- **Unit — `critical_admin_alert`:** dedup window + `severity→priority` mapping, against a mocked `NotificationService`; assert graceful no-op when uninitialized / no admins.
- **Integration (optional, light):** monitor against a throwaway test DB with a tiny `DB_CAPACITY_BYTES`; insert rows to cross a threshold; assert the alert helper is invoked. Marked optional for v1.
- `NotificationService` transports themselves are already covered; we don't re-test them.

## Fast-follow — reactive error propagation (gap 2, separate spec/plan)

At the swallow sites (`src/api/persistent_app.py:4533` and siblings in `persistent_app.py` / `src/api/orchestrator_client.py`), classify the caught exception: disk-full = asyncpg SQLSTATE `53100` or message contains `No space left on device`. Keep the write **non-fatal** (turn never crashes) but lift the severity: the agent reports it via the existing **heartbeat health channel** (extend the `AuxHealth` → `aux_degraded` plumbing with a `db_critical` flag), and the orchestrator turns that into `critical_admin_alert(...)`. The orchestrator also wraps its own critical DB writes directly (no round-trip). This reuses Component A end-to-end.

## Companion: checkpoint retention (prevents the bloat this incident exposed)

The capacity monitor *detects* a filling DB; it doesn't stop the specific bloat that caused the outage. Root filler: the LangGraph Postgres checkpointer writes a **full cumulative snapshot per super-step** and nothing prunes them — the deepest thread had **2368 checkpoints** (avg 465 across 113 threads), and because the `messages` channel (holding every screenshot) is re-serialized whole each step, old blob *versions* are the bulk of the weight.

**Validated retention policy — keep last N per thread (N=3):**
- `checkpoints` / `checkpoint_writes`: keep the newest N `checkpoint_id`s per `(thread_id, checkpoint_ns)` — `checkpoint_id` is a UUIDv6, so it sorts chronologically.
- `checkpoint_blobs`: keep the newest N `version`s per `(thread_id, checkpoint_ns, channel)` — `version` is a zero-padded monotonic counter (`0000…NNNN.0.<hash>`), lexically sortable.
- **Safe:** resume loads only the *latest* checkpoint; N=3 aligns exactly with "last 3 checkpoints" because LangGraph writes ≤1 version per channel per super-step, so the kept blobs always cover the kept checkpoints (no dangling refs).
- **Reclaim (measured 2026-07-23, dev):** `checkpoint_blobs` 8349 MB → keep-3 leaves **103 MB (98.8 % reclaimed)**; keep-1 would leave 39 MB.

**One-time cleanup:** applied to dev on 2026-07-23 (keep-3 across all three tables, autocommit per table), then `VACUUM FULL` to return space to the OS → **db 9269 MB → 191 MB, disk 100 % → 10 %**.

**Second one-time cleanup, 2026-07-27/28** (the recurrence — 16 Gi refilled in 4 days, killed job `e1192a9d`): PVC `srw-postgres-data` expanded 16→32 Gi first (online, no pod restart) because at 23 MB free a `VACUUM` cannot write WAL without risking a `PANIC`; then keep-**5** across all three tables → `DELETE 14511 / 9511 / 33500`; then `VACUUM FULL` (~40 s, run with `PGOPTIONS=-c lock_timeout=20000` so it fails fast instead of queuing behind the ACCESS EXCLUSIVE lock and blocking every reader). **Result: checkpoint_blobs 15 GB → 133 MB, db 15 GB → 225 MB, disk 100 % → 7 %.** Chart follow-up landed as `postgres.storageSize: 32Gi` in `values-experimental.yaml`.

Three corrections to the 07-23 assumptions, learned the hard way on the rerun:
- **The bloat is LIVE data, not dead tuples.** Plain `VACUUM` removed 2,548 dead TOAST tuples out of **6,741,172** and freed *zero* pages — these tables are insert-only. `VACUUM FULL` alone would also have reclaimed nothing. **DELETE is the only lever.** Do not estimate reclaimable space by comparing a `TABLESAMPLE` average blob size against the TOAST total; blob sizes span orders of magnitude so the sample is meaningless.
- **keep-5 costs ~7 MB more than keep-3** (124 MB vs 117 MB retained; only 78 rows differ, since most `(thread, channel)` groups have ≤3 versions). Prefer 5.
- **Verifying "no dangling refs" naively reports ~87 % false positives.** Only **7 channels are ever blob-backed** (`messages`, `todos`, `staged_todos`, `freeze_data`, `__start__`, `metadata`, `error`); the other ~30 (`iteration`, `job_id`, `turn_count`, `branch:to:*`, …) are scalars stored inline in the `checkpoint` JSONB, so comparing every `channel_versions` key against `checkpoint_blobs` flags them all. Correct test: a ref is genuinely orphaned only if its version is **older than `min(surviving version)`** for that `(thread, channel)` — that came back **0**.

**Autovacuum footnote:** it had *never* run on these tables (`autovacuum_count = 0`, `last_autovacuum` NULL) because the stats were empty (`n_live_tup = 0` despite 15k real rows), so it never crossed `threshold 50 + 0.2 × n_live_tup`. `ANALYZE` repopulated them; it triggers normally now.

**Durable mechanism (separate implementation slice):** a periodic **leader-gated retention pass** — natural home is the same orchestrator background service as the capacity monitor — that trims each thread to the newest N checkpoints. Alternative: trim-on-write (prune a thread's tail each time it checkpoints). Config: `CHECKPOINT_RETENTION_KEEP` (default 3), `CHECKPOINT_RETENTION_INTERVAL`. This is the *primary* defense against recurrence; the capacity monitor is the backstop for anything it misses (e.g. a different table bloating, or an external/managed DB).

## Open questions / assumptions

- **Channel:** email-only for now (only SMTP wired); adding Ntfy/Slack is env-only later. (Confirmed acceptable.)
- **Thresholds:** 70 / 90 defaults (user hinted "70 %"); fully configurable.
- **Leader-gate API** to reuse — pin exact call in the plan (ref M0 active-passive design).
- **Capacity source = chart `storageSize`** (resolved): Helm renders `databases.<db>.storageSize` → `DB_CAPACITY_<TARGET>_SIZE`; the monitor parses the K8s quantity to bytes and computes `%` against it, so it tracks whatever size a deployment configures (10Gi homelab or 500Gi enterprise). External/managed DBs use an operator override or fall back to reactive-only coverage. Pin the exact Helm wiring + quantity parser in the plan.

## References

- Incident memory: `srw_postgres_checkpoint_blobs_disk_full`, `srw_codex_stream_stall_no_timeout`.
- Notification service: `orchestrator/services/notification_service.py`, `orchestrator/services/webhook_transports.py`, `orchestrator/services/email.py`.
- Escalation pattern to copy (fast-follow): `src/services/auxiliary.py:803`; `docs/issues/surface_silent_aux_failures.md`.
- Leader-gating: `docs/superpowers/specs/2026-06-24-orchestrator-m0-active-passive-design.md`.
- The anticipated-but-unwired capacity alert hook: `orchestrator/services/container_provisioner.py:869-874`.
