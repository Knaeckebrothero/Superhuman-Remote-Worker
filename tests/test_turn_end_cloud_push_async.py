"""Turn-end cloud push runs off the turn-close critical path.

The old inline ``await push_all()`` in ``_loop_on_turn_complete`` held the
persistent loop for the whole push — minutes on a fresh pod whose in-memory
dedup state was empty — AFTER ``turn.completed`` had already gone out. Queued
input sat in an invisible pre-turn limbo the whole time.

Now the push is a background task. Ordering still matters: the next turn's
start hook awaits it before its pull (strict push(N) → pull(N+1) per mount),
and teardown awaits it before the final sync/aclose.

knowledge-base/knowledge/issues/session_turn_end_cloud_push_blocks_queued_input.md
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent.api.persistent_app as papp


def _session_stub() -> MagicMock:
    session = MagicMock()
    session.postgres_conn = None  # skips message-save + auto-title awaits
    session.overlay_mount_manager = None  # skips the cloud-stage ping
    session.tool_decisions = {}
    return session


@pytest.fixture(autouse=True)
def _clear_pending_task():
    papp._pending_cloud_push_task = None
    yield
    papp._pending_cloud_push_task = None


class TestTurnCompleteSpawnsPush:
    @pytest.mark.asyncio
    async def test_turn_complete_returns_while_push_still_running(self):
        """The whole point: the loop parks (and the next queued input can
        start) without waiting for WebDAV round-trips."""
        release = asyncio.Event()
        session = _session_stub()
        session.workspace_sync.push_all = AsyncMock(side_effect=release.wait)
        events: list[str] = []

        with (
            patch.object(papp, "_session", session),
            patch.object(papp, "_thread_id", "t1"),
            patch.object(papp, "_retire_announced_permission_rows", AsyncMock()),
            patch.object(papp, "_broadcast", lambda k, p: events.append(k)),
        ):
            await asyncio.wait_for(papp._loop_on_turn_complete(8), timeout=1)

            task = papp._pending_cloud_push_task
            assert task is not None and not task.done()
            assert "turn.completed" in events
            assert "workspace_sync.pushed" not in events

            release.set()
            await asyncio.wait_for(task, timeout=1)
        assert events.index("workspace_sync.pushing") < events.index(
            "workspace_sync.pushed"
        )

    @pytest.mark.asyncio
    async def test_no_sync_no_task(self):
        session = _session_stub()
        session.workspace_sync = None

        with (
            patch.object(papp, "_session", session),
            patch.object(papp, "_thread_id", "t1"),
            patch.object(papp, "_retire_announced_permission_rows", AsyncMock()),
            patch.object(papp, "_broadcast", lambda k, p: None),
        ):
            await papp._loop_on_turn_complete(3)
        assert papp._pending_cloud_push_task is None


class TestTurnStartAwaitsPush:
    @pytest.mark.asyncio
    async def test_pending_push_lands_before_the_pull(self):
        """push(N) → pull(N+1) stays strict per mount: no concurrent walk of
        the same dedup state, and the pull's listing sees the last turn's
        writes."""
        order: list[str] = []
        release = asyncio.Event()

        async def _push():
            await release.wait()
            order.append("push")

        session = _session_stub()
        session.workspace_sync.pull_all = AsyncMock(
            side_effect=lambda: order.append("pull")
        )

        with (
            patch.object(papp, "_session", session),
            patch.object(papp, "_thread_id", "t1"),
            patch.object(papp, "_cloud_sync_retry_pending", False),
            patch.object(papp, "_broadcast", lambda k, p: None),
        ):
            papp._pending_cloud_push_task = asyncio.create_task(_push())
            release.set()
            await asyncio.wait_for(papp._loop_on_turn_start(9), timeout=1)

        assert order == ["push", "pull"]
        assert papp._pending_cloud_push_task is None

    @pytest.mark.asyncio
    async def test_no_pending_task_is_a_noop(self):
        session = _session_stub()
        session.workspace_sync = None

        with (
            patch.object(papp, "_session", session),
            patch.object(papp, "_thread_id", "t1"),
            patch.object(papp, "_cloud_sync_retry_pending", False),
            patch.object(papp, "_broadcast", lambda k, p: None),
        ):
            await asyncio.wait_for(papp._loop_on_turn_start(2), timeout=1)


class TestAwaitPendingCloudPush:
    @pytest.mark.asyncio
    async def test_failed_task_is_contained(self):
        """The task body never raises in production (_resilient_cloud_sync
        swallows), but the awaiter must survive task-machinery failures —
        a broken push delays the pull, never kills the turn or teardown."""

        async def _boom():
            raise RuntimeError("transport died")

        papp._pending_cloud_push_task = asyncio.create_task(_boom())
        await asyncio.sleep(0)  # let it fail
        await papp._await_pending_cloud_push()  # must not raise
        assert papp._pending_cloud_push_task is None

    @pytest.mark.asyncio
    async def test_clears_the_slot_exactly_once(self):
        done: list[int] = []

        async def _push():
            done.append(1)

        papp._pending_cloud_push_task = asyncio.create_task(_push())
        await papp._await_pending_cloud_push()
        await papp._await_pending_cloud_push()  # second call: no task, no-op
        assert done == [1]
