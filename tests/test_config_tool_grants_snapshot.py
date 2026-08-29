"""Every shipped config's tool grants are pinned to a checked-in snapshot.

A config refactor must not change what an agent can do as a side effect. On
2026-07-22 commit ``57430a2a`` renamed ``persistent_defaults.yaml`` →
``session_base.yaml`` and ``defaults.yaml`` → ``worker_base.yaml``, and in the
same diff dropped ``tools.shell`` from the session base and ``tools.shell`` +
``tools.delegation`` from the worker base. The commit was titled a chore and its
body reads "No functional changes introduced." Sessions ran without shell for
eleven days before a user noticed. Nothing in CI objected, because nothing
asserted what the configs grant — only that the names they *do* list exist
(``test_config_tool_names_are_registered.py``, which passes happily on an empty
list).

This is the tripwire for that class of change. It does not judge whether a grant
is right; it forces every change to one to be deliberate and reviewed. When a
diff here is intended, regenerate and let the fixture diff carry the intent:

    UPDATE_TOOL_GRANTS_SNAPSHOT=1 pytest tests/test_config_tool_grants_snapshot.py

**Scope.** This pins the *config-derived* grant, which is precisely the input to
both runtime consumption paths — ``src/agent.py:3052`` for worker jobs and
``src/api/persistent_session.py:1411`` for sessions, both
``expand_tool_wildcards(get_all_tool_names(config))``. It is deliberately NOT
the agent's final bound toolset: the runtime then *adds* tools no config
mentions (``persistent_session.py:1408-1557`` injects ``sleep`` /
``notify_user``, plus datasource categories and the ``task_*`` family) and
*subtracts* via ``filter_tools_by_backend`` and the capability grants. Pinning
that final set needs a live agent to report it and is Phase 1.1 of
``knowledge-base/knowledge/issues/tool_configuration_defects_and_fix_roadmap.md``.

No shipped config uses a ``*`` wildcard today, so this snapshot is currently
independent of the registry's contents. ``expand_tool_wildcards`` is applied
anyway to stay faithful to the runtime; if a config ever adopts a wildcard, the
snapshot will legitimately move when that category gains a tool.
"""

import dataclasses
import json
import os
from pathlib import Path

import pytest

from src.core.loader import (
    ToolsConfig,
    get_all_tool_names,
    load_agent_config,
    load_and_merge_config,
    resolve_config_path,
)
from src.tools.registry import expand_tool_wildcards

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_DIR = _REPO_ROOT / "config"
_SNAPSHOT = Path(__file__).parent / "fixtures" / "config_tool_grants.json"

# The chain roots (the shared expert_base and the three role overlays, by
# their public names) plus the one standalone session profile. Experts are
# discovered.
_STANDALONE_CONFIGS = [
    "expert_base",
    "session_base",
    "subagent_base",
    "worker_base",
    "interactive",
]

_UPDATE = os.environ.get("UPDATE_TOOL_GRANTS_SNAPSHOT") == "1"

#: ``ToolsConfig`` field names — the only ``tools.*`` keys the loader reads.
#: A key outside this set is silently discarded, so a config can appear to grant
#: a category while granting nothing. ``config/schema.json`` cannot catch it.
_TOOLS_CONFIG_FIELDS = frozenset(f.name for f in dataclasses.fields(ToolsConfig))


def _expert_config_names() -> list[str]:
    """Every bundled expert, discovered — so a new one is covered on arrival."""
    return sorted(
        p.parent.name
        for p in _CONFIG_DIR.glob("experts/*/config.yaml")
        if p.parent.name != "__pycache__"
    )


def _all_config_names() -> list[str]:
    return _STANDALONE_CONFIGS + _expert_config_names()


def _resolved_tool_grants(config_name: str) -> list[str]:
    """The tool names this config grants, exactly as the runtime computes them."""
    path, deployment_dir = resolve_config_path(config_name)
    config = load_agent_config(path, deployment_dir)
    return sorted(expand_tool_wildcards(get_all_tool_names(config)))


