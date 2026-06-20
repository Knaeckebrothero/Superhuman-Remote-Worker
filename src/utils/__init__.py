"""
Core utilities for data processing and validation.
"""

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

# NOTE: citation integration lives in the vendored ``citation_engine`` package
# (constructed via ``ToolContext.get_citation_engine`` on the vector pool) and
# the agent tools in ``src.tools.citation``. The former ``citation_utils``
# helper module was dead/fictional (it referenced an engine API that never
# existed) and was removed in the citation-engine native integration (Phase 1).

__all__ = [
    # Document Processing
    "DocumentProcessor",
    "DocumentExtractor",
    "DocumentChunker",
    # Document Models
    "DocumentChunk",
    "DocumentMetadata",
    "RequirementCandidate",
    "ValidatedRequirement",
    "ProcessingOptions",
    "PipelineReport",
    # Configuration
    "load_config",
    "load_prompt",
    "get_project_root",
]
