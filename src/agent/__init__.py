"""
Agent implementations for requirement analysis.

This module provides the Universal Agent - a config-driven agent that can
be deployed as Creator or Validator by changing its JSON configuration.
"""

# Universal Agent
from src.agent.agent import UniversalAgent
from src.agent.state import UniversalAgentState
from src.agent.app import create_app

# Shared utilities
from src.agent.workspace_manager import WorkspaceManager
from src.agent.todo_manager import TodoManager
from src.agent.context_manager import ContextManager

__all__ = [
    # Universal Agent
    'UniversalAgent',
    'UniversalAgentState',
    'create_app',
    # Shared utilities
    'WorkspaceManager',
    'TodoManager',
    'ContextManager',
]
