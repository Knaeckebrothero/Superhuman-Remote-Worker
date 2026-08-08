"""Mock-based tests for the M3 stateless turn executor (turn_executor.py).

The run_queue substrate itself is covered by the real-Postgres suite
(tests/test_run_queue.py); everything here fakes the queue functions and the
persistent_app machinery and asserts the DRIVER's behavior: the claim →
bundle → attach → inject → complete flow, skip-if-answered, the error/release
paths, the lease-lost polite abort, the strip-restored-pending helper, the
scrub-on-claim acceptance (§5.6), soft affinity, and the fenced persist in
postgres_db.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

import src.api.persistent_app as pa
import src.api.turn_executor as te
from src.api.lease_context import (
    LeaseHandle,
    LeaseLostError,
    current_lease,
    get_current_lease,
)
from src.api.orchestrator_client import ClaimBundleError
from src.shared.run_queue import ClaimedUnit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_claim(
    unit_id=None,
    token: int = 3,
    input_seq: Optional[int] = 10,
    consumed_seq: Optional[int] = None,
    attempts: int = 1,
) -> ClaimedUnit:
    return ClaimedUnit(
        unit_id=unit_id or uuid4(),
        unit_kind="session_turn",
        fair_key=None,
        lease_token=token,
        input_seq=input_seq,
        consumed_seq=consumed_seq,
        attempts_since_completion=attempts,
        leased_until=datetime.now(timezone.utc),
    )


class FakeDB:
    """Stands in for the agent's PostgresDB pool wrapper."""

    def __init__(self, pending_rows: Optional[List[Dict[str, Any]]] = None):
        self.pending_rows = list(pending_rows or [])
        self.fetch_calls: List[tuple] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return list(self.pending_rows)


class FakeSession:
    def __init__(self):
        self.messages: List[Any] = []
        self.postgres_conn = None


