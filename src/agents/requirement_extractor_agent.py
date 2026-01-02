"""
Requirement Extractor Agent

LangGraph-based agent for extracting requirement candidates from document chunks.
Uses linguistic patterns and LLM analysis to identify, classify, and score
potential requirements for GoBD compliance and system traceability.
"""

import os
import re
import uuid
from typing import Dict, Any, List, TypedDict, Annotated, Sequence, Optional, Tuple
from datetime import datetime
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages

from src.core.document_models import (
    DocumentChunk,
    DocumentMetadata,
    RequirementCandidate,
    RequirementType,
    ExtractionStats,
    ProcessingStatus,
)
from src.core.config import load_config


# =============================================================================
# Extraction Patterns
# =============================================================================

# Linguistic patterns for requirement detection
REQUIREMENT_PATTERNS = {
    "modal_verbs_en": [
        r"\b(?:must|shall|should|will|may)\s+(?:be|have|provide|ensure|support|allow)",
        r"\b(?:must|shall|should)\s+not\b",
        r"\b(?:is|are)\s+required\s+to\b",
    ],
    "modal_verbs_de": [
        r"\b(?:muss|soll|sollte|wird|darf)\s+(?:sein|haben|bereitstellen|sicherstellen)",
        r"\b(?:muss|soll)\s+nicht\b",
        r"\bist\s+(?:erforderlich|verpflichtet|notwendig)\b",
    ],
    "obligation_phrases": [
        r"\brequired\s+to\b",
        r"\bobligated\s+to\b",
        r"\bnecessary\s+to\b",
        r"\bessential\s+(?:to|that)\b",
        r"\bverpflichtet\s+(?:zu|dass)\b",
        r"\berforderlich\s+(?:für|dass)\b",
        r"\bnotwendig\s+(?:für|dass)\b",
    ],
    "constraint_patterns": [
        r"\bat\s+least\s+\d+\b",
        r"\bat\s+most\s+\d+\b",
        r"\bwithin\s+\d+\s+(?:days|hours|minutes|seconds|weeks)\b",
        r"\bno\s+more\s+than\s+\d+\b",
        r"\bmaximum\s+(?:of\s+)?\d+\b",
        r"\bminimum\s+(?:of\s+)?\d+\b",
        r"\bmindestens\s+\d+\b",
        r"\bhöchstens\s+\d+\b",
        r"\bmaximal\s+\d+\b",
        r"\bminimal\s+\d+\b",
    ],
    "compliance_keywords": [
        r"\bcompliant\s+with\b",
        r"\bconform(?:s|ing)?\s+to\b",
        r"\bin\s+accordance\s+with\b",
        r"\bper\s+(?:the\s+)?(?:standard|regulation|requirement)\b",
        r"\bgemäß\b",
        r"\bentsprechend\b",
        r"\bim\s+Einklang\s+mit\b",
    ],
}

# GoBD relevance indicators
GOBD_INDICATORS = [
    # German terms
    "aufbewahrung",  # retention
    "nachvollziehbar",  # traceable
    "unveränderbar",  # immutable
    "buchung",  # booking/posting
    "beleg",  # document/voucher
    "rechnung",  # invoice
    "archivierung",  # archiving
    "revisionssicher",  # audit-proof
    "ordnungsmäßig",  # proper/compliant
    "prüfbar",  # auditable
    "protokollierung",  # logging
    "verfahrensdokumentation",  # process documentation
    "datenzugriff",  # data access
    "manipulationssicher",  # tamper-proof
    "langzeitarchivierung",  # long-term archival
    # English equivalents
    "retention",
    "traceable",
    "immutable",
    "audit-proof",
    "audit trail",
    "compliant",
    "archival",
    "invoice",
    "ledger",
    "transaction log",
]

