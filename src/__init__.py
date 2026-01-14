"""
Graph-RAG Requirement Analysis System

Universal Agent architecture for requirement extraction and validation.
"""

__version__ = "2.0.0"

# Core utilities
from src.database.neo4j_utils import Neo4jConnection, create_neo4j_connection

# Universal Agent (new architecture)
from src.agent import UniversalAgent, UniversalAgentState, create_app
from src.agent import WorkspaceManager, TodoManager, ContextManager

__all__ = [
    # Core
    'Neo4jConnection',
    'create_neo4j_connection',
    # Universal Agent
    'UniversalAgent',
    'UniversalAgentState',
    'create_app',
    'WorkspaceManager',
    'TodoManager',
    'ContextManager',
]
