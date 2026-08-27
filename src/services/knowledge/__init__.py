"""Knowledge-base ingestion services (OKF KB slice 2 PR2).

The KB-write-path counterpart to ``src/services/memory/``: the ingestion
verdict gate that adjudicates curation candidates against their nearest
existing notes before any ``kb_write``/``kb_update``. See
knowledge-base/knowledge/features/okf_knowledge_base.md §3 (curator refactor) and §11 (slice 2).
"""

from src.services.knowledge.ingestion import (
    KnowledgeVerdictService,
    build_knowledge_verdict_service,
)

__all__ = ["KnowledgeVerdictService", "build_knowledge_verdict_service"]
