"""Cache tools for the Universal Agent.

Provides operations for writing to the requirement cache (PostgreSQL).
Used by the Creator Agent to pass requirements to the Validator Agent.
"""

import logging
import uuid
from typing import List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
CACHE_TOOLS_METADATA = {
    "write_requirement_to_cache": {
        "module": "cache_tools",
        "function": "write_requirement_to_cache",
        "description": "Write a requirement to the PostgreSQL cache for validation",
        "category": "domain",
    },
}


def create_cache_tools(context: ToolContext) -> List:
    """Create cache tools with injected context.

    Args:
        context: ToolContext with dependencies

    Returns:
        List of LangChain tool functions
    """

    @tool
    async def write_requirement_to_cache(
        text: str,
        name: str,
        req_type: str = "functional",
        priority: str = "medium",
        gobd_relevant: bool = False,
        gdpr_relevant: bool = False,
        source_document: Optional[str] = None,
        source_location: Optional[str] = None,
        citations: Optional[str] = None,
        mentioned_objects: Optional[str] = None,
        mentioned_messages: Optional[str] = None,
        reasoning: Optional[str] = None,
        confidence: float = 0.8
    ) -> str:
        """Write a requirement to the PostgreSQL cache for validation.

        Args:
            text: Full requirement text
            name: Short name/title (max 80 chars)
            req_type: Type (functional, compliance, constraint, non_functional)
            priority: Priority (high, medium, low)
            gobd_relevant: GoBD relevance flag
            gdpr_relevant: GDPR relevance flag
            source_document: Source document path
            source_location: Location in document (e.g., "Section 3.2")
            citations: Comma-separated citation IDs
            mentioned_objects: Comma-separated BusinessObject names
            mentioned_messages: Comma-separated Message names
            reasoning: Extraction reasoning
            confidence: Confidence score (0.0-1.0)

        Returns:
            Requirement ID and confirmation
        """
        try:
            if not context.postgres_conn:
                return "Error: No database connection available"

            if not context.job_id:
                return "Error: No active job context"

            # Parse comma-separated lists
            citation_list = [c.strip() for c in (citations or "").split(",") if c.strip()]
            object_list = [o.strip() for o in (mentioned_objects or "").split(",") if o.strip()]
            message_list = [m.strip() for m in (mentioned_messages or "").split(",") if m.strip()]

            # Generate requirement ID
            req_id = str(uuid.uuid4())

            # Insert into requirement_cache table
            query = """
                INSERT INTO requirement_cache (
                    id, job_id, text, name, req_type, priority,
                    source_document, source_location,
                    gobd_relevant, gdpr_relevant,
                    citations, mentioned_objects, mentioned_messages,
                    reasoning, confidence, status,
                    created_at
                ) VALUES (
                    $1::uuid, $2::uuid, $3, $4, $5, $6,
                    $7, $8,
                    $9, $10,
                    $11, $12, $13,
                    $14, $15, 'pending',
                    NOW()
                )
                RETURNING id
            """

            result = await context.postgres_conn.fetchrow(
                query,
                req_id,
                context.job_id,
                text,
                name[:80],  # Ensure name fits
                req_type,
                priority,
                source_document,
                {"section": source_location} if source_location else None,
                gobd_relevant,
                gdpr_relevant,
                citation_list,
                object_list,
                message_list,
                reasoning,
                confidence,
            )

            if result:
                return f"""Requirement Written to Cache

Requirement ID: {req_id}
Name: {name}
Type: {req_type}
Priority: {priority}
Confidence: {confidence:.2f}

GoBD Relevant: {gobd_relevant}
GDPR Relevant: {gdpr_relevant}

Status: pending (awaiting validation)

The Validator Agent will process this requirement next."""
            else:
                return "Error: Requirement not created"

        except Exception as e:
            logger.error(f"Cache write error: {e}")
            return f"Error writing requirement: {str(e)}"

    return [
        write_requirement_to_cache,
    ]
