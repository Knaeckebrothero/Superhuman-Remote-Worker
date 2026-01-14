"""Citation Engine integration utilities.

This module provides helper functions for integrating the Citation Engine
with the Graph-RAG agent system. The Citation Engine is an external pip
package that provides source verification and citation management.
"""

import os
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Flag to track if Citation Engine is available
_citation_engine_available = False

try:
    from citation_engine import CitationEngine, Citation, Source
    _citation_engine_available = True
except ImportError:
    CitationEngine = None
    Citation = None
    Source = None
    logger.warning(
        "Citation Engine not installed. "
        "Install with: pip install -e ./citation_tool[full]"
    )


def is_citation_engine_available() -> bool:
    """Check if the Citation Engine is available.

    Returns:
        True if Citation Engine is installed and importable
    """
    return _citation_engine_available


def get_citation_engine_config() -> Dict[str, Any]:
    """Get Citation Engine configuration from environment variables.

    Returns:
        Configuration dictionary for Citation Engine
    """
    return {
        "db_url": os.getenv("CITATION_DB_URL", os.getenv("DATABASE_URL")),
        "llm_url": os.getenv("CITATION_LLM_URL", os.getenv("LLM_BASE_URL")),
        "llm_model": os.getenv("CITATION_LLM_MODEL", "gpt-4"),
        "reasoning_required": os.getenv("CITATION_REASONING_REQUIRED", "low"),
    }


async def create_citation_engine(
    config: Optional[Dict[str, Any]] = None
) -> Optional[Any]:
    """Create and initialize a Citation Engine instance.

    Args:
        config: Optional configuration dictionary. If not provided,
                reads from environment variables.

    Returns:
        CitationEngine instance or None if not available
    """
    if not _citation_engine_available:
        logger.warning("Citation Engine not available")
        return None

    config = config or get_citation_engine_config()

    try:
        engine = CitationEngine(
            db_url=config.get("db_url"),
            llm_url=config.get("llm_url"),
            llm_model=config.get("llm_model"),
            reasoning_level=config.get("reasoning_required", "low"),
        )
        await engine.initialize()
        logger.info("Citation Engine initialized successfully")
        return engine
    except Exception as e:
        logger.error(f"Failed to initialize Citation Engine: {e}")
        return None


