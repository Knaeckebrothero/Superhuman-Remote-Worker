"""
User interface components.
"""
from typing import Optional

import streamlit as st

from src.core.neo4j_utils import Neo4jConnection
from src.core.config import get_project_root, load_config, load_prompt

# Re-export config functions for backwards compatibility
__all__ = ['get_project_root', 'load_config', 'load_prompt', 'get_neo4j_connection', 'require_connection']


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
