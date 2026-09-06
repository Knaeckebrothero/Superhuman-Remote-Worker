"""The Centurion's job grant is pinned to the officer caller defaults (E5).

officer_supervision_surface.md §3 splits the job surface into planes and
generates the officer lane's defaults from the shared descriptors (E2):
7 ``job_control`` writes + 18 ``job_observability`` reads + 3 ``job_evidence``
reads, and **zero** ``job_workspace``-plane tools. The Centurion expert config
is the reviewed, checked-in claim of that same surface.

The claim is spelled as explicit name lists rather than ``true`` because
config-time policy expansion is lane-blind: ``true`` resolves to the
session-grantable subset, which would claim the two workspace-plane reads
(``get_job_file``/``list_job_files``) and could never claim the
``grant: "explicit"`` officer tools (``steer_job``, ``get_job_progress``, …).
An explicit list is exempt from both problems — and this pin is what keeps it
from drifting: when the descriptor policy moves, this fails and the config
must be updated (and the grants snapshot regenerated) as a reviewed diff.

Runtime enforcement is separate and unchanged: the officer lane appends
``caller_default_names("officer", …)`` at bind time and
``registry.apply_officer_tool_ceiling`` refuses ``plane="job_workspace"``
even for names smuggled in by a thread override.
"""

from pathlib import Path

import yaml

from shared.orch_surface.jobs import caller_default_names

_CONFIG = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "experts"
    / "centurion"
    / "config.yaml"
)


def _declared(group: str) -> list[str]:
    tools = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))["tools"]
    value = tools[group]
    assert isinstance(value, list), (
        f"tools.{group} must stay an explicit reviewed list (see module "
        "docstring); policy forms cannot claim the officer lane's "
        "grant='explicit' tools."
    )
    return value


class TestCenturionJobGrantMirrorsOfficerDefaults:
    def test_job_control_is_exactly_the_officer_lane(self):
        declared = _declared("job_control")
        expected = caller_default_names("officer", "job_control")
        assert set(declared) == set(expected), (
            "config/experts/centurion job_control drifted from the generated "
            f"officer defaults. Missing: {sorted(set(expected) - set(declared))}; "
            f"extra: {sorted(set(declared) - set(expected))}. Update the config "
            "and regenerate tests/fixtures/config_tool_grants.json."
        )

    def test_job_inspection_is_exactly_the_officer_lane(self):
        declared = _declared("job_inspection")
        expected = caller_default_names("officer", "job_inspection")
        assert set(declared) == set(expected), (
            "config/experts/centurion job_inspection drifted from the generated "
            f"officer defaults. Missing: {sorted(set(expected) - set(declared))}; "
            f"extra: {sorted(set(declared) - set(expected))}. Update the config "
            "and regenerate tests/fixtures/config_tool_grants.json."
        )

    def test_no_declared_job_tool_is_workspace_plane(self):
        """Belt to the ceiling's braces: the reviewed claim itself must never
        name an object-plane job read (officer_supervision_surface §3.4)."""
        from agent.tools.registry import TOOL_REGISTRY

        declared = _declared("job_control") + _declared("job_inspection")
        offenders = sorted(
            name
            for name in declared
            if TOOL_REGISTRY.get(name, {}).get("plane") == "job_workspace"
        )
        assert not offenders, (
            f"{offenders} are job_workspace-plane tools; the background "
            "officer's grant must not claim the object plane."
        )

    def test_the_evidence_tools_are_claimed(self):
        """The three E4 evidence reads are the officer's disposition material;
        the prompt doctrine names them, so the grant must carry them."""
        declared = set(_declared("job_inspection"))
        assert {
            "get_job_completion_report",
            "list_job_evidence",
            "read_job_evidence",
        } <= declared