# Business object indicators
BUSINESS_OBJECT_PATTERNS = [
    r"\b(?:the\s+)?([A-Z][a-z]+(?:[A-Z][a-z]+)*)\s+(?:object|entity|record|data)\b",
    r"\b(?:das|die|der)\s+(\w+objekt|\w+datensatz)\b",
    r"\b(customer|order|invoice|payment|booking|transaction|vehicle|rental|contract)\b",
    r"\b(Kunde|Auftrag|Rechnung|Zahlung|Buchung|Transaktion|Fahrzeug|Vermietung|Vertrag)\b",
]

# Message/interface indicators
MESSAGE_PATTERNS = [
    r"\b(?:the\s+)?(\w+)\s+(?:message|request|response|notification|event)\b",
    r"\b(?:die\s+)?(\w+)(?:nachricht|anfrage|antwort|benachrichtigung)\b",
    r"\b(API|REST|SOAP|webhook|endpoint)\b",
]


# =============================================================================
# Agent State Definition
# =============================================================================

class RequirementExtractorState(TypedDict):
    """State for the requirement extraction agent."""
    # Messages for LLM conversation
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # Input
    chunks: List[Dict[str, Any]]  # Serialized DocumentChunks
    document_metadata: Dict[str, Any]  # Serialized DocumentMetadata

    # Processing state
    current_chunk_index: int
    candidates: List[Dict[str, Any]]  # Serialized RequirementCandidates

    # Configuration
    extraction_mode: str  # strict, balanced, permissive
    min_confidence_threshold: float

    # Statistics
    extraction_stats: Dict[str, Any]

    # Control
    iteration: int
    max_iterations: int
    is_complete: bool
    error_message: Optional[str]


# =============================================================================
# Requirement Extraction Tools
# =============================================================================

