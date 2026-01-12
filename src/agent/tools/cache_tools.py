"""Requirement management tools for the Universal Agent.

Provides operations for managing requirements in PostgreSQL:
- add_requirement: Create and submit finalized requirements for validation
- list_requirements: Browse and filter existing requirements
- get_requirement: Retrieve full details of a specific requirement

Used by the Creator Agent to manage requirements before the Validator
Agent integrates them into Neo4j.
"""

import asyncio
import json
import logging
import uuid
from typing import List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
CACHE_TOOLS_METADATA = {
    "add_requirement": {
        "module": "cache_tools",
        "function": "add_requirement",
        "description": "Create and submit a finalized requirement for validation",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Submit a finalized requirement for validation by the Validator Agent.",
    },
    "list_requirements": {
        "module": "cache_tools",
        "function": "list_requirements",
        "description": "List requirements from the database with optional filters",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "List requirements with filters (status, job_id, limit).",
    },
    "get_requirement": {
        "module": "cache_tools",
        "function": "get_requirement",
        "description": "Get full details of a specific requirement by ID",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Get complete details of a requirement by ID.",
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
    def add_requirement(
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
        """Create and submit a finalized requirement for validation.

        Use this tool when you have finished processing a requirement and are
        confident it is ready for the Validator Agent to integrate into Neo4j.

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
            "ok: {uuid}" on success, "error: {reason}" on failure
        """
        try:
            if not context.postgres_conn:
                return "error: no database connection"

            if not context.job_id:
                return "error: no job context"

            # Parse comma-separated lists
            citation_list = [c.strip() for c in (citations or "").split(",") if c.strip()]
            object_list = [o.strip() for o in (mentioned_objects or "").split(",") if o.strip()]
            message_list = [m.strip() for m in (mentioned_messages or "").split(",") if m.strip()]

            # Generate requirement ID
            req_id = str(uuid.uuid4())

            # Insert into requirements table
            query = """
                INSERT INTO requirements (
                    id, job_id, text, name, type, priority,
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

            # Serialize JSONB fields - asyncpg requires JSON strings for JSONB columns
            source_location_json = json.dumps({"section": source_location}) if source_location else None
            citations_json = json.dumps(citation_list)
            objects_json = json.dumps(object_list)
            messages_json = json.dumps(message_list)

            result = asyncio.run(context.postgres_conn.fetchrow(
                query,
                req_id,
                context.job_id,
                text,
                name[:80],  # Ensure name fits
                req_type,
                priority,
                source_document,
                source_location_json,
                gobd_relevant,
                gdpr_relevant,
                citations_json,
                objects_json,
                messages_json,
                reasoning,
                confidence,
            ))

            if result:
                return f"ok: {req_id}"
            else:
                return "error: requirement not created"

        except Exception as e:
            logger.error(f"Error adding requirement: {e}")
            return f"error: {str(e)}"

    @tool
    def list_requirements(
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
            if not context.postgres_conn:
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

            rows = asyncio.run(context.postgres_conn.fetch(query, *params))

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
    def get_requirement(requirement_id: str) -> str:
        """Get full details of a specific requirement by ID.

        Args:
            requirement_id: UUID of the requirement to retrieve

        Returns:
            Complete requirement details including text, source, and validation status
        """
        try:
            if not context.postgres_conn:
                return "Error: No database connection available"

            query = """
                SELECT *
                FROM requirements
                WHERE id = $1::uuid
                LIMIT 1
            """

            row = asyncio.run(context.postgres_conn.fetchrow(query, requirement_id))

            if not row:
                return f"Requirement not found: {requirement_id}"

            # Format the output
            result = f"""Requirement Details
{'=' * 50}

Identification:
  ID: {row['id']}
  Job ID: {row['job_id']}
  Created: {row['created_at']}
  Updated: {row.get('updated_at', 'N/A')}

Content:
  Name: {row['name']}
  Type: {row['type']}
  Priority: {row['priority']}
  Confidence: {row['confidence']:.2f}

Full Text:
{row['text']}

Source:
  Document: {row.get('source_document', 'N/A')}
  Location: {row.get('source_location', 'N/A')}

Compliance:
  GoBD Relevant: {row['gobd_relevant']}
  GDPR Relevant: {row['gdpr_relevant']}

Extraction:
  Reasoning: {row.get('reasoning', 'N/A')}
  Citations: {row.get('citations', '[]')}
  Mentioned Objects: {row.get('mentioned_objects', '[]')}
  Mentioned Messages: {row.get('mentioned_messages', '[]')}

Validation:
  Status: {row['status']}
  Neo4j ID: {row.get('neo4j_id', 'Not integrated')}
  Validated At: {row.get('validated_at', 'N/A')}
  Validation Result: {row.get('validation_result', 'N/A')}
  Rejection Reason: {row.get('rejection_reason', 'N/A')}
  Retry Count: {row.get('retry_count', 0)}
"""

            return result

        except Exception as e:
            logger.error(f"Error getting requirement: {e}")
            return f"Error getting requirement: {str(e)}"

    return [
        add_requirement,
        list_requirements,
        get_requirement,
    ]
