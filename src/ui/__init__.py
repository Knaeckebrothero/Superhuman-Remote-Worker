"""
User interface components.
"""
import json
from pathlib import Path
from typing import Optional

import streamlit as st

from src.core.neo4j_utils import Neo4jConnection


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent.parent


def load_config(name: str) -> dict:
    """
    Load JSON config from config/ directory.

    Args:
        name: Config filename (e.g., 'agent_config.json')

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


def get_neo4j_connection() -> Optional[Neo4jConnection]:
    """
    Get Neo4j connection from session state.

    Returns:
        Neo4jConnection if connected, None otherwise
    """
    return st.session_state.get("neo4j_connection", None)


def require_connection() -> bool:
    """
    Check if Neo4j connection exists. Show warning if not.

    Returns:
        True if connected, False otherwise
    """
    conn = get_neo4j_connection()
    if conn is None:
        st.warning("Not connected to Neo4j database. Please connect on the Home page first.")
        return False
    return True
