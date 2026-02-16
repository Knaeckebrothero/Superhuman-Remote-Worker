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


class FileResolver:
    """Resolves template files with deployment override support.

    Checks deployment directory first, then falls back to a framework directory.
    This allows deployments to override specific files while using
    framework defaults for others.

    Example:
        ```python
        resolver = FileResolver(deployment_dir="/project/config/my_agent")

        # Will check: /project/config/my_agent/instructions.md
        # Falls back to: config/prompts/instructions.md
        content = resolver.load("instructions.md")
        ```
    """

    def __init__(
        self,
        deployment_dir: Optional[str] = None,
        framework_dir: Optional[Path] = None,
    ):
        """Initialize file resolver.

        Args:
            deployment_dir: Path to deployment directory (e.g., config/my_agent)
                          If None, only framework files are used.
            framework_dir: Path to framework directory. Defaults to config/prompts/.
        """
        self.deployment_dir = Path(deployment_dir) if deployment_dir else None
        self.framework_dir = framework_dir or (get_project_root() / "config" / "prompts")

    def resolve(self, template_name: str) -> Path:
        """Find template file, checking deployment dir first.

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
            f"Template not found: {template_name} "
            f"(checked: {self.deployment_dir}, {self.framework_dir})"
        )

    def load(self, template_name: str) -> str:
        """Load template content.

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


# Backward compatibility alias
PromptResolver = FileResolver


class MatrixResolver:
    """Base class for matrix-based file resolution.

    Resolves logical names to filenames through a 2D matrix: (type, model_family).
    Resolution chain (4 levels):
    1. Expert matrix → model-specific key → type
    2. Expert matrix → "default" key → type
    3. Base matrix → model-specific key → type
    4. Base matrix → "default" key → type

    Once the filename is determined, FileResolver locates the actual file
    (expert directory → framework directory).

    Subclasses define MATRIX_FILENAME, FRAMEWORK_DIR, and HARDCODED_DEFAULTS.
    """

    MATRIX_FILENAME: str = "matrix.yaml"
    FRAMEWORK_DIR: str = "config/prompts"
    HARDCODED_DEFAULTS: Dict[str, str] = {}

    def __init__(
        self,
        deployment_dir: Optional[str] = None,
        model_family: str = "default",
    ):
        self.deployment_dir = Path(deployment_dir) if deployment_dir else None
        self.model_family = model_family
        self._file_resolver = FileResolver(
            deployment_dir=deployment_dir,
            framework_dir=get_project_root() / self.FRAMEWORK_DIR,
        )

        # Load matrices
        self._expert_matrix = self._load_matrix(self.deployment_dir)
        base_matrix_path = get_project_root() / "config" / self.MATRIX_FILENAME
        self._base_matrix = self._load_matrix_from_path(base_matrix_path)

    @staticmethod
    def _load_matrix_from_path(path: Path) -> Dict[str, Dict[str, str]]:
        """Load a matrix YAML file. Returns empty dict if not found."""
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                return {}
            # Validate structure: each key maps to a dict of type → filename
            result = {}
            for key, value in data.items():
                if isinstance(value, dict):
                    result[key] = {k: str(v) for k, v in value.items() if v is not None}
            return result
        except Exception as e:
            logger.warning(f"Failed to load matrix {path}: {e}")
            return {}

    def _load_matrix(self, directory: Optional[Path]) -> Dict[str, Dict[str, str]]:
        """Load matrix YAML from a directory. Returns empty dict if not found."""
        if not directory:
            return {}
        return self._load_matrix_from_path(directory / self.MATRIX_FILENAME)

    def resolve_filename(self, entry_type: str) -> str:
        """Resolve a type to a filename through the 4-level fallback chain.

        Args:
            entry_type: Logical name (e.g., "systemprompt", "instructions")

        Returns:
            Filename string (e.g., "systemprompt.txt")
        """
        family = self.model_family

        # Level 1: Expert matrix, model-specific
        if family != "default" and family in self._expert_matrix:
            if entry_type in self._expert_matrix[family]:
                return self._expert_matrix[family][entry_type]

        # Level 2: Expert matrix, default
        if "default" in self._expert_matrix:
            if entry_type in self._expert_matrix["default"]:
                return self._expert_matrix["default"][entry_type]

        # Level 3: Base matrix, model-specific
        if family != "default" and family in self._base_matrix:
            if entry_type in self._base_matrix[family]:
                return self._base_matrix[family][entry_type]

        # Level 4: Base matrix, default
        if "default" in self._base_matrix:
            if entry_type in self._base_matrix["default"]:
                return self._base_matrix["default"][entry_type]

        # Final fallback: hardcoded defaults
        return self.HARDCODED_DEFAULTS.get(entry_type, f"{entry_type}.txt")

    def load(self, entry_type: str) -> str:
        """Resolve filename and load the content.

        Args:
            entry_type: Type to resolve and load

        Returns:
            File content as string
        """
        filename = self.resolve_filename(entry_type)
        return self._file_resolver.load(filename)

    def exists(self, entry_type: str) -> bool:
        """Check if a type can be resolved and the file exists."""
        filename = self.resolve_filename(entry_type)
        return self._file_resolver.exists(filename)


class PromptMatrixResolver(MatrixResolver):
    """Resolves prompt filenames through a 2D matrix: (prompt_type, model_family).

    Thin subclass of MatrixResolver for prompt files (config/prompts/).
    """

    MATRIX_FILENAME = "prompt_matrix.yaml"
    FRAMEWORK_DIR = "config/prompts"
    HARDCODED_DEFAULTS = {
        "systemprompt": "systemprompt.txt",
        "strategic": "strategic.txt",
        "tactical": "tactical.txt",
        "summarization": "summarization_prompt.txt",
    }

    # Backward compatibility: expose _prompt_resolver as alias for _file_resolver
    @property
    def _prompt_resolver(self):
        return self._file_resolver

    @_prompt_resolver.setter
    def _prompt_resolver(self, value):
        self._file_resolver = value


