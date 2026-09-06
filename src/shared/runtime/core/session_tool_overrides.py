"""The user-selectable persistent-session application tool groups.

These are the closed groups the New Session / Settings→Tools UI has always
rendered as independent switches, and the set the
``/api/persistent/threads/{id}/tool-groups`` endpoint answers for.  They are a
**presentation** vocabulary: which groups the product offers as one checkbox.

They are no longer a **validation** vocabulary.  Request-boundary validation
moved to ``shared.runtime.core.tool_policy.validate_tool_override_fragment``, which checks
every category against the registry instead of four against a hand-curated
name list.  Keeping the list in that second role was the defect: the boundary
honoured these four and silently discarded the other eight categories the form
renders, so unticking ``research`` or ``shell`` was accepted and thrown away.
It also left the smuggling case — ``tools.canvas = ["run_command"]``, which the
loader resolves by registry metadata rather than by the key it arrived under —
open on the job surface, which never consulted this list at all.

The safety judgement this list used to carry now lives in registry metadata as
``grant: "explicit"`` (``src/agent/tools/registry.py``), which is what keeps a
category-level ``true`` for one of these groups from widening past the names
below.  ``tests/test_tool_grant_classification.py`` pins the two to agree.

Keep this module framework-free — the orchestrator, the agent runtime and the
cockpit-mirror test all read it.
"""

from __future__ import annotations

from typing import Any

from shared.orch_surface.jobs import caller_default_names


SESSION_TOOL_OVERRIDE_NAMES: dict[str, frozenset[str]] = {
    "orchestrator": frozenset(
        {
            "get_session_context",
            "get_current_project",
            "list_project_jobs",
            "list_project_repositories",
            "get_default_project_repository",
        }
    ),
    "job_control": caller_default_names("session", "job_control"),
    "job_inspection": caller_default_names("session", "job_inspection"),
    "agent_catalog": frozenset(
        {
            "list_experts",
            "get_expert",
            "list_skills",
            "search_skills",
            "get_skill",
        }
    ),
    "workflows": frozenset(
        {
            "list_automations",
            "get_automation",
            "list_automation_runs",
            "propose_automation",
            "get_project_loop",
            "list_project_loop_jobs",
            "explain_project_loop",
        }
    ),
    "canvas": frozenset({"get_canvas", "set_canvas", "clear_canvas"}),
    # Catalogue-authoring writes. A checkbox of its own rather than membership in
    # `agent_catalog` / `workflows`, because those labels read as lookup
    # capabilities and a box must not grant more than it says.
    "catalog_authoring": frozenset(
        {
            "get_expert_bundle",
            "set_expert_bundle",
            "get_skill_bundle",
            "set_skill_bundle",
            "get_automation_bundle",
            "set_automation_bundle",
        }
    ),
}

#: The groups the LEGACY (experts-off) agent path re-adds when no ``[]`` disable
#: marker is present. Job names are descriptor-derived; the three older app
#: groups retain their narrow compatibility lists. This is why an unset group
#: reads as ENABLED there and DISABLED on the resolved path.
#:
#: This is a HISTORICAL FACT about deployed agent code, not a property of "how
#: many checkboxes the product offers", and the two must not be conflated: a
#: group added to ``SESSION_TOOL_OVERRIDE_NAMES`` today is not retroactively
#: appended by an agent image built before it existed. Deriving the append rule
#: from the presentation vocabulary would have made an unset ``catalog_authoring``
#: predict six write tools that no agent binds.
#:
#: ``canvas`` is absent because its legacy branch is strip-only with no append.
LEGACY_APPENDED_GROUPS: frozenset[str] = frozenset(
    {
        "orchestrator",
        "job_control",
        "job_inspection",
        "agent_catalog",
        "workflows",
    }
)


def session_tool_group_enablement(merged_config: Any) -> dict[str, bool]:
    """Return resolved-path enablement per closed session group.

    A group is DISABLED iff its merged list is exactly ``[]`` — the positive
    reading of ``orchestrator.main._session_tool_group_disabled_markers`` and
    the exact predicate the runtime gates apply
    (``persistent_session._fleet_management_enabled`` and its siblings).

    An ABSENT key reads as enabled.  That is only reachable on the legacy
    (experts-off) path: the resolved path always merges ``session_base``, which
    declares every one of these groups explicitly.

    Takes the fully merged config fragment — ``capture["merged_fragment"]`` from
    ``resolve_config`` — NOT a request-layer ``config_override``.  Feeding it the
    latter reproduces the very bug this exists to close, because an unset group
    would read enabled when the base ships it empty.
    """

    tools = merged_config.get("tools") if isinstance(merged_config, dict) else None
    if not isinstance(tools, dict):
        return {group: True for group in SESSION_TOOL_OVERRIDE_NAMES}
    return {group: tools.get(group) != [] for group in SESSION_TOOL_OVERRIDE_NAMES}


__all__ = [
    "LEGACY_APPENDED_GROUPS",
    "SESSION_TOOL_OVERRIDE_NAMES",
    "session_tool_group_enablement",
]
