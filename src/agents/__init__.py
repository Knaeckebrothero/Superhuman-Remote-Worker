"""
Agent implementations for requirement analysis and document ingestion.

This module provides both legacy pipeline agents and the new two-agent
autonomous system (Creator + Validator).
"""

from src.agents.graph_agent import RequirementGraphAgent, create_graph_agent

# Document ingestion pipeline agents (legacy)
from src.agents.document_processor_agent import (
    DocumentProcessorAgent,
    create_document_processor_agent,
)
from src.agents.requirement_extractor_agent import (
    RequirementExtractorAgent,
    create_requirement_extractor_agent,
)
from src.agents.requirement_validator_agent import (
    RequirementValidatorAgent,
    create_requirement_validator_agent,
)
from src.agents.document_ingestion_supervisor import (
    DocumentIngestionSupervisor,
    create_document_ingestion_supervisor,
)

# Two-Agent Autonomous System (Phase 2+)
from src.agents.creator import (
    CreatorAgent,
    CreatorAgentState,
    create_creator_agent,
    CreatorDocumentProcessor,
    CandidateExtractor,
    Researcher,
    RequirementCacheWriter,
    CreatorAgentTools,
)

# Validator Agent (Phase 3)
from src.agents.validator import (
    ValidatorAgent,
    ValidatorAgentState,
    create_validator_agent,
    RelevanceAnalyzer,
    RelevanceDecision,
    FulfillmentChecker,
    FulfillmentStatus,
    GraphIntegrator,
    ValidatorAgentTools,
    RequirementCacheReader,
)

__all__ = [
    # Original requirement analysis agent
    'RequirementGraphAgent',
    'create_graph_agent',
    # Document ingestion pipeline agents (legacy)
    'DocumentProcessorAgent',
    'create_document_processor_agent',
    'RequirementExtractorAgent',
    'create_requirement_extractor_agent',
    'RequirementValidatorAgent',
    'create_requirement_validator_agent',
    'DocumentIngestionSupervisor',
    'create_document_ingestion_supervisor',
    # Creator Agent (two-agent system)
    'CreatorAgent',
    'CreatorAgentState',
    'create_creator_agent',
    'CreatorDocumentProcessor',
    'CandidateExtractor',
    'Researcher',
    'RequirementCacheWriter',
    'CreatorAgentTools',
    # Validator Agent (two-agent system)
    'ValidatorAgent',
    'ValidatorAgentState',
    'create_validator_agent',
    'RelevanceAnalyzer',
    'RelevanceDecision',
    'FulfillmentChecker',
    'FulfillmentStatus',
    'GraphIntegrator',
    'ValidatorAgentTools',
    'RequirementCacheReader',
]