class CitationHelper:
    """Helper class for common citation operations.

    Provides simplified methods for creating and verifying citations
    during agent operations.

    Example:
        ```python
        helper = CitationHelper(citation_engine)

        # Create citation from document
        citation = await helper.cite_document(
            document_path="/path/to/gdpr.pdf",
            quote="Personal data shall be processed lawfully",
            claim="GDPR requires lawful data processing",
            page=10
        )

        # Create citation from web search
        citation = await helper.cite_web(
            url="https://example.com/gdpr-guide",
            quote="Article 6 defines lawfulness",
            claim="Article 6 defines lawful processing bases"
        )

        # Verify a citation
        is_valid = await helper.verify(citation.id)
        ```
    """

    def __init__(self, engine: Any):
        """Initialize citation helper.

        Args:
            engine: CitationEngine instance
        """
        self.engine = engine

    async def cite_document(
        self,
        document_path: str,
        quote: str,
        claim: str,
        page: Optional[int] = None,
        section: Optional[str] = None,
        article: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Create a citation from a document source.

        Args:
            document_path: Path to source document
            quote: Exact quote from document
            claim: Claim being supported
            page: Optional page number
            section: Optional section identifier
            article: Optional article number
            metadata: Optional additional metadata

        Returns:
            Citation ID or None if failed
        """
        if not self.engine:
            return None

        try:
            source_metadata = {
                "type": "document",
                "path": document_path,
                **({"page": page} if page else {}),
                **({"section": section} if section else {}),
                **({"article": article} if article else {}),
                **(metadata or {}),
            }

            citation = await self.engine.create_citation(
                source_path=document_path,
                quote=quote,
                claim=claim,
                metadata=source_metadata,
            )

            logger.debug(f"Created document citation: {citation.id}")
            return citation.id

        except Exception as e:
            logger.error(f"Failed to create document citation: {e}")
            return None

    async def cite_web(
        self,
        url: str,
        quote: str,
        claim: str,
        accessed_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Create a citation from a web source.

        Args:
            url: Source URL
            quote: Exact quote from page
            claim: Claim being supported
            accessed_at: Optional access timestamp
            metadata: Optional additional metadata

        Returns:
            Citation ID or None if failed
        """
        if not self.engine:
            return None

        try:
            source_metadata = {
                "type": "web",
                "url": url,
                **({"accessed_at": accessed_at} if accessed_at else {}),
                **(metadata or {}),
            }

            citation = await self.engine.create_citation(
                source_url=url,
                quote=quote,
                claim=claim,
                metadata=source_metadata,
            )

            logger.debug(f"Created web citation: {citation.id}")
            return citation.id

        except Exception as e:
            logger.error(f"Failed to create web citation: {e}")
            return None

    async def cite_database(
        self,
        query: str,
        result_summary: str,
        claim: str,
        database: str = "neo4j",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Create a citation from a database query.

        Args:
            query: Database query executed
            result_summary: Summary of query results
            claim: Claim being supported
            database: Database name
            metadata: Optional additional metadata

        Returns:
            Citation ID or None if failed
        """
        if not self.engine:
            return None

        try:
            source_metadata = {
                "type": "database",
                "database": database,
                "query": query,
                "result_summary": result_summary,
                **(metadata or {}),
            }

            citation = await self.engine.create_citation(
                source_type="database_query",
                quote=result_summary,
                claim=claim,
                metadata=source_metadata,
            )

            logger.debug(f"Created database citation: {citation.id}")
            return citation.id

        except Exception as e:
            logger.error(f"Failed to create database citation: {e}")
            return None

    async def verify(self, citation_id: str) -> bool:
        """Verify a citation.

        Args:
            citation_id: ID of citation to verify

        Returns:
            True if citation is valid
        """
        if not self.engine:
            return False

        try:
            result = await self.engine.verify_citation(citation_id)
            return result.is_valid

        except Exception as e:
            logger.error(f"Failed to verify citation {citation_id}: {e}")
            return False

    async def get_citation(self, citation_id: str) -> Optional[Dict[str, Any]]:
        """Get citation details.

        Args:
            citation_id: Citation ID

        Returns:
            Citation details or None
        """
        if not self.engine:
            return None

        try:
            citation = await self.engine.get_citation(citation_id)
            if citation:
                return {
                    "id": citation.id,
                    "quote": citation.quote,
                    "claim": citation.claim,
                    "source": citation.source,
                    "verified": citation.verified,
                    "verification_result": citation.verification_result,
                    "created_at": citation.created_at,
                }
            return None

        except Exception as e:
            logger.error(f"Failed to get citation {citation_id}: {e}")
            return None

    async def list_citations(
        self,
        job_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """List citations, optionally filtered by job.

        Args:
            job_id: Optional job ID filter
            limit: Maximum number to return

        Returns:
            List of citation summaries
        """
        if not self.engine:
            return []

        try:
            citations = await self.engine.list_citations(
                job_id=job_id,
                limit=limit,
            )
            return [
                {
                    "id": c.id,
                    "claim": c.claim[:100] + "..." if len(c.claim) > 100 else c.claim,
                    "verified": c.verified,
                    "source_type": c.source.get("type") if c.source else None,
                }
                for c in citations
            ]

        except Exception as e:
            logger.error(f"Failed to list citations: {e}")
            return []


def create_citation_tools(helper: CitationHelper) -> List[Dict[str, Any]]:
    """Create tool function definitions for LangGraph agents.

    Returns tool definitions that can be added to an agent's toolkit
    for managing citations.

    Args:
        helper: CitationHelper instance

    Returns:
        List of tool definitions
    """
    async def cite_document(
        document_path: str,
        quote: str,
        claim: str,
        page: int = None,
        section: str = None
    ) -> str:
        """Create a citation from a document source."""
        citation_id = await helper.cite_document(
            document_path=document_path,
            quote=quote,
            claim=claim,
            page=page,
            section=section,
        )
        if citation_id:
            return f"Created citation: {citation_id}"
        return "Failed to create citation"

    async def cite_web(url: str, quote: str, claim: str) -> str:
        """Create a citation from a web source."""
        citation_id = await helper.cite_web(url=url, quote=quote, claim=claim)
        if citation_id:
            return f"Created citation: {citation_id}"
        return "Failed to create citation"

    async def verify_citation(citation_id: str) -> str:
        """Verify a citation."""
        is_valid = await helper.verify(citation_id)
        return f"Citation {citation_id} is {'valid' if is_valid else 'invalid'}"

    return [
        {
            "func": cite_document,
            "name": "cite_document",
            "description": "Create a citation from a document source",
        },
        {
            "func": cite_web,
            "name": "cite_web",
            "description": "Create a citation from a web source",
        },
        {
            "func": verify_citation,
            "name": "verify_citation",
            "description": "Verify a citation is valid",
        },
    ]
