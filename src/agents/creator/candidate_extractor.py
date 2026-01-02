"""Candidate Extractor for Creator Agent.

Implements requirement candidate identification using linguistic patterns,
GoBD indicator detection, and entity extraction.
"""

import logging
import re
import uuid
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# =============================================================================
# Extraction Patterns
# =============================================================================

# Modal verb patterns indicating requirements
MODAL_PATTERNS = {
    "modal_verbs_en": [
        r"\b(?:must|shall|should|will|may)\s+(?:be|have|provide|ensure|support|allow|enable)",
        r"\b(?:must|shall|should)\s+not\b",
        r"\b(?:is|are)\s+required\s+to\b",
        r"\b(?:needs?\s+to)\b",
    ],
    "modal_verbs_de": [
        r"\b(?:muss|soll|sollte|wird|darf)\s+(?:sein|haben|bereitstellen|sicherstellen|gewährleisten)",
        r"\b(?:muss|soll)\s+nicht\b",
        r"\bist\s+(?:erforderlich|verpflichtet|notwendig)\b",
        r"\b(?:hat\s+zu|haben\s+zu)\b",
    ],
    "obligation_phrases": [
        r"\brequired\s+to\b",
        r"\bobligated\s+to\b",
        r"\bnecessary\s+(?:to|that)\b",
        r"\bessential\s+(?:to|that)\b",
        r"\bverpflichtet\s+(?:zu|dass)\b",
        r"\berforderlich\s+(?:für|dass)\b",
        r"\bnotwendig\s+(?:für|dass)\b",
        r"\bzwingend\s+(?:erforderlich|notwendig)\b",
    ],
    "constraint_patterns": [
        r"\bat\s+least\s+\d+\b",
        r"\bat\s+most\s+\d+\b",
        r"\bwithin\s+\d+\s+(?:days?|hours?|minutes?|seconds?|weeks?|months?|years?)\b",
        r"\bno\s+more\s+than\s+\d+\b",
        r"\bmaximum\s+(?:of\s+)?\d+\b",
        r"\bminimum\s+(?:of\s+)?\d+\b",
        r"\bmindestens\s+\d+\b",
        r"\bhöchstens\s+\d+\b",
        r"\bmaximal\s+\d+\b",
        r"\bminimal\s+\d+\b",
        r"\bspätestens\s+(?:nach\s+)?\d+\b",
        r"\binnerhalb\s+(?:von\s+)?\d+\b",
    ],
    "compliance_keywords": [
        r"\bcompliant\s+with\b",
        r"\bconform(?:s|ing)?\s+to\b",
        r"\bin\s+accordance\s+with\b",
        r"\bper\s+(?:the\s+)?(?:standard|regulation|requirement|law)\b",
        r"\bgemäß\b",
        r"\bentsprechend\b",
        r"\bim\s+Einklang\s+mit\b",
        r"\bin\s+Übereinstimmung\s+mit\b",
    ],
}

# GoBD relevance indicators
GOBD_INDICATORS = [
    # German terms
    "aufbewahrung", "nachvollziehbar", "unveränderbar", "buchung", "beleg",
    "rechnung", "archivierung", "revisionssicher", "ordnungsmäßig", "prüfbar",
    "protokollierung", "verfahrensdokumentation", "datenzugriff", "manipulationssicher",
    "langzeitarchivierung", "aufzeichnung", "geschäftsvorfall", "steuerrelevant",
    "aufbewahrungsfrist", "änderungsprotokoll",
    # English equivalents
    "retention", "traceable", "immutable", "audit-proof", "audit trail",
    "archival", "invoice", "ledger", "transaction log", "record keeping",
    "audit", "compliant", "accounting", "tax-relevant",
]

# GoBD categories
GOBD_CATEGORIES = {
    "retention": ["aufbewahrung", "archivierung", "langzeitarchivierung", "retention", "archival", "aufbewahrungsfrist"],
    "traceability": ["nachvollziehbar", "protokollierung", "traceable", "audit trail", "änderungsprotokoll"],
    "immutability": ["unveränderbar", "manipulationssicher", "immutable", "tamper-proof"],
    "documentation": ["verfahrensdokumentation", "documentation", "aufzeichnung"],
    "accessibility": ["datenzugriff", "prüfbar", "data access", "audit-proof"],
    "completeness": ["vollständig", "lückenlos", "complete", "ordnungsmäßig"],
}

