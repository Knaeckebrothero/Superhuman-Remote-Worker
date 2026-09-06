"""The cockpit must not carry a hand-maintained mirror of the tool vocabulary.

This file used to pin two such mirrors — ``SESSION_TOOL_GROUP_NAMES`` (a copy
of ``SESSION_TOOL_OVERRIDE_NAMES``) and ``SESSION_TOOL_GROUP_BASE_ENABLED`` (a
copy of ``config/session_base.yaml``'s enablement) — against drift. Pinning
them was the right thing to do while they existed, and the drift it guarded
against was real: a stale copy put the wrong tick on the screen.

They are gone. The cockpit no longer needs either fact:

* to turn a category ON it sends a POLICY (``true``, or the ``enumerate_only``
  enumeration the response serves for a category that refuses ``true``), and
  the write boundary expands it against the registry;
* to know whether a category IS on it reads the resolved answer, which is the
  agent's own report where there is one — a strictly better source than the
  base config, which cannot see the runtime injection layer, the backend
  capability gate, or a tool that failed to instantiate.

So the test inverts. Rather than checking a copy is faithful, it checks the
copy has not come back. A mirror that must be kept in sync is a defect with a
delay on it, and this whole change is about removing that species.
"""

import re
from pathlib import Path

TYPES_FILE = (
    Path(__file__).resolve().parents[1]
    / "cockpit/src/app/views/agent-settings/agent-settings.types.ts"
)

#: Symbols whose whole job was to duplicate a backend fact in TypeScript.
RETIRED_MIRRORS = (
    "SESSION_TOOL_GROUP_NAMES",
    "SESSION_TOOL_GROUP_BASE_ENABLED",
    "toolGroupDefaultsConfig",
    "LIVE_TOOL_CATEGORIES",
)


def _cockpit_sources() -> list[Path]:
    root = TYPES_FILE.parents[4]  # the cockpit/ project root
    return [p for p in (root / "src").rglob("*.ts") if "node_modules" not in p.parts]


def test_the_retired_mirrors_stay_retired():
    offenders: dict[str, list[str]] = {}
    for path in _cockpit_sources():
        text = path.read_text(encoding="utf-8")
        for name in RETIRED_MIRRORS:
            # Match a real identifier use, not the name inside a comment
            # explaining why it is gone.
            for line in text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith(("*", "//", "/*")):
                    continue
                if re.search(rf"\b{name}\b", line):
                    offenders.setdefault(name, []).append(f"{path.name}: {stripped}")
    assert not offenders, (
        "a retired tool-vocabulary mirror is back in the cockpit. Turning a "
        "category on is a POLICY the write boundary expands against the "
        "registry (src/core/tool_policy.py), and whether it IS on comes from "
        f"GET /api/persistent/threads/{{id}}/tool-groups. Found: {offenders}"
    )


def test_the_tool_name_lists_are_gone_from_the_types_module():
    """The narrow version of the above, on the file that held them."""
    source = TYPES_FILE.read_text(encoding="utf-8")
    assert "get_session_context" not in source, (
        "agent-settings.types.ts names individual tools again. The cockpit has "
        "no business knowing which tools are in a category; the registry "
        "decides, server-side, at the write boundary."
    )


def test_the_write_vocabulary_is_served_not_transcribed():
    """`shell` is the one category a client must enumerate — from the server."""
    from shared.runtime.core.tool_policy import (
        ENUMERATE_ONLY_CATEGORIES,
        enumerate_only_members,
    )

    served = enumerate_only_members()
    assert set(served) == set(ENUMERATE_ONLY_CATEGORIES)
    source = TYPES_FILE.read_text(encoding="utf-8")
    for names in served.values():
        for name in names:
            assert name not in source, (
                f"{name} is hardcoded in the cockpit. It is served on every "
                "tool-groups response as `enumerate_only`."
            )
