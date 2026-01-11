"""Configuration and tool loader for Universal Agent.

Handles loading agent configuration from JSON files and dynamically
loading the appropriate tools based on configuration.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    """LLM configuration."""

    model: str = "gpt-4o"
    temperature: float = 0.0
    reasoning_level: str = "high"
    base_url: Optional[str] = None
    api_key: Optional[str] = None


@dataclass
class WorkspaceConfig:
    """Workspace configuration."""

    structure: List[str] = field(default_factory=list)
    instructions_template: str = ""
    initial_files: Dict[str, str] = field(default_factory=dict)


@dataclass
class ToolsConfig:
    """Tools configuration by category."""

    workspace: List[str] = field(default_factory=list)
    todo: List[str] = field(default_factory=list)
    domain: List[str] = field(default_factory=list)


@dataclass
class TodoConfig:
    """Todo manager configuration."""

    max_items: int = 25
    archive_on_reset: bool = True
    archive_path: str = "archive/"
    auto_reflection: bool = True
    reflection_task_content: str = "Review plan, update progress, create next todos"


@dataclass
class ConnectionsConfig:
    """Database connections configuration."""

    postgres: bool = True
    neo4j: bool = False


@dataclass
class PollingConfig:
    """Job polling configuration."""

    enabled: bool = True
    table: str = "jobs"
    status_field: str = "status"
    status_value_pending: str = "pending"
    status_value_processing: str = "processing"
    status_value_complete: str = "complete"
    status_value_failed: str = "failed"
    interval_seconds: int = 30
    use_skip_locked: bool = False


@dataclass
class LimitsConfig:
    """Execution limits configuration."""

    max_iterations: int = 500
    context_threshold_tokens: int = 80000
    tool_retry_count: int = 3


@dataclass
class ContextManagementConfig:
    """Context management configuration."""

    compact_on_archive: bool = True
    keep_recent_tool_results: int = 5
    summarization_prompt: str = "Summarize the work completed so far."
    # Protected context settings (maintains "roter Faden" across compaction)
    protected_context_enabled: bool = True
    protected_context_plan_file: str = "main_plan.md"
    protected_context_max_chars: int = 2000
    protected_context_include_todos: bool = True


@dataclass
class AgentConfig:
    """Complete agent configuration.

    Loaded from JSON configuration file (e.g., creator.json, validator.json).
    """

    agent_id: str
    display_name: str
    description: str = ""
    llm: LLMConfig = field(default_factory=LLMConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    todo: TodoConfig = field(default_factory=TodoConfig)
    connections: ConnectionsConfig = field(default_factory=ConnectionsConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    context_management: ContextManagementConfig = field(
        default_factory=ContextManagementConfig
    )

    # Additional agent-specific config (preserved from JSON)
    extra: Dict[str, Any] = field(default_factory=dict)


def load_agent_config(config_path: str) -> AgentConfig:
    """Load agent configuration from a JSON file.

    Args:
        config_path: Path to the configuration JSON file

    Returns:
        AgentConfig dataclass with loaded configuration

    Raises:
        FileNotFoundError: If config file doesn't exist
        json.JSONDecodeError: If config file has invalid JSON
        ValueError: If required fields are missing

    Example:
        ```python
        config = load_agent_config("config/agents/creator.json")
        print(config.display_name)  # "Creator Agent"
        ```
    """
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Validate required fields
    required = ["agent_id", "display_name"]
    missing = [field for field in required if field not in data]
    if missing:
        raise ValueError(f"Missing required config fields: {missing}")

    # Parse nested configs
    llm_data = data.get("llm", {})
    llm_config = LLMConfig(
        model=llm_data.get("model", "gpt-4o"),
        temperature=llm_data.get("temperature", 0.0),
        reasoning_level=llm_data.get("reasoning_level", "high"),
        base_url=llm_data.get("base_url"),
        api_key=llm_data.get("api_key"),
    )

    workspace_data = data.get("workspace", {})
    workspace_config = WorkspaceConfig(
        structure=workspace_data.get("structure", []),
        instructions_template=workspace_data.get("instructions_template", ""),
        initial_files=workspace_data.get("initial_files", {}),
    )

    tools_data = data.get("tools", {})
    tools_config = ToolsConfig(
        workspace=tools_data.get("workspace", []),
        todo=tools_data.get("todo", []),
        domain=tools_data.get("domain", []),
    )

    todo_data = data.get("todo", {})
    todo_config = TodoConfig(
        max_items=todo_data.get("max_items", 25),
        archive_on_reset=todo_data.get("archive_on_reset", True),
        archive_path=todo_data.get("archive_path", "archive/"),
    )

    connections_data = data.get("connections", {})
    connections_config = ConnectionsConfig(
        postgres=connections_data.get("postgres", True),
        neo4j=connections_data.get("neo4j", False),
    )

    polling_data = data.get("polling", {})
    polling_config = PollingConfig(
        enabled=polling_data.get("enabled", True),
        table=polling_data.get("table", "jobs"),
        status_field=polling_data.get("status_field", "status"),
        status_value_pending=polling_data.get("status_value_pending", "pending"),
        status_value_processing=polling_data.get("status_value_processing", "processing"),
        status_value_complete=polling_data.get("status_value_complete", "complete"),
        status_value_failed=polling_data.get("status_value_failed", "failed"),
        interval_seconds=polling_data.get("interval_seconds", 30),
        use_skip_locked=polling_data.get("use_skip_locked", False),
    )

    limits_data = data.get("limits", {})
    limits_config = LimitsConfig(
        max_iterations=limits_data.get("max_iterations", 500),
        context_threshold_tokens=limits_data.get("context_threshold_tokens", 80000),
        tool_retry_count=limits_data.get("tool_retry_count", 3),
    )

    context_data = data.get("context_management", {})
    context_config = ContextManagementConfig(
        compact_on_archive=context_data.get("compact_on_archive", True),
        keep_recent_tool_results=context_data.get("keep_recent_tool_results", 5),
        summarization_prompt=context_data.get(
            "summarization_prompt",
            "Summarize the work completed so far."
        ),
    )

    # Collect extra fields (agent-specific config)
    known_fields = {
        "$schema", "agent_id", "display_name", "description", "llm", "workspace",
        "tools", "todo", "connections", "polling", "limits", "context_management"
    }
    extra = {k: v for k, v in data.items() if k not in known_fields}

    return AgentConfig(
        agent_id=data["agent_id"],
        display_name=data["display_name"],
        description=data.get("description", ""),
        llm=llm_config,
        workspace=workspace_config,
        tools=tools_config,
        todo=todo_config,
        connections=connections_config,
        polling=polling_config,
        limits=limits_config,
        context_management=context_config,
        extra=extra,
    )


def create_llm(config: LLMConfig) -> BaseChatModel:
    """Create an LLM instance from configuration.

    Args:
        config: LLM configuration

    Returns:
        Configured ChatOpenAI instance

    Note:
        Uses OpenAI-compatible API. The base_url can point to
        any OpenAI-compatible endpoint (local LLM server, etc.)
    """
    # Get API key from config or environment
    api_key = config.api_key or os.getenv("OPENAI_API_KEY", "not-needed")

    # Get base URL from config or environment
    base_url = config.base_url or os.getenv("LLM_BASE_URL")

    # Build model kwargs
    model_kwargs = {}

    # Add reasoning level if model supports it
    if config.reasoning_level and config.reasoning_level != "none":
        # Some models support reasoning_effort parameter
        model_kwargs["reasoning_effort"] = config.reasoning_level

    # Build kwargs for ChatOpenAI
    llm_kwargs = {
        "model": config.model,
        "temperature": config.temperature,
        "api_key": api_key,
    }

    # Only add base_url if specified
    if base_url:
        llm_kwargs["base_url"] = base_url

    # Only add model_kwargs if non-empty
    if model_kwargs:
        llm_kwargs["model_kwargs"] = model_kwargs

    llm = ChatOpenAI(**llm_kwargs)

    logger.info(
        f"Created LLM: model={config.model}, temp={config.temperature}, "
        f"base_url={base_url or 'default'}"
    )

    return llm


def load_system_prompt(
    config: AgentConfig,
    job_id: str,
    config_dir: Optional[str] = None,
    workspace_manager: Optional[Any] = None,
) -> str:
    """Load and format the system prompt for the agent.

    Args:
        config: Agent configuration
        job_id: Current job ID for placeholder substitution
        config_dir: Base directory for config files (default: config/agents)
        workspace_manager: Optional workspace manager for reading workspace.md

    Returns:
        Formatted system prompt string
    """
    if config_dir is None:
        # __file__ = src/agents/loader.py -> project root is 3 levels up
        config_dir = Path(__file__).parent.parent.parent / "src" / "config" / "agents"
    else:
        config_dir = Path(config_dir)

    system_prompt_path = config_dir / "instructions" / "system_prompt.md"

    if system_prompt_path.exists():
        with open(system_prompt_path, "r", encoding="utf-8") as f:
            template = f.read()
    else:
        # Fallback minimal prompt
        template = """You are the {agent_display_name}.