class Harness:
    """Wires fake run_queue functions + fake persistent_app machinery."""

    def __init__(self, monkeypatch, pending_rows=None):
        self.calls: Dict[str, List[Dict[str, Any]]] = {
            "claim": [],
            "complete": [],
            "heartbeat": [],
            "release": [],
            "bundle": [],
            "attach": [],
            "terminate": [],
        }
        self.consumed: List[Dict[str, Any]] = []
        self.db = FakeDB(pending_rows)
        self.loop_behavior = "complete"  # complete | hang | die
        self.heartbeat_result: Any = datetime.now(timezone.utc)
        self.bundle_error: Optional[Exception] = None
        self.attach_error: Optional[Exception] = None
        self._fake_loop_tasks: List[asyncio.Task] = []

        pa._agent = SimpleNamespace(postgres_conn=self.db)
        pa._session = None
        pa._thread_id = None
        pa._loop_user_queue = None
        pa._loop_task = None
        pa._turn_complete_external_hook = None
        pa._tool_inflight = False
        pa._loop_interrupt_flag = None
        pa._hard_interrupt_event = asyncio.Event()
        pa._pending_cloud_push_task = None

        harness = self

        async def fake_complete(db, *, unit_id, lease_token, consumed_seq):
            harness.calls["complete"].append(
                {
                    "unit_id": unit_id,
                    "lease_token": lease_token,
                    "consumed_seq": consumed_seq,
                }
            )
            return "done"

        async def fake_release(
            db, *, unit_id, lease_token, backoff_seconds=0.0, error=False
        ):
            harness.calls["release"].append(
                {
                    "unit_id": unit_id,
                    "lease_token": lease_token,
                    "backoff_seconds": backoff_seconds,
                    "error": error,
                }
            )
            return "queued"

        async def fake_heartbeat(db, *, unit_id, lease_token, lease_ttl_seconds=60.0):
            harness.calls["heartbeat"].append(
                {"unit_id": unit_id, "lease_token": lease_token}
            )
            return harness.heartbeat_result

        monkeypatch.setattr(te, "complete_unit", fake_complete)
        monkeypatch.setattr(te, "release_unit", fake_release)
        monkeypatch.setattr(te, "heartbeat_unit", fake_heartbeat)

        # --- fake orchestrator client -------------------------------------
        class FakeClient:
            async def get_claim_bundle(self, unit_id, lease_token):
                harness.calls["bundle"].append(
                    {"unit_id": unit_id, "lease_token": lease_token}
                )
                if harness.bundle_error is not None:
                    raise harness.bundle_error
                return {
                    "unit_id": unit_id,
                    "thread_id": unit_id,
                    "unit_kind": "session_turn",
                    "execution_lane": "stateless",
                    "watermarks": {"input_seq": None, "consumed_seq": None},
                    "attach": harness.attach_for(unit_id),
                }

        pa._orchestrator_client = FakeClient()
        self._attach_overrides: Dict[str, Dict[str, Any]] = {}

        # --- fake persistent_app machinery --------------------------------
        async def fake_attach(**kwargs):
            harness.calls["attach"].append(kwargs)
            if harness.attach_error is not None:
                raise harness.attach_error
            pa._session = FakeSession()
            pa._thread_id = kwargs.get("thread_id")
            pa._loop_user_queue = asyncio.Queue()

        async def fake_terminate(reason, *, mark_thread=True):
            harness.calls["terminate"].append(
                {"reason": reason, "mark_thread": mark_thread}
            )
            task = pa._loop_task
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            pa._loop_task = None
            pa._session = None
            pa._thread_id = None
            pa._loop_user_queue = None

        def fake_ensure(source, client_id=None):
            if pa._loop_user_queue is None:
                return False
            if pa._loop_task is None or pa._loop_task.done():
                pa._loop_task = asyncio.create_task(harness._fake_loop())
                harness._fake_loop_tasks.append(pa._loop_task)
            return True

        monkeypatch.setattr(pa, "_attach_session", fake_attach)
        monkeypatch.setattr(pa, "_terminate_session", fake_terminate)
        monkeypatch.setattr(pa, "_ensure_persistent_loop_started", fake_ensure)

        self.executor = te.StatelessTurnExecutor(
            pod_name="test-pod", abort_grace_seconds=0.05
        )

    def attach_for(self, unit_id: str) -> Dict[str, Any]:
        return self._attach_overrides.get(
            unit_id,
            {
                "thread_id": unit_id,
                "config_override": None,
                "resolved_config": {"agent": {"llm": {"model": "m"}}},
                "project_ids": [],
                "datasources": None,
                "config_name": "session_base",
            },
        )

    def set_attach(self, unit_id: str, attach: Dict[str, Any]) -> None:
        self._attach_overrides[str(unit_id)] = attach

    async def _fake_loop(self):
        while True:
            item = await pa._loop_user_queue.get()
            self.consumed.append(item)
            if self.loop_behavior == "hang":
                await asyncio.sleep(3600)
            elif self.loop_behavior == "die":
                raise RuntimeError("scripted turn failure")
            else:
                hook = pa._turn_complete_external_hook
                if hook is not None:
                    hook(1)

    async def cleanup(self):
        for task in self._fake_loop_tasks:
            if not task.done():
                task.cancel()
            # Await even already-done tasks so a scripted failure's exception
            # is retrieved (no "Task exception was never retrieved" noise).
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


_PA_SAVED_ATTRS = (
    "_session",
    "_thread_id",
    "_loop_user_queue",
    "_loop_task",
    "_turn_complete_external_hook",
    "_agent",
    "_orchestrator_client",
    "_tool_inflight",
    "_loop_interrupt_flag",
    "_hard_interrupt_event",
    "_pending_cloud_push_task",
)


@pytest.fixture
def harness(monkeypatch):
    saved = {name: getattr(pa, name) for name in _PA_SAVED_ATTRS}
    h = Harness(monkeypatch)
    try:
        yield h
    finally:
        for name, value in saved.items():
            setattr(pa, name, value)


async def _finish(h: Harness):
    await h.cleanup()


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_claim_bundle_attach_inject_complete(self, harness):
        unit = uuid4()
        row_id = str(uuid4())
        harness.db.pending_rows = [{"id": row_id, "seq": 5, "content": "hello"}]
        claim = make_claim(unit_id=unit, token=7, input_seq=5, consumed_seq=None)

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        # Bundle fetched with the claim's identity.
        assert harness.calls["bundle"] == [{"unit_id": str(unit), "lease_token": 7}]
        # Attach fed the bundle's attach object unchanged.
        assert len(harness.calls["attach"]) == 1
        assert harness.calls["attach"][0]["thread_id"] == str(unit)
        assert harness.calls["attach"][0]["config_name"] == "session_base"
        # The injected item carried the DB row id + content (no re-persist).
        assert harness.consumed == [{"content": "hello", "id": row_id}]
        # Completion carried the injected message's seq.
        assert harness.calls["complete"] == [
            {"unit_id": unit, "lease_token": 7, "consumed_seq": 5}
        ]
        assert not harness.calls["release"]
        # Affinity hint updated for the next poll.
        assert harness.executor._prefer_unit_id == unit
        # Lease handle points at this claim (fenced writers read it live).
        assert get_current_lease() is None  # var only set inside run()
        assert harness.executor._lease.unit_id == str(unit)
        assert harness.executor._lease.lease_token == 7

    @pytest.mark.asyncio
    async def test_no_pending_input_completes_with_fallback(self, harness):
        unit = uuid4()
        harness.db.pending_rows = []
        claim = make_claim(unit_id=unit, token=2, input_seq=9, consumed_seq=4)

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        # COALESCE(input_seq, consumed_seq, 0) → input_seq.
        assert harness.calls["complete"] == [
            {"unit_id": unit, "lease_token": 2, "consumed_seq": 9}
        ]
        assert not harness.calls["release"]


