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

from src.llm.reasoning_chat import ReasoningChatOpenAI


logger = logging.getLogger(__name__)


# =============================================================================
# Config Merging Utilities
# =============================================================================


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge two dictionaries.

    Merge semantics:
    - Objects (dicts): Recursively merge
    - Arrays (lists): Override replaces entirely
    - Scalars: Override replaces
    - None in override: Clears the key from result

    Args:
        base: Base dictionary (defaults)
        override: Override dictionary (deployment-specific)

    Returns:
        Merged dictionary

    Example:
        ```python
        base = {"llm": {"model": "gpt-4", "temp": 0.0}, "tools": ["a", "b"]}
        override = {"llm": {"model": "gpt-oss"}, "tools": ["c"]}
        result = deep_merge(base, override)
        # {"llm": {"model": "gpt-oss", "temp": 0.0}, "tools": ["c"]}
        ```
    """
    result = base.copy()

    for key, value in override.items():
        if value is None:
            # None explicitly clears the key
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            # Recursively merge dicts
            result[key] = deep_merge(result[key], value)
        else:
            # Arrays and scalars: override replaces
            result[key] = value

    return result


def get_project_root() -> Path:
    """Get the project root directory.

    Traverses up from this file to find the project root
    (directory containing .git or pyproject.toml).

    Returns:
        Path to project root
    """
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
            return parent
    # Fallback: assume src/agent/core/loader.py -> project root is 4 levels up
    return Path(__file__).parent.parent.parent.parent


def load_and_merge_config(config_path: str) -> Dict[str, Any]:
    """Load configuration with inheritance resolution.

    Handles $extends field to load and merge parent configs.
    Supports chained inheritance (A extends B extends C).

    Args:
        config_path: Path to the configuration JSON file

    Returns:
        Merged configuration dictionary

    Example:
        ```python
        # configs/creator/config.json: {"$extends": "defaults", "agent_id": "creator"}
        data = load_and_merge_config("configs/creator/config.json")
        # Returns merged defaults + creator overrides
        ```
    """
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)

    # Handle $extends inheritance
    if "$extends" in config_data:
        parent_name = config_data.pop("$extends")

        # Resolve parent config path
        parent_path, _ = resolve_config_path(parent_name)
        if not Path(parent_path).exists():
            raise FileNotFoundError(
                f"Parent config not found: {parent_name} (resolved to {parent_path})"
            )

        # Recursively load parent (supports chained inheritance)
        parent_data = load_and_merge_config(parent_path)

        # Merge: parent as base, current as override
        config_data = deep_merge(parent_data, config_data)

    # Remove $comment if present (documentation only)
    config_data.pop("$comment", None)

    return config_data


class PromptResolver:
    """Resolves prompt templates with deployment override support.

    Checks deployment directory first, then falls back to framework prompts.
    This allows deployments to override specific prompts while using
    framework defaults for others.

    Example:
        ```python
        resolver = PromptResolver(deployment_dir="/project/configs/creator")

        # Will check: /project/configs/creator/instructions.md
        # Falls back to: src/agent/config/prompts/instructions.md
        content = resolver.load("instructions.md")
        ```
    """

    def __init__(self, deployment_dir: Optional[str] = None):
        """Initialize prompt resolver.

        Args:
            deployment_dir: Path to deployment directory (e.g., configs/creator)
                          If None, only framework prompts are used.
        """
        self.deployment_dir = Path(deployment_dir) if deployment_dir else None
        self.framework_dir = Path(__file__).parent.parent / "config" / "prompts"

    def resolve(self, template_name: str) -> Path:
        """Find prompt template, checking deployment dir first.

        Args:
            template_name: Name of the template file (e.g., "instructions.md")

        Returns:
            Path to the template file

        Raises:
            FileNotFoundError: If template not found in either location
        """
        # Check deployment directory first
        if self.deployment_dir:
            deployment_path = self.deployment_dir / template_name
            if deployment_path.exists():
                return deployment_path

        # Fall back to framework directory
        framework_path = self.framework_dir / template_name
        if framework_path.exists():
            return framework_path

        raise FileNotFoundError(
            f"Prompt template not found: {template_name} "
            f"(checked: {self.deployment_dir}, {self.framework_dir})"
        )

    def load(self, template_name: str) -> str:
        """Load prompt template content.

        Args:
            template_name: Name of the template file

        Returns:
            Template content as string

        Raises:
            FileNotFoundError: If template not found
        """
        path = self.resolve(template_name)
        return path.read_text(encoding="utf-8")

    def exists(self, template_name: str) -> bool:
        """Check if a template exists.

        Args:
            template_name: Name of the template file

        Returns:
            True if template exists in either location
        """
        try:
            self.resolve(template_name)
            return True
        except FileNotFoundError:
            return False


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
    completion: List[str] = field(default_factory=list)


@dataclass
class TodoConfig:
    """Todo manager configuration."""

    max_items: int = 25
    archive_on_reset: bool = True
    archive_path: str = "archive/"


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
    summarization_template: str = "summarization_prompt.md"


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

    # Internal: deployment directory for prompt resolution
    # Set automatically by load_agent_config when loading from configs/
    _deployment_dir: Optional[str] = None


def load_agent_config(
    config_path: str,
    deployment_dir: Optional[str] = None
) -> AgentConfig:
    """Load agent configuration from a JSON file.

    Supports config inheritance via $extends field. When a config extends
    another, the parent is loaded first and the child's values are merged on top.

    Args:
        config_path: Path to the configuration JSON file
        deployment_dir: Optional deployment directory for prompt resolution.
                       Set automatically when loading from configs/{name}/.

    Returns:
        AgentConfig dataclass with loaded configuration

    Raises:
        FileNotFoundError: If config file doesn't exist
        json.JSONDecodeError: If config file has invalid JSON
        ValueError: If required fields are missing

    Example:
        ```python
        # Direct load (backward compat)
        config = load_agent_config("src/agent/config/creator.json")

        # With inheritance (new style)
        # configs/creator/config.json: {"$extends": "defaults", "agent_id": "creator"}
        config = load_agent_config("configs/creator/config.json", "configs/creator")
        ```
    """
    config_path_obj = Path(config_path)

    if not config_path_obj.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # Load config with inheritance resolution
    data = load_and_merge_config(config_path)

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
        completion=tools_data.get("completion", []),
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
        summarization_template=context_data.get(
            "summarization_template",
            "summarization_prompt.md"
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
        _deployment_dir=deployment_dir,
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

    llm = ReasoningChatOpenAI(**llm_kwargs)

    logger.info(
        f"Created LLM: model={config.model}, temp={config.temperature}, "
        f"base_url={base_url or 'default'}"
    )

    return llm


def load_system_prompt_template(config_dir: Optional[str] = None) -> str:
    """Load the raw system prompt template without substitution.

    Args:
        config_dir: Base directory for config files (default: src/agent/config)

    Returns:
        Raw template string with placeholders
    """
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "config"
    else:
        config_dir = Path(config_dir)

    system_prompt_path = config_dir / "prompts" / "01_system_prompt.txt"

    if not system_prompt_path.exists():
        raise FileNotFoundError(f"System prompt not found: {system_prompt_path}")

    with open(system_prompt_path, "r", encoding="utf-8") as f:
        return f.read()


def render_system_prompt(
    template: str,
    config: AgentConfig,
    todos_content: str = "",
    workspace_content: str = "",
) -> str:
    """Render system prompt template with current values.

    Args:
        template: Raw template string with placeholders
        config: Agent configuration
        todos_content: Formatted todo list string
        workspace_content: Contents of workspace.md

    Returns:
        Rendered system prompt string
    """
    oss_reasoning_level = config.llm.reasoning_level or "high"

    return template.format(
        agent_display_name=config.display_name,
        oss_reasoning_level=oss_reasoning_level,
        todos_content=todos_content,
        workspace_content=workspace_content,
    )


def load_system_prompt(
    config: AgentConfig,
    config_dir: Optional[str] = None,
    workspace_manager: Optional[Any] = None,
    todos_content: str = "",
) -> str:
    """Load and format the system prompt for the agent.

    This is a convenience function that combines load_system_prompt_template()
    and render_system_prompt() for backwards compatibility.

    Args:
        config: Agent configuration
        config_dir: Base directory for config files (default: src/agent/config)
        workspace_manager: Optional workspace manager for reading workspace.md
        todos_content: Formatted todo list string

    Returns:
        Formatted system prompt string
    """
    template = load_system_prompt_template(config_dir)

    # Read workspace.md content if available
    workspace_content = ""
    if workspace_manager:
        try:
            if workspace_manager.exists("workspace.md"):
                workspace_content = workspace_manager.read_file("workspace.md")
        except Exception as e:
            logger.debug(f"Could not read workspace.md: {e}")

    return render_system_prompt(
        template=template,
        config=config,
        todos_content=todos_content,
        workspace_content=workspace_content,
    )


def load_instructions(
    config: AgentConfig,
    config_dir: Optional[str] = None,
    prompt_resolver: Optional[PromptResolver] = None,
) -> str:
    """Load the instructions template for the agent.

    Uses PromptResolver to check deployment directory first if available,
    allowing deployments to override framework prompts.

    Args:
        config: Agent configuration
        config_dir: Base directory for config files (deprecated, use prompt_resolver)
        prompt_resolver: Optional resolver for finding prompts (preferred)

    Returns:
        Instructions content to be placed in workspace
    """
    template_name = config.workspace.instructions_template

    # Try PromptResolver first (new style)
    if prompt_resolver is None and config._deployment_dir:
        prompt_resolver = PromptResolver(config._deployment_dir)

    if prompt_resolver:
        try:
            return prompt_resolver.load(template_name)
        except FileNotFoundError:
            pass  # Fall through to legacy handling

    # Legacy path resolution (backward compatibility)
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "config"
    else:
        config_dir = Path(config_dir)

    instructions_path = config_dir / "prompts" / template_name

    if instructions_path.exists():
        with open(instructions_path, "r", encoding="utf-8") as f:
            return f.read()
    else:
        logger.warning(
            f"Instructions template not found: {template_name}. "
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


def load_summarization_prompt(
    config: Optional["AgentConfig"] = None,
    config_dir: Optional[str] = None,
    prompt_resolver: Optional[PromptResolver] = None,
) -> str:
    """Load the summarization prompt template.

    Uses PromptResolver to check deployment directory first if available.

    Args:
        config: Agent configuration (to get agent-specific template)
        config_dir: Base directory for config files (deprecated, use prompt_resolver)
        prompt_resolver: Optional resolver for finding prompts (preferred)

    Returns:
        Summarization prompt content
    """
    # Determine template name
    template_name = "summarization_prompt.md"
    if config and config.context_management.summarization_template:
        template_name = config.context_management.summarization_template

    # Try PromptResolver first (new style)
    if prompt_resolver is None and config and config._deployment_dir:
        prompt_resolver = PromptResolver(config._deployment_dir)

    if prompt_resolver:
        try:
            return prompt_resolver.load(template_name)
        except FileNotFoundError:
            pass  # Fall through to legacy handling

    # Legacy path resolution (backward compatibility)
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "config"
    else:
        config_dir = Path(config_dir)

    prompt_path = config_dir / "prompts" / template_name

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
"""


