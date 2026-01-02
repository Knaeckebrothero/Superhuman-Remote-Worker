"""Relevance Analyzer for Validator Agent.

Implements domain relevance checking for requirement candidates,
including business object discovery and relevance decision tree.
"""

import logging
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from difflib import SequenceMatcher

from src.core.neo4j_utils import Neo4jConnection

logger = logging.getLogger(__name__)


class RelevanceDecision(Enum):
    """Relevance decision categories."""
    RELEVANT = "relevant"
    PARTIALLY_RELEVANT = "partially_relevant"
    NOT_RELEVANT = "not_relevant"
    NEEDS_REVIEW = "needs_review"


@dataclass
class RelevanceResult:
    """Result of relevance analysis."""
    decision: RelevanceDecision
    confidence: float
    related_objects: List[Dict[str, Any]] = field(default_factory=list)
    related_messages: List[Dict[str, Any]] = field(default_factory=list)
    domain_match: bool = False
    entity_match_ratio: float = 0.0
    reasoning: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "decision": self.decision.value,
            "confidence": self.confidence,
            "related_objects": self.related_objects,
            "related_messages": self.related_messages,
            "domain_match": self.domain_match,
            "entity_match_ratio": self.entity_match_ratio,
            "reasoning": self.reasoning,
            "warnings": self.warnings,
        }