class RequirementExtractionTools:
    """Tools for requirement extraction operations."""

    def __init__(self, mode: str = "balanced"):
        """
        Initialize extraction tools.

        Args:
            mode: Extraction mode (strict, balanced, permissive)
        """
        self.mode = mode
        self._compile_patterns()

    def _compile_patterns(self):
        """Compile all regex patterns for efficiency."""
        self._requirement_patterns = {}
        for category, patterns in REQUIREMENT_PATTERNS.items():
            self._requirement_patterns[category] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

        self._gobd_pattern = re.compile(
            r"\b(" + "|".join(GOBD_INDICATORS) + r")\b",
            re.IGNORECASE
        )

        self._business_patterns = [
            re.compile(p, re.IGNORECASE) for p in BUSINESS_OBJECT_PATTERNS
        ]
        self._message_patterns = [
            re.compile(p, re.IGNORECASE) for p in MESSAGE_PATTERNS
        ]

    def get_tools(self):
        """Return list of tools for the agent."""

        @tool
        def identify_requirements(text: str, mode: str = "balanced") -> str:
            """
            Identify requirement-like statements in text.

            Args:
                text: Text to analyze for requirements
                mode: Detection mode:
                    - "strict": High precision, only clear requirements
                    - "balanced": Balance between precision and recall
                    - "permissive": High recall, may include false positives

            Returns:
                JSON-formatted list of potential requirements with:
                - text: The requirement statement
                - position: Start position in text
                - patterns_matched: Which patterns triggered
                - preliminary_score: Initial confidence score
            """
            results = []

            # Split text into sentences
            sentences = self._split_sentences(text)

            for sentence_start, sentence in sentences:
                patterns_matched = []
                score = 0.0

                # Check all pattern categories
                for category, patterns in self._requirement_patterns.items():
                    for pattern in patterns:
                        if pattern.search(sentence):
                            patterns_matched.append(category)
                            score += 0.2  # Each pattern match adds to score
                            break  # Only count each category once

                # Adjust for mode
                threshold = {"strict": 0.4, "balanced": 0.2, "permissive": 0.1}[mode]

                if score >= threshold:
                    results.append({
                        "text": sentence.strip(),
                        "position": sentence_start,
                        "patterns_matched": list(set(patterns_matched)),
                        "preliminary_score": min(score, 1.0),
                    })

            summary = f"Found {len(results)} potential requirements in text.\n\n"
            for i, r in enumerate(results[:10], 1):
                summary += f"{i}. Score: {r['preliminary_score']:.2f}, Patterns: {r['patterns_matched']}\n"
                summary += f"   '{r['text'][:100]}...'\n\n"

            if len(results) > 10:
                summary += f"... and {len(results) - 10} more\n"

            return summary

        @tool
        def classify_requirement(text: str) -> str:
            """
            Classify a requirement statement by type.

            Args:
                text: Requirement text to classify

            Returns:
                Classification with:
                - type: functional, non_functional, constraint, compliance
                - subtype: More specific classification
                - confidence: 0.0-1.0
            """
            text_lower = text.lower()

            # Classification rules
            classifications = {
                "compliance": [
                    r"\b(?:compliant|conform|gobd|gdpr|dsgvo|regulation)\b",
                    r"\b(?:audit|retention|archiv|protokoll)\b",
                    r"\b(?:gemäß|entsprechend|rechtlich)\b",
                ],
                "constraint": [
                    r"\b(?:at least|at most|within|maximum|minimum)\b",
                    r"\b(?:mindestens|höchstens|maximal|spätestens)\b",
                    r"\b(?:must not|shall not|muss nicht|darf nicht)\b",
                    r"\b\d+\s*(?:seconds?|minutes?|hours?|days?|%)\b",
                ],
                "non_functional": [
                    r"\b(?:performance|security|reliability|availability)\b",
                    r"\b(?:scalab|maintainab|usab|accessib)\w*\b",
                    r"\b(?:Leistung|Sicherheit|Zuverlässigkeit|Verfügbarkeit)\b",
                ],
                "functional": [
                    r"\b(?:shall|must|will)\s+(?:provide|enable|allow|support)\b",
                    r"\b(?:soll|muss|wird)\s+(?:bereitstellen|ermöglichen|unterstützen)\b",
                    r"\b(?:user|system|application)\s+(?:can|shall|must)\b",
                ],
            }

            scores = {rtype: 0.0 for rtype in classifications}

            for rtype, patterns in classifications.items():
                for pattern in patterns:
                    if re.search(pattern, text_lower):
                        scores[rtype] += 0.3

            # Get best match
            best_type = max(scores, key=scores.get)
            confidence = min(scores[best_type], 1.0)

            # Default to functional if no strong signals
            if confidence < 0.1:
                best_type = "functional"
                confidence = 0.3

            result = f"Classification: {best_type.upper()}\n"
            result += f"Confidence: {confidence:.2f}\n\n"
            result += "All scores:\n"
            for rtype, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
                result += f"  - {rtype}: {score:.2f}\n"

            return result

        @tool
        def assess_gobd_relevance(text: str) -> str:
            """
            Assess whether a requirement is relevant to GoBD compliance.

            GoBD (Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von
            Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie
            zum Datenzugriff) defines German requirements for electronic
            bookkeeping and data retention.

            Args:
                text: Requirement text to assess

            Returns:
                Assessment with:
                - is_relevant: bool
                - confidence: 0.0-1.0
                - indicators: Matched GoBD keywords
                - category: Which GoBD principle it relates to
            """
            text_lower = text.lower()

            # Find GoBD indicators
            matches = self._gobd_pattern.findall(text_lower)
            unique_matches = list(set(m.lower() for m in matches))

            # Categorize by GoBD principles
            categories = {
                "retention": ["aufbewahrung", "archivierung", "langzeitarchivierung", "retention", "archival"],
                "traceability": ["nachvollziehbar", "protokollierung", "traceable", "audit trail"],
                "immutability": ["unveränderbar", "manipulationssicher", "immutable", "tamper-proof"],
                "documentation": ["verfahrensdokumentation", "documentation"],
                "accessibility": ["datenzugriff", "prüfbar", "data access", "audit-proof"],
            }

            matched_categories = []
            for cat, keywords in categories.items():
                if any(kw in text_lower for kw in keywords):
                    matched_categories.append(cat)

            is_relevant = len(unique_matches) > 0
            confidence = min(len(unique_matches) * 0.3, 1.0) if is_relevant else 0.0

            result = f"GoBD Relevance Assessment:\n\n"
            result += f"Is Relevant: {is_relevant}\n"
            result += f"Confidence: {confidence:.2f}\n"
            result += f"Indicators Found: {unique_matches}\n"
            result += f"GoBD Categories: {matched_categories}\n"

            return result

        @tool
        def extract_entity_mentions(text: str) -> str:
            """
            Find referenced business objects and messages in requirement text.

            Looks for patterns indicating:
            - Business objects (entities, records, data types)
            - Messages (API calls, notifications, events)
            - Other requirements (references like "REQ-001")

            Args:
                text: Requirement text to analyze

            Returns:
                Extracted entities with their types and positions
            """
            business_objects = []
            messages = []
            requirements = []

            # Find business objects
            for pattern in self._business_patterns:
                for match in pattern.finditer(text):
                    obj = match.group(1) if match.lastindex else match.group(0)
                    business_objects.append(obj)

            # Find messages
            for pattern in self._message_patterns:
                for match in pattern.finditer(text):
                    msg = match.group(1) if match.lastindex else match.group(0)
                    messages.append(msg)

            # Find requirement references
            req_pattern = re.compile(r"\b(?:REQ|FR|NFR|R)-?\d+\b", re.IGNORECASE)
            for match in req_pattern.finditer(text):
                requirements.append(match.group(0))

            result = f"Entity Mentions Found:\n\n"
            result += f"Business Objects ({len(set(business_objects))}):\n"
            for obj in sorted(set(business_objects)):
                result += f"  - {obj}\n"
            result += f"\nMessages/Interfaces ({len(set(messages))}):\n"
            for msg in sorted(set(messages)):
                result += f"  - {msg}\n"
            result += f"\nRequirement References ({len(set(requirements))}):\n"
            for req in sorted(set(requirements)):
                result += f"  - {req}\n"

            return result

        @tool
        def compute_extraction_confidence(
            patterns_matched: str,
            has_modal_verb: bool,
            sentence_length: int,
            gobd_relevant: bool
        ) -> str:
            """
            Compute overall confidence score for a requirement candidate.

            Args:
                patterns_matched: Comma-separated list of matched pattern categories
                has_modal_verb: Whether a modal verb (must, shall, etc.) was found
                sentence_length: Number of words in the sentence
                gobd_relevant: Whether GoBD indicators were found

            Returns:
                Confidence score with breakdown
            """
            score = 0.0
            breakdown = []

            # Pattern matches
            patterns = [p.strip() for p in patterns_matched.split(",") if p.strip()]
            pattern_score = min(len(patterns) * 0.15, 0.45)
            score += pattern_score
            breakdown.append(f"Pattern matches ({len(patterns)}): +{pattern_score:.2f}")

            # Modal verb bonus
            if has_modal_verb:
                score += 0.2
                breakdown.append("Modal verb present: +0.20")

            # Sentence length (too short or too long reduces confidence)
            if 10 <= sentence_length <= 50:
                score += 0.15
                breakdown.append(f"Good sentence length ({sentence_length}): +0.15")
            elif sentence_length < 5:
                score -= 0.1
                breakdown.append(f"Too short ({sentence_length}): -0.10")

            # GoBD relevance bonus
            if gobd_relevant:
                score += 0.2
                breakdown.append("GoBD relevant: +0.20")

            final_score = max(0.0, min(1.0, score))

            result = f"Confidence Calculation:\n\n"
            for item in breakdown:
                result += f"  {item}\n"
            result += f"\nFinal Score: {final_score:.2f}"

            return result

        def _split_sentences(text: str) -> List[Tuple[int, str]]:
            """Split text into sentences with positions."""
            # Simple sentence splitter
            pattern = re.compile(r"(?<=[.!?])\s+")
            sentences = []
            pos = 0
            for part in pattern.split(text):
                if part.strip():
                    sentences.append((pos, part.strip()))
                pos += len(part) + 1
            return sentences

        # Store helper as method
        self._split_sentences = _split_sentences

        return [
            identify_requirements,
            classify_requirement,
            assess_gobd_relevance,
            extract_entity_mentions,
            compute_extraction_confidence,
        ]


