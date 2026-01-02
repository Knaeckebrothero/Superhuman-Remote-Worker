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
from src.core.config import load_config, load_prompt, get_project_root

# PostgreSQL utilities (optional - requires asyncpg)
try:
    from src.core.postgres_utils import (
        PostgresConnection,
        create_postgres_connection,
        create_job,
        get_job,
        update_job_status,
        create_requirement,
        get_pending_requirement,
        update_requirement_status,
        count_requirements_by_status,
        log_llm_request,
        save_checkpoint,
        get_latest_checkpoint,
        save_workspace_data,
        get_workspace_data,
        update_workspace_data,
    )
    _postgres_available = True
except ImportError:
    _postgres_available = False

# Citation utilities (optional - requires citation_engine)
try:
    from src.core.citation_utils import (
        CitationHelper,
        create_citation_engine,
        is_citation_engine_available,
        get_citation_engine_config,
        create_citation_tools,
    )
    _citation_available = True
except ImportError:
    _citation_available = False

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
    # Configuration
    'load_config',
    'load_prompt',
    'get_project_root',
]

# Add PostgreSQL exports if available
if _postgres_available:
    __all__.extend([
        'PostgresConnection',
        'create_postgres_connection',
        'create_job',
        'get_job',
        'update_job_status',
        'create_requirement',
        'get_pending_requirement',
        'update_requirement_status',
        'count_requirements_by_status',
        'log_llm_request',
        'save_checkpoint',
        'get_latest_checkpoint',
        'save_workspace_data',
        'get_workspace_data',
        'update_workspace_data',
    ])

# Add citation exports if available
if _citation_available:
    __all__.extend([
        'CitationHelper',
        'create_citation_engine',
        'is_citation_engine_available',
        'get_citation_engine_config',
        'create_citation_tools',
    ])