# Business object patterns
BUSINESS_OBJECT_PATTERNS = [
    r"\b(?:the\s+)?([A-Z][a-z]+(?:[A-Z][a-z]+)*)\s+(?:object|entity|record|data|table)\b",
    r"\b(?:das|die|der)\s+(\w+objekt|\w+datensatz|\w+tabelle)\b",
    r"\b(customer|order|invoice|payment|booking|transaction|vehicle|rental|contract|reservation)\b",
    r"\b(Kunde|Auftrag|Rechnung|Zahlung|Buchung|Transaktion|Fahrzeug|Vermietung|Vertrag|Reservierung)\b",
    r"\b(user|account|session|document|file|record|entry|log)\b",
]

# Message/Interface patterns
MESSAGE_PATTERNS = [
    r"\b(?:the\s+)?(\w+)\s+(?:message|request|response|notification|event|signal)\b",
    r"\b(?:die\s+)?(\w+)(?:nachricht|anfrage|antwort|benachrichtigung|meldung)\b",
    r"\b(API|REST|SOAP|webhook|endpoint|interface|service)\b",
    r"\b(email|notification|alert|reminder|confirmation)\b",
]


@dataclass
class RequirementCandidate:
    """A requirement candidate extracted from text."""
    candidate_id: str
    text: str
    type: str  # functional, compliance, constraint, non_functional
    confidence: float
    patterns_matched: List[str] = field(default_factory=list)
    gobd_relevant: bool = False
    gobd_indicators: List[str] = field(default_factory=list)
    gobd_categories: List[str] = field(default_factory=list)
    gdpr_relevant: bool = False
    mentioned_objects: List[str] = field(default_factory=list)
    mentioned_messages: List[str] = field(default_factory=list)
    mentioned_requirements: List[str] = field(default_factory=list)
    source_position: Tuple[int, int] = (0, 0)
    section_context: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "text": self.text,
            "type": self.type,
            "confidence": self.confidence,
            "patterns_matched": self.patterns_matched,
            "gobd_relevant": self.gobd_relevant,
            "gobd_indicators": self.gobd_indicators,
            "gobd_categories": self.gobd_categories,
            "gdpr_relevant": self.gdpr_relevant,
            "mentioned_objects": self.mentioned_objects,
            "mentioned_messages": self.mentioned_messages,
            "mentioned_requirements": self.mentioned_requirements,
            "source_position": list(self.source_position),
            "section_context": self.section_context,
        }


