# Orchestrator M1 — Leader Election Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `replicas: 2+` of the orchestrator safe by running the singleton background loops on exactly one elected replica, with the correctness-critical dispatch path additionally guarded at the database.

**Architecture:** A single session-scoped Postgres advisory lock = leadership, held on a dedicated reconnecting connection (modeled on `services/cloud/reload.py:run_listen_loop`). One `run_as_leader` task sets a module-level `is_leader` `asyncio.Event`; the ~9 singleton loops gate each tick on it. Because leader election has **no fencing** (two leaders can briefly coexist during a partition / Postgres failover), the dispatch path also gets an **atomic CAS claim** so a job can never be assigned twice. Postgres TCP-keepalives are tuned so a dead leader's lock releases in ~40s instead of ~2h.

**Tech Stack:** Python 3.12, `asyncpg` (direct pool, no pooler), FastAPI lifespan, Helm chart, pytest + `testcontainers` `PostgresContainer`.

**Spec / inputs:** `docs/features/orchestrator_ha_scaling.md` (Layer 1 + Phase 1) and `docs/researches/orchestrator_leader_election.md`. This plan IS M1.

## Global Constraints

- **Session-scoped `pg_advisory_lock` / `pg_try_advisory_lock`** (held for the loop's life) — NOT `pg_advisory_xact_lock`. All existing repo locks are xact-scoped; this is the new pattern.
- **Single leadership lock, dedicated connection** — never per-loop (a session lock pins a connection for life; the pool `max_size` is 10).
- **Direct Postgres only** — session locks break behind a transaction-mode pooler. Verified: SRW uses direct asyncpg. The external-Postgres path must be guarded (Task 4).
- **Leader election is efficiency, not correctness** — anything correctness-critical (job dispatch) is guarded at the DB via CAS, not by the lock.
- **Lock-ID style:** packed-ASCII `int64` constants, deliberately distinct (existing: `LOCK_ID = 0x5352575F4D4947` "SRW_MIG", `MAINT_LOCK_ID = 0x5352575F41554454` "SRW_AUDT").
- **Stays `replicas: 1`.** M1 makes 2 *safe*; flipping the count is M4/Phase 5.
- **Tests:** pytest + `testcontainers.PostgresContainer("postgres:16")` (base pattern: `tests/test_audit_store.py`). Local pytest is env-noisy — CI (Py3.12) is the gate.
- **Commit trailer:** every commit ends with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Commit to `develop`; do not push.
- **Scope cut-point:** Tasks 1-4 = the required-correctness M1 (makes `replicas: 2` safe). Task 5 = defense-in-depth loop hardening (includable or fast-follow). Task 6 = verification + docs.

---

### Task 1: Leader-election module + lock-ID registry

**Files:**
- Create: `orchestrator/database/lock_ids.py`
- Create: `orchestrator/services/leader_election.py`
- Test: `tests/test_leader_election.py`

**Interfaces:**
- Produces: `lock_ids.LEADER_ID: int`; `leader_election.is_leader: asyncio.Event` (set iff this replica holds leadership); `leader_election.run_as_leader(db, lock_id, shutdown_event) -> None`.

- [ ] **Step 1: Create the lock-ID registry**

Create `orchestrator/database/lock_ids.py`:
```python
"""Central registry of Postgres advisory-lock IDs (packed-ASCII int64).

Keep every advisory-lock key here so they never collide. Existing keys:
  LOCK_ID       = 0x5352575F4D4947  # "SRW_MIG"  (migrate.py — xact-scoped)
  MAINT_LOCK_ID = 0x5352575F41554454 # "SRW_AUDT" (audit_partitions.py — xact)
"""

# Session-scoped leadership lock (orchestrator/services/leader_election.py).
# "SRW_LEAD" packed into int64. Distinct from LOCK_ID / MAINT_LOCK_ID.
LEADER_ID = 0x5352575F4C454144
```

- [ ] **Step 2: Write the failing test (exactly-one-leader + failover)**

Create `tests/test_leader_election.py` (mirrors the `PostgresContainer` setup in `tests/test_audit_store.py`):
```python
import asyncio
import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer
from orchestrator.database.lock_ids import LEADER_ID

@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        # asyncpg wants postgresql:// (testcontainers yields postgresql+psycopg2://)
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")

@pytest.mark.asyncio
async def test_only_one_leader_then_failover(pg_dsn):
    c1 = await asyncpg.connect(pg_dsn)
    c2 = await asyncpg.connect(pg_dsn)
    try:
        assert await c1.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_ID) is True
        assert await c2.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_ID) is False
        await c1.close()  # leader "dies" — session ends, lock auto-releases
        acquired = False
        for _ in range(50):
            if await c2.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_ID):
                acquired = True
                break
            await asyncio.sleep(0.1)
        assert acquired, "follower never acquired after leader died"
    finally:
        await c2.close()
```

- [ ] **Step 3: Run it to verify it fails**

Run: `pytest tests/test_leader_election.py -v`
Expected: FAIL at import — `ModuleNotFoundError`/`ImportError: cannot import name 'LEADER_ID'` until Step 1 is saved; once Step 1 exists this test should PASS (it exercises raw Postgres semantics, proving the failover model before we wrap it). If it fails after Step 1, the keepalive/connection model is wrong — stop and investigate.

- [ ] **Step 4: Implement `run_as_leader`**

Create `orchestrator/services/leader_election.py`:
```python
"""Single-leader election via a session-scoped Postgres advisory lock.

One `run_as_leader` task per replica holds `LEADER_ID` on a dedicated pooled
connection. While held, the module `is_leader` event is set and the singleton
background loops run their work; otherwise they idle. On the leader's death the
Postgres session ends and the lock auto-releases, so a follower takes over
within ~one poll interval (failover speed also depends on Postgres TCP
keepalives — see Task 4). Pattern mirrors services/cloud/reload.py.
"""
import asyncio
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 10.0
_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 30.0

# Set iff THIS replica currently holds leadership. Singleton loops gate on it.
is_leader = asyncio.Event()


async def _sleep_or_shutdown(seconds: float, shutdown_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


async def run_as_leader(db: Any, lock_id: int, shutdown_event: asyncio.Event) -> None:
    backoff = _RECONNECT_MIN
    await _sleep_or_shutdown(random.uniform(0.0, 3.0), shutdown_event)  # anti-thundering-herd
    while not shutdown_event.is_set():
        conn = None
        try:
            if db._pool is None:
                await _sleep_or_shutdown(backoff, shutdown_event)
                backoff = min(backoff * 2, _RECONNECT_MAX)
                continue
            conn = await db._pool.acquire()
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_id)
            backoff = _RECONNECT_MIN
            if got:
                logger.info("leader_election: acquired leadership (lock %s)", lock_id)
                is_leader.set()
                try:
                    while not shutdown_event.is_set():
                        await _sleep_or_shutdown(_POLL_INTERVAL, shutdown_event)
                        await conn.fetchval("SELECT 1")  # keep-warm + dead-conn detect
                finally:
                    is_leader.clear()
                    try:
                        await conn.execute("SELECT pg_advisory_unlock($1)", lock_id)
                    except Exception:
                        pass
                    logger.info("leader_election: released leadership")
            else:
                is_leader.clear()
                await _sleep_or_shutdown(_POLL_INTERVAL, shutdown_event)
        except asyncio.CancelledError:
            is_leader.clear()
            raise
        except Exception as e:
            is_leader.clear()
            logger.warning("leader_election: connection lost (%s); stepping down", e)
        finally:
            if conn is not None:
                try:
                    await db._pool.release(conn)
                except Exception:
                    pass
        if shutdown_event.is_set():
            break
        await _sleep_or_shutdown(backoff, shutdown_event)
        backoff = min(backoff * 2, _RECONNECT_MAX)
    is_leader.clear()
    logger.info("leader_election: shutdown")
```

- [ ] **Step 5: Run tests + lint**

Run: `pytest tests/test_leader_election.py -v` → Expected: PASS.
Run: `ruff check orchestrator/services/leader_election.py orchestrator/database/lock_ids.py` → Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/database/lock_ids.py orchestrator/services/leader_election.py tests/test_leader_election.py
git commit -m "feat(orchestrator): session-scoped leader-election primitive (M1)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire leadership into lifespan + gate the singleton loops

**Files:**
- Modify: `orchestrator/main.py` (lifespan registration ~`:5314`; the 9 singleton loop bodies — see list)

**Interfaces:**
- Consumes: `leader_election.run_as_leader`, `leader_election.is_leader`, `lock_ids.LEADER_ID`.

- [ ] **Step 1: Start the leader task in lifespan**

In `orchestrator/main.py`, just after `_shutdown_event = asyncio.Event()` (`:5315`), add:
```python
    from orchestrator.services.leader_election import run_as_leader
    from orchestrator.database.lock_ids import LEADER_ID
    leader_task = asyncio.create_task(run_as_leader(postgres_db, LEADER_ID, _shutdown_event))
```
Add `leader_task` to the shutdown-await list alongside the other tasks (find the `await asyncio.gather(...)`/cancel block in the lifespan shutdown path and include it).

- [ ] **Step 2: Add the leadership gate to each singleton loop**

At the top of `orchestrator/services/leader_election.py` consumers, import `is_leader`. In **each** singleton loop below, insert this guard as the **first statement inside the `while not shutdown_event.is_set():` body**, before any work:
```python
        if not is_leader.is_set():
            # not the leader this tick — idle until we are (or shutdown)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=<EXISTING_LOOP_INTERVAL>)
            except asyncio.TimeoutError:
                pass
            continue
```
Use each loop's existing tick interval for `<EXISTING_LOOP_INTERVAL>`. Gate exactly these (singleton, correctness/duplication-prone):

| Loop | `main.py` def | Interval |
|---|---|---|
| `auto_assign_dispatcher` | `:4194` | 30s (also gate `_trigger_dispatch` — Step 3) |
| `imap_poll_loop` | `:986` | `IMAP_POLL_INTERVAL` (30s) |
| `delegation_timeout_sweeper` | `:9082` | 60s |
| `quiet_hours_digest_loop` | `:938` | 300s |
| `stale_agent_detector` | `:570` | 60s |
| `agent_pool_reconciler` | `:678` | 60s |
| `lifecycle_reconciler_loop` | `:707` | 60s |
| `quota_poll_loop` | `:1381` | 120s |
| `thread_permission_notify_sweeper` | `:17122` | 30s |

Do **NOT** gate (run on every replica by design): `cron_dispatcher_loop`, `project_loop_sweeper_loop`, audit `maintenance_loop`, `workspace_metering_loop`, `llm_usage_poll_loop`, `main_cloud_listen_task`. The idempotent sweepers (`sudo_expiration_sweeper`, `thread_events_prune_sweeper`, `security_events_prune_sweeper`, `ide_session_ttl_sweeper`, `workspace_idle_sweeper`, `code_server_settings_sweeper`, `snapshot_gc_sweeper`, `attention_sleep_sweeper`, `cleanup_expired_tokens`, `cleanup_expired_sessions`) may stay ungated for M1 (idempotent; gating is optional efficiency).

- [ ] **Step 3: Gate the event-driven dispatch trigger**

`_trigger_dispatch` (event-driven dispatch poke) must also no-op on a non-leader. At the top of `_trigger_dispatch` add:
```python
    from orchestrator.services.leader_election import is_leader
    if not is_leader.is_set():
        return
```

- [ ] **Step 4: Verify it imports + the gate is syntactically sound**

Run: `python -c "import ast; ast.parse(open('orchestrator/main.py').read())"` → Expected: no error.
Run: `ruff check orchestrator/main.py` → Expected: clean (no unused-import / undefined-name).
(Behavioral verification is the two-replica test in Task 6 — lifespan can't be meaningfully unit-tested.)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py
git commit -m "feat(orchestrator): run singleton loops only on the elected leader (M1)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Dispatcher correctness — atomic CAS claim

**Files:**
- Modify: `orchestrator/database/postgres.py` (new `claim_job_for_agent`)
- Modify: `orchestrator/main.py` (`_try_dispatch_pending_jobs` assignment point, ~`:3822`)
- Test: `tests/test_job_claim.py`

**Interfaces:**
- Produces: `PostgresDB.claim_job_for_agent(job_id: str, agent_id: str) -> bool` (True iff this caller won the claim).

- [ ] **Step 1: Write the failing test (two concurrent claims, exactly one wins)**

Create `tests/test_job_claim.py` (uses the project's real migration runner against a `PostgresContainer`, mirroring `tests/test_audit_store.py`'s fixture — reuse its DB-setup helper to create the schema, then):
```python
@pytest.mark.asyncio
async def test_claim_job_is_atomic(db):  # db: connected PostgresDB on a fresh test DB
    job_id = await _insert_created_job(db)  # status='created', assigned_agent_id NULL
    r1, r2 = await asyncio.gather(
        db.claim_job_for_agent(job_id, "agent-1"),
        db.claim_job_for_agent(job_id, "agent-2"),
    )
    assert sorted([r1, r2]) == [False, True]            # exactly one wins
    row = await db.get_job(job_id)
    assert row["status"] == "processing"
    assert row["assigned_agent_id"] in ("agent-1", "agent-2")
    # a second claim of an already-claimed job fails
    assert await db.claim_job_for_agent(job_id, "agent-3") is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_job_claim.py -v` → Expected: FAIL (`AttributeError: 'PostgresDB' object has no attribute 'claim_job_for_agent'`).

- [ ] **Step 3: Implement the CAS claim**

In `orchestrator/database/postgres.py`, add (near `get_dispatchable_jobs`, ~`:2569`):
```python
    async def claim_job_for_agent(self, job_id: str, agent_id: str) -> bool:
        """Atomically claim a dispatchable job for an agent.

        Returns True iff THIS call won the claim. The CAS predicate
        (`assigned_agent_id IS NULL AND status IN ('created','paused')`) makes
        the assignment safe against concurrent dispatchers and the transient
        dual-leader window — a job can never be handed to two agents.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jobs
                   SET status = 'processing',
                       assigned_agent_id = $2,
                       updated_at = now()
                 WHERE id = $1
                   AND assigned_agent_id IS NULL
                   AND status IN ('created', 'paused')
                RETURNING id
                """,
                job_id,
                agent_id,
            )
            return row is not None
```

- [ ] **Step 4: Use the claim at the dispatch assignment point**

In `_try_dispatch_pending_jobs` (`orchestrator/main.py:3822`), find where a matched job is assigned to an agent (the `update_job_status(..., status="processing", assigned_agent_id=...)` call in the assignment phase). Replace the "mark assigned then notify the agent" sequence with **claim-first**:
```python
        claimed = await postgres_db.claim_job_for_agent(job_id, agent.id)
        if not claimed:
            # another dispatcher/leader already took this job — skip it
            logger.debug("Dispatcher: job %s already claimed; skipping", job_id)
            continue
        # ... only now notify/dispatch to the agent (HTTP POST) ...
```
**Order matters:** claim BEFORE notifying the agent, so we never tell an agent to start a job we didn't win. Leave the in-process `_dispatch_lock` in place (harmless; it serializes the single leader's own loop). Do not change the generic `update_job_status`.

- [ ] **Step 5: Run tests + lint**

Run: `pytest tests/test_job_claim.py -v` → Expected: PASS.
Run: `ruff check orchestrator/database/postgres.py orchestrator/main.py` → Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/database/postgres.py orchestrator/main.py tests/test_job_claim.py
git commit -m "feat(orchestrator): atomic CAS job claim — no double-assign under dual-leader (M1)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Postgres keepalive tuning + external-pooler guard

**Files:**
- Modify: `helm/templates/databases/postgres.yaml` (container args)
- Modify: `helm/values.yaml` + `helm/values.example.yaml` (warning comment)

- [ ] **Step 1: Add TCP-keepalive args to the Postgres container**

In `helm/templates/databases/postgres.yaml`, add to the `postgres` container so a dead client backend (and thus a dead leader's session lock) is detected in ~40s instead of the ~2h OS default:
```yaml
          args:
            - "-c"
            - "tcp_keepalives_idle=10"
            - "-c"
            - "tcp_keepalives_interval=10"
            - "-c"
            - "tcp_keepalives_count=3"
```
(Server-side keepalives only affect dead-connection detection — no risk to live sessions. `idle_session_timeout` is intentionally NOT set: it's global and would reap idle pool connections; the leader's 10s `SELECT 1` keeps its own session alive regardless.)

- [ ] **Step 2: Document the external-pooler constraint**

In `helm/values.yaml`, above the `databases.postgres.externalHost` block, add:
```yaml
    # -- IMPORTANT (HA): orchestrator leader election uses a SESSION-scoped
    # advisory lock, which breaks behind a TRANSACTION-mode connection pooler
    # (PgBouncer/pgcat/RDS-Proxy txn mode). Point externalHost at a direct or
    # session-pooled Postgres endpoint. See docs/researches/orchestrator_leader_election.md.
```
Mirror the warning in `helm/values.example.yaml` near its `databases.postgres` block.

- [ ] **Step 3: Verify the render**

Run:
```bash
helm template srw helm/ -f helm/ci/test-values.yaml --show-only templates/databases/postgres.yaml | grep -E "tcp_keepalives_idle|tcp_keepalives_interval|tcp_keepalives_count"
helm lint helm/ -f helm/ci/test-values.yaml
```
Expected: the three keepalive args render; lint reports `0 chart(s) failed`.

- [ ] **Step 4: Commit**

```bash
git add helm/templates/databases/postgres.yaml helm/values.yaml helm/values.example.yaml
git commit -m "feat(helm): tune Postgres TCP keepalives for ~40s leader failover + pooler warning (M1)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Defense-in-depth — harden the other non-idempotent loops (CUT-POINT)

> This task closes the *user-visible-duplicate* footguns that survive the rare dual-leader window. It is **separable** — if you want a leaner first M1, ship Tasks 1-4 + 6 and do this as a fast-follow. Leader election already makes these single in steady state; these guards only matter during a partition / Postgres failover.

**Files:** `orchestrator/database/migrations/app/<next>_imap_dedup_unique.sql` (new migration); `orchestrator/services/imap_poller.py`; `orchestrator/main.py` (`_check_delegation_timeouts`, digest, notify sweeper).

- [ ] **Step 1: IMAP dedup — make it a real unique constraint**

New migration adding `CREATE UNIQUE INDEX CONCURRENTLY ... ON message_log (email_message_id) WHERE email_message_id IS NOT NULL;` (today it's a non-unique partial index). In `imap_poller.py:_process_email`, change the insert to `INSERT ... ON CONFLICT (email_message_id) DO NOTHING` and treat "no row inserted" as "already processed → skip the reply handler".
- [ ] **Step 2:** Test: two concurrent `_process_email` of the same `email_message_id` → exactly one routes; assert via `PostgresContainer`.
- [ ] **Step 3: Delegation resume CAS** — in `_check_delegation_timeouts` (`main.py:8939`), guard the parent-resume with a status CAS (`UPDATE ... WHERE status='waiting' RETURNING ...`) and only resume if a row was returned. Test concurrent double-resume → one wins.
- [ ] **Step 4: Digest / notify marker-before-send** — for `quiet_hours_digest_loop` and `thread_permission_notify_sweeper`, insert the "sent" marker row (unique on the natural key) in the same transaction *before* the email send, and skip the send on conflict. Test concurrent double-send → one sends.
- [ ] **Step 5: Commit**
```bash
git add orchestrator/database/migrations/app/ orchestrator/services/imap_poller.py orchestrator/main.py tests/
git commit -m "feat(orchestrator): DB-guard imap/delegation/digest/notify against dual-leader duplicates (M1)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Two-replica verification + docs

**Files:** `docs/features/orchestrator_ha_scaling.md` (Phase 1 checkboxes); `docs/tests/orchestrator_m1_leader_election_verification.md` (new).

- [ ] **Step 1: Manual two-replica test on local k3d** (per `local_tilt_dev_stack_stinkpad` memory): set `orchestrator.replicas: 2` + `orchestrator.pdb.minAvailable: 1` in `deployment/values-tilt.yaml`; `tilt trigger srw` (note: full re-render). Confirm: exactly one pod logs `acquired leadership`; `kubectl delete pod <leader>`; the survivor logs `acquired leadership` within ~poll interval and the dispatcher/loops resume. Submit a job during the window → it is dispatched exactly once.
- [ ] **Step 2: Write `docs/tests/orchestrator_m1_leader_election_verification.md`** recording the unit-test results (Tasks 1/3) + the manual two-replica result, and what remains for the live cluster.
- [ ] **Step 3: Update `orchestrator_ha_scaling.md`** — tick the Phase 1 checkboxes for what landed; flip the reality-check matrix M1 row to "SHIPPED (replicas:2 safe)".
- [ ] **Step 4: Commit**
```bash
git add docs/
git commit -m "docs(ha): M1 leader-election verification + mark Phase 1 shipped" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** single leadership lock (Task 1), dedicated reconnecting connection (Task 1, modeled on reload.py), gate the 9 singleton loops + leave HA-safe ones (Task 2), dispatcher CAS so the dual-leader window can't double-assign (Task 3), keepalive tuning + external-pooler guard (Task 4), other-loop hardening (Task 5), verification + docs (Task 6). All Phase-1 checklist items map to a task.

**Placeholder scan:** new code (lock_ids.py, leader_election.py, claim_job_for_agent, both unit tests, keepalive args) is shown in full. The two MODIFY tasks (lifespan gate in Task 2, dispatch claim site in Task 3) give the exact insertion code + a precise anchor (`main.py:3822` / `:5315` / the 9 loop line refs) — the executor reads the surrounding live code, which is unavoidable for surgical edits into large existing functions; this is flagged, not a vague "handle it". Task 5 is intentionally lighter-grained and marked a cut-point.

**Type/name consistency:** `LEADER_ID` (lock_ids.py) consumed by `run_as_leader` (Task 1) and lifespan (Task 2); `is_leader` event produced by leader_election (Task 1), consumed by the loop gates + `_trigger_dispatch` (Task 2); `claim_job_for_agent(job_id, agent_id) -> bool` defined (Task 3 Step 3) and called identically (Task 3 Step 4) and tested (Task 3 Step 1). Consistent throughout.

**One known risk to confirm at execution:** `tests/test_audit_store.py`'s fixture is the assumed base for the `PostgresContainer` + real-migration setup in Tasks 1/3/5 — confirm its helper is importable/reusable (or lift the fixture into a shared `conftest.py`) when writing the first test.
