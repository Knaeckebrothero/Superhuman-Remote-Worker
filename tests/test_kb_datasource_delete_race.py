"""Lifecycle ordering for deletion of external OKF KB datasource indexes."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services import knowledge_index
from orchestrator.services.kb_reindex import kb_index_lock, reindex_kb
from orchestrator.services.kb_task_registry import KbDatasourceTaskRegistry


def _index_dependencies(
    store, *, tasks=None
) -> "knowledge_index.KnowledgeIndexDependencies":
    """The collaborators main's ``_knowledge_index_dependencies()`` supplies.

    ``store`` is the app pool the delete path writes the row through; the
    vector pool only reaches ``KnowledgeStore``, which these tests patch.
    """
    return knowledge_index.KnowledgeIndexDependencies(
        store=store,
        vector_db=MagicMock(),
        gitea_client=MagicMock(),
        logger=MagicMock(),
        tasks=tasks if tasks is not None else KbDatasourceTaskRegistry(),
        inject_system_kb_embedding_profile=AsyncMock(return_value=None),
    )


class _CoordinatedStore:
    """Small concrete store whose advisory contexts share one test lock."""

    def __init__(self, events: list[str]):
        self.events = events
        self.advisory_lock = asyncio.Lock()
        self.delete_kb_index = AsyncMock(side_effect=self._delete_index)
        self.set_watermark_status = AsyncMock()

    async def _delete_index(self, _kb_id: uuid.UUID, *, conn=None) -> None:
        assert conn is self
        self.events.append("vector-delete")

    @asynccontextmanager
    async def try_reindex_lock(self, _kb_id: uuid.UUID):
        if self.advisory_lock.locked():
            yield False
            return
        await self.advisory_lock.acquire()
        try:
            yield True
        finally:
            self.advisory_lock.release()

    @asynccontextmanager
    async def reindex_lock(self, _kb_id: uuid.UUID):
        async with self.advisory_lock:
            yield self


@pytest.mark.asyncio
async def test_delete_waits_for_writer_then_stale_reindex_cannot_resurrect_index():
    datasource_id = "11111111-2222-3333-4444-555555555555"
    kb_id = uuid.UUID(datasource_id)
    events: list[str] = []
    writer_started = asyncio.Event()
    release_writer = asyncio.Event()
    store = _CoordinatedStore(events)
    alive = True

    async def delete_row(
        _datasource_id: str,
        *,
        authority_project_scope_id: str | None = None,
        deleted_by: str | None = None,
    ) -> bool:
        nonlocal alive
        assert authority_project_scope_id is None
        # This test calls _delete_kb_datasource_with_index directly with no
        # deleted_by, so the default must reach here as None, not be dropped
        # silently — the same passthrough the endpoint relies on (Task 12
        # item C, fix round 1).
        assert deleted_by is None
        events.append("app-delete")
        alive = False
        return True

    async def get_datasource(_datasource_id: str):
        return {"id": datasource_id, "type": "kb"} if alive else None

    db = MagicMock()
    db.delete_datasource = AsyncMock(side_effect=delete_row)
    db.get_datasource = AsyncMock(side_effect=get_datasource)

    async def in_flight_writer() -> None:
        async with kb_index_lock(store, kb_id):
            events.append("writer-start")
            writer_started.set()
            await release_writer.wait()
            events.append("writer-finish")

    writer = asyncio.create_task(in_flight_writer())
    await writer_started.wait()

    cancel_local = AsyncMock()
    dependencies = _index_dependencies(db)
    with (
        patch(
            "shared.runtime.services.knowledge_store.KnowledgeStore", return_value=store
        ),
        patch(
            "orchestrator.services.knowledge_index.cancel_kb_datasource_reindexes",
            cancel_local,
        ),
    ):
        deletion = asyncio.create_task(
            knowledge_index.delete_kb_datasource_with_index(
                datasource_id, dependencies=dependencies
            )
        )
        await asyncio.sleep(0)
        assert not deletion.done(), "delete passed an in-flight KB writer"

        release_writer.set()
        assert await deletion is True

    await writer
    cancel_local.assert_awaited_once_with(datasource_id, dependencies=dependencies)
    assert events == [
        "writer-start",
        "writer-finish",
        "vector-delete",
        "app-delete",
    ]

    # A sweeper can hold a datasource object captured before app-row deletion.
    # Its under-lock liveness check must stop before HEAD/status/vector writes.
    source = MagicMock(label=f"datasource:{datasource_id}")
    source.get_head = AsyncMock()

    async def source_is_active() -> bool:
        return bool(await get_datasource(datasource_id))

    result = await reindex_kb(
        source=source,
        store=store,
        embedding_service=MagicMock(),
        kb_id=kb_id,
        is_active=source_is_active,
    )

    assert result["status"] == "source-deleted"
    source.get_head.assert_not_awaited()
    store.set_watermark_status.assert_not_awaited()
    store.delete_kb_index.assert_awaited_once_with(kb_id, conn=store)


@pytest.mark.asyncio
async def test_cancel_datasource_reindexes_drains_and_forgets_owned_task():
    datasource_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    started = asyncio.Event()
    cleaned = asyncio.Event()
    never = asyncio.Event()

    async def scheduled(_datasource_id: str, *, force_full: bool, dependencies) -> None:
        assert force_full is True
        started.set()
        try:
            await never.wait()
        finally:
            cleaned.set()

    tasks = KbDatasourceTaskRegistry()
    dependencies = _index_dependencies(MagicMock(), tasks=tasks)
    with patch(
        "orchestrator.services.knowledge_index.run_scheduled_kb_datasource_reindex",
        scheduled,
    ):
        knowledge_index.schedule_kb_datasource_reindex(
            datasource_id, force_full=True, dependencies=dependencies
        )
        await started.wait()
        await knowledge_index.cancel_kb_datasource_reindexes(
            datasource_id, dependencies=dependencies
        )
        await asyncio.sleep(0)

    assert cleaned.is_set()
    # The registry's per-datasource index is what a delete fences against, so
    # this reaches its internal view deliberately: ``pending()`` is the
    # shutdown ownership set and would not catch a leaked per-id entry.
    assert datasource_id not in tasks._tasks_by_id
