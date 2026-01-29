"""Requirement management tools for the Universal Agent.

Provides operations for managing requirements in PostgreSQL:
- add_requirement: Create and submit finalized requirements for validation
- list_requirements: Browse and filter existing requirements
- get_requirement: Retrieve full details of a specific requirement

Used by the Creator Agent to manage requirements before the Validator
Agent integrates them into Neo4j.
"""

import json
import logging
import uuid
from typing import List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
# Phase availability: domain tools are tactical-only
CACHE_TOOLS_METADATA = {
    "add_requirement": {
        "module": "cache_tools",
        "function": "add_requirement",
        "description": "Create and submit a finalized requirement for validation",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Submit a finalized requirement for validation by the Validator Agent.",
        "phases": ["tactical"],
    },
    "list_requirements": {
        "module": "cache_tools",
        "function": "list_requirements",
        "description": "List requirements from the database with optional filters",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "List requirements with filters (status, job_id, limit).",
        "phases": ["tactical"],
    },
    "get_requirement": {
        "module": "cache_tools",
        "function": "get_requirement",
        "description": "Get full details of a specific requirement by ID",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Get complete details of a requirement by ID.",
        "phases": ["tactical"],
    },
    "edit_requirement": {
        "module": "cache_tools",
        "function": "edit_requirement",
        "description": "Edit content fields of an existing pending requirement",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Edit fields of a pending requirement (text, name, type, etc.).",
        "phases": ["tactical"],
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
    async def add_requirement(
        text: str,
        name: str,
        req_type: str = "functional",
        priority: str = "medium",
        gobd_relevant: bool = False,
        gdpr_relevant: bool = False,
        source_document: Optional[str] = None,
        source_location: Optional[str] = None,
        citations: Optional[str] = None,
        reasoning: Optional[str] = None,
    ) -> str:
        """Create and submit a finalized requirement for validation.

        Use this tool when you have finished processing a requirement and are
        confident it is ready for the Validator Agent to integrate into Neo4j.

        Args:
            text: Full requirement text
            name: Short name/title (max 500 chars)
            req_type: Type (functional, compliance, constraint, non_functional)
            priority: Priority (high, medium, low)
            gobd_relevant: GoBD relevance flag
            gdpr_relevant: GDPR relevance flag
            source_document: Source document path
            source_location: Location in document (e.g., "Section 3.2")
            citations: Comma-separated citation IDs
            reasoning: Extraction reasoning

        Returns:
            "ok: {uuid}" on success, "error: {reason}" on failure
        """
        try:
            # Use new PostgresDB namespace method
            if not context.db:
                return "error: no database connection"

            if not context.job_id:
                return "error: no job context"

            # Parse comma-separated lists
            citation_list = [c.strip() for c in (citations or "").split(",") if c.strip()]

            # Parse source_location into structured format
            source_location_dict = {"section": source_location} if source_location else None

            # Use PostgresDB.requirements.create()
            req_uuid = await context.db.requirements.create(
                job_id=uuid.UUID(context.job_id),
                text=text,
                name=name,
                req_type=req_type,
                priority=priority,
                source_document=source_document,
                source_location=source_location_dict,
                gobd_relevant=gobd_relevant,
                gdpr_relevant=gdpr_relevant,
                citations=citation_list,
                reasoning=reasoning,
            )

            return f"ok: {req_uuid}"

        except Exception as e:
            logger.error(f"Error adding requirement: {e}")
            return f"error: {str(e)}"

    @tool
    async def list_requirements(
        status: Optional[str] = None,
        job_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0
    ) -> str:
        """List requirements from the database with optional filters.

        Use this tool to browse existing requirements and check for duplicates
        before adding new requirements.

        Args:
            status: Filter by status (pending, validating, integrated, rejected, failed)
            job_id: Filter by job ID (defaults to current job if not specified)
            limit: Maximum number of results (default 20, max 100)
            offset: Number of results to skip for pagination

        Returns:
            Formatted list of requirements with key fields
        """
        try:
            # Use new PostgresDB connection
            if not context.db:
                return "Error: No database connection available"

            # Build query with filters
            conditions = []
            params = []
            param_idx = 1

            # Use current job_id if not specified
            effective_job_id = job_id or (str(context.job_id) if context.job_id else None)
            if effective_job_id:
                conditions.append(f"job_id = ${param_idx}::uuid")
                params.append(effective_job_id)
                param_idx += 1

            if status:
                conditions.append(f"status = ${param_idx}")
                params.append(status)
                param_idx += 1

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            # Clamp limit
            limit = min(max(1, limit), 100)

            query = f"""
                SELECT id, name, type, priority, status, confidence,
                       gobd_relevant, gdpr_relevant, created_at, neo4j_id
                FROM requirements
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ${param_idx} OFFSET ${param_idx + 1}
            """
            params.extend([limit, offset])

            rows = await context.db.fetch(query, *params)

            if not rows:
                filter_desc = []
                if effective_job_id:
                    filter_desc.append(f"job_id={effective_job_id}")
                if status:
                    filter_desc.append(f"status={status}")
                filter_str = f" (filters: {', '.join(filter_desc)})" if filter_desc else ""
                return f"No requirements found{filter_str}."

            result = f"Requirements Found: {len(rows)}\n"
            if offset > 0:
                result += f"(showing {offset + 1} to {offset + len(rows)})\n"
            result += "\n"

            for row in rows:
                integrated = "[NEO4J]" if row["neo4j_id"] else ""
                gobd = "[GoBD]" if row["gobd_relevant"] else ""
                gdpr = "[GDPR]" if row["gdpr_relevant"] else ""
                flags = " ".join(filter(None, [integrated, gobd, gdpr]))

                result += f"ID: {row['id']}\n"
                result += f"  Name: {row['name']}\n"
                result += f"  Type: {row['type']} | Priority: {row['priority']} | Status: {row['status']}\n"
                result += f"  Confidence: {row['confidence']:.2f} {flags}\n"
                result += f"  Created: {row['created_at']}\n\n"

            result += f"Use get_requirement(id) for full details."

            return result

        except Exception as e:
            logger.error(f"Error listing requirements: {e}")
            return f"Error listing requirements: {str(e)}"

    @tool
    async def get_requirement(requirement_id: str) -> str:
        """Get full details of a specific requirement by ID.

        Args:
            requirement_id: UUID of the requirement to retrieve

        Returns:
            Complete requirement details including text, source, and validation status
        """
        try:
            # Use new PostgresDB namespace method
            if not context.db:
                return "Error: No database connection available"

            # Use PostgresDB.requirements.get()
            requirement = await context.db.requirements.get(uuid.UUID(requirement_id))

            if not requirement:
                return f"Requirement not found: {requirement_id}"

            # Format the output
            result = f"""Requirement Details
{'=' * 50}

Identification:
  ID: {requirement['id']}
  Job ID: {requirement['job_id']}
  Created: {requirement['created_at']}
  Updated: {requirement.get('updated_at', 'N/A')}

Content:
  Name: {requirement['name']}
  Type: {requirement['type']}
  Priority: {requirement['priority']}
  Confidence: {requirement['confidence']:.2f}

Full Text:
{requirement['text']}

Source:
  Document: {requirement.get('source_document', 'N/A')}
  Location: {requirement.get('source_location', 'N/A')}

Compliance:
  GoBD Relevant: {requirement['gobd_relevant']}
  GDPR Relevant: {requirement['gdpr_relevant']}

Extraction:
  Reasoning: {requirement.get('reasoning', 'N/A')}
  Citations: {requirement.get('citations', '[]')}

Validation:
  Status: {requirement['status']}
  Neo4j ID: {requirement.get('neo4j_id', 'Not integrated')}
  Validated At: {requirement.get('validated_at', 'N/A')}
  Validation Result: {requirement.get('validation_result', 'N/A')}
  Rejection Reason: {requirement.get('rejection_reason', 'N/A')}
  Retry Count: {requirement.get('retry_count', 0)}
"""

            return result

        except Exception as e:
            logger.error(f"Error getting requirement: {e}")
            return f"Error getting requirement: {str(e)}"

    @tool
    async def edit_requirement(
        requirement_id: str,
        text: Optional[str] = None,
        name: Optional[str] = None,
        req_type: Optional[str] = None,
        priority: Optional[str] = None,
        gobd_relevant: Optional[bool] = None,
        gdpr_relevant: Optional[bool] = None,
        source_document: Optional[str] = None,
        source_location: Optional[str] = None,
        citations: Optional[str] = None,
        reasoning: Optional[str] = None,
        research_notes: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> str:
        """Edit content fields of an existing pending requirement.

        Only requirements with status='pending' can be edited. Protected fields
        (id, job_id, status, neo4j_id, validation_result, etc.) cannot be changed.
        Provide only the fields you want to update.

        Args:
            requirement_id: UUID of the requirement to edit
            text: Full requirement text
            name: Short name/title (max 500 chars)
            req_type: Type (functional, compliance, constraint, non_functional)
            priority: Priority (high, medium, low)
            gobd_relevant: GoBD relevance flag
            gdpr_relevant: GDPR relevance flag
            source_document: Source document path
            source_location: Location in document (e.g., "Section 3.2")
            citations: Comma-separated citation IDs (replaces existing)
            reasoning: Extraction reasoning
            research_notes: Research notes
            tags: Comma-separated tags (replaces existing)

        Returns:
            "ok: edited {uuid}" on success, "error: {reason}" on failure
        """
        try:
            if not context.db:
                return "error: no database connection"

            # Build kwargs for edit_content, only including non-None values
            kwargs = {}
            if text is not None:
                kwargs["text"] = text
            if name is not None:
                kwargs["name"] = name
            if req_type is not None:
                kwargs["req_type"] = req_type
            if priority is not None:
                kwargs["priority"] = priority
            if gobd_relevant is not None:
                kwargs["gobd_relevant"] = gobd_relevant
            if gdpr_relevant is not None:
                kwargs["gdpr_relevant"] = gdpr_relevant
            if source_document is not None:
                kwargs["source_document"] = source_document
            if source_location is not None:
                kwargs["source_location"] = {"section": source_location}
            if citations is not None:
                kwargs["citations"] = [c.strip() for c in citations.split(",") if c.strip()]
            if reasoning is not None:
                kwargs["reasoning"] = reasoning
            if research_notes is not None:
                kwargs["research_notes"] = research_notes
            if tags is not None:
                kwargs["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

            if not kwargs:
                return "error: no fields provided to edit"

            await context.db.requirements.edit_content(
                requirement_uuid=uuid.UUID(requirement_id),
                **kwargs
            )

            return f"ok: edited {requirement_id}"

        except ValueError as e:
            return f"error: {str(e)}"
        except Exception as e:
            logger.error(f"Error editing requirement: {e}")
            return f"error: {str(e)}"

    return [
        add_requirement,
        list_requirements,
        get_requirement,
        edit_requirement,
    ]
