"""Skill tools for the Universal Agent (Agent Skills, Slice 2).

``use_skill`` loads a skill's SKILL.md body (Level 2) from the workspace. Skill
directories are materialized at job start under skills/<name>/ by
_deploy_instruction_files. The L1 menu (name + description) is already in the
system prompt; this tool brings the body into context on demand. References
(skills/<name>/references/) are read with read_file; scripts are executed with
run_command on a shell-capable tier. On a lite (virtual) tier use_skill notes
that the scripts need a workspace upgrade first (Slice 4).

Design: docs/features/agent_skills.md (Slice 2).
"""

import logging
from typing import Any, Dict, List

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)

SKILL_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "use_skill": {
        "module": "workspace.skills",
        "function": "use_skill",
        "description": "Load a skill's SKILL.md guidance into context for the current task",
        "category": "workspace",
        "phases": ["strategic", "tactical"],
    },
}


def create_skill_tools(context: ToolContext) -> List[Any]:
    """Create skill tools with injected context."""
    if not context.has_workspace():
        raise ValueError("ToolContext must have a workspace_manager for skill tools")

    workspace = context.workspace_manager

    def _script_availability_note(skill_name: str) -> str:
        """When a skill bundles scripts but this tier has no shell, tell the
        agent the scripts can't run here and how to unlock them (Slice 4).

        A skill is script-bearing iff it has a ``scripts/`` directory — a single
        dir-aware ``exists`` probe. The note fires only on a shell-less tier
        (``virtual``); on a sandbox/vm the agent just runs the scripts via
        run_command, so no note. Detection must never break use_skill.
        """
        try:
            if getattr(workspace.backend, "supports_shell", True):
                return ""  # has a shell → agent runs scripts directly
            if not workspace.exists(f"skills/{skill_name}/scripts"):
                return ""  # prompt-only skill → nothing to gate
        except Exception:
            return ""
        return (
            "\n\n---\n"
            "[scripts need a workspace] This skill bundles runnable scripts under "
            f"`skills/{skill_name}/scripts/`, but you are on a virtual filesystem "
            "with no shell, so they cannot be executed here. The guidance above "
            "still applies without them. To run the scripts, call "
            "`request_workspace_upgrade(reason=...)`: a human approves, a sandbox "
            "with a shell is provisioned, your files carry over, and you can then "
            "run the script with `run_command` on a later turn."
        )

    @tool
    def use_skill(skill_name: str) -> str:
        """Load a skill's guidance (its SKILL.md body) into your context.

        Skills are reusable "how to do X well" procedures listed in your system
        prompt under available_skills. Call this when a listed skill matches the
        task at hand; the body will appear in your context and walk you through
        the procedure. If the skill bundles references/ files, read them with
        read_file as the body directs.

        Args:
            skill_name: The skill's name exactly as shown in the available_skills
                menu (e.g. "hello-skill").

        Returns:
            The SKILL.md body, or a friendly message if the skill is not present.
        """
        skill_md = f"skills/{skill_name}/SKILL.md"
        try:
            if not workspace.exists(skill_md):
                return (
                    f"Skill '{skill_name}' not found in this workspace. "
                    f"Use only skills listed in the available_skills menu, by their "
                    f"exact name."
                )
            body = workspace.read_file(skill_md)
            context.record_file_read(skill_md)
            return f"[skill: {skill_name}]\n\n{body}{_script_availability_note(skill_name)}"
        except Exception as e:  # never raise to the model
            logger.warning("use_skill(%s) failed: %s", skill_name, e)
            return f"Error loading skill '{skill_name}': {e}"

    return [use_skill]
