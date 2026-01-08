"""
Configuration utilities for loading config files and prompts.

Environment variables take precedence over config file values.
"""
import json
import os
from pathlib import Path
from typing import Any, Optional


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent.parent


def load_config(name: str) -> dict:
    """
    Load JSON config from config/ directory.

    Args:
        name: Config filename (e.g., 'llm_config.json')

    Returns:
        Config dictionary
    """
    config_path = get_project_root() / "config" / name
    with open(config_path, 'r') as f:
        return json.load(f)


def load_prompt(name: str) -> str:
    """
    Load prompt text from config/prompts/ directory.

    Args:
        name: Prompt filename (e.g., 'agent_system.txt')

    Returns:
        Prompt text content
    """
    prompt_path = get_project_root() / "config" / "prompts" / name
    with open(prompt_path, 'r') as f:
        return f.read()


# =============================================================================
# Environment Variable Configuration
# =============================================================================
# Priority: ENV VAR > llm_config.json > hardcoded default

# Cache for llm_config.json
_llm_config_cache: Optional[dict] = None


def _get_llm_config() -> dict:
    """Get cached LLM config."""
    global _llm_config_cache
    if _llm_config_cache is None:
        try:
            _llm_config_cache = load_config("llm_config.json")
        except FileNotFoundError:
            _llm_config_cache = {}
    return _llm_config_cache


def get_env_int(env_var: str, config_path: list[str], default: int) -> int:
    """
    Get integer config value with priority: ENV > config file > default.

    Args:
        env_var: Environment variable name
        config_path: Path in llm_config.json (e.g., ["creator_agent", "max_iterations_per_candidate"])
        default: Default value if neither env nor config is set

    Returns:
        Integer configuration value
    """
    # Check environment variable first
    env_value = os.getenv(env_var)
    if env_value is not None:
        try:
            return int(env_value)
        except ValueError:
            pass

    # Check config file
    config = _get_llm_config()
    value = config
    for key in config_path:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            value = None
            break

    if value is not None:
        try:
            return int(value)
        except (ValueError, TypeError):
            pass

    return default


def get_env_float(env_var: str, config_path: list[str], default: float) -> float:
    """
    Get float config value with priority: ENV > config file > default.

    Args:
        env_var: Environment variable name
        config_path: Path in llm_config.json
        default: Default value if neither env nor config is set

    Returns:
        Float configuration value
    """
    env_value = os.getenv(env_var)
    if env_value is not None:
        try:
            return float(env_value)
        except ValueError:
            pass

    config = _get_llm_config()
    value = config
    for key in config_path:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            value = None
            break

    if value is not None:
        try:
            return float(value)
        except (ValueError, TypeError):
            pass

    return default


def get_env_str(env_var: str, config_path: list[str], default: str) -> str:
    """
    Get string config value with priority: ENV > config file > default.

    Args:
        env_var: Environment variable name
        config_path: Path in llm_config.json
        default: Default value if neither env nor config is set

    Returns:
        String configuration value
    """
    env_value = os.getenv(env_var)
    if env_value is not None:
        return env_value

    config = _get_llm_config()
    value = config
    for key in config_path:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            value = None
            break

    if value is not None:
        return str(value)

    return default


# =============================================================================
# Agent Iteration Limits
# =============================================================================

def get_creator_max_iterations() -> int:
    """Get max iterations for Creator agent (default: 500)."""
    return get_env_int(
        "CREATOR_MAX_ITERATIONS",
        ["creator_agent", "max_iterations_per_candidate"],
        500
    )


def get_validator_max_iterations() -> int:
    """Get max iterations for Validator agent (default: 500)."""
    return get_env_int(
        "VALIDATOR_MAX_ITERATIONS",
        ["validator_agent", "max_iterations_per_requirement"],
        500
    )


def get_creator_recursion_limit() -> int:
    """Get LangGraph recursion limit for Creator agent (default: 500)."""
    return get_env_int(
        "CREATOR_RECURSION_LIMIT",
        ["creator_agent", "recursion_limit"],
        500
    )


