"""Acknowledged drift is skipped at attach; unacknowledged drift still denies."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.main import _apply_acknowledged_grant_drift
from orchestrator.services.config_drift import (
    acknowledged_drift_ids,
    acknowledged_grant_keys,
    strip_acknowledged,
)


DS_GONE = "d7555d5d-ce46-49e2-b1fa-8235d720badc"
DS_OK = "2991589e-249d-4cca-98ce-780db69b2520"


def test_acknowledged_drift_ids_reads_metadata():
    metadata = {"config_drift_ack": {f"connector:{DS_GONE}": "deleted"}}
    assert acknowledged_drift_ids(metadata) == {f"connector:{DS_GONE}"}


def test_acknowledged_drift_ids_tolerates_a_json_string():
    """asyncpg hands back JSONB as a raw string; a guard that only accepts dict
    silently disables the feature."""
    metadata = '{"config_drift_ack": {"connector:x": "deleted"}}'
    assert acknowledged_drift_ids(metadata) == {"connector:x"}


def test_acknowledged_drift_ids_missing_key_is_empty():
    assert acknowledged_drift_ids({}) == set()


def test_strip_acknowledged_removes_only_acked_ids():
    result = strip_acknowledged(
        [DS_OK, DS_GONE], {f"connector:{DS_GONE}"}, prefix="connector"
    )
    assert result == [DS_OK]


def test_strip_acknowledged_leaves_unacked_ids_in_place():
    result = strip_acknowledged([DS_OK, DS_GONE], set(), prefix="connector")
    assert result == [DS_OK, DS_GONE]


def test_acknowledged_grant_keys_unprefixes_only_grants():
    metadata = {
        "config_drift_ack": {
            "grant:shell_tools": "revoked",
            f"connector:{DS_GONE}": "deleted",
        }
    }
    assert acknowledged_grant_keys(metadata) == {"shell_tools"}


@pytest.mark.asyncio
async def test_unacknowledged_grant_violation_is_not_stripped():
    """Acknowledging ONE grant must never smuggle a different one through.
    With shell_tools acked but vm_workspace also violating, the fragment must
    come back untouched so the dispatch PEP still denies."""
    merged = {"tools": {"shell": True}, "workspace": {"backend": "vm"}}
    grants = {"shell_tools": False, "vm_workspace": False}

    with patch(
        "orchestrator.main._resolve_runner_grants",
        AsyncMock(return_value=grants),
    ):
        result = await _apply_acknowledged_grant_drift(
            merged,
            acknowledged={"shell_tools"},
            runner_user_id="u1",
            project_ids=[],
        )

    assert result == merged


@pytest.mark.asyncio
async def test_fully_acknowledged_grant_violations_are_stripped():
    merged = {"tools": {"shell": True}, "workspace": {"backend": "vm"}}
    grants = {"shell_tools": False, "vm_workspace": False}

    with patch(
        "orchestrator.main._resolve_runner_grants",
        AsyncMock(return_value=grants),
    ):
        result = await _apply_acknowledged_grant_drift(
            merged,
            acknowledged={"shell_tools", "vm_workspace"},
            runner_user_id="u1",
            project_ids=[],
        )

    assert "shell" not in result.get("tools", {})
    assert "backend" not in result.get("workspace", {})
    # The original must not be mutated in place — callers reuse it.
    assert merged["tools"]["shell"] is True


@pytest.mark.asyncio
async def test_admin_bypass_returns_the_fragment_untouched():
    merged = {"tools": {"shell": True}}

    with patch(
        "orchestrator.main._resolve_runner_grants",
        AsyncMock(return_value=None),
    ):
        result = await _apply_acknowledged_grant_drift(
            merged,
            acknowledged={"shell_tools"},
            runner_user_id="u1",
            project_ids=[],
        )

    assert result == merged