# =============================================================================
# Requirement Extractor Agent
# =============================================================================

class RequirementExtractorAgent:
    """
    LangGraph-based agent for requirement extraction from document chunks.

    Processes chunks sequentially or in parallel to identify and classify
    requirement candidates with confidence scores.
    """

    def __init__(
        self,
        llm_model: str = "gpt-4o",
        temperature: float = 0.0,
        extraction_mode: str = "balanced",
        min_confidence: float = 0.6,
        reasoning_level: str = "medium",
    ):
        """
        Initialize the requirement extractor agent.

        Args:
            llm_model: LLM model to use
            temperature: LLM temperature setting
            extraction_mode: Extraction mode (strict, balanced, permissive)
            min_confidence: Minimum confidence threshold
            reasoning_level: Reasoning effort level
        """
        self.llm_model = llm_model
        self.temperature = temperature
        self.extraction_mode = extraction_mode
        self.min_confidence = min_confidence
        self.reasoning_level = reasoning_level

        # Initialize LLM
        llm_kwargs = {
            "model": self.llm_model,
            "temperature": self.temperature,
            "api_key": os.getenv("OPENAI_API_KEY"),
        }
        base_url = os.getenv("LLM_BASE_URL")
        if base_url:
            llm_kwargs["base_url"] = base_url

        self.llm = ChatOpenAI(**llm_kwargs)

        # Initialize tools
        self.extraction_tools = RequirementExtractionTools(extraction_mode)
        self.tools = self.extraction_tools.get_tools()

        # Bind tools to LLM
        self.llm_with_tools = self.llm.bind_tools(self.tools)

        # Build the graph
        self.graph = self._build_graph()

    def _get_system_prompt(self) -> str:
        """Get the system prompt for the extractor."""
        return f"""You are a requirement extraction specialist for a car rental system's
compliance and traceability framework.

Your task is to analyze document chunks and identify requirement candidates.
Focus on:

1. **Requirement Identification**: Find statements that express:
   - Obligations (must, shall, required to)
   - Capabilities (can, may, should)
   - Constraints (at least, maximum, within)
   - Compliance needs (conform to, compliant with)

2. **GoBD Compliance**: Pay special attention to requirements related to:
   - Document retention (Aufbewahrung)
   - Traceability (Nachvollziehbarkeit)
   - Immutability (Unveränderbarkeit)
   - Audit trails (Protokollierung)
   - Tax/accounting compliance

3. **Entity Recognition**: Identify mentions of:
   - Business objects (Customer, Order, Invoice, Vehicle, Rental)
   - System messages (API calls, notifications)
   - Other requirements (REQ-001, FR-123)

4. **Classification**: Categorize each requirement as:
   - FUNCTIONAL: What the system does
   - NON_FUNCTIONAL: Quality attributes (performance, security)
   - CONSTRAINT: Limits and boundaries
   - COMPLIANCE: Regulatory requirements

Extraction Mode: {self.extraction_mode}
- strict: Only clear, unambiguous requirements
- balanced: Good mix of precision and recall
- permissive: Capture possible requirements for review

Minimum Confidence: {self.min_confidence}
Reasoning Level: {self.reasoning_level}

For each chunk, analyze the text and output a structured list of requirement candidates."""

    def _extract_node(self, state: RequirementExtractorState) -> RequirementExtractorState:
        """Initial extraction planning node."""
        chunks = state["chunks"]

        system_msg = self._get_system_prompt()
        user_msg = f"""Analyze {len(chunks)} document chunks to extract requirement candidates.

Process each chunk and use the tools to:
1. Identify potential requirements
2. Classify each requirement
3. Assess GoBD relevance
4. Extract entity mentions
5. Compute confidence scores

Document metadata: {state.get('document_metadata', {})}

Begin extraction process."""

        state["messages"] = [
            SystemMessage(content=system_msg),
            HumanMessage(content=user_msg),
        ]
        state["current_chunk_index"] = 0
        state["candidates"] = []
        state["is_complete"] = False

        # Get LLM response
        response = self.llm_with_tools.invoke(state["messages"])
        state["messages"].append(response)

        return state

    def _process_chunk_node(self, state: RequirementExtractorState) -> RequirementExtractorState:
        """Process current chunk and extract requirements."""
        chunks = state["chunks"]
        current_idx = state["current_chunk_index"]

        if current_idx >= len(chunks):
            state["is_complete"] = True
            return state

        chunk = chunks[current_idx]

        # Add chunk context
        context_msg = f"""Process chunk {current_idx + 1}/{len(chunks)}:

Section: {chunk.get('section_hierarchy', [])}
Text ({len(chunk.get('text', ''))} chars):
---
{chunk.get('text', '')}
---

Use the tools to extract requirements from this chunk."""

        state["messages"].append(HumanMessage(content=context_msg))

        # Get LLM response
        response = self.llm_with_tools.invoke(state["messages"])
        state["messages"].append(response)

        return state

    def _should_continue(self, state: RequirementExtractorState) -> str:
        """Determine if extraction should continue."""
        messages = state["messages"]
        last_message = messages[-1] if messages else None

        # Check for tool calls
        if last_message and hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"

        # Check if complete
        if state.get("is_complete"):
            return "finalize"

        # Check iteration limit
        if state.get("iteration", 0) >= state.get("max_iterations", 20):
            return "finalize"

        # Move to next chunk
        return "next_chunk"

    def _increment_node(self, state: RequirementExtractorState) -> RequirementExtractorState:
        """Increment counters after tool execution."""
        state["iteration"] = state.get("iteration", 0) + 1
        return state

    def _next_chunk_node(self, state: RequirementExtractorState) -> RequirementExtractorState:
        """Move to the next chunk."""
        state["current_chunk_index"] = state.get("current_chunk_index", 0) + 1
        return state

    def _finalize_node(self, state: RequirementExtractorState) -> RequirementExtractorState:
        """Finalize extraction and compile statistics."""
        candidates = state.get("candidates", [])

        # If no candidates extracted via LLM, do pattern-based extraction
        if not candidates:
            candidates = self._pattern_based_extraction(state["chunks"])
            state["candidates"] = candidates

        # Compute statistics
        stats = ExtractionStats(
            total_chunks=len(state.get("chunks", [])),
            processed_chunks=state.get("current_chunk_index", 0),
            candidates_found=len(candidates),
            high_confidence=sum(1 for c in candidates if c.get("confidence_score", 0) >= 0.8),
            medium_confidence=sum(1 for c in candidates if 0.5 <= c.get("confidence_score", 0) < 0.8),
            low_confidence=sum(1 for c in candidates if c.get("confidence_score", 0) < 0.5),
            gobd_relevant=sum(1 for c in candidates if c.get("gobd_relevant", False)),
        )
        state["extraction_stats"] = stats.to_dict()
        state["is_complete"] = True

        return state

    def _pattern_based_extraction(self, chunks: List[Dict]) -> List[Dict]:
        """Fallback pattern-based extraction without LLM."""
        candidates = []
        tools = RequirementExtractionTools(self.extraction_mode)
        tools._compile_patterns()

        for chunk in chunks:
            text = chunk.get("text", "")
            chunk_id = chunk.get("chunk_id", "")

            # Split into sentences
            sentences = re.split(r"(?<=[.!?])\s+", text)
            pos = 0

            for sentence in sentences:
                if len(sentence) < 10:
                    pos += len(sentence) + 1
                    continue

                # Check for requirement patterns
                patterns_matched = []
                for category, compiled_patterns in tools._requirement_patterns.items():
                    for pattern in compiled_patterns:
                        if pattern.search(sentence):
                            patterns_matched.append(category)
                            break

                if patterns_matched:
                    # Check GoBD relevance
                    gobd_matches = tools._gobd_pattern.findall(sentence.lower())
                    gobd_relevant = len(gobd_matches) > 0

                    # Compute confidence
                    confidence = min(len(patterns_matched) * 0.2 + (0.2 if gobd_relevant else 0), 1.0)

                    if confidence >= self.min_confidence:
                        # Classify
                        req_type = self._classify_requirement(sentence)

                        candidate = {
                            "candidate_id": f"RC-{uuid.uuid4().hex[:8]}",
                            "text": sentence.strip(),
                            "source_chunk_id": chunk_id,
                            "source_position": [pos, pos + len(sentence)],
                            "requirement_type": req_type.value,
                            "confidence_score": confidence,
                            "gobd_relevant": gobd_relevant,
                            "gobd_indicators": list(set(m.lower() for m in gobd_matches)),
                            "mentioned_objects": self._extract_objects(sentence),
                            "mentioned_messages": self._extract_messages(sentence),
                            "mentioned_requirements": self._extract_req_refs(sentence),
                            "section_context": " > ".join(chunk.get("section_hierarchy", [])),
                            "surrounding_context": "",
                        }
                        candidates.append(candidate)

                pos += len(sentence) + 1

        return candidates

    def _classify_requirement(self, text: str) -> RequirementType:
        """Simple requirement classification."""
        text_lower = text.lower()

        if any(kw in text_lower for kw in ["compliant", "regulation", "gobd", "audit"]):
            return RequirementType.COMPLIANCE
        elif any(kw in text_lower for kw in ["at least", "maximum", "within"]):
            return RequirementType.CONSTRAINT
        elif any(kw in text_lower for kw in ["performance", "security", "availability"]):
            return RequirementType.NON_FUNCTIONAL
        else:
            return RequirementType.FUNCTIONAL

    def _extract_objects(self, text: str) -> List[str]:
        """Extract business object mentions."""
        objects = []
        patterns = [
            r"\b(customer|order|invoice|payment|booking|transaction|vehicle|rental|contract)\b",
            r"\b(Kunde|Auftrag|Rechnung|Zahlung|Buchung|Transaktion|Fahrzeug|Vermietung|Vertrag)\b",
        ]
        for pattern in patterns:
            objects.extend(re.findall(pattern, text, re.IGNORECASE))
        return list(set(o.lower() for o in objects))

    def _extract_messages(self, text: str) -> List[str]:
        """Extract message/interface mentions."""
        messages = []
        patterns = [r"\b(API|REST|notification|event|request|response)\b"]
        for pattern in patterns:
            messages.extend(re.findall(pattern, text, re.IGNORECASE))
        return list(set(m.lower() for m in messages))

    def _extract_req_refs(self, text: str) -> List[str]:
        """Extract requirement references."""
        refs = re.findall(r"\b(?:REQ|FR|NFR|R)-?\d+\b", text, re.IGNORECASE)
        return list(set(r.upper() for r in refs))

    def _build_graph(self) -> StateGraph:
        """Build the LangGraph state machine."""
        workflow = StateGraph(RequirementExtractorState)

        # Add nodes
        workflow.add_node("extract", self._extract_node)
        workflow.add_node("process_chunk", self._process_chunk_node)
        workflow.add_node("tools", ToolNode(self.tools))
        workflow.add_node("increment", self._increment_node)
        workflow.add_node("next_chunk", self._next_chunk_node)
        workflow.add_node("finalize", self._finalize_node)

        # Set entry point
        workflow.set_entry_point("extract")

        # Add edges
        workflow.add_conditional_edges(
            "extract",
            self._should_continue,
            {
                "tools": "tools",
                "next_chunk": "process_chunk",
                "finalize": "finalize",
            },
        )
        workflow.add_edge("tools", "increment")
        workflow.add_conditional_edges(
            "increment",
            self._should_continue,
            {
                "tools": "tools",
                "next_chunk": "process_chunk",
                "finalize": "finalize",
            },
        )
        workflow.add_conditional_edges(
            "process_chunk",
            self._should_continue,
            {
                "tools": "tools",
                "next_chunk": "next_chunk",
                "finalize": "finalize",
            },
        )
        workflow.add_edge("next_chunk", "process_chunk")
        workflow.add_edge("finalize", END)

        return workflow.compile()

    def extract_requirements(
        self,
        chunks: List[DocumentChunk],
        metadata: Optional[DocumentMetadata] = None,
        max_iterations: int = 20,
    ) -> Dict[str, Any]:
        """
        Extract requirements from document chunks.

        Args:
            chunks: List of DocumentChunk objects
            metadata: Optional document metadata
            max_iterations: Maximum processing iterations

        Returns:
            Dictionary with extraction results
        """
        # Serialize inputs
        chunk_dicts = [c.to_dict() if hasattr(c, "to_dict") else c for c in chunks]
        metadata_dict = metadata.to_dict() if metadata and hasattr(metadata, "to_dict") else metadata or {}

        # Initialize state
        initial_state = RequirementExtractorState(
            messages=[],
            chunks=chunk_dicts,
            document_metadata=metadata_dict,
            current_chunk_index=0,
            candidates=[],
            extraction_mode=self.extraction_mode,
            min_confidence_threshold=self.min_confidence,
            extraction_stats={},
            iteration=0,
            max_iterations=max_iterations,
            is_complete=False,
            error_message=None,
        )

        # Run the graph
        final_state = self.graph.invoke(
            initial_state, config={"recursion_limit": 100}
        )

        return {
            "candidates": final_state.get("candidates", []),
            "extraction_stats": final_state.get("extraction_stats", {}),
            "is_complete": final_state.get("is_complete", False),
            "error_message": final_state.get("error_message"),
        }

    def extract_requirements_stream(
        self,
        chunks: List[DocumentChunk],
        metadata: Optional[DocumentMetadata] = None,
        max_iterations: int = 20,
    ):
        """
        Extract requirements and yield state updates for streaming.

        Args:
            chunks: List of DocumentChunk objects
            metadata: Optional document metadata
            max_iterations: Maximum processing iterations

        Yields:
            Dictionary containing current state of the workflow
        """
        chunk_dicts = [c.to_dict() if hasattr(c, "to_dict") else c for c in chunks]
        metadata_dict = metadata.to_dict() if metadata and hasattr(metadata, "to_dict") else metadata or {}

        initial_state = RequirementExtractorState(
            messages=[],
            chunks=chunk_dicts,
            document_metadata=metadata_dict,
            current_chunk_index=0,
            candidates=[],
            extraction_mode=self.extraction_mode,
            min_confidence_threshold=self.min_confidence,
            extraction_stats={},
            iteration=0,
            max_iterations=max_iterations,
            is_complete=False,
            error_message=None,
        )

        return self.graph.stream(
            initial_state, config={"recursion_limit": 100}
        )


# =============================================================================
# Factory Function
# =============================================================================

def create_requirement_extractor_agent() -> RequirementExtractorAgent:
    """
    Create a requirement extractor agent with configuration from config file.

    Returns:
        Configured RequirementExtractorAgent instance
    """
    config = load_config("llm_config.json")

    # Use document_ingestion config if available
    doc_config = config.get("document_ingestion", config.get("agent", {}))

    return RequirementExtractorAgent(
        llm_model=doc_config.get("model", "gpt-4o"),
        temperature=doc_config.get("temperature", 0.0),
        extraction_mode=doc_config.get("extraction_mode", "balanced"),
        min_confidence=doc_config.get("min_confidence", 0.6),
        reasoning_level=doc_config.get("reasoning_level", "medium"),
    )