# ---------------------------------------------------------------------------
# 2. Skip-if-answered
# ---------------------------------------------------------------------------


class TestSkipIfAnswered:
    @pytest.mark.asyncio
    async def test_consumed_at_or_past_input_completes_immediately(self, harness):
        unit = uuid4()
        claim = make_claim(unit_id=unit, token=4, input_seq=7, consumed_seq=7)

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        assert harness.calls["complete"] == [
            {"unit_id": unit, "lease_token": 4, "consumed_seq": 7}
        ]
        # No LLM path: no bundle fetch, no attach, no injection.
        assert not harness.calls["bundle"]
        assert not harness.calls["attach"]
        assert not harness.consumed
        assert harness.executor._prefer_unit_id == unit


# ---------------------------------------------------------------------------
# 3. Bundle failures
# ---------------------------------------------------------------------------


class TestBundleFailures:
    @pytest.mark.asyncio
    async def test_bundle_403_releases_with_error_and_continues(self, harness):
        harness.bundle_error = ClaimBundleError(403, "token mismatch")
        claim = make_claim(token=3)

        await harness.executor._serve_claim(claim)  # must not raise
        await _finish(harness)

        assert len(harness.calls["release"]) == 1
        assert harness.calls["release"][0]["error"] is True
        assert not harness.calls["attach"]
        assert not harness.calls["complete"]

    @pytest.mark.asyncio
    async def test_bundle_404_drops_without_release(self, harness):
        harness.bundle_error = ClaimBundleError(404, "gone")
        claim = make_claim()

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        assert not harness.calls["release"]
        assert not harness.calls["attach"]
        assert not harness.calls["complete"]

    @pytest.mark.asyncio
    async def test_bundle_network_error_releases(self, harness):
        harness.bundle_error = ConnectionError("orchestrator down")
        claim = make_claim()

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        assert len(harness.calls["release"]) == 1
        assert harness.calls["release"][0]["error"] is True


# ---------------------------------------------------------------------------
# 4. Turn error → release + loop continues
# ---------------------------------------------------------------------------


class TestTurnError:
    @pytest.mark.asyncio
    async def test_loop_death_releases_with_error(self, harness):
        harness.loop_behavior = "die"
        unit = uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 3, "content": "x"}]
        claim = make_claim(unit_id=unit, token=6, input_seq=3)

        await harness.executor._serve_claim(claim)  # must not raise
        await _finish(harness)

        assert len(harness.calls["release"]) == 1
        assert harness.calls["release"][0] == {
            "unit_id": unit,
            "lease_token": 6,
            "backoff_seconds": 0.0,
            "error": True,
        }
        assert not harness.calls["complete"]
        # Broken session was detached, never marking the thread.
        assert harness.calls["terminate"]
        assert all(t["mark_thread"] is False for t in harness.calls["terminate"])

    @pytest.mark.asyncio
    async def test_attach_failure_releases_and_clears(self, harness):
        harness.attach_error = RuntimeError("workspace exploded")
        unit = uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "x"}]
        claim = make_claim(unit_id=unit, input_seq=1)

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        assert len(harness.calls["release"]) == 1
        assert harness.calls["release"][0]["error"] is True
        assert pa._thread_id is None
        assert not harness.calls["complete"]


# ---------------------------------------------------------------------------
# 5. Heartbeat lease-lost → polite abort, NO release
# ---------------------------------------------------------------------------


