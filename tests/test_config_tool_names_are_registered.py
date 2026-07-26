"""Every tool name in the session base config must exist in the registry.

A name that no longer exists makes ``load_tools`` raise ``ValueError`` for the
WHOLE batch (registry.py validates all names up front), which drops the
persistent session into the per-tool fallback loop in ``_setup_tools``. That
loop swallows individual failures at DEBUG, so the session still starts and the
only symptom is a single warning — real bind failures hide behind it.

``browse_website`` / ``download_from_website`` sat in session_base long after
being removed from the registry and did exactly this on every session start.
This pins the class of bug, not just those two names.
"""

import pytest

from src.core.loader import load_and_merge_config, resolve_config_path
from src.tools.registry import TOOL_REGISTRY

# Wildcards are expanded at load time against the live registry, not looked up.
_WILDCARDS = {"*"}


def _config_tool_names(config_name: str) -> list[tuple[str, str]]:
    path, _ = resolve_config_path(config_name)
    tools = (load_and_merge_config(path) or {}).get("tools") or {}
    return [
        (category, name)
        for category, names in tools.items()
        if isinstance(names, list)
        for name in names
        if isinstance(name, str) and name not in _WILDCARDS
    ]


def test_session_base_tool_names_all_exist_in_registry():
    unknown = [
        f"tools.{category}: {name}"
        for category, name in _config_tool_names("session_base")
        if name not in TOOL_REGISTRY
    ]
    assert not unknown, (
        "config/session_base.yaml references tool names that are not in "
        "TOOL_REGISTRY — every session start will fail the batch load and "
        f"degrade into the silent per-tool fallback: {unknown}"
    )


def test_session_base_declares_some_tools():
    """Guard the guard: an empty parse would make the test above vacuous."""
    assert len(_config_tool_names("session_base")) > 20


@pytest.mark.parametrize("category", ["orchestrator", "agent_catalog", "workflows"])
def test_session_base_control_groups_ship_empty(category):
    """The default-off policy the Settings→Tools checkboxes must reflect.

    If a base ever turns one of these on, the cockpit's fallback mirror
    (SESSION_TOOL_GROUP_BASE_ENABLED) has to move with it — that pairing is
    enforced by tests/test_session_tool_group_mirror.py.
    """
    path, _ = resolve_config_path("session_base")
    tools = (load_and_merge_config(path) or {}).get("tools") or {}
    assert tools.get(category) == []
