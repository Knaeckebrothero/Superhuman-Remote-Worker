# Session Rewind Implementation Plan

> **EXECUTED 2026-08-07 — archived.** All 10 tasks completed via
> subagent-driven development on `develop` (14 feature commits `59e5185f..2622a11e`
> interleaved with unrelated work, + 5 final-review fix-wave commits
> `00060bce..f0c636dd`, + k3d gate docs `e8c65586`). Every task passed a
> spec+quality review; the final whole-branch review's 1 Critical + 3 Important
> findings were fixed and re-review-verified. Live-gated on local k3d the same
> day (see `session_rewind.md` §Live gate — k3d results). Two owner-approved
> deviations from this plan's literal text: validation precedes the interrupt
> (Task 5), and the detached 409 fires only on live agent bindings (Task 7).
> This document is the frozen execution record.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a session owner pick an earlier user message and revert the session to just before it — conversation, workspace files, or both — plus a "summarize up to here" action, per the decided design in `docs/features/session_rewind.md` (§Decided design, 2026-08-07).

**Architecture:** Linear tombstones (`thread_messages.rewound_at` marker) + a `thread_rewinds` ledger + a `thread_turn_commits` turn→commit map fed by the existing per-turn auto-commits. Attached sessions rewind via a new `rewind` WS verb (interrupt → git forward-restore → DB sweep → in-memory truncate → events-epoch bump); detached sessions via an owner-only orchestrator REST endpoint (conversation mode only). Workspace restore is always forward commits (`read-tree -u --reset` + commit), never `reset --hard`, so the Gitea push stays fast-forward.

**Tech Stack:** Python 3.12 (FastAPI, asyncpg, LangChain messages), PostgreSQL (app DB), git via `GitManager._run_git` (SSH-backend aware), Angular cockpit (signals, transloco, IndexedDB via `indexed-db.service`).

## Global Constraints

- **CI (Python 3.12) is the test gate.** Local pytest on 3.14 is noisy with unrelated failures — run the targeted test files locally, trust CI for the full suite.
- **Ruff runs on push.** Before every commit: `ruff format <changed .py files> && ruff check <changed .py files>`.
- **Commit per task on `develop`. NEVER `git push` — the user pushes explicitly.** No sub-branches.
- **Migration numbers:** this plan uses `0110`/`0111`. Duplicate prefixes hard-fail the runner at boot — re-check `ls orchestrator/database/migrations/app/ | tail -3` immediately before writing the files and renumber if someone landed `0110` first.
- **asyncpg returns JSONB columns as raw JSON strings** — `json.loads` before dict access (existing house pattern; the new primitives below avoid JSONB reads entirely).
- **WS control frames are FLAT** — `{method: 'rewind', message_id: …}`, never `{method, params}`. `params` exists only on the inbound (server→client) direction.
- **Server→client acks must NOT carry `params._seq`** — the cockpit control-WS `onmessage` filter (`persistent-chat.service.ts:1908-1910`) drops any frame with `_seq` as an SSE duplicate. Use `_ws_send` (direct ack) for the initiator; `_broadcast` (journaled, carries `_seq`, arrives via SSE) for all-viewer signals.
- **Grep trap:** orchestrator `get_thread_messages` / MCP `get_message_thread` read the `message_log` table (job messaging), NOT `thread_messages`. Do not "fix" them.
- **Do not add columns to the two `save_thread_message` upserts' SET lists** — `rewound_at` must survive the turn-complete re-save (`ON CONFLICT (id) DO UPDATE` on the agent side only updates the columns it already names; leaving `rewound_at` out is what makes a swept row stay swept).

---

### Task 1: Migration — `rewound_at`, `thread_rewinds`, `thread_turn_commits`

**Files:**
- Create: `orchestrator/database/migrations/app/0110_session_rewind_foundations.sql`
- Create: `orchestrator/database/migrations/app/0111_thread_messages_live_index.notx.sql`
- Modify (regenerated): `orchestrator/database/schema_current.sql`

**Interfaces:**
- Produces: columns/tables every later task's SQL uses verbatim: `thread_messages.rewound_at TIMESTAMPTZ NULL`; `thread_rewinds(id, thread_id, from_seq, mode, actor, swept_count, abandoned_sha, restored_to_sha, restore_commit_sha, created_at)`; `thread_turn_commits(thread_id, seq, commit_sha, created_at)` PK `(thread_id, seq)`.

- [ ] **Step 1: Re-verify the free migration numbers**

Run: `ls orchestrator/database/migrations/app/ | tail -3`
Expected: `0109_compute_exact_epoch_lifecycle.sql` is still the tail. If not, renumber both new files to the next free numbers and use those numbers everywhere below.

- [ ] **Step 2: Write `0110_session_rewind_foundations.sql`**

```sql
-- migration:     0110_session_rewind_foundations.sql
-- description:   Session rewind (docs/features/session_rewind.md, decided
--                2026-08-07): tombstone marker on thread_messages, the
--                thread_rewinds audit ledger, and the thread_turn_commits
--                turn→workspace-commit map. Nothing is ever deleted: a rewind
--                stamps rewound_at on the abandoned tail and live readers
--                filter on rewound_at IS NULL.
-- depends-on:    0109_compute_exact_epoch_lifecycle.sql
-- expected:      < 5s. ADD COLUMN is nullable/no-default (catalog-only);
--                both CREATE TABLEs are new.
-- locks:         Brief ACCESS EXCLUSIVE on thread_messages for the ADD COLUMN.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE thread_messages
    ADD COLUMN rewound_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN thread_messages.rewound_at IS
    'Set when a session rewind supersedes this row (seq >= the rewind''s '
    'from_seq). Live conversation readers filter rewound_at IS NULL; the row '
    'itself is never deleted. See docs/features/session_rewind.md.';

CREATE TABLE thread_rewinds (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    thread_id          UUID NOT NULL,
    from_seq           BIGINT NOT NULL,
    mode               TEXT NOT NULL CHECK (mode IN ('both', 'conversation', 'code')),
    actor              TEXT,
    swept_count        INTEGER NOT NULL DEFAULT 0,
    abandoned_sha      TEXT,
    restored_to_sha    TEXT,
    restore_commit_sha TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE thread_rewinds IS
    'One row per session rewind: the audit trail, un-tombstone metadata, and '
    'the workspace SHAs of the forward-restore. Append-only.';

CREATE INDEX idx_thread_rewinds_thread
    ON thread_rewinds (thread_id, created_at DESC);

CREATE TABLE thread_turn_commits (
    thread_id  UUID NOT NULL,
    seq        BIGINT NOT NULL,
    commit_sha TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, seq)
);

COMMENT ON TABLE thread_turn_commits IS
    'Workspace state after transcript seq <= N: written right after each '
    'per-turn auto-commit / compaction checkpoint commit succeeds. The '
    'restore target for a rewind to seq S is the row with the largest '
    'seq < S. seq 0 = the pre-first-message workspace.';

COMMIT;
```

- [ ] **Step 3: Write `0111_thread_messages_live_index.notx.sql`**

The `.notx.sql` suffix is the house convention for `CONCURRENTLY` files that must run outside a transaction (precedent: `0020_thread_messages_window_index.notx.sql`).

```sql
-- migration:     0111_thread_messages_live_index.notx.sql
-- description:   Partial index matching the live-reader shape introduced in
--                0110: every conversation read now filters
--                rewound_at IS NULL, and rewound rows are expected to stay a
--                small minority of the table.
-- depends-on:    0110_session_rewind_foundations.sql
-- expected:      Minutes on a large thread_messages; CONCURRENTLY, no lock.
-- locks:         None (CONCURRENTLY).
-- transactional: NO (.notx — CREATE INDEX CONCURRENTLY cannot run in a txn)

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_thread_messages_thread_seq_live
    ON thread_messages (thread_id, seq)
    WHERE rewound_at IS NULL;
```

- [ ] **Step 4: Regenerate the schema snapshot artifacts**

Run: `scripts/schema-snapshot.sh` (starts a throwaway podman postgres, replays every migration from zero, dumps). Requires podman running locally.
Expected: `orchestrator/database/schema_current.sql` diff shows `rewound_at`, both new tables, and both new indexes — nothing else.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/database/migrations/app/0110_session_rewind_foundations.sql \
        orchestrator/database/migrations/app/0111_thread_messages_live_index.notx.sql \
        orchestrator/database/schema_current.sql
