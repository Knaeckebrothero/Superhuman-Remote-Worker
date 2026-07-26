"""Pin the cockpit's mirror of the session tool-group vocabulary.

The live settings pane re-enables a tool group by sending the group's full
canonical tool list (agent-settings.types.ts SESSION_TOOL_GROUP_NAMES); the
agent and orchestrator validate it against the closed vocabulary in
src/core/session_tool_overrides.py. A drift between the two turns a re-enable
toggle into a hard 422 — this test fails the build instead.
"""

import re
from pathlib import Path

from src.core.loader import load_and_merge_config, resolve_config_path
from src.core.session_tool_overrides import SESSION_TOOL_OVERRIDE_NAMES

TYPES_FILE = (
    Path(__file__).resolve().parents[1]
    / "cockpit/src/app/views/agent-settings/agent-settings.types.ts"
)


def _parse_ts_mirror() -> dict[str, frozenset[str]]:
    src = TYPES_FILE.read_text(encoding="utf-8")
    match = re.search(
        r"export const SESSION_TOOL_GROUP_NAMES[^=]*=\s*\{(.*?)\n\};",
        src,
        re.DOTALL,
    )
    assert match, "SESSION_TOOL_GROUP_NAMES not found in agent-settings.types.ts"
    body = match.group(1)
    groups: dict[str, frozenset[str]] = {}
    for group_match in re.finditer(r"(\w+):\s*\[(.*?)\]", body, re.DOTALL):
        names = re.findall(r"'([^']+)'", group_match.group(2))
        groups[group_match.group(1)] = frozenset(names)
    return groups


def _parse_ts_base_enabled() -> dict[str, bool]:
    src = TYPES_FILE.read_text(encoding="utf-8")
    match = re.search(
        r"export const SESSION_TOOL_GROUP_BASE_ENABLED[^=]*=\s*\{(.*?)\n\};",
        src,
        re.DOTALL,
    )
    assert match, "SESSION_TOOL_GROUP_BASE_ENABLED not found in agent-settings.types.ts"
    return {
        name: value == "true"
        for name, value in re.findall(r"(\w+):\s*(true|false)", match.group(1))
    }


def _session_base_group_enablement() -> dict[str, bool]:
    """Ground truth: a group is enabled iff session_base declares it non-empty.

    Goes through the loader rather than raw yaml.safe_load so a future
    ``$extends`` on session_base is followed. The settings matrix that
    ``resolve_config`` layers on top writes only llm/limits/shell.mode, so
    skipping it cannot move a tool group.
    """
    base_path, _ = resolve_config_path("session_base")
    tools = (load_and_merge_config(base_path) or {}).get("tools") or {}
    return {group: bool(tools.get(group)) for group in SESSION_TOOL_OVERRIDE_NAMES}


def test_cockpit_mirror_matches_backend_vocabulary():
    mirror = _parse_ts_mirror()
    backend = {k: frozenset(v) for k, v in SESSION_TOOL_OVERRIDE_NAMES.items()}
    assert mirror == backend, (
        "cockpit SESSION_TOOL_GROUP_NAMES drifted from "
        "src/core/session_tool_overrides.py SESSION_TOOL_OVERRIDE_NAMES — "
        f"cockpit-only: { {k: sorted(v - backend.get(k, frozenset())) for k, v in mirror.items()} }, "
        f"backend-only: { {k: sorted(v - mirror.get(k, frozenset())) for k, v in backend.items()} }"
    )


def test_cockpit_base_enabled_mirror_matches_session_base_yaml():
    """The cockpit's fallback defaults must match what the base config ships.

    This is the client half of the "checkbox says on, agent has no tools" bug:
    when the tool-groups endpoint is unavailable the pane falls back to this
    map, and a stale copy would put the wrong tick back on the screen.
    """
    mirror = _parse_ts_base_enabled()
    expected = _session_base_group_enablement()
    assert mirror == expected, (
        "cockpit SESSION_TOOL_GROUP_BASE_ENABLED drifted from "
        "config/session_base.yaml tool groups — "
        f"cockpit: {mirror}, session_base.yaml: {expected}"
    )


def test_cockpit_base_enabled_covers_every_closed_group():
    """A group missing from the map would read ``undefined`` → wrongly disabled."""
    assert set(_parse_ts_base_enabled()) == set(SESSION_TOOL_OVERRIDE_NAMES)
