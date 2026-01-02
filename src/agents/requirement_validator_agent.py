"""
Requirement Validator Agent

LangGraph-based agent for validating requirement candidates against the
existing Neo4j knowledge graph. Performs duplicate detection, entity
resolution, and metamodel compliance checks.
"""

import os
import uuid
from typing import Dict, Any, List, TypedDict, Annotated, Sequence, Optional
from datetime import datetime
from difflib import SequenceMatcher
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages

from src.core.neo4j_utils import Neo4jConnection
from src.core.metamodel_validator import MetamodelValidator
from src.core.document_models import (
    RequirementCandidate,
    ValidatedRequirement,
    RejectedCandidate,
    ValidationStatus,
    EntityMatch,
    SimilarRequirement,
)
from src.core.config import load_config


# =============================================================================
# Agent State Definition
# =============================================================================

class RequirementValidatorState(TypedDict):
    """State for the requirement validation agent."""
    # Messages for LLM conversation
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # Input
    candidates: List[Dict[str, Any]]  # Serialized RequirementCandidates

    # Processing state
    current_candidate_index: int
    validated_requirements: List[Dict[str, Any]]  # Serialized ValidatedRequirements
    rejected_candidates: List[Dict[str, Any]]  # Serialized RejectedCandidates

    # Graph context (cached)
    existing_requirements: List[Dict[str, Any]]  # Loaded once
    existing_business_objects: List[Dict[str, Any]]
    existing_messages: List[Dict[str, Any]]

    # Validation settings
    duplicate_similarity_threshold: float
    require_entity_existence: bool

    # Control
    iteration: int
    max_iterations: int
    is_complete: bool
    error_message: Optional[str]


# =============================================================================
# Validation Tools
# =============================================================================