class TestLeaseLost:
    @pytest.mark.asyncio
    async def test_heartbeat_loss_aborts_without_release(self, harness, monkeypatch):
        monkeypatch.setattr(te, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
        harness.heartbeat_result = None  # renewal finds no leased row
        harness.loop_behavior = "hang"  # the turn never completes on its own
        unit = uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 2, "content": "hi"}]
        claim = make_claim(unit_id=unit, token=9, input_seq=2)

        await asyncio.wait_for(harness.executor._serve_claim(claim), timeout=5.0)
        await _finish(harness)

        # Polite abort: interrupt signalled, session discarded, and neither
        # complete_unit nor release_unit was called — the lease is gone.
        assert not harness.calls["release"]
        assert not harness.calls["complete"]
        assert pa._loop_interrupt_flag in ("hard", "graceful")
        assert harness.calls["terminate"]
        assert harness.calls["terminate"][-1]["mark_thread"] is False
        # Affinity dropped: the discarded session must not be preferred.
        assert harness.executor._prefer_unit_id is None
        assert harness.executor._attached_fingerprint is None


# ---------------------------------------------------------------------------
# 6. strip_restored_pending_humans
# ---------------------------------------------------------------------------


class TestStripRestoredPending:
    def _msgs(self):
        from langchain_core.messages import AIMessage, HumanMessage

        return [
            HumanMessage(content="q1", id="a"),
            AIMessage(content="a1", id="b"),
            HumanMessage(content="pending-1", id="fresh-uuid-1"),
            HumanMessage(content="pending-2", id="fresh-uuid-2"),
        ]

    def test_removes_trailing_pending_by_content(self):
        msgs = self._msgs()
        pending = [
            {"id": "row-1", "seq": 10, "content": "pending-1"},
            {"id": "row-2", "seq": 11, "content": "pending-2"},
        ]
        removed = te.strip_restored_pending_humans(msgs, pending)
        assert removed == 2
        assert [m.content for m in msgs] == ["q1", "a1"]

    def test_removes_by_id_when_restore_preserves_ids(self):
        from langchain_core.messages import HumanMessage

        msgs = [HumanMessage(content="anything", id="row-9")]
        pending = [{"id": "row-9", "seq": 4, "content": "different text"}]
        removed = te.strip_restored_pending_humans(msgs, pending)
        assert removed == 1
        assert msgs == []

    def test_non_trailing_human_untouched(self):
        from langchain_core.messages import AIMessage, HumanMessage

        msgs = [
            HumanMessage(content="pending-1", id="x"),
            AIMessage(content="answer", id="y"),
        ]
        pending = [{"id": "row-1", "seq": 10, "content": "pending-1"}]
        removed = te.strip_restored_pending_humans(msgs, pending)
        assert removed == 0
        assert len(msgs) == 2

    def test_stops_at_unmatched_trailing_human(self):
        from langchain_core.messages import HumanMessage

        # An unanswered role='event' row restores as a HumanMessage but is
        # never in pending_rows (orchestrator enqueues role='human' only) —
        # it stays, and pending humans BELOW it also stay (documented).
        msgs = [
            HumanMessage(content="pending-1", id="p1"),
            HumanMessage(content="[wake] job finished", id="ev"),
        ]
        pending = [{"id": "row-1", "seq": 10, "content": "pending-1"}]
        removed = te.strip_restored_pending_humans(msgs, pending)
        assert removed == 0
        assert len(msgs) == 2

    def test_empty_inputs_are_noops(self):
        assert te.strip_restored_pending_humans([], [{"id": "a"}]) == 0
        from langchain_core.messages import HumanMessage

        msgs = [HumanMessage(content="x", id="a")]
        assert te.strip_restored_pending_humans(msgs, []) == 0
        assert len(msgs) == 1


# ---------------------------------------------------------------------------
# 7. Scrub-on-claim acceptance (§5.6 — deliverable D)
# ---------------------------------------------------------------------------