Your workspace is at /job_{job_id}/ with tools to read and write files.
Start by reading `instructions.md` to understand your task.

{workspace_content}

Write findings to files as you go to manage context.
Use todos to track your immediate steps.
When done, write completion status to `output/completion.json`.
"""

    # Read workspace.md content if available
    workspace_content = ""
    if workspace_manager:
        try:
            if workspace_manager.exists("workspace.md"):
                workspace_content = workspace_manager.read_file("workspace.md")
        except Exception as e:
            logger.debug(f"Could not read workspace.md: {e}")

    # Substitute placeholders
    prompt = template.format(
        agent_display_name=config.display_name,
        job_id=job_id,
        workspace_content=workspace_content,
    )

    return prompt


def load_instructions(
    config: AgentConfig,
    config_dir: Optional[str] = None
) -> str:
    """Load the instructions template for the agent.

    Args:
        config: Agent configuration
        config_dir: Base directory for config files

    Returns:
        Instructions content to be placed in workspace
    """
    if config_dir is None:
        # __file__ = src/agents/loader.py -> project root is 3 levels up
        config_dir = Path(__file__).parent.parent.parent / "src" / "config" / "agents"
    else:
        config_dir = Path(config_dir)

    instructions_path = config_dir / "instructions" / config.workspace.instructions_template

    if instructions_path.exists():
        with open(instructions_path, "r", encoding="utf-8") as f:
            return f.read()
    else:
        logger.warning(
            f"Instructions template not found: {instructions_path}. "
            "Using minimal instructions."
        )
        return f"""# {config.display_name} Instructions

