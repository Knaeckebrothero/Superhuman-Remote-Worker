"""Builder configuration resolver.

Loads settings and prompt matrices for the instruction builder,
using two-level resolution (default + model-family override).

Matrix files live in orchestrator/config/.
Prompt files live in orchestrator/config/prompts/.
"""

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dicts. Objects merge recursively, null clears a key.

    Kept in sync with the same utility in src/core/loader.py.
    """
    result = {**base}
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def detect_model_family(model: str) -> str:
    """Detect model family from model name for matrix resolution.

    Strips provider prefixes (openrouter/, groq/, openai/) and pattern-matches
    the model name to a family string.  Returns ``"default"`` as fallback.

    Kept in sync with src/core/loader.py:detect_model_family().
    """
    name = model.lower()

    # Strip provider prefixes
    for prefix in ("openrouter/", "groq/", "codex/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            if "/" in name:
                name = name.split("/", 1)[1]
            break

    if name.startswith("openai/"):
        name = name[len("openai/") :]

    # Pattern match
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
    if "gemma" in name:
        return "gemma"
    if "minimax" in name:
        return "minimax"

    return "default"


# ---------------------------------------------------------------------------
# YAML loading with caching
# ---------------------------------------------------------------------------

_yaml_cache: dict[str, dict[str, Any]] = {}


def _load_yaml(filename: str) -> dict[str, Any]:
    """Load a YAML file from orchestrator/config/, caching the result."""
    if filename in _yaml_cache:
        return _yaml_cache[filename]

    path = _CONFIG_DIR / filename
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("Builder config file not found: %s", path)
        data = {}
    except Exception as e:
        logger.warning("Failed to load builder config %s: %s", path, e)
        data = {}

    _yaml_cache[filename] = data
    return data


def clear_cache() -> None:
    """Clear the YAML cache (useful for hot-reloading)."""
    _yaml_cache.clear()


# ---------------------------------------------------------------------------
# Settings Matrix
# ---------------------------------------------------------------------------


def resolve_builder_settings(model: str) -> dict[str, Any]:
    """Resolve builder inference settings for a model.

    Loads ``builder_settings_matrix.yaml``, detects the model family,
    and deep-merges ``default`` + family-specific overrides.

    Returns a flat dict.  Keys with ``None`` values are excluded so callers
    can use ``settings.get("temperature")`` and get ``None`` for unset params.
    """
    matrix = _load_yaml("builder_settings_matrix.yaml")
    family = detect_model_family(model)

    base = dict(matrix.get("default", {}))
    if family != "default" and family in matrix:
        base = deep_merge(base, matrix[family])

    # Strip None values so callers can rely on .get() returning None for unset
    return {k: v for k, v in base.items() if v is not None}


# ---------------------------------------------------------------------------
# Prompt Matrix
# ---------------------------------------------------------------------------


def resolve_builder_prompt(model: str, prompt_type: str = "system_prompt") -> str:
    """Resolve and load a builder prompt for a model.

    Loads ``builder_prompt_matrix.yaml``, detects the model family,
    resolves the filename, and reads it from ``orchestrator/config/prompts/``.

    Falls back to the ``default`` entry if no family-specific override exists.
    """
    matrix = _load_yaml("builder_prompt_matrix.yaml")
    family = detect_model_family(model)

    # Resolve filename: family-specific → default
    filename = None
    if family != "default" and family in matrix:
        filename = matrix[family].get(prompt_type)
    if not filename:
        filename = matrix.get("default", {}).get(prompt_type)

    if not filename:
        raise FileNotFoundError(
            f"No prompt file for type={prompt_type!r}, family={family!r} in builder_prompt_matrix.yaml"
        )

    path = _CONFIG_DIR / "prompts" / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"Builder prompt file not found: {path}")