class TestScrubOnClaim:
    def test_tenant_a_then_no_env_keys_leaves_no_residue(self, monkeypatch):
        from src.services import embedding_service as emb

        # Tenant A attach: env keys land, singleton would be rebuilt lazily.
        pa._apply_session_embedding_env(
            {
                "EMBEDDING_PROVIDER": "openai",
                "EMBEDDING_MODEL": "tenant-a-model",
                "EMBEDDING_BASE_URL": "https://a.example",
                "EMBEDDING_API_KEY": "sk-tenant-a",
                "KB_EMBEDDING_MODEL": "kb-a",
                "KB_EMBEDDING_API_KEY": "sk-kb-a",
            }
        )
        assert os.environ.get("EMBEDDING_API_KEY") == "sk-tenant-a"
        assert os.environ.get("KB_EMBEDDING_MODEL") == "kb-a"
        emb._embedding_service = object()  # simulate a built singleton

        # Tenant B attach with env_keys ABSENT: no A values anywhere,
        # singleton None.
        pa._apply_session_embedding_env(None)
        for key in pa.MEMORY_EMBEDDING_ENV_KEYS:
            assert key not in os.environ, key
        for key in emb.KB_EMBEDDING_ENV_KEYS:
            assert key not in os.environ, key
        assert emb._embedding_service is None
        assert emb._kb_embedding_service is None

        # Cleanup safety: nothing to restore — the helper popped everything.

    def test_partial_override_replaces_not_merges(self, monkeypatch):
        from src.services import embedding_service as emb

        pa._apply_session_embedding_env(
            {"EMBEDDING_MODEL": "a-model", "EMBEDDING_API_KEY": "sk-a"}
        )
        # Tenant B supplies only a model: A's key must NOT survive.
        pa._apply_session_embedding_env({"EMBEDDING_MODEL": "b-model"})
        assert os.environ.get("EMBEDDING_MODEL") == "b-model"
        assert "EMBEDDING_API_KEY" not in os.environ
        assert emb._embedding_service is None
        pa._apply_session_embedding_env(None)  # cleanup

    def test_executor_scrub_clears_dual_inboxes(self, harness):
        import src.api.dual_app as dual_app

        dual_app._guidance_inbox["job-1"] = [{"id": "g1"}]
        dual_app._reply_inbox["job-1"] = [{"id": "r1"}]
        harness.executor._scrub_process_residue()
        assert dual_app._guidance_inbox == {}
        assert dual_app._reply_inbox == {}


# ---------------------------------------------------------------------------
# 8. Affinity
# ---------------------------------------------------------------------------


class TestAffinity:
    @pytest.mark.asyncio
    async def test_same_thread_same_fingerprint_skips_reattach(self, harness):
        unit = uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "one"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=1, input_seq=1)
        )
        assert len(harness.calls["attach"]) == 1

        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 2, "content": "two"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=2, input_seq=2)
        )
        await _finish(harness)

        # Reused: no second attach, no detach in between.
        assert len(harness.calls["attach"]) == 1
        assert not harness.calls["terminate"]
        # The lease handle was repointed at the NEW claim's token.
        assert harness.executor._lease.lease_token == 2
        assert len(harness.calls["complete"]) == 2

    @pytest.mark.asyncio
    async def test_different_thread_detaches_then_attaches(self, harness):
        unit_a, unit_b = uuid4(), uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "a"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit_a, token=1, input_seq=1)
        )
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "b"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit_b, token=1, input_seq=1)
        )
        await _finish(harness)

        assert len(harness.calls["attach"]) == 2
        assert harness.calls["attach"][1]["thread_id"] == str(unit_b)
        # Old session detached first, thread never marked.
        assert len(harness.calls["terminate"]) == 1
        assert harness.calls["terminate"][0]["mark_thread"] is False

    @pytest.mark.asyncio
    async def test_same_thread_changed_config_reattaches(self, harness):
        unit = uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "a"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=1, input_seq=1)
        )
        harness.set_attach(
            str(unit),
            {
                "thread_id": str(unit),
                "config_override": {"llm": {"model": "different"}},
                "resolved_config": None,
                "project_ids": [],
                "datasources": None,
                "config_name": "session_base",
            },
        )
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 2, "content": "b"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=2, input_seq=2)
        )
        await _finish(harness)

        assert len(harness.calls["attach"]) == 2
        assert len(harness.calls["terminate"]) == 1


# ---------------------------------------------------------------------------
# 9. Fenced persistence (postgres_db + lease context)
# ---------------------------------------------------------------------------


class _FenceConn:
    """Fake asyncpg connection: fence query returns None (rejected)."""

    def __init__(self, fence_row=None):
        self.fence_row = fence_row
        self.sql_log: List[str] = []

    def transaction(self):
        @contextlib.asynccontextmanager
        async def _tx():
            yield

        return _tx()

    async def fetchrow(self, sql, *args):
        self.sql_log.append(sql)
        if "FROM run_queue" in sql:
            return self.fence_row
        return {"id": args[0] if args else "x", "seq": 42}

    async def execute(self, sql, *args):
        self.sql_log.append(sql)
        return "UPDATE 1"

    async def executemany(self, sql, args):
        self.sql_log.append(sql)


