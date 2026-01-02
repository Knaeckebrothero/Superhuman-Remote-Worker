"""Fulfillment Checker for Validator Agent.

Analyzes whether requirements are fulfilled by existing graph entities,
identifies gaps, and prepares fulfillment relationship data.
"""

import logging
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

from src.core.neo4j_utils import Neo4jConnection

logger = logging.getLogger(__name__)


class FulfillmentStatus(Enum):
    """Fulfillment status categories."""
    FULFILLED = "fulfilled"
    PARTIALLY_FULFILLED = "partially_fulfilled"
    NOT_FULFILLED = "not_fulfilled"
    UNKNOWN = "unknown"


class GapSeverity(Enum):
    """Severity levels for fulfillment gaps."""
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


@dataclass
class FulfillmentGap:
    """Represents a gap in requirement fulfillment."""
    entity_id: str
    entity_type: str  # BusinessObject or Message
    entity_name: str
    gap_description: str
    severity: GapSeverity
    remediation: str
    detected_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "entity_name": self.entity_name,
            "gap_description": self.gap_description,
            "severity": self.severity.value,
            "remediation": self.remediation,
            "detected_at": self.detected_at,
        }


@dataclass
class EntityFulfillment:
    """Fulfillment analysis for a single entity."""
    entity_id: str
    entity_type: str
    entity_name: str
    status: FulfillmentStatus
    confidence: float
    evidence: str
    gaps: List[FulfillmentGap] = field(default_factory=list)
    relationship_type: str = ""  # FULFILLED_BY_* or NOT_FULFILLED_BY_*

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "entity_name": self.entity_name,
            "status": self.status.value,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "gaps": [g.to_dict() for g in self.gaps],
            "relationship_type": self.relationship_type,
        }


@dataclass
class FulfillmentResult:
    """Complete fulfillment analysis result."""
    overall_status: FulfillmentStatus
    overall_confidence: float
    object_fulfillments: List[EntityFulfillment] = field(default_factory=list)
    message_fulfillments: List[EntityFulfillment] = field(default_factory=list)
    total_gaps: int = 0
    critical_gaps: int = 0
    recommended_compliance_status: str = "open"  # open, partial, fulfilled
    reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "overall_status": self.overall_status.value,
            "overall_confidence": self.overall_confidence,
            "object_fulfillments": [f.to_dict() for f in self.object_fulfillments],
            "message_fulfillments": [f.to_dict() for f in self.message_fulfillments],
            "total_gaps": self.total_gaps,
            "critical_gaps": self.critical_gaps,
            "recommended_compliance_status": self.recommended_compliance_status,
            "reasoning": self.reasoning,
        }


