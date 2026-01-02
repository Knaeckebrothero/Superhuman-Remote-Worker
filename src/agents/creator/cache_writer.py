"""Requirement Cache Writer for Creator Agent.

Handles writing extracted requirements to the PostgreSQL cache
for validation by the Validator Agent.
"""

import logging
import uuid
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class RequirementCacheWriter:
    """Writes requirements to the PostgreSQL shared cache.

    Provides validation, duplicate detection, and proper formatting
    before writing requirements to the cache table.
    """

    def __init__(self, postgres_conn: Any):
        """Initialize the cache writer.

        Args:
            postgres_conn: PostgreSQL connection instance
        """
        self.postgres_conn = postgres_conn

    async def write_requirement(
        self,
        job_id: str,
        text: str,
        name: str,
        req_type: str = "functional",
        priority: str = "medium",
        source_document: Optional[str] = None,
        source_location: Optional[Dict] = None,
        gobd_relevant: bool = False,
        gdpr_relevant: bool = False,
        citations: Optional[List[str]] = None,
        mentioned_objects: Optional[List[str]] = None,
        mentioned_messages: Optional[List[str]] = None,
        reasoning: Optional[str] = None,
        research_notes: Optional[str] = None,
        confidence: float = 0.8,
        tags: Optional[List[str]] = None
    ) -> Optional[str]:
        """Write a requirement to the cache.

        Args:
            job_id: Parent job UUID
            text: Requirement text
            name: Short name/title
            req_type: Type (functional, compliance, constraint, non_functional)
            priority: Priority (high, medium, low)
            source_document: Source document path
            source_location: Location in document
            gobd_relevant: GoBD relevance flag
            gdpr_relevant: GDPR relevance flag
            citations: List of citation IDs
            mentioned_objects: Referenced BusinessObject names
            mentioned_messages: Referenced Message names
            reasoning: Extraction reasoning
            research_notes: Research context notes
            confidence: Confidence score (0.0-1.0)
            tags: Traceability tags

        Returns:
            Requirement ID if successful, None otherwise
        """
        # Validate input
        validation_error = self._validate_requirement(text, name, req_type, confidence)
        if validation_error:
            logger.warning(f"Requirement validation failed: {validation_error}")
            return None

        # Check for duplicates
        is_duplicate = await self._check_duplicate(job_id, text)
        if is_duplicate:
            logger.info(f"Duplicate requirement detected, skipping: {name}")
            return None

        try:
            from src.core.postgres_utils import create_requirement

            req_id = await create_requirement(
                conn=self.postgres_conn,
                job_id=uuid.UUID(job_id),
                text=text,
                name=name,
                req_type=req_type,
                priority=priority,
                source_document=source_document,
                source_location=source_location,
                gobd_relevant=gobd_relevant,
                gdpr_relevant=gdpr_relevant,
                citations=citations or [],
                mentioned_objects=mentioned_objects or [],
                mentioned_messages=mentioned_messages or [],
                reasoning=reasoning,
                research_notes=research_notes,
                confidence=confidence,
                tags=tags or []
            )

            logger.info(f"Requirement {req_id} written to cache for job {job_id}")
            return str(req_id)

        except Exception as e:
            logger.error(f"Failed to write requirement: {e}")
            return None

    async def write_candidate(
        self,
        job_id: str,
        candidate: Dict[str, Any],
        research_results: Optional[Dict[str, Any]] = None,
        reasoning: Optional[str] = None
    ) -> Optional[str]:
        """Write a candidate directly to the cache.

        Convenience method that extracts fields from candidate dict.

        Args:
            job_id: Parent job UUID
            candidate: Candidate dictionary from extractor
            research_results: Optional research context
            reasoning: Optional extraction reasoning

        Returns:
            Requirement ID if successful, None otherwise
        """
        # Generate name from text if not provided
        text = candidate.get("text", "")
        name = self._generate_name(text)

        # Build research notes from research results
        research_notes = None
        if research_results:
            notes = research_results.get("research_notes", [])
            if notes:
                research_notes = "\n".join(notes)

        return await self.write_requirement(
            job_id=job_id,
            text=text,
            name=name,
            req_type=candidate.get("type", "functional"),
            priority=self._infer_priority(candidate),
            source_document=candidate.get("document_metadata", {}).get("file_path"),
            source_location={
                "section": candidate.get("section_context"),
                "position": candidate.get("source_position"),
            },
            gobd_relevant=candidate.get("gobd_relevant", False),
            gdpr_relevant=candidate.get("gdpr_relevant", False),
            citations=[],  # Citations created separately
            mentioned_objects=candidate.get("mentioned_objects", []),
            mentioned_messages=candidate.get("mentioned_messages", []),
            reasoning=reasoning,
            research_notes=research_notes,
            confidence=candidate.get("confidence", 0.6),
            tags=candidate.get("gobd_categories", []),
        )

    async def write_batch(
        self,
        job_id: str,
        candidates: List[Dict[str, Any]],
        reasoning_map: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Write multiple candidates to the cache.

        Args:
            job_id: Parent job UUID
            candidates: List of candidate dictionaries
            reasoning_map: Optional map of candidate_id -> reasoning

        Returns:
            Summary dictionary with counts and IDs
        """
        reasoning_map = reasoning_map or {}

        results = {
            "total": len(candidates),
            "written": 0,
            "skipped": 0,
            "failed": 0,
            "requirement_ids": [],
            "errors": [],
        }

        for candidate in candidates:
            candidate_id = candidate.get("candidate_id", "")
            reasoning = reasoning_map.get(candidate_id)

            try:
                req_id = await self.write_candidate(
                    job_id=job_id,
                    candidate=candidate,
                    reasoning=reasoning
                )

                if req_id:
                    results["written"] += 1
                    results["requirement_ids"].append(req_id)
                else:
                    results["skipped"] += 1

            except Exception as e:
                results["failed"] += 1
                results["errors"].append({
                    "candidate_id": candidate_id,
                    "error": str(e)
                })

        logger.info(
            f"Batch write complete: {results['written']} written, "
            f"{results['skipped']} skipped, {results['failed']} failed"
        )

        return results

    def _validate_requirement(
        self,
        text: str,
        name: str,
        req_type: str,
        confidence: float
    ) -> Optional[str]:
        """Validate requirement fields.

        Args:
            text: Requirement text
            name: Requirement name
            req_type: Requirement type
            confidence: Confidence score

        Returns:
            Error message if invalid, None if valid
        """
        if not text or len(text.strip()) < 10:
            return "Requirement text too short (minimum 10 characters)"

        if len(text) > 10000:
            return "Requirement text too long (maximum 10000 characters)"

        if not name or len(name.strip()) < 3:
            return "Requirement name too short (minimum 3 characters)"

        valid_types = {"functional", "compliance", "constraint", "non_functional"}
        if req_type not in valid_types:
            return f"Invalid requirement type: {req_type}"

        if not 0.0 <= confidence <= 1.0:
            return f"Confidence must be between 0.0 and 1.0"

        return None

    async def _check_duplicate(self, job_id: str, text: str) -> bool:
        """Check if a similar requirement already exists.

        Args:
            job_id: Job UUID
            text: Requirement text

        Returns:
            True if duplicate found
        """
        try:
            # Simple text-based duplicate check
            # For production, could use more sophisticated similarity matching
            text_normalized = text.lower().strip()

            existing = await self.postgres_conn.fetch(
                """
                SELECT id FROM requirement_cache
                WHERE job_id = $1
                AND LOWER(TRIM(text)) = $2
                LIMIT 1
                """,
                uuid.UUID(job_id),
                text_normalized
            )

            return len(existing) > 0

        except Exception as e:
            logger.warning(f"Duplicate check failed: {e}")
            return False  # Allow through on error

    def _generate_name(self, text: str, max_length: int = 80) -> str:
        """Generate a name from requirement text.

        Args:
            text: Requirement text
            max_length: Maximum name length

        Returns:
            Generated name string
        """
        # Take first sentence or first N words
        import re

        # Find first sentence
        match = re.match(r'^[^.!?]+[.!?]?', text.strip())
        if match:
            first_sentence = match.group(0).strip()
        else:
            first_sentence = text.strip()

        # Truncate if needed
        if len(first_sentence) > max_length:
            # Cut at word boundary
            truncated = first_sentence[:max_length]
            last_space = truncated.rfind(' ')
            if last_space > 20:
                truncated = truncated[:last_space]
            first_sentence = truncated + "..."

        return first_sentence

    def _infer_priority(self, candidate: Dict[str, Any]) -> str:
        """Infer priority from candidate attributes.

        Args:
            candidate: Candidate dictionary

        Returns:
            Priority string (high, medium, low)
        """
        # High priority indicators
        if candidate.get("gobd_relevant"):
            return "high"

        if candidate.get("type") == "compliance":
            return "high"

        confidence = candidate.get("confidence", 0.5)
        if confidence >= 0.85:
            return "high"

        if confidence >= 0.6:
            return "medium"

        return "low"


# =============================================================================
# Factory Function
# =============================================================================

def create_cache_writer(postgres_conn: Any) -> RequirementCacheWriter:
    """Create a cache writer instance.

    Args:
        postgres_conn: PostgreSQL connection

    Returns:
        RequirementCacheWriter instance
    """
    return RequirementCacheWriter(postgres_conn)
