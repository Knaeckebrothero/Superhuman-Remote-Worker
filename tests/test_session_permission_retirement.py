"""Exact-token stateless permission retirement and rolling repair contracts."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.shared import session_permission_retirement as mod


def test_permission_migration_chain_is_expand_validate_and_snapshotted():
    root = Path(__file__).resolve().parents[1]
    migrations = root / "orchestrator/database/migrations/app"
    add = (migrations / "0147_thread_permission_lease_receipts.sql").read_text()
    index = (migrations / "0148_thread_permission_receipt_idx.notx.sql").read_text()
    validate = (
        migrations / "0149_thread_permission_validate_constraints.sql"
    ).read_text()
    correction = (migrations / "0153_thread_permission_lease_comment.sql").read_text()
    snapshot = (root / "orchestrator/database/schema_current.sql").read_text()

    assert "ADD COLUMN accepted_lease_token BIGINT" in add
    assert "thread_permission_accepted_lease_positive" in add
    assert "thread_events_permission_request_thread_fkey" in add
    assert add.count("NOT VALID") >= 2
    assert "CREATE UNIQUE INDEX CONCURRENTLY" in index
    assert "idx_thread_events_permission_request" in index
    assert "VALIDATE CONSTRAINT thread_permission_accepted_lease_positive" in validate
    assert (
        "VALIDATE CONSTRAINT thread_events_permission_request_thread_fkey" in validate
    )
    assert "never guessed by a generic expiry sweep" in correction
    assert "accepted_lease_token bigint" in snapshot
    assert "permission_request_id uuid" in snapshot
    assert "CREATE UNIQUE INDEX idx_thread_events_permission_request" in snapshot
    assert "ADD CONSTRAINT thread_events_permission_request_thread_fkey" in snapshot


def test_shared_helper_is_agent_image_import_safe():
    """The agent Dockerfile copies src/ but no orchestrator/ package."""

    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "docker/Dockerfile.agent").read_text()
    assert "COPY --chown=srw:srw src/ ./src/" in dockerfile
    assert "COPY --chown=srw:srw orchestrator/" not in dockerfile

    tree = ast.parse((root / "src/shared/session_permission_retirement.py").read_text())
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "orchestrator" not in imported_roots


def _row(request_id: str, tool_call_id: str, token: int | None):
    return {
        "id": request_id,
        "tool_call_id": tool_call_id,
        "accepted_lease_token": token,
    }


@pytest.mark.asyncio
async def test_boundary_retires_all_older_generations_and_legacy_null(monkeypatch):
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            _row("00000000-0000-4000-8000-000000000001", "legacy", None),
            _row("00000000-0000-4000-8000-000000000002", "old", 7),
            _row("00000000-0000-4000-8000-000000000003", "previous", 9),
        ]
    )
    conn.fetchrow = AsyncMock(
        side_effect=lambda _sql, rid, _tid, token, _by: {
            "id": rid,
            "tool_call_id": {
                "00000000-0000-4000-8000-000000000001": "legacy",
                "00000000-0000-4000-8000-000000000002": "old",
                "00000000-0000-4000-8000-000000000003": "previous",
            }[rid],
            "accepted_lease_token": token,
        }
    )
    bump = AsyncMock(return_value=4)
    frame = AsyncMock(side_effect=[(4, 1), (4, 2), (4, 3)])
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    result = await mod.retire_stale_stateless_permissions(
        conn,
        thread_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        retired_lease_token=9,
        successor_lease_token=10,
        reason="lease_expired",
        epoch_already_bumped=False,
    )

    assert result.count == 3
    assert result.epoch_bumped is True
    bump.assert_awaited_once()
    assert [entry.args[3] for entry in conn.fetchrow.await_args_list] == [None, 7, 9]
    update_sql = conn.fetchrow.await_args_list[0].args[0]
    assert "IS NOT DISTINCT FROM $3::bigint" in update_sql
    assert [entry.kwargs["payload"] for entry in frame.await_args_list] == [
        {
            "id": "legacy",
            "approval_id": "00000000-0000-4000-8000-000000000001",
            "decision": "expired",
            "reason": "lease_expired",
            "accepted_lease_token": None,
            "legacy_unbound": True,
        },
        {
            "id": "old",
            "approval_id": "00000000-0000-4000-8000-000000000002",
            "decision": "expired",
            "reason": "lease_expired",
            "accepted_lease_token": 7,
            "legacy_unbound": False,
        },
        {
            "id": "previous",
            "approval_id": "00000000-0000-4000-8000-000000000003",
            "decision": "expired",
            "reason": "lease_expired",
            "accepted_lease_token": 9,
            "legacy_unbound": False,
        },
    ]
    assert all(
        entry.kwargs["permission_request_id"] == entry.kwargs["payload"]["approval_id"]
        for entry in frame.await_args_list
    )


@pytest.mark.asyncio
async def test_approval_winner_is_not_selected_and_gets_no_receipt(monkeypatch):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock()
    bump = AsyncMock()
    frame = AsyncMock()
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    result = await mod.retire_stale_stateless_permissions(
        conn,
        thread_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        retired_lease_token=4,
        successor_lease_token=5,
        reason="force_end",
        epoch_already_bumped=False,
    )

    assert result.count == 0
    assert "status = 'pending'" in conn.fetch.await_args.args[0]
    bump.assert_not_awaited()
    frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_boundary_epoch_is_reused_without_second_bump(monkeypatch):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[_row("rid", "tc", 2)])
    conn.fetchrow = AsyncMock(
        return_value={"id": "rid", "tool_call_id": "tc", "accepted_lease_token": 2}
    )
    bump = AsyncMock()
    frame = AsyncMock(return_value=(8, 4))
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    result = await mod.retire_stale_stateless_permissions(
        conn,
        thread_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        retired_lease_token=2,
        successor_lease_token=3,
        reason="lease_expired",
        epoch_already_bumped=True,
    )

    assert result.receipts[0].seq == 4
    assert result.epoch_bumped is False
    bump.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_or_nonconsecutive_boundary_fails_closed():
    conn = MagicMock()
    conn.fetch = AsyncMock()
    with pytest.raises(ValueError, match="one exact token bump"):
        await mod.retire_stale_stateless_permissions(
            conn,
            thread_id="tid",
            retired_lease_token=3,
            successor_lease_token=5,
            reason="lease_expired",
            epoch_already_bumped=False,
        )
    conn.fetch.assert_not_awaited()