class FulfillmentChecker:
    """Analyzes fulfillment of requirements by graph entities.

    For each related business object and message, determines:
    - Whether the entity fulfills the requirement
    - What gaps exist if not fully fulfilled
    - Evidence for the fulfillment decision
    - Recommended relationship type (FULFILLED_BY_* or NOT_FULFILLED_BY_*)

    Example:
        ```python
        checker = FulfillmentChecker(neo4j_conn)
        result = checker.check_fulfillment(
            requirement_text="The system must log all invoice modifications",
            related_objects=[{"boid": "BO-001", "name": "Invoice"}],
            related_messages=[],
            gobd_relevant=True,
        )
        ```
    """

    # Fulfillment keywords indicating support
    FULFILLMENT_INDICATORS = {
        "positive": [
            "supports", "implements", "provides", "enables", "tracks",
            "logs", "stores", "maintains", "manages", "handles",
            "processes", "validates", "verifies", "enforces",
        ],
        "negative": [
            "lacks", "missing", "without", "no support", "cannot",
            "unable", "not implemented", "partial", "limited",
        ],
    }

    # GoBD-specific fulfillment checks
    GOBD_FULFILLMENT_CRITERIA = {
        "retention": [
            "aufbewahrung", "retention", "archive", "storage period",
            "10 years", "10 jahre",
        ],
        "traceability": [
            "nachvollziehbar", "traceable", "audit trail", "log",
            "protokoll", "history",
        ],
        "immutability": [
            "unveränderbar", "immutable", "tamper-proof", "readonly",
            "manipulationssicher",
        ],
    }

    def __init__(
        self,
        neo4j_connection: Neo4jConnection,
        min_confidence_threshold: float = 0.5,
    ):
        """Initialize the fulfillment checker.

        Args:
            neo4j_connection: Active Neo4j connection
            min_confidence_threshold: Minimum confidence to consider fulfilled
        """
        self.neo4j = neo4j_connection
        self.min_confidence_threshold = min_confidence_threshold

    def _get_entity_details(
        self,
        entity_id: str,
        entity_type: str,
    ) -> Optional[Dict[str, Any]]:
        """Get full details of an entity from the graph.

        Args:
            entity_id: Entity identifier (boid or mid)
            entity_type: "BusinessObject" or "Message"

        Returns:
            Entity details or None
        """
        if entity_type == "BusinessObject":
            query = """
            MATCH (bo:BusinessObject {boid: $entity_id})
            OPTIONAL MATCH (bo)<-[:RELATES_TO_OBJECT|IMPACTS_OBJECT]-(r:Requirement)
            OPTIONAL MATCH (m:Message)-[:USES_OBJECT|PRODUCES_OBJECT]->(bo)
            RETURN bo, collect(DISTINCT r.name) AS related_requirements,
                   collect(DISTINCT m.name) AS related_messages
            """
        else:  # Message
            query = """
            MATCH (m:Message {mid: $entity_id})
            OPTIONAL MATCH (m)<-[:RELATES_TO_MESSAGE|IMPACTS_MESSAGE]-(r:Requirement)
            OPTIONAL MATCH (m)-[:USES_OBJECT|PRODUCES_OBJECT]->(bo:BusinessObject)
            RETURN m, collect(DISTINCT r.name) AS related_requirements,
                   collect(DISTINCT bo.name) AS related_objects
            """

        try:
            results = self.neo4j.execute_query(query, {"entity_id": entity_id})
            if results:
                return results[0]
        except Exception as e:
            logger.error(f"Error fetching entity {entity_id}: {e}")

        return None

    def _analyze_gobd_fulfillment(
        self,
        requirement_text: str,
        entity: Dict[str, Any],
    ) -> Tuple[bool, List[FulfillmentGap], str]:
        """Analyze GoBD-specific fulfillment.

        Args:
            requirement_text: Requirement text
            entity: Entity data

        Returns:
            Tuple of (is_fulfilled, gaps, evidence)
        """
        text_lower = requirement_text.lower()
        entity_name = entity.get("name", entity.get("entity_name", "Unknown"))
        entity_id = entity.get("boid", entity.get("mid", entity.get("entity_id", "")))
        entity_type = "BusinessObject" if entity.get("boid") else "Message"

        gaps = []
        fulfilled_criteria = []

        for category, keywords in self.GOBD_FULFILLMENT_CRITERIA.items():
            # Check if requirement mentions this category
            if any(kw in text_lower for kw in keywords):
                # For now, mark as gap unless entity description indicates support
                entity_desc = (entity.get("description") or "").lower()

                if any(kw in entity_desc for kw in keywords):
                    fulfilled_criteria.append(category)
                else:
                    # Create gap
                    gaps.append(FulfillmentGap(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        entity_name=entity_name,
                        gap_description=f"Entity does not explicitly support {category} requirement",
                        severity=GapSeverity.CRITICAL if category == "immutability" else GapSeverity.MAJOR,
                        remediation=f"Verify {entity_name} supports {category} or document existing implementation",
                    ))

        is_fulfilled = len(gaps) == 0 and len(fulfilled_criteria) > 0
        evidence = f"GoBD criteria analysis: {', '.join(fulfilled_criteria) if fulfilled_criteria else 'none explicitly supported'}"

        return is_fulfilled, gaps, evidence

    def _analyze_entity_fulfillment(
        self,
        requirement_text: str,
        entity: Dict[str, Any],
        gobd_relevant: bool,
        gdpr_relevant: bool,
    ) -> EntityFulfillment:
        """Analyze fulfillment for a single entity.

        Args:
            requirement_text: Requirement text
            entity: Entity data with id, name, type
            gobd_relevant: Whether requirement is GoBD-relevant
            gdpr_relevant: Whether requirement is GDPR-relevant

        Returns:
            EntityFulfillment result
        """
        entity_id = entity.get("entity_id", entity.get("boid", entity.get("mid", "")))
        entity_name = entity.get("entity_name", entity.get("name", "Unknown"))
        entity_type = entity.get("entity_type", "BusinessObject" if entity.get("boid") else "Message")

        gaps = []
        confidence = 0.5
        evidence_parts = []

        # Get full entity details from graph
        full_entity = self._get_entity_details(entity_id, entity_type)
        if full_entity:
            # Check for existing relationships
            related_reqs = full_entity.get("related_requirements", [])
            if related_reqs:
                evidence_parts.append(f"Entity has {len(related_reqs)} existing requirement relationships")
                confidence += 0.1

            # Check entity description for fulfillment indicators
            node_data = full_entity.get("bo") or full_entity.get("m") or {}
            if hasattr(node_data, "__getitem__"):
                description = str(node_data.get("description") or "")
            else:
                description = ""

            # Positive indicators
            desc_lower = description.lower()
            for indicator in self.FULFILLMENT_INDICATORS["positive"]:
                if indicator in desc_lower:
                    confidence += 0.05
                    evidence_parts.append(f"Entity description indicates '{indicator}'")

            # Negative indicators
            for indicator in self.FULFILLMENT_INDICATORS["negative"]:
                if indicator in desc_lower:
                    confidence -= 0.1
                    gaps.append(FulfillmentGap(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        entity_name=entity_name,
                        gap_description=f"Entity description indicates '{indicator}'",
                        severity=GapSeverity.MAJOR,
                        remediation="Review entity implementation for gaps",
                    ))

        # GoBD-specific analysis
        if gobd_relevant:
            gobd_fulfilled, gobd_gaps, gobd_evidence = self._analyze_gobd_fulfillment(
                requirement_text, entity
            )
            gaps.extend(gobd_gaps)
            evidence_parts.append(gobd_evidence)
            if gobd_fulfilled:
                confidence += 0.2
            elif gobd_gaps:
                confidence -= 0.2

        # Determine fulfillment status
        confidence = max(0.0, min(1.0, confidence))

        if confidence >= 0.7 and len(gaps) == 0:
            status = FulfillmentStatus.FULFILLED
            relationship_type = f"FULFILLED_BY_{entity_type.upper()}"
        elif confidence >= 0.5 or (len(gaps) > 0 and confidence >= 0.3):
            status = FulfillmentStatus.PARTIALLY_FULFILLED
            relationship_type = f"FULFILLED_BY_{entity_type.upper()}"  # Still creates positive relationship
        else:
            status = FulfillmentStatus.NOT_FULFILLED
            relationship_type = f"NOT_FULFILLED_BY_{entity_type.upper()}"

        evidence = "; ".join(evidence_parts) if evidence_parts else "No explicit evidence found"

        return EntityFulfillment(
            entity_id=entity_id,
            entity_type=entity_type,
            entity_name=entity_name,
            status=status,
            confidence=confidence,
            evidence=evidence,
            gaps=gaps,
            relationship_type=relationship_type,
        )

    def check_fulfillment(
        self,
        requirement_text: str,
        related_objects: List[Dict[str, Any]],
        related_messages: List[Dict[str, Any]],
        gobd_relevant: bool = False,
        gdpr_relevant: bool = False,
    ) -> FulfillmentResult:
        """Check fulfillment of a requirement by related entities.

        Args:
            requirement_text: Requirement text
            related_objects: List of related BusinessObjects
            related_messages: List of related Messages
            gobd_relevant: GoBD relevance flag
            gdpr_relevant: GDPR relevance flag

        Returns:
            Complete FulfillmentResult
        """
        object_fulfillments = []
        message_fulfillments = []

        # Analyze each business object
        for obj in related_objects:
            fulfillment = self._analyze_entity_fulfillment(
                requirement_text, obj, gobd_relevant, gdpr_relevant
            )
            object_fulfillments.append(fulfillment)

        # Analyze each message
        for msg in related_messages:
            fulfillment = self._analyze_entity_fulfillment(
                requirement_text, msg, gobd_relevant, gdpr_relevant
            )
            message_fulfillments.append(fulfillment)

        # Calculate overall status
        all_fulfillments = object_fulfillments + message_fulfillments

        if not all_fulfillments:
            # No entities to check
            return FulfillmentResult(
                overall_status=FulfillmentStatus.UNKNOWN,
                overall_confidence=0.3,
                object_fulfillments=[],
                message_fulfillments=[],
                total_gaps=0,
                critical_gaps=0,
                recommended_compliance_status="open",
                reasoning="No related entities to check fulfillment against",
            )

        # Count fulfillment statuses
        fulfilled_count = sum(1 for f in all_fulfillments if f.status == FulfillmentStatus.FULFILLED)
        partial_count = sum(1 for f in all_fulfillments if f.status == FulfillmentStatus.PARTIALLY_FULFILLED)
        not_fulfilled_count = sum(1 for f in all_fulfillments if f.status == FulfillmentStatus.NOT_FULFILLED)

        # Calculate total gaps
        total_gaps = sum(len(f.gaps) for f in all_fulfillments)
        critical_gaps = sum(
            1 for f in all_fulfillments
            for g in f.gaps if g.severity == GapSeverity.CRITICAL
        )

        # Average confidence
        avg_confidence = sum(f.confidence for f in all_fulfillments) / len(all_fulfillments)

        # Determine overall status
        if fulfilled_count == len(all_fulfillments):
            overall_status = FulfillmentStatus.FULFILLED
            compliance_status = "fulfilled"
            reasoning = f"All {len(all_fulfillments)} entities fulfill the requirement"
        elif not_fulfilled_count == len(all_fulfillments):
            overall_status = FulfillmentStatus.NOT_FULFILLED
            compliance_status = "open"
            reasoning = f"None of {len(all_fulfillments)} entities fulfill the requirement"
        else:
            overall_status = FulfillmentStatus.PARTIALLY_FULFILLED
            compliance_status = "partial"
            reasoning = f"{fulfilled_count} fulfilled, {partial_count} partial, {not_fulfilled_count} not fulfilled"

        return FulfillmentResult(
            overall_status=overall_status,
            overall_confidence=round(avg_confidence, 3),
            object_fulfillments=object_fulfillments,
            message_fulfillments=message_fulfillments,
            total_gaps=total_gaps,
            critical_gaps=critical_gaps,
            recommended_compliance_status=compliance_status,
            reasoning=reasoning,
        )


def create_fulfillment_checker(
    neo4j_connection: Neo4jConnection,
) -> FulfillmentChecker:
    """Create a FulfillmentChecker instance.

    Args:
        neo4j_connection: Active Neo4j connection

    Returns:
        Configured FulfillmentChecker
    """
    return FulfillmentChecker(neo4j_connection)
