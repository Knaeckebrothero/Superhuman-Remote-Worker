"""Pin the cockpit's mirror of the session tool-group vocabulary.

The live settings pane re-enables a tool group by sending the group's full
canonical tool list (agent-settings.types.ts SESSION_TOOL_GROUP_NAMES); the
agent and orchestrator validate it against the closed vocabulary in
src/core/session_tool_overrides.py. A drift between the two turns a re-enable
toggle into a hard 422 — this test fails the build instead.
"""

import re
from pathlib import Path

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


def test_cockpit_mirror_matches_backend_vocabulary():
    mirror = _parse_ts_mirror()
    backend = {k: frozenset(v) for k, v in SESSION_TOOL_OVERRIDE_NAMES.items()}
    assert mirror == backend, (
        "cockpit SESSION_TOOL_GROUP_NAMES drifted from "
        "src/core/session_tool_overrides.py SESSION_TOOL_OVERRIDE_NAMES — "
        f"cockpit-only: { {k: sorted(v - backend.get(k, frozenset())) for k, v in mirror.items()} }, "
        f"backend-only: { {k: sorted(v - mirror.get(k, frozenset())) for k, v in backend.items()} }"
    )
