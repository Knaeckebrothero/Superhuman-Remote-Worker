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

from src.core.model_registry import family_of
from src.llm.reasoning_chat import ReasoningChatOpenAI

logger = logging.getLogger(__name__)

VALID_AUTONOMY_LEVELS = {"full", "review", "partial", "guided", "dependent"}


# =============================================================================
# Context-window limit fractions
# =============================================================================
# The context-management `limits` leaves are DERIVED as fixed fractions of a
# single base context-window number (see _apply_settings_matrix). The base is
# the per-model window when set (Admin → Models `context_window`, injected at
# dispatch into llm.model_max_context_tokens), else the family's
# settings.model_max_context_tokens — the model's true max. There is no separate
# conservative working-window cap; restrict a model by giving it a smaller
# per-model `context_window`. Fractions are uniform across families: the
# threshold leaves headroom below the window so the response has room to work.
# Keep these in sync with the hardcoded LimitsConfig / ContextConfig fallback
# defaults (which are a base=100_000 instance of these).
#
# Deliberately ABSENT: summarization budgets. They were derived here from the
# MAIN model's window until 2026-06-12, which sent 951k-token payloads to a
# 131k auxiliary summarizer. They are now computed at call time from the aux
# model's own window — src/core/summarizer.py,
# docs/features/context_summarization_rework.md.
CONTEXT_THRESHOLD_FRACTION = 0.80
MESSAGE_COUNT_MIN_FRACTION = 0.40


# =============================================================================
# DB-backed config overrides (CONFIG_DB_OVERRIDES_ENABLED)
# =============================================================================
# Populated once per job by the agent at first run (before
# serialize_resolved_config), then read synchronously by the resolver. Two maps,
# keyed family -> {(kind, name): value}; global (NULL-family) overrides live
# under the "" key. Text kinds (prompts, instructions) carry resolved content;
# structured kinds (settings, guardrails) carry parsed JSON values. One job per
# agent process at a time, so module-level maps are safe. When the flag is off
# (or no row matches), resolution falls through to the bundled config/ files.

_CONFIG_OVERRIDES: Dict[
    str, Dict[tuple, str]
] = {}  # text kinds: (kind, name) -> content
_VALUE_OVERRIDES: Dict[
    str, Dict[tuple, Any]
] = {}  # structured kinds: (kind, name) -> value


def _is_config_db_overrides_enabled() -> bool:
    """True when DB-backed config overrides are turned on via env."""
    return os.getenv("CONFIG_DB_OVERRIDES_ENABLED", "").lower().strip() in (
        "true",
        "1",
        "yes",
    )


def set_config_overrides(rows: List[Dict[str, Any]]) -> None:
    """Load override rows into the process maps (replaces any previous set).

    Text kinds (prompts, instructions) carry ``content``; structured kinds
    (settings, guardrails) carry ``value_json``. NULL/empty family -> "" bucket.
    """
    import json as _json

    text_map: Dict[str, Dict[tuple, str]] = {}
    value_map: Dict[str, Dict[tuple, Any]] = {}
    for row in rows:
        fam = row.get("family") or ""
        kind = row["kind"]
        if kind in ("prompts", "instructions"):
            if row.get("content") is not None:
                text_map.setdefault(fam, {})[(kind, row["name"])] = row["content"]
        elif kind in ("settings", "guardrails"):
            val = row.get("value_json")
            if isinstance(val, str):  # asyncpg JSONB w/o codec -> str
                val = _json.loads(val)
            value_map.setdefault(fam, {})[(kind, row["name"])] = val
    global _CONFIG_OVERRIDES, _VALUE_OVERRIDES
    _CONFIG_OVERRIDES = text_map
    _VALUE_OVERRIDES = value_map


def clear_config_overrides() -> None:
    """Drop all process-local overrides (used between jobs and in tests)."""
    global _CONFIG_OVERRIDES, _VALUE_OVERRIDES
    _CONFIG_OVERRIDES = {}
    _VALUE_OVERRIDES = {}


def _db_lookup(kind: str, family: str, name: str) -> Optional[str]:
    """Return an override for (kind, family, name): family-specific, then global.

    Returns None when the flag is off or no row matches, so callers fall through
    to bundled-file resolution.
    """
    if not _is_config_db_overrides_enabled():
        return None
    fam_map = _CONFIG_OVERRIDES.get(family)
    if fam_map is not None and (kind, name) in fam_map:
        return fam_map[(kind, name)]
    global_map = _CONFIG_OVERRIDES.get("")
    if global_map is not None and (kind, name) in global_map:
        return global_map[(kind, name)]
    return None


def _expand_dotted(flat: Dict[str, Any]) -> Dict[str, Any]:
    """Expand dotted keys ('limits.x') into nested dicts ({'limits': {'x': ...}})."""
    out: Dict[str, Any] = {}
    for key, val in flat.items():
        parts = key.split(".")
        node = out
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = val
    return out


def _settings_override_for(family: str) -> Dict[str, Any]:
    """DB settings override for <family> (global then family) as a nested dict
    ready to deep_merge onto file settings. {} when flag off or no rows."""
    if not _is_config_db_overrides_enabled():
        return {}

    def collect(fam: str) -> Dict[str, Any]:
        flat = {
            name: val
            for (kind, name), val in _VALUE_OVERRIDES.get(fam, {}).items()
            if kind == "settings"
        }
        return _expand_dotted(flat)

    return deep_merge(collect(""), collect(family))


def _guardrails_override_for(family: str) -> Dict[str, Any]:
    """DB guardrails override ({tool_examples, nudges}) for <family>. {} when off."""
    if not _is_config_db_overrides_enabled():
        return {}

    def collect(fam: str) -> Dict[str, Any]:
        for (kind, name), val in _VALUE_OVERRIDES.get(fam, {}).items():
            if kind == "guardrails":
                return val if isinstance(val, dict) else {}
        return {}

    return deep_merge(collect(""), collect(family))


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


# =============================================================================
# Model Config Matrix — unified prompt + instruction + settings table
# =============================================================================
#
# Top-level keys are model families; each family block carries up to three
# subsections — `prompts`, `instructions`, `settings`. The same parsed file
# powers PromptMatrixResolver (`prompts`), InstructionMatrixResolver
# (`instructions`), and the inference-param applier (`settings`). One file,
# one cache, three views — eliminates the family-list drift that the legacy
# three-file split allowed.

_model_config_matrix_cache: Dict[Path, Dict[str, Dict[str, Any]]] = {}


def _load_model_config_matrix_file(path: Path) -> Dict[str, Dict[str, Any]]:
    """Parse a single model_config_matrix.yaml file (cached by path).

    Returns ``{family: {prompts: {...}, instructions: {...}, settings: {...}}}``.
    Subsections that aren't present at a given family fall through to
    ``default`` at lookup time. Falls back to an empty dict on any read error
    so missing/optional files (e.g. an expert without overrides) don't break
    the loader.
    """
    if path in _model_config_matrix_cache:
        return _model_config_matrix_cache[path]
    if not path.exists():
        _model_config_matrix_cache[path] = {}
        return _model_config_matrix_cache[path]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.warning(
                f"Invalid model_config_matrix {path}: expected dict, got {type(data)}"
            )
            _model_config_matrix_cache[path] = {}
            return _model_config_matrix_cache[path]
        result: Dict[str, Dict[str, Any]] = {}
        for family, family_block in data.items():
            if not isinstance(family_block, dict):
                logger.warning(
                    f"model_config_matrix: skipping '{family}' (expected dict, "
                    f"got {type(family_block)})"
                )
                continue
            normalized: Dict[str, Any] = {}
            for section, payload in family_block.items():
                if section in ("prompts", "instructions", "settings", "guardrails"):
                    if isinstance(payload, dict):
                        normalized[section] = payload
                    else:
                        logger.warning(
                            f"model_config_matrix: skipping '{family}.{section}' "
                            f"(expected dict, got {type(payload)})"
                        )
                else:
                    logger.warning(
                        f"model_config_matrix: ignoring unknown section "
                        f"'{family}.{section}'"
                    )
            if normalized:
                result[family] = normalized
        _model_config_matrix_cache[path] = result
        return result
    except Exception as e:
        logger.warning(f"Failed to load {path}: {e}")
        _model_config_matrix_cache[path] = {}
        return _model_config_matrix_cache[path]


def _matrix_subsection(
    matrix: Dict[str, Dict[str, Any]], section: str
) -> Dict[str, Dict[str, Any]]:
    """Project a parsed model_config_matrix to one section as a flat
    family→entries map (the legacy single-section shape).

    A family that doesn't define ``section`` is dropped from the result rather
    than appearing as an empty dict — so the legacy resolution chain
    (`family in matrix` checks) still does the right thing without bonus keys.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for family, sections in matrix.items():
        payload = sections.get(section)
        if isinstance(payload, dict):
            out[family] = payload
    return out


def _load_settings_matrix(deployment_dir: str = None) -> Dict[str, Dict[str, Any]]:
    """Return the ``settings`` subsection of the unified matrix.

    Base file: ``config/model_config_matrix.yaml`` (cached). Optional expert
    overlay: ``<deployment_dir>/model_config_matrix.yaml`` deep-merged on top.
    Result is the same family→params shape the legacy ``settings_matrix.yaml``
    produced, so callers (`_apply_settings_matrix`, `resolve_model_settings`)
    don't change.
    """
    base_path = get_project_root() / "config" / "model_config_matrix.yaml"
    base_settings = _matrix_subsection(
        _load_model_config_matrix_file(base_path), "settings"
    )

    if not deployment_dir:
        return base_settings

    expert_path = Path(deployment_dir) / "model_config_matrix.yaml"
    if not expert_path.exists():
        return base_settings
    expert_settings = _matrix_subsection(
        _load_model_config_matrix_file(expert_path), "settings"
    )
    if not expert_settings:
        return base_settings
    return deep_merge(base_settings, expert_settings)


_guardrails_file_cache: Dict[Path, Dict[str, Any]] = {}


def _load_guardrails_file(path: Path) -> Dict[str, Any]:
    """Parse a single guardrails YAML (cached by path).

    Returns the raw dict shape: ``{tool_examples: {...}, nudges: {...}}``.
    Falls back to an empty dict on any error so missing files don't break
    the loader.
    """
    if path in _guardrails_file_cache:
        return _guardrails_file_cache[path]
    if not path.exists():
        _guardrails_file_cache[path] = {}
        return _guardrails_file_cache[path]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.warning(
                f"Invalid guardrails file {path}: expected dict, got {type(data)}"
            )
            _guardrails_file_cache[path] = {}
            return _guardrails_file_cache[path]
        _guardrails_file_cache[path] = data
        return data
    except Exception as e:
        logger.warning(f"Failed to load guardrails file {path}: {e}")
        _guardrails_file_cache[path] = {}
        return _guardrails_file_cache[path]


def _load_guardrails_matrix(
    deployment_dir: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return the ``guardrails`` subsection of the unified matrix.

    The matrix value at each family is ``{file: <basename>}``. This loader
    additionally dereferences the file pointer and returns the merged contents
    keyed by family: ``{family: {tool_examples: {...}, nudges: {...}}}``.

    Resolution per family:
      base ``config/guardrails/<file>`` (cached) plus optional expert overlay
      at ``<deployment_dir>/guardrails/<file>`` deep-merged on top.
    """
    base_path = get_project_root() / "config" / "model_config_matrix.yaml"
    base_pointers = _matrix_subsection(
        _load_model_config_matrix_file(base_path), "guardrails"
    )

    expert_pointers: Dict[str, Dict[str, Any]] = {}
    if deployment_dir:
        expert_path = Path(deployment_dir) / "model_config_matrix.yaml"
        if expert_path.exists():
            expert_pointers = _matrix_subsection(
                _load_model_config_matrix_file(expert_path), "guardrails"
            )

    families = set(base_pointers) | set(expert_pointers)
    out: Dict[str, Dict[str, Any]] = {}
    project_root = get_project_root()

    for family in families:
        base_filename = base_pointers.get(family, {}).get("file")
        merged: Dict[str, Any] = {}
        if base_filename:
            merged = _load_guardrails_file(
                project_root / "config" / "guardrails" / base_filename
            )

        expert_filename = expert_pointers.get(family, {}).get("file")
        if expert_filename and deployment_dir:
            expert_data = _load_guardrails_file(
                Path(deployment_dir) / "guardrails" / expert_filename
            )
            if expert_data:
                merged = deep_merge(merged, expert_data)

        if merged:
            out[family] = merged

    return out