class ValidationTools:
    """Tools for requirement validation against Neo4j graph."""

    def __init__(
        self,
        neo4j_connection: Neo4jConnection,
        duplicate_threshold: float = 0.95,
    ):
        """
        Initialize validation tools.

        Args:
            neo4j_connection: Active Neo4j connection
            duplicate_threshold: Similarity threshold for duplicate detection
        """
        self.neo4j = neo4j_connection
        self.duplicate_threshold = duplicate_threshold
        self.validator = MetamodelValidator(neo4j_connection)

        # Cache for graph data
        self._requirements_cache = None
        self._business_objects_cache = None
        self._messages_cache = None

    def _load_requirements(self) -> List[Dict]:
        """Load all requirements from the graph."""
        if self._requirements_cache is None:
            query = """
            MATCH (r:Requirement)
            RETURN r.rid AS rid, r.name AS name, r.text AS text,
                   r.type AS type, r.goBDRelevant AS gobd_relevant
            LIMIT 1000
            """
            self._requirements_cache = self.neo4j.execute_query(query)
        return self._requirements_cache

    def _load_business_objects(self) -> List[Dict]:
        """Load all business objects from the graph."""
        if self._business_objects_cache is None:
            query = """
            MATCH (bo:BusinessObject)
            RETURN bo.boid AS boid, bo.name AS name, bo.description AS description
            LIMIT 500
            """
            self._business_objects_cache = self.neo4j.execute_query(query)
        return self._business_objects_cache

    def _load_messages(self) -> List[Dict]:
        """Load all messages from the graph."""
        if self._messages_cache is None:
            query = """
            MATCH (m:Message)
            RETURN m.mid AS mid, m.name AS name, m.description AS description
            LIMIT 500
            """
            self._messages_cache = self.neo4j.execute_query(query)
        return self._messages_cache

    def get_tools(self):
        """Return list of tools for the agent."""

        @tool
        def find_similar_requirements(text: str, threshold: float = 0.7) -> str:
            """
            Find existing requirements similar to the given text.

            Uses string similarity matching to find potential duplicates
            or related requirements in the graph.

            Args:
                text: Requirement text to compare
                threshold: Minimum similarity score (0.0-1.0)

            Returns:
                List of similar requirements with:
                - rid: Requirement ID
                - name: Requirement name
                - text: Requirement text
                - similarity_score: 0.0-1.0
            """
            existing = self._load_requirements()
            similar = []

            text_lower = text.lower().strip()

            for req in existing:
                req_text = (req.get("text") or "").lower().strip()
                if not req_text:
                    continue

                # Calculate similarity
                similarity = SequenceMatcher(None, text_lower, req_text).ratio()

                if similarity >= threshold:
                    similar.append({
                        "rid": req.get("rid"),
                        "name": req.get("name"),
                        "text": req.get("text", "")[:200],
                        "similarity_score": round(similarity, 3),
                    })

            # Sort by similarity
            similar.sort(key=lambda x: x["similarity_score"], reverse=True)
            similar = similar[:10]  # Limit to top 10

            result = f"Found {len(similar)} similar requirements (threshold: {threshold}):\n\n"
            for i, s in enumerate(similar, 1):
                result += f"{i}. [{s['rid']}] {s['name']}\n"
                result += f"   Similarity: {s['similarity_score']:.1%}\n"
                result += f"   Text: {s['text'][:100]}...\n\n"

            if not similar:
                result = "No similar requirements found in the graph."

            return result

        @tool
        def resolve_business_object(mention: str) -> str:
            """
            Resolve a business object mention to an existing graph node.

            Uses fuzzy matching to find the best matching BusinessObject.

            Args:
                mention: Text mention of a business object (e.g., "Customer", "Invoice")

            Returns:
                Matched entity details or None if no match found
            """
            existing = self._load_business_objects()
            mention_lower = mention.lower().strip()

            best_match = None
            best_score = 0.0

            for bo in existing:
                name = (bo.get("name") or "").lower()
                desc = (bo.get("description") or "").lower()

                # Check for exact match
                if mention_lower == name:
                    return f"Exact match found: {bo['boid']} - {bo['name']}"

                # Check for substring match
                if mention_lower in name or name in mention_lower:
                    score = 0.8
                    if score > best_score:
                        best_match = bo
                        best_score = score
                    continue

                # Fuzzy match on name
                score = SequenceMatcher(None, mention_lower, name).ratio()
                if score > best_score:
                    best_match = bo
                    best_score = score

            if best_match and best_score >= 0.6:
                result = f"Best match for '{mention}':\n"
                result += f"  BOID: {best_match['boid']}\n"
                result += f"  Name: {best_match['name']}\n"
                result += f"  Match Score: {best_score:.1%}\n"
                result += f"  Match Type: {'fuzzy' if best_score < 1.0 else 'exact'}"
                return result

            return f"No matching BusinessObject found for '{mention}'"

        @tool
        def resolve_message(mention: str) -> str:
            """
            Resolve a message mention to an existing graph node.

            Uses fuzzy matching to find the best matching Message.

            Args:
                mention: Text mention of a message (e.g., "CreateOrderRequest", "InvoiceEvent")

            Returns:
                Matched entity details or None if no match found
            """
            existing = self._load_messages()
            mention_lower = mention.lower().strip()

            best_match = None
            best_score = 0.0

            for msg in existing:
                name = (msg.get("name") or "").lower()

                if mention_lower == name:
                    return f"Exact match found: {msg['mid']} - {msg['name']}"

                if mention_lower in name or name in mention_lower:
                    score = 0.8
                    if score > best_score:
                        best_match = msg
                        best_score = score
                    continue

                score = SequenceMatcher(None, mention_lower, name).ratio()
                if score > best_score:
                    best_match = msg
                    best_score = score

            if best_match and best_score >= 0.6:
                result = f"Best match for '{mention}':\n"
                result += f"  MID: {best_match['mid']}\n"
                result += f"  Name: {best_match['name']}\n"
                result += f"  Match Score: {best_score:.1%}"
                return result

            return f"No matching Message found for '{mention}'"

        @tool
        def check_metamodel_compatibility(requirement_type: str, gobd_relevant: bool) -> str:
            """
            Pre-validate a requirement against FINIUS metamodel constraints.

            Checks if the requirement structure would be valid in the graph.

            Args:
                requirement_type: Type of requirement (functional, non_functional, constraint, compliance)
                gobd_relevant: Whether the requirement is GoBD-relevant

            Returns:
                Validation result with any compatibility warnings
            """
            warnings = []

            # Check valid requirement type
            valid_types = ["functional", "non_functional", "constraint", "compliance"]
            if requirement_type.lower() not in valid_types:
                warnings.append(f"Unknown requirement type: {requirement_type}")

            # Check GoBD implications
            if gobd_relevant:
                # GoBD requirements should have impact relationships
                warnings.append("Note: GoBD-relevant requirements should have IMPACTS_OBJECT or IMPACTS_MESSAGE relationships for full traceability")

            # Run actual metamodel check
            try:
                report = self.validator.run_quality_gate_checks()
                if not report.passed:
                    warnings.append(f"Existing graph has {report.warning_count} quality warnings")
            except Exception as e:
                warnings.append(f"Could not run metamodel checks: {e}")

            result = "Metamodel Compatibility Check:\n\n"
            result += f"Requirement Type: {requirement_type}\n"
            result += f"GoBD Relevant: {gobd_relevant}\n\n"

            if warnings:
                result += "Warnings:\n"
                for w in warnings:
                    result += f"  - {w}\n"
            else:
                result += "No compatibility issues detected."

            return result

        @tool
        def generate_requirement_id() -> str:
            """
            Generate a unique requirement ID following project conventions.

            Queries Neo4j to find the next available RID.

            Returns:
                New RID like "R-0042"
            """
            try:
                query = """
                MATCH (r:Requirement)
                WHERE r.rid STARTS WITH 'R-'
                RETURN r.rid AS rid
                ORDER BY r.rid DESC
                LIMIT 1
                """
                results = self.neo4j.execute_query(query)

                if results and results[0].get("rid"):
                    last_rid = results[0]["rid"]
                    # Extract number and increment
                    import re
                    match = re.search(r"R-(\d+)", last_rid)
                    if match:
                        next_num = int(match.group(1)) + 1
                        return f"Generated new RID: R-{next_num:04d}"

                return "Generated new RID: R-0001"

            except Exception as e:
                # Fallback to UUID-based ID
                return f"Generated fallback RID: R-{uuid.uuid4().hex[:8].upper()}"

        @tool
        def check_for_duplicates(
            text: str,
            candidate_id: str
        ) -> str:
            """
            Check if a requirement is a duplicate of an existing one.

            Uses high-threshold similarity matching to identify near-exact duplicates.

            Args:
                text: Requirement text to check
                candidate_id: ID of the candidate for tracking

            Returns:
                Duplicate check result with decision
            """
            existing = self._load_requirements()
            text_lower = text.lower().strip()

            for req in existing:
                req_text = (req.get("text") or "").lower().strip()
                if not req_text:
                    continue

                similarity = SequenceMatcher(None, text_lower, req_text).ratio()

                if similarity >= self.duplicate_threshold:
                    result = f"DUPLICATE DETECTED for candidate {candidate_id}:\n\n"
                    result += f"Existing Requirement: {req['rid']}\n"
                    result += f"Name: {req['name']}\n"
                    result += f"Similarity: {similarity:.1%}\n"
                    result += f"\nRecommendation: REJECT as duplicate"
                    return result

            return f"No duplicates found for candidate {candidate_id}"

        @tool
        def query_graph_context(entity_type: str, limit: int = 10) -> str:
            """
            Query the graph for context about existing entities.

            Args:
                entity_type: Type to query ("requirements", "business_objects", "messages", "relationships")
                limit: Maximum results to return

            Returns:
                Summary of existing entities in the graph
            """
            try:
                if entity_type == "requirements":
                    query = f"MATCH (r:Requirement) RETURN r.rid AS id, r.name AS name LIMIT {limit}"
                elif entity_type == "business_objects":
                    query = f"MATCH (bo:BusinessObject) RETURN bo.boid AS id, bo.name AS name LIMIT {limit}"
                elif entity_type == "messages":
                    query = f"MATCH (m:Message) RETURN m.mid AS id, m.name AS name LIMIT {limit}"
                elif entity_type == "relationships":
                    query = """
                    MATCH (a)-[r]->(b)
                    RETURN type(r) AS type, labels(a)[0] AS source, labels(b)[0] AS target, count(*) AS count
                    GROUP BY type(r), labels(a)[0], labels(b)[0]
                    LIMIT 20
                    """
                else:
                    return f"Unknown entity type: {entity_type}"

                results = self.neo4j.execute_query(query)

                result = f"Graph Context - {entity_type.replace('_', ' ').title()}:\n\n"
                for r in results:
                    if entity_type == "relationships":
                        result += f"  {r['source']} --[{r['type']}]--> {r['target']}: {r['count']} instances\n"
                    else:
                        result += f"  [{r['id']}] {r['name']}\n"

                return result

            except Exception as e:
                return f"Error querying graph: {e}"

        return [
            find_similar_requirements,
            resolve_business_object,
            resolve_message,
            check_metamodel_compatibility,
            generate_requirement_id,
            check_for_duplicates,
            query_graph_context,
        ]