def load_planning_prompt(config_dir: Optional[str] = None) -> str:
    """Load the planning prompt template.

    Args:
        config_dir: Base directory for config files

    Returns:
        Planning prompt content
    """
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "config"
    else:
        config_dir = Path(config_dir)

    prompt_path = config_dir / "prompts" / "planning_prompt.md"

    if not prompt_path.exists():
        raise FileNotFoundError(f"Planning prompt not found: {prompt_path}")
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()


def load_todo_extraction_prompt(config_dir: Optional[str] = None) -> str:
    """Load the todo extraction prompt template.

    Template variables: {current_phase}, {plan_content}

    Args:
        config_dir: Base directory for config files

    Returns:
        Todo extraction prompt content
    """
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "config"
    else:
        config_dir = Path(config_dir)

    prompt_path = config_dir / "prompts" / "todo_extraction_prompt.md"

    if not prompt_path.exists():
        raise FileNotFoundError(f"Todo extraction prompt not found: {prompt_path}")
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()


def load_memory_update_prompt(config_dir: Optional[str] = None) -> str:
    """Load the memory update prompt template.

    Template variable: {current_memory}

    Args:
        config_dir: Base directory for config files

    Returns:
        Memory update prompt content
    """
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "config"
    else:
        config_dir = Path(config_dir)

    prompt_path = config_dir / "prompts" / "memory_update_prompt.md"

    if not prompt_path.exists():
        raise FileNotFoundError(f"Memory update prompt not found: {prompt_path}")
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()


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
        config.tools.domain +
        config.tools.completion
    )