git commit -m "feat(rewind): migration 0110/0111 — rewound_at tombstone, thread_rewinds ledger, thread_turn_commits map"
```

---

### Task 2: Agent-side DB primitives + live-read filters

**Files:**
- Modify: `src/database/postgres_db.py` (new methods after `get_latest_compaction_checkpoint`, which ends near line 487; filter edits at lines 397-402, 464-471, 503)
- Test: `tests/test_rewind_db.py` (create)

**Interfaces:**
- Consumes: Task 1 DDL.
- Produces (exact signatures Task 4/5 call):
  - `async def get_live_message(self, thread_id: str, msg_id: str) -> Optional[Dict[str, Any]]` → `{"seq": int, "role": str, "content": str}` or `None`
  - `async def apply_rewind(self, thread_id: str, from_seq: int, mode: str, actor: Optional[str] = None, abandoned_sha: Optional[str] = None, restored_to_sha: Optional[str] = None, restore_commit_sha: Optional[str] = None) -> Dict[str, Any]` → `{"rewind_id": str, "swept": int, "surviving_turn": int}`
  - `async def record_turn_commit(self, thread_id: str, commit_sha: str) -> None`
  - `async def resolve_restore_commit(self, thread_id: str, before_seq: int) -> Optional[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rewind_db.py`. The house DB layer is exercised through fakes that capture SQL (no live PG in unit CI); the live dev gate (Task 10) validates against real Postgres. `PostgresDB.__new__` skips `__init__` so no pool is needed.

```python
"""Tests for the session-rewind DB primitives in src/database/postgres_db.py."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.database.postgres_db import PostgresDB


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Captures every statement; scripted fetchrow/fetchval returns."""

    def __init__(self, fetchrow_returns=None, fetchval_returns=None):
        self.calls = []
        self._fetchrow_returns = list(fetchrow_returns or [])
        self._fetchval_returns = list(fetchval_returns or [])

    def transaction(self):
        return _FakeTxn()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "UPDATE 0"

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self._fetchrow_returns.pop(0) if self._fetchrow_returns else None

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self._fetchval_returns.pop(0) if self._fetchval_returns else None


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _db_with(conn):
    db = PostgresDB.__new__(PostgresDB)
    db.acquire = lambda: _FakeAcquire(conn)
    return db


def test_apply_rewind_sweeps_ledgers_and_returns_stats():
    conn = _FakeConn(
        fetchrow_returns=[{"id": "11111111-1111-1111-1111-111111111111"}],
        fetchval_returns=[7, 3],  # swept count, surviving turn
    )
    db = _db_with(conn)
    out = asyncio.run(
        db.apply_rewind(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            from_seq=42,
            mode="conversation",
            actor="ws_client",
        )
    )
    assert out == {
        "rewind_id": "11111111-1111-1111-1111-111111111111",
        "swept": 7,
        "surviving_turn": 3,
    }
    sql_blob = " ".join(q for _, q, _ in conn.calls)
    assert "SET rewound_at = now()" in sql_blob
    assert "seq >= $2" in sql_blob
    assert "rewound_at IS NULL" in sql_blob
    assert "INSERT INTO thread_rewinds" in sql_blob


def test_apply_rewind_code_mode_skips_sweep():
    conn = _FakeConn(
        fetchrow_returns=[{"id": "22222222-2222-2222-2222-222222222222"}],
        fetchval_returns=[3],  # surviving turn only
    )
    db = _db_with(conn)
    out = asyncio.run(
        db.apply_rewind(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            from_seq=42,
            mode="code",
            restored_to_sha="abc123",
        )
    )
    assert out["swept"] == 0
    sweep_calls = [q for _, q, _ in conn.calls if "SET rewound_at" in q]
    assert sweep_calls == []


def test_record_turn_commit_upserts_at_max_seq():
    conn = _FakeConn()
    db = _db_with(conn)
    asyncio.run(db.record_turn_commit("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "sha1"))
    (_, query, args) = conn.calls[0]
    assert "INSERT INTO thread_turn_commits" in query
    assert "ON CONFLICT (thread_id, seq) DO UPDATE" in query
    assert "COALESCE(MAX(seq), 0)" in query
    assert args == ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "sha1")


def test_resolve_restore_commit_takes_largest_seq_below_target():
    db = PostgresDB.__new__(PostgresDB)
    db.fetchval = AsyncMock(return_value="shaX")
    got = asyncio.run(
        db.resolve_restore_commit("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 42)
    )
    assert got == "shaX"
    query = db.fetchval.await_args.args[0]
    assert "seq < $2" in query
    assert "ORDER BY seq DESC" in query
    db.fetchval.assert_awaited_once()


def test_live_readers_filter_tombstones():
    """The three agent-side conversation reads must exclude rewound rows."""
    import inspect

    from src.database import postgres_db as mod

    hist_src = inspect.getsource(mod.PostgresDB.get_thread_messages_history)
    ckpt_src = inspect.getsource(mod.PostgresDB.get_latest_compaction_checkpoint)
    seq_src = inspect.getsource(mod.PostgresDB.get_seq_for_message_id)
    assert "rewound_at IS NULL" in hist_src
    assert "rewound_at IS NULL" in ckpt_src
    assert "rewound_at IS NULL" in seq_src
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rewind_db.py -v`
Expected: FAIL — `AttributeError: 'PostgresDB' object has no attribute 'apply_rewind'` (and the filter test fails on missing `rewound_at IS NULL`).

- [ ] **Step 3: Add the filters to the three existing readers**

In `src/database/postgres_db.py`:

1. `get_thread_messages_history` — the base query (currently lines 397-402) becomes:

```python
        query = """
            SELECT role, content, tool_calls, tool_call_id, turn_number
            FROM thread_messages
            WHERE thread_id = $1
              AND role NOT IN ('summary', 'error')
              AND rewound_at IS NULL
        """
```

2. `get_latest_compaction_checkpoint` — its query becomes:

```python
        query = """
            SELECT content, metrics, turn_number
            FROM thread_messages
            WHERE thread_id = $1
              AND role = 'summary'
              AND rewound_at IS NULL
            ORDER BY turn_number DESC NULLS LAST, created_at DESC
            LIMIT 1
        """
```

3. `get_seq_for_message_id` — the SELECT becomes:

```python
        row = await self.fetchrow(
            "SELECT seq FROM thread_messages "
            "WHERE thread_id = $1 AND id = $2 AND rewound_at IS NULL",
            thread_id,
            _coerce_row_id(msg_id),
        )
```

(A tombstoned id now resolves to `None`: the compaction caller already falls back to `boundary_turn`, and the rewind caller treats it as "message not found" — both correct.)

- [ ] **Step 4: Implement the four new methods**

Add after `get_latest_compaction_checkpoint`:

```python
    async def get_live_message(
        self, thread_id: str, msg_id: str
    ) -> Optional[Dict[str, Any]]:
        """Resolve a message id to its live row (seq/role/content).

        The rewind target lookup: returns ``None`` for unknown ids AND for
        rows already tombstoned by an earlier rewind — a rewound-away message
        is not a valid rewind target.
        """
        row = await self.fetchrow(
            "SELECT seq, role, content FROM thread_messages "
            "WHERE thread_id = $1 AND id = $2 AND rewound_at IS NULL",
            thread_id,
            _coerce_row_id(msg_id),
        )
        if row is None:
            return None
        return {"seq": row["seq"], "role": row["role"], "content": row["content"]}

    async def apply_rewind(
        self,
        thread_id: str,
        from_seq: int,
        mode: str,
        actor: Optional[str] = None,
        abandoned_sha: Optional[str] = None,
        restored_to_sha: Optional[str] = None,
        restore_commit_sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Tombstone the tail at ``seq >= from_seq`` and ledger the rewind.

        One transaction: the sweep (skipped for mode='code' — files-only
        rewinds leave the transcript untouched), the ``thread_rewinds``
        ledger insert, and the surviving-turn readback the caller uses to
        reset ``turn_count``. Idempotent re-run sweeps 0 rows (the
        ``rewound_at IS NULL`` guard) but does append a second ledger row —
        callers serialize (session loop / advisory lock).
        """
        if mode not in ("both", "conversation", "code"):
            raise ValueError(f"invalid rewind mode: {mode}")
        swept = 0
        async with self.acquire() as conn:
            async with conn.transaction():
                if mode in ("both", "conversation"):
                    swept = await conn.fetchval(
                        """
                        WITH swept AS (
                            UPDATE thread_messages
                            SET rewound_at = now()
                            WHERE thread_id = $1
                              AND seq >= $2
                              AND rewound_at IS NULL
                            RETURNING 1
                        )
                        SELECT COUNT(*) FROM swept
                        """,
                        thread_id,
                        from_seq,
                    )
                row = await conn.fetchrow(
                    """
                    INSERT INTO thread_rewinds
                        (thread_id, from_seq, mode, actor, swept_count,
                         abandoned_sha, restored_to_sha, restore_commit_sha)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING id
                    """,
                    thread_id,
                    from_seq,
                    mode,
                    actor,
                    swept or 0,
                    abandoned_sha,
                    restored_to_sha,
                    restore_commit_sha,
                )
                surviving_turn = await conn.fetchval(
                    """
                    SELECT COALESCE(MAX(turn_number), 0)
                    FROM thread_messages
                    WHERE thread_id = $1
                      AND rewound_at IS NULL
                      AND role NOT IN ('summary', 'error')
                    """,
                    thread_id,
                )
        return {
            "rewind_id": str(row["id"]),
            "swept": int(swept or 0),
            "surviving_turn": int(surviving_turn or 0),
        }

    async def record_turn_commit(self, thread_id: str, commit_sha: str) -> None:
        """Map the workspace commit that just landed to the transcript position.

        seq = MAX(seq) over the thread's rows at commit time (0 before the
        first message — the pre-conversation workspace state, a valid restore
        target for a rewind to the very first message). Two commits at the
        same transcript position (e.g. a compaction checkpoint right after a
        turn commit) collapse to the later SHA — the newest workspace state
        for that position is the correct restore target.
        """
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO thread_turn_commits (thread_id, seq, commit_sha)
                SELECT $1,
                       COALESCE(MAX(seq), 0),
                       $2
                FROM thread_messages
                WHERE thread_id = $1
                ON CONFLICT (thread_id, seq) DO UPDATE
                    SET commit_sha = EXCLUDED.commit_sha,
                        created_at = now()
                """,
                thread_id,
                commit_sha,
            )

    async def resolve_restore_commit(
        self, thread_id: str, before_seq: int
    ) -> Optional[str]:
        """Workspace SHA for 'state before the message at before_seq'.

        The newest mapped commit strictly below the target: the workspace as
        it stood after every turn that survives the rewind. ``None`` = no
        coverage (thread predates the feature) — code restore unavailable.
        """
        return await self.fetchval(
            """
            SELECT commit_sha FROM thread_turn_commits
            WHERE thread_id = $1 AND seq < $2
            ORDER BY seq DESC
            LIMIT 1
            """,
            thread_id,
            before_seq,
        )
```

Note: `record_turn_commit` intentionally computes MAX(seq) including tombstoned rows — a commit made right after a rewind maps above the swept range, which is correct (that workspace state postdates the sweep).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_rewind_db.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Ruff + commit**

```bash
ruff format src/database/postgres_db.py tests/test_rewind_db.py
ruff check src/database/postgres_db.py tests/test_rewind_db.py
git add src/database/postgres_db.py tests/test_rewind_db.py
git commit -m "feat(rewind): agent-side rewind primitives + tombstone filters on live readers"
```

---

### Task 3: `GitManager.restore_tree` — forward tree restore

**Files:**
- Modify: `src/managers/git_manager.py` (new method after `commit`, which ends near line 291)
- Test: `tests/test_rewind_git.py` (create)

**Interfaces:**
- Consumes: `GitManager._run_git`, `GitManager.is_active`.
- Produces: `def restore_tree(self, commit_sha: str) -> bool` — makes worktree+index exactly match `commit_sha`'s tree (modifications, re-creations, AND deletions of since-created tracked files), HEAD unmoved, nothing committed. Caller composes snapshot-commit → `restore_tree` → restore-commit.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rewind_git.py`. Uses a real local git repo in tmp_path (the local-subprocess `_run_git` path — no backend).

```python
"""GitManager.restore_tree — forward restore of a full tree, deletions included."""

import subprocess

from src.managers.git_manager import GitManager


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _make_repo(tmp_path):
    repo = tmp_path / "ws"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    return repo


def test_restore_tree_restores_content_and_deletes_new_files(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "kept.txt").write_text("v1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "A")
    sha_a = _git(repo, "rev-parse", "HEAD")

    (repo / "kept.txt").write_text("v2")
    (repo / "new.txt").write_text("added later")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "B")

    mgr = GitManager(repo)
    assert mgr.is_active
    assert mgr.restore_tree(sha_a) is True

    assert (repo / "kept.txt").read_text() == "v1"
    assert not (repo / "new.txt").exists()  # the checkout -- . trap: must delete
    # HEAD did not move — this is a worktree/index restore, not a reset.
    assert _git(repo, "rev-parse", "HEAD") != sha_a
    # Committing the restored state keeps history linear (fast-forward safe).
    assert mgr.commit("Rewind: restore workspace") is True
    log = _git(repo, "log", "--oneline")
    assert len(log.splitlines()) == 3


def test_restore_tree_bad_sha_returns_false(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "a.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "A")
    mgr = GitManager(repo)
    assert mgr.restore_tree("0000000000000000000000000000000000000000") is False


def test_restore_tree_inactive_repo_returns_false(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    mgr = GitManager(plain)
    assert mgr.restore_tree("HEAD") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rewind_git.py -v`
Expected: FAIL — `AttributeError: 'GitManager' object has no attribute 'restore_tree'`.

(If `GitManager(repo)` needs different constructor args than a bare path, mirror the construction used in `tests/test_tools_git.py` — check that file's fixture before adjusting the test, not the production signature.)

- [ ] **Step 3: Implement `restore_tree`**

Add to `src/managers/git_manager.py` directly after `commit`:

```python
    def restore_tree(self, commit_sha: str) -> bool:
        """Make worktree + index exactly match ``commit_sha``'s tree.

        Forward restore for session rewind: HEAD does not move (no
        ``reset --hard`` — that would strand the branch behind the Gitea
        remote and break the fast-forward push). ``read-tree -u --reset``
        is the one porcelain-adjacent verb that also DELETES files that are
        tracked now but absent at the target (``checkout <sha> -- .``
        leaves them behind). The caller commits the abandoned state first,
        so everything current is tracked and nothing is lost.

        Returns True on success; False on any git failure (bad SHA,
        inactive repo). Does not commit — callers follow with commit().
        """
        if not self.is_active:
            logger.debug("Git not active, skipping restore_tree")
            return False
        try:
            result = self._run_git(["read-tree", "-u", "--reset", commit_sha])
            if result.returncode != 0:
                logger.warning(f"git read-tree failed: {result.stderr}")
                return False
            return True
        except Exception as e:
            logger.error(f"Failed to restore tree: {e}")
            return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rewind_git.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Ruff + commit**

```bash
ruff format src/managers/git_manager.py tests/test_rewind_git.py
ruff check src/managers/git_manager.py tests/test_rewind_git.py
git add src/managers/git_manager.py tests/test_rewind_git.py
git commit -m "feat(rewind): GitManager.restore_tree — forward full-tree restore, deletions included"
```

---

### Task 4: Record turn→commit mappings at every workspace commit site

**Files:**
- Modify: `src/persistent_graph.py` (callbacks dataclass ~line 447-530; auto-commit block lines 1009-1038; compaction checkpoint commit ~lines 1509-1525)
- Modify: `src/api/persistent_app.py` (callbacks construction ~line 515; `_handle_compact` git block lines 6305-6316; new `_loop_on_workspace_commit`)
- Test: extend `tests/test_rewind_db.py` → new file section, and `tests/test_persistent_app.py`-style unit test in `tests/test_rewind_handler.py` (created here, extended in Task 5)

**Interfaces:**
- Consumes: `PostgresDB.record_turn_commit` (Task 2), `GitManager.get_current_commit()`.
- Produces: `PersistentLoopCallbacks.on_workspace_commit: Optional[Callable[[str], Awaitable[None]]]` — awaited with the new HEAD SHA after every successful workspace commit; app-side implementation `_loop_on_workspace_commit(sha: str)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rewind_handler.py`:

```python
"""Turn→commit mapping wiring (Task 4) + the rewind WS handler (Task 5)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.persistent_graph import PersistentLoopCallbacks


def _minimal_callbacks(**overrides):
    """Build the callbacks dataclass with every required field stubbed."""
    import inspect

    kwargs = {}
    for name, param in inspect.signature(PersistentLoopCallbacks).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        kwargs[name] = AsyncMock()
    kwargs.update(overrides)
    return PersistentLoopCallbacks(**kwargs)


def test_callbacks_accept_on_workspace_commit():
    spy = AsyncMock()
    cbs = _minimal_callbacks(on_workspace_commit=spy)
    assert cbs.on_workspace_commit is spy


def test_on_workspace_commit_defaults_none():
    cbs = _minimal_callbacks()
    assert cbs.on_workspace_commit is None


def test_loop_on_workspace_commit_records_via_conn(monkeypatch):
    from src.api import persistent_app as app_mod

    conn = MagicMock()
    conn.record_turn_commit = AsyncMock()
    session = MagicMock()
    session.postgres_conn = conn
    monkeypatch.setattr(app_mod, "_session", session)
    monkeypatch.setattr(app_mod, "_thread_id", "tid-1")

    asyncio.run(app_mod._loop_on_workspace_commit("sha42"))
    conn.record_turn_commit.assert_awaited_once_with("tid-1", "sha42")


def test_loop_on_workspace_commit_tolerates_no_session(monkeypatch):
    from src.api import persistent_app as app_mod

    monkeypatch.setattr(app_mod, "_session", None)
    asyncio.run(app_mod._loop_on_workspace_commit("sha42"))  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rewind_handler.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'on_workspace_commit'` / `AttributeError: … has no attribute '_loop_on_workspace_commit'`.

- [ ] **Step 3: Add the callback field**

In `src/persistent_graph.py`, inside the `PersistentLoopCallbacks` dataclass, next to `on_workspace_upgrade_needed` (~line 523), add:

```python
    # Awaited with the new HEAD SHA after every successful workspace commit
    # (per-turn auto-commit + compaction checkpoint). Feeds the
    # thread_turn_commits map that session rewind's code-restore resolves
    # against. Optional: worker-job transports don't wire it.
    on_workspace_commit: Optional[Callable[[str], Awaitable[None]]] = None
```

- [ ] **Step 4: Fire it at both persistent_graph commit sites**

In the auto-commit block (`src/persistent_graph.py:1013-1024`), after a successful commit:

```python
                if tool_calls_this_turn > 0:
                    try:
                        if git_mgr.has_uncommitted_changes():
                            if not git_mgr.commit(f"Auto-commit after turn {turn_id}"):
                                logger.warning(
                                    f"Turn {turn_id}: workspace auto-commit failed"
                                )
                            elif callbacks.on_workspace_commit:
                                sha = git_mgr.get_current_commit()
                                if sha:
                                    try:
                                        await callbacks.on_workspace_commit(sha)
                                    except Exception:
                                        logger.warning(
                                            f"Turn {turn_id}: turn-commit mapping "
                                            "failed (rewind code-restore loses this "
                                            "granularity point)",
                                            exc_info=True,
                                        )
                    except Exception:
                        logger.warning(
                            f"Turn {turn_id}: workspace auto-commit raised",
                            exc_info=True,
                        )
```

At the compaction checkpoint commit (~line 1524, the `git_mgr.commit("Compaction checkpoint …")` inside `run_persistent_loop`), apply the same pattern: if the `commit(...)` call returns True and `callbacks.on_workspace_commit` is set, `sha = git_mgr.get_current_commit()` and await the callback under the same try/except-log.

- [ ] **Step 5: App-side implementation + wiring + `_handle_compact`**

In `src/api/persistent_app.py`, next to `_loop_on_turn_complete` (~line 5301), add:

```python
async def _loop_on_workspace_commit(sha: str) -> None:
    """Record a workspace commit against the current transcript position.

    Best-effort: a miss only degrades rewind code-restore granularity for
    this turn (the resolver falls back to the previous mapped commit).
    """
    if _session is None or _session.postgres_conn is None or _thread_id is None:
        return
    try:
        await _session.postgres_conn.record_turn_commit(_thread_id, sha)
    except Exception:
        logger.warning("record_turn_commit failed (non-fatal)", exc_info=True)
```

Wire it into the `PersistentLoopCallbacks(...)` construction (~line 515, alongside `on_turn_complete=_loop_on_turn_complete`):

```python
            on_workspace_commit=_loop_on_workspace_commit,
```

In `_handle_compact`'s git block (lines 6305-6316), record the checkpoint commit too:

```python
        if _session.workspace_manager:
            git_mgr = getattr(_session.workspace_manager, "git_manager", None)
            if git_mgr and git_mgr.is_active:
                try:
                    if git_mgr.has_uncommitted_changes():
                        if git_mgr.commit(
                            f"Compaction checkpoint ({before_count} → {after_count} msgs)"
                        ):
                            sha = git_mgr.get_current_commit()
                            if sha:
                                await _loop_on_workspace_commit(sha)
                    git_mgr.push()
                except Exception as e:
                    logger.debug(f"Git push on compaction failed (non-fatal): {e}")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_rewind_handler.py tests/test_persistent_app.py -v`
Expected: new tests PASS; the existing `test_persistent_app.py` suite stays green (the new dataclass field is optional-with-default, `_handle_compact`'s change is behavior-preserving when the commit fails).

- [ ] **Step 7: Ruff + commit**

```bash
ruff format src/persistent_graph.py src/api/persistent_app.py tests/test_rewind_handler.py
ruff check src/persistent_graph.py src/api/persistent_app.py tests/test_rewind_handler.py
git add src/persistent_graph.py src/api/persistent_app.py tests/test_rewind_handler.py
git commit -m "feat(rewind): map every workspace commit to its transcript position"
```

---

### Task 5: The `rewind` WS verb — interrupt, restore, sweep, truncate, re-epoch

**Files:**
- Modify: `src/api/persistent_app.py` — new module state next to `_loop_interrupt_flag` (~line 197), new `_handle_rewind` next to `_handle_compact` (~line 6230), new dispatch arm after `undo` (line 3193)
- Test: extend `tests/test_rewind_handler.py`

**Interfaces:**
- Consumes: `get_live_message` / `apply_rewind` / `resolve_restore_commit` (Task 2), `restore_tree` (Task 3), `_resolve_event_journal_epoch`, `_restore_session_messages`, `_ws_send`, `_broadcast`, the interrupt globals (`_loop_interrupt_flag`, `_hard_interrupt_event`, `_tool_inflight`, `_turn_event_open`), `_loop_user_queue`.
- Produces (wire contract Tasks 7-9 rely on):
  - Client frame: `{method: "rewind", message_id: <thread_messages row id>, mode: "both"|"conversation"|"code", request_id: <uuid>}`
  - Direct ack (initiator only, no `_seq`): `rewind.ack` `{request_id, message_id, mode, prompt, swept, restored_to_sha}` — or `error` `{message, request_id}`
  - Broadcast (all viewers, journaled, arrives via SSE): `rewind.done` `{message_id, mode}` for transcript-changing modes; `rewind.files_restored` `{restored_to_sha}` for mode='code'

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rewind_handler.py`:

```python
import json
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage


def _mk_session(messages, git_active=False):
    conn = MagicMock()
    conn.get_live_message = AsyncMock()
    conn.apply_rewind = AsyncMock(
        return_value={"rewind_id": "r-1", "swept": 4, "surviving_turn": 2}
    )
    conn.resolve_restore_commit = AsyncMock(return_value="sha-target")
    git_mgr = MagicMock()
    git_mgr.is_active = git_active
    git_mgr.commit = MagicMock(return_value=True)
    git_mgr.get_current_commit = MagicMock(side_effect=["sha-snap", "sha-restore"])
    git_mgr.restore_tree = MagicMock(return_value=True)
    ws_mgr = SimpleNamespace(git_manager=git_mgr if git_active else None)
    session = SimpleNamespace(
        messages=messages,
        turn_count=9,
        postgres_conn=conn,
        workspace_manager=ws_mgr,
    )
    return session, conn, git_mgr


def _human(msg_id, text):
    m = HumanMessage(content=text)
    m.id = msg_id
    return m


def _run_rewind(app_mod, ws, data):
    asyncio.run(app_mod._handle_rewind(ws, data))


def _patched_app(monkeypatch, session, *, turn_open=False):
    from src.api import persistent_app as app_mod

    monkeypatch.setattr(app_mod, "_session", session)
    monkeypatch.setattr(app_mod, "_thread_id", "tid-1")
    monkeypatch.setattr(app_mod, "_turn_event_open", turn_open)
    monkeypatch.setattr(app_mod, "_tool_inflight", False)
    monkeypatch.setattr(app_mod, "_loop_user_queue", asyncio.Queue())
    monkeypatch.setattr(app_mod, "_loop_interrupt_flag", None)
    monkeypatch.setattr(app_mod, "_hard_interrupt_event", asyncio.Event())
    monkeypatch.setattr(
        app_mod, "_resolve_event_journal_epoch", AsyncMock(return_value=7)
    )
    monkeypatch.setattr(app_mod, "_restore_session_messages", AsyncMock())
    monkeypatch.setattr(app_mod, "_broadcast", MagicMock())
    ws_sent = []

    async def _fake_ws_send(ws, method, params):
        ws_sent.append((method, params))

    monkeypatch.setattr(app_mod, "_ws_send", _fake_ws_send)
    return app_mod, ws_sent


def test_rewind_shallow_truncates_in_place(monkeypatch):
    target = _human("msg_target", "redo this")
    msgs = [
        _human("msg_old", "earlier"),
        AIMessage(content="ok"),
        target,
        AIMessage(content="bad path"),
    ]
    session, conn, _ = _mk_session(msgs)
    conn.get_live_message.return_value = {"seq": 42, "role": "human", "content": "redo this"}
    app_mod, ws_sent = _patched_app(monkeypatch, session)

    _run_rewind(
        app_mod,
        MagicMock(),
        {"message_id": "msg_target", "mode": "conversation", "request_id": "rq1"},
    )

    assert len(session.messages) == 2  # truncated at the target, inclusive
    assert session.turn_count == 2  # from apply_rewind surviving_turn
    conn.apply_rewind.assert_awaited_once()
    assert conn.apply_rewind.await_args.kwargs["from_seq"] == 42
    app_mod._restore_session_messages.assert_not_awaited()
    acks = [p for m, p in ws_sent if m == "rewind.ack"]
    assert acks and acks[0]["prompt"] == "redo this"
    assert acks[0]["request_id"] == "rq1"
    app_mod._broadcast.assert_called_once()
    assert app_mod._broadcast.call_args.args[0] == "rewind.done"


def test_rewind_deep_falls_back_to_rehydrate(monkeypatch):
    # Target id is NOT in the in-memory list (compacted away / restored prefix).
    msgs = [_human("msg_other", "x"), AIMessage(content="y")]
    session, conn, _ = _mk_session(msgs)
    conn.get_live_message.return_value = {"seq": 5, "role": "human", "content": "old"}
    app_mod, ws_sent = _patched_app(monkeypatch, session)

    _run_rewind(
        app_mod,
        MagicMock(),
        {"message_id": "msg_gone", "mode": "conversation", "request_id": "rq2"},
    )

    assert session.messages == []  # cleared…
    app_mod._restore_session_messages.assert_awaited_once()  # …and rehydrated


def test_rewind_code_mode_requires_git(monkeypatch):
    session, conn, _ = _mk_session([_human("m", "x")], git_active=False)
    conn.get_live_message.return_value = {"seq": 3, "role": "human", "content": "x"}
    app_mod, ws_sent = _patched_app(monkeypatch, session)

    _run_rewind(app_mod, MagicMock(), {"message_id": "m", "mode": "code", "request_id": "r"})

    errors = [p for m, p in ws_sent if m == "error"]
    assert errors and "version" in errors[0]["message"].lower()
    conn.apply_rewind.assert_not_awaited()


def test_rewind_both_git_failure_aborts_before_sweep(monkeypatch):
    target = _human("m", "x")
    session, conn, git = _mk_session([target], git_active=True)
    conn.get_live_message.return_value = {"seq": 3, "role": "human", "content": "x"}
    git.restore_tree = MagicMock(return_value=False)
    app_mod, ws_sent = _patched_app(monkeypatch, session)

    _run_rewind(app_mod, MagicMock(), {"message_id": "m", "mode": "both", "request_id": "r"})

    conn.apply_rewind.assert_not_awaited()  # sweep gated behind git success
    assert session.messages == [target]  # memory untouched
    assert [m for m, _ in ws_sent if m == "error"]


def test_rewind_rejects_non_human_target(monkeypatch):
    session, conn, _ = _mk_session([_human("m", "x")])
    conn.get_live_message.return_value = {"seq": 4, "role": "ai", "content": "resp"}
    app_mod, ws_sent = _patched_app(monkeypatch, session)

    _run_rewind(app_mod, MagicMock(), {"message_id": "m", "mode": "conversation", "request_id": "r"})

    conn.apply_rewind.assert_not_awaited()
    assert [m for m, _ in ws_sent if m == "error"]


def test_rewind_drains_pending_queue(monkeypatch):
    target = _human("m", "x")
    session, conn, _ = _mk_session([target])
    conn.get_live_message.return_value = {"seq": 3, "role": "human", "content": "x"}
    app_mod, ws_sent = _patched_app(monkeypatch, session)
    from src.api import persistent_app as pa

    pa._loop_user_queue.put_nowait("queued-input")

    _run_rewind(app_mod, MagicMock(), {"message_id": "m", "mode": "conversation", "request_id": "r"})

    assert pa._loop_user_queue.empty()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rewind_handler.py -v`
Expected: the six new tests FAIL — `AttributeError: … has no attribute '_handle_rewind'`.

- [ ] **Step 3: Implement `_handle_rewind`**

In `src/api/persistent_app.py`. First, module state next to the interrupt globals (~line 204):

```python
# Serializes rewinds: two concurrent rewind frames on one session would race
# the sweep/truncate pair. Second caller gets an error, not a queue.
_rewind_lock: asyncio.Lock = asyncio.Lock()
```

Then the handler, placed directly before `_handle_compact` (~line 6230):

```python
async def _handle_rewind(ws: WebSocket, data: Dict[str, Any]) -> None:
    """Rewind the session to just before an earlier user message.

    docs/features/session_rewind.md §Flow — attached. Order is load-bearing:
    interrupt → resolve+validate target → git forward-restore (fallible,
    gates everything) → DB sweep+ledger → in-memory truncate/rehydrate →
    events-epoch bump → acks. Bash side effects and non-git sessions degrade
    exactly like Claude Code: conversation-only.
    """
    global _loop_interrupt_flag, _events_epoch, _next_seq

    request_id = data.get("request_id")

    async def _err(message: str) -> None:
        await _ws_send(ws, "error", {"message": message, "request_id": request_id})

    if _session is None or _session.postgres_conn is None or _thread_id is None:
        await _err("Session no longer active")
        return
    mode = data.get("mode", "conversation")
    if mode not in ("both", "conversation", "code"):
        await _err(f"Invalid rewind mode: {mode}")
        return
    message_id = data.get("message_id")
    if not message_id:
        await _err("rewind requires message_id")
        return

    if _rewind_lock.locked():
        await _err("A rewind is already in progress")
        return
    async with _rewind_lock:
        conn = _session.postgres_conn

        # 1. Interrupt any in-flight turn (same policy as the interrupt verb:
        #    graceful while a tool is mid-invoke, hard otherwise) and wait for
        #    the loop to park. _turn_event_open is the turn-in-flight signal.
        if _turn_event_open:
            _loop_interrupt_flag = "graceful" if _tool_inflight else "hard"
            if _loop_interrupt_flag == "hard" and _hard_interrupt_event is not None:
                _hard_interrupt_event.set()
            deadline = asyncio.get_event_loop().time() + 60.0
            while _turn_event_open:
                if asyncio.get_event_loop().time() > deadline:
                    await _err("Could not interrupt the running turn — try again")
                    return
                await asyncio.sleep(0.1)

        # 2. Drain queued inputs: their rows sit past the sweep boundary and
        #    are about to be tombstoned; processing them post-rewind would
        #    resurrect the abandoned timeline.
        if _loop_user_queue is not None:
            while True:
                try:
                    _loop_user_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        # 3. Resolve + validate the target.
        row = await conn.get_live_message(_thread_id, message_id)
        if row is None:
            await _err("Message not found (it may already be rewound)")
            return
        if row["role"] != "human":
            await _err("Rewind targets must be user messages")
            return
        from_seq = row["seq"]
        prompt = row["content"] or ""

        # 4. Workspace forward-restore — fallible, so it gates the sweep.
        abandoned_sha = None
        restored_to_sha = None
        restore_commit_sha = None
        if mode in ("both", "code"):
            ws_mgr = _session.workspace_manager
            git_mgr = getattr(ws_mgr, "git_manager", None) if ws_mgr else None
            if not (git_mgr and git_mgr.is_active):
                await _err(
                    "This session has no version history — file restore is "
                    "unavailable (conversation-only rewind still works)"
                )
                return
            restored_to_sha = await conn.resolve_restore_commit(_thread_id, from_seq)
            if not restored_to_sha:
                await _err(
                    "No workspace checkpoint exists before this message — "
                    "file restore is unavailable for this target"
                )
                return
            # Snapshot the abandoned state first: nothing is ever lost in git.
            if not git_mgr.commit("Rewind: pre-rewind snapshot"):
                await _err("Could not snapshot the current workspace state")
                return
            abandoned_sha = git_mgr.get_current_commit()
            if not git_mgr.restore_tree(restored_to_sha):
                await _err(
                    "Workspace restore failed — files are unchanged (a "
                    "snapshot commit was kept); conversation was not rewound"
                )
                return
            if not git_mgr.commit(
                f"Rewind: restore workspace to {restored_to_sha[:12]}"
            ):
                await _err("Workspace restore could not be committed")
                return
            restore_commit_sha = git_mgr.get_current_commit()

        # 5. Sweep + ledger (one transaction). mode='code' ledgers only.
        result = await conn.apply_rewind(
            _thread_id,
            from_seq=from_seq,
            mode=mode,
            actor="ws_client",
            abandoned_sha=abandoned_sha,
            restored_to_sha=restored_to_sha,
            restore_commit_sha=restore_commit_sha,
        )

        # 6. Fix in-memory state (transcript-changing modes only).
        # _coerce_row_id maps in-memory `msg_…` ids to the row UUIDs the
        # frontend sends; restored-prefix messages carry no id (the HF-7
        # resume diet drops the column) and correctly fall through to the
        # deep-rewind path.
        if mode in ("both", "conversation"):
            from ..database.postgres_db import _coerce_row_id

            target_uuid = str(_coerce_row_id(message_id))
            cut_index = None
            for i, m in enumerate(_session.messages):
                mid = getattr(m, "id", None)
                if mid and str(_coerce_row_id(mid)) == target_uuid:
                    cut_index = i
                    break
            if cut_index is not None:
                # Shallow rewind: fidelity-preserving in-place truncate.
                del _session.messages[cut_index:]
            else:
                # Deep rewind (target predates the live compaction boundary,
                # or the prefix was restored without ids): rebuild from the
                # now-filtered transcript.
                _session.messages.clear()
                await _restore_session_messages()
            _session.turn_count = result["surviving_turn"]
            _loop_last_user_content[0] = ""

            # 7. New event generation → every SSE viewer takes the existing
            #    gone_beyond_horizon repaint against the filtered history.
            try:
                _events_epoch = await _resolve_event_journal_epoch(conn, _thread_id)
                _next_seq = 0
            except Exception:
                logger.warning(
                    "Rewind epoch bump failed — viewers repaint on next attach",
                    exc_info=True,
                )

        # 8. Acks: direct to the initiator (no _seq), then the journaled
        #    all-viewer signal in the NEW epoch.
        await _ws_send(
            ws,
            "rewind.ack",
            {
                "request_id": request_id,
                "message_id": message_id,
                "mode": mode,
                "prompt": prompt,
                "swept": result["swept"],
                "restored_to_sha": restored_to_sha,
            },
        )
        if mode in ("both", "conversation"):
            _broadcast("rewind.done", {"message_id": message_id, "mode": mode})
        else:
            _broadcast("rewind.files_restored", {"restored_to_sha": restored_to_sha})
        logger.info(
            "Rewind applied: thread=%s mode=%s from_seq=%s swept=%s",
            _thread_id,
            mode,
            from_seq,
            result["swept"],
        )
```

- [ ] **Step 4: Register the verb**

In the WS dispatch chain, directly after the `undo` arm (line 3193), add:

```python
            elif method == "rewind":
                if _session is None:
                    await _ws_send(
                        ws,
                        "error",
                        {"message": "Session no longer active"},
                    )
                    continue
                asyncio.create_task(
                    _handle_rewind(ws, data), name="handle-rewind"
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_rewind_handler.py tests/test_persistent_app.py -v`
Expected: all new tests PASS; existing suite green.

- [ ] **Step 6: Ruff + commit**

```bash
ruff format src/api/persistent_app.py tests/test_rewind_handler.py
ruff check src/api/persistent_app.py tests/test_rewind_handler.py
git add src/api/persistent_app.py tests/test_rewind_handler.py
git commit -m "feat(rewind): the rewind WS verb — interrupt, forward-restore, sweep, truncate, re-epoch"
```

---

### Task 6: "Summarize up to here" — boundary for the `compact` verb

**Files:**
- Modify: `src/api/persistent_app.py` — `_handle_compact` signature + boundary math (line 6230), dispatch arm (line 3151-3154)
- Test: extend `tests/test_rewind_handler.py`

**Interfaces:**
- Consumes: `summarize_and_compact(…, keep_recent_override=N)` (`src/core/context.py:1997`), `is_workspace_injection_message` (`src/core/workspace_injection.py`).
- Produces: client frame `{method: "compact", focus: "", boundary_message_id: <row id>}` — compacts everything BEFORE that message; the message and everything after stay verbatim. Not a rewind: no tombstones, no ledger.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rewind_handler.py`:

```python
def test_compact_boundary_maps_to_keep_recent(monkeypatch):
    """boundary_message_id=X → keep_recent_override counts non-injection
    messages from X (inclusive) to the end."""
    from src.api import persistent_app as app_mod

    target = _human("msg_b", "keep from here")
    msgs = [
        _human("msg_a", "old"),
        AIMessage(content="old reply"),
        target,
        AIMessage(content="recent reply"),
    ]
    captured = {}

    async def _fake_summarize(**kwargs):
        captured.update(kwargs)
        return list(kwargs["messages"])

    ctx_mgr = MagicMock()
    ctx_mgr.summarize_and_compact = AsyncMock(side_effect=_fake_summarize)
    ctx_mgr.compaction_runs = 0
    session = SimpleNamespace(
        messages=msgs,
        turn_count=4,
        context_manager=ctx_mgr,
        auxiliary_llm=MagicMock(),
        config=SimpleNamespace(
            context_management=SimpleNamespace(max_summary_length=10000)
        ),
        workspace_manager=None,
        postgres_conn=MagicMock(),
    )
    monkeypatch.setattr(app_mod, "_session", session)
    ws_sent = []

    async def _fake_ws_send(ws, method, params):
        ws_sent.append((method, params))

    monkeypatch.setattr(app_mod, "_ws_send", _fake_ws_send)

    asyncio.run(app_mod._handle_compact(MagicMock(), "", boundary_message_id="msg_b"))

    assert ctx_mgr.summarize_and_compact.await_count == 1
    assert captured["keep_recent_override"] == 2  # target + 1 later message


def test_compact_boundary_unknown_id_errors(monkeypatch):
    from src.api import persistent_app as app_mod

    session = SimpleNamespace(
        messages=[_human("msg_a", "x")],
        context_manager=MagicMock(),
        auxiliary_llm=MagicMock(),
        config=SimpleNamespace(
            context_management=SimpleNamespace(max_summary_length=10000)
        ),
        workspace_manager=None,
        postgres_conn=MagicMock(),
    )
    monkeypatch.setattr(app_mod, "_session", session)
    ws_sent = []

    async def _fake_ws_send(ws, method, params):
        ws_sent.append((method, params))

    monkeypatch.setattr(app_mod, "_ws_send", _fake_ws_send)

    asyncio.run(
        app_mod._handle_compact(MagicMock(), "", boundary_message_id="msg_missing")
    )

    session.context_manager.summarize_and_compact.assert_not_awaited()
    assert [m for m, _ in ws_sent if m == "error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rewind_handler.py -k boundary -v`
Expected: FAIL — `TypeError: _handle_compact() got an unexpected keyword argument 'boundary_message_id'`.

- [ ] **Step 3: Implement the boundary**

Change `_handle_compact`'s signature (line 6230):

```python
async def _handle_compact(
    ws: WebSocket, focus: str = "", boundary_message_id: Optional[str] = None
) -> None:
```

After the `ctx_mgr = _session.context_manager` setup and before the `summarize_and_compact` call, insert:

```python
        # "Summarize up to here" (session rewind's sibling action): map the
        # chosen message to keep_recent_override = the number of messages from
        # it (inclusive) to the end, counted on the same basis
        # summarize_and_compact uses (workspace injections excluded — they are
        # filtered before keep_recent applies).
        keep_recent_override = None
        if boundary_message_id:
            from ..core.workspace_injection import is_workspace_injection_message
            from ..database.postgres_db import _coerce_row_id

            target_uuid = str(_coerce_row_id(boundary_message_id))
            cut_index = None
            for i, m in enumerate(_session.messages):
                mid = getattr(m, "id", None)
                if mid and str(_coerce_row_id(mid)) == target_uuid:
                    cut_index = i
                    break
            if cut_index is None:
                await _ws_send(
                    ws,
                    "error",
                    {
                        "message": "That message is no longer in working "
                        "context — it may already be summarized"
                    },
                )
                return
            keep_recent_override = sum(
                1
                for m in _session.messages[cut_index:]
                if not is_workspace_injection_message(m)
            )
```

and pass it through the existing call:

```python
        result = await ctx_mgr.summarize_and_compact(
            messages=_session.messages,
            auxiliary=_session.auxiliary_llm,
            max_summary_length=getattr(
                _session.config.context_management, "max_summary_length", 10000
            ),
            keep_recent_override=keep_recent_override,
            trigger="manual",
            focus=focus or None,
        )
```

Update the dispatch arm (line 3151-3154):

```python
            elif method == "compact":
                # Manual compaction (/compact command, or the rewind action
                # sheet's "Summarize up to here" with boundary_message_id).
                focus = data.get("focus", "")
                asyncio.create_task(
                    _handle_compact(
                        ws, focus, boundary_message_id=data.get("boundary_message_id")
                    )
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rewind_handler.py tests/test_persistent_app.py -v`
Expected: PASS, existing `_handle_compact` tests untouched (new kwarg defaults to `None`).

- [ ] **Step 5: Ruff + commit**

```bash
ruff format src/api/persistent_app.py tests/test_rewind_handler.py
ruff check src/api/persistent_app.py tests/test_rewind_handler.py
git add src/api/persistent_app.py tests/test_rewind_handler.py
git commit -m "feat(rewind): summarize-up-to-here — explicit boundary for manual compaction"
```

---

### Task 7: Orchestrator — detached rewind REST + server-side filters

**Files:**
- Modify: `orchestrator/database/postgres.py` — new `apply_thread_rewind` + `get_live_thread_message` next to `get_thread_messages_history` (line 7849); filters at lines 7871-7876 (`get_thread_messages_history`), 7909-7931 (`get_thread_messages_page`), 7947-7951 (`get_thread_message_count`), 6580 (`get_officer_last_engagement`, the `thread_messages` arm)
- Modify: `orchestrator/main.py` — new endpoint near `resume_thread` (line 29389)
- Test: `tests/test_rewind_orchestrator.py` (create)

**Interfaces:**
- Consumes: Task 1 DDL, `require_thread_owner` (`orchestrator/security/access.py:530`).
- Produces:
  - `POST /api/agents/threads/{thread_id}/rewind` body `{"message_id": str, "mode": "conversation"}` → `{"rewind_id", "swept", "prompt"}`; 400 non-conversation mode; 404 unknown/tombstoned target; 409 live agent bound.
  - `async def apply_thread_rewind(self, thread_id: str, from_seq: int, actor: str) -> Dict[str, Any]` → `{"rewind_id", "swept", "surviving_turn"}` — advisory-locked; also bumps `threads.events_epoch` and journals a `rewind.done` `thread_events` row in the new epoch.
  - `async def get_live_thread_message(self, thread_id: str, message_id: str) -> Optional[Dict[str, Any]]` → `{"seq", "role", "content"}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rewind_orchestrator.py`:

```python
"""Detached-rewind REST endpoint + orchestrator-side rewind SQL."""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_orchestrator_live_readers_filter_tombstones():
    from orchestrator.database import postgres as mod

    for meth in (
        "get_thread_messages_history",
        "get_thread_messages_page",
        "get_thread_message_count",
        "get_officer_last_engagement",
    ):
        src = inspect.getsource(getattr(mod.PostgresDB, meth))
        assert "rewound_at IS NULL" in src, f"{meth} must filter tombstones"


def test_apply_thread_rewind_locks_sweeps_bumps_and_journals():
    from orchestrator.database.postgres import PostgresDB

    class _FakeTxn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeConn:
        def __init__(self):
            self.calls = []

        def transaction(self):
            return _FakeTxn()

        async def execute(self, q, *a):
            self.calls.append(q)

        async def fetchrow(self, q, *a):
            self.calls.append(q)
            return {"id": "33333333-3333-3333-3333-333333333333"}

        async def fetchval(self, q, *a):
            self.calls.append(q)
            if "COUNT" in q:
                return 5
            if "events_epoch" in q:
                return 9
            return 2

    conn = _FakeConn()

    class _FakeAcquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    db = PostgresDB.__new__(PostgresDB)
    db.acquire = lambda: _FakeAcquire()

    out = asyncio.run(
        db.apply_thread_rewind(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", from_seq=10, actor="user-1"
        )
    )
    assert out["swept"] == 5
    blob = " ".join(conn.calls)
    assert "pg_advisory_xact_lock" in blob
    assert "SET rewound_at = now()" in blob
    assert "INSERT INTO thread_rewinds" in blob
    assert "events_epoch = events_epoch + 1" in blob
    assert "INSERT INTO thread_events" in blob


@pytest.mark.anyio
async def test_rewind_endpoint_rejects_live_agent(monkeypatch):
    from orchestrator import main as orch_main

    async def _fake_owner(request, db, thread_id):
        return ({"id": "user-1"}, {"id": thread_id, "agent_id": "agent-9"})

    monkeypatch.setattr(orch_main, "require_thread_owner", _fake_owner)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await orch_main.rewind_thread_detached(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            MagicMock(),
            orch_main.ThreadRewindRequest(message_id="m1", mode="conversation"),
        )
    assert exc.value.status_code == 409


@pytest.mark.anyio
async def test_rewind_endpoint_rejects_code_mode(monkeypatch):
    from orchestrator import main as orch_main

    async def _fake_owner(request, db, thread_id):
        return ({"id": "user-1"}, {"id": thread_id, "agent_id": None})

    monkeypatch.setattr(orch_main, "require_thread_owner", _fake_owner)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await orch_main.rewind_thread_detached(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            MagicMock(),
            orch_main.ThreadRewindRequest(message_id="m1", mode="both"),
        )
    assert exc.value.status_code == 400
    assert "resume" in str(exc.value.detail).lower()


@pytest.mark.anyio
async def test_rewind_endpoint_happy_path(monkeypatch):
    from orchestrator import main as orch_main

    async def _fake_owner(request, db, thread_id):
        return ({"id": "user-1"}, {"id": thread_id, "agent_id": None})

    monkeypatch.setattr(orch_main, "require_thread_owner", _fake_owner)
    fake_db = MagicMock()
    fake_db.get_live_thread_message = AsyncMock(
        return_value={"seq": 8, "role": "human", "content": "the prompt"}
    )
    fake_db.apply_thread_rewind = AsyncMock(
        return_value={"rewind_id": "r1", "swept": 3, "surviving_turn": 1}
    )
    monkeypatch.setattr(orch_main, "postgres_db", fake_db)

    out = await orch_main.rewind_thread_detached(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        MagicMock(),
        orch_main.ThreadRewindRequest(message_id="m1", mode="conversation"),
    )
    assert out == {"rewind_id": "r1", "swept": 3, "prompt": "the prompt"}
    fake_db.apply_thread_rewind.assert_awaited_once_with(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", from_seq=8, actor="user-1"
    )
```

(If `pytest.mark.anyio` isn't the house pattern in nearby endpoint tests, use `asyncio.run(...)` wrappers exactly as in the sync tests above.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rewind_orchestrator.py -v`
Expected: FAIL — missing filters, missing `apply_thread_rewind`, missing endpoint.

- [ ] **Step 3: Add the four orchestrator-side filters**

In `orchestrator/database/postgres.py`:

1. `get_thread_messages_history` (line 7871): `"WHERE thread_id = $1 "` → `"WHERE thread_id = $1 AND rewound_at IS NULL "`.
2. `get_thread_messages_page`: in the `clauses` construction (~line 7909), change `clauses = ["thread_id = $1"]` → `clauses = ["thread_id = $1", "rewound_at IS NULL"]`.
3. `get_thread_message_count` (line 7951): append `AND rewound_at IS NULL` to its WHERE.
4. `get_officer_last_engagement` (line 6580): the `thread_messages` arm becomes `(SELECT MAX(created_at) FROM thread_messages WHERE thread_id = $1 AND rewound_at IS NULL),`.

- [ ] **Step 4: Implement `get_live_thread_message` + `apply_thread_rewind`**

Add after `get_thread_messages_history` in `orchestrator/database/postgres.py`:

```python
    async def get_live_thread_message(
        self, thread_id: str, message_id: str
    ) -> Optional[Dict[str, Any]]:
        """Rewind-target lookup: the row must exist and not be tombstoned."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT seq, role, content FROM thread_messages "
                "WHERE thread_id = $1 AND id = $2 AND rewound_at IS NULL",
                thread_id,
                message_id,
            )
        if row is None:
            return None
        return {"seq": row["seq"], "role": row["role"], "content": row["content"]}

    async def apply_thread_rewind(
        self, thread_id: str, from_seq: int, actor: str
    ) -> Dict[str, Any]:
        """Detached (conversation-only) rewind, orchestrator-side.

        One advisory-locked transaction: tombstone sweep, thread_rewinds
        ledger, events_epoch bump (so any open SSE viewer takes the
        gone_beyond_horizon repaint), and a rewind.done thread_events row in
        the NEW epoch (so those viewers also clear their IndexedDB message
        cache — the repaint alone merges append-only and would keep showing
        the swept rows). Writing thread_events here is safe precisely
        because the thread is detached: there is no agent event-writer to
        collide with.
        """
        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"thread_rewind:{thread_id}",
                )
                swept = await conn.fetchval(
                    """
                    WITH swept AS (
                        UPDATE thread_messages
                        SET rewound_at = now()
                        WHERE thread_id = $1
                          AND seq >= $2
                          AND rewound_at IS NULL
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM swept
                    """,
                    thread_id,
                    from_seq,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO thread_rewinds
                        (thread_id, from_seq, mode, actor, swept_count)
                    VALUES ($1, $2, 'conversation', $3, $4)
                    RETURNING id
                    """,
                    thread_id,
                    from_seq,
                    actor,
                    swept or 0,
                )
                surviving_turn = await conn.fetchval(
                    """
                    SELECT COALESCE(MAX(turn_number), 0)
                    FROM thread_messages
                    WHERE thread_id = $1
                      AND rewound_at IS NULL
                      AND role NOT IN ('summary', 'error')
                    """,
                    thread_id,
                )
                new_epoch = await conn.fetchval(
                    """
                    UPDATE threads
                    SET events_epoch = events_epoch + 1
                    WHERE id = $1
                    RETURNING events_epoch
                    """,
                    thread_id,
                )
                await conn.execute(
                    """
                    INSERT INTO thread_events (thread_id, epoch, seq, kind, payload)
                    VALUES ($1, $2, 1, 'rewind.done',
                            jsonb_build_object('mode', 'conversation'))
                    """,
                    thread_id,
                    new_epoch,
                )
        return {
            "rewind_id": str(row["id"]),
            "swept": int(swept or 0),
            "surviving_turn": int(surviving_turn or 0),
        }
```

**asyncpg PREPARE hazard check (house rule):** `hashtext($1)` receives a plain text argument built in Python — typed context, safe. No `$n` appears only inside `IS NULL`/`NULLIF`.

- [ ] **Step 5: Implement the endpoint**

In `orchestrator/main.py`, near `resume_thread` (line 29389). Request model next to the other `BaseModel`s in that region:

```python
class ThreadRewindRequest(BaseModel):
    """Body for POST /api/agents/threads/{id}/rewind (detached sessions)."""

    message_id: str
    mode: str = "conversation"
```

```python
@app.post("/api/agents/threads/{thread_id}/rewind")
async def rewind_thread_detached(
    thread_id: str,
    request: Request,
    body: ThreadRewindRequest,
) -> dict[str, Any]:
    """Rewind a DETACHED session's transcript (auth: owner only).

    docs/features/session_rewind.md §Flow — detached. Conversation mode
    only: file restore needs the agent that holds the workspace, so live
    sessions rewind through the session WebSocket instead, and code modes
    here answer 400 ("resume first"). A bound agent means the in-memory
    authority is live and a DB-only sweep would diverge it → 409.
    """
    user, thread = await require_thread_owner(request, postgres_db, thread_id)
    if thread.get("agent_id"):
        raise HTTPException(
            status_code=409,
            detail="Session is live — rewind from the session connection",
        )
    if body.mode != "conversation":
        raise HTTPException(
            status_code=400,
            detail="File restore needs a running session — resume it first, "
            "then rewind from the chat",
        )
    row = await postgres_db.get_live_thread_message(thread_id, body.message_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Message not found (it may already be rewound)",
        )
    if row["role"] != "human":
        raise HTTPException(
            status_code=400, detail="Rewind targets must be user messages"
        )
    result = await postgres_db.apply_thread_rewind(
        thread_id, from_seq=row["seq"], actor=str(user["id"])
    )
    return {
        "rewind_id": result["rewind_id"],
        "swept": result["swept"],
        "prompt": row["content"] or "",
    }
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_rewind_orchestrator.py -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Ruff + commit**

```bash
ruff format orchestrator/database/postgres.py orchestrator/main.py tests/test_rewind_orchestrator.py
ruff check orchestrator/database/postgres.py orchestrator/main.py tests/test_rewind_orchestrator.py
git add orchestrator/database/postgres.py orchestrator/main.py tests/test_rewind_orchestrator.py
git commit -m "feat(rewind): detached-session rewind endpoint + orchestrator-side tombstone filters"
```

---

### Task 8: Cockpit service — `rewind()`, acks, cache truncation, composer handoff

**Files:**
- Modify: `cockpit/src/app/core/services/persistent-chat.service.ts` — new public method near `updateConfig` (line 2907); new `_handleEvent` cases near `files.restored` (line 3626); new signal near `undoAvailable`
- Test: `cockpit/src/app/core/services/persistent-chat.service.rewind.spec.ts` (create; vitest)

**Interfaces:**
- Consumes: Task 5's wire contract (`rewind` frame, `rewind.ack`/`rewind.done`/`rewind.files_restored`/`error` events), `IndexedDbService.clearThreadMessages` / `deleteThreadCursor` (`indexed-db.service.ts:458/417`).
- Produces (Task 9 consumes):
  - `rewind(messageId: string, mode: 'both' | 'conversation' | 'code'): string` — sends the frame, returns the `request_id`
  - `summarizeUpTo(messageId: string): void` — sends `{method: 'compact', focus: '', boundary_message_id}`
  - `readonly rewindPrefill = signal<string | null>(null)` — set from `rewind.ack`; the component consumes and clears it
  - `readonly rewindInFlight = signal<boolean>(false)`

- [ ] **Step 1: Write the failing test**

Create `cockpit/src/app/core/services/persistent-chat.service.rewind.spec.ts`. The service has heavy constructor dependencies — test the pure seams the same way neighboring specs do (if a `persistent-chat.service.spec.ts` exists, copy its TestBed/instantiation harness; otherwise test via a minimal manual instance with stubbed deps):

```typescript
import {describe, expect, it, vi} from 'vitest';

import {PersistentChatService} from './persistent-chat.service';

function makeService(): PersistentChatService {
    // Bypass Angular DI — we only exercise frame construction + ack handling.
    const svc = Object.create(PersistentChatService.prototype) as PersistentChatService;
    (svc as any).controlWs = null;
    (svc as any).controlOutbox = [];
    (svc as any).intentionalClose = false;
    (svc as any).sentFrames = [];
    (svc as any)._sendControl = function (data: Record<string, unknown>) {
        (this as any).sentFrames.push(data);
    };
    return svc;
}

describe('PersistentChatService rewind', () => {
    it('sends a flat rewind frame with a request_id and flags in-flight', () => {
        const svc = makeService();
        (svc as any).rewindInFlight = {set: vi.fn()};
        const requestId = svc.rewind('row-1', 'conversation');
        const frame = (svc as any).sentFrames[0];
        expect(frame.method).toBe('rewind');
        expect(frame.message_id).toBe('row-1');
        expect(frame.mode).toBe('conversation');
        expect(frame.request_id).toBe(requestId);
        expect(frame.params).toBeUndefined();
    });

    it('summarizeUpTo rides the compact verb with a boundary', () => {
        const svc = makeService();
        svc.summarizeUpTo('row-2');
        const frame = (svc as any).sentFrames[0];
        expect(frame.method).toBe('compact');
        expect(frame.boundary_message_id).toBe('row-2');
    });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/core/services/persistent-chat.service.rewind.spec.ts`
Expected: FAIL — `svc.rewind is not a function`.

- [ ] **Step 3: Implement the service surface**

In `persistent-chat.service.ts`:

1. Signals, next to `undoAvailable`:

```typescript
    /** Prompt text handed back by rewind.ack — the component moves it into
     *  the composer (edit-and-resend) and clears the signal. */
    readonly rewindPrefill = signal<string | null>(null);
    readonly rewindInFlight = signal<boolean>(false);
```

2. Public methods, next to `updateConfig` (line 2907), same request_id pattern:

```typescript
    /** Rewind the session to just before an earlier user message.
     *  Returns the request_id echoed on the rewind.ack / error frame. */
    rewind(messageId: string, mode: 'both' | 'conversation' | 'code'): string {
        const requestId = crypto.randomUUID();
        this.rewindInFlight.set(true);
        this._sendControl({
            method: 'rewind',
            message_id: messageId,
            mode,
            request_id: requestId,
        });
        return requestId;
    }

    /** "Summarize up to here" — manual compaction bounded at a message. */
    summarizeUpTo(messageId: string): void {
        this._sendControl({
            method: 'compact',
            focus: '',
            boundary_message_id: messageId,
        });
    }
```

3. `_handleEvent` cases, next to `files.restored` (line 3626):

```typescript
            case 'rewind.ack': {
                this.rewindInFlight.set(false);
                const prompt = params['prompt'] as string | undefined;
                if (prompt) this.rewindPrefill.set(prompt);
                // Truncate-then-reload: the IndexedDB cache is append-only
                // (loadHistory merges ?after=), so tombstoned rows must be
                // dropped explicitly or they re-render forever.
                void this._reloadAfterRewind();
                break;
            }

            case 'rewind.done': {
                // Journaled all-viewer signal (arrives via SSE in the new
                // epoch). Idempotent with the initiator's ack-driven reload.
                void this._reloadAfterRewind();
                break;
            }

            case 'rewind.files_restored': {
                this.rewindInFlight.set(false);
                this._systemMessage('Workspace files restored to the selected point.');
                break;
            }
```

4. The reload helper, next to `_handleGoneBeyondHorizon` (line 1600):

```typescript
    /** Full transcript repaint after a rewind: drop the (append-only)
     *  cache + cursor, then reload from the server's filtered history. */
    private async _reloadAfterRewind(): Promise<void> {
        const tid = this.threadId();
        if (!tid) return;
        const generation = this.connectGeneration;
        await this.cache.clearThreadMessages(tid);
        await this.cache.deleteThreadCursor(tid);
        if (!this._isCurrentConnect(tid, generation)) return;
        await this.loadHistory(tid, generation);
    }
```

5. In the `case 'error'` handler (line 3678), add one line so a rewind error clears the in-flight flag:

```typescript
            case 'error': {
                this.rewindInFlight.set(false);
                const detail = params['detail'] as string | undefined;
                // …existing body unchanged…
```

- [ ] **Step 4: Run tests + typecheck**

Run: `cd cockpit && npx vitest run src/app/core/services/persistent-chat.service.rewind.spec.ts && npx ng build --configuration development 2>&1 | tail -5`
Expected: tests PASS; build compiles. (Full `ng build` needs `@monaco-editor/loader` present — `npm ci` first if node_modules is stale.)

- [ ] **Step 5: Commit**

```bash
git add cockpit/src/app/core/services/persistent-chat.service.ts \
        cockpit/src/app/core/services/persistent-chat.service.rewind.spec.ts
git commit -m "feat(rewind): cockpit service — rewind verb, ack handling, cache truncation"
```

---

### Task 9: Cockpit UI — inline affordance, action dialog, composer refill

**Files:**
- Modify: `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` — user-turn template block (lines 1118-1180), dialog near the canvas-replacement precedent, state + handlers near `denyOffer` (line 3187)
- Modify: `cockpit/src/app/views/persistent-chat/persistent-chat.component.scss` — hover affordance near `.message-user` (line 645)
- Modify: `cockpit/src/assets/i18n/en.json` — new `chat.rewind.*` keys (and mirror the keys into every other locale file present in `cockpit/src/assets/i18n/`, English values as placeholders, matching how new keys land today)

**Interfaces:**
- Consumes: `chat.rewind()` / `chat.summarizeUpTo()` / `chat.rewindPrefill` / `chat.rewindInFlight` (Task 8), `AppDialogComponent` (`ui/dialog/dialog.component.ts`), `saveDraft` + `inputEl` + `autoResizeInput` (this component), `turn.historical` + `chat.outboxIds()`.
- Produces: the user-visible feature; no downstream consumers.

- [ ] **Step 1: Add the hover affordance to the user bubble**

In the `@case ('user')` block (insert inside the bubble div, after the `message-body` div, ~line 1151) — gated so optimistic local bubbles (non-resolvable ids) never show it:

```html
                @if (turn.historical && !chat.outboxIds().has(turn.id)) {
                  <button type="button"
                          class="rewind-btn"
                          [attr.aria-label]="'chat.rewind.button' | transloco"
                          [title]="'chat.rewind.button' | transloco"
                          (click)="openRewindSheet(turn)">
                    <app-icon size="sm">history</app-icon>
                  </button>
                }
```

SCSS, next to the `.message-user` rules (line 645):

```scss
  .rewind-btn {
    opacity: 0;
    transition: opacity 0.15s ease;
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-secondary);
    padding: 2px;
    align-self: center;

    &:hover {
      color: var(--text-primary);
    }
  }

  .message-user:hover .rewind-btn,
  .rewind-btn:focus-visible {
    opacity: 1;
  }
```

- [ ] **Step 2: Add the action dialog**

Component state + handlers (near `denyOffer`, line 3187):

```typescript
    /** The user turn the rewind sheet is open for (null = closed). */
    rewindTarget = signal<UserTurn | null>(null);

    openRewindSheet(turn: UserTurn): void {
        this.rewindTarget.set(turn);
    }

    closeRewindSheet(): void {
        this.rewindTarget.set(null);
    }

    confirmRewind(mode: 'both' | 'conversation' | 'code'): void {
        const target = this.rewindTarget();
        if (!target) return;
        this.chat.rewind(target.id, mode);
        this.rewindTarget.set(null);
    }

    confirmSummarizeUpTo(): void {
        const target = this.rewindTarget();
        if (!target) return;
        this.chat.summarizeUpTo(target.id);
        this.rewindTarget.set(null);
    }

    /** First 160 chars of the target prompt for the dialog header quote
     *  (avoids importing SlicePipe into this standalone component). */
    rewindQuote(): string {
        const text = this.rewindTarget()?.content || '';
        return text.length > 160 ? text.slice(0, 160) + '…' : text;
    }
```

(`UserTurn` is already imported in this file's model imports; add it if the existing import list lacks it: `import {…, UserTurn} from '../../core/models/turn.model';`)

Template — place next to the existing dialogs, following the canvas-replacement precedent (`chat-page.component.ts:140-154` shape; `AppDialogComponent` + `AppButtonComponent` are the house dialog kit — add `AppDialogComponent` to this component's `imports` array if absent):

```html
      <app-dialog
        [open]="rewindTarget() !== null"
        (closed)="closeRewindSheet()"
        [title]="'chat.rewind.title' | transloco"
        size="sm">
        <p class="rewind-quote">"{{ rewindQuote() }}"</p>
        <p>{{ 'chat.rewind.body' | transloco }}</p>
        <p class="rewind-caveat">{{ 'chat.rewind.caveat' | transloco }}</p>
        <ng-container appDialogActions>
          <app-button variant="warning" size="sm"
                      [disabled]="chat.rewindInFlight()"
                      (clicked)="confirmRewind('both')">
            {{ 'chat.rewind.both' | transloco }}
          </app-button>
          <app-button variant="warning" size="sm"
                      [disabled]="chat.rewindInFlight()"
                      (clicked)="confirmRewind('conversation')">
            {{ 'chat.rewind.conversation' | transloco }}
          </app-button>
          <app-button variant="info" size="sm"
                      [disabled]="chat.rewindInFlight()"
                      (clicked)="confirmRewind('code')">
            {{ 'chat.rewind.code' | transloco }}
          </app-button>
          <app-button variant="info" size="sm"
                      (clicked)="confirmSummarizeUpTo()">
            {{ 'chat.rewind.summarize' | transloco }}
          </app-button>
          <app-button variant="ghost" size="sm" (clicked)="closeRewindSheet()">
            {{ 'common.cancel' | transloco }}
          </app-button>
        </ng-container>
      </app-dialog>
```

Degraded modes are server-authoritative in v1: a lite/no-git session or an unmapped target answers the code buttons with an `error` frame, surfaced through the existing error banner. (Client-side pre-disabling by tier is a fast-follow — it needs a tier signal this component doesn't expose today.)

- [ ] **Step 3: Composer refill effect**

In the constructor (next to the draft-restore effect, ~line 2339):

```typescript
        // Rewind hands the un-sent prompt back for edit-and-resend. Plain
        // ngModel field: assign + saveDraft by hand (no ngModelChange fires),
        // same trap denyOffer documents.
        effect(() => {
            const prompt = this.chat.rewindPrefill();
            if (prompt === null) return;
            this.inputText = prompt;
            saveDraft(this.chat.threadId(), this.inputText);
            this.chat.rewindPrefill.set(null);
            setTimeout(() => {
                this.inputEl?.nativeElement?.focus();
                this.autoResizeInput();
            });
        });
```

- [ ] **Step 4: i18n keys**

In `cockpit/src/assets/i18n/en.json`, inside the `chat` object:

```json
    "rewind": {
      "button": "Rewind to here",
      "title": "Rewind session",
      "body": "Go back to before this message? Messages after this point are hidden from the conversation (kept in the audit trail). Files return to their state at that point.",
      "caveat": "Commands with external effects (network calls, deployments) can't be undone.",
      "both": "Restore conversation and files",
      "conversation": "Restore conversation only",
      "code": "Restore files only",
      "summarize": "Summarize up to here"
    }
```

Mirror the same keys into the other locale files in `cockpit/src/assets/i18n/` (English values).

- [ ] **Step 5: Build + vitest**

Run: `cd cockpit && npx vitest run && npx ng build 2>&1 | tail -10`
Expected: vitest green; production build passes bundle budgets (this adds ~2 KB of template/SCSS — nowhere near budget).

- [ ] **Step 6: Commit**

```bash
git add cockpit/src/app/views/persistent-chat/persistent-chat.component.ts \
        cockpit/src/app/views/persistent-chat/persistent-chat.component.scss \
        cockpit/src/assets/i18n/
git commit -m "feat(rewind): cockpit UI — inline rewind affordance, action dialog, composer refill"
```

---

### Task 10: Docs + dev live gate

**Files:**
- Modify: `docs/features/session_rewind.md` (status header)
- No code.

- [ ] **Step 1: Update the feature doc status**

In `docs/features/session_rewind.md`, change the status line to:

```markdown
> **Status (2026-08-XX): IMPLEMENTED on develop — dev live gate pending.**
```

(fill the real date), and append a "Live gate checklist" section:

```markdown
## Live gate checklist (dev)

Run against a real dev session before calling this shipped:

1. Sandbox session, ≥4 turns with file edits → rewind (both) to turn 2:
   transcript truncates, composer prefills, files revert, Gitea history is
   LINEAR (snapshot + restore commits, no force-push), turn_count resumes at 2.
2. Second browser tab on the same thread repaints (no stale tail from
   IndexedDB) after the rewind.
3. Rewind mid-stream: the in-flight turn interrupts first.
4. Deep rewind: force a compaction (/compact), then rewind past the boundary
   → rehydrate path, summary row superseded, no dangling banner.
5. Detached: end the session → POST /api/agents/threads/{id}/rewind
   (conversation) → 200; re-open the thread → truncated history; code mode →
   400; live thread → 409.
6. Lite session: code buttons answer with the no-version-history error;
   conversation rewind works.
7. Summarize up to here: banner appears, earlier turns fold into the summary,
   the chosen message and everything after stay verbatim.
8. asyncpg smoke: watch orchestrator + agent logs for PREPARE errors on the
   new statements during 1-7.
```

- [ ] **Step 2: Commit**

```bash
git add docs/features/session_rewind.md
git commit -m "docs(rewind): implementation status + dev live-gate checklist"
```

---

## Self-review notes (kept for the executor)

- **Spec coverage:** every §Decided-design element maps to a task — data model (1), filters (2, 7), forward-restore (3), turn→commit map (4), attached flow + epoch + acks (5), summarize (6), detached flow (7), cache-truncation + UI + composer (8, 9), live gate (10). The spec's "owner-only, both surfaces" is enforced by `require_thread_owner` on REST and by the session-JWT (`_validate_session_token`, tid-bound) on WS — the WS path has no per-verb owner check, same trust model as the existing `archive`/`undo` verbs.
- **Deliberate deviation from spec:** the cockpit has no non-owner viewer concept today (no `isOwner` signal exists), so "non-owner viewers see no affordance" is vacuously satisfied and no gating UI is built. Client-side pre-disabling of code modes by tier is deferred to a fast-follow; the server is authoritative either way.
- **Type consistency spot-checks:** `rewound_at` name is identical in migration/filters/tests; `apply_rewind` (agent) vs `apply_thread_rewind` (orchestrator) are deliberately distinct names for distinct contracts (the orchestrator one also bumps the epoch + journals); `rewind.ack`/`rewind.done`/`rewind.files_restored` kinds match between Task 5 (producer) and Task 8 (consumer); `thread_turn_commits` column set matches between Task 1 DDL and Task 2 SQL.
- **Known post-plan risks** (verify while executing): the exact `GitManager` constructor in Task 3's test (mirror `tests/test_tools_git.py`); whether `effect()` in Task 9 needs an injector context in this component (move into constructor body if so); vitest harness availability for the service spec (fall back to the manual-instance pattern shown).