def resolve_guardrails(
    model: str, deployment_dir: Optional[str] = None, *, bundled_only: bool = False
) -> Dict[str, Any]:
    """Resolve the merged guardrails dict for a model.

    Returns ``{tool_examples: {...}, nudges: {...}}`` produced by deep-merging
    the family-specific guardrails on top of the ``default`` family. Callers
    use this single dict for both tool docstring injection and runtime nudges.

    Args:
        model: Model name (e.g., ``"google/gemma-4-31b"``)
        deployment_dir: Optional expert directory for per-expert overlay

    Returns:
        Merged guardrails dict; empty if no defaults exist.
    """
    family = family_of(model)
    matrix = _load_guardrails_matrix(deployment_dir)
    default_guardrails = matrix.get("default", {})
    family_guardrails = matrix.get(family, {}) if family != "default" else {}
    merged = deep_merge(default_guardrails, family_guardrails)
    if not bundled_only:
        merged = deep_merge(merged, _guardrails_override_for(family))
    return merged


def resolve_model_settings(
    model: str, deployment_dir: str = None, *, bundled_only: bool = False
) -> Dict[str, Any]:
    """Resolve settings matrix values for a given model.

    Returns the merged default + family-specific settings (flat LLM keys only,
    no 'limits' block). Useful for configuring auxiliary or secondary LLMs
    with the correct inference parameters for their model family.

    Args:
        model: Model name (e.g., "openai/gpt-oss-120b", "gpt-4o")
        deployment_dir: Optional expert directory for per-expert matrix override

    Returns:
        Dict of inference params (temperature, top_p, top_k, model_max_context_tokens, etc.)
    """
    family = family_of(model)
    matrix = _load_settings_matrix(deployment_dir)
    default_settings = matrix.get("default", {})
    family_settings = matrix.get(family, {}) if family != "default" else {}
    settings = deep_merge(default_settings, family_settings)
    if not bundled_only:
        settings = deep_merge(settings, _settings_override_for(family))

    # Strip 'limits' — callers want LLM inference params only
    settings.pop("limits", None)
    return settings


def bundled_settings_for_family(family: str, name: str) -> Any:
    """File-resolved settings leaf for <family> (default ⊕ family), ignoring DB
    overrides. ``name`` may be a dotted path into limits (e.g.
    'limits.context_threshold_tokens'). Returns None if the leaf is absent."""
    matrix = _load_settings_matrix(None)
    default_settings = matrix.get("default", {})
    family_settings = matrix.get(family, {}) if family != "default" else {}
    settings = deep_merge(default_settings, family_settings)
    node: Any = settings
    for part in name.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def bundled_guardrails_for_family(family: str) -> Dict[str, Any]:
    """File-resolved guardrails ({tool_examples, nudges}) for <family>, ignoring
    DB overrides."""
    matrix = _load_guardrails_matrix(None)
    default_guardrails = matrix.get("default", {})
    family_guardrails = matrix.get(family, {}) if family != "default" else {}
    return deep_merge(default_guardrails, family_guardrails)


def _apply_settings_matrix(
    data: Dict[str, Any],
    expert_llm_keys: set,
    deployment_dir: str = None,
) -> Dict[str, Any]:
    """Apply settings matrix values to a merged config dict.

    Resolution: default entry → family-specific entry (deep_merge) → apply.
    Flat keys go to data["llm"] (respecting expert_llm_keys).
    Limits go to data["limits"] (matrix is sole source, no expert override check).

    Args:
        data: Merged config dict (after load_and_merge_config)
        expert_llm_keys: Set of llm keys explicitly set in the raw expert config
        deployment_dir: Optional expert directory for per-expert matrix override

    Returns:
        Modified data dict (mutated in place and returned for convenience)
    """
    llm_data = data.get("llm", {})
    model = llm_data.get("model", "gpt-4o")
    family = family_of(model)

    matrix = _load_settings_matrix(deployment_dir)
    default_settings = matrix.get("default", {})
    family_settings = matrix.get(family, {}) if family != "default" else {}
    settings = deep_merge(default_settings, family_settings)
    settings = deep_merge(settings, _settings_override_for(family))

    if not settings:
        return data

    applied = []

    # Apply flat keys -> data["llm"] (skip any "limits" — never an LLM param)
    for key, value in settings.items():
        if key == "limits":
            continue
        if key not in expert_llm_keys:
            data.setdefault("llm", {})[key] = value
            applied.append(f"llm.{key}={value}")

    # Derive the context-management `limits` leaves from a single base window:
    #   - the per-model window (Admin → Models `context_window`) when set — it is
    #     injected at dispatch into llm.model_max_context_tokens and survives the
    #     flat-key loop above because it's in expert_llm_keys;
    #   - otherwise the family's settings.model_max_context_tokens (the model's
    #     true max), which the flat-key loop just wrote into llm.
    # There is no separate conservative working-window cap — a model runs at its
    # full declared window unless an admin restricts it with a smaller per-model
    # `context_window`. Runs last so it is the sole authority for the leaves.
    base = (data.get("llm") or {}).get("model_max_context_tokens")
    if not base:  # falsy per-model override (0/None) -> fall back to family max
        base = settings.get("model_max_context_tokens")
    if base and base > 0:
        lim = data.setdefault("limits", {})
        lim["model_max_context_tokens"] = int(base)
        lim["context_threshold_tokens"] = int(base * CONTEXT_THRESHOLD_FRACTION)
        lim["message_count_min_tokens"] = int(base * MESSAGE_COUNT_MIN_FRACTION)
        applied.append(f"limits<-derived(base={int(base)})")

    if applied:
        logger.debug(f"Settings matrix ({family}): applied {', '.join(applied)}")

    return data


def apply_settings_overrides(config: "AgentConfig") -> bool:
    """Apply ONLY the DB settings override on top of an already-resolved config,
    in place. File/expert settings are already baked in by load_agent_config; this
    writes just the DB delta, so it never clobbers non-overridden values. Call at
    job start after set_config_overrides(), before LLM (re)creation and the freeze.
    Returns True if anything changed. No-op when flag off or no settings rows."""
    override = _settings_override_for(family_of(config.llm.model))
    if not override:
        return False
    changed = False
    for key, val in override.items():
        if key == "limits" and isinstance(val, dict):
            for lk, lv in val.items():
                if hasattr(config.limits, lk) and getattr(config.limits, lk) != lv:
                    setattr(config.limits, lk, lv)
                    changed = True
        elif hasattr(config.llm, key) and getattr(config.llm, key) != val:
            setattr(config.llm, key, val)
            changed = True
    return changed


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
        self.framework_dir = framework_dir or (
            get_project_root() / "config" / "prompts"
        )

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