class InstructionMatrixResolver(MatrixResolver):
    """Resolves instruction filenames through a 2D matrix: (instruction_type, model_family).

    Handles non-prompt template files: instructions, strategic todos templates,
    workspace template, and todo guide.
    """

    MATRIX_FILENAME = "instruction_matrix.yaml"
    FRAMEWORK_DIR = "config/templates"
    HARDCODED_DEFAULTS = {
        "instructions": "instructions.md",
        "strategic_todos_initial": "strategic_todos_initial.yaml",
        "strategic_todos_transition": "strategic_todos_transition.yaml",
        "strategic_todos_resume": "strategic_todos_resume.yaml",
        "workspace_template": "workspace_template.md",
        "todo_guide": "todo_guide.md",
    }


@dataclass
class PhaseLLMOverride:
    """Phase-specific LLM overrides.

    Only specified (non-None) fields override the base LLM config.
    Used for strategic, tactical, and summarization phase customization.
    """

    model: Optional[str] = None
    provider: Optional[str] = None
    temperature: Optional[float] = None
    reasoning_level: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout: Optional[float] = None
    max_retries: Optional[int] = None
    multimodal: Optional[bool] = None
    parallel_tool_calls: Optional[bool] = None
    max_output_tokens: Optional[int] = None


@dataclass
class LLMConfig:
    """LLM configuration with optional phase-specific overrides.

    Base fields define the default model. Phase-specific overrides (strategic,
    tactical, summarization) can specify different models/providers for each phase.

    Example:
        llm:
          model: claude-sonnet-4-20250514
          temperature: 0.3
          multimodal: true  # Model can process images directly
          strategic:
            model: claude-opus-4-5-20250514
          tactical:
            temperature: 0.2
          summarization:
            model: gpt-4o
            provider: openai
    """

    model: str = "gpt-4o"
    provider: Optional[str] = None  # "openai", "anthropic", "google", "groq", "openrouter" (auto-detect if None)
    temperature: float = 0.0
    reasoning_level: str = "high"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout: Optional[float] = 600.0  # 10 minutes default
    max_retries: int = 3
    multimodal: bool = False  # Whether model can process images directly
    parallel_tool_calls: bool = False  # Allow multiple tool calls per response
    max_output_tokens: Optional[int] = None  # Override max output tokens (auto-detected if None)

    # Phase-specific overrides (optional)
    strategic: Optional[PhaseLLMOverride] = None
    tactical: Optional[PhaseLLMOverride] = None
    summarization: Optional[PhaseLLMOverride] = None

    def get_phase_config(self, phase: str) -> "LLMConfig":
        """Get effective LLM config for a specific phase.

        Merges phase-specific overrides with base config. Only non-None
        fields from the phase override replace base values.

        Args:
            phase: One of "strategic", "tactical", "summarization"

        Returns:
            New LLMConfig with phase-specific overrides applied.
            Returns self if no override exists for the phase.
        """
        override = getattr(self, phase, None)
        if not override:
            return self

        # Create new config with overrides applied (don't copy phase fields)
        return LLMConfig(
            model=override.model if override.model is not None else self.model,
            provider=override.provider if override.provider is not None else self.provider,
            temperature=override.temperature if override.temperature is not None else self.temperature,
            reasoning_level=override.reasoning_level if override.reasoning_level is not None else self.reasoning_level,
            base_url=override.base_url if override.base_url is not None else self.base_url,
            api_key=override.api_key if override.api_key is not None else self.api_key,
            timeout=override.timeout if override.timeout is not None else self.timeout,
            max_retries=override.max_retries if override.max_retries is not None else self.max_retries,
            multimodal=override.multimodal if override.multimodal is not None else self.multimodal,
            parallel_tool_calls=override.parallel_tool_calls if override.parallel_tool_calls is not None else self.parallel_tool_calls,
            max_output_tokens=override.max_output_tokens if override.max_output_tokens is not None else self.max_output_tokens,
            # Phase overrides not inherited to resolved config
            strategic=None,
            tactical=None,
            summarization=None,
        )

    def has_phase_overrides(self) -> bool:
        """Check if any phase-specific overrides are configured."""
        return any([self.strategic, self.tactical, self.summarization])


@dataclass
class WorkspaceConfig:
    """Workspace configuration."""

    structure: List[str] = field(default_factory=list)
    instructions_template: str = ""
    initial_files: Dict[str, str] = field(default_factory=dict)
    max_read_words: int = 25000  # Maximum word count for file reads
    git_versioning: bool = True  # Enable git versioning for workspace history
    git_ignore_patterns: List[str] = field(
        default_factory=lambda: ["*.db", "*.log", "__pycache__/", ".DS_Store", "*.pyc", "documents/"]
    )


@dataclass
class ToolsConfig:
    """Tools configuration by category (matches src/tools/ packages)."""

    workspace: List[str] = field(default_factory=list)
    core: List[str] = field(default_factory=list)
    document: List[str] = field(default_factory=list)
    research: List[str] = field(default_factory=list)
    citation: List[str] = field(default_factory=list)
    graph: List[str] = field(default_factory=list)
    git: List[str] = field(default_factory=list)
    coding: List[str] = field(default_factory=list)


@dataclass
class ConnectionsConfig:
    """Database connections configuration."""

    postgres: bool = True


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


