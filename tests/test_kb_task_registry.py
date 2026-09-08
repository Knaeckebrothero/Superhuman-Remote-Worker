"""Ownership contract for the app-owned KB datasource reindex registry.

Before R1.B03 this was two module globals in ``main`` — a shutdown ownership
set and a per-datasource cancellation view — and only the *effects* were
tested (``tests/test_kb_datasource_delete_race.py`` pins the delete fence). The
behaviors pinned here are the ones a move can quietly change:

* both views release a finished task, and the per-source key disappears when
  it empties, so a long-lived process does not accumulate one empty set per
  connector ever reindexed;
* a per-source cancel touches only that source;
* the calling task is never cancelled — the delete path calls this from inside
  a request task that may itself be registered, and cancelling yourself there
  would abort the delete instead of the writer it is fencing against;
* ``drain`` cancels and awaits everything, which is what shutdown relies on
  before it closes the git/vector clients.
"""

import asyncio

import pytest

from orchestrator.services.kb_task_registry import KbDatasourceTaskRegistry


async def _blocks_forever() -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_finished_task_leaves_both_views_and_pops_the_empty_key():
    registry = KbDatasourceTaskRegistry()

    async def work() -> str:
        return "done"

    task = registry.schedule("ds-1", work(), name="kb-datasource-reindex-ds-1")
    assert registry.pending() == [task]
    # The per-source view is the second structure the port names explicitly;
    # a leak there is invisible through `pending()`.
    assert set(registry._tasks_by_id) == {"ds-1"}

    assert await task == "done"
    await asyncio.sleep(0)  # let the done-callback run

    assert registry.pending() == []
    assert registry._tasks_by_id == {}


@pytest.mark.asyncio
async def test_named_task_carries_the_supplied_name():
    registry = KbDatasourceTaskRegistry()
    task = registry.schedule("ds-1", _blocks_forever(), name="kb-datasource-reindex-ds")
    try:
        assert task.get_name() == "kb-datasource-reindex-ds"
    finally:
        await registry.drain()


@pytest.mark.asyncio
async def test_cancel_for_datasource_leaves_other_sources_running():
    registry = KbDatasourceTaskRegistry()
    doomed = registry.schedule("ds-1", _blocks_forever(), name="a")
    spared = registry.schedule("ds-2", _blocks_forever(), name="b")

    await registry.cancel_for_datasource("ds-1")

    assert doomed.cancelled()
    assert not spared.done()
    assert registry.pending() == [spared]
    assert set(registry._tasks_by_id) == {"ds-2"}

    await registry.drain()


@pytest.mark.asyncio
async def test_cancel_for_datasource_never_cancels_the_calling_task():
    """The delete path runs inside a task that may itself be registered."""
    registry = KbDatasourceTaskRegistry()
    sibling_started = asyncio.Event()
    finished = asyncio.Event()

    async def sibling() -> None:
        sibling_started.set()
        await asyncio.Event().wait()

    async def self_cancelling_deleter() -> str:
        await sibling_started.wait()
        await registry.cancel_for_datasource("ds-1")
        finished.set()
        return "survived"

    doomed = registry.schedule("ds-1", sibling(), name="sibling")
    deleter = registry.schedule("ds-1", self_cancelling_deleter(), name="deleter")

    assert await deleter == "survived"
    assert finished.is_set()
    assert doomed.cancelled()


@pytest.mark.asyncio
async def test_cancel_for_unknown_datasource_is_a_no_op():
    registry = KbDatasourceTaskRegistry()
    kept = registry.schedule("ds-1", _blocks_forever(), name="a")

    await registry.cancel_for_datasource("ds-absent")

    assert not kept.done()
    await registry.drain()


@pytest.mark.asyncio
async def test_drain_cancels_and_awaits_every_registered_task():
    registry = KbDatasourceTaskRegistry()
    first = registry.schedule("ds-1", _blocks_forever(), name="a")
    second = registry.schedule("ds-2", _blocks_forever(), name="b")

    await registry.drain()

    assert first.cancelled() and second.cancelled()
    await asyncio.sleep(0)
    assert registry.pending() == []
    assert registry._tasks_by_id == {}


@pytest.mark.asyncio
async def test_drain_swallows_a_failing_task():
    """Shutdown must not be blocked by a rebuild that raised."""
    registry = KbDatasourceTaskRegistry()

    async def explodes() -> None:
        raise RuntimeError("boom")

    registry.schedule("ds-1", explodes(), name="a")

    await registry.drain()  # must not raise

    assert registry.pending() == []
