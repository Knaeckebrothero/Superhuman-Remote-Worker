"""System prompt for the instruction builder AI.

The builder is an instruction-writing assistant that helps users craft
agent instructions and configuration through a chat interface. It mutates
a shared "artifact" (instructions + config + description) via tool calls.

The base system prompt is loaded from external files via the builder prompt
matrix (orchestrator/config/builder_prompt_matrix.yaml), with model-family-
specific variants for gpt-oss, minimax, etc.
"""

import logging

from services.builder_config import resolve_builder_prompt

logger = logging.getLogger(__name__)


def build_system_prompt(
    model: str = "default",
    instructions_content: str | None = None,
    config_settings: dict | None = None,
    description: str | None = None,
    active_job_id: str | None = None,
    active_project_id: str | None = None,
) -> str:
    """Build the full system prompt with current artifact state.

    Loads the model-family-appropriate base prompt from the prompt matrix,
    then appends dynamic artifact state.

    Args:
        model: Raw model name for prompt matrix resolution
        instructions_content: Current instructions.md content
        config_settings: Current config override settings
        description: Current job description
        active_job_id: Active job context for inspection tools
        active_project_id: Active project context for project-scoped tools

    Returns:
        Complete system prompt with artifact state
    """
    try:
        base_prompt = resolve_builder_prompt(model, "system_prompt")
    except FileNotFoundError:
        logger.warning("Failed to load builder prompt for model=%s, using default", model)
        base_prompt = resolve_builder_prompt("default", "system_prompt")

    parts = [base_prompt]

    parts.append("\n\n---\n\n## Current Artifact State\n")

    # Active job context
    if active_job_id:
        parts.append("### Active Job Context\n")
        parts.append(f"The user has selected job `{active_job_id}` as the active context. ")
        parts.append("Use this job_id by default when they ask to inspect, review, or act on a job.\n\n")

    # Active project context
    if active_project_id:
        parts.append("### Active Project Context\n")
        parts.append(f"The user has selected project `{active_project_id}` as the active context. ")
        parts.append("Use this project_id by default for knowledge base tools, project management, ")
        parts.append("and project-scoped operations.\n\n")

    # Instructions
    parts.append("### Instructions\n")
    if instructions_content:
        parts.append(f"```markdown\n{instructions_content}\n```\n")
    else:
        parts.append("*(empty — no instructions written yet)*\n")

    # Config
    parts.append("\n### Configuration Override\n")
    if config_settings:
        import json
        parts.append(f"```json\n{json.dumps(config_settings, indent=2)}\n```\n")
    else:
        parts.append("*(using expert defaults — no overrides)*\n")

    # Description
    parts.append("\n### Job Description\n")
    if description:
        parts.append(f"> {description}\n")
    else:
        parts.append("*(no description set)*\n")

    return "".join(parts)