# =============================================================================
# Requirement Validator Agent
# =============================================================================

class RequirementValidatorAgent:
    """
    LangGraph-based agent for validating requirement candidates.

    Validates candidates against the Neo4j graph for:
    - Duplicate detection
    - Entity resolution (BusinessObjects, Messages)
    - Metamodel compliance
    - Relationship suggestions
    """

    def __init__(
        self,
        neo4j_connection: Neo4jConnection,
        llm_model: str = "gpt-4o",
        temperature: float = 0.0,
        duplicate_threshold: float = 0.95,
        require_entity_existence: bool = False,
        reasoning_level: str = "medium",
    ):
        """
        Initialize the requirement validator agent.

        Args:
            neo4j_connection: Active Neo4j connection
            llm_model: LLM model to use
            temperature: LLM temperature setting
            duplicate_threshold: Similarity threshold for duplicates
            require_entity_existence: Strict mode for entity resolution
            reasoning_level: Reasoning effort level
        """
        self.neo4j = neo4j_connection
        self.llm_model = llm_model
        self.temperature = temperature
        self.duplicate_threshold = duplicate_threshold
        self.require_entity_existence = require_entity_existence
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
        self.validation_tools = ValidationTools(neo4j_connection, duplicate_threshold)
        self.tools = self.validation_tools.get_tools()

        # Bind tools to LLM
        self.llm_with_tools = self.llm.bind_tools(self.tools)

        # Build the graph
        self.graph = self._build_graph()

    def _get_system_prompt(self) -> str:
        """Get the system prompt for the validator."""
        return f"""You are a requirement validation specialist for a car rental system's
Neo4j knowledge graph.

Your task is to validate requirement candidates against the existing graph.
For each candidate, you must:

1. **Duplicate Detection**: Check for near-identical existing requirements
   - Similarity threshold: {self.duplicate_threshold:.0%}
   - If duplicate found: REJECT with reason

2. **Entity Resolution**: Match mentioned entities to graph nodes
   - BusinessObjects: Customer, Vehicle, Rental, Invoice, etc.
   - Messages: API requests, events, notifications
   - Track unresolved entities

3. **Metamodel Compatibility**: Verify FINIUS metamodel compliance
   - Valid requirement types
   - Appropriate relationship suggestions
   - GoBD traceability for compliance items

4. **Validation Scoring**: Compute overall confidence
   - Extraction confidence from source
   - Entity resolution success rate
   - Metamodel compatibility
   - Similar requirement presence

5. **Decision Making**:
   - ACCEPTED: High confidence, no duplicates, entities resolved
   - NEEDS_REVIEW: Medium confidence or unresolved entities
   - REJECTED: Duplicate, low confidence, or invalid structure

Entity Existence Mode: {'Strict' if self.require_entity_existence else 'Lenient'}
Reasoning Level: {self.reasoning_level}

For each validated requirement, generate:
- Suggested RID (new requirement ID)
- Suggested relationships to existing entities
- Compliance warnings if any"""

    def _load_context_node(self, state: RequirementValidatorState) -> RequirementValidatorState:
        """Load graph context for validation."""
        try:
            # Load existing entities
            tools = self.validation_tools
            state["existing_requirements"] = tools._load_requirements()
            state["existing_business_objects"] = tools._load_business_objects()
            state["existing_messages"] = tools._load_messages()
        except Exception as e:
            state["error_message"] = f"Failed to load graph context: {e}"

        return state

    def _validate_node(self, state: RequirementValidatorState) -> RequirementValidatorState:
        """Initial validation planning node."""
        candidates = state["candidates"]

        system_msg = self._get_system_prompt()
        user_msg = f"""Validate {len(candidates)} requirement candidates against the Neo4j graph.

Graph Context:
- {len(state.get('existing_requirements', []))} existing requirements
- {len(state.get('existing_business_objects', []))} business objects
- {len(state.get('existing_messages', []))} messages

For each candidate, validate and determine its status.
Begin validation process."""

        state["messages"] = [
            SystemMessage(content=system_msg),
            HumanMessage(content=user_msg),
        ]
        state["current_candidate_index"] = 0
        state["validated_requirements"] = []
        state["rejected_candidates"] = []
        state["is_complete"] = False

        # Get LLM response
        response = self.llm_with_tools.invoke(state["messages"])
        state["messages"].append(response)

        return state

    def _process_candidate_node(self, state: RequirementValidatorState) -> RequirementValidatorState:
        """Process current candidate for validation."""
        candidates = state["candidates"]
        current_idx = state["current_candidate_index"]

        if current_idx >= len(candidates):
            state["is_complete"] = True
            return state

        candidate = candidates[current_idx]

        # Add candidate context
        context_msg = f"""Validate candidate {current_idx + 1}/{len(candidates)}:

ID: {candidate.get('candidate_id')}
Text: {candidate.get('text', '')}
Type: {candidate.get('requirement_type')}
Confidence: {candidate.get('confidence_score', 0):.2f}
GoBD Relevant: {candidate.get('gobd_relevant', False)}
Mentioned Objects: {candidate.get('mentioned_objects', [])}
Mentioned Messages: {candidate.get('mentioned_messages', [])}

Use tools to:
1. Check for duplicates
2. Resolve mentioned entities
3. Verify metamodel compatibility
4. Generate suggested RID if valid"""

        state["messages"].append(HumanMessage(content=context_msg))

        # Get LLM response
        response = self.llm_with_tools.invoke(state["messages"])
        state["messages"].append(response)

        return state

    def _should_continue(self, state: RequirementValidatorState) -> str:
        """Determine if validation should continue."""
        messages = state["messages"]
        last_message = messages[-1] if messages else None

        # Check for tool calls
        if last_message and hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"

        # Check if complete
        if state.get("is_complete"):
            return "finalize"

        # Check iteration limit
        if state.get("iteration", 0) >= state.get("max_iterations", 50):
            return "finalize"

        # Move to next candidate
        return "next_candidate"

    def _increment_node(self, state: RequirementValidatorState) -> RequirementValidatorState:
        """Increment counters after tool execution."""
        state["iteration"] = state.get("iteration", 0) + 1
        return state

    def _next_candidate_node(self, state: RequirementValidatorState) -> RequirementValidatorState:
        """Move to the next candidate."""
        state["current_candidate_index"] = state.get("current_candidate_index", 0) + 1
        return state

    def _finalize_node(self, state: RequirementValidatorState) -> RequirementValidatorState:
        """Finalize validation and produce results."""
        # If validation wasn't done via LLM, do deterministic validation
        if not state.get("validated_requirements") and not state.get("rejected_candidates"):
            validated, rejected = self._deterministic_validation(state)
            state["validated_requirements"] = validated
            state["rejected_candidates"] = rejected

        state["is_complete"] = True
        return state

    def _deterministic_validation(
        self, state: RequirementValidatorState
    ) -> tuple[List[Dict], List[Dict]]:
        """Fallback deterministic validation without LLM."""
        validated = []
        rejected = []

        existing_reqs = state.get("existing_requirements", [])
        existing_bos = state.get("existing_business_objects", [])
        existing_msgs = state.get("existing_messages", [])

        for candidate in state.get("candidates", []):
            text = candidate.get("text", "")
            text_lower = text.lower().strip()

            # Check for duplicates
            is_duplicate = False
            duplicate_of = None

            for req in existing_reqs:
                req_text = (req.get("text") or "").lower().strip()
                if req_text:
                    similarity = SequenceMatcher(None, text_lower, req_text).ratio()
                    if similarity >= self.duplicate_threshold:
                        is_duplicate = True
                        duplicate_of = req.get("rid")
                        break

            if is_duplicate:
                rejected.append({
                    "candidate": candidate,
                    "rejection_reason": "duplicate",
                    "rejection_details": f"Near-duplicate of {duplicate_of}",
                })
                continue

            # Find similar requirements
            similar = []
            for req in existing_reqs:
                req_text = (req.get("text") or "").lower().strip()
                if req_text:
                    similarity = SequenceMatcher(None, text_lower, req_text).ratio()
                    if 0.5 <= similarity < self.duplicate_threshold:
                        similar.append({
                            "rid": req.get("rid"),
                            "name": req.get("name"),
                            "similarity_score": similarity,
                        })

            similar.sort(key=lambda x: x["similarity_score"], reverse=True)
            similar = similar[:5]

            # Resolve entities
            resolved_objects = []
            for obj in candidate.get("mentioned_objects", []):
                obj_lower = obj.lower()
                for bo in existing_bos:
                    if obj_lower in (bo.get("name") or "").lower():
                        resolved_objects.append({
                            "entity_id": bo.get("boid"),
                            "name": bo.get("name"),
                            "match_type": "substring",
                        })
                        break

            resolved_messages = []
            for msg in candidate.get("mentioned_messages", []):
                msg_lower = msg.lower()
                for m in existing_msgs:
                    if msg_lower in (m.get("name") or "").lower():
                        resolved_messages.append({
                            "entity_id": m.get("mid"),
                            "name": m.get("name"),
                            "match_type": "substring",
                        })
                        break

            # Compute validation score
            extraction_score = candidate.get("confidence_score", 0.5)
            entity_score = 1.0
            mentioned = len(candidate.get("mentioned_objects", [])) + len(candidate.get("mentioned_messages", []))
            resolved = len(resolved_objects) + len(resolved_messages)
            if mentioned > 0:
                entity_score = resolved / mentioned

            validation_score = (extraction_score * 0.6) + (entity_score * 0.4)

            # Determine status
            if validation_score >= 0.7:
                status = ValidationStatus.ACCEPTED.value
            elif validation_score >= 0.4:
                status = ValidationStatus.NEEDS_REVIEW.value
            else:
                status = ValidationStatus.REJECTED.value

            if status == ValidationStatus.REJECTED.value:
                rejected.append({
                    "candidate": candidate,
                    "rejection_reason": "low_confidence",
                    "rejection_details": f"Validation score {validation_score:.2f} below threshold",
                })
                continue

            # Generate RID
            next_num = len(validated) + len(existing_reqs) + 1
            suggested_rid = f"R-{next_num:04d}"

            # Build suggested relationships
            relationships = []
            for obj in resolved_objects:
                relationships.append({
                    "type": "RELATES_TO_OBJECT",
                    "target_id": obj["entity_id"],
                    "target_name": obj["name"],
                })
            for msg in resolved_messages:
                relationships.append({
                    "type": "RELATES_TO_MESSAGE",
                    "target_id": msg["entity_id"],
                    "target_name": msg["name"],
                })

            # Compliance warnings
            warnings = []
            if candidate.get("gobd_relevant") and not relationships:
                warnings.append("GoBD-relevant requirement lacks entity connections")

            validated.append({
                "candidate": candidate,
                "validation_status": status,
                "validation_score": validation_score,
                "similar_requirements": similar,
                "is_duplicate": False,
                "duplicate_of": None,
                "resolved_objects": resolved_objects,
                "resolved_messages": resolved_messages,
                "unresolved_entities": [],
                "suggested_rid": suggested_rid,
                "suggested_relationships": relationships,
                "metamodel_valid": True,
                "compliance_warnings": warnings,
            })

        return validated, rejected

    def _build_graph(self) -> StateGraph:
        """Build the LangGraph state machine."""
        workflow = StateGraph(RequirementValidatorState)

        # Add nodes
        workflow.add_node("load_context", self._load_context_node)
        workflow.add_node("validate", self._validate_node)
        workflow.add_node("process_candidate", self._process_candidate_node)
        workflow.add_node("tools", ToolNode(self.tools))
        workflow.add_node("increment", self._increment_node)
        workflow.add_node("next_candidate", self._next_candidate_node)
        workflow.add_node("finalize", self._finalize_node)

        # Set entry point
        workflow.set_entry_point("load_context")

        # Add edges
        workflow.add_edge("load_context", "validate")
        workflow.add_conditional_edges(
            "validate",
            self._should_continue,
            {
                "tools": "tools",
                "next_candidate": "process_candidate",
                "finalize": "finalize",
            },
        )
        workflow.add_edge("tools", "increment")
        workflow.add_conditional_edges(
            "increment",
            self._should_continue,
            {
                "tools": "tools",
                "next_candidate": "process_candidate",
                "finalize": "finalize",
            },
        )
        workflow.add_conditional_edges(
            "process_candidate",
            self._should_continue,
            {
                "tools": "tools",
                "next_candidate": "next_candidate",
                "finalize": "finalize",
            },
        )
        workflow.add_edge("next_candidate", "process_candidate")
        workflow.add_edge("finalize", END)

        return workflow.compile()

    def validate_requirements(
        self,
        candidates: List[RequirementCandidate],
        max_iterations: int = 50,
    ) -> Dict[str, Any]:
        """
        Validate requirement candidates against the graph.

        Args:
            candidates: List of RequirementCandidate objects
            max_iterations: Maximum processing iterations

        Returns:
            Dictionary with validation results
        """
        # Serialize inputs
        candidate_dicts = [
            c.to_dict() if hasattr(c, "to_dict") else c for c in candidates
        ]

        # Initialize state
        initial_state = RequirementValidatorState(
            messages=[],
            candidates=candidate_dicts,
            current_candidate_index=0,
            validated_requirements=[],
            rejected_candidates=[],
            existing_requirements=[],
            existing_business_objects=[],
            existing_messages=[],
            duplicate_similarity_threshold=self.duplicate_threshold,
            require_entity_existence=self.require_entity_existence,
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
            "validated_requirements": final_state.get("validated_requirements", []),
            "rejected_candidates": final_state.get("rejected_candidates", []),
            "is_complete": final_state.get("is_complete", False),
            "error_message": final_state.get("error_message"),
        }

    def validate_requirements_stream(
        self,
        candidates: List[RequirementCandidate],
        max_iterations: int = 50,
    ):
        """
        Validate requirements and yield state updates for streaming.

        Args:
            candidates: List of RequirementCandidate objects
            max_iterations: Maximum processing iterations

        Yields:
            Dictionary containing current state of the workflow
        """
        candidate_dicts = [
            c.to_dict() if hasattr(c, "to_dict") else c for c in candidates
        ]

        initial_state = RequirementValidatorState(
            messages=[],
            candidates=candidate_dicts,
            current_candidate_index=0,
            validated_requirements=[],
            rejected_candidates=[],
            existing_requirements=[],
            existing_business_objects=[],
            existing_messages=[],
            duplicate_similarity_threshold=self.duplicate_threshold,
            require_entity_existence=self.require_entity_existence,
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

def create_requirement_validator_agent(
    neo4j_connection: Neo4jConnection,
) -> RequirementValidatorAgent:
    """
    Create a requirement validator agent with configuration from config file.

    Args:
        neo4j_connection: Active Neo4j connection

    Returns:
        Configured RequirementValidatorAgent instance
    """
    config = load_config("llm_config.json")

    # Use document_ingestion config if available
    doc_config = config.get("document_ingestion", config.get("agent", {}))

    return RequirementValidatorAgent(
        neo4j_connection=neo4j_connection,
        llm_model=doc_config.get("model", "gpt-4o"),
        temperature=doc_config.get("temperature", 0.0),
        duplicate_threshold=doc_config.get("duplicate_similarity_threshold", 0.95),
        require_entity_existence=doc_config.get("require_entity_existence", False),
        reasoning_level=doc_config.get("reasoning_level", "medium"),
    )