def resolve_config_path(config_name: str) -> tuple[str, Optional[str]]:
    """
    Resolve a config name to a full path and deployment directory.

    Resolution order:
    1. Absolute path or .json suffix -> use as-is
    2. configs/{name}/config.json (deployment configs at project root)
    3. src/agent/config/{name}.json (framework configs, backward compat)

    Args:
        config_name: Config name (e.g., "creator", "validator")
                    or full path to config file

    Returns:
        Tuple of (config_path, deployment_dir_or_none)
        - config_path: Full path to the config file
        - deployment_dir: Directory containing deployment files (for prompt resolution)
                         None if using framework config or direct path
    """
    # If it's already a full path or explicit .json file
    if os.path.isabs(config_name) or config_name.endswith(".json"):
        return (config_name, None)

    # Try deployment config first (configs/{name}/config.json)
    project_root = get_project_root()
    deployment_dir = project_root / "configs" / config_name
    deployment_config = deployment_dir / "config.json"

    if deployment_config.exists():
        return (str(deployment_config), str(deployment_dir))

    # Fall back to framework config (src/agent/config/{name}.json)
    # This maintains backward compatibility
    framework_dir = Path(__file__).parent.parent / "config"
    framework_config = framework_dir / f"{config_name}.json"

    if framework_config.exists():
        return (str(framework_config), None)

    # Return framework path even if it doesn't exist (let caller handle error)
    return (str(framework_config), None)
