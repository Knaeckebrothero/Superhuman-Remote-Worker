"""Configuration and tool loader for Universal Agent.

Handles loading agent configuration from YAML files and dynamically
loading the appropriate tools based on configuration.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
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
    Supports both YAML and JSON config files.

    Args:
        config_path: Path to the configuration file (YAML or JSON)

    Returns:
        Merged configuration dictionary

    Example:
        ```python
        # config/my_agent.yaml with $extends: defaults
        data = load_and_merge_config("config/my_agent.yaml")
        # Returns merged defaults + agent overrides
        ```
    """
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f)

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
        resolver = PromptResolver(deployment_dir="/project/config/my_agent")

        # Will check: /project/config/my_agent/instructions.md
        # Falls back to: config/prompts/instructions.md
        content = resolver.load("instructions.md")
        ```
    """

    def __init__(self, deployment_dir: Optional[str] = None):
        """Initialize prompt resolver.

        Args:
            deployment_dir: Path to deployment directory (e.g., config/my_agent)
                          If None, only framework prompts are used.
        """
        self.deployment_dir = Path(deployment_dir) if deployment_dir else None
        self.framework_dir = get_project_root() / "config" / "prompts"

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
    timeout: Optional[float] = 600.0  # 10 minutes default
    max_retries: int = 3


@dataclass
class WorkspaceConfig:
    """Workspace configuration."""

    structure: List[str] = field(default_factory=list)
    instructions_template: str = ""
    initial_files: Dict[str, str] = field(default_factory=dict)
    max_read_words: int = 25000  # Maximum word count for file reads


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
class LimitsConfig:
    """Execution limits configuration."""

    context_threshold_tokens: int = 80000
    message_count_threshold: int = 200
    message_count_min_tokens: int = 30000
    tool_retry_count: int = 3
    # Safety layer constants for preventing context overflow
    model_max_context_tokens: int = 128000  # Hard limit for model context window
    summarization_safe_limit: int = 100000  # Max input tokens for summarization LLM
    summarization_chunk_size: int = 80000   # Chunk size for recursive summarization


@dataclass
class ContextManagementConfig:
    """Context management configuration."""

    compact_on_archive: bool = True
    keep_recent_tool_results: int = 10
    keep_recent_messages: int = 10
    summarization_template: str = "summarization_prompt.txt"
    reasoning_level: str = "high"
    max_summary_length: int = 10000


@dataclass
class PhaseSettings:
    """Phase alternation settings.

    Controls the strategic/tactical phase transitions.
    """

    min_todos: int = 5  # Minimum todos required for strategic->tactical transition
    max_todos: int = 20  # Maximum todos allowed for strategic->tactical transition
    archive_on_transition: bool = True  # Archive todos on tactical->strategic transition


@dataclass
class AgentConfig:
    """Complete agent configuration.

    Loaded from YAML configuration file (e.g., defaults.yaml, my_agent.yaml).
    """

    agent_id: str
    display_name: str
    description: str = ""
    llm: LLMConfig = field(default_factory=LLMConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    todo: TodoConfig = field(default_factory=TodoConfig)
    connections: ConnectionsConfig = field(default_factory=ConnectionsConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    context_management: ContextManagementConfig = field(
        default_factory=ContextManagementConfig
    )
    phase_settings: PhaseSettings = field(default_factory=PhaseSettings)

    # Additional agent-specific config (preserved from JSON)
    extra: Dict[str, Any] = field(default_factory=dict)

    # Internal: deployment directory for prompt resolution
    # Set automatically by load_agent_config when loading from config/
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
                       Set automatically when loading from config/{name}/.

    Returns:
        AgentConfig dataclass with loaded configuration

    Raises:
        FileNotFoundError: If config file doesn't exist
        json.JSONDecodeError: If config file has invalid JSON
        ValueError: If required fields are missing

    Example:
        ```python
        # Single file config
        config = load_agent_config("config/my_agent.yaml")

        # Directory config with prompt overrides
        # config/my_agent/config.yaml with $extends: defaults
        config = load_agent_config("config/my_agent/config.yaml", "config/my_agent")
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
        timeout=llm_data.get("timeout", 600.0),
        max_retries=llm_data.get("max_retries", 3),
    )

    workspace_data = data.get("workspace", {})

    # Handle backward compatibility: if max_read_words not set but max_read_size is,
    # convert bytes to words using average of 5.5 bytes per word
    max_read_words = workspace_data.get("max_read_words")
    max_read_size_legacy = workspace_data.get("max_read_size")

    if max_read_words is None and max_read_size_legacy is not None:
        # Convert legacy bytes to words
        max_read_words = int(max_read_size_legacy / 5.5)
        logger.debug(
            f"Converting legacy max_read_size ({max_read_size_legacy} bytes) "
            f"to max_read_words ({max_read_words} words)"
        )
    elif max_read_words is None:
        max_read_words = 25000  # Default

    workspace_config = WorkspaceConfig(
        structure=workspace_data.get("structure", []),
        instructions_template=workspace_data.get("instructions_template", ""),
        initial_files=workspace_data.get("initial_files", {}),
        max_read_words=max_read_words,
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

    limits_data = data.get("limits", {})
    limits_config = LimitsConfig(
        context_threshold_tokens=limits_data.get("context_threshold_tokens", 80000),
        message_count_threshold=limits_data.get("message_count_threshold", 200),
        message_count_min_tokens=limits_data.get("message_count_min_tokens", 30000),
        tool_retry_count=limits_data.get("tool_retry_count", 3),
        model_max_context_tokens=limits_data.get("model_max_context_tokens", 128000),
        summarization_safe_limit=limits_data.get("summarization_safe_limit", 100000),
        summarization_chunk_size=limits_data.get("summarization_chunk_size", 80000),
    )

    context_data = data.get("context_management", {})
    context_config = ContextManagementConfig(
        compact_on_archive=context_data.get("compact_on_archive", True),
        keep_recent_tool_results=context_data.get("keep_recent_tool_results", 10),
        keep_recent_messages=context_data.get("keep_recent_messages", 10),
        summarization_template=context_data.get(
            "summarization_template",
            "summarization_prompt.txt"
        ),
        reasoning_level=context_data.get("reasoning_level", "high"),
        max_summary_length=context_data.get("max_summary_length", 10000),
    )

    phase_data = data.get("phase_settings", {})
    phase_config = PhaseSettings(
        min_todos=phase_data.get("min_todos", 5),
        max_todos=phase_data.get("max_todos", 20),
        archive_on_transition=phase_data.get("archive_on_transition", True),
    )

    # Collect extra fields (agent-specific config)
    known_fields = {
        "$schema", "agent_id", "display_name", "description", "llm", "workspace",
        "tools", "todo", "connections", "polling", "limits", "context_management",
        "phase_settings"
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
        limits=limits_config,
        context_management=context_config,
        phase_settings=phase_config,
        extra=extra,
        _deployment_dir=deployment_dir,
    )


def load_agent_config_from_dict(
    data: Dict[str, Any],
    deployment_dir: Optional[str] = None
) -> AgentConfig:
    """Create an AgentConfig from a pre-merged configuration dictionary.

    This is useful when you've already merged config data (e.g., from an uploaded
    config merged with defaults) and want to create an AgentConfig.

    Args:
        data: Merged configuration dictionary
        deployment_dir: Optional deployment directory for prompt resolution

    Returns:
        AgentConfig dataclass

    Raises:
        ValueError: If required fields are missing
    """
    # Validate required fields
    required = ["agent_id", "display_name"]
    missing = [field for field in required if field not in data]
    if missing:
        raise ValueError(f"Missing required config fields: {missing}")

    # Parse nested configs (same as load_agent_config)
    llm_data = data.get("llm", {})
    llm_config = LLMConfig(
        model=llm_data.get("model", "gpt-4o"),
        temperature=llm_data.get("temperature", 0.0),
        reasoning_level=llm_data.get("reasoning_level", "high"),
        base_url=llm_data.get("base_url"),
        api_key=llm_data.get("api_key"),
        timeout=llm_data.get("timeout", 600.0),
        max_retries=llm_data.get("max_retries", 3),
    )

    workspace_data = data.get("workspace", {})
    max_read_words = workspace_data.get("max_read_words")
    max_read_size_legacy = workspace_data.get("max_read_size")
    if max_read_words is None and max_read_size_legacy is not None:
        max_read_words = int(max_read_size_legacy / 5.5)
    elif max_read_words is None:
        max_read_words = 25000

    workspace_config = WorkspaceConfig(
        structure=workspace_data.get("structure", []),
        instructions_template=workspace_data.get("instructions_template", ""),
        initial_files=workspace_data.get("initial_files", {}),
        max_read_words=max_read_words,
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

    limits_data = data.get("limits", {})
    limits_config = LimitsConfig(
        context_threshold_tokens=limits_data.get("context_threshold_tokens", 80000),
        message_count_threshold=limits_data.get("message_count_threshold", 200),
        message_count_min_tokens=limits_data.get("message_count_min_tokens", 30000),
        tool_retry_count=limits_data.get("tool_retry_count", 3),
        model_max_context_tokens=limits_data.get("model_max_context_tokens", 128000),
        summarization_safe_limit=limits_data.get("summarization_safe_limit", 100000),
        summarization_chunk_size=limits_data.get("summarization_chunk_size", 80000),
    )

    context_data = data.get("context_management", {})
    context_config = ContextManagementConfig(
        compact_on_archive=context_data.get("compact_on_archive", True),
        keep_recent_tool_results=context_data.get("keep_recent_tool_results", 10),
        keep_recent_messages=context_data.get("keep_recent_messages", 10),
        summarization_template=context_data.get(
            "summarization_template",
            "summarization_prompt.txt"
        ),
        reasoning_level=context_data.get("reasoning_level", "high"),
        max_summary_length=context_data.get("max_summary_length", 10000),
    )

    phase_data = data.get("phase_settings", {})
    phase_config = PhaseSettings(
        min_todos=phase_data.get("min_todos", 5),
        max_todos=phase_data.get("max_todos", 20),
        archive_on_transition=phase_data.get("archive_on_transition", True),
    )

    # Collect extra fields
    known_fields = {
        "$schema", "agent_id", "display_name", "description", "llm", "workspace",
        "tools", "todo", "connections", "polling", "limits", "context_management",
        "phase_settings"
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
        limits=limits_config,
        context_management=context_config,
        phase_settings=phase_config,
        extra=extra,
        _deployment_dir=deployment_dir,
    )


def load_uploaded_config(uploaded_config_path: Path) -> Dict[str, Any]:
    """Load an uploaded config file and merge with defaults.

    The uploaded config is treated as an override on top of defaults.yaml.
    Uses the same deep_merge semantics as $extends inheritance.

    This enables per-job config customization without modifying the defaults.

    Args:
        uploaded_config_path: Path to the uploaded YAML config file

    Returns:
        Merged configuration dictionary (defaults + uploaded overrides)

    Example:
        ```python
        # User uploads a YAML file with:
        # llm:
        #   temperature: 0.7
        #
        # Result is defaults.yaml with temperature overridden to 0.7

        merged = load_uploaded_config(Path("/workspace/uploads/config_123/agent.yaml"))
        config = load_agent_config_from_dict(merged)
        ```
    """
    # Load defaults first
    defaults_path, _ = resolve_config_path("defaults")
    defaults_data = load_and_merge_config(defaults_path)

    # Load uploaded config
    with open(uploaded_config_path, "r", encoding="utf-8") as f:
        uploaded_data = yaml.safe_load(f) or {}

    # Remove $extends if present - we always extend defaults for uploaded configs
    uploaded_data.pop("$extends", None)
    uploaded_data.pop("$comment", None)

    # Merge: defaults as base, uploaded as override
    merged = deep_merge(defaults_data, uploaded_data)

    logger.info(
        f"Merged uploaded config with defaults: "
        f"agent_id={merged.get('agent_id')}, "
        f"overrides={list(uploaded_data.keys())}"
    )

    return merged


def create_llm(
    config: LLMConfig,
    limits: Optional[LimitsConfig] = None,
) -> BaseChatModel:
    """Create an LLM instance from configuration.

    Args:
        config: LLM configuration
        limits: Optional limits configuration for context token limit.
                When provided, enables HTTP-layer context overflow protection.

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
        "max_retries": config.max_retries,
    }

    # Add timeout if specified
    if config.timeout is not None:
        llm_kwargs["timeout"] = config.timeout

    # Only add base_url if specified
    if base_url:
        llm_kwargs["base_url"] = base_url

    # Only add model_kwargs if non-empty
    if model_kwargs:
        llm_kwargs["model_kwargs"] = model_kwargs

    # Add max_context_tokens for HTTP-layer validation (Layer 0 safety)
    max_context_tokens = limits.model_max_context_tokens if limits else None
    if max_context_tokens:
        llm_kwargs["max_context_tokens"] = max_context_tokens

    llm = ReasoningChatOpenAI(**llm_kwargs)

    logger.info(
        f"Created LLM: model={config.model}, temp={config.temperature}, "
        f"base_url={base_url or 'default'}, timeout={config.timeout}s, "
        f"max_retries={config.max_retries}, max_context_tokens={max_context_tokens or 'default'}"
    )

    return llm


# =============================================================================
# Phase-Aware System Prompts
# =============================================================================


def load_base_system_prompt(
    deployment_dir: Optional[str] = None,
) -> str:
    """Load the base system prompt template (systemprompt.txt).

    Uses PromptResolver to check deployment directory first if available,
    allowing deployments to override the base template.

    Args:
        deployment_dir: Path to deployment directory (e.g., config/my_agent).
                       If None, only framework templates are used.

    Returns:
        Raw template string with placeholders ({prompt_content}, {todos_content}, etc.)

    Raises:
        FileNotFoundError: If template not found in either location
    """
    resolver = PromptResolver(deployment_dir)
    return resolver.load("systemprompt.txt")


def load_phase_component(
    is_strategic: bool,
    deployment_dir: Optional[str] = None,
) -> str:
    """Load the phase-specific component (strategic.txt or tactical.txt).

    Uses PromptResolver to check deployment directory first if available,
    allowing deployments to override phase components.

    Args:
        is_strategic: True for strategic phase, False for tactical
        deployment_dir: Path to deployment directory (e.g., config/my_agent).
                       If None, only framework templates are used.

    Returns:
        Raw template string with {phase_number} placeholder

    Raises:
        FileNotFoundError: If template not found in either location
    """
    resolver = PromptResolver(deployment_dir)
    template_name = "strategic.txt" if is_strategic else "tactical.txt"
    return resolver.load(template_name)


def get_phase_system_prompt(
    config: AgentConfig,
    is_strategic: bool,
    phase_number: int = 0,
    todos_content: str = "",
    config_dir: Optional[str] = None,
) -> str:
    """Get the complete system prompt for the current phase.

    This is the main entry point for phase-aware prompts. It uses a
    component-based system:
    1. Load base template (systemprompt.txt)
    2. Load phase component (strategic.txt or tactical.txt)
    3. Render phase component's {phase_number} placeholder
    4. Inject rendered component into base template's {prompt_content}
    5. Render remaining placeholders ({todos_content}, etc.)

    Note: workspace.md content is now injected as a synthetic tool call result
    in graph.py, not included in the system prompt.

    Args:
        config: Agent configuration
        is_strategic: True for strategic phase, False for tactical
        phase_number: Current phase number
        todos_content: Formatted todo list string
        config_dir: Base directory for config files (deprecated, uses deployment_dir)

    Returns:
        Fully rendered system prompt string

    Example:
        ```python
        prompt = get_phase_system_prompt(
            config=config,
            is_strategic=True,
            phase_number=1,
            todos_content="- Explore workspace\\n- Create plan",
        )
        ```
    """
    deployment_dir = config._deployment_dir

    # 1. Load base template
    base_template = load_base_system_prompt(deployment_dir)

    # 2. Load phase component
    phase_component = load_phase_component(is_strategic, deployment_dir)

    # 3. Render phase component's {phase_number} placeholder
    rendered_component = phase_component.format(phase_number=phase_number)

    # 4. & 5. Inject component and render remaining placeholders
    oss_reasoning_level = config.llm.reasoning_level or "high"

    return base_template.format(
        oss_reasoning_level=oss_reasoning_level,
        agent_display_name=config.display_name,
        prompt_content=rendered_component,
        todos_content=todos_content,
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
        config_dir = get_project_root() / "config"
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

1. Create a plan in `plan.md`
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
    template_name = "summarization_prompt.txt"
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
        config_dir = get_project_root() / "config"
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
    1. Absolute path or explicit extension (.yaml/.json) -> use as-is
    2. config/{name}/config.yaml (directory with possible prompt overrides)
    3. config/{name}.yaml (single file config)

    Args:
        config_name: Config name (e.g., "defaults", "my_agent")
                    or full path to config file

    Returns:
        Tuple of (config_path, deployment_dir_or_none)
        - config_path: Full path to the config file
        - deployment_dir: Directory containing deployment files (for prompt resolution)
                         None if using single file config or direct path
    """
    # If it's already a full path or has explicit extension
    if os.path.isabs(config_name) or config_name.endswith((".yaml", ".yml", ".json")):
        return (config_name, None)

    project_root = get_project_root()
    config_dir = project_root / "config"

    # Try directory config first (config/{name}/config.yaml)
    # This allows prompt overrides in the same directory
    deployment_dir = config_dir / config_name
    deployment_config = deployment_dir / "config.yaml"

    if deployment_config.exists():
        return (str(deployment_config), str(deployment_dir))

    # Fall back to single file config (config/{name}.yaml)
    single_file_config = config_dir / f"{config_name}.yaml"

    if single_file_config.exists():
        return (str(single_file_config), None)

    # Return single file path even if it doesn't exist (let caller handle error)
    return (str(single_file_config), None)


# =============================================================================
# Strategic Todos Template Loaders
# =============================================================================


class StrategicTodosValidationError(Exception):
    """Raised when strategic todos template validation fails."""

    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or [message]


def _parse_strategic_todos_yaml(path: Path) -> List[Dict[str, Any]]:
    """Parse and validate a strategic todos YAML template.

    Expected schema:
    ```yaml
    todos:
      - id: 1
        content: "First task description"
      - id: 2
        content: "Second task description"
    ```

    Args:
        path: Path to the YAML template file

    Returns:
        List of todo dicts with 'id' and 'content' keys

    Raises:
        StrategicTodosValidationError: If validation fails
    """
    errors: List[str] = []

    # Read file
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        raise StrategicTodosValidationError(
            f"Failed to read strategic todos template: {path}",
            [str(e)],
        )

    # Parse YAML
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise StrategicTodosValidationError(
            f"Invalid YAML syntax in {path}: {e}",
            [f"YAML parse error: {e}"],
        )

    if data is None:
        raise StrategicTodosValidationError(
            f"Empty strategic todos template: {path}",
            ["File is empty or contains only whitespace"],
        )

    if not isinstance(data, dict):
        raise StrategicTodosValidationError(
            f"Strategic todos template must be a YAML mapping: {path}",
            [f"Expected mapping, got {type(data).__name__}"],
        )

    # Check required 'todos' key
    if "todos" not in data:
        raise StrategicTodosValidationError(
            f"Missing required 'todos' key in {path}",
            ["Strategic todos template must have a 'todos' key with a list of todo items"],
        )

    todos_raw = data["todos"]
    if not isinstance(todos_raw, list):
        raise StrategicTodosValidationError(
            f"'todos' must be a list in {path}",
            [f"Expected list for 'todos', got {type(todos_raw).__name__}"],
        )

    # Validate each todo item
    validated_todos: List[Dict[str, Any]] = []
    seen_ids: set = set()

    for i, item in enumerate(todos_raw):
        if not isinstance(item, dict):
            errors.append(f"Todo #{i + 1}: Expected mapping, got {type(item).__name__}")
            continue

        # Validate 'id'
        todo_id = item.get("id")
        if todo_id is None:
            errors.append(f"Todo #{i + 1}: Missing required 'id' field")
        elif not isinstance(todo_id, int):
            errors.append(
                f"Todo #{i + 1}: 'id' must be an integer, got {type(todo_id).__name__}"
            )
        elif todo_id in seen_ids:
            errors.append(f"Todo #{i + 1}: Duplicate id '{todo_id}'")
        else:
            seen_ids.add(todo_id)

        # Validate 'content'
        content_val = item.get("content")
        if content_val is None:
            errors.append(f"Todo #{i + 1}: Missing required 'content' field")
        elif not isinstance(content_val, str):
            errors.append(
                f"Todo #{i + 1}: 'content' must be a string, "
                f"got {type(content_val).__name__}"
            )
        elif len(content_val.strip()) < 10:
            errors.append(
                f"Todo #{i + 1}: 'content' too short ({len(content_val.strip())} chars). "
                f"Provide a meaningful task description."
            )

        # If valid so far, add to validated list
        if todo_id is not None and content_val is not None and not errors:
            validated_todos.append({
                "id": todo_id,
                "content": content_val.strip(),
            })

    if errors:
        raise StrategicTodosValidationError(
            f"Strategic todos validation failed with {len(errors)} error(s)",
            errors,
        )

    logger.debug(f"Parsed strategic todos template: {len(validated_todos)} todos from {path}")
    return validated_todos


def load_strategic_todos_template(
    template_name: str,
    deployment_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load a strategic todos template with deployment override support.

    Checks deployment directory first, then falls back to framework templates.
    This allows deployments to override strategic todos for customization.

    Args:
        template_name: Name of the template file (e.g., "strategic_todos_initial.yaml")
        deployment_dir: Path to deployment directory (e.g., config/my_agent).
                       If None, only framework templates are used.

    Returns:
        List of todo dicts with 'id' and 'content' keys

    Raises:
        FileNotFoundError: If template not found in either location
        StrategicTodosValidationError: If template is invalid

    Example:
        ```python
        todos = load_strategic_todos_template(
            "strategic_todos_initial.yaml",
            deployment_dir="config/my_agent"
        )
        # Returns: [{"id": 1, "content": "..."}, {"id": 2, "content": "..."}, ...]
        ```
    """
    # Check deployment directory first
    if deployment_dir:
        deployment_path = Path(deployment_dir) / template_name
        if deployment_path.exists():
            logger.debug(f"Loading strategic todos from deployment: {deployment_path}")
            return _parse_strategic_todos_yaml(deployment_path)

    # Fall back to framework templates directory
    templates_dir = get_project_root() / "config" / "templates"
    framework_path = templates_dir / template_name

    if framework_path.exists():
        logger.debug(f"Loading strategic todos from framework: {framework_path}")
        return _parse_strategic_todos_yaml(framework_path)

    raise FileNotFoundError(
        f"Strategic todos template not found: {template_name} "
        f"(checked: {deployment_dir}, {templates_dir})"
    )


def get_initial_strategic_todos_from_config(
    config: Optional["AgentConfig"] = None,
) -> List[Dict[str, Any]]:
    """Get initial strategic todos for job start.

    Loads from strategic_todos_initial.yaml template with deployment override support.

    Args:
        config: Agent configuration (for deployment directory). If None, uses
               framework defaults only.

    Returns:
        List of todo dicts ready for TodoManager.set_todos_from_list():
        [{"id": "todo_1", "content": "...", "status": "pending", "priority": "medium"}, ...]

    Example:
        ```python
        todos = get_initial_strategic_todos_from_config(config)
        todo_manager.set_todos_from_list(todos)
        ```
    """
    deployment_dir = config._deployment_dir if config else None

    try:
        raw_todos = load_strategic_todos_template(
            "strategic_todos_initial.yaml",
            deployment_dir=deployment_dir,
        )
    except FileNotFoundError:
        logger.warning(
            "strategic_todos_initial.yaml not found, using empty list. "
            "Create config/templates/strategic_todos_initial.yaml or deployment override."
        )
        return []

    # Convert to TodoManager format
    return [
        {
            "id": f"todo_{t['id']}",
            "content": t["content"],
            "status": "pending",
            "priority": "medium",
        }
        for t in raw_todos
    ]


def get_transition_strategic_todos_from_config(
    config: Optional["AgentConfig"] = None,
) -> List[Dict[str, Any]]:
    """Get strategic todos for phase transitions.

    Loads from strategic_todos_transition.yaml template with deployment override support.

    Args:
        config: Agent configuration (for deployment directory). If None, uses
               framework defaults only.

    Returns:
        List of todo dicts ready for TodoManager.set_todos_from_list():
        [{"id": "todo_1", "content": "...", "status": "pending", "priority": "medium"}, ...]

    Example:
        ```python
        todos = get_transition_strategic_todos_from_config(config)
        todo_manager.set_todos_from_list(todos)
        ```
    """
    deployment_dir = config._deployment_dir if config else None

    try:
        raw_todos = load_strategic_todos_template(
            "strategic_todos_transition.yaml",
            deployment_dir=deployment_dir,
        )
    except FileNotFoundError:
        logger.warning(
            "strategic_todos_transition.yaml not found, using empty list. "
            "Create config/templates/strategic_todos_transition.yaml or deployment override."
        )
        return []

    # Convert to TodoManager format
    return [
        {
            "id": f"todo_{t['id']}",
            "content": t["content"],
            "status": "pending",
            "priority": "medium",
        }
        for t in raw_todos
    ]
