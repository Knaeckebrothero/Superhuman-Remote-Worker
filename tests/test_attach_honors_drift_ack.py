"""Acknowledged drift is skipped at attach; unacknowledged drift still denies.

Round-1 fix: ``_strip_acknowledged_grants`` is SYNC and pure (no grant
resolution inside it) so it can run as ``resolve_config``'s ``grant_strip``
hook on the fully-merged ``data`` — not just on the detached
``capture["merged_fragment"]`` copy the PDP evaluates. Grants are resolved by
the caller (``_resolve_session_config``) and handed in directly; "admin
bypass" is therefore the CALLER's concern now (it never builds a grant_strip
closure when ``_resolve_runner_grants`` returns ``None``), not this helper's
— see ``test_resolve_session_config_strips_the_delivered_blob_not_just_the_capture``
below for the wiring, and ``tests/test_session_toolset_measurement.py`` for
existing broad coverage of ``_resolve_runner_grants``'s admin short-circuit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.main import _resolve_session_config, _strip_acknowledged_grants
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


def test_unacknowledged_grant_violation_is_not_stripped():
    """Acknowledging ONE grant must never smuggle a different one through.
    With shell_tools acked but vm_workspace also violating, the fragment must
    come back untouched so the dispatch PEP still denies."""
    merged = {"tools": {"shell": True}, "workspace": {"backend": "vm"}}
    grants = {"shell_tools": False, "vm_workspace": False}

    result = _strip_acknowledged_grants(merged, grants, {"shell_tools"})

    assert result == merged


def test_fully_acknowledged_grant_violations_are_stripped():
    merged = {"tools": {"shell": True}, "workspace": {"backend": "vm"}}
    grants = {"shell_tools": False, "vm_workspace": False}

    result = _strip_acknowledged_grants(
        merged, grants, {"shell_tools", "vm_workspace"}
    )

    assert "shell" not in result.get("tools", {})
    assert "backend" not in result.get("workspace", {})
    # The original must not be mutated in place — callers reuse it.
    assert merged["tools"]["shell"] is True


def test_no_violations_returns_the_fragment_unchanged():
    merged = {"tools": {"shell": True}}

    result = _strip_acknowledged_grants(merged, {"shell_tools": True}, {"shell_tools"})

    assert result == merged


@pytest.mark.asyncio
async def test_resolve_session_config_strips_the_delivered_blob_not_just_the_capture():
    """MANDATORY end-to-end regression for the round-1 privilege-escalation
    finding: every earlier grant test called the stripping helper in
    isolation, which is exactly why the leak shipped — the helper reassigned
    ``_cap["merged_fragment"]`` (a detached deepcopy taken purely for the PDP)
    while the actually-delivered blob was already built from
    ``resolve_config``'s internal ``data`` beforehand. A user who acknowledged
    ``shell_tools`` stopped getting denied, but the agent still hydrated
    ``tools.shell`` with the full tool list — the capability the grant
    revoked came back.

    Drives ``_resolve_session_config`` for real (``resolve_config`` itself is
    NOT mocked — that is precisely the code path the bug lived in) and
    asserts both halves: no ``GrantDenied``, AND the returned blob's
    ``agent.tools.shell`` is empty.
    """
    thread = {"id": "11111111-1111-4111-8111-111111111111", "user_id": "u1"}
    metadata = {"config_drift_ack": {"grant:shell_tools": "revoked"}}

    with (
        patch("orchestrator.main._is_experts_db_enabled", return_value=True),
        patch(
            "orchestrator.main._user_experts_enabled", AsyncMock(return_value=True)
        ),
        patch(
            "orchestrator.main._resolve_session_account_defaults",
            AsyncMock(return_value={}),
        ),
        patch(
            "orchestrator.main._gather_in_scope_skills", AsyncMock(return_value={})
        ),
        patch("orchestrator.main._thread_project_ids", AsyncMock(return_value=[])),
        patch(
            "orchestrator.main._resolve_runner_grants",
            AsyncMock(return_value={"shell_tools": False}),
        ),
        patch(
            "orchestrator.main._inject_thread_dispatch_credentials",
            AsyncMock(side_effect=lambda co, **_kw: co),
        ),
    ):
        delivered = await _resolve_session_config(
            thread,
            metadata,
            config_override={"tools": {"shell": ["run_command"]}},
        )

    assert delivered is not None
    assert not delivered["agent"]["tools"].get("shell")
