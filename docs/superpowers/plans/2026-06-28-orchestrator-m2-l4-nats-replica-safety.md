# Orchestrator M2-L4 — NATS Replica-Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the two harmful NATS handlers (`sudo.request`, daemon `register`) safe under `replicas: 2`, so the orchestrator stops double-prompting for sudo and double-SSH-seeding VMs.

**Architecture:** Reuse M1's two shipped primitives, no queue groups. (1) `sudo.request` keeps NATS fan-out but `_insert_request` becomes an insert-as-claim on the request's unique NATS reply subject (new migration 0040 + `ON CONFLICT DO NOTHING`), so exactly one replica owns each request; the handler drops silently when it loses the claim and still denies on a genuine DB error. (2) daemon `register` keeps fan-out but the IDE-config SSH seed is leader-gated, so the leader (which always receives the broadcast) seeds exactly once while the leader-gated dispatch poke is unaffected.

**Tech Stack:** Python 3.12, asyncpg, Postgres 16, NATS (`nats-py`), pytest + `pytest-asyncio` + `testcontainers` `PostgresContainer`, Helm/Fleet.

**Spec:** `docs/superpowers/specs/2026-06-28-orchestrator-m2-l4-nats-replica-safety-design.md`.

## Global Constraints

- **Flattened imports in orchestrator code** — any new import inside `orchestrator/**` uses the sibling form (`from services.leader_election import is_leader`, `from database.X import …`), **never** `from orchestrator.services…`. The package-prefixed form passes unit tests but crashes the deployed flattened `/app` image (`ModuleNotFoundError: No module named 'orchestrator'` — the bug M1's k3d run caught).
- **No behaviour change at `replicas: 1`** — one subscriber, claim always wins, seed always runs.
- **No queue groups; `session.events` untouched** — it requires fan-out.
- **Tests:** `pytest` + `testcontainers.PostgresContainer("postgres:16")` for DB tests; mock-only for handler/gate tests. The repo `conftest.py` puts both repo-root and `orchestrator/` on `sys.path`; for shared module state (`is_leader`), import the **flattened** path so the test toggles the same object production reads.
- **PostgresContainer run recipe** (the `.venv` `pip` shebang is stale; testcontainers needs the podman socket + ryuk off):
  ```
  DOCKER_HOST="unix:///run/user/$(id -u)/podman/podman.sock" TESTCONTAINERS_RYUK_DISABLED=true \
    .venv/bin/python -m pytest <files> -v
  ```
  Mock-only tests run with plain `.venv/bin/python -m pytest <files> -v`.
- **Commit trailer (every commit):** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Commit to `develop`; **do not push**.
- **CI (Py3.12) is the gate**; local `pytest` env is noisy.

---

### Task 1: Migration 0040 + `_insert_request` becomes a claim

**Files:**
- Create: `orchestrator/database/migrations/app/0040_sudo_request_reply_subject_unique.sql`
- Modify: `orchestrator/services/sudo_gate.py:519-559` (`_insert_request`)
- Test: `tests/test_sudo_request_claim.py`

**Interfaces:**
- Produces: `SudoGateService._insert_request(job_id, vm_name, command, arguments, cwd, requesting_user, target_user, nats_reply_subject, metadata) -> Optional[str]` — returns the new request id on success, `None` when another replica already claimed (ON CONFLICT, no row), and **raises** on a genuine DB error (no longer swallows it).
- Consumes (later tasks): the same method + the `uq_sudo_request_reply_subject` partial unique index.

- [ ] **Step 1: Write the migration**

Create `orchestrator/database/migrations/app/0040_sudo_request_reply_subject_unique.sql`:

```sql
-- migration:     0040_sudo_request_reply_subject_unique.sql
-- description:   Partial unique index on sudo_approval_requests.nats_reply_subject
--                — the insert-as-claim dedup slot for fan-out NATS sudo requests
--                (HA / M2 Layer 4,
--                docs/superpowers/specs/2026-06-28-orchestrator-m2-l4-nats-replica-safety-design.md).
--
--                NATS sudo requests fan out to BOTH orchestrator replicas (no
--                queue group), so on_sudo_request runs twice -> two approval
--                rows + two prompts + two NATS replies per request. The request
--                carries a unique NATS reply inbox, identical across replicas,
--                so it is the natural claim key: _insert_request now INSERTs
--                with ON CONFLICT DO NOTHING on this index and only the winner
--                proceeds.
--
--                Pre-existing duplicates (one extra per request created under
--                replicas:2) sharing a nats_reply_subject are collapsed first
--                (keep the lowest id) so the index can build. They are redundant
--                request-log artifacts; sudo_approval_requests has no inbound FK
--                references. NULL reply subjects (the vm_upgrade path) are
--                excluded by the partial predicate and stay unconstrained.
-- depends-on:    0039_drop_per_phase_account_model_defaults.sql
-- expected:      < 200ms. sudo_approval_requests is small + low-traffic.
-- locks:         Brief SHARE lock on sudo_approval_requests for the non-concurrent
--                index build (blocks writes for the build only); covered by
--                lock_timeout. Acceptable on a small table.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Collapse pre-existing duplicates sharing a reply subject, keeping the lowest
-- id, so the partial unique index can build.
DELETE FROM sudo_approval_requests a
USING sudo_approval_requests b
WHERE a.nats_reply_subject IS NOT NULL
  AND a.nats_reply_subject = b.nats_reply_subject
  AND a.id > b.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sudo_request_reply_subject
    ON sudo_approval_requests (nats_reply_subject)
    WHERE nats_reply_subject IS NOT NULL;

COMMENT ON INDEX uq_sudo_request_reply_subject IS
    'At most one sudo_approval_requests row per NATS reply subject. The '
    'insert-as-claim dedup slot for fan-out NATS sudo requests (HA / M2-L4) — '
    'on_sudo_request claims it before acting so replicas:2 cannot double-insert '
    'or double-prompt. NULL reply subjects (vm_upgrade path) are unconstrained.';

COMMIT;
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_sudo_request_claim.py`:

```python
"""Atomic sudo-request claim (HA / M2-L4 NATS replica-safety).

NATS sudo requests fan out to BOTH orchestrator replicas (no queue group), so
on_sudo_request runs twice. _insert_request claims the request on its unique
NATS reply subject (migration 0040) so exactly one replica inserts the row and
acts. Mirrors tests/test_notify_dedup.py.
"""

import asyncio

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from database.postgres import PostgresDB
from services.sudo_gate import SudoGateService

JOB = "11111111-1111-1111-1111-111111111111"
REPLY = "_INBOX.aaaaaaaaaaaaaaaaaaaaaa"
REPLY2 = "_INBOX.bbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def gate(pg_dsn):
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        # Minimal sudo_approval_requests (no FK to jobs; status as plain text to
        # avoid the sudo_request_status enum) + the partial unique index from
        # migration 0040 (DDL kept identical to the migration).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sudo_approval_requests (
                id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                job_id             uuid NOT NULL,
                vm_name            varchar(255) NOT NULL,
                command            text NOT NULL,
                arguments          text[] DEFAULT '{}',
                working_directory  text,
                requesting_user    varchar(255) NOT NULL,
                target_user        varchar(255) NOT NULL DEFAULT 'root',
                status             text NOT NULL DEFAULT 'pending',
                requested_at       timestamptz NOT NULL DEFAULT now(),
                expires_at         timestamptz NOT NULL DEFAULT (now() + interval '300 seconds'),
                nats_reply_subject text,
                metadata           jsonb DEFAULT '{}'
            )
            """
        )
        await conn.execute("TRUNCATE sudo_approval_requests")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sudo_request_reply_subject "
            "ON sudo_approval_requests (nats_reply_subject) "
            "WHERE nats_reply_subject IS NOT NULL"
        )
    g = SudoGateService()
    g._db = d
    yield g
    await d.close()


async def _row_count(gate, reply: str) -> int:
    async with gate._db.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM sudo_approval_requests WHERE nats_reply_subject = $1",
            reply,
        )


async def _claim(gate, reply, user="agent"):
    return await gate._insert_request(
        job_id=JOB, vm_name="vm1", command="rm", arguments=["-rf", "/tmp/x"],
        cwd="/tmp", requesting_user=user, target_user="root",
        nats_reply_subject=reply, metadata={"k": "v"},
    )


@pytest.mark.asyncio
async def test_claim_atomic_exactly_one_wins(gate):
    r1, r2 = await asyncio.gather(_claim(gate, REPLY), _claim(gate, REPLY))
    won = [r for r in (r1, r2) if r is not None]
    lost = [r for r in (r1, r2) if r is None]
    assert len(won) == 1 and len(lost) == 1, "two replicas must not both insert"
    assert await _row_count(gate, REPLY) == 1


@pytest.mark.asyncio
async def test_distinct_replies_each_claim(gate):
    assert await _claim(gate, REPLY) is not None
    assert await _claim(gate, REPLY2) is not None


@pytest.mark.asyncio
async def test_null_reply_unconstrained(gate):
    # vm_upgrade-style requests carry no reply subject — both must insert
    # (NULLs are distinct under the partial index).
    assert await _claim(gate, None) is not None
    assert await _claim(gate, None) is not None


@pytest.mark.asyncio
async def test_db_error_raises_not_none(gate):
    # A genuine DB failure (NOT NULL violation on requesting_user) must RAISE so
    # the caller denies rather than silently dropping. Pre-change this returned
    # None (swallowed); the new contract raises. This is the red test.
    with pytest.raises(Exception):
        await _claim(gate, "_INBOX.cccccccccccc", user=None)
```

- [ ] **Step 3: Run the test, verify it fails**

Run:
```
DOCKER_HOST="unix:///run/user/$(id -u)/podman/podman.sock" TESTCONTAINERS_RYUK_DISABLED=true \
  .venv/bin/python -m pytest tests/test_sudo_request_claim.py -v
```
Expected: `test_db_error_raises_not_none` **FAILS** (current `_insert_request` swallows the exception and returns `None`, so `pytest.raises` sees no exception). The other three may pass incidentally (the unique index + the swallow give one-wins behaviour today) — they lock the behaviour going forward.

- [ ] **Step 4: Implement the claim**

Replace `_insert_request` in `orchestrator/services/sudo_gate.py` (currently lines 519-559) with:

```python
    async def _insert_request(
        self,
        job_id: str,
        vm_name: str,
        command: str,
        arguments: list,
        cwd: str,
        requesting_user: str,
        target_user: str,
        nats_reply_subject: Optional[str],
        metadata: dict,
    ) -> Optional[str]:
        """Claim-and-insert a sudo approval request.

        Returns the new request id on success, or None when another replica
        already claimed this request. The NATS sudo subject fans out to every
        replica (no queue group), so both run this; the unique reply subject is
        the per-request claim key (migration 0040 / uq_sudo_request_reply_subject).
        Raises on a genuine DB error so the caller can deny rather than silently
        drop — a None must mean "someone else owns it", never "the insert failed".
        """
        if not self._db:
            return None
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO sudo_approval_requests
                    (job_id, vm_name, command, arguments, working_directory,
                     requesting_user, target_user, nats_reply_subject, metadata,
                     expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                        NOW() + INTERVAL '300 seconds')
                ON CONFLICT (nats_reply_subject) WHERE nats_reply_subject IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                job_id,
                vm_name,
                command,
                arguments,
                cwd,
                requesting_user,
                target_user,
                nats_reply_subject,
                json.dumps(metadata),
            )
        return str(row["id"]) if row else None
```

(The change: add the `ON CONFLICT … DO NOTHING` arbiter clause, and drop the `try/except … return None` so DB errors propagate.)

- [ ] **Step 5: Run the test, verify it passes**

Run the Step 3 command. Expected: all 4 **PASS**.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/database/migrations/app/0040_sudo_request_reply_subject_unique.sql \
        orchestrator/services/sudo_gate.py tests/test_sudo_request_claim.py
git commit -m "feat(orchestrator): claim sudo requests on the NATS reply subject — no double-fire under replicas:2 (M2-L4)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire the claim into `on_sudo_request`

**Files:**
- Modify: `orchestrator/services/sudo_gate.py:83-100` (the insert + early-return block in `on_sudo_request`)
- Test: `tests/test_on_sudo_request_claim.py`

**Interfaces:**
- Consumes: `_insert_request` (Task 1) — `None` = lost claim, raises = DB error.
- Produces: `on_sudo_request(msg)` that drops silently on a lost claim, denies on a DB error, and otherwise proceeds unchanged (auto-rule eval / respond / store `_pending_msgs` + broadcast).

- [ ] **Step 1: Write the failing test**

Create `tests/test_on_sudo_request_claim.py`:

```python
"""on_sudo_request claim branching (HA / M2-L4).

The handler must: drop silently when it loses the claim (winner owns it), deny
when the insert genuinely errors (so the daemon doesn't hang), and otherwise
proceed as before. Mock-only — no Postgres.
"""

import json

import pytest
from unittest.mock import AsyncMock

from services.sudo_gate import SudoGateService


class FakeMsg:
    def __init__(self, payload: dict, reply):
        self.data = json.dumps(payload).encode()
        self.reply = reply
        self.responded = None

    async def respond(self, data: bytes):
        self.responded = data


def _payload():
    return {
        "job_id": "job-1", "vm_id": "vm1", "command": "ls", "argv": ["-la"],
        "user": "agent", "runas_user": "root", "cwd": "/",
    }


@pytest.mark.asyncio
async def test_drops_on_lost_claim():
    g = SudoGateService()
    g._insert_request = AsyncMock(return_value=None)      # lost the claim
    g._evaluate_auto_rules = AsyncMock()
    g._broadcast_sse = AsyncMock()
    g._nats_reply = AsyncMock()
    msg = FakeMsg(_payload(), reply="_INBOX.x")

    await g.on_sudo_request(msg)

    g._evaluate_auto_rules.assert_not_awaited()
    g._broadcast_sse.assert_not_awaited()
    g._nats_reply.assert_not_awaited()      # NOT a denial — the winner responds
    assert msg.responded is None
    assert "_INBOX.x" not in g._pending_msgs


@pytest.mark.asyncio
async def test_denies_on_db_error():
    g = SudoGateService()
    g._insert_request = AsyncMock(side_effect=RuntimeError("db down"))
    g._evaluate_auto_rules = AsyncMock()
    g._nats_reply = AsyncMock()
    msg = FakeMsg(_payload(), reply="_INBOX.y")

    await g.on_sudo_request(msg)

    g._nats_reply.assert_awaited_once()     # denied so the daemon doesn't hang
    assert g._nats_reply.await_args.args[1] is False     # approved=False
    g._evaluate_auto_rules.assert_not_awaited()


@pytest.mark.asyncio
async def test_winner_with_no_automatch_broadcasts():
    g = SudoGateService()
    g._insert_request = AsyncMock(return_value="req-1")   # won the claim
    g._evaluate_auto_rules = AsyncMock(return_value=None)  # no auto-rule match
    g._broadcast_sse = AsyncMock()
    msg = FakeMsg(_payload(), reply="_INBOX.z")

    await g.on_sudo_request(msg)

    g._broadcast_sse.assert_awaited_once()
    assert g._pending_msgs.get("req-1") is msg
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_on_sudo_request_claim.py -v`
Expected: `test_denies_on_db_error` **FAILS** — today a raising `_insert_request` is not caught inside `on_sudo_request`, so the exception propagates out of the handler instead of denying. (`test_drops_on_lost_claim` also fails today: `None` currently triggers a deny via `_nats_reply`, so `assert_not_awaited` fails.)

- [ ] **Step 3: Implement the branch**

In `orchestrator/services/sudo_gate.py`, replace the current insert + early-return block in `on_sudo_request` (lines 83-100):

```python
        # Store the request with the NATS reply subject.
        reply_subject = msg.reply if hasattr(msg, "reply") else None
        request_id = await self._insert_request(
            job_id=job_id,
            vm_name=vm_id,
            command=command,
            arguments=argv,
            cwd=cwd,
            requesting_user=user,
            target_user=runas_user,
            nats_reply_subject=reply_subject,
            metadata=data,
        )

        if not request_id:
            # DB insert failed — deny immediately.
            await self._nats_reply(reply_subject, False, "internal error")
            return
```

with:

```python
        # Store the request with the NATS reply subject. This is also the claim:
        # the NATS sudo subject fans out to every replica (no queue group), so
        # both run this handler; _insert_request claims the request on its unique
        # reply subject (migration 0040) and only the winner proceeds. (HA / M2-L4)
        reply_subject = msg.reply if hasattr(msg, "reply") else None
        try:
            request_id = await self._insert_request(
                job_id=job_id,
                vm_name=vm_id,
                command=command,
                arguments=argv,
                cwd=cwd,
                requesting_user=user,
                target_user=runas_user,
                nats_reply_subject=reply_subject,
                metadata=data,
            )
        except Exception as e:
            # Genuine DB failure — deny so the daemon doesn't hang.
            logger.error("Sudo request insert failed: %s", e)
            await self._nats_reply(reply_subject, False, "internal error")
            return

        if request_id is None:
            # Lost the claim to the other replica — it owns this request; drop
            # silently (do NOT deny: the winner responds).
            logger.debug("Sudo request already claimed by another replica; dropping")
            return
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest tests/test_on_sudo_request_claim.py -v`
Expected: all 3 **PASS**.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/sudo_gate.py tests/test_on_sudo_request_claim.py
git commit -m "feat(orchestrator): on_sudo_request drops on lost claim, denies on DB error (M2-L4)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Leader-gate the daemon-register IDE seed

**Files:**
- Modify: `orchestrator/services/nats_bridge.py:488-494` (the seed spawn in `_on_daemon_register`)
- Test: `tests/test_nats_register_seed_gate.py`

**Interfaces:**
- Consumes: `services.leader_election.is_leader` (the module-level `asyncio.Event` from M1).
- Produces: `_on_daemon_register` that spawns `_seed_vm_ide_config` only when this replica is the leader; the context upsert and the dispatch poke are unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_nats_register_seed_gate.py`:

```python
"""daemon-register IDE seed is leader-gated (HA / M2-L4).

agent.vm.*.register fans out to both replicas (no queue group). The IDE-config
SSH seed must run on exactly one — the leader (which always receives the
broadcast), so the leader-gated dispatch poke is unaffected. Mock-only.

Imports the FLATTENED is_leader (from services.leader_election) so the test
toggles the same Event _on_daemon_register reads — conftest puts orchestrator/
on sys.path, and the package-prefixed orchestrator.services.leader_election
would be a *different* module object.
"""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock

from services.leader_election import is_leader
from services.nats_bridge import NatsBridge


class FakeMsg:
    def __init__(self, payload: dict):
        self.data = json.dumps(payload).encode()


def _payload():
    return {"job_id": "job-1", "ip": "100.64.0.9", "hostname": "vm1", "pid": 42}


@pytest.fixture
def bridge():
    b = NatsBridge()
    b._db = None              # _set_vm_context no-ops when _db is None
    b._on_vm_ready = None     # skip the dispatch poke
    b._thread_vm_ids = set()  # job-1 takes the job (not thread) path
    return b


@pytest.mark.asyncio
async def test_seeds_on_leader(bridge):
    bridge._seed_vm_ide_config = AsyncMock()
    is_leader.set()
    try:
        await bridge._on_daemon_register(FakeMsg(_payload()))
        await asyncio.sleep(0)        # let the create_task run
    finally:
        is_leader.clear()
    bridge._seed_vm_ide_config.assert_called_once()


@pytest.mark.asyncio
async def test_skips_seed_on_follower(bridge):
    bridge._seed_vm_ide_config = AsyncMock()
    is_leader.clear()                 # follower
    await bridge._on_daemon_register(FakeMsg(_payload()))
    await asyncio.sleep(0)
    bridge._seed_vm_ide_config.assert_not_called()
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nats_register_seed_gate.py -v`
Expected: `test_skips_seed_on_follower` **FAILS** — today the seed is spawned unconditionally, so it runs even when `is_leader` is clear.

- [ ] **Step 3: Implement the gate**

In `orchestrator/services/nats_bridge.py`, find the seed spawn in `_on_daemon_register` (currently ~lines 488-494):

```python
            # Seed the owner-user's code-server config into the freshly-ready VM
            # (theme/keybindings/snippets). Fire-and-forget so the register
            # handler isn't blocked on SSH; the helper is best-effort.
            if ssh_host:
                asyncio.create_task(
                    self._seed_vm_ide_config(job_id, is_thread, ssh_host, ssh_port)
                )
```

Replace with:

```python
            # Seed the owner-user's code-server config into the freshly-ready VM
            # (theme/keybindings/snippets). Fire-and-forget so the register
            # handler isn't blocked on SSH; the helper is best-effort.
            #
            # agent.vm.*.register fans out to every replica (no queue group), so
            # gate the seed on leadership to run it exactly once — the leader
            # always receives the broadcast. A queue group would instead risk the
            # follower winning and the leader-gated dispatch poke below no-op'ing
            # (see the M2-L4 spec).
            from services.leader_election import is_leader  # flattened import (M1 lesson)
            if ssh_host and is_leader.is_set():
                asyncio.create_task(
                    self._seed_vm_ide_config(job_id, is_thread, ssh_host, ssh_port)
                )
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest tests/test_nats_register_seed_gate.py -v`
Expected: both **PASS**.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/nats_bridge.py tests/test_nats_register_seed_gate.py
git commit -m "feat(orchestrator): leader-gate the daemon-register IDE seed — no double SSH under replicas:2 (M2-L4)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Full-suite run + verification-record stub

**Files:**
- Create: `docs/tests/orchestrator_m2_l4_nats_replica_safety_verification.md`

- [ ] **Step 1: Run all three new test files together**

Run:
```
DOCKER_HOST="unix:///run/user/$(id -u)/podman/podman.sock" TESTCONTAINERS_RYUK_DISABLED=true \
  .venv/bin/python -m pytest tests/test_sudo_request_claim.py \
    tests/test_on_sudo_request_claim.py tests/test_nats_register_seed_gate.py -v
```
Expected: all green.

- [ ] **Step 2: Lint the touched files**

Run: `ruff check orchestrator/services/sudo_gate.py orchestrator/services/nats_bridge.py tests/test_sudo_request_claim.py tests/test_on_sudo_request_claim.py tests/test_nats_register_seed_gate.py`
Expected: clean (no unused-import / undefined-name). If `ruff` flags the in-function `is_leader` import, leave it — the flattened in-function import is deliberate (matches `_trigger_dispatch` at main.py:4279).

- [ ] **Step 3: Write the verification-record stub**

Create `docs/tests/orchestrator_m2_l4_nats_replica_safety_verification.md` recording: the unit results from Tasks 1-3, and the **still-owed** live checks from the spec's test plan (k3d two-replica: one SSH seed per register, one approval row/prompt per sudo, approve on the non-consuming replica reaches the daemon; then live dev under real traffic). Mark them pending until run — these are operator steps like M1's.

- [ ] **Step 4: Commit**

```bash
git add docs/tests/orchestrator_m2_l4_nats_replica_safety_verification.md
git commit -m "docs(ha): M2-L4 NATS replica-safety verification record (unit done; live pending)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Fix 1 (register seed leader-gate) → Task 3. ✓
- Fix 2 (sudo claim on `nats_reply_subject`, migration 0040, lost-claim-vs-DB-error split) → Tasks 1 (migration + `_insert_request` contract) + 2 (handler branch). ✓
- `session.events` / benign handlers untouched → no task modifies them (explicit non-goal). ✓
- Cross-replica approve unchanged → no task touches `_finalize_request`/`_nats_reply` (spec confirmed it already works). ✓
- Test plan (unit → k3d → dev) → unit in Tasks 1-3; k3d/dev recorded as pending in Task 4. ✓

**Placeholder scan:** No TBD/TODO; every code + test block is complete; commands have expected output. The verification stub (Task 4) intentionally records *future operator* steps as pending — that is the live-test handoff, not an implementation placeholder.

**Type/name consistency:** `_insert_request(...) -> Optional[str]` is defined in Task 1 and consumed identically in Task 2 (None = lost claim, raises = error). `is_leader` (Event) is produced by M1's `services.leader_election` and consumed flattened in Task 3 and the test. `_seed_vm_ide_config`, `_evaluate_auto_rules`, `_broadcast_sse`, `_pending_msgs`, `_nats_reply` match the live `sudo_gate.py` / `nats_bridge.py` members.

**Note for the executor:** the three production edits are independent — Task 3 does not depend on Tasks 1-2 — but keep the commit order (1 → 2 → 3 → 4) so the sudo migration and its consumer land together.