def render_instruction_content(
    content: str,
    tool_names: List[str],
    cli_datasources: Optional[List[str]] = None,
) -> str:
    """Render Jinja2 template markers in instruction file content.

    Supports ``{% if has_tool("kb_write") %}`` conditionals,
    ``{% if cli_datasources %}`` for read-write datasource access, and
    ``{{ tools }}`` variable access.  Non-templated content (no ``{%``
    or ``{{`` markers) passes through unchanged with zero overhead.

    Args:
        content: Raw instruction file content (may contain Jinja2 markers).
        tool_names: List of actually-loaded tool names for this job.
        cli_datasources: List of datasource types with read-write CLI access
            (e.g. ``["postgresql", "neo4j"]``).  Enables
            ``{% if cli_datasources %}`` and ``has_cli_datasource("postgresql")``
            conditionals in templates.

    Returns:
        Rendered content with conditionals resolved.
    """
    if "{%" not in content and "{{" not in content:
        return content  # Fast path: no template markers

    from jinja2 import Environment

    env = Environment(keep_trailing_newline=True)
    template = env.from_string(content)
    tool_set = set(tool_names)
    ds_set = set(cli_datasources or [])
    return template.render(
        tools=tool_names,
        has_tool=lambda name: name in tool_set,
        cli_datasources=list(ds_set),
        has_cli_datasource=lambda ds_type: ds_type in ds_set,
    )


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

    Subclasses define MATRIX_SUBSECTION (``prompts`` or ``instructions``) and
    FRAMEWORK_DIR + HARDCODED_DEFAULTS for fallback. The matrix data itself
    lives in the unified ``model_config_matrix.yaml`` (one file at the project
    root, optional one per expert directory).
    """

    MATRIX_FILENAME: str = "model_config_matrix.yaml"
    MATRIX_SUBSECTION: str = "prompts"
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

        # Load matrices — share the parsed-once cache with _load_settings_matrix
        # so the unified file is only read from disk once per process.
        self._expert_matrix = self._load_matrix(self.deployment_dir)
        base_matrix_path = get_project_root() / "config" / self.MATRIX_FILENAME
        self._base_matrix = self._load_matrix_from_path(base_matrix_path)

    @classmethod
    def _load_matrix_from_path(cls, path: Path) -> Dict[str, Dict[str, str]]:
        """Load the matrix YAML and project to this resolver's subsection.

        Returns the legacy family→{type:filename} shape. Missing file or
        missing subsection both yield an empty dict so the resolution chain
        falls through cleanly.
        """
        parsed = _load_model_config_matrix_file(path)
        section = _matrix_subsection(parsed, cls.MATRIX_SUBSECTION)
        # Coerce filename values to strings (matches the legacy loader's contract).
        return {
            family: {k: str(v) for k, v in entries.items() if v is not None}
            for family, entries in section.items()
        }

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

    def load(self, entry_type: str, *, bundled_only: bool = False) -> str:
        """Resolve filename and load the content.

        When DB-backed config overrides are enabled, an override for
        ``(MATRIX_SUBSECTION, model_family, entry_type)`` is returned before any
        bundled file is read. Pass ``bundled_only=True`` to bypass overrides and
        always read the shipped ``config/`` file (used by the admin "bundled
        default" view).

        Args:
            entry_type: Type to resolve and load
            bundled_only: Skip DB overrides and read the bundled file directly

        Returns:
            File content as string
        """
        if not bundled_only:
            override = _db_lookup(self.MATRIX_SUBSECTION, self.model_family, entry_type)
            if override is not None:
                return override
        filename = self.resolve_filename(entry_type)
        return self._file_resolver.load(filename)

    def exists(self, entry_type: str) -> bool:
        """Check if a type can be resolved and the file exists."""
        filename = self.resolve_filename(entry_type)
        return self._file_resolver.exists(filename)


class PromptMatrixResolver(MatrixResolver):
    """Resolves prompt filenames through a 2D matrix: (prompt_type, model_family).

    Reads the ``prompts`` subsection of the unified model_config_matrix.yaml.
    """

    MATRIX_SUBSECTION = "prompts"
    FRAMEWORK_DIR = "config/prompts"
    HARDCODED_DEFAULTS = {
        "systemprompt": "systemprompt.txt",
        "systemprompt_interactive": "systemprompt_interactive.txt",
        "persona": "persona.txt",
        "strategic": "strategic.txt",
        "tactical": "tactical.txt",
        "summarization": "summarization_prompt.txt",
        "memory_extraction": "memory_extraction_prompt.txt",
        "curation": "curation_prompt.txt",
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

    Reads the ``instructions`` subsection of the unified model_config_matrix.yaml.
    Handles non-prompt template files: instructions, strategic todos templates,
    workspace template, and todo guide.
    """

    MATRIX_SUBSECTION = "instructions"
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
class InstructionFileEntry:
    """An instruction file with trigger conditions for auto-injection.

    Defines when and how an instruction file is delivered to the agent.

    Attributes:
        file: Workspace-relative path (e.g., "todo_guide.md")
        trigger: Trigger condition string:
            - "before_tool:<tool_name>" — fires when the named tool is called
            - "phase:strategic" / "phase:tactical" — fires on phase transition
        enforce: If True, tool rejects until agent reads the file (passive).
                 If False, system injects content automatically (active).
    """

    file: str
    trigger: str
    enforce: bool = True

    @property
    def trigger_type(self) -> str:
        """Extract trigger type: 'before_tool' or 'phase'."""
        return self.trigger.split(":")[0]

    @property
    def trigger_target(self) -> str:
        """Extract trigger target: tool name or phase name."""
        parts = self.trigger.split(":", 1)
        return parts[1] if len(parts) > 1 else ""


@dataclass
class PhaseLLMOverride:
    """Phase-specific LLM overrides.

    Only specified (non-None) fields override the base LLM config.
    Used for strategic, tactical, and summarization phase customization.
    """

    model: Optional[str] = None
    provider: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    reasoning_level: Optional[str] = None
    reasoning_method: Optional[str] = (
        None  # "prompt", "api", "none", or None (auto-detect)
    )
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout: Optional[float] = None
    max_retries: Optional[int] = None
    multimodal: Optional[bool] = None
    parallel_tool_calls: Optional[bool] = None
    max_output_tokens: Optional[int] = None
    model_max_context_tokens: Optional[int] = None


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
    provider: Optional[str] = (
        None  # "openai", "anthropic", "google", "groq", "openrouter" (auto-detect if None)
    )
    temperature: float = 0.0
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    reasoning_level: str = "high"
    reasoning_method: Optional[str] = (
        None  # "prompt", "api", "none", or None (auto-detect from model)
    )
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout: Optional[float] = 600.0  # 10 minutes default
    max_retries: int = 3
    multimodal: bool = False  # Whether model can process images directly
    parallel_tool_calls: bool = False  # Allow multiple tool calls per response
    max_output_tokens: Optional[int] = (
        None  # Override max output tokens (auto-detected if None)
    )
    model_max_context_tokens: Optional[int] = (
        None  # Per-model context window limit (falls back to limits.model_max_context_tokens)
    )

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
            provider=override.provider
            if override.provider is not None
            else self.provider,
            temperature=override.temperature
            if override.temperature is not None
            else self.temperature,
            top_p=override.top_p if override.top_p is not None else self.top_p,
            top_k=override.top_k if override.top_k is not None else self.top_k,
            reasoning_level=override.reasoning_level
            if override.reasoning_level is not None
            else self.reasoning_level,
            reasoning_method=override.reasoning_method
            if override.reasoning_method is not None
            else self.reasoning_method,
            base_url=override.base_url
            if override.base_url is not None
            else self.base_url,
            api_key=override.api_key if override.api_key is not None else self.api_key,
            timeout=override.timeout if override.timeout is not None else self.timeout,
            max_retries=override.max_retries
            if override.max_retries is not None
            else self.max_retries,
            multimodal=override.multimodal
            if override.multimodal is not None
            else self.multimodal,
            parallel_tool_calls=override.parallel_tool_calls
            if override.parallel_tool_calls is not None
            else self.parallel_tool_calls,
            max_output_tokens=override.max_output_tokens
            if override.max_output_tokens is not None
            else self.max_output_tokens,
            model_max_context_tokens=override.model_max_context_tokens
            if override.model_max_context_tokens is not None
            else self.model_max_context_tokens,
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

    _VALID_BACKENDS = ("sandbox", "vm", "virtual", "none")
    _LEGACY_BACKEND_MAP = {"remote": "sandbox", "container": "sandbox"}

    structure: List[str] = field(default_factory=list)
    instructions_template: str = ""
    initial_files: Dict[str, str] = field(default_factory=dict)
    max_read_words: int = 25000  # Maximum word count for file reads
    git_versioning: bool = True  # Enable git versioning for workspace history
    # "sandbox"/"vm" → SSH workspace container/VM (RemoteBackend); "virtual" →
    # object-store file ops, no workspace pod (VirtualWorkspaceBackend); "none"
    # → no file tools (ScratchBackend). See no_workspace_agent_mode.md §4.
    backend: str = "sandbox"
    remote: Optional[Dict[str, Any]] = (
        None  # {host, port, username, key_path, workspace_path}
    )
    # "virtual" tier only: object-store mount specs from dispatch — each a
    # {name, rclone_spec: {type, config, root}, prefix, access} (§4).
    mounts: Optional[List[Dict[str, Any]]] = None

    def __post_init__(self) -> None:
        # Backward compatibility: translate legacy backend names
        if self.backend in self._LEGACY_BACKEND_MAP:
            self.backend = self._LEGACY_BACKEND_MAP[self.backend]

        if self.backend not in self._VALID_BACKENDS:
            raise ValueError(
                f"Invalid workspace.backend={self.backend!r}. "
                f"Expected one of {self._VALID_BACKENDS} (sandbox/vm = isolated "
                f"SSH workspace; virtual = object-store file ops, no workspace "
                f"pod; none = no file tools)."
            )


@dataclass
class ToolsConfig:
    """Tools configuration by category (matches src/tools/ packages)."""

    workspace: List[str] = field(default_factory=list)
    core: List[str] = field(default_factory=list)
    research: List[str] = field(default_factory=list)
    browser_direct: List[str] = field(default_factory=list)
    citation: List[str] = field(default_factory=list)
    graph: List[str] = field(default_factory=list)
    sql: List[str] = field(default_factory=list)
    mongodb: List[str] = field(default_factory=list)
    git: List[str] = field(default_factory=list)
    shell: List[str] = field(default_factory=list)
    evaluation: List[str] = field(default_factory=list)
    knowledge: List[str] = field(default_factory=list)
    webdav: List[str] = field(default_factory=list)
    communication: List[str] = field(default_factory=list)
    delegation: List[str] = field(default_factory=list)
    orchestrator: List[str] = field(default_factory=list)


@dataclass
class ConnectionsConfig:
    """Database connections configuration."""

    postgres: bool = True


@dataclass
class ResponseValidationConfig:
    """LLM response degeneration validation settings."""

    enabled: bool = True
    max_content_length: int = 50000
    max_tag_repetitions: int = 50
    max_token_repetitions: int = 20
    max_line_repetitions: int = 10


@dataclass
class LimitsConfig:
    """Execution limits configuration."""

    # Derived-leaf fallbacks: a base=100_000 instance of the limit fractions
    # (see CONTEXT_THRESHOLD_FRACTION et al.). Real values come from the matrix
    # derivation; these only fire when a key is wholly absent (test/edge paths).
    context_threshold_tokens: int = 80000  # 100_000 * 0.80
    message_count_threshold: int = 200
    message_count_min_tokens: int = 40000  # 100_000 * 0.40
    tool_retry_count: int = 3
    model_max_context_tokens: int = 100000
    response_validation: ResponseValidationConfig = field(
        default_factory=ResponseValidationConfig
    )
    progress_stall_threshold: int = (
        30  # tool calls without progress before nudge reminder
    )
    max_tool_calls_per_phase: int = (
        200  # max tool calls per phase before rewind (tactical) or freeze (strategic)
    )


@dataclass
class ContextManagementConfig:
    """Context management configuration."""

    compact_on_archive: bool = True
    keep_recent_tool_results: int = 15
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
class MemoryPipelineConfig:
    """Named plugins the MemoryManager binds per stage (memory.pipeline).

    Names resolve against MEMORY_PLUGIN_REGISTRY
    (src/services/memory/registry.py); an unknown name fails loudly at
    bind time. Empty lists bind a no-op manager. Defaults stay empty
    until the Phase-1 transplant registers the current-behaviour plugins
    (docs/features/agent_memory_overhaul.md §5/§6).
    """

    retrievers: List[str] = field(default_factory=list)
    scorers: List[str] = field(default_factory=list)
    policies: List[str] = field(default_factory=list)
    writers: List[str] = field(default_factory=list)
    extensions: List[str] = field(default_factory=list)


@dataclass
class RerankerConfig:
    """memory.reranker — options for the 'reranker' scorer (overhaul Phase 3).

    Only consulted when ``reranker`` appears in ``memory.pipeline.scorers``.
    ``base_url``/``api_key`` default to the auxiliary endpoint at bind time
    (the same router serves the ``/rerank`` route), so production needs no
    extra credential plumbing — the reranker rides the auxiliary key the
    orchestrator injects at dispatch.
    """

    model: str = "qwen3-reranker-8b"
    base_url: Optional[str] = None  # null = auxiliary.base_url
    api_key: Optional[str] = None  # null = auxiliary.api_key
    top_k: int = 64  # rerank at most this many candidates per assemble
    timeout: float = 10.0  # seconds per rerank call
    # Keep TTL-pinned items (the recency working set) ahead of the
    # reranked tail — Phase 3's bounded-core policy revisits pinning
    # itself; the scorer doesn't change tier semantics.
    keep_pinned_first: bool = True


@dataclass
class BoundedConfig:
    """memory.bounded — options for the 'bounded' injection policy.

    Only consulted when ``bounded`` appears in ``memory.pipeline.policies``.
    Caps the memory-kind items of the assembled payload AFTER scorers run.
    ``memory.budget_tokens`` trims inside the retriever — in legacy hybrid
    order, before a reranker can surface the evidence — so post-scorer
    bounding has to live in the policy stage. At least one cap must be set
    for the policy to bind.
    """

    max_items: Optional[int] = None  # keep at most N memory items
    max_tokens: Optional[int] = None  # keep memory items within this budget
    # B5: count knowledge-kind items against max_tokens too — one token
    # budget across the memory + KB blocks (the legacy KB block is uncapped
    # on every call). max_items stays a memory-count cap; requires
    # max_tokens to be set.
    include_knowledge: bool = False


@dataclass
class GateConfig:
    """memory.gate — options for the 'gate' injection policy (P4).

    Only consulted when ``gate`` appears in ``memory.pipeline.policies``.
    Drops memory items whose score on ``channel`` falls below the floor:
    ``threshold`` itself (mode "absolute") or ``threshold × the
    assemble's top score`` (mode "relative" — the measured
    recommendation: qwen3-reranker's absolute scale varies by orders of
    magnitude per query while evidence/distractor separation stays
    strong, so absolute cutoffs delete weakly-phrased evidence). Items
    the channel never scored (scorer outage, candidates past the
    reranker's top_k, a pinned head under keep_pinned_first) pass
    through ungated, so a failed scorer degrades to the legacy full dump
    rather than an empty injection. ``threshold`` must be set for the
    policy to bind.
    """

    threshold: Optional[float] = None
    channel: str = "rerank"  # channel_scores key the gate reads
    mode: str = "absolute"  # absolute | relative (floor = threshold × top)


@dataclass
class IngestionConfig:
    """memory.ingestion — write-path ingestion verdicts + bi-temporal supersede
    (overhaul Phase 4, docs/features/agent_memory_overhaul.md §5).

    When ``enabled``, ``RecallStore.store()`` replaces the lossy cosine-0.85
    dedup-merge with an aux-LLM adjudication: a new candidate is compared
    against its top-``verdict_top_k`` currently-valid neighbours and the LLM
    returns ADD / UPDATE / MERGE / NOOP. UPDATE and MERGE retire the
    superseded rows (set ``valid_to``/``superseded_at``/``superseded_by``) so
    default retrieval stops serving them — the washing-machine fix (P3).

    Cost guard ("bound verdict calls per write"): the LLM is consulted only
    when a neighbour scores at/above ``review_floor`` similarity. A genuinely
    new fact (no near-duplicate) is a straight ADD with zero LLM calls, so
    verdict calls are bounded to roughly the near-duplicate rate — at most one
    per stored memory. Default off: it changes what the store keeps, so it is
    a measured opt-in and ships inert until the harness/soak greenlights it.
    """

    enabled: bool = False
    verdict_top_k: int = 5  # neighbours shown to the adjudicator
    review_floor: float = 0.6  # min cosine similarity that triggers a verdict call


@dataclass
class ExtractionConfig:
    """memory.extraction — write-path extraction policy (overhaul Phase 4).

    ``write_gate`` keeps the legacy write-time importance floor
    (``importance < importance_threshold`` → skip). Phase 4 sets it False to
    follow completeness-over-precision (§4 writers): a fact skipped at write
    time is unrecoverable, and relevance is now gated at *retrieval* (the
    reranker + gate), so the write-time floor is redundant. Default True —
    dropping it is a measured opt-in. Boundary-driven extraction (phase-end,
    session-end, idle) is already always-on via the registered
    phase_boundary / teardown writers with the interval extractor as the
    turn-count fallback, so it needs no separate trigger knob here.
    """

    write_gate: bool = True


@dataclass
class QueryConfig:
    """memory.query — retrieval query formation (overhaul §4).

    ``digest`` swaps the legacy per-mode query texts (worker: top todo +
    phase descriptor; persistent: last user message) for the unified
    request digest — a recent message window plus the task frame. Default
    off: it changes what gets embedded/reranked, so it stays a measured
    opt-in (agent_memory_overhaul.md Phase 3 slice 4).
    """

    digest: bool = False
    digest_window: int = 4  # trailing Human/AI messages in the digest
    digest_max_chars_per_message: int = 500


@dataclass
class MemoryConfig:
    """Memory Light (RecallStore) configuration.

    Controls the memory subsystem that stores and retrieves memories
    from PostgreSQL with hybrid search (dense vector + sparse keyword + recency).
    See docs/features/memory_light.md for full architecture.
    """

    enabled: bool = False
    budget_tokens: int = 10000
    max_memories_per_injection: int = 150
    observer_interval: int = 5
    assembler_interval: int = 7
    default_ttl: int = 10
    importance_threshold: float = 0.3
    dedup_threshold: float = 0.85
    retrieval_importance_floor: float = 0.4
    project_scoped: bool = True
    # MemoryManager seam (memory overhaul Phase 1). manager_enabled is the
    # cutover guard (memory.manager.enabled): while False the graphs keep
    # their legacy direct-store paths and the manager is never constructed.
    manager_enabled: bool = False
    pipeline: MemoryPipelineConfig = field(default_factory=MemoryPipelineConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    bounded: BoundedConfig = field(default_factory=BoundedConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    ingestion: IngestionConfig = field(default_factory=IngestionConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)


@dataclass
class AuxiliaryTaskConfig:
    """Per-task configuration overrides for auxiliary tasks."""

    enabled: bool = True


@dataclass
class AuxiliaryConfig:
    """Auxiliary LLM configuration for unified support tasks.

    Controls the AuxiliaryLLM class that handles background tasks like
    memory extraction and knowledge curation using structured output.
    See docs/features/auxiliary.md for full design.
    """

    enabled: bool = True
    model: Optional[str] = None  # null = use main LLM
    base_url: Optional[str] = None  # null = use main LLM endpoint
    api_key: Optional[str] = None  # null = use provider env var
    temperature: float = 0.0
    max_iterations: int = 15  # Cap for agent mode loops
    timeout: float = 120.0  # Seconds per LLM call (quick interactive tasks)
    # Per fold-call timeout for conversation summarization. Each summarization
    # pass is one bounded call (src/core/summarizer.py); a hung aux endpoint
    # costs at most this much per attempt, not a single shared 600s blob.
    summarization_call_timeout: float = 240.0
    tasks: Dict[str, AuxiliaryTaskConfig] = field(
        default_factory=lambda: {
            "extract_memories": AuxiliaryTaskConfig(enabled=True),
            "curate_knowledge": AuxiliaryTaskConfig(enabled=True),
            "assemble_memories": AuxiliaryTaskConfig(enabled=True),
        }
    )


@dataclass
class InteractiveConfig:
    """Configuration for persistent interactive mode.

    Only used when the agent is started with --mode persistent.
    Controls permission defaults and idle behavior.
    """

    permission_mode: str = "supervised"  # supervised | auto_accept | autonomous
    narration_mode: str = "auto"  # silent | verbose | auto
    idle_timeout_minutes: int = 30  # 0 = disabled
    greeting: str = "Hello! I'm ready to help. What would you like to work on?"


@dataclass
class HeadlessConfig:
    """Headless / untethered behavior for persistent sessions.

    Sourced from users.settings.persistent_agent, optionally overridden per
    thread via threads.metadata.config_override.headless.
    """

    mode: str = "eager"  # eager | polite
    attention_sleep_minutes: int = 60  # 0 disables the watchdog
    notification_channels: List[str] = field(default_factory=lambda: ["email"])


@dataclass
class DelegationConfig:
    """Subagent delegation configuration.

    Controls whether agents can spawn child jobs via the delegate_work tool.
    Children branch off the parent workspace and work in parallel via git worktrees.
    See docs/features/subagent_delegation.md.

    Depth is computed by counting delegation links (creation_order IS NOT NULL)
    in the ancestor chain.  Lifecycle links (scholar/critic with creation_order
    IS NULL) do not increment depth.  A job can delegate when its delegation
    depth is strictly less than max_depth.
    """

    enabled: bool = False
    max_depth: int = 1  # Max delegation nesting; only delegation links count (lifecycle links are depth-transparent)
    default_timeout: int = 7200  # 2 hours
    max_timeout: int = 14400  # 4 hours
    allowed_configs: List[str] = field(default_factory=list)  # empty = any


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
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    auxiliary: AuxiliaryConfig = field(default_factory=AuxiliaryConfig)
    instruction_files: List[InstructionFileEntry] = field(default_factory=list)
    delegation: DelegationConfig = field(default_factory=DelegationConfig)
    interactive: InteractiveConfig = field(default_factory=InteractiveConfig)
    headless: HeadlessConfig = field(default_factory=HeadlessConfig)
    autonomy: str = "partial"

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
        top_p=data.get("top_p"),
        top_k=data.get("top_k"),
        reasoning_level=data.get("reasoning_level"),
        reasoning_method=data.get("reasoning_method"),
        base_url=data.get("base_url"),
        api_key=data.get("api_key"),
        timeout=data.get("timeout"),
        max_retries=data.get("max_retries"),
        multimodal=data.get("multimodal"),
        parallel_tool_calls=data.get("parallel_tool_calls"),
        max_output_tokens=data.get("max_output_tokens"),
        model_max_context_tokens=data.get("model_max_context_tokens"),
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
        top_p=llm_data.get("top_p"),
        top_k=llm_data.get("top_k"),
        reasoning_level=llm_data.get("reasoning_level", "high"),
        reasoning_method=llm_data.get("reasoning_method"),
        base_url=llm_data.get("base_url"),
        api_key=llm_data.get("api_key"),
        timeout=llm_data.get("timeout", 600.0),
        max_retries=llm_data.get("max_retries", 3),
        multimodal=llm_data.get("multimodal", False),
        parallel_tool_calls=llm_data.get("parallel_tool_calls", False),
        max_output_tokens=llm_data.get("max_output_tokens"),
        model_max_context_tokens=llm_data.get("model_max_context_tokens"),
        # Phase-specific overrides
        strategic=_parse_phase_override(llm_data.get("strategic")),
        tactical=_parse_phase_override(llm_data.get("tactical")),
        summarization=_parse_phase_override(llm_data.get("summarization")),
    )


def _parse_memory_config(data: Dict[str, Any]) -> MemoryConfig:
    """Parse memory configuration from dict.

    Args:
        data: Memory config dictionary from YAML

    Returns:
        MemoryConfig dataclass
    """
    manager_data = data.get("manager", {}) or {}
    if not isinstance(manager_data, dict):
        # Bool shorthand (`manager: true`), same tolerance as auxiliary tasks
        manager_data = {"enabled": bool(manager_data)}
    pipeline_data = data.get("pipeline", {}) or {}
    pipeline = MemoryPipelineConfig(
        retrievers=list(pipeline_data.get("retrievers", []) or []),
        scorers=list(pipeline_data.get("scorers", []) or []),
        policies=list(pipeline_data.get("policies", []) or []),
        writers=list(pipeline_data.get("writers", []) or []),
        extensions=list(pipeline_data.get("extensions", []) or []),
    )
    reranker_data = data.get("reranker", {}) or {}
    reranker = RerankerConfig(
        model=reranker_data.get("model", "qwen3-reranker-8b"),
        base_url=reranker_data.get("base_url"),
        api_key=reranker_data.get("api_key"),
        top_k=int(reranker_data.get("top_k", 64)),
        timeout=float(reranker_data.get("timeout", 10.0)),
        keep_pinned_first=bool(reranker_data.get("keep_pinned_first", True)),
    )
    bounded_data = data.get("bounded", {}) or {}
    bounded = BoundedConfig(
        max_items=(
            int(bounded_data["max_items"])
            if bounded_data.get("max_items") is not None
            else None
        ),
        max_tokens=(
            int(bounded_data["max_tokens"])
            if bounded_data.get("max_tokens") is not None
            else None
        ),
        include_knowledge=bool(bounded_data.get("include_knowledge", False)),
    )
    gate_data = data.get("gate", {}) or {}
    gate = GateConfig(
        threshold=(
            float(gate_data["threshold"])
            if gate_data.get("threshold") is not None
            else None
        ),
        channel=str(gate_data.get("channel", "rerank")),
        mode=str(gate_data.get("mode", "absolute")),
    )
    query_data = data.get("query", {}) or {}
    query = QueryConfig(
        digest=bool(query_data.get("digest", False)),
        digest_window=int(query_data.get("digest_window", 4)),
        digest_max_chars_per_message=int(
            query_data.get("digest_max_chars_per_message", 500)
        ),
    )
    ingestion_data = data.get("ingestion", {}) or {}
    if not isinstance(ingestion_data, dict):
        # Bool shorthand (`ingestion: true`), same tolerance as manager/tasks.
        ingestion_data = {"enabled": bool(ingestion_data)}
    ingestion = IngestionConfig(
        enabled=bool(ingestion_data.get("enabled", False)),
        verdict_top_k=int(ingestion_data.get("verdict_top_k", 5)),
        review_floor=float(ingestion_data.get("review_floor", 0.6)),
    )
    extraction_data = data.get("extraction", {}) or {}
    extraction = ExtractionConfig(
        write_gate=bool(extraction_data.get("write_gate", True)),
    )
    return MemoryConfig(
        enabled=data.get("enabled", False),
        budget_tokens=data.get("budget_tokens", 10000),
        max_memories_per_injection=data.get("max_memories_per_injection", 150),
        observer_interval=data.get("observer_interval", 5),
        assembler_interval=data.get("assembler_interval", 7),
        default_ttl=data.get("default_ttl", 10),
        importance_threshold=data.get("importance_threshold", 0.3),
        dedup_threshold=data.get("dedup_threshold", 0.85),
        retrieval_importance_floor=data.get("retrieval_importance_floor", 0.4),
        project_scoped=data.get("project_scoped", True),
        # Accept both shapes: the YAML nesting (`manager.enabled`) and the
        # flat dataclass field (`manager_enabled`) that dataclasses.asdict()
        # emits when dispatch paths round-trip a live config through
        # deep_merge + re-parse (job config_override, session config
        # assembly, config.update). Without the fallback the cutover flag
        # silently resets to False on every dispatched job/session.
        manager_enabled=bool(
            manager_data.get("enabled", data.get("manager_enabled", False))
        ),
        pipeline=pipeline,
        reranker=reranker,
        bounded=bounded,
        gate=gate,
        query=query,
        ingestion=ingestion,
        extraction=extraction,
    )


def _parse_auxiliary_config(data: Dict[str, Any]) -> AuxiliaryConfig:
    """Parse auxiliary LLM configuration from dict.

    Args:
        data: Auxiliary config dictionary from YAML

    Returns:
        AuxiliaryConfig dataclass
    """
    tasks_data = data.get("tasks", {})
    tasks = {}
    for task_name, task_conf in tasks_data.items():
        if isinstance(task_conf, dict):
            tasks[task_name] = AuxiliaryTaskConfig(
                enabled=task_conf.get("enabled", True),
            )
        else:
            tasks[task_name] = AuxiliaryTaskConfig(enabled=bool(task_conf))

    # Ensure defaults for known tasks
    for default_task in ("extract_memories", "curate_knowledge", "assemble_memories"):
        if default_task not in tasks:
            tasks[default_task] = AuxiliaryTaskConfig(enabled=True)

    return AuxiliaryConfig(
        enabled=data.get("enabled", True),
        model=data.get("model"),
        base_url=data.get("base_url"),
        api_key=data.get("api_key"),
        temperature=data.get("temperature", 0.0),
        max_iterations=data.get("max_iterations", 15),
        timeout=data.get("timeout", 120.0),
        summarization_call_timeout=data.get("summarization_call_timeout", 240.0),
        tasks=tasks,
    )


def _parse_response_validation(data: Dict[str, Any]) -> ResponseValidationConfig:
    """Parse response validation configuration from dict."""
    if not data:
        return ResponseValidationConfig()
    return ResponseValidationConfig(
        enabled=data.get("enabled", True),
        max_content_length=data.get("max_content_length", 50000),
        max_tag_repetitions=data.get("max_tag_repetitions", 50),
        max_token_repetitions=data.get("max_token_repetitions", 20),
        max_line_repetitions=data.get("max_line_repetitions", 10),
    )


def load_agent_config(
    config_path: str, deployment_dir: Optional[str] = None
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

    # Apply settings matrix: model-family defaults between defaults.yaml and expert config.
    # Read the raw expert file to know which llm keys were explicitly set.
    raw_expert_llm_keys = set()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw_expert = yaml.safe_load(f) or {}
        raw_expert_llm_keys = set((raw_expert.get("llm") or {}).keys())
    except Exception:
        pass
    _apply_settings_matrix(data, raw_expert_llm_keys, deployment_dir)

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
        backend=workspace_data.get("backend", "sandbox"),
        remote=workspace_data.get("remote"),
        mounts=workspace_data.get("mounts"),
    )

    tools_data = data.get("tools", {})
    tools_config = ToolsConfig(
        workspace=tools_data.get("workspace", []),
        core=tools_data.get("core", []),
        research=tools_data.get("research", []),
        browser_direct=tools_data.get("browser_direct", []),
        citation=tools_data.get("citation", []),
        graph=tools_data.get("graph", []),
        sql=tools_data.get("sql", []),
        mongodb=tools_data.get("mongodb", []),
        git=tools_data.get("git", []),
        shell=tools_data.get("shell", tools_data.get("coding", [])),
        evaluation=tools_data.get("evaluation", []),
        knowledge=tools_data.get("knowledge", []),
        webdav=tools_data.get("webdav", []),
        delegation=tools_data.get("delegation", []),
        orchestrator=tools_data.get("orchestrator", []),
    )

    connections_data = data.get("connections", {})
    connections_config = ConnectionsConfig(
        postgres=connections_data.get("postgres", True),
    )

    limits_data = data.get("limits", {})
    limits_config = LimitsConfig(
        context_threshold_tokens=limits_data.get("context_threshold_tokens", 80000),
        message_count_threshold=limits_data.get("message_count_threshold", 200),
        message_count_min_tokens=limits_data.get("message_count_min_tokens", 40000),
        tool_retry_count=limits_data.get("tool_retry_count", 3),
        model_max_context_tokens=limits_data.get("model_max_context_tokens", 100000),
        response_validation=_parse_response_validation(
            limits_data.get("response_validation", {})
        ),
        progress_stall_threshold=limits_data.get("progress_stall_threshold", 30),
        max_tool_calls_per_phase=limits_data.get("max_tool_calls_per_phase", 200),
    )

    context_data = data.get("context_management", {})
    context_config = ContextManagementConfig(
        compact_on_archive=context_data.get("compact_on_archive", True),
        keep_recent_tool_results=context_data.get("keep_recent_tool_results", 15),
        keep_recent_messages=context_data.get("keep_recent_messages", 10),
        summarization_template=context_data.get(
            "summarization_template", "summarization_prompt.txt"
        ),
        reasoning_level=context_data.get("reasoning_level", "high"),
        max_summary_length=context_data.get("max_summary_length", 10000),
    )

    phase_data = data.get("phase_settings", {})
    phase_config = PhaseSettings(
        min_todos=phase_data.get("min_todos", 5),
        max_todos=phase_data.get("max_todos", 20),
    )

    memory_data = data.get("memory", {})
    memory_config = _parse_memory_config(memory_data)

    auxiliary_data = data.get("auxiliary", {})
    auxiliary_config = _parse_auxiliary_config(auxiliary_data)

    # Parse instruction_files entries
    instruction_files_data = data.get("instruction_files", [])
    instruction_files = [
        InstructionFileEntry(
            file=entry["file"],
            trigger=entry["trigger"],
            enforce=entry.get("enforce", True),
        )
        for entry in instruction_files_data
    ]

    # Parse delegation config
    delegation_data = data.get("delegation", {})
    delegation_config = DelegationConfig(
        enabled=delegation_data.get("enabled", False),
        max_depth=delegation_data.get("max_depth", 1),
        default_timeout=delegation_data.get("default_timeout", 7200),
        max_timeout=delegation_data.get("max_timeout", 14400),
        allowed_configs=delegation_data.get("allowed_configs", []),
    )

    # Parse autonomy level
    autonomy = data.get("autonomy", "partial")
    if autonomy not in VALID_AUTONOMY_LEVELS:
        logger.warning(f"Invalid autonomy level '{autonomy}', defaulting to 'partial'")
        autonomy = "partial"

    # Collect extra fields (agent-specific config)
    known_fields = {
        "$schema",
        "agent_id",
        "display_name",
        "description",
        "llm",
        "workspace",
        "tools",
        "connections",
        "polling",
        "limits",
        "context_management",
        "phase_settings",
        "memory",
        "auxiliary",
        "instruction_files",
        "delegation",
        "interactive",
        "headless",
        "autonomy",
    }
    extra = {k: v for k, v in data.items() if k not in known_fields}

    # Parse interactive config
    interactive_data = data.get("interactive", {})
    interactive_config = InteractiveConfig(
        permission_mode=interactive_data.get("permission_mode", "supervised"),
        idle_timeout_minutes=interactive_data.get("idle_timeout_minutes", 30),
    )

    # Parse headless config (Phase 6 — polite mode + per-thread attention-sleep)
    headless_data = data.get("headless") or {}
    headless_config = HeadlessConfig(
        mode=headless_data.get("mode") or "eager",
        attention_sleep_minutes=int(headless_data.get("attention_sleep_minutes") or 60),
        notification_channels=list(
            headless_data.get("notification_channels") or ["email"]
        ),
    )

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
        memory=memory_config,
        auxiliary=auxiliary_config,
        instruction_files=instruction_files,
        delegation=delegation_config,
        interactive=interactive_config,
        headless=headless_config,
        autonomy=autonomy,
        extra=extra,
        _deployment_dir=deployment_dir,
    )


def load_agent_config_from_dict(
    data: Dict[str, Any], deployment_dir: Optional[str] = None
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
        backend=workspace_data.get("backend", "sandbox"),
        remote=workspace_data.get("remote"),
        mounts=workspace_data.get("mounts"),
    )

    tools_data = data.get("tools", {})
    tools_config = ToolsConfig(
        workspace=tools_data.get("workspace", []),
        core=tools_data.get("core", []),
        research=tools_data.get("research", []),
        browser_direct=tools_data.get("browser_direct", []),
        citation=tools_data.get("citation", []),
        graph=tools_data.get("graph", []),
        sql=tools_data.get("sql", []),
        mongodb=tools_data.get("mongodb", []),
        git=tools_data.get("git", []),
        shell=tools_data.get("shell", tools_data.get("coding", [])),
        evaluation=tools_data.get("evaluation", []),
        knowledge=tools_data.get("knowledge", []),
        webdav=tools_data.get("webdav", []),
        delegation=tools_data.get("delegation", []),
        orchestrator=tools_data.get("orchestrator", []),
    )

    connections_data = data.get("connections", {})
    connections_config = ConnectionsConfig(
        postgres=connections_data.get("postgres", True),
    )

    limits_data = data.get("limits", {})
    limits_config = LimitsConfig(
        context_threshold_tokens=limits_data.get("context_threshold_tokens", 80000),
        message_count_threshold=limits_data.get("message_count_threshold", 200),
        message_count_min_tokens=limits_data.get("message_count_min_tokens", 40000),
        tool_retry_count=limits_data.get("tool_retry_count", 3),
        model_max_context_tokens=limits_data.get("model_max_context_tokens", 100000),
        response_validation=_parse_response_validation(
            limits_data.get("response_validation", {})
        ),
        progress_stall_threshold=limits_data.get("progress_stall_threshold", 30),
        max_tool_calls_per_phase=limits_data.get("max_tool_calls_per_phase", 200),
    )

    context_data = data.get("context_management", {})
    context_config = ContextManagementConfig(
        compact_on_archive=context_data.get("compact_on_archive", True),
        keep_recent_tool_results=context_data.get("keep_recent_tool_results", 15),
        keep_recent_messages=context_data.get("keep_recent_messages", 10),
        summarization_template=context_data.get(
            "summarization_template", "summarization_prompt.txt"
        ),
        reasoning_level=context_data.get("reasoning_level", "high"),
        max_summary_length=context_data.get("max_summary_length", 10000),
    )

    phase_data = data.get("phase_settings", {})
    phase_config = PhaseSettings(
        min_todos=phase_data.get("min_todos", 5),
        max_todos=phase_data.get("max_todos", 20),
    )

    memory_data = data.get("memory", {})
    memory_config = _parse_memory_config(memory_data)

    auxiliary_data = data.get("auxiliary", {})
    auxiliary_config = _parse_auxiliary_config(auxiliary_data)

    # Parse instruction_files entries
    instruction_files_data = data.get("instruction_files", [])
    instruction_files = [
        InstructionFileEntry(
            file=entry["file"],
            trigger=entry["trigger"],
            enforce=entry.get("enforce", True),
        )
        for entry in instruction_files_data
    ]

    # Parse delegation config
    delegation_data = data.get("delegation", {})
    delegation_config = DelegationConfig(
        enabled=delegation_data.get("enabled", False),
        max_depth=delegation_data.get("max_depth", 1),
        default_timeout=delegation_data.get("default_timeout", 7200),
        max_timeout=delegation_data.get("max_timeout", 14400),
        allowed_configs=delegation_data.get("allowed_configs", []),
    )

    # Parse autonomy level
    autonomy = data.get("autonomy", "partial")
    if autonomy not in VALID_AUTONOMY_LEVELS:
        logger.warning(f"Invalid autonomy level '{autonomy}', defaulting to 'partial'")
        autonomy = "partial"

    # Collect extra fields
    known_fields = {
        "$schema",
        "agent_id",
        "display_name",
        "description",
        "llm",
        "workspace",
        "tools",
        "connections",
        "polling",
        "limits",
        "context_management",
        "phase_settings",
        "memory",
        "auxiliary",
        "instruction_files",
        "delegation",
        "interactive",
        "headless",
        "autonomy",
    }
    extra = {k: v for k, v in data.items() if k not in known_fields}

    # Parse interactive config
    interactive_data = data.get("interactive", {})
    interactive_config = InteractiveConfig(
        permission_mode=interactive_data.get("permission_mode", "supervised"),
        idle_timeout_minutes=interactive_data.get("idle_timeout_minutes", 30),
    )

    # Parse headless config (Phase 6 — polite mode + per-thread attention-sleep)
    headless_data = data.get("headless") or {}
    headless_config = HeadlessConfig(
        mode=headless_data.get("mode") or "eager",
        attention_sleep_minutes=int(headless_data.get("attention_sleep_minutes") or 60),
        notification_channels=list(
            headless_data.get("notification_channels") or ["email"]
        ),
    )

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
        memory=memory_config,
        auxiliary=auxiliary_config,
        instruction_files=instruction_files,
        delegation=delegation_config,
        interactive=interactive_config,
        headless=headless_config,
        autonomy=autonomy,
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

    # Apply settings matrix: uploaded llm keys are the explicit overrides
    uploaded_llm_keys = set((uploaded_data.get("llm") or {}).keys())
    _apply_settings_matrix(merged, uploaded_llm_keys)

    logger.info(
        f"Merged uploaded config with defaults: "
        f"agent_id={merged.get('agent_id')}, "
        f"overrides={list(uploaded_data.keys())}"
    )

    return merged


def detect_reasoning_method(model: str, explicit_method: Optional[str] = None) -> str:
    """Determine how reasoning level is delivered to the model.

    - "prompt": Inject `Reasoning: {level}` in system prompt (gpt-oss via vLLM)
    - "api": Pass as API parameter (OpenAI, OpenRouter native models)
    - "none": Model doesn't support reasoning level control (Anthropic, Google, Groq)

    Args:
        model: Model name for family detection
        explicit_method: Explicit override from config (skips auto-detection)

    Returns:
        One of "prompt", "api", or "none"
    """
    if explicit_method:
        return explicit_method

    family = family_of(model)

    if family == "gpt-oss":
        return "prompt"
    if family in (
        "claude-opus",
        "claude-sonnet",
        "claude-haiku",
        "gemini",
        "minimax",
        "minimax-m3",
        "gemma",
    ):
        return "none"
    # gpt-5, gpt-4o, o-series, deepseek, qwen, llama, default
    return "api"


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


# Reasoning levels supported by each provider API
_OPENAI_REASONING_LEVELS = {"low", "medium", "high"}


def _clamp_reasoning_level(level: str, supported: set[str]) -> str:
    """Clamp a reasoning level to the nearest supported value.

    Maps unsupported levels to the closest supported equivalent:
    - 'minimal' -> 'low'
    - 'xhigh' -> 'high'
    """
    if level in supported:
        return level
    mapping = {"minimal": "low", "xhigh": "high"}
    clamped = mapping.get(level, level)
    if clamped not in supported:
        return "high"  # safe fallback
    logger.debug(f"Clamped reasoning level '{level}' -> '{clamped}' for provider")
    return clamped


def supports_parallel_tool_calls(provider: Optional[str], model: Optional[str]) -> bool:
    """Whether the bind-time ``parallel_tool_calls`` kwarg may be passed.

    ``parallel_tool_calls`` is an OpenAI Chat Completions parameter. It must NOT
    be forwarded to providers/models that reject unknown fields:

    - **Google**: ``langchain_google_genai`` threads the kwarg into the GenAI
      SDK's ``GenerateContentConfig``, a strict Pydantic model
      (``model_config = {"extra": "forbid"}``). Passing it raises
      ``1 validation error for GenerateContentConfig / parallel_tool_calls /
      Extra inputs are not permitted``.
    - **OpenAI o-series reasoning models** (``o1``/``o3``/``o4``) don't accept
      the parameter.

    OpenAI-compatible providers (openai, openrouter, codex, groq) and Anthropic
    accept it, so it is only suppressed for the cases above.
    """
    provider = (provider or "").lower()
    model = (model or "").lower()
    if provider == "google":
        return False
    if model.startswith(("o1", "o3", "o4")):
        return False
    return True


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
    # Resolution: orchestrator dispatcher injects ``provider`` from the
    # catalog row (system_api_keys provider slug or ``openai`` for endpoint-
    # backed rows). Default to ``openai`` when missing — covers native
    # OpenAI models and anything routed through an OpenAI-compatible
    # endpoint, which is every endpoint case post-chunk-6 (the legacy
    # YAML fallback that distinguished ``anthropic``/``google``/``groq``
    # native is gone; the dispatcher now sets ``provider`` explicitly).
    provider = config.provider.lower() if config.provider else "openai"

    if provider == "anthropic":
        return _create_anthropic_llm(config, limits)
    elif provider == "google":
        return _create_google_llm(config, limits)
    elif provider == "groq":
        return _create_groq_llm(config, limits)
    elif provider == "openrouter":
        return _create_openrouter_llm(config, limits)
    elif provider == "codex":
        return _create_codex_llm(config, limits)
    else:
        return _create_openai_llm(config, limits)


def _resolve_max_output_tokens(
    config: LLMConfig,
    limits: Optional[LimitsConfig] = None,
) -> int:
    """Resolve the output token cap for non-Anthropic providers.

    Mirrors the safety pattern in `_create_anthropic_llm`: when the user
    hasn't set `max_output_tokens` explicitly, derive a sensible cap from
    the model's declared context window. Without this, vLLM/llama.cpp
    style endpoints fall back to their server-side default (effectively
    unbounded for most local servers), and a single runaway generation
    (e.g. the known gemma4 + xgrammar repetition loop, vllm#40080) can
    emit millions of tokens of repeated content and poison the next turn.

    Resolution order:
      1. Explicit ``config.max_output_tokens`` (user / per-job override)
      2. ``min(16384, ctx // 4)`` when the context window is known
      3. ``8192`` as last resort
    """
    if config.max_output_tokens is not None:
        return config.max_output_tokens
    ctx = config.model_max_context_tokens or (
        limits.model_max_context_tokens if limits else None
    )
    if ctx:
        return min(16384, ctx // 4)
    return 8192


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
    key_ring = get_or_create_key_ring(
        keys, provider="openai", cooldown_seconds=cooldown
    )

    # SDK gets the first key; KeyRing overrides the header in send()
    api_key = keys[0]

    # Base URL: dispatcher-injected for endpoint-backed catalog rows,
    # custom user endpoints, and system endpoints. Native OpenAI models
    # leave it None and the SDK uses api.openai.com.
    #
    # The legacy YAML fallback (LLM_BASE_URL inheritance via
    # `_load_builtin_catalog`'s Local-group branch) was removed in chunk 6
    # of the models_yaml_removal work. A self-hosted model now MUST have
    # a catalog row pointing at an explicit `llm_endpoints` transport;
    # the dispatcher's `_inject_model_credentials` injects its `base_url`.
    base_url = config.base_url

    # Build model kwargs
    model_kwargs = {}
    # top_k is non-standard for the OpenAI Chat Completions API. Route it
    # via extra_body so OpenAI-compatible endpoints (vLLM, Ollama, etc.)
    # receive it in the request body; skip it for native api.openai.com
    # since the SDK rejects unknown kwargs.
    extra_body: dict = {}
    if config.top_k is not None and base_url:
        extra_body["top_k"] = config.top_k

    # Build kwargs for ChatOpenAI.
    # Force Chat Completions API — LangChain auto-detects Responses API for
    # gpt-5.*/o3/o4 models, but its streaming is broken for tool calls
    # (https://github.com/langchain-ai/langchain/issues/34660, still open).
    llm_kwargs = {
        "model": config.model,
        "temperature": config.temperature,
        "api_key": api_key,
        "max_retries": config.max_retries,
        "use_responses_api": False,
    }
    if config.top_p is not None:
        llm_kwargs["top_p"] = config.top_p

    # Reasoning via Chat Completions API (reasoning_effort in model_kwargs).
    reasoning_mode = "none"
    if config.reasoning_level and config.reasoning_level != "none":
        level = _clamp_reasoning_level(config.reasoning_level, _OPENAI_REASONING_LEVELS)
        model_kwargs["reasoning_effort"] = level
        reasoning_mode = f"chat_completions(effort={level})"

    # Add timeout if specified
    if config.timeout is not None:
        llm_kwargs["timeout"] = config.timeout

    # Only add base_url if specified
    if base_url:
        llm_kwargs["base_url"] = base_url

    # Only add model_kwargs if non-empty
    if model_kwargs:
        llm_kwargs["model_kwargs"] = model_kwargs

    if extra_body:
        llm_kwargs["extra_body"] = extra_body

    max_tokens = _resolve_max_output_tokens(config, limits)
    llm_kwargs["max_tokens"] = max_tokens

    # Add max_context_tokens for HTTP-layer validation (Layer 0 safety)
    # Prefer per-model config value, fall back to global limits
    max_context_tokens = config.model_max_context_tokens or (
        limits.model_max_context_tokens if limits else None
    )
    if max_context_tokens:
        llm_kwargs["max_context_tokens"] = max_context_tokens

    # Request usage on streamed responses (stream_options.include_usage).
    # Without it, OpenAI-compatible streaming (vLLM et al.) returns no token
    # usage at all — the persistent path streams every main call, so turn
    # metrics / usage.updated frames were empty
    # (docs/features/context_summarization_rework.md S5; verified on k3d).
    llm_kwargs["stream_usage"] = True

    # Pass KeyRing for automatic key rotation
    llm_kwargs["key_ring"] = key_ring

    llm = ReasoningChatOpenAI(**llm_kwargs)

    key_info = f"{len(keys)} key(s)" if len(keys) > 1 else "1 key"
    logger.info(
        f"Created OpenAI LLM: model={config.model}, temp={config.temperature}, "
        f"base_url={base_url or 'default'}, timeout={config.timeout}s, "
        f"max_retries={config.max_retries}, max_context_tokens={max_context_tokens or 'default'}, "
        f"max_tokens={max_tokens}, reasoning={reasoning_mode}, keys={key_info}"
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
    if config.top_p is not None:
        llm_kwargs["top_p"] = config.top_p
    if config.top_k is not None:
        llm_kwargs["top_k"] = config.top_k

    if config.timeout is not None:
        llm_kwargs["timeout"] = config.timeout

    # Anthropic requires max_tokens - use config override or model-aware defaults
    if config.max_output_tokens is not None:
        llm_kwargs["max_tokens"] = config.max_output_tokens
    else:
        model_lower = config.model.lower()
        if any(
            x in model_lower for x in ("opus-4-6", "opus-4-5", "opus-4-1", "opus-4-0")
        ):
            llm_kwargs["max_tokens"] = 32000
        elif any(x in model_lower for x in ("sonnet-4-5", "sonnet-4-0")):
            llm_kwargs["max_tokens"] = 16384
        elif config.model_max_context_tokens:
            llm_kwargs["max_tokens"] = min(8192, config.model_max_context_tokens // 4)
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
    if config.top_p is not None:
        llm_kwargs["top_p"] = config.top_p
    if config.top_k is not None:
        llm_kwargs["top_k"] = config.top_k

    # Google's timeout parameter name differs
    if config.timeout is not None:
        llm_kwargs["timeout"] = config.timeout

    # ChatGoogleGenerativeAI uses ``max_output_tokens`` (not ``max_tokens``)
    max_tokens = _resolve_max_output_tokens(config, limits)
    llm_kwargs["max_output_tokens"] = max_tokens

    llm = ChatGoogleGenerativeAI(**llm_kwargs)

    logger.info(
        f"Created Google LLM: model={config.model}, temp={config.temperature}, "
        f"timeout={config.timeout}s, max_output_tokens={max_tokens}"
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
        model = model[len("groq/") :]

    llm_kwargs = {
        "model": model,
        "temperature": config.temperature,
        "api_key": api_key,
        "max_retries": config.max_retries,
    }

    groq_model_kwargs = {}
    if config.top_p is not None:
        groq_model_kwargs["top_p"] = config.top_p
    if config.top_k is not None:
        groq_model_kwargs["top_k"] = config.top_k
    if groq_model_kwargs:
        llm_kwargs["model_kwargs"] = groq_model_kwargs

    if config.timeout is not None:
        llm_kwargs["timeout"] = config.timeout

    # Optional: custom base URL for Groq enterprise/proxy
    if config.base_url:
        llm_kwargs["groq_api_base"] = config.base_url

    max_tokens = _resolve_max_output_tokens(config, limits)
    llm_kwargs["max_tokens"] = max_tokens

    llm = ChatGroq(**llm_kwargs)

    logger.info(
        f"Created Groq LLM: model={model}, temp={config.temperature}, "
        f"timeout={config.timeout}s, max_retries={config.max_retries}, "
        f"max_tokens={max_tokens}"
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
    key_ring = get_or_create_key_ring(
        keys, provider="openrouter", cooldown_seconds=cooldown
    )

    # SDK gets the first key; KeyRing overrides the header in send()
    api_key = keys[0]

    # Strip openrouter/ prefix — OpenRouter expects provider/model format
    model = config.model
    if model.lower().startswith("openrouter/"):
        model = model[len("openrouter/") :]

    # Base URL: explicit config wins, otherwise always OpenRouter
    base_url = config.base_url or "https://openrouter.ai/api/v1"

    # Build model kwargs
    model_kwargs = {}

    # OpenRouter uses a nested reasoning object in the request body.
    # OpenRouter supports all levels (none, minimal, low, medium, high, xhigh) — no clamping needed.
    # It must travel via extra_body: langchain-openai >= 1.x forwards a
    # first-class ``reasoning`` field into the Chat Completions payload, and
    # the OpenAI SDK's typed create() rejects it (TypeError: unexpected
    # keyword argument 'reasoning'). extra_body merges into the JSON body
    # without going through the typed signature.
    extra_body = {}
    if config.reasoning_level and config.reasoning_level != "none":
        extra_body["reasoning"] = {"effort": config.reasoning_level}

    # top_k is likewise non-standard for the typed Chat Completions signature.
    if config.top_k is not None:
        extra_body["top_k"] = config.top_k

    # Build kwargs for ReasoningChatOpenAI
    llm_kwargs = {
        "model": model,
        "temperature": config.temperature,
        "api_key": api_key,
        "base_url": base_url,
        "max_retries": config.max_retries,
        # OpenRouter supports its reasoning object on Chat Completions.
        # LangChain infers the Responses API whenever ``reasoning`` is set,
        # which is not compatible with all OpenRouter-routed models.
        "use_responses_api": False,
    }
    if config.top_p is not None:
        llm_kwargs["top_p"] = config.top_p

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

    if extra_body:
        llm_kwargs["extra_body"] = extra_body

    max_tokens = _resolve_max_output_tokens(config, limits)
    llm_kwargs["max_tokens"] = max_tokens

    # Add max_context_tokens for HTTP-layer validation (Layer 0 safety)
    # Prefer per-model config value, fall back to global limits
    max_context_tokens = config.model_max_context_tokens or (
        limits.model_max_context_tokens if limits else None
    )
    if max_context_tokens:
        llm_kwargs["max_context_tokens"] = max_context_tokens

    # Request usage on streamed responses (stream_options.include_usage).
    # Without it, OpenAI-compatible streaming (vLLM et al.) returns no token
    # usage at all — the persistent path streams every main call, so turn
    # metrics / usage.updated frames were empty
    # (docs/features/context_summarization_rework.md S5; verified on k3d).
    llm_kwargs["stream_usage"] = True

    # Pass KeyRing for automatic key rotation
    llm_kwargs["key_ring"] = key_ring

    llm = ReasoningChatOpenAI(**llm_kwargs)

    key_info = f"{len(keys)} key(s)" if len(keys) > 1 else "1 key"
    reasoning_mode = (
        f"chat_completions(effort={config.reasoning_level})"
        if config.reasoning_level and config.reasoning_level != "none"
        else "none"
    )
    logger.info(
        f"Created OpenRouter LLM: model={model}, temp={config.temperature}, "
        f"base_url={base_url}, timeout={config.timeout}s, "
        f"max_retries={config.max_retries}, max_context_tokens={max_context_tokens or 'default'}, "
        f"max_tokens={max_tokens}, reasoning={reasoning_mode}, keys={key_info}"
    )

    return llm


def _create_codex_llm(
    config: LLMConfig,
    limits: Optional[LimitsConfig] = None,
) -> BaseChatModel:
    """Create Codex LLM (ChatGPT Plus/Pro subscription via CLIProxyAPI OAuth proxy).

    Routes through CLIProxyAPI at localhost:8317/v1 (configurable via CODEX_BASE_URL
    env var or config.base_url). The proxy handles OAuth authentication for ChatGPT
    Plus/Pro subscriptions, providing API access through the subscription.

    Model names are specified as codex/<model>, e.g.:
    - codex/gpt-5.4-pro
    - codex/o3-pro
    - codex/gpt-4o

    The codex/ prefix is stripped before sending to the proxy API.

    Configuration resolution (project → user → fallback):
    - Base URL: config.base_url → CODEX_BASE_URL env → http://localhost:8317/v1
    - API key:  config.api_key  → CODEX_API_KEY env  → "not-needed"

    Multiple API keys can be provided as a comma-separated string in
    CODEX_API_KEY for automatic fallback rotation (though typically
    not needed as CLIProxyAPI handles auth).
    """
    from src.llm.key_ring import parse_key_string, get_or_create_key_ring

    # Parse API keys — CLIProxyAPI handles OAuth, so "not-needed" is the default
    raw_key = config.api_key or os.getenv("CODEX_API_KEY", "not-needed")
    keys = parse_key_string(raw_key) or ["not-needed"]
    cooldown = float(os.getenv("KEY_COOLDOWN_SECONDS", "1800"))
    key_ring = get_or_create_key_ring(keys, provider="codex", cooldown_seconds=cooldown)

    # SDK gets the first key; KeyRing overrides the header in send()
    api_key = keys[0]

    # Strip codex/ prefix — the proxy expects bare model names
    model = config.model
    if model.lower().startswith("codex/"):
        model = model[len("codex/") :]

    # Base URL: explicit config → env var → default localhost proxy
    base_url = config.base_url or os.getenv(
        "CODEX_BASE_URL", "http://localhost:8317/v1"
    )

    # Build model kwargs
    model_kwargs = {}
    # top_k is non-standard for the OpenAI Responses API. Route it via
    # extra_body so the proxy can forward it to the underlying provider.
    extra_body: dict = {}
    if config.top_k is not None:
        extra_body["top_k"] = config.top_k

    # Build kwargs for ReasoningChatOpenAI.
    # The Codex proxy (CLIProxyAPI) only supports the Responses API endpoint
    # (/v1/responses), NOT Chat Completions (/v1/chat/completions).
    # We must use the Responses API here. LangChain's Responses API streaming
    # has a known bug with tool call args (langchain-ai/langchain#34660),
    # but the ainvoke workaround in persistent_graph.py handles this.
    llm_kwargs = {
        "model": model,
        "temperature": config.temperature,
        "api_key": api_key,
        "base_url": base_url,
        "max_retries": config.max_retries,
    }
    if config.top_p is not None:
        llm_kwargs["top_p"] = config.top_p

    # Reasoning via Responses API (required by Codex proxy).
    reasoning_mode = "none"
    if config.reasoning_level and config.reasoning_level != "none":
        level = _clamp_reasoning_level(config.reasoning_level, _OPENAI_REASONING_LEVELS)
        if _should_use_reasoning_summary(model):
            llm_kwargs["reasoning"] = {
                "effort": level,
                "summary": "auto",
            }
            reasoning_mode = f"responses_api(effort={level})"
        else:
            model_kwargs["reasoning_effort"] = level
            reasoning_mode = f"chat_completions(effort={level})"

    if config.timeout is not None:
        llm_kwargs["timeout"] = config.timeout

    if model_kwargs:
        llm_kwargs["model_kwargs"] = model_kwargs

    if extra_body:
        llm_kwargs["extra_body"] = extra_body

    max_tokens = _resolve_max_output_tokens(config, limits)
    llm_kwargs["max_tokens"] = max_tokens

    # Add max_context_tokens for HTTP-layer validation (Layer 0 safety)
    # Prefer per-model config value, fall back to global limits
    max_context_tokens = config.model_max_context_tokens or (
        limits.model_max_context_tokens if limits else None
    )
    if max_context_tokens:
        llm_kwargs["max_context_tokens"] = max_context_tokens

    # Pass KeyRing for automatic key rotation
    llm_kwargs["key_ring"] = key_ring

    llm = ReasoningChatOpenAI(**llm_kwargs)

    key_info = f"{len(keys)} key(s)" if len(keys) > 1 else "1 key"
    logger.info(
        f"Created Codex LLM: model={model}, temp={config.temperature}, "
        f"base_url={base_url}, timeout={config.timeout}s, "
        f"max_retries={config.max_retries}, max_context_tokens={max_context_tokens or 'default'}, "
        f"max_tokens={max_tokens}, reasoning={reasoning_mode}, keys={key_info}"
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
        Raw template string with placeholders ({prompt_content}, etc.)

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
    model: str = "",
    tool_names: Optional[List[str]] = None,
    prompt_type: Optional[str] = None,
) -> str:
    """Get the complete system prompt for the current phase.

    This is the main entry point for phase-aware prompts. It uses a
    component-based system:
    1. Load base template (systemprompt.txt)
    2. Load phase component (strategic.txt or tactical.txt)
    3. Render phase component's {phase_number} placeholder
    4. Inject rendered component into base template's {prompt_content}
    5. Render remaining placeholders ({agent_display_name}, etc.)
    6. Render Jinja2 conditionals ({% if has_tool("kb_write") %} etc.)

    Note: todos, memory, and knowledge are injected as transient messages
    in graph.py, not included in the system prompt.

    Args:
        config: Agent configuration
        is_strategic: True for strategic phase, False for tactical
        phase_number: Current phase number
        model: Model name for prompt matrix resolution.
        tool_names: List of loaded tool names for Jinja2 conditionals.

    Returns:
        Fully rendered system prompt string

    Example:
        ```python
        prompt = get_phase_system_prompt(
            config=config,
            is_strategic=True,
            phase_number=1,
            model="claude-opus-4-6",
            tool_names=["kb_write", "todo_complete"],
        )
        ```
    """
    # Check for pre-resolved prompt content (from resolved_config JSONB)
    resolved_prompts = config.extra.get("_resolved_prompts", {})

    model_family = family_of(model) if model else "default"
    resolver = PromptMatrixResolver(config._deployment_dir, model_family)

    # Interactive mode: single self-contained prompt, no phase component injection
    if prompt_type == "interactive":
        template = resolved_prompts.get("systemprompt_interactive") or resolver.load(
            "systemprompt_interactive"
        )

        # Load expert persona
        expert_identity = resolved_prompts.get("persona") or ""
        if not expert_identity:
            try:
                expert_identity = resolver.load("persona")
            except FileNotFoundError:
                expert_identity = ""

        # Render Jinja2 conditionals
        cli_ds_interactive = config.extra.get("_cli_datasources", [])
        if tool_names is not None:
            template = render_instruction_content(
                template, tool_names, cli_datasources=cli_ds_interactive
            )

        rendered = template.format(
            agent_display_name=config.display_name,
            expert_identity=expert_identity,
        )

        # Prepend reasoning directive for OSS models
        method = detect_reasoning_method(
            model or config.llm.model, config.llm.reasoning_method
        )
        if method == "prompt":
            level = config.llm.reasoning_level or "high"
            rendered = f"Reasoning: {level}\n\n{rendered}"

        return rendered

    # Worker mode: base template (systemprompt.txt) + phase component (strategic/tactical)
    base_template = resolved_prompts.get("systemprompt") or load_base_system_prompt(
        resolver
    )

    # Load expert persona (empty string if no persona file exists)
    expert_identity = resolved_prompts.get("persona") or ""
    if not expert_identity:
        try:
            expert_identity = resolver.load("persona")
        except FileNotFoundError:
            expert_identity = ""

    # Load phase component
    prompt_type_key = prompt_type or ("strategic" if is_strategic else "tactical")
    phase_component = resolved_prompts.get(prompt_type_key) or load_phase_component(
        is_strategic, resolver
    )

    # Render Jinja2 conditionals BEFORE .format() — Python's str.format()
    # chokes on {%..%} blocks. Jinja2 leaves single-brace placeholders untouched.
    cli_ds = config.extra.get("_cli_datasources", [])
    if tool_names is not None:
        phase_component = render_instruction_content(
            phase_component, tool_names, cli_datasources=cli_ds
        )
        base_template = render_instruction_content(
            base_template, tool_names, cli_datasources=cli_ds
        )

    # Render phase component's {phase_number} placeholder
    rendered_component = phase_component.format(phase_number=phase_number)

    # Inject all components and render remaining placeholders
    rendered = base_template.format(
        agent_display_name=config.display_name,
        expert_identity=expert_identity,
        prompt_content=rendered_component,
    )

    # Prepend reasoning directive only for OSS models that need it as prompt text
    method = detect_reasoning_method(
        model or config.llm.model, config.llm.reasoning_method
    )
    if method == "prompt":
        level = config.llm.reasoning_level or "high"
        rendered = f"Reasoning: {level}\n\n{rendered}"

    return rendered


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

    model_family = family_of(model) if model else "default"
    resolver = InstructionMatrixResolver(config._deployment_dir, model_family)
    try:
        return resolver.load("instructions")
    except FileNotFoundError:
        logger.warning("Instructions template not found. Using minimal instructions.")
        # Build tool list from all categories
        all_tools = []
        all_tools.extend(config.tools.workspace)
        all_tools.extend(config.tools.core)
        all_tools.extend(config.tools.research)
        all_tools.extend(config.tools.citation)
        all_tools.extend(config.tools.graph)
        all_tools.extend(config.tools.sql)
        all_tools.extend(config.tools.mongodb)
        all_tools.extend(config.tools.git)
        all_tools.extend(config.tools.shell)
        all_tools.extend(config.tools.evaluation)
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
    Prepends a reasoning directive for OSS models that need it as prompt text.

    Args:
        config: Agent configuration
        model: Model name for prompt matrix resolution.

    Returns:
        Summarization prompt content ready for use
    """
    # Check for pre-resolved content (from resolved_config JSONB)
    resolved = config.extra.get("_resolved_prompts", {})
    template = resolved.get("summarization") or ""

    if not template:
        model_family = family_of(model) if model else "default"
        resolver = PromptMatrixResolver(config._deployment_dir, model_family)
        try:
            template = resolver.load("summarization")
        except FileNotFoundError:
            logger.warning("Summarization prompt not found. Using default prompt.")
            template = """Summarize this agent conversation concisely.
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

    # Prepend reasoning directive only for OSS models that need it as prompt text
    summarization_config = config.llm.get_phase_config("summarization")
    method = detect_reasoning_method(
        model or summarization_config.model,
        summarization_config.reasoning_method,
    )
    if method == "prompt":
        level = (
            config.context_management.reasoning_level
            or config.llm.reasoning_level
            or "high"
        )
        template = f"Reasoning: {level}\n\n{template}"

    return template


def load_auxiliary_prompt(
    config: AgentConfig, prompt_type: str, model: str = ""
) -> str:
    """Load an auxiliary task prompt via the prompt matrix.

    Uses PromptMatrixResolver for model-aware prompt resolution.
    Supports "memory_extraction" and "curation" prompt types.

    Args:
        config: Agent configuration
        prompt_type: Prompt type key (e.g., "memory_extraction", "curation")
        model: Model name for prompt matrix resolution.

    Returns:
        Prompt content as string

    Raises:
        FileNotFoundError: If the prompt file is not found in the matrix
    """
    # Check for pre-resolved content (from resolved_config JSONB)
    resolved = config.extra.get("_resolved_prompts", {})
    template = resolved.get(prompt_type) or ""

    if not template:
        model_family = family_of(model) if model else "default"
        resolver = PromptMatrixResolver(config._deployment_dir, model_family)
        template = resolver.load(prompt_type)

    # Prepend reasoning directive for models that need it as prompt text (e.g. gpt-oss)
    method = detect_reasoning_method(
        model or config.llm.model, config.llm.reasoning_method
    )
    if method == "prompt":
        level = config.llm.reasoning_level or "high"
        template = f"Reasoning: {level}\n\n{template}"

    return template


def get_all_tool_names(config: AgentConfig) -> List[str]:
    """Get all tool names from configuration.

    Applies shell mode aliasing: when mode=stateless, shell_execute is
    mapped to run_command (and vice versa for persistent mode). This
    ensures backward compatibility with existing configs.

    Args:
        config: Agent configuration

    Returns:
        List of all configured tool names
    """
    names = (
        config.tools.workspace
        + config.tools.core
        + config.tools.research
        + config.tools.browser_direct
        + config.tools.citation
        + config.tools.graph
        + config.tools.sql
        + config.tools.mongodb
        + config.tools.git
        + config.tools.shell
        + config.tools.evaluation
        + config.tools.knowledge
        + config.tools.webdav
        + config.tools.communication
        + config.tools.delegation
        + config.tools.orchestrator
    )

    # Shell mode aliasing for backward compatibility
    shell_config = config.extra.get("shell", {})
    mode = (
        shell_config.get("mode", "stateless")
        if isinstance(shell_config, dict)
        else "stateless"
    )
    if mode == "stateless":
        names = ["run_command" if n == "shell_execute" else n for n in names]
    elif mode == "persistent":
        names = ["shell_execute" if n == "run_command" else n for n in names]

    return names


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
            [
                "Strategic todos template must have a 'todos' key with a list of todo items"
            ],
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
            validated_todos.append(
                {
                    "id": todo_id,
                    "content": content_val.strip(),
                }
            )

    if errors:
        raise StrategicTodosValidationError(
            f"Strategic todos validation failed with {len(errors)} error(s)",
            errors,
        )

    logger.debug(
        f"Parsed strategic todos template: {len(validated_todos)} todos from {path}"
    )
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
            f"Strategic todos validation failed with {len(errors)} error(s)",
            errors,
        )
    return validated_todos


def load_strategic_todos_template(
    template_name: str,
    deployment_dir: Optional[str] = None,
    model: str = "",
    resolved_content: Optional[str] = None,
    tool_names: Optional[List[str]] = None,
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
        tool_names: List of loaded tool names for Jinja2 template rendering.

    Returns:
        List of todo dicts with 'id' and 'content' keys

    Raises:
        FileNotFoundError: If template not found in either location
        StrategicTodosValidationError: If template is invalid
    """
    # Check for pre-resolved content first
    if resolved_content and isinstance(resolved_content, str):
        logger.debug("Loading strategic todos from resolved content")
        if tool_names:
            resolved_content = render_instruction_content(resolved_content, tool_names)
        return _parse_strategic_todos_yaml_from_string(resolved_content)

    # Use InstructionMatrixResolver for 4-level fallback
    model_family = family_of(model) if model else "default"
    resolver = InstructionMatrixResolver(deployment_dir, model_family)

    # Strip .yaml extension for instruction type key
    instruction_type = template_name.replace(".yaml", "")

    try:
        path = resolver._file_resolver.resolve(
            resolver.resolve_filename(instruction_type)
        )
        logger.debug(f"Loading strategic todos from: {path}")
        # Render Jinja2 templates before YAML parsing
        if tool_names:
            raw_content = path.read_text(encoding="utf-8")
            rendered = render_instruction_content(raw_content, tool_names)
            return _parse_strategic_todos_yaml_from_string(rendered)
        return _parse_strategic_todos_yaml(path)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Strategic todos template not found: {template_name} "
            f"(checked: {deployment_dir}, config/templates/)"
        )


def get_initial_strategic_todos_from_config(
    config: Optional["AgentConfig"] = None,
    tool_names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Get initial strategic todos for job start.

    Loads from strategic_todos_initial.yaml template with deployment override support.

    Args:
        config: Agent configuration (for deployment directory). If None, uses
               framework defaults only.
        tool_names: List of loaded tool names for Jinja2 template rendering.

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
            tool_names=tool_names,
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
    tool_names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Get strategic todos for phase transitions.

    Loads from strategic_todos_transition.yaml template with deployment override support.

    Args:
        config: Agent configuration (for deployment directory). If None, uses
               framework defaults only.
        tool_names: List of loaded tool names for Jinja2 template rendering.

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
            tool_names=tool_names,
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
    tool_names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Get strategic todos for resuming a frozen job with feedback.

    Loads from strategic_todos_resume.yaml template with deployment override support.

    Args:
        config: Agent configuration (for deployment directory). If None, uses
               framework defaults only.
        tool_names: List of loaded tool names for Jinja2 template rendering.

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
            tool_names=tool_names,
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

    model_family = family_of(model) if model else "default"

    # Agent config as dict (strip internal fields and secrets)
    agent_dict = dataclasses.asdict(config)
    agent_dict.pop("_deployment_dir", None)

    # Flatten extra into top level to prevent double-nesting on deserialization.
    # dataclasses.asdict() includes extra as a literal dict key, but
    # load_agent_config_from_dict() expects these keys at the top level
    # (just like fresh YAML input). Flatten them back.
    extra = agent_dict.pop("extra", {})
    for k, v in extra.items():
        if k not in agent_dict:  # Don't overwrite standard fields
            agent_dict[k] = v
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
    for pt in [
        "systemprompt",
        "systemprompt_interactive",
        "persona",
        "strategic",
        "tactical",
        "summarization",
    ]:
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

    # Also resolve custom instruction files from config.instruction_files
    # (e.g. research_guide.md) — these aren't in the matrix but need to survive
    # serialization so resumed/VM jobs can copy them to the workspace.
    if config.instruction_files:
        templates_dir = get_project_root() / "config" / "templates"
        file_resolver = FileResolver(
            deployment_dir=config._deployment_dir,
            framework_dir=templates_dir,
        )
        for entry in config.instruction_files:
            basename = Path(entry.file).stem
            if basename not in instructions:
                try:
                    instructions[basename] = file_resolver.load(Path(entry.file).name)
                except FileNotFoundError:
                    pass

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

    # Fix double-nesting from pre-fix serialized configs:
    # Old serialize_resolved_config() stored extra as {"extra": {shell, ...}},
    # which load_agent_config_from_dict() wraps into extra["extra"].
    if "extra" in config.extra and isinstance(config.extra["extra"], dict):
        nested = config.extra.pop("extra")
        for k, v in nested.items():
            if k not in config.extra:
                config.extra[k] = v

    # Store pre-resolved content for runtime use
    config.extra["_resolved_prompts"] = resolved.get("prompts", {})
    config.extra["_resolved_instructions"] = resolved.get("instructions", {})
    return config
