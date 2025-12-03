"""
Configuration utilities for loading config files and prompts.
"""
import json
from pathlib import Path


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
