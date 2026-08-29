"""Every tool name in a shipped config must exist in the registry.

A name that no longer exists makes ``load_tools`` raise ``ValueError`` for the
WHOLE batch (registry.py validates all names up front), which drops the caller
into a per-tool fallback loop — ``_setup_tools`` for sessions
(``persistent_session.py``), ``_setup_job_tools`` for worker jobs
(``agent.py``). Both loops swallow individual failures at DEBUG, so the run
still starts and the only symptom is a single warning. The batch validation is
the safety net, and one dead name spends it for everyone.

``browse_website`` / ``download_from_website`` sat in these configs long after
being removed from the registry and did exactly this on every session start and
every worker job. This pins the class of bug, not just those two names.

Configs are checked in their MERGED form, so a stale name inherited from a base
through ``$extends`` is caught in the expert that actually loads it.
"""

from pathlib import Path

import pytest

from src.core.loader import load_and_merge_config, resolve_config_path
from src.tools.registry import TOOL_REGISTRY

# Wildcards are expanded at load time against the live registry, not looked up.
_WILDCARDS = {"*"}

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

# The chain roots by their public names: the shared root and the three role
# overlays (each checked MERGED, i.e. expert_base + overlay).
_BASE_CONFIGS = ["expert_base", "session_base", "subagent_base", "worker_base"]


def _expert_config_names() -> list[str]:
    """Every bundled expert, discovered — so a new one is covered on arrival."""
    return sorted(
        p.parent.name
        for p in _CONFIG_DIR.glob("experts/*/config.yaml")
        if p.parent.name != "__pycache__"
    )


def _library_config_names() -> list[str]:
    """Every subagent-library entry (``config/subagents/<name>``), discovered."""
    return sorted(
        f"subagents/{p.parent.name}"
        for p in _CONFIG_DIR.glob("subagents/*/config.yaml")
        if p.parent.name != "__pycache__"
    )


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


@pytest.mark.parametrize(
    "config_name", _BASE_CONFIGS + _expert_config_names() + _library_config_names()
)
def test_config_tool_names_all_exist_in_registry(config_name):
    unknown = [
        f"tools.{category}: {name}"
        for category, name in _config_tool_names(config_name)
        if name not in TOOL_REGISTRY
    ]
    assert not unknown, (
        f"config '{config_name}' references tool names that are not in "
        "TOOL_REGISTRY — every run on this config fails the batch load and "
        f"degrades into the silent per-tool fallback: {unknown}"
    )


def test_experts_are_actually_discovered():
    """Guard the guard: an empty glob would make the parametrization vacuous."""
    experts = _expert_config_names()
    assert len(experts) >= 4, experts
    assert "developer" in experts


@pytest.mark.parametrize("config_name", _BASE_CONFIGS)
def test_base_configs_declare_some_tools(config_name):
    """Guard the guard: an empty parse would make the check above vacuous."""
    assert len(_config_tool_names(config_name)) > 20


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
