"""
Core utilities for data processing and validation.
"""

from .metamodel_validator import (
    MetamodelValidator,
    ComplianceReport,
    CheckResult,
    Severity,
)
from .document_processor import (
    DocumentProcessor,
    DocumentExtractor,
    DocumentChunker,
)
from .document_models import (
    DocumentChunk,
    DocumentMetadata,
    RequirementCandidate,
    ValidatedRequirement,
    ProcessingOptions,
    PipelineReport,
)
from .config import load_config, load_prompt, get_project_root

# Citation utilities (optional - requires citation_engine)
try:
    from .citation_utils import (  # noqa: F401
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

# Add citation exports if available
if _citation_available:
    __all__.extend([
        'CitationHelper',
        'create_citation_engine',
        'is_citation_engine_available',
        'get_citation_engine_config',
        'create_citation_tools',
    ])
