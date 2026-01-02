"""Validator Agent package for requirement validation and graph integration.

This package implements the Validator Agent, responsible for:
- Polling the requirement_cache for pending requirements
- Validating relevance to the domain graph
- Analyzing fulfillment by existing entities
- Integrating valid requirements into Neo4j
- Creating fulfillment relationships (FULFILLED_BY_*, NOT_FULFILLED_BY_*)
- Updating requirement cache status

Components:
- ValidatorAgent: Main LangGraph-based agent
- RelevanceAnalyzer: Domain relevance checking
- FulfillmentChecker: Per-entity fulfillment analysis
- GraphIntegrator: Neo4j integration with transaction safety
- ValidatorAgentTools: Tool definitions for the agent
- RequirementCacheReader: PostgreSQL cache operations
"""

from src.agents.validator.validator_agent import (
    ValidatorAgent,
    ValidatorAgentState,
    create_validator_agent,
)
from src.agents.validator.relevance_analyzer import (
    RelevanceAnalyzer,
    RelevanceDecision,
    RelevanceResult,
    create_relevance_analyzer,
)
from src.agents.validator.fulfillment_checker import (
    FulfillmentChecker,
    FulfillmentStatus,
    FulfillmentResult,
    FulfillmentGap,
    EntityFulfillment,
    GapSeverity,
    create_fulfillment_checker,
)
from src.agents.validator.graph_integrator import (
    GraphIntegrator,
    GraphOperation,
    IntegrationResult,
    create_graph_integrator,
)
from src.agents.validator.tools import ValidatorAgentTools
from src.agents.validator.cache_reader import (
    RequirementCacheReader,
    create_cache_reader,
)

__all__ = [
    # Main agent
    "ValidatorAgent",
    "ValidatorAgentState",
    "create_validator_agent",
    # Relevance analysis
    "RelevanceAnalyzer",
    "RelevanceDecision",
    "RelevanceResult",
    "create_relevance_analyzer",
    # Fulfillment checking
    "FulfillmentChecker",
    "FulfillmentStatus",
    "FulfillmentResult",
    "FulfillmentGap",
    "EntityFulfillment",
    "GapSeverity",
    "create_fulfillment_checker",
    # Graph integration
    "GraphIntegrator",
    "GraphOperation",
    "IntegrationResult",
    "create_graph_integrator",
    # Tools
    "ValidatorAgentTools",
    # Cache operations
    "RequirementCacheReader",
    "create_cache_reader",
]
