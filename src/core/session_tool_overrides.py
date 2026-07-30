"""Closed validation for user-selectable persistent-session tool groups.

Tool names are globally registered and are grouped by their registry metadata,
not by the key they arrived under in a config fragment.  Consequently, a
request such as ``tools.canvas = ["run_command"]`` must never reach the generic
loader: the loader would correctly find ``run_command`` in the global registry
and instantiate a shell tool even though the request hid it under an unrelated
group.

Keep this module framework-free so both the orchestrator request boundary and
the persistent runtime's live ``config.update`` boundary use the exact same
closed category-to-name mapping.
"""

from __future__ import annotations

from typing import Any


SESSION_TOOL_OVERRIDE_NAMES: dict[str, frozenset[str]] = {
    "orchestrator": frozenset(
        {
            "get_session_context",
            "create_worker_job",
            "list_worker_jobs",
            "get_worker_job",
            "get_job_workspace_file",
            "list_job_workspace_files",
            "approve_worker_job",
            "resume_worker_job",
            "cancel_worker_job",
            "pause_worker_job",
            "get_current_project",
            "list_project_jobs",
            "list_project_repositories",
            "get_default_project_repository",
        }
    ),
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
}


class SessionToolOverrideError(ValueError):
    """A selectable session group contains an invalid or foreign tool name."""


def validate_session_tool_overrides(
    config_override: Any,
) -> dict[str, list[str]]:
    """Return the safe session-facing tool-group subset of an override.

    Other tool categories are intentionally ignored, matching the New Session
    API's existing contract: only the independently toggled session groups in
    :data:`SESSION_TOOL_OVERRIDE_NAMES` are accepted from this surface.  Every
    accepted group is type-checked and then checked against its own closed name
    set, so a globally valid tool cannot be smuggled through a different group.
    """

    tools = config_override.get("tools") if isinstance(config_override, dict) else None
    if not isinstance(tools, dict):
        return {}

    accepted: dict[str, list[str]] = {}
    for group, allowed_names in SESSION_TOOL_OVERRIDE_NAMES.items():
        if group not in tools:
            continue
        value = tools.get(group)
        if not isinstance(value, list) or not all(
            isinstance(name, str) for name in value
        ):
            raise SessionToolOverrideError(
                f"Invalid tools.{group} override; expected a string list."
            )
        unexpected = sorted(set(value) - allowed_names)
        if unexpected:
            raise SessionToolOverrideError(
                f"Invalid tools.{group} override; unknown or cross-category "
                f"tool name(s): {', '.join(unexpected)}."
            )
        accepted[group] = list(value)
    return accepted


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
    "SESSION_TOOL_OVERRIDE_NAMES",
    "SessionToolOverrideError",
    "session_tool_group_enablement",
    "validate_session_tool_overrides",
]