class RelevanceAnalyzer:
    """Analyzes relevance of requirements to the domain graph.

    Performs domain relevance checking by:
    1. Identifying mentioned entities in requirement text
    2. Discovering related business objects in the graph
    3. Matching messages referenced by the requirement
    4. Applying decision tree for relevance classification

    Example:
        ```python
        analyzer = RelevanceAnalyzer(neo4j_conn)
        result = analyzer.analyze(requirement_data)
        if result.decision == RelevanceDecision.RELEVANT:
            # Proceed with fulfillment analysis
        ```
    """

    # Domain keywords for the car rental business context
    DOMAIN_KEYWORDS = {
        "core": [
            "vehicle", "car", "rental", "customer", "booking", "reservation",
            "invoice", "payment", "return", "pickup", "fleet", "driver",
            "license", "insurance", "damage", "maintenance", "fuel",
        ],
        "compliance": [
            "gobd", "gdpr", "retention", "audit", "log", "trace", "archive",
            "immutable", "nachvollziehbar", "revisionssicher", "aufbewahrung",
            "buchung", "beleg", "protokoll", "datenschutz", "privacy",
        ],
        "technical": [
            "api", "message", "event", "request", "response", "notification",
            "system", "interface", "integration", "service", "endpoint",
        ],
    }

    def __init__(
        self,
        neo4j_connection: Neo4jConnection,
        min_entity_match_ratio: float = 0.3,
        fuzzy_match_threshold: float = 0.6,
    ):
        """Initialize the relevance analyzer.

        Args:
            neo4j_connection: Active Neo4j connection
            min_entity_match_ratio: Minimum ratio of entities that must match (0.0-1.0)
            fuzzy_match_threshold: Threshold for fuzzy string matching (0.0-1.0)
        """
        self.neo4j = neo4j_connection
        self.min_entity_match_ratio = min_entity_match_ratio
        self.fuzzy_match_threshold = fuzzy_match_threshold

        # Cache for graph entities
        self._business_objects_cache: Optional[List[Dict]] = None
        self._messages_cache: Optional[List[Dict]] = None

    def _load_business_objects(self) -> List[Dict]:
        """Load all business objects from the graph."""
        if self._business_objects_cache is None:
            query = """
            MATCH (bo:BusinessObject)
            RETURN bo.boid AS boid, bo.name AS name, bo.description AS description,
                   bo.domain AS domain, bo.owner AS owner
            LIMIT 1000
            """
            self._business_objects_cache = self.neo4j.execute_query(query)
        return self._business_objects_cache

    def _load_messages(self) -> List[Dict]:
        """Load all messages from the graph."""
        if self._messages_cache is None:
            query = """
            MATCH (m:Message)
            RETURN m.mid AS mid, m.name AS name, m.description AS description,
                   m.direction AS direction, m.format AS format
            LIMIT 1000
            """
            self._messages_cache = self.neo4j.execute_query(query)
        return self._messages_cache

    def _check_domain_relevance(self, text: str) -> Tuple[bool, List[str]]:
        """Check if text contains domain-relevant keywords.

        Args:
            text: Text to analyze

        Returns:
            Tuple of (is_relevant, matched_keywords)
        """
        text_lower = text.lower()
        matched = []

        for category, keywords in self.DOMAIN_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text_lower:
                    matched.append(f"{keyword} ({category})")

        return len(matched) > 0, matched

    def _find_similar_entity(
        self,
        mention: str,
        entities: List[Dict],
        name_key: str,
    ) -> Optional[Dict]:
        """Find an entity similar to a mention.

        Args:
            mention: Text mention to match
            entities: List of entities to search
            name_key: Key for entity name field

        Returns:
            Best matching entity or None
        """
        mention_lower = mention.lower().strip()
        best_match = None
        best_score = 0.0

        for entity in entities:
            name = (entity.get(name_key) or "").lower()
            if not name:
                continue

            # Exact match
            if mention_lower == name:
                return entity

            # Substring match
            if mention_lower in name or name in mention_lower:
                score = 0.85
                if score > best_score:
                    best_match = entity
                    best_score = score
                continue

            # Fuzzy match
            score = SequenceMatcher(None, mention_lower, name).ratio()
            if score > best_score and score >= self.fuzzy_match_threshold:
                best_match = entity
                best_score = score

        return best_match

    def _resolve_business_objects(
        self,
        mentioned_objects: List[str],
    ) -> Tuple[List[Dict], List[str]]:
        """Resolve mentioned business objects to graph entities.

        Args:
            mentioned_objects: List of object mentions

        Returns:
            Tuple of (resolved_entities, unresolved_mentions)
        """
        entities = self._load_business_objects()
        resolved = []
        unresolved = []

        for mention in mentioned_objects:
            match = self._find_similar_entity(mention, entities, "name")
            if match:
                resolved.append({
                    "mention": mention,
                    "entity_id": match.get("boid"),
                    "entity_name": match.get("name"),
                    "domain": match.get("domain"),
                    "match_type": "exact" if mention.lower() == match.get("name", "").lower() else "fuzzy",
                })
            else:
                unresolved.append(mention)

        return resolved, unresolved

    def _resolve_messages(
        self,
        mentioned_messages: List[str],
    ) -> Tuple[List[Dict], List[str]]:
        """Resolve mentioned messages to graph entities.

        Args:
            mentioned_messages: List of message mentions

        Returns:
            Tuple of (resolved_entities, unresolved_mentions)
        """
        entities = self._load_messages()
        resolved = []
        unresolved = []

        for mention in mentioned_messages:
            match = self._find_similar_entity(mention, entities, "name")
            if match:
                resolved.append({
                    "mention": mention,
                    "entity_id": match.get("mid"),
                    "entity_name": match.get("name"),
                    "direction": match.get("direction"),
                    "match_type": "exact" if mention.lower() == match.get("name", "").lower() else "fuzzy",
                })
            else:
                unresolved.append(mention)

        return resolved, unresolved

    def _discover_related_objects(
        self,
        text: str,
        limit: int = 10,
    ) -> List[Dict]:
        """Discover business objects related to requirement text.

        Uses keyword matching against entity names and descriptions.

        Args:
            text: Requirement text
            limit: Maximum results

        Returns:
            List of related business objects
        """
        entities = self._load_business_objects()
        text_lower = text.lower()
        scored = []

        for entity in entities:
            name = (entity.get("name") or "").lower()
            desc = (entity.get("description") or "").lower()
            score = 0.0

            # Check if entity name appears in text
            if name and name in text_lower:
                score += 0.5

            # Check word overlap
            name_words = set(name.split())
            desc_words = set(desc.split())
            text_words = set(text_lower.split())

            name_overlap = len(name_words & text_words)
            desc_overlap = len(desc_words & text_words)

            if name_words:
                score += (name_overlap / len(name_words)) * 0.3
            if desc_words:
                score += (desc_overlap / len(desc_words)) * 0.2

            if score > 0.1:
                scored.append({
                    **entity,
                    "relevance_score": round(score, 3),
                })

        # Sort by score and return top results
        scored.sort(key=lambda x: x["relevance_score"], reverse=True)
        return scored[:limit]

    def _apply_decision_tree(
        self,
        domain_match: bool,
        entity_match_ratio: float,
        resolved_objects: List[Dict],
        resolved_messages: List[Dict],
        unresolved_objects: List[str],
        unresolved_messages: List[str],
        gobd_relevant: bool,
        gdpr_relevant: bool,
    ) -> Tuple[RelevanceDecision, float, str]:
        """Apply decision tree to determine relevance.

        Args:
            domain_match: Whether domain keywords matched
            entity_match_ratio: Ratio of matched entities
            resolved_objects: Resolved business objects
            resolved_messages: Resolved messages
            unresolved_objects: Unresolved object mentions
            unresolved_messages: Unresolved message mentions
            gobd_relevant: GoBD relevance flag
            gdpr_relevant: GDPR relevance flag

        Returns:
            Tuple of (decision, confidence, reasoning)
        """
        # Decision tree logic

        # High relevance: Compliance requirement with domain match
        if (gobd_relevant or gdpr_relevant) and domain_match:
            confidence = 0.9 if entity_match_ratio > 0.5 else 0.75
            return (
                RelevanceDecision.RELEVANT,
                confidence,
                f"Compliance requirement ({['GoBD' if gobd_relevant else '', 'GDPR' if gdpr_relevant else '']}) with domain match"
            )

        # High relevance: Good entity matching
        if entity_match_ratio >= 0.7 and (resolved_objects or resolved_messages):
            return (
                RelevanceDecision.RELEVANT,
                0.85,
                f"Strong entity match ({entity_match_ratio:.0%} resolved)"
            )

        # Medium relevance: Domain match with some entities
        if domain_match and entity_match_ratio >= 0.3:
            return (
                RelevanceDecision.RELEVANT,
                0.7,
                f"Domain match with partial entity resolution ({entity_match_ratio:.0%})"
            )

        # Partial relevance: Domain match but no entity resolution
        if domain_match and entity_match_ratio < 0.3:
            return (
                RelevanceDecision.PARTIALLY_RELEVANT,
                0.55,
                "Domain match but low entity resolution"
            )

        # Needs review: Some entities resolved but no domain match
        if entity_match_ratio > 0 and not domain_match:
            return (
                RelevanceDecision.NEEDS_REVIEW,
                0.5,
                f"Entity match ({entity_match_ratio:.0%}) but no domain keywords"
            )

        # Not relevant: No domain match, no entities
        if not domain_match and entity_match_ratio == 0:
            return (
                RelevanceDecision.NOT_RELEVANT,
                0.3,
                "No domain keywords or entity matches"
            )

        # Default: Needs review
        return (
            RelevanceDecision.NEEDS_REVIEW,
            0.4,
            "Could not determine relevance with confidence"
        )

    def analyze(self, requirement: Dict[str, Any]) -> RelevanceResult:
        """Analyze relevance of a requirement.

        Args:
            requirement: Requirement data dictionary with:
                - text: Requirement text
                - mentioned_objects: List of object mentions
                - mentioned_messages: List of message mentions
                - gobd_relevant: GoBD flag
                - gdpr_relevant: GDPR flag

        Returns:
            RelevanceResult with decision and details
        """
        text = requirement.get("text", "")
        mentioned_objects = requirement.get("mentioned_objects", [])
        mentioned_messages = requirement.get("mentioned_messages", [])
        gobd_relevant = requirement.get("gobd_relevant", False)
        gdpr_relevant = requirement.get("gdpr_relevant", False)

        warnings = []

        # 1. Check domain relevance
        domain_match, matched_keywords = self._check_domain_relevance(text)
        if not domain_match:
            warnings.append("No domain keywords found in requirement text")

        # 2. Resolve mentioned entities
        resolved_objects, unresolved_objects = self._resolve_business_objects(
            mentioned_objects if isinstance(mentioned_objects, list) else []
        )
        resolved_messages, unresolved_messages = self._resolve_messages(
            mentioned_messages if isinstance(mentioned_messages, list) else []
        )

        # Add warnings for unresolved entities
        if unresolved_objects:
            warnings.append(f"Unresolved business objects: {', '.join(unresolved_objects)}")
        if unresolved_messages:
            warnings.append(f"Unresolved messages: {', '.join(unresolved_messages)}")

        # 3. Calculate entity match ratio
        total_mentions = len(mentioned_objects) + len(mentioned_messages)
        total_resolved = len(resolved_objects) + len(resolved_messages)
        entity_match_ratio = total_resolved / total_mentions if total_mentions > 0 else 1.0

        # 4. Discover additional related objects
        discovered_objects = self._discover_related_objects(text)

        # 5. Apply decision tree
        decision, confidence, reasoning = self._apply_decision_tree(
            domain_match=domain_match,
            entity_match_ratio=entity_match_ratio,
            resolved_objects=resolved_objects,
            resolved_messages=resolved_messages,
            unresolved_objects=unresolved_objects,
            unresolved_messages=unresolved_messages,
            gobd_relevant=gobd_relevant,
            gdpr_relevant=gdpr_relevant,
        )

        # Build result
        return RelevanceResult(
            decision=decision,
            confidence=confidence,
            related_objects=resolved_objects + discovered_objects,
            related_messages=resolved_messages,
            domain_match=domain_match,
            entity_match_ratio=entity_match_ratio,
            reasoning=reasoning,
            warnings=warnings,
        )

    def clear_cache(self) -> None:
        """Clear entity caches to force reload."""
        self._business_objects_cache = None
        self._messages_cache = None


def create_relevance_analyzer(
    neo4j_connection: Neo4jConnection,
) -> RelevanceAnalyzer:
    """Create a RelevanceAnalyzer instance.

    Args:
        neo4j_connection: Active Neo4j connection

    Returns:
        Configured RelevanceAnalyzer
    """
    return RelevanceAnalyzer(neo4j_connection)
