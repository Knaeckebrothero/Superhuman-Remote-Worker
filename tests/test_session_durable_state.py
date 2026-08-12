"""S2 close-out: durable session tasks, cursors, anchors and undo ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.managers.session_tasks import SessionTaskManager
from src.api.persistent_session import PersistentSession
from src.api.lease_context import LeaseLostError
from src.services.memory import CaptureEvent, MemoryRuntime
from src.services.memory.plugins.legacy_writers import PersistentIntervalExtractor
from src.tools.core.session_task_tools import create_session_task_tools


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT / "orchestrator/database/migrations/app/0133_thread_session_durable_state.sql"
)


def _row(number: int, *, status: str = "pending") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "task_number": number,
        "description": f"task {number}",
        "status": status,
        "priority": "medium",
        "notes": "",
        "created_at": now,
        "completed_at": now if status == "completed" else None,
    }


def test_migration_0133_owns_only_the_three_session_state_tables():
    sql = MIGRATION.read_text()
    assert "CREATE TABLE thread_session_tasks" in sql
    assert "CREATE TABLE thread_session_runtime_state" in sql
    assert "CREATE TABLE thread_cloud_citation_anchors" in sql
    assert "REFERENCES threads(id) ON DELETE CASCADE" in sql
    assert "CREATE TABLE run_queue" not in sql
    assert "ALTER TABLE run_queue" not in sql


@pytest.mark.asyncio
async def test_claim_setup_hydrates_tasks_and_cloud_anchors_before_use():
    postgres = MagicMock()
    postgres.list_thread_cloud_anchors = AsyncMock(
        return_value={
            "documents/cloud.pdf": {
                "provider": "nextcloud",
                "version": "etag-7",
            }
        }
    )
    manager = MagicMock()
    manager.hydrate = AsyncMock()
    context = MagicMock()
    session = PersistentSession(thread_id="thread-1", config=MagicMock())
    session.postgres_conn = postgres
    session.session_task_manager = manager
    session.tool_context = context

    await session._hydrate_durable_session_state()

    manager.hydrate.assert_awaited_once_with()
    context.record_cloud_anchor.assert_called_once_with(
        "documents/cloud.pdf",
        {"provider": "nextcloud", "version": "etag-7"},
    )


@pytest.mark.asyncio
async def test_first_file_changing_turn_has_baseline_and_current_mapping():
    postgres = MagicMock()
    postgres.seed_workspace_baseline_commit = AsyncMock()
    postgres.record_turn_commit = AsyncMock()
    calls = MagicMock()
    calls.attach_mock(postgres.seed_workspace_baseline_commit, "seed")
    calls.attach_mock(postgres.record_turn_commit, "record")

    git_manager = MagicMock(is_active=True)
    git_manager.get_current_commit.return_value = "baseline-sha"
    session = PersistentSession(
        thread_id="thread-1",
        config=MagicMock(),
        workspace_manager=MagicMock(git_manager=git_manager),
    )

    await session._seed_workspace_baseline_commit(postgres)
    await postgres.record_turn_commit("thread-1", "first-turn-sha")

    assert calls.mock_calls == [
        call.seed("thread-1", "baseline-sha"),
        call.record("thread-1", "baseline-sha"),
        call.record("thread-1", "first-turn-sha"),
    ]


@pytest.mark.asyncio
async def test_stateless_attach_fails_when_workspace_baseline_is_not_durable():
    postgres = MagicMock()
    postgres.seed_workspace_baseline_commit = AsyncMock(
        side_effect=LeaseLostError("stale")
    )
    postgres.record_turn_commit = AsyncMock()
    git_manager = MagicMock(is_active=True)
    git_manager.get_current_commit.return_value = "baseline-sha"
    session = PersistentSession(
        thread_id="thread-1",
        config=MagicMock(),
        workspace_manager=MagicMock(git_manager=git_manager),
        shell_owner_token=17,
    )

    with pytest.raises(LeaseLostError, match="stale"):
        await session._seed_workspace_baseline_commit(postgres)


@pytest.mark.asyncio
async def test_attach_reconciles_pushed_head_after_mapping_crash():
    postgres = MagicMock()
    postgres.seed_workspace_baseline_commit = AsyncMock()
    postgres.record_turn_commit = AsyncMock()
    git_manager = MagicMock(is_active=True)
    git_manager.get_current_commit.return_value = "pushed-unmapped-sha"
    session = PersistentSession(
        thread_id="thread-1",
        config=MagicMock(),
        workspace_manager=MagicMock(git_manager=git_manager),
        shell_owner_token=18,
    )

    await session._seed_workspace_baseline_commit(postgres)

    postgres.seed_workspace_baseline_commit.assert_awaited_once_with(
        "thread-1", "pushed-unmapped-sha"
    )
    postgres.record_turn_commit.assert_awaited_once_with(
        "thread-1", "pushed-unmapped-sha"
    )


@pytest.mark.asyncio
async def test_session_task_manager_hydrates_and_keeps_monotonic_ids():
    postgres = MagicMock()
    postgres.list_session_tasks = AsyncMock(return_value=[_row(2), _row(7)])
    postgres.create_session_task = AsyncMock(return_value=_row(8))
    manager = SessionTaskManager(thread_id="thread-1", postgres=postgres)

    await manager.hydrate()
    task = await manager.add("task 8")

    assert [item.id for item in manager.list_all()] == ["task_2", "task_7", "task_8"]
    assert task.id == "task_8"
    postgres.create_session_task.assert_awaited_once_with(
        "thread-1", "task 8", "medium"
    )


@pytest.mark.asyncio
async def test_session_task_completion_is_durable_before_cache_changes():
    postgres = MagicMock()
    postgres.list_session_tasks = AsyncMock(return_value=[_row(1)])
    postgres.complete_session_task = AsyncMock(
        return_value={**_row(1, status="completed"), "notes": "done"}
    )
    manager = SessionTaskManager(thread_id="thread-1", postgres=postgres)
    await manager.hydrate()

    completed = await manager.complete("task_1", "done")

    assert completed is not None
    assert completed.status == "completed"
    assert manager.to_dict_list()[0]["notes"] == "done"
    postgres.complete_session_task.assert_awaited_once_with("thread-1", 1, "done")


@pytest.mark.asyncio
async def test_session_task_tools_await_the_durable_manager():
    manager = SessionTaskManager()
    context = MagicMock(session_task_manager=manager)
    tools = {tool.name: tool for tool in create_session_task_tools(context)}

    added = await tools["task_add"].ainvoke(
        {"description": "survive a handoff", "priority": "high"}
    )
    listed = await tools["task_list"].ainvoke({})
    completed = await tools["task_complete"].ainvoke(
        {"task_id": "task_1", "notes": "verified"}
    )

    assert "Added task_1" in added
    assert "survive a handoff" in listed
    assert "Completed task_1" in completed


@pytest.mark.asyncio
async def test_memory_interval_writer_uses_durable_claim_before_extraction(monkeypatch):
    claim = AsyncMock(return_value=False)
    extract = AsyncMock()
    monkeypatch.setattr(
        "src.services.auxiliary.extract_and_store_memories",
        extract,
    )
    memory_config = MagicMock(observer_interval=5)
    runtime = MemoryRuntime(
        recall_store=object(),
        auxiliary_llm=object(),
        memory_config=memory_config,
        extra={"claim_persistent_extraction_interval": claim},
    )
    writer = PersistentIntervalExtractor(runtime)

    await writer.on_event(CaptureEvent(kind="turn_end", messages=[], turn_count=10))

    claim.assert_awaited_once_with(10, 5)
    extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_interval_writer_extracts_after_durable_claim(monkeypatch):
    claim = AsyncMock(return_value=True)
    extract = AsyncMock()
    monkeypatch.setattr(
        "src.services.auxiliary.extract_and_store_memories",
        extract,
    )
    memory_config = MagicMock(observer_interval=5)
    runtime = MemoryRuntime(
        recall_store=object(),
        auxiliary_llm=object(),
        memory_config=memory_config,
        extraction_prompt="prompt",
        extra={"claim_persistent_extraction_interval": claim},
    )
    writer = PersistentIntervalExtractor(runtime)

    await writer.on_event(CaptureEvent(kind="turn_end", messages=[], turn_count=10))

    extract.assert_awaited_once()
    assert extract.await_args.kwargs["source_turn_start"] == 5
    assert extract.await_args.kwargs["source_turn_end"] == 10