def _current_snapshot() -> dict[str, list[str]]:
    return {name: _resolved_tool_grants(name) for name in _all_config_names()}


def _load_snapshot() -> dict[str, list[str]]:
    if not _SNAPSHOT.exists():
        pytest.fail(
            f"Snapshot missing at {_SNAPSHOT.relative_to(_REPO_ROOT)}. "
            "Regenerate with UPDATE_TOOL_GRANTS_SNAPSHOT=1 pytest "
            "tests/test_config_tool_grants_snapshot.py"
        )
    return json.loads(_SNAPSHOT.read_text())


def _write_snapshot(data: dict[str, list[str]]) -> None:
    _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    _SNAPSHOT.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


@pytest.fixture(scope="module", autouse=True)
def _maybe_regenerate():
    """Rewrite the fixture when explicitly asked, before any test reads it."""
    if _UPDATE:
        _write_snapshot(_current_snapshot())


@pytest.fixture(scope="module")
def snapshot() -> dict[str, list[str]]:
    return _load_snapshot()


@pytest.mark.parametrize("config_name", _all_config_names())
def test_config_tool_grants_match_snapshot(config_name, snapshot):
    """A config's granted tools must match the snapshot, exactly."""
    if config_name not in snapshot:
        pytest.fail(
            f"'{config_name}' has no snapshot entry. If this config is new, "
            "regenerate with UPDATE_TOOL_GRANTS_SNAPSHOT=1 and review the "
            "added grants in the fixture diff."
        )

    expected = set(snapshot[config_name])
    actual = set(_resolved_tool_grants(config_name))
    if expected == actual:
        return

    removed = sorted(expected - actual)
    added = sorted(actual - expected)
    pytest.fail(
        f"Tool grants for '{config_name}' changed.\n"
        f"  REVOKED ({len(removed)}): {removed or '-'}\n"
        f"  GRANTED ({len(added)}): {added or '-'}\n"
        "If deliberate, regenerate with UPDATE_TOOL_GRANTS_SNAPSHOT=1 and let "
        "the fixture diff show the intent in review. If not, a config change "
        "has altered what this agent can do — see "
        "knowledge-base/knowledge/issues/tool_configuration_defects_and_fix_roadmap.md."
    )


def test_snapshot_covers_every_shipped_config(snapshot):
    """A new expert must arrive with its grants reviewed, not unpinned."""
    assert sorted(snapshot) == sorted(_all_config_names()), (
        "Snapshot keys drifted from the shipped configs. Regenerate with "
        "UPDATE_TOOL_GRANTS_SNAPSHOT=1 and review the diff."
    )


@pytest.mark.parametrize("config_name", _all_config_names())
def test_declared_tool_categories_are_read_by_the_loader(config_name):
    """A ``tools.*`` key the loader does not read is silently discarded.

    ``ToolsConfig`` now has one field per registry category plus ``mcp``, so the
    only keys that go nowhere are typos (``tools.workspaces``). Until
    2026-08-02 it had 23 fields against the registry's 24 categories, and
    ``tools.product_help:`` / ``tools.session_task:`` parsed and did nothing;
    those fields now exist and the three vocabularies are pinned to agree by
    ``tests/test_tool_policy.py::TestCategoryVocabularyAgreement``. This
    assertion is unchanged and still the tripwire for a key that goes nowhere.
    """
    path, _ = resolve_config_path(config_name)
    declared = (load_and_merge_config(path) or {}).get("tools") or {}
    unread = sorted(set(declared) - _TOOLS_CONFIG_FIELDS)
    assert not unread, (
        f"'{config_name}' declares tools.{{{', '.join(unread)}}}, which "
        "ToolsConfig has no field for — the loader discards these silently. "
        "Either add the field or remove the key."
    )
