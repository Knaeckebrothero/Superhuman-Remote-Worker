"""
Neo4j Database Requirement Checker
A workflow for checking Neo4j database compliance against requirements from CSV files.
"""

__version__ = "1.0.0"

# Re-export from submodules for backwards compatibility
from src.core.neo4j_utils import Neo4jConnection, create_neo4j_connection
from src.core.csv_processor import RequirementProcessor, load_requirements_from_env
from src.agents.graph_agent import RequirementGraphAgent, create_graph_agent
from src.workflow import RequirementWorkflow

__all__ = [
    # Core
    'Neo4jConnection',
    'create_neo4j_connection',
    'RequirementProcessor',
    'load_requirements_from_env',
    # Agents
    'RequirementGraphAgent',
    'create_graph_agent',
    # Workflow
    'RequirementWorkflow',
]
