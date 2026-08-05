"""Focused tests for durable datasource/project knowledge reconciliation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.datasource_reconciliation import (
    ReconciliationStats,
    reconcile_datasource_projects_once,
    reconciliation_retry_delay,
    run_datasource_project_reconciler,
    safe_reconciliation_error,
)

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
DATASOURCE_ID = "22222222-2222-2222-2222-222222222222"


def _queue_row(
    *, revision: int = 7, claim_token: int = 700, attempts: int = 1
) -> dict[str, object]:
    return {
        "project_id": PROJECT_ID,
        "datasource_id": DATASOURCE_ID,
        "policy_revision": revision,
        "claim_token": claim_token,
        "attempts": attempts,
    }


def _db(*, rows: list[dict[str, object]]) -> MagicMock:
    db = MagicMock()
    db.claim_datasource_project_reconciliations = AsyncMock(return_value=rows)
    db.list_project_datasources = AsyncMock(return_value=[])
    db.finish_datasource_project_reconciliation = AsyncMock(return_value=True)
    db.retry_datasource_project_reconciliation = AsyncMock(return_value=True)
    return db


@pytest.mark.asyncio
async def test_non_leader_does_not_claim() -> None:
    db = _db(rows=[_queue_row()])

    stats = await reconcile_datasource_projects_once(
        db,
        sync_fn=AsyncMock(),
        delete_fn=AsyncMock(),
        leader=False,
    )

    assert stats == ReconciliationStats()
    db.claim_datasource_project_reconciliations.assert_not_awaited()


@pytest.mark.asyncio
async def test_linked_datasource_syncs_authoritative_descriptor_and_finishes() -> None:
    descriptor = {
        "id": DATASOURCE_ID,
        "name": "Application database",
        "project_read_only": True,
        "project_description": "Production reporting",
    }
    db = _db(rows=[_queue_row(revision=12)])
    db.list_project_datasources.return_value = [descriptor]
    sync = AsyncMock()
    delete = AsyncMock()

    stats = await reconcile_datasource_projects_once(
        db,
        sync_fn=sync,
        delete_fn=delete,
        leader=True,
        batch_size=9,
        lease_seconds=45,
    )

    assert stats == ReconciliationStats(claimed=1, succeeded=1)
    db.claim_datasource_project_reconciliations.assert_awaited_once_with(
        limit=9,
        lease_seconds=45,
    )
    db.list_project_datasources.assert_awaited_once_with(PROJECT_ID)
    sync.assert_awaited_once_with(PROJECT_ID, descriptor)
    delete.assert_not_awaited()
    db.finish_datasource_project_reconciliation.assert_awaited_once_with(
        PROJECT_ID, DATASOURCE_ID, 700
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("authoritative_rows", [[], [{"id": "another-id"}]])
async def test_missing_link_or_deleted_authority_deletes_external_note(
    authoritative_rows: list[dict[str, object]],
) -> None:
    db = _db(rows=[_queue_row()])
    db.list_project_datasources.return_value = authoritative_rows
    sync = AsyncMock()
    delete = AsyncMock()

    stats = await reconcile_datasource_projects_once(
        db,
        sync_fn=sync,
        delete_fn=delete,
        leader=True,
    )

    assert stats == ReconciliationStats(claimed=1, succeeded=1)
    delete.assert_awaited_once_with(PROJECT_ID, DATASOURCE_ID)
    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_retries_with_bounded_delay_and_never_persists_exception_data() -> (
    None
):
    secret = "postgresql://alice:hunter2@database.internal/app"
    db = _db(rows=[_queue_row(revision=14, attempts=4)])
    db.list_project_datasources.return_value = [{"id": DATASOURCE_ID}]
    sync = AsyncMock(
        side_effect=RuntimeError(
            f"could not connect to {secret}; config={{'password': 'hunter2'}}"
        )
    )

    stats = await reconcile_datasource_projects_once(
        db,
        sync_fn=sync,
        delete_fn=AsyncMock(),
        leader=True,
    )

    assert stats == ReconciliationStats(claimed=1, failed=1)
    db.finish_datasource_project_reconciliation.assert_not_awaited()
    db.retry_datasource_project_reconciliation.assert_awaited_once()
    retry = db.retry_datasource_project_reconciliation.await_args
    assert retry.args == (PROJECT_ID, DATASOURCE_ID, 700)
    assert retry.kwargs == {
        "safe_error": "RuntimeError: datasource knowledge sync failed",
        "delay_seconds": 40,
    }
    persisted = retry.kwargs["safe_error"]
    assert "hunter2" not in persisted
    assert "database.internal" not in persisted
    assert "config" not in persisted


@pytest.mark.asyncio
async def test_one_failure_does_not_block_the_rest_of_the_claim() -> None:
    second_id = "33333333-3333-3333-3333-333333333333"
    rows = [_queue_row(), {**_queue_row(), "datasource_id": second_id}]
    db = _db(rows=rows)
    db.list_project_datasources.return_value = [
        {"id": DATASOURCE_ID},
        {"id": second_id},
    ]
    sync = AsyncMock(side_effect=[TimeoutError("private DSN"), None])

    stats = await reconcile_datasource_projects_once(
        db,
        sync_fn=sync,
        delete_fn=AsyncMock(),
        leader=True,
    )

    assert stats == ReconciliationStats(claimed=2, succeeded=1, failed=1)
    assert sync.await_count == 2
    db.retry_datasource_project_reconciliation.assert_awaited_once()
    db.finish_datasource_project_reconciliation.assert_awaited_once_with(
        PROJECT_ID,
        second_id,
        700,
    )


@pytest.mark.asyncio
async def test_revision_guard_preserves_newer_coalesced_event() -> None:
    db = _db(rows=[_queue_row(revision=21)])
    db.list_project_datasources.return_value = [{"id": DATASOURCE_ID}]
    db.finish_datasource_project_reconciliation.return_value = False

    stats = await reconcile_datasource_projects_once(
        db,
        sync_fn=AsyncMock(),
        delete_fn=AsyncMock(),
        leader=True,
    )

    assert stats == ReconciliationStats(claimed=1, superseded=1)
    db.finish_datasource_project_reconciliation.assert_awaited_once_with(
        PROJECT_ID, DATASOURCE_ID, 700
    )


@pytest.mark.asyncio
async def test_stale_partial_failure_uses_claim_token_guard() -> None:
    """A strict sync may mutate one store before a newer claimant ACKs."""
    db = _db(rows=[_queue_row(revision=31, claim_token=3100)])
    db.list_project_datasources.return_value = [{"id": DATASOURCE_ID}]
    db.retry_datasource_project_reconciliation.return_value = False

    stats = await reconcile_datasource_projects_once(
        db,
        sync_fn=AsyncMock(side_effect=RuntimeError("second store failed")),
        delete_fn=AsyncMock(),
        leader=True,
    )

    assert stats == ReconciliationStats(claimed=1, failed=1, superseded=1)
    retry = db.retry_datasource_project_reconciliation.await_args
    assert retry.args == (PROJECT_ID, DATASOURCE_ID, 3100)


def test_retry_delay_is_exponential_and_bounded() -> None:
    assert reconciliation_retry_delay(0) == 5
    assert reconciliation_retry_delay(1) == 5
    assert reconciliation_retry_delay(2) == 10
    assert reconciliation_retry_delay(4) == 40
    assert reconciliation_retry_delay(100) == 3600
    assert reconciliation_retry_delay(10, base_seconds=3, max_seconds=20) == 20


def test_safe_error_uses_only_class_and_fixed_operation() -> None:
    error = ValueError("https://user:token@example.test config={'secret': 'value'}")

    result = safe_reconciliation_error(error, operation="state/read")

    assert result == "ValueError: datasource knowledge state_read failed"
    assert "token" not in result
    assert "example.test" not in result
    assert "secret" not in result


@pytest.mark.asyncio
async def test_loop_uses_live_leader_gate_and_stops_cooperatively() -> None:
    db = _db(rows=[])
    shutdown = asyncio.Event()
    leaders = iter([False, True])

    async def claim_then_stop(**_kwargs: object) -> list[dict[str, object]]:
        shutdown.set()
        return []

    db.claim_datasource_project_reconciliations.side_effect = claim_then_stop
    await run_datasource_project_reconciler(
        db,
        shutdown,
        lambda: next(leaders, True),
        sync_fn=AsyncMock(),
        delete_fn=AsyncMock(),
        interval_seconds=0.01,
        batch_size=3,
        lease_seconds=10,
    )

    # The first non-leader iteration skips the claim; the next live-leader
    # iteration claims exactly once and then observes cooperative shutdown.
    db.claim_datasource_project_reconciliations.assert_awaited_once_with(
        limit=3,
        lease_seconds=10,
    )