You are running as {config.display_name}.

## Available Tools

Workspace tools: {', '.join(config.tools.workspace)}
Todo tools: {', '.join(config.tools.todo)}
Domain tools: {', '.join(config.tools.domain)}

## How to Work

1. Create a plan in `main_plan.md`
2. Use todos to track immediate steps
3. Write results to files as you go
4. When complete, write status to `output/completion.json`
"""


def load_summarization_prompt(config_dir: Optional[str] = None) -> str:
    """Load the summarization prompt template.

    Args:
        config_dir: Base directory for config files

    Returns:
        Summarization prompt content
    """
    if config_dir is None:
        # __file__ = src/agents/loader.py -> project root is 3 levels up
        config_dir = Path(__file__).parent.parent.parent / "src" / "config" / "agents"
    else:
        config_dir = Path(config_dir)

    prompt_path = config_dir / "instructions" / "summarization_prompt.md"

    if prompt_path.exists():
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read()
    else:
        logger.warning(
            f"Summarization prompt not found: {prompt_path}. "
            "Using default prompt."
        )
        return """Summarize this agent conversation concisely.
Focus on:
1. What tasks were completed
2. Key decisions made
3. Important information discovered
4. Current progress and next steps
5. Any errors or blockers encountered

Keep the summary under 500 words. Use bullet points.

Conversation:
{conversation}

Summary:"""


def get_all_tool_names(config: AgentConfig) -> List[str]:
    """Get all tool names from configuration.

    Args:
        config: Agent configuration

    Returns:
        List of all configured tool names
    """
    return (
        config.tools.workspace +
        config.tools.todo +
        config.tools.domain
    )


def resolve_config_path(config_name: str) -> str:
    """Resolve a config name to a full path.

    Args:
        config_name: Config name (e.g., "creator", "validator")
                    or full path to config file

    Returns:
        Full path to config file

    Example:
        ```python
        path = resolve_config_path("creator")
        # Returns: "/path/to/project/config/agents/creator.json"
        ```
    """
    # If it's already a full path
    if os.path.isabs(config_name) or config_name.endswith(".json"):
        return config_name

    # Resolve relative to config/agents directory
    # __file__ = src/agents/loader.py
    # .parent = src/agents, .parent.parent = src, .parent.parent.parent = project root
    project_root = Path(__file__).parent.parent.parent
    config_path = project_root / "src" / "config" / "agents" / f"{config_name}.json"

    return str(config_path)