def get_validator_recursion_limit() -> int:
    """Get LangGraph recursion limit for Validator agent (default: 200)."""
    return get_env_int(
        "VALIDATOR_RECURSION_LIMIT",
        ["validator_agent", "recursion_limit"],
        200
    )


# =============================================================================
# Polling & Retry
# =============================================================================

def get_creator_polling_interval() -> int:
    """Get Creator agent polling interval in seconds (default: 30)."""
    return get_env_int(
        "CREATOR_POLLING_INTERVAL",
        ["creator_agent", "polling_interval_seconds"],
        30
    )


def get_validator_polling_interval() -> int:
    """Get Validator agent polling interval in seconds (default: 10)."""
    return get_env_int(
        "VALIDATOR_POLLING_INTERVAL",
        ["validator_agent", "polling_interval_seconds"],
        10
    )


def get_agent_retry_delay() -> int:
    """Get delay in seconds before retrying after an error (default: 10)."""
    return get_env_int(
        "AGENT_RETRY_DELAY",
        ["agent", "retry_delay_seconds"],
        10
    )


# =============================================================================
# Thresholds
# =============================================================================

def get_min_confidence_threshold() -> float:
    """Get minimum confidence threshold for candidate extraction (default: 0.6)."""
    return get_env_float(
        "MIN_CONFIDENCE_THRESHOLD",
        ["creator_agent", "min_confidence_threshold"],
        0.6
    )


def get_duplicate_similarity_threshold() -> float:
    """Get similarity threshold for duplicate detection (default: 0.95)."""
    return get_env_float(
        "DUPLICATE_SIMILARITY_THRESHOLD",
        ["validator_agent", "duplicate_threshold"],
        0.95
    )


def get_fulfillment_confidence_threshold() -> float:
    """Get confidence threshold for fulfillment analysis (default: 0.7)."""
    return get_env_float(
        "FULFILLMENT_CONFIDENCE_THRESHOLD",
        ["validator_agent", "fulfillment_confidence_threshold"],
        0.7
    )


# =============================================================================
# Context Management
# =============================================================================

def get_context_compaction_threshold() -> int:
    """Get token threshold for context compaction (default: 100000)."""
    return get_env_int(
        "CONTEXT_COMPACTION_THRESHOLD",
        ["context_management", "compaction_threshold_tokens"],
        100000
    )


def get_context_max_output_tokens() -> int:
    """Get max output tokens for context (default: 80000)."""
    return get_env_int(
        "CONTEXT_MAX_OUTPUT_TOKENS",
        ["context_management", "max_output_tokens"],
        80000
    )


# =============================================================================
# Orchestrator
# =============================================================================

def get_job_timeout_hours() -> int:
    """Get job timeout in hours (default: 168 = 7 days)."""
    return get_env_int(
        "JOB_TIMEOUT_HOURS",
        ["orchestrator", "job_timeout_hours"],
        168
    )


def get_max_requirement_retries() -> int:
    """Get max retries for failed requirements (default: 5)."""
    return get_env_int(
        "MAX_REQUIREMENT_RETRIES",
        ["orchestrator", "max_requirement_retries"],
        5
    )


# =============================================================================
# Workspace Configuration
# =============================================================================

def get_workspace_base_path() -> str:
    """Get base path for agent workspaces.

    Priority: ENV > config file > default

    Returns:
        Base path string (empty string means use auto-detection)
    """
    return get_env_str(
        "WORKSPACE_PATH",
        ["workspace", "base_path"],
        ""  # Empty means auto-detect in WorkspaceManager
    )


def get_workspace_structure() -> list[str]:
    """Get default workspace directory structure."""
    config = _get_llm_config()
    workspace_config = config.get("workspace", {})
    return workspace_config.get("structure", [
        "plans",
        "archive",
        "documents",
        "documents/sources",
        "notes",
        "chunks",
        "candidates",
        "requirements",
        "output",
    ])