def _parse_phase_override(data: Optional[Dict[str, Any]]) -> Optional[PhaseLLMOverride]:
    """Parse a phase-specific LLM override from config dict.

    Args:
        data: Dict with override fields, or None

    Returns:
        PhaseLLMOverride if data provided, None otherwise
    """
    if not data:
        return None

    return PhaseLLMOverride(
        model=data.get("model"),
        provider=data.get("provider"),
        temperature=data.get("temperature"),
        reasoning_level=data.get("reasoning_level"),
        base_url=data.get("base_url"),
        api_key=data.get("api_key"),
        timeout=data.get("timeout"),
        max_retries=data.get("max_retries"),
        multimodal=data.get("multimodal"),
        parallel_tool_calls=data.get("parallel_tool_calls"),
        max_output_tokens=data.get("max_output_tokens"),
    )


def _parse_llm_config(llm_data: Dict[str, Any]) -> LLMConfig:
    """Parse LLM configuration including phase-specific overrides.

    Args:
        llm_data: Dict with LLM config fields

    Returns:
        LLMConfig with base settings and optional phase overrides
    """
    return LLMConfig(
        model=llm_data.get("model", "gpt-4o"),
        provider=llm_data.get("provider"),
        temperature=llm_data.get("temperature", 0.0),
        reasoning_level=llm_data.get("reasoning_level", "high"),
        base_url=llm_data.get("base_url"),
        api_key=llm_data.get("api_key"),
        timeout=llm_data.get("timeout", 600.0),
        max_retries=llm_data.get("max_retries", 3),
        multimodal=llm_data.get("multimodal", False),
        parallel_tool_calls=llm_data.get("parallel_tool_calls", False),
        max_output_tokens=llm_data.get("max_output_tokens"),
        # Phase-specific overrides
        strategic=_parse_phase_override(llm_data.get("strategic")),
        tactical=_parse_phase_override(llm_data.get("tactical")),
        summarization=_parse_phase_override(llm_data.get("summarization")),
    )


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
    llm_config = _parse_llm_config(llm_data)

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
        git_versioning=workspace_data.get("git_versioning", True),
        git_ignore_patterns=workspace_data.get(
            "git_ignore_patterns",
            ["*.db", "*.log", "__pycache__/", ".DS_Store", "*.pyc", "documents/"]
        ),
    )

    tools_data = data.get("tools", {})
    tools_config = ToolsConfig(
        workspace=tools_data.get("workspace", []),
        core=tools_data.get("core", []),
        document=tools_data.get("document", []),
        research=tools_data.get("research", []),
        citation=tools_data.get("citation", []),
        graph=tools_data.get("graph", []),
        git=tools_data.get("git", []),
        coding=tools_data.get("coding", []),
    )

    connections_data = data.get("connections", {})
    connections_config = ConnectionsConfig(
        postgres=connections_data.get("postgres", True),
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
    )

    # Collect extra fields (agent-specific config)
    known_fields = {
        "$schema", "agent_id", "display_name", "description", "llm", "workspace",
        "tools", "connections", "polling", "limits", "context_management",
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
    llm_config = _parse_llm_config(llm_data)

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
        git_versioning=workspace_data.get("git_versioning", True),
        git_ignore_patterns=workspace_data.get(
            "git_ignore_patterns",
            ["*.db", "*.log", "__pycache__/", ".DS_Store", "*.pyc", "documents/"]
        ),
    )

    tools_data = data.get("tools", {})
    tools_config = ToolsConfig(
        workspace=tools_data.get("workspace", []),
        core=tools_data.get("core", []),
        document=tools_data.get("document", []),
        research=tools_data.get("research", []),
        citation=tools_data.get("citation", []),
        graph=tools_data.get("graph", []),
        git=tools_data.get("git", []),
        coding=tools_data.get("coding", []),
    )

    connections_data = data.get("connections", {})
    connections_config = ConnectionsConfig(
        postgres=connections_data.get("postgres", True),
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
    )

    # Collect extra fields
    known_fields = {
        "$schema", "agent_id", "display_name", "description", "llm", "workspace",
        "tools", "connections", "polling", "limits", "context_management",
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


def _detect_provider(model: str, explicit_provider: Optional[str] = None) -> str:
    """Detect LLM provider from model name or explicit setting.

    Args:
        model: Model name (e.g., "claude-sonnet-4-20250514", "gemini-2.0-flash", "gpt-4o",
               "openrouter/anthropic/claude-opus-4")
        explicit_provider: Explicit provider override ("openai", "anthropic", "google",
                          "groq", "openrouter")

    Returns:
        Provider name: "openai", "anthropic", "google", "groq", or "openrouter"

    Note:
        Groq requires explicit provider setting since it hosts open models
        (Llama, Mixtral, etc.) that could also be served via other providers.
    """
    if explicit_provider:
        return explicit_provider.lower()

    model_lower = model.lower()
    if model_lower.startswith("openrouter/"):
        return "openrouter"
    if model_lower.startswith("groq/"):
        return "groq"
    if model_lower.startswith("claude"):
        return "anthropic"
    if model_lower.startswith("gemini"):
        return "google"
    # Default to openai (covers gpt-*, openai/*, local models)
    return "openai"


def detect_model_family(model: str) -> str:
    """Detect model family from model name for prompt matrix resolution.

    Strips provider prefixes (openrouter/, groq/, openai/) and pattern-matches
    the model name to a family string. Returns "default" as fallback.

    Args:
        model: Model name (e.g., "claude-opus-4-6", "openrouter/deepseek/deepseek-r1")

    Returns:
        Family string (e.g., "claude-opus", "deepseek", "default")
    """
    name = model.lower()

    # Strip provider prefixes to get the actual model name
    for prefix in ("openrouter/", "groq/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            # Strip second-level provider prefix (e.g., "anthropic/" in "openrouter/anthropic/claude-opus-4")
            if "/" in name:
                name = name.split("/", 1)[1]
            break

    # Strip openai/ prefix for self-hosted models
    if name.startswith("openai/"):
        name = name[len("openai/"):]

    # Pattern match model name to family
    if name.startswith("claude-opus"):
        return "claude-opus"
    if name.startswith("claude-sonnet"):
        return "claude-sonnet"
    if name.startswith("claude-haiku"):
        return "claude-haiku"
    if name.startswith("gpt-5"):
        return "gpt-5"
    if name.startswith("gpt-4o"):
        return "gpt-4o"
    if name.startswith(("o1", "o3", "o4")):
        return "o-series"
    if "deepseek" in name:
        return "deepseek"
    if "qwen" in name or "qwq" in name:
        return "qwen"
    if "llama" in name:
        return "llama"
    if name.startswith("gemini"):
        return "gemini"
    if name.startswith("gpt-oss"):
        return "gpt-oss"

    return "default"


def _needs_custom_base_url(model: str) -> bool:
    """Check if model requires a custom base URL (OpenAI-compatible endpoint).

    Models with 'openai/' prefix are served via OpenAI-compatible endpoints
    (vLLM, llama.cpp, sglang) and need LLM_BASE_URL. Native OpenAI models
    (gpt-*, o1-*, o3-*, o4-*) go directly to api.openai.com.
    """
    return model.lower().startswith("openai/")


def _should_use_reasoning_summary(model: str) -> bool:
    """Check if model supports readable reasoning summaries via the Responses API.

    Native OpenAI reasoning models return reasoning content through the
    Responses API when the reasoning.summary parameter is set.
    Models with a '/' prefix (openai/*, groq/*) are proxy models and excluded.
    """
    model_lower = model.lower()
    if "/" in model_lower:
        return False
    reasoning_prefixes = ("o1", "o3", "o4", "gpt-5")
    return any(model_lower.startswith(p) for p in reasoning_prefixes)


def create_llm(
    config: LLMConfig,
    limits: Optional[LimitsConfig] = None,
) -> BaseChatModel:
    """Create an LLM instance from configuration.

    Supports multiple providers:
    - OpenAI (and OpenAI-compatible APIs like vLLM, Ollama)
    - Anthropic (Claude models)
    - Google (Gemini models)
    - Groq (fast inference for open models)
    - OpenRouter (300+ models via unified API)

    Provider is auto-detected from model name or can be explicitly set via config.provider.

    Args:
        config: LLM configuration
        limits: Optional limits configuration for context token limit.

    Returns:
        Configured LLM instance (ChatOpenAI, ChatAnthropic, ChatGoogleGenerativeAI, or ChatGroq)
    """
    provider = _detect_provider(config.model, config.provider)

    if provider == "anthropic":
        return _create_anthropic_llm(config, limits)
    elif provider == "google":
        return _create_google_llm(config, limits)
    elif provider == "groq":
        return _create_groq_llm(config, limits)
    elif provider == "openrouter":
        return _create_openrouter_llm(config, limits)
    else:
        return _create_openai_llm(config, limits)


def _create_openai_llm(
    config: LLMConfig,
    limits: Optional[LimitsConfig] = None,
) -> BaseChatModel:
    """Create OpenAI-compatible LLM.

    Uses ReasoningChatOpenAI which provides:
    - reasoning_content capture for DeepSeek-style models
    - HTTP-layer context overflow protection
    - Automatic API key rotation via KeyRing (when multiple keys configured)

    The base_url can point to any OpenAI-compatible endpoint (vLLM, Ollama, etc.)

    Multiple API keys can be provided as a comma-separated string in
    OPENAI_API_KEY (e.g. "sk-key1,sk-key2,sk-key3"). The KeyRing will
    rotate through them on auth/quota failures.
    """
    from src.llm.key_ring import parse_key_string, get_or_create_key_ring

    # Parse API keys (supports comma-separated list for fallback)
    raw_key = config.api_key or os.getenv("OPENAI_API_KEY", "not-needed")
    keys = parse_key_string(raw_key) or ["not-needed"]
    cooldown = float(os.getenv("KEY_COOLDOWN_SECONDS", "1800"))
    key_ring = get_or_create_key_ring(keys, provider="openai", cooldown_seconds=cooldown)

    # SDK gets the first key; KeyRing overrides the header in send()
    api_key = keys[0]

    # Get base URL: explicit config wins, then LLM_BASE_URL for openai/ models only.
    # Native OpenAI models (gpt-*, o1-*, etc.) go directly to api.openai.com.
    if config.base_url:
        base_url = config.base_url
    elif _needs_custom_base_url(config.model):
        base_url = os.getenv("LLM_BASE_URL")
    else:
        base_url = None

    # Build model kwargs
    model_kwargs = {}

    # Build kwargs for ChatOpenAI
    llm_kwargs = {
        "model": config.model,
        "temperature": config.temperature,
        "api_key": api_key,
        "max_retries": config.max_retries,
    }

    # Add reasoning parameters
    reasoning_mode = "none"
    if config.reasoning_level and config.reasoning_level != "none":
        if _should_use_reasoning_summary(config.model):
            # Native OpenAI reasoning models: use Responses API with readable summaries.
            # Passing reasoning={} triggers LangChain's automatic Responses API routing.
            llm_kwargs["reasoning"] = {
                "effort": config.reasoning_level,
                "summary": "auto",
            }
            reasoning_mode = f"responses_api(effort={config.reasoning_level})"
        else:
            # Other models (DeepSeek, vLLM-hosted, etc.): use Chat Completions API.
            model_kwargs["reasoning_effort"] = config.reasoning_level
            reasoning_mode = f"chat_completions(effort={config.reasoning_level})"

    # Add timeout if specified
    if config.timeout is not None:
        llm_kwargs["timeout"] = config.timeout

    # Only add base_url if specified
    if base_url:
        llm_kwargs["base_url"] = base_url

    # Only add model_kwargs if non-empty
    if model_kwargs:
        llm_kwargs["model_kwargs"] = model_kwargs

    if config.max_output_tokens is not None:
        llm_kwargs["max_tokens"] = config.max_output_tokens

    # Add max_context_tokens for HTTP-layer validation (Layer 0 safety)
    max_context_tokens = limits.model_max_context_tokens if limits else None
    if max_context_tokens:
        llm_kwargs["max_context_tokens"] = max_context_tokens

    # Pass KeyRing for automatic key rotation
    llm_kwargs["key_ring"] = key_ring

    llm = ReasoningChatOpenAI(**llm_kwargs)

    key_info = f"{len(keys)} key(s)" if len(keys) > 1 else "1 key"
    logger.info(
        f"Created OpenAI LLM: model={config.model}, temp={config.temperature}, "
        f"base_url={base_url or 'default'}, timeout={config.timeout}s, "
        f"max_retries={config.max_retries}, max_context_tokens={max_context_tokens or 'default'}, "
        f"reasoning={reasoning_mode}, keys={key_info}"
    )

    return llm


def _create_anthropic_llm(
    config: LLMConfig,
    limits: Optional[LimitsConfig] = None,
) -> BaseChatModel:
    """Create Anthropic Claude LLM.

    Requires ANTHROPIC_API_KEY environment variable or config.api_key.
    """
    # Lazy import to avoid requiring the package when not used
    from langchain_anthropic import ChatAnthropic

    api_key = config.api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY environment variable required for Anthropic provider. "
            "Set it in your environment or provide api_key in config."
        )

    llm_kwargs = {
        "model": config.model,
        "temperature": config.temperature,
        "api_key": api_key,
        "max_retries": config.max_retries,
    }

    if config.timeout is not None:
        llm_kwargs["timeout"] = config.timeout

    # Anthropic requires max_tokens - use config override or model-aware defaults
    if config.max_output_tokens is not None:
        llm_kwargs["max_tokens"] = config.max_output_tokens
    else:
        model_lower = config.model.lower()
        if any(x in model_lower for x in ("opus-4-6", "opus-4-5", "opus-4-1", "opus-4-0")):
            llm_kwargs["max_tokens"] = 32000
        elif any(x in model_lower for x in ("sonnet-4-5", "sonnet-4-0")):
            llm_kwargs["max_tokens"] = 16384
        elif limits and limits.model_max_context_tokens:
            llm_kwargs["max_tokens"] = min(8192, limits.model_max_context_tokens // 4)
        else:
            llm_kwargs["max_tokens"] = 4096

    llm = ChatAnthropic(**llm_kwargs)

    logger.info(
        f"Created Anthropic LLM: model={config.model}, temp={config.temperature}, "
        f"timeout={config.timeout}s, max_retries={config.max_retries}, "
        f"max_tokens={llm_kwargs['max_tokens']}"
    )

    return llm


def _create_google_llm(
    config: LLMConfig,
    limits: Optional[LimitsConfig] = None,
) -> BaseChatModel:
    """Create Google Gemini LLM.

    Requires GOOGLE_API_KEY environment variable or config.api_key.
    """
    # Lazy import to avoid requiring the package when not used
    from langchain_google_genai import ChatGoogleGenerativeAI

    api_key = config.api_key or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError(
            "GOOGLE_API_KEY environment variable required for Google provider. "
            "Set it in your environment or provide api_key in config."
        )

    llm_kwargs = {
        "model": config.model,
        "temperature": config.temperature,
        "google_api_key": api_key,
    }

    # Google's timeout parameter name differs
    if config.timeout is not None:
        llm_kwargs["timeout"] = config.timeout

    llm = ChatGoogleGenerativeAI(**llm_kwargs)

    logger.info(
        f"Created Google LLM: model={config.model}, temp={config.temperature}, "
        f"timeout={config.timeout}s"
    )

    return llm


def _create_groq_llm(
    config: LLMConfig,
    limits: Optional[LimitsConfig] = None,
) -> BaseChatModel:
    """Create Groq LLM for fast inference.

    Requires GROQ_API_KEY environment variable or config.api_key.
    Groq hosts open models (Llama, Mixtral, Gemma) with fast inference.
    """
    # Lazy import to avoid requiring the package when not used
    from langchain_groq import ChatGroq

    api_key = config.api_key or os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError(
            "GROQ_API_KEY environment variable required for Groq provider. "
            "Set it in your environment or provide api_key in config."
        )

    # Strip groq/ prefix — Groq API expects bare model names
    model = config.model
    if model.lower().startswith("groq/"):
        model = model[len("groq/"):]

    llm_kwargs = {
        "model": model,
        "temperature": config.temperature,
        "api_key": api_key,
        "max_retries": config.max_retries,
    }

    if config.timeout is not None:
        llm_kwargs["timeout"] = config.timeout

    # Optional: custom base URL for Groq enterprise/proxy
    if config.base_url:
        llm_kwargs["groq_api_base"] = config.base_url

    llm = ChatGroq(**llm_kwargs)

    logger.info(
        f"Created Groq LLM: model={model}, temp={config.temperature}, "
        f"timeout={config.timeout}s, max_retries={config.max_retries}"
    )

    return llm


def _create_openrouter_llm(
    config: LLMConfig,
    limits: Optional[LimitsConfig] = None,
) -> BaseChatModel:
    """Create OpenRouter LLM (300+ models via unified OpenAI-compatible API).

    Requires OPENROUTER_API_KEY environment variable or config.api_key.
    Routes through ReasoningChatOpenAI with the OpenRouter base URL.

    Model names are specified as openrouter/<provider>/<model>, e.g.:
    - openrouter/anthropic/claude-opus-4
    - openrouter/openai/gpt-4o
    - openrouter/meta-llama/llama-3.3-70b-instruct
    - openrouter/deepseek/deepseek-r1

    The openrouter/ prefix is stripped before sending to the API.

    Multiple API keys can be provided as a comma-separated string in
    OPENROUTER_API_KEY for automatic fallback rotation.

    Optional headers (for OpenRouter leaderboard):
    - OPENROUTER_REFERER: Your site URL
    - OPENROUTER_TITLE: Your app name
    """
    from src.llm.key_ring import parse_key_string, get_or_create_key_ring

    # Parse API keys (supports comma-separated list for fallback)
    raw_key = config.api_key or os.getenv("OPENROUTER_API_KEY")
    if not raw_key:
        raise ValueError(
            "OPENROUTER_API_KEY environment variable required for OpenRouter provider. "
            "Set it in your environment or provide api_key in config."
        )
    keys = parse_key_string(raw_key)
    if not keys:
        raise ValueError("OPENROUTER_API_KEY is empty after parsing.")
    cooldown = float(os.getenv("KEY_COOLDOWN_SECONDS", "1800"))
    key_ring = get_or_create_key_ring(keys, provider="openrouter", cooldown_seconds=cooldown)

    # SDK gets the first key; KeyRing overrides the header in send()
    api_key = keys[0]

    # Strip openrouter/ prefix — OpenRouter expects provider/model format
    model = config.model
    if model.lower().startswith("openrouter/"):
        model = model[len("openrouter/"):]

    # Base URL: explicit config wins, otherwise always OpenRouter
    base_url = config.base_url or "https://openrouter.ai/api/v1"

    # Build model kwargs
    model_kwargs = {}

    # OpenRouter proxies Chat Completions only (no Responses API),
    # so reasoning_effort goes in model_kwargs when set
    if config.reasoning_level and config.reasoning_level != "none":
        model_kwargs["reasoning_effort"] = config.reasoning_level

    # Build kwargs for ReasoningChatOpenAI
    llm_kwargs = {
        "model": model,
        "temperature": config.temperature,
        "api_key": api_key,
        "base_url": base_url,
        "max_retries": config.max_retries,
    }

    # Add optional OpenRouter headers for leaderboard identification
    default_headers = {}
    referer = os.getenv("OPENROUTER_REFERER")
    title = os.getenv("OPENROUTER_TITLE")
    if referer:
        default_headers["HTTP-Referer"] = referer
    if title:
        default_headers["X-Title"] = title
    if default_headers:
        llm_kwargs["default_headers"] = default_headers

    if config.timeout is not None:
        llm_kwargs["timeout"] = config.timeout

    if model_kwargs:
        llm_kwargs["model_kwargs"] = model_kwargs

    if config.max_output_tokens is not None:
        llm_kwargs["max_tokens"] = config.max_output_tokens

    # Add max_context_tokens for HTTP-layer validation (Layer 0 safety)
    max_context_tokens = limits.model_max_context_tokens if limits else None
    if max_context_tokens:
        llm_kwargs["max_context_tokens"] = max_context_tokens

    # Pass KeyRing for automatic key rotation
    llm_kwargs["key_ring"] = key_ring

    llm = ReasoningChatOpenAI(**llm_kwargs)

    key_info = f"{len(keys)} key(s)" if len(keys) > 1 else "1 key"
    reasoning_mode = f"chat_completions(effort={config.reasoning_level})" if config.reasoning_level and config.reasoning_level != "none" else "none"
    logger.info(
        f"Created OpenRouter LLM: model={model}, temp={config.temperature}, "
        f"base_url={base_url}, timeout={config.timeout}s, "
        f"max_retries={config.max_retries}, max_context_tokens={max_context_tokens or 'default'}, "
        f"reasoning={reasoning_mode}, keys={key_info}"
    )

    return llm


# =============================================================================
# Phase-Aware System Prompts
# =============================================================================


def load_base_system_prompt(matrix_resolver: PromptMatrixResolver) -> str:
    """Load the base system prompt template via prompt matrix resolution.

    Args:
        matrix_resolver: PromptMatrixResolver for model-aware filename resolution.

    Returns:
        Raw template string with placeholders ({prompt_content}, {todos_content}, etc.)

    Raises:
        FileNotFoundError: If template not found
    """
    return matrix_resolver.load("systemprompt")


def load_phase_component(
    is_strategic: bool,
    matrix_resolver: PromptMatrixResolver,
) -> str:
    """Load the phase-specific component (strategic.txt or tactical.txt).

    Args:
        is_strategic: True for strategic phase, False for tactical
        matrix_resolver: PromptMatrixResolver for model-aware filename resolution.

    Returns:
        Raw template string with {phase_number} placeholder

    Raises:
        FileNotFoundError: If template not found
    """
    prompt_type = "strategic" if is_strategic else "tactical"
    return matrix_resolver.load(prompt_type)


def get_phase_system_prompt(
    config: AgentConfig,
    is_strategic: bool,
    phase_number: int = 0,
    todos_content: str = "",
    model: str = "",
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
        model: Model name for prompt matrix resolution.

    Returns:
        Fully rendered system prompt string

    Example:
        ```python
        prompt = get_phase_system_prompt(
            config=config,
            is_strategic=True,
            phase_number=1,
            todos_content="- Explore workspace\\n- Create plan",
            model="claude-opus-4-6",
        )
        ```
    """
    # Check for pre-resolved prompt content (from resolved_config JSONB)
    resolved_prompts = config.extra.get("_resolved_prompts", {})

    model_family = detect_model_family(model) if model else "default"
    resolver = PromptMatrixResolver(config._deployment_dir, model_family)

    # 1. Load base template
    base_template = resolved_prompts.get("systemprompt") or load_base_system_prompt(resolver)

    # 2. Load phase component
    prompt_type = "strategic" if is_strategic else "tactical"
    phase_component = resolved_prompts.get(prompt_type) or load_phase_component(is_strategic, resolver)

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


def load_instructions(config: AgentConfig, model: str = "") -> str:
    """Load the instructions template for the agent.

    Uses InstructionMatrixResolver for model-aware instruction resolution.

    Args:
        config: Agent configuration
        model: Model name for instruction matrix resolution.

    Returns:
        Instructions content to be placed in workspace
    """
    # Check for pre-resolved content (from resolved_config JSONB)
    resolved = config.extra.get("_resolved_instructions", {})
    if resolved.get("instructions"):
        return resolved["instructions"]

    model_family = detect_model_family(model) if model else "default"
    resolver = InstructionMatrixResolver(config._deployment_dir, model_family)
    try:
        return resolver.load("instructions")
    except FileNotFoundError:
        logger.warning(
            "Instructions template not found. Using minimal instructions."
        )
        # Build tool list from all categories
        all_tools = []
        all_tools.extend(config.tools.workspace)
        all_tools.extend(config.tools.core)
        all_tools.extend(config.tools.document)
        all_tools.extend(config.tools.research)
        all_tools.extend(config.tools.citation)
        all_tools.extend(config.tools.graph)
        all_tools.extend(config.tools.coding)
        tools_str = ", ".join(all_tools) if all_tools else "(none configured)"

        return f"""# {config.display_name} Instructions

You are running as {config.display_name}.

## Available Tools

{tools_str}

See `tools/README.md` for detailed documentation of each tool.

## How to Work

1. Create a plan in `plan.md`
2. Use todos to track immediate steps
3. Write results to files as you go
4. When complete, call `job_complete`
"""


def load_summarization_prompt(config: AgentConfig, model: str = "") -> str:
    """Load the summarization prompt template.

    Uses PromptMatrixResolver for model-aware prompt resolution.

    Args:
        config: Agent configuration
        model: Model name for prompt matrix resolution.

    Returns:
        Summarization prompt content
    """
    # Check for pre-resolved content (from resolved_config JSONB)
    resolved = config.extra.get("_resolved_prompts", {})
    if resolved.get("summarization"):
        return resolved["summarization"]

    model_family = detect_model_family(model) if model else "default"
    resolver = PromptMatrixResolver(config._deployment_dir, model_family)
    try:
        return resolver.load("summarization")
    except FileNotFoundError:
        logger.warning(
            "Summarization prompt not found. Using default prompt."
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
        config.tools.core +
        config.tools.document +
        config.tools.research +
        config.tools.citation +
        config.tools.graph +
        config.tools.git +
        config.tools.coding
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

    # Try experts directory (config/experts/{name}/config.yaml)
    experts_dir = config_dir / "experts" / config_name
    experts_config = experts_dir / "config.yaml"

    if experts_config.exists():
        return (str(experts_config), str(experts_dir))

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


def _parse_strategic_todos_yaml_from_string(content: str) -> List[Dict[str, Any]]:
    """Parse and validate strategic todos from a YAML string.

    Same validation as _parse_strategic_todos_yaml but works on string content
    instead of a file path. Used when loading from resolved_config JSONB.

    Args:
        content: YAML string with todos schema

    Returns:
        List of todo dicts with 'id' and 'content' keys
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise StrategicTodosValidationError(
            f"Invalid YAML syntax in strategic todos: {e}",
            [f"YAML parse error: {e}"],
        )

    if data is None or not isinstance(data, dict) or "todos" not in data:
        raise StrategicTodosValidationError(
            "Strategic todos content must be a YAML mapping with 'todos' key",
            ["Missing or invalid 'todos' structure"],
        )

    todos_raw = data["todos"]
    if not isinstance(todos_raw, list):
        raise StrategicTodosValidationError(
            "'todos' must be a list",
            [f"Expected list for 'todos', got {type(todos_raw).__name__}"],
        )

    validated_todos: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen_ids: set = set()

    for i, item in enumerate(todos_raw):
        if not isinstance(item, dict):
            errors.append(f"Todo #{i + 1}: Expected mapping, got {type(item).__name__}")
            continue
        todo_id = item.get("id")
        content_val = item.get("content")
        if todo_id is None:
            errors.append(f"Todo #{i + 1}: Missing required 'id' field")
        elif not isinstance(todo_id, int):
            errors.append(f"Todo #{i + 1}: 'id' must be an integer")
        elif todo_id in seen_ids:
            errors.append(f"Todo #{i + 1}: Duplicate id '{todo_id}'")
        else:
            seen_ids.add(todo_id)
        if content_val is None:
            errors.append(f"Todo #{i + 1}: Missing required 'content' field")
        elif not isinstance(content_val, str):
            errors.append(f"Todo #{i + 1}: 'content' must be a string")
        elif len(content_val.strip()) < 10:
            errors.append(f"Todo #{i + 1}: 'content' too short")
        if todo_id is not None and content_val is not None and not errors:
            validated_todos.append({"id": todo_id, "content": content_val.strip()})

    if errors:
        raise StrategicTodosValidationError(
            f"Strategic todos validation failed with {len(errors)} error(s)", errors,
        )
    return validated_todos


def load_strategic_todos_template(
    template_name: str,
    deployment_dir: Optional[str] = None,
    model: str = "",
    resolved_content: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load a strategic todos template with deployment override support.

    Uses InstructionMatrixResolver for model-aware resolution. Checks
    deployment directory first, then falls back to framework templates.

    Args:
        template_name: Name of the template file (e.g., "strategic_todos_initial.yaml")
        deployment_dir: Path to deployment directory (e.g., config/my_agent).
                       If None, only framework templates are used.
        model: Model name for instruction matrix resolution.
        resolved_content: Pre-resolved YAML content (from resolved_config JSONB).

    Returns:
        List of todo dicts with 'id' and 'content' keys

    Raises:
        FileNotFoundError: If template not found in either location
        StrategicTodosValidationError: If template is invalid
    """
    # Check for pre-resolved content first
    if resolved_content:
        logger.debug("Loading strategic todos from resolved content")
        return _parse_strategic_todos_yaml_from_string(resolved_content)

    # Use InstructionMatrixResolver for 4-level fallback
    model_family = detect_model_family(model) if model else "default"
    resolver = InstructionMatrixResolver(deployment_dir, model_family)

    # Strip .yaml extension for instruction type key
    instruction_type = template_name.replace(".yaml", "")

    try:
        path = resolver._file_resolver.resolve(resolver.resolve_filename(instruction_type))
        logger.debug(f"Loading strategic todos from: {path}")
        return _parse_strategic_todos_yaml(path)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Strategic todos template not found: {template_name} "
            f"(checked: {deployment_dir}, config/templates/)"
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
    """
    deployment_dir = config._deployment_dir if config else None
    model = config.llm.model if config else ""

    # Check for pre-resolved content
    resolved_content = None
    if config:
        resolved = config.extra.get("_resolved_instructions", {})
        resolved_content = resolved.get("strategic_todos_initial")

    try:
        raw_todos = load_strategic_todos_template(
            "strategic_todos_initial.yaml",
            deployment_dir=deployment_dir,
            model=model,
            resolved_content=resolved_content,
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
    """
    deployment_dir = config._deployment_dir if config else None
    model = config.llm.model if config else ""

    # Check for pre-resolved content
    resolved_content = None
    if config:
        resolved = config.extra.get("_resolved_instructions", {})
        resolved_content = resolved.get("strategic_todos_transition")

    try:
        raw_todos = load_strategic_todos_template(
            "strategic_todos_transition.yaml",
            deployment_dir=deployment_dir,
            model=model,
            resolved_content=resolved_content,
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


def get_resume_strategic_todos_from_config(
    config: Optional["AgentConfig"] = None,
) -> List[Dict[str, Any]]:
    """Get strategic todos for resuming a frozen job with feedback.

    Loads from strategic_todos_resume.yaml template with deployment override support.

    Args:
        config: Agent configuration (for deployment directory). If None, uses
               framework defaults only.

    Returns:
        List of todo dicts ready for TodoManager.set_todos_from_list():
        [{"id": "todo_1", "content": "...", "status": "pending", "priority": "medium"}, ...]
    """
    deployment_dir = config._deployment_dir if config else None
    model = config.llm.model if config else ""

    # Check for pre-resolved content
    resolved_content = None
    if config:
        resolved = config.extra.get("_resolved_instructions", {})
        resolved_content = resolved.get("strategic_todos_resume")

    try:
        raw_todos = load_strategic_todos_template(
            "strategic_todos_resume.yaml",
            deployment_dir=deployment_dir,
            model=model,
            resolved_content=resolved_content,
        )
    except FileNotFoundError:
        logger.warning(
            "strategic_todos_resume.yaml not found, using empty list. "
            "Create config/templates/strategic_todos_resume.yaml or deployment override."
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


# =============================================================================
# Resolved Config Serialization
# =============================================================================


def serialize_resolved_config(config: AgentConfig, model: str = "") -> dict:
    """Serialize the fully resolved config (agent config + all prompt/instruction content).

    Captures everything needed to reproduce a job's config without disk access.
    Used to freeze config into the resolved_config JSONB column at job start.

    Args:
        config: Fully resolved AgentConfig
        model: Model name for matrix resolution

    Returns:
        Dict suitable for JSON serialization and storage in JSONB
    """
    import dataclasses
    from datetime import datetime, timezone

    model_family = detect_model_family(model) if model else "default"

    # Agent config as dict (strip internal fields and secrets)
    agent_dict = dataclasses.asdict(config)
    agent_dict.pop("_deployment_dir", None)
    # Strip API keys from LLM configs
    for key in ["api_key"]:
        agent_dict.get("llm", {}).pop(key, None)
        for phase in ["strategic", "tactical", "summarization"]:
            override = agent_dict.get("llm", {}).get(phase)
            if isinstance(override, dict):
                override.pop(key, None)

    # Resolve all prompts to full text
    prompt_resolver = PromptMatrixResolver(config._deployment_dir, model_family)
    prompts = {}
    for pt in ["systemprompt", "strategic", "tactical", "summarization"]:
        try:
            prompts[pt] = prompt_resolver.load(pt)
        except FileNotFoundError:
            prompts[pt] = None

    # Resolve all instructions to full text
    instr_resolver = InstructionMatrixResolver(config._deployment_dir, model_family)
    instructions = {}
    for it in InstructionMatrixResolver.HARDCODED_DEFAULTS:
        try:
            instructions[it] = instr_resolver.load(it)
        except FileNotFoundError:
            instructions[it] = None

    return {
        "agent": agent_dict,
        "prompts": prompts,
        "instructions": instructions,
        "model_family": model_family,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


def load_config_from_resolved(resolved: dict) -> AgentConfig:
    """Reconstruct an AgentConfig from a resolved_config JSONB snapshot.

    The returned config has pre-resolved prompt and instruction content
    stored in config.extra, so loading functions can bypass disk access.

    Args:
        resolved: Dict from resolved_config JSONB column

    Returns:
        AgentConfig with pre-resolved content in config.extra
    """
    config = load_agent_config_from_dict(resolved["agent"])
    # Store pre-resolved content for runtime use
    config.extra["_resolved_prompts"] = resolved.get("prompts", {})
    config.extra["_resolved_instructions"] = resolved.get("instructions", {})
    return config
