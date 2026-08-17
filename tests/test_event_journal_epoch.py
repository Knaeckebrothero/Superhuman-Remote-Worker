"""Stateless agents M2 — conditional epoch reuse, fenced writer, system frames.

Covers the knowledge-base/knowledge/features/stateless_agents.md §5.3.2 redesign:
  - _resolve_event_journal_epoch returns (epoch, seq_seed): REUSE on clean
    reattach with seed = GREATEST(events_seq_hwm, MAX(seq)); BUMP on terminal
    status / terminal lifecycle frame / beyond-retention epoch / probe failure.
  - _OrderedPersistentEventWriter owns its epoch and fences every flush on
    threads.events_epoch; a fenced-out or epoch-mixed batch is terminal and
    stops the writer.
  - src.shared.event_journal: append_system_frame (hwm-allocating one-statement
    CTE) and bump_epoch (epoch+1, hwm=0) — the shared contract the M4 reaper
    imports.
  - _bump_event_journal_epoch + the seq-reset arithmetic that puts the first
    post-rewind broadcast at (new_epoch, 1).

No real database: a scripted fake asyncpg connection records (method, sql,
args) and serves queued results — same shape as the ad-hoc fakes in
tests/test_thread_events_phase2.py, with scripting added.
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from src.shared import event_journal


# ---------------------------------------------------------------------------
# Scripted fake connection / pool
# ---------------------------------------------------------------------------


class ScriptedConn:
    """Fake asyncpg connection: records calls, serves FIFO-scripted results.

    ``fail_on`` maps an SQL substring to an exception raised when a statement
    containing it executes — used to script the seed-probe failure path.
    """

    def __init__(self):
        self.calls = []  # (method, sql, args)
        self.fetchrow_results = []
        self.fetchval_results = []
        self.fail_on = {}

    def _maybe_fail(self, sql):
        for fragment, exc in self.fail_on.items():
            if fragment in sql:
                raise exc

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        self._maybe_fail(sql)
        return self.fetchrow_results.pop(0)

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        self._maybe_fail(sql)
        return self.fetchval_results.pop(0)

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        self._maybe_fail(sql)
        return "UPDATE 1"

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]


def _pool(conn):
    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    pool = MagicMock()
    pool.acquire = lambda: _Acquire()
    return pool


def _thread_row(epoch, hwm, status):
    return {"events_epoch": epoch, "events_seq_hwm": hwm, "status": status}


# ---------------------------------------------------------------------------
# 1. _resolve_event_journal_epoch — REUSE paths
# ---------------------------------------------------------------------------


class TestEpochReuse:
    @pytest.mark.asyncio
    async def test_reuse_seeds_from_hwm_when_hwm_above_max(self):
        """Retention pruned the tail's rows; the mark keeps the seed above
        every seq a client ever saw. No correction UPDATE is issued."""
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [_thread_row(3, 500, "active")]
        conn.fetchval_results = [False, 200]  # terminal-frame EXISTS, MAX(seq)

        epoch, seed = await mod._resolve_event_journal_epoch(_pool(conn), "t-1")

        assert (epoch, seed) == (3, 500)
        assert conn.calls_of("execute") == []
        assert len(conn.calls_of("fetchrow")) == 1  # no bump statement

    @pytest.mark.asyncio
    async def test_reuse_seeds_from_max_and_persists_hwm_correction(self):
        """Pre-0116 rows can put MAX above the mark; the correction UPDATE
        makes the mark authoritative again."""
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [_thread_row(3, 10, "active")]
        conn.fetchval_results = [False, 42]

        epoch, seed = await mod._resolve_event_journal_epoch(_pool(conn), "t-2")

        assert (epoch, seed) == (3, 42)
        executes = conn.calls_of("execute")
        assert len(executes) == 1
        sql, args = executes[0][1], executes[0][2]
        assert "events_seq_hwm = $2" in sql
        assert "events_seq_hwm < $2" in sql
        assert args == ("t-2", 42)

    @pytest.mark.asyncio
    async def test_virgin_thread_reuses_epoch_zero_with_seed_zero(self):
        """First attach of a never-journaled thread: epoch stays 0 — no
        pointless generation burn, and no client holds any cursor to serve."""
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [_thread_row(0, 0, "created")]
        conn.fetchval_results = [False, None]

        epoch, seed = await mod._resolve_event_journal_epoch(_pool(conn), "t-3")

        assert (epoch, seed) == (0, 0)
        assert conn.calls_of("execute") == []

    @pytest.mark.asyncio
    async def test_reuse_probes_terminal_kinds_in_cockpit_lockstep(self):
        """The EXISTS probe must carry exactly the kinds the cockpit's
        _isSupersededLifecycleFrame-guarded handlers listen for."""
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [_thread_row(5, 7, "awaiting_user")]
        conn.fetchval_results = [False, 7]

        await mod._resolve_event_journal_epoch(_pool(conn), "t-4")

        exists_call = conn.calls_of("fetchval")[0]
        assert "EXISTS" in exists_call[1]
        assert exists_call[2] == (
            "t-4",
            5,
            ["session.ended", "session.idle_timeout"],
        )
        assert mod._TERMINAL_LIFECYCLE_EVENT_KINDS == (
            "session.ended",
            "session.idle_timeout",
        )


# ---------------------------------------------------------------------------
# 2. _resolve_event_journal_epoch — BUMP paths
# ---------------------------------------------------------------------------


class TestEpochBump:
    @pytest.mark.asyncio
    async def test_terminal_status_bumps_without_probing_frames(self):
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [
            _thread_row(6, 12, "ended"),
            {"events_epoch": 7},  # bump statement RETURNING
        ]

        epoch, seed = await mod._resolve_event_journal_epoch(_pool(conn), "t-5")

        assert (epoch, seed) == (7, 0)
        assert conn.calls_of("fetchval") == []  # status short-circuits
        bump_sql = conn.calls_of("fetchrow")[1][1]
        assert "events_epoch = events_epoch + 1" in bump_sql
        assert "events_seq_hwm = 0" in bump_sql

    @pytest.mark.asyncio
    async def test_terminal_lifecycle_frame_in_epoch_bumps(self):
        """A resumed thread ('created' again) whose epoch still carries
        session.ended/idle_timeout must move to a higher epoch — otherwise the
        cockpit's resumedFromEpoch guard swallows the next life's terminal
        frames forever."""
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [
            _thread_row(6, 12, "created"),
            {"events_epoch": 7},
        ]
        conn.fetchval_results = [True]  # EXISTS → terminal frame present

        epoch, seed = await mod._resolve_event_journal_epoch(_pool(conn), "t-6")

        assert (epoch, seed) == (7, 0)
        assert len(conn.calls_of("fetchval")) == 1  # no MAX read after EXISTS

    @pytest.mark.asyncio
    async def test_beyond_retention_epoch_bumps_even_without_terminal_signal(self):
        """hwm > 0 with zero surviving rows: the epoch's whole history is
        past retention, so no client cursor can be served — bump."""
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [
            _thread_row(2, 77, "active"),
            {"events_epoch": 3},
        ]
        conn.fetchval_results = [False, None]

        epoch, seed = await mod._resolve_event_journal_epoch(_pool(conn), "t-7")

        assert (epoch, seed) == (3, 0)

    @pytest.mark.asyncio
    async def test_pruned_pre_0116_epoch_with_zero_backfill_bumps(self):
        """Cutover poison: an old epoch fully pruned before 0116 backfills
        hwm=0. Reusing it would seed at 0, below any cached cursor (dead
        polls). epoch > 0 marks it non-virgin → bump."""
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [
            _thread_row(4, 0, "created"),
            {"events_epoch": 5},
        ]
        conn.fetchval_results = [False, None]

        epoch, seed = await mod._resolve_event_journal_epoch(_pool(conn), "t-8")

        assert (epoch, seed) == (5, 0)

    @pytest.mark.asyncio
    async def test_seed_probe_failure_falls_back_to_bump(self):
        """Never reuse an epoch we could not read: MAX/EXISTS errors bump."""
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [
            _thread_row(6, 12, "active"),
            {"events_epoch": 7},
        ]
        conn.fail_on["MAX(seq)"] = RuntimeError("probe lost the connection")
        conn.fetchval_results = [False, "unused"]

        epoch, seed = await mod._resolve_event_journal_epoch(_pool(conn), "t-9")

        assert (epoch, seed) == (7, 0)

    @pytest.mark.asyncio
    async def test_missing_thread_fails_closed(self):
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [None]

        with pytest.raises(mod.EventJournalUnavailable, match="does not exist"):
            await mod._resolve_event_journal_epoch(_pool(conn), "t-10")

    @pytest.mark.asyncio
    async def test_thread_vanishing_before_bump_fails_closed(self):
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [_thread_row(1, 5, "ended"), None]

        with pytest.raises(mod.EventJournalUnavailable, match="does not exist"):
            await mod._resolve_event_journal_epoch(_pool(conn), "t-11")

    @pytest.mark.asyncio
    async def test_database_error_is_wrapped_as_journal_unavailable(self):
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fail_on["FROM threads"] = RuntimeError("database offline")
        conn.fetchrow_results = ["unused"]

        with pytest.raises(mod.EventJournalUnavailable, match="initialization failed"):
            await mod._resolve_event_journal_epoch(_pool(conn), "t-12")


# ---------------------------------------------------------------------------
# 3. _OrderedPersistentEventWriter — writer-owned epoch + fenced flush
# ---------------------------------------------------------------------------


class TestFencedWriter:
    def _writer(self, mod, conn, failures, epoch=7, **kwargs):
        return mod._OrderedPersistentEventWriter(
            postgres_conn=_pool(conn),
            thread_id="t-writer",
            epoch=epoch,
            on_terminal_failure=lambda events, reason: failures.append(
                (list(events), reason)
            ),
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_flush_carries_writer_epoch_and_updates_hwm_in_statement(self):
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchval_results = [2]
        failures = []
        writer = self._writer(mod, conn, failures, epoch=7)
        writer.start()
        assert writer.enqueue(mod._QueuedPersistentEvent(7, 1, "token", {"c": "a"}))
        assert writer.enqueue(mod._QueuedPersistentEvent(7, 2, "token", {"c": "b"}))
        await writer.close()

        assert failures == []
        flushes = conn.calls_of("fetchval")
        assert len(flushes) == 1
        sql, args = flushes[0][1], flushes[0][2]
        assert "events_epoch = $3" in sql
        assert "GREATEST" in sql and "events_seq_hwm" in sql
        assert args[0] == "t-writer"
        assert args[2] == 7
        rows = json.loads(args[1])
        assert [(r["epoch"], r["seq"]) for r in rows] == [(7, 1), (7, 2)]

    @pytest.mark.asyncio
    async def test_fenced_out_flush_is_terminal_and_stops_the_writer(self):
        """Zero rows inserted on a non-empty batch = the epoch moved
        underneath us. No retry, loud failure, and the writer accepts nothing
        further — a superseded runtime must never keep writing."""
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchval_results = [0]
        failures = []
        writer = self._writer(mod, conn, failures, epoch=7)
        writer.start()
        assert writer.enqueue(mod._QueuedPersistentEvent(7, 1, "token", {"c": "x"}))
        for _ in range(20):
            await asyncio.sleep(0)
            if failures:
                break

        assert [reason for _e, reason in failures] == ["epoch_fenced"]
        assert len(conn.calls_of("fetchval")) == 1  # deterministic — no retry

        # Post-fence enqueue is refused and reported, not silently queued.
        assert not writer.enqueue(mod._QueuedPersistentEvent(7, 2, "token", {"c": "y"}))
        assert failures[-1][1] == "writer_unavailable"
        await writer.close()
        assert len(conn.calls_of("fetchval")) == 1

    @pytest.mark.asyncio
    async def test_mixed_epoch_batch_is_terminal_and_stops_the_writer(self):
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchval_results = [2]  # would succeed if it ever reached SQL
        failures = []
        writer = self._writer(mod, conn, failures, epoch=7)
        writer.start()
        assert writer.enqueue(mod._QueuedPersistentEvent(7, 5, "token", {"c": "a"}))
        assert writer.enqueue(mod._QueuedPersistentEvent(8, 1, "token", {"c": "b"}))
        for _ in range(20):
            await asyncio.sleep(0)
            if failures:
                break

        assert [reason for _e, reason in failures] == ["epoch_mismatch"]
        assert conn.calls_of("fetchval") == []  # rejected before any SQL
        assert not writer.enqueue(mod._QueuedPersistentEvent(8, 2, "token", {"c": "c"}))
        await writer.close()

    def test_writer_requires_a_non_negative_epoch(self):
        import src.api.persistent_app as mod

        with pytest.raises(ValueError, match="epoch"):
            mod._OrderedPersistentEventWriter(
                postgres_conn=MagicMock(),
                thread_id="t",
                epoch=-1,
                on_terminal_failure=lambda e, r: None,
            )


# ---------------------------------------------------------------------------
# 4. src.shared.event_journal — system frames + shared bump
# ---------------------------------------------------------------------------


class TestSharedEventJournal:
    @pytest.mark.asyncio
    async def test_append_system_frame_allocates_from_hwm_in_one_statement(self):
        conn = ScriptedConn()
        conn.fetchrow_results = [{"epoch": 3, "seq": 42}]

        result = await event_journal.append_system_frame(
            conn,
            thread_id="t-sys",
            kind="turn.interrupted",
            payload={"reason": "lease_stolen"},
        )

        assert result == (3, 42)
        assert len(conn.calls) == 1  # single statement, single round-trip
        method, sql, args = conn.calls[0]
        assert method == "fetchrow"
        assert "events_seq_hwm = events_seq_hwm + 1" in sql
        assert "INSERT INTO thread_events" in sql
        assert args[0] == "t-sys"
        assert args[1] == "turn.interrupted"
        assert json.loads(args[2]) == {"reason": "lease_stolen"}
        assert args[3] is None
        assert args[4] is None

    @pytest.mark.asyncio
    async def test_append_system_frame_links_interrupt_receipt(self):
        conn = ScriptedConn()
        conn.fetchrow_results = [{"epoch": 7, "seq": 3}]
        request_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

        result = await event_journal.append_system_frame(
            conn,
            thread_id="t-sys",
            kind="interrupt.ack",
            payload={"applied": False, "error_code": "lease_expired"},
            interrupt_request_id=request_id,
        )

        assert result == (7, 3)
        _method, sql, args = conn.calls[0]
        assert "interrupt_request_id" in sql
        assert "$4::uuid" in sql
        assert args[3] == request_id
        assert args[4] is None

    @pytest.mark.asyncio
    async def test_append_system_frame_links_permission_receipt(self):
        conn = ScriptedConn()
        conn.fetchrow_results = [{"epoch": 9, "seq": 6}]
        request_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

        result = await event_journal.append_system_frame(
            conn,
            thread_id="t-sys",
            kind="permission.resolved",
            payload={"decision": "expired"},
            permission_request_id=request_id,
        )

        assert result == (9, 6)
        _method, sql, args = conn.calls[0]
        assert "permission_request_id" in sql
        assert "$5::uuid" in sql
        assert args[3] is None
        assert args[4] == request_id

    @pytest.mark.asyncio
    async def test_append_system_frame_returns_none_for_missing_thread(self):
        conn = ScriptedConn()
        conn.fetchrow_results = [None]

        result = await event_journal.append_system_frame(
            conn, thread_id="t-gone", kind="turn.parked", payload={}
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_bump_epoch_resets_hwm_and_returns_new_epoch(self):
        conn = ScriptedConn()
        conn.fetchrow_results = [{"events_epoch": 9}]

        new_epoch = await event_journal.bump_epoch(conn, thread_id="t-bump")

        assert new_epoch == 9
        sql = conn.calls[0][1]
        assert "events_epoch = events_epoch + 1" in sql
        assert "events_seq_hwm = 0" in sql

    @pytest.mark.asyncio
    async def test_bump_epoch_raises_lookup_error_for_missing_thread(self):
        conn = ScriptedConn()
        conn.fetchrow_results = [None]

        with pytest.raises(LookupError):
            await event_journal.bump_epoch(conn, thread_id="t-none")


# ---------------------------------------------------------------------------
# 5. Rewind: deliberate bump wrapper + seq reset arithmetic
# ---------------------------------------------------------------------------


class TestRewindBump:
    @pytest.mark.asyncio
    async def test_bump_wrapper_uses_shared_statement(self):
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [{"events_epoch": 8}]

        new_epoch = await mod._bump_event_journal_epoch(_pool(conn), "t-rw")

        assert new_epoch == 8
        sql = conn.calls_of("fetchrow")[0][1]
        assert "events_epoch = events_epoch + 1" in sql
        assert "events_seq_hwm = 0" in sql

    @pytest.mark.asyncio
    async def test_bump_wrapper_wraps_missing_thread(self):
        import src.api.persistent_app as mod

        conn = ScriptedConn()
        conn.fetchrow_results = [None]

        with pytest.raises(mod.EventJournalUnavailable, match="does not exist"):
            await mod._bump_event_journal_epoch(_pool(conn), "t-rw-none")

    def test_first_broadcast_after_bump_lands_at_new_epoch_seq_one(self):
        """The rewind handler sets (_events_epoch=new, _next_seq=0);
        _broadcast pre-increments, so rewind.done stamps (new_epoch, 1) — the
        row whose flush pushes the hwm to 1 and closes doc §5.3.2 item 5."""
        import src.api.persistent_app as mod

        mod._subscribers.clear()
        mod._session = None  # keeps the journal enqueue out of this test
        mod._events_epoch = 5
        mod._next_seq = 0
        try:
            q = mod._subscribe("viewer")
            mod._broadcast("rewind.done", {"message_id": "m-1", "mode": "both"})
            frame = q.get_nowait()
            assert frame["params"]["_seq"] == [5, 1]
            assert mod._next_seq == 1
        finally:
            mod._subscribers.clear()
            mod._events_epoch = 0
            mod._next_seq = 0
            mod._session = None
