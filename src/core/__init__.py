"""
Core utilities for database connectivity and data processing.
"""

from src.core.neo4j_utils import Neo4jConnection, create_neo4j_connection
from src.core.csv_processor import RequirementProcessor, load_requirements_from_env
from src.core.metamodel_validator import (
    MetamodelValidator,
    ComplianceReport,
    CheckResult,
    Severity,
)
from src.core.document_processor import (
    DocumentProcessor,
    DocumentExtractor,
    DocumentChunker,
)
from src.core.document_models import (
    DocumentChunk,
    DocumentMetadata,
    RequirementCandidate,
    ValidatedRequirement,
    ProcessingOptions,
    PipelineReport,
)

__all__ = [
    # Neo4j
    'Neo4jConnection',
    'create_neo4j_connection',
    # CSV Processing
    'RequirementProcessor',
    'load_requirements_from_env',
    # Metamodel Validation
    'MetamodelValidator',
    'ComplianceReport',
    'CheckResult',
    'Severity',
    # Document Processing
    'DocumentProcessor',
    'DocumentExtractor',
    'DocumentChunker',
    # Document Models
    'DocumentChunk',
    'DocumentMetadata',
    'RequirementCandidate',
    'ValidatedRequirement',
    'ProcessingOptions',
    'PipelineReport',
]
