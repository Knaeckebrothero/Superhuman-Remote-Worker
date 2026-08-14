"""The job-surface caller-default policy is pinned to a checked-in snapshot.

``caller_defaults`` on a descriptor decides which lanes (mcp / officer /
session) receive a job tool by default — it IS the authorization surface of
the unified job toolset. A one-token flip (``_MCP`` -> ``_ALL`` on
``delete_job``) would silently hand destructive controls to every session,
and nothing else in CI would object: the descriptor tests derive their
expectations from the same source they check.

This is the independent literal fixture (same mechanism as
``tests/fixtures/config_tool_grants.json``): the expectation lives in a
checked-in JSON file, so any caller-default change produces a failing diff
and must be regenerated deliberately:

    UPDATE_JOB_SURFACE_POLICY_SNAPSHOT=1 pytest tests/test_job_surface_policy_snapshot.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.shared.orch_surface.jobs import JOB_DESCRIPTORS, caller_default_names

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SNAPSHOT = Path(__file__).parent / "fixtures" / "job_surface_caller_defaults.json"

_CALLER_KINDS = ("mcp", "officer", "session")
_GROUPS = ("job_control", "job_inspection")

_UPDATE = os.environ.get("UPDATE_JOB_SURFACE_POLICY_SNAPSHOT") == "1"


def _current_snapshot() -> dict[str, dict[str, list[str]]]:
    return {
        kind: {group: sorted(caller_default_names(kind, group)) for group in _GROUPS}
        for kind in _CALLER_KINDS
    }


def _load_snapshot() -> dict[str, dict[str, list[str]]]:
    if not _SNAPSHOT.exists():
        pytest.fail(
            f"Snapshot missing at {_SNAPSHOT.relative_to(_REPO_ROOT)}. "
            "Regenerate with UPDATE_JOB_SURFACE_POLICY_SNAPSHOT=1 pytest "
            "tests/test_job_surface_policy_snapshot.py"
        )
    return json.loads(_SNAPSHOT.read_text())


def _write_snapshot(data: dict[str, dict[str, list[str]]]) -> None:
    _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    _SNAPSHOT.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


@pytest.fixture(scope="module", autouse=True)
def _maybe_regenerate():
    """Rewrite the fixture when explicitly asked, before any test reads it."""
    if _UPDATE:
        _write_snapshot(_current_snapshot())


@pytest.fixture(scope="module")
def snapshot() -> dict[str, dict[str, list[str]]]:
    return _load_snapshot()


@pytest.mark.parametrize("caller_kind", _CALLER_KINDS)
@pytest.mark.parametrize("group", _GROUPS)
def test_caller_defaults_match_snapshot(caller_kind, group, snapshot):
    """Per (caller_kind, group): the sorted default names, exactly."""
    expected = snapshot.get(caller_kind, {}).get(group)
    assert expected is not None, (
        f"snapshot has no entry for ({caller_kind}, {group}); regenerate with "
        "UPDATE_JOB_SURFACE_POLICY_SNAPSHOT=1 and review the diff."
    )
    actual = sorted(caller_default_names(caller_kind, group))
    if actual == expected:
        return
    revoked = sorted(set(expected) - set(actual))
    granted = sorted(set(actual) - set(expected))
    pytest.fail(
        f"Caller defaults for ({caller_kind}, {group}) changed.\n"
        f"  REVOKED ({len(revoked)}): {revoked or '-'}\n"
        f"  GRANTED ({len(granted)}): {granted or '-'}\n"
        "If deliberate, regenerate with UPDATE_JOB_SURFACE_POLICY_SNAPSHOT=1 "
        "and let the fixture diff carry the intent in review."
    )


def test_snapshot_covers_every_lane_and_group(snapshot):
    assert sorted(snapshot) == sorted(_CALLER_KINDS)
    for kind in _CALLER_KINDS:
        assert sorted(snapshot[kind]) == sorted(_GROUPS)


def test_snapshot_covers_every_descriptor(snapshot):
    """Every descriptor must appear in at least the mcp lane's defaults or be
    an explicit-grant tool; a descriptor in NO lane's defaults and without a
    grant mark would be unreachable by policy."""
    pinned = {
        name
        for kind in _CALLER_KINDS
        for group in _GROUPS
        for name in snapshot[kind][group]
    }
    for item in JOB_DESCRIPTORS:
        assert item.name in pinned or item.grant is not None, item.name


@pytest.mark.parametrize("name", ["delete_job", "assign_job", "promote_job"])
def test_destructive_admin_controls_stay_mcp_only(name):
    """Explicit assertions, independent of the fixture: these three never
    appear in session or officer defaults."""
    item = next(item for item in JOB_DESCRIPTORS if item.name == name)
    assert item.caller_defaults == frozenset({"mcp"}), name
    assert name in caller_default_names("mcp", "job_control")
    assert name not in caller_default_names("session", "job_control")
    assert name not in caller_default_names("officer", "job_control")