class CandidateExtractor:
    """Extracts requirement candidates from text.

    Uses pattern matching and linguistic analysis to identify statements
    that may represent requirements, with particular focus on GoBD compliance.
    """

    def __init__(
        self,
        mode: str = "balanced",
        min_confidence: float = 0.6
    ):
        """Initialize the candidate extractor.

        Args:
            mode: Extraction mode ('strict', 'balanced', 'permissive')
            min_confidence: Minimum confidence threshold for candidates
        """
        self.mode = mode
        self.min_confidence = min_confidence

        # Compile patterns
        self._compiled_patterns = {}
        for category, patterns in MODAL_PATTERNS.items():
            self._compiled_patterns[category] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

        self._gobd_pattern = re.compile(
            r"\b(" + "|".join(GOBD_INDICATORS) + r")\b",
            re.IGNORECASE
        )

        self._gdpr_pattern = re.compile(
            r"\b(gdpr|dsgvo|datenschutz|personal\s+data|personenbezogen|data\s+protection)\b",
            re.IGNORECASE
        )

        self._business_patterns = [
            re.compile(p, re.IGNORECASE) for p in BUSINESS_OBJECT_PATTERNS
        ]
        self._message_patterns = [
            re.compile(p, re.IGNORECASE) for p in MESSAGE_PATTERNS
        ]
        self._req_ref_pattern = re.compile(
            r"\b(?:REQ|FR|NFR|R|GoBD|GDPR)-?\d+(?:\.\d+)*\b",
            re.IGNORECASE
        )

    def identify(
        self,
        text: str,
        mode: Optional[str] = None,
        section_context: str = ""
    ) -> List[Dict[str, Any]]:
        """Identify requirement candidates in text.

        Args:
            text: Text to analyze
            mode: Override extraction mode
            section_context: Section hierarchy context

        Returns:
            List of candidate dictionaries
        """
        mode = mode or self.mode
        candidates = []

        # Split into sentences
        sentences = self._split_sentences(text)

        for start_pos, sentence in sentences:
            if len(sentence.strip()) < 10:
                continue

            candidate = self._analyze_sentence(
                sentence,
                start_pos,
                mode,
                section_context
            )

            if candidate and candidate.confidence >= self.min_confidence:
                candidates.append(candidate.to_dict())

        # Deduplicate similar candidates
        candidates = self._deduplicate_candidates(candidates)

        logger.debug(f"Identified {len(candidates)} candidates from {len(sentences)} sentences")
        return candidates

    def _analyze_sentence(
        self,
        sentence: str,
        position: int,
        mode: str,
        section_context: str
    ) -> Optional[RequirementCandidate]:
        """Analyze a single sentence for requirement indicators.

        Args:
            sentence: Sentence to analyze
            position: Start position in source text
            mode: Extraction mode
            section_context: Section hierarchy

        Returns:
            RequirementCandidate or None
        """
        patterns_matched = []
        score = 0.0

        # Check all pattern categories
        for category, patterns in self._compiled_patterns.items():
            for pattern in patterns:
                if pattern.search(sentence):
                    patterns_matched.append(category)
                    score += 0.15
                    break  # Only count each category once

        # Mode-specific threshold
        threshold = {"strict": 0.3, "balanced": 0.15, "permissive": 0.05}[mode]

        if score < threshold:
            return None

        # Check GoBD relevance
        gobd_matches = self._gobd_pattern.findall(sentence.lower())
        gobd_relevant = len(gobd_matches) > 0
        gobd_indicators = list(set(m.lower() for m in gobd_matches))
        gobd_categories = self._categorize_gobd(sentence.lower())

        if gobd_relevant:
            score += 0.2

        # Check GDPR relevance
        gdpr_relevant = bool(self._gdpr_pattern.search(sentence))
        if gdpr_relevant:
            score += 0.1

        # Classify requirement type
        req_type = self._classify_type(sentence, patterns_matched, gobd_relevant)

        # Sentence length bonus (well-formed requirements tend to be moderate length)
        word_count = len(sentence.split())
        if 10 <= word_count <= 50:
            score += 0.1
        elif word_count < 5:
            score -= 0.15

        # Extract entities
        objects = self._extract_business_objects(sentence)
        messages = self._extract_messages(sentence)
        req_refs = self._extract_req_refs(sentence)

        if objects or messages:
            score += 0.1

        return RequirementCandidate(
            candidate_id=f"RC-{uuid.uuid4().hex[:8].upper()}",
            text=sentence.strip(),
            type=req_type,
            confidence=min(max(score, 0.0), 1.0),
            patterns_matched=list(set(patterns_matched)),
            gobd_relevant=gobd_relevant,
            gobd_indicators=gobd_indicators,
            gobd_categories=gobd_categories,
            gdpr_relevant=gdpr_relevant,
            mentioned_objects=objects,
            mentioned_messages=messages,
            mentioned_requirements=req_refs,
            source_position=(position, position + len(sentence)),
            section_context=section_context,
        )

    def _classify_type(
        self,
        text: str,
        patterns: List[str],
        gobd_relevant: bool
    ) -> str:
        """Classify the requirement type.

        Args:
            text: Requirement text
            patterns: Matched pattern categories
            gobd_relevant: GoBD relevance flag

        Returns:
            Requirement type string
        """
        text_lower = text.lower()

        # Compliance indicators
        compliance_keywords = [
            "compliant", "regulation", "gobd", "gdpr", "dsgvo", "audit",
            "gemäß", "entsprechend", "rechtlich", "gesetzlich"
        ]
        if gobd_relevant or any(kw in text_lower for kw in compliance_keywords):
            return "compliance"

        # Constraint indicators
        if "constraint_patterns" in patterns:
            return "constraint"

        constraint_keywords = [
            "at least", "at most", "within", "maximum", "minimum",
            "mindestens", "höchstens", "spätestens", "not exceed"
        ]
        if any(kw in text_lower for kw in constraint_keywords):
            return "constraint"

        # Non-functional indicators
        nfr_keywords = [
            "performance", "security", "reliability", "availability",
            "scalab", "maintainab", "usab", "accessib",
            "leistung", "sicherheit", "verfügbarkeit"
        ]
        if any(kw in text_lower for kw in nfr_keywords):
            return "non_functional"

        # Default to functional
        return "functional"

    def _categorize_gobd(self, text: str) -> List[str]:
        """Categorize GoBD relevance.

        Args:
            text: Text to categorize

        Returns:
            List of GoBD category names
        """
        categories = []
        for category, keywords in GOBD_CATEGORIES.items():
            if any(kw in text for kw in keywords):
                categories.append(category)
        return categories

    def _extract_business_objects(self, text: str) -> List[str]:
        """Extract business object mentions.

        Args:
            text: Text to search

        Returns:
            List of business object names
        """
        objects = []
        for pattern in self._business_patterns:
            for match in pattern.finditer(text):
                obj = match.group(1) if match.lastindex else match.group(0)
                objects.append(obj.lower())
        return list(set(objects))

    def _extract_messages(self, text: str) -> List[str]:
        """Extract message/interface mentions.

        Args:
            text: Text to search

        Returns:
            List of message names
        """
        messages = []
        for pattern in self._message_patterns:
            for match in pattern.finditer(text):
                msg = match.group(1) if match.lastindex else match.group(0)
                messages.append(msg.lower())
        return list(set(messages))

    def _extract_req_refs(self, text: str) -> List[str]:
        """Extract requirement references.

        Args:
            text: Text to search

        Returns:
            List of requirement IDs
        """
        refs = self._req_ref_pattern.findall(text)
        return list(set(r.upper() for r in refs))

    def _split_sentences(self, text: str) -> List[Tuple[int, str]]:
        """Split text into sentences with positions.

        Args:
            text: Text to split

        Returns:
            List of (position, sentence) tuples
        """
        # Handle common abbreviations to avoid false splits
        text_processed = text
        abbreviations = ["Dr.", "Mr.", "Mrs.", "Ms.", "Prof.", "Inc.", "Ltd.", "vs.", "e.g.", "i.e.", "etc."]
        for abbr in abbreviations:
            text_processed = text_processed.replace(abbr, abbr.replace(".", "<DOT>"))

        # Split on sentence boundaries
        pattern = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])")
        parts = pattern.split(text_processed)

        # Rebuild with positions
        sentences = []
        pos = 0
        for part in parts:
            # Restore dots
            part = part.replace("<DOT>", ".")
            if part.strip():
                sentences.append((pos, part.strip()))
            pos = text.find(part, pos) + len(part) if part in text else pos + len(part)

        return sentences

    def _deduplicate_candidates(
        self,
        candidates: List[Dict],
        similarity_threshold: float = 0.9
    ) -> List[Dict]:
        """Remove near-duplicate candidates.

        Args:
            candidates: List of candidates
            similarity_threshold: Similarity threshold for dedup

        Returns:
            Deduplicated list
        """
        if not candidates:
            return candidates

        unique = []
        for cand in candidates:
            is_duplicate = False
            cand_text = cand.get("text", "").lower()

            for existing in unique:
                existing_text = existing.get("text", "").lower()

                # Simple Jaccard similarity
                words1 = set(cand_text.split())
                words2 = set(existing_text.split())

                if words1 and words2:
                    similarity = len(words1 & words2) / len(words1 | words2)
                    if similarity >= similarity_threshold:
                        is_duplicate = True
                        # Keep higher confidence one
                        if cand.get("confidence", 0) > existing.get("confidence", 0):
                            unique.remove(existing)
                            unique.append(cand)
                        break

            if not is_duplicate:
                unique.append(cand)

        return unique

    def assess_gobd(self, text: str) -> Dict[str, Any]:
        """Assess GoBD relevance of text.

        Args:
            text: Text to assess

        Returns:
            Assessment dictionary
        """
        text_lower = text.lower()

        matches = self._gobd_pattern.findall(text_lower)
        unique_matches = list(set(m.lower() for m in matches))

        categories = self._categorize_gobd(text_lower)

        is_relevant = len(unique_matches) > 0
        confidence = min(len(unique_matches) * 0.25, 1.0) if is_relevant else 0.0

        return {
            "is_relevant": is_relevant,
            "confidence": confidence,
            "indicators": unique_matches,
            "categories": categories,
        }

    def extract_entities(self, text: str) -> Dict[str, List[str]]:
        """Extract all entity mentions from text.

        Args:
            text: Text to analyze

        Returns:
            Dictionary with 'objects', 'messages', 'requirements' lists
        """
        return {
            "objects": self._extract_business_objects(text),
            "messages": self._extract_messages(text),
            "requirements": self._extract_req_refs(text),
        }


# =============================================================================
# Factory Function
# =============================================================================

def create_candidate_extractor(
    mode: str = "balanced",
    min_confidence: float = 0.6
) -> CandidateExtractor:
    """Create a candidate extractor instance.

    Args:
        mode: Extraction mode
        min_confidence: Minimum confidence threshold

    Returns:
        Configured CandidateExtractor
    """
    return CandidateExtractor(mode=mode, min_confidence=min_confidence)