def _db_with_conn(conn):
    from src.database.postgres_db import PostgresDB

    db = PostgresDB(connection_string="postgresql://t:t@localhost:1/t")

    @contextlib.asynccontextmanager
    async def _acquire():
        yield conn

    db.acquire = _acquire  # type: ignore[method-assign]
    return db


class TestFencedPersistence:
    @pytest.mark.asyncio
    async def test_fence_rejection_raises_and_marks_lost(self):
        conn = _FenceConn(fence_row=None)
        db = _db_with_conn(conn)
        handle = LeaseHandle()
        handle.update(str(uuid4()), 5)
        token = current_lease.set(handle)
        try:
            with pytest.raises(LeaseLostError):
                await db.save_thread_message(
                    thread_id=str(uuid4()), role="human", content="x"
                )
            assert handle.lost.is_set()
            # The fence ran and nothing else did inside the transaction.
            assert any("FROM run_queue" in s for s in conn.sql_log)
            assert not any("INSERT INTO thread_messages" in s for s in conn.sql_log)
        finally:
            current_lease.reset(token)

    @pytest.mark.asyncio
    async def test_fence_pass_writes_row(self):
        conn = _FenceConn(fence_row={"?column?": 1})
        db = _db_with_conn(conn)
        handle = LeaseHandle()
        handle.update(str(uuid4()), 5)
        token = current_lease.set(handle)
        try:
            result = await db.save_thread_message(
                thread_id=str(uuid4()), role="human", content="x"
            )
            assert result["seq"] == 42
            assert any("FROM run_queue" in s for s in conn.sql_log)
            assert any("INSERT INTO thread_messages" in s for s in conn.sql_log)
        finally:
            current_lease.reset(token)

    @pytest.mark.asyncio
    async def test_no_lease_context_keeps_pinned_behavior(self):
        conn = _FenceConn(fence_row=None)  # would reject IF consulted
        db = _db_with_conn(conn)
        assert get_current_lease() is None
        result = await db.save_thread_message(
            thread_id=str(uuid4()), role="human", content="x"
        )
        assert result["seq"] == 42
        assert not any("FROM run_queue" in s for s in conn.sql_log)

    @pytest.mark.asyncio
    async def test_batch_reconcile_fenced(self):
        conn = _FenceConn(fence_row=None)
        db = _db_with_conn(conn)
        handle = LeaseHandle()
        handle.update(str(uuid4()), 8)
        token = current_lease.set(handle)
        try:
            with pytest.raises(LeaseLostError):
                await db.save_thread_messages(
                    str(uuid4()),
                    [{"id": None, "role": "ai", "content": "x", "turn_number": 1}],
                )
        finally:
            current_lease.reset(token)


# ---------------------------------------------------------------------------
# 10. Journal writer lease fence (persistent_app._OrderedPersistentEventWriter)
# ---------------------------------------------------------------------------


class TestWriterLeaseFence:
    def _writer(self, lease):
        recorded = []

        class _Conn:
            async def fetchval(self, sql, *args):
                recorded.append((sql, args))
                return 1

        class _Acquire:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, exc_type, exc, tb):
                return None

        pool = SimpleNamespace(acquire=lambda: _Acquire())
        writer = pa._OrderedPersistentEventWriter(
            postgres_conn=pool,
            thread_id="thread-1",
            epoch=0,
            on_terminal_failure=lambda events, reason: None,
            lease=lease,
        )
        return writer, recorded

    @pytest.mark.asyncio
    async def test_lease_flush_carries_fence_params_read_at_flush_time(self):
        handle = LeaseHandle()
        unit = str(uuid4())
        handle.update(unit, 3)
        writer, recorded = self._writer(handle)
        event = pa._QueuedPersistentEvent(epoch=0, seq=1, kind="token", payload={})

        await writer._write_batch([event])
        sql, args = recorded[0]
        assert "run_queue" in sql and "FOR SHARE" in sql
        assert args[3] == unit and args[4] == 3

        # Affinity re-claim: same writer, handle repointed → new token flows.
        handle.update(unit, 4)
        await writer._write_batch([event])
        assert recorded[1][1][4] == 4

    @pytest.mark.asyncio
    async def test_pinned_writer_keeps_four_arg_shape(self):
        writer, recorded = self._writer(None)
        event = pa._QueuedPersistentEvent(epoch=0, seq=1, kind="token", payload={})
        await writer._write_batch([event])
        sql, args = recorded[0]
        assert "run_queue" not in sql
        assert len(args) == 3  # thread_id, rows_json, epoch
