"""
Document Ingestion Supervisor

LangGraph-based supervisor agent that orchestrates the multi-agent document
ingestion pipeline. Coordinates document processing, requirement extraction,
validation, and integration stages.
"""

import os
import time
from typing import Dict, Any, List, TypedDict, Annotated, Sequence, Optional
from datetime import datetime
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from src.core.neo4j_utils import Neo4jConnection
from src.core.document_processor import DocumentProcessor
from src.core.document_models import (
    DocumentChunk,
    DocumentMetadata,
    ProcessingOptions,
    PipelineReport,
    PipelineStage,
    PipelineStatus,
    ProcessingStatus,
    ValidationStatus,
)
from src.core.config import load_config

from src.agents.document_processor_agent import (
    DocumentProcessorAgent,
    create_document_processor_agent,
)
from src.agents.requirement_extractor_agent import (
    RequirementExtractorAgent,
    create_requirement_extractor_agent,
)
from src.agents.requirement_validator_agent import (
    RequirementValidatorAgent,
    create_requirement_validator_agent,
)


# =============================================================================
# Pipeline State Definition
# =============================================================================

class DocumentIngestionPipelineState(TypedDict):
    """
    Top-level state for the entire ingestion pipeline.
    Managed by the supervisor agent.
    """
    # Messages for supervisor coordination
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # ─── Input ───────────────────────────────────────────────────
    document_path: str
    document_type: str
    processing_options: Dict[str, Any]

    # ─── Stage 1: Document Processing ────────────────────────────
    document_metadata: Dict[str, Any]
    chunks: List[Dict[str, Any]]
    document_processing_status: str
    document_processing_error: Optional[str]

    # ─── Stage 2: Requirement Extraction ─────────────────────────
    candidates: List[Dict[str, Any]]
    extraction_status: str
    extraction_stats: Dict[str, Any]

    # ─── Stage 3: Validation ─────────────────────────────────────
    validated_requirements: List[Dict[str, Any]]
    rejected_candidates: List[Dict[str, Any]]
    needs_review: List[Dict[str, Any]]
    validation_status: str

    # ─── Stage 4: Integration ────────────────────────────────────
    integration_results: List[Dict[str, Any]]
    integration_status: str

    # ─── Human-in-the-Loop ───────────────────────────────────────
    awaiting_human_review: bool
    human_decisions: List[Dict[str, Any]]

    # ─── Overall Control ─────────────────────────────────────────
    current_stage: str
    pipeline_status: str
    error_log: List[str]

    # ─── Audit Trail ─────────────────────────────────────────────
    started_at: str
    completed_at: Optional[str]
    stage_timings: Dict[str, float]


# =============================================================================
# Document Ingestion Supervisor
# =============================================================================

class DocumentIngestionSupervisor:
    """
    Orchestrates the multi-agent document ingestion pipeline.

    Responsibilities:
    - Route tasks to specialized agents
    - Handle stage transitions
    - Manage human-in-the-loop pauses
    - Handle errors and recovery
    - Track overall progress
    """

    def __init__(
        self,
        neo4j_connection: Neo4jConnection,
        llm_model: str = "gpt-4o",
        processing_options: Optional[ProcessingOptions] = None,
    ):
        """
        Initialize the document ingestion supervisor.

        Args:
            neo4j_connection: Active Neo4j connection
            llm_model: LLM model to use for coordination
            processing_options: Pipeline configuration options
        """
        self.neo4j = neo4j_connection
        self.llm_model = llm_model
        self.options = processing_options or ProcessingOptions()

        # Initialize LLM for supervisor reasoning
        llm_kwargs = {
            "model": self.llm_model,
            "temperature": 0.0,
            "api_key": os.getenv("OPENAI_API_KEY"),
        }
        base_url = os.getenv("LLM_BASE_URL")
        if base_url:
            llm_kwargs["base_url"] = base_url

        self.llm = ChatOpenAI(**llm_kwargs)

        # Initialize sub-agents (lazy loaded)
        self._document_processor = None
        self._extractor = None
        self._validator = None

        # Build the supervisor graph
        self.graph = self._build_supervisor_graph()

    @property
    def document_processor(self) -> DocumentProcessor:
        """Lazy load document processor."""
        if self._document_processor is None:
            self._document_processor = DocumentProcessor(
                chunking_strategy=self.options.chunking_strategy
            )
        return self._document_processor

    @property
    def extractor(self) -> RequirementExtractorAgent:
        """Lazy load requirement extractor agent."""
        if self._extractor is None:
            self._extractor = create_requirement_extractor_agent()
        return self._extractor

    @property
    def validator(self) -> RequirementValidatorAgent:
        """Lazy load requirement validator agent."""
        if self._validator is None:
            self._validator = create_requirement_validator_agent(self.neo4j)
        return self._validator

    def _build_supervisor_graph(self) -> StateGraph:
        """Build the LangGraph state machine for orchestration."""
        workflow = StateGraph(DocumentIngestionPipelineState)

        # Add nodes
        workflow.add_node("route", self._route_node)
        workflow.add_node("document_processing", self._document_processing_node)
        workflow.add_node("extraction", self._extraction_node)
        workflow.add_node("validation", self._validation_node)
        workflow.add_node("human_review", self._human_review_node)
        workflow.add_node("integration", self._integration_node)
        workflow.add_node("finalize", self._finalize_node)
        workflow.add_node("error_handler", self._error_handler_node)

        # Entry point
        workflow.set_entry_point("route")

        # Conditional edges from router
        workflow.add_conditional_edges(
            "route",
            self._determine_next_stage,
            {
                "document_processing": "document_processing",
                "extraction": "extraction",
                "validation": "validation",
                "human_review": "human_review",
                "integration": "integration",
                "finalize": "finalize",
                "error": "error_handler",
            },
        )

        # Stage completion edges (all go back to router)
        for stage in ["document_processing", "extraction", "validation", "integration"]:
            workflow.add_edge(stage, "route")

        workflow.add_edge("human_review", "route")
        workflow.add_edge("error_handler", "finalize")
        workflow.add_edge("finalize", END)

        return workflow.compile()

    def _determine_next_stage(self, state: DocumentIngestionPipelineState) -> str:
        """
        Determine which stage to execute next based on current state.
        """
        # Check for fatal errors
        if state.get("pipeline_status") == PipelineStatus.FAILED.value:
            return "error"

        # Stage progression logic
        if state.get("document_processing_status") != ProcessingStatus.COMPLETED.value:
            return "document_processing"

        if state.get("extraction_status") != ProcessingStatus.COMPLETED.value:
            return "extraction"

        if state.get("validation_status") != ProcessingStatus.COMPLETED.value:
            return "validation"

        # Human-in-the-loop check
        needs_review = state.get("needs_review", [])
        auto_integrate = state.get("processing_options", {}).get("auto_integrate", False)

        if needs_review and not auto_integrate:
            if not state.get("awaiting_human_review"):
                return "human_review"

        if state.get("integration_status") != ProcessingStatus.COMPLETED.value:
            # Skip integration if no requirements to integrate
            validated = state.get("validated_requirements", [])
            accepted = [
                v for v in validated
                if v.get("validation_status") == ValidationStatus.ACCEPTED.value
            ]
            if not accepted:
                state["integration_status"] = ProcessingStatus.COMPLETED.value
                return "finalize"
            return "integration"

        return "finalize"

    def _route_node(self, state: DocumentIngestionPipelineState) -> DocumentIngestionPipelineState:
        """
        Router node - logs current state and prepares for routing.
        """
        current_stage = state.get("current_stage", "starting")

        # Add routing message
        status_msg = f"""Pipeline Status Update:
Current Stage: {current_stage}
Document Processing: {state.get('document_processing_status', 'pending')}
Extraction: {state.get('extraction_status', 'pending')}
Validation: {state.get('validation_status', 'pending')}
Integration: {state.get('integration_status', 'pending')}
Errors: {len(state.get('error_log', []))}"""

        state["messages"].append(SystemMessage(content=status_msg))
        return state

    def _document_processing_node(
        self, state: DocumentIngestionPipelineState
    ) -> DocumentIngestionPipelineState:
        """
        Stage 1: Document processing - extract and chunk document.
        """
        state["current_stage"] = PipelineStage.DOCUMENT_PROCESSING.value
        state["document_processing_status"] = ProcessingStatus.IN_PROGRESS.value

        start_time = time.time()

        try:
            document_path = state["document_path"]

            # Process document
            metadata, chunks = self.document_processor.process(document_path)

            # Store results
            state["document_metadata"] = metadata.to_dict()
            state["chunks"] = [c.to_dict() for c in chunks]
            state["document_processing_status"] = ProcessingStatus.COMPLETED.value

            # Log success
            msg = f"Document processing complete: {len(chunks)} chunks from {metadata.page_count} pages"
            state["messages"].append(AIMessage(content=msg))

        except Exception as e:
            state["document_processing_status"] = ProcessingStatus.FAILED.value
            state["document_processing_error"] = str(e)
            state["error_log"].append(f"Document processing failed: {e}")
            state["pipeline_status"] = PipelineStatus.FAILED.value

        # Record timing
        elapsed = time.time() - start_time
        state["stage_timings"]["document_processing"] = elapsed

        return state

    def _extraction_node(
        self, state: DocumentIngestionPipelineState
    ) -> DocumentIngestionPipelineState:
        """
        Stage 2: Requirement extraction from chunks.
        """
        state["current_stage"] = PipelineStage.EXTRACTION.value
        state["extraction_status"] = ProcessingStatus.IN_PROGRESS.value

        start_time = time.time()

        try:
            chunks = state.get("chunks", [])
            metadata = state.get("document_metadata", {})

            # Extract requirements
            result = self.extractor.extract_requirements(
                chunks=chunks,
                metadata=metadata,
                max_iterations=20,
            )

            # Store results
            state["candidates"] = result.get("candidates", [])
            state["extraction_stats"] = result.get("extraction_stats", {})
            state["extraction_status"] = ProcessingStatus.COMPLETED.value

            # Log success
            msg = f"Extraction complete: {len(state['candidates'])} candidates found"
            state["messages"].append(AIMessage(content=msg))

        except Exception as e:
            state["extraction_status"] = ProcessingStatus.FAILED.value
            state["error_log"].append(f"Extraction failed: {e}")
            # Continue pipeline with empty candidates
            state["candidates"] = []
            state["extraction_status"] = ProcessingStatus.COMPLETED.value

        # Record timing
        elapsed = time.time() - start_time
        state["stage_timings"]["extraction"] = elapsed

        return state

    def _validation_node(
        self, state: DocumentIngestionPipelineState
    ) -> DocumentIngestionPipelineState:
        """
        Stage 3: Validate candidates against the graph.
        """
        state["current_stage"] = PipelineStage.VALIDATION.value
        state["validation_status"] = ProcessingStatus.IN_PROGRESS.value

        start_time = time.time()

        try:
            candidates = state.get("candidates", [])

            if not candidates:
                state["validated_requirements"] = []
                state["rejected_candidates"] = []
                state["needs_review"] = []
                state["validation_status"] = ProcessingStatus.COMPLETED.value
                return state

            # Validate requirements
            result = self.validator.validate_requirements(
                candidates=candidates,
                max_iterations=50,
            )

            # Store and categorize results
            validated = result.get("validated_requirements", [])
            rejected = result.get("rejected_candidates", [])

            state["validated_requirements"] = validated
            state["rejected_candidates"] = rejected

            # Separate needs_review items
            needs_review = [
                v for v in validated
                if v.get("validation_status") == ValidationStatus.NEEDS_REVIEW.value
            ]
            state["needs_review"] = needs_review

            state["validation_status"] = ProcessingStatus.COMPLETED.value

            # Log success
            accepted = len([v for v in validated if v.get("validation_status") == ValidationStatus.ACCEPTED.value])
            msg = f"Validation complete: {accepted} accepted, {len(needs_review)} need review, {len(rejected)} rejected"
            state["messages"].append(AIMessage(content=msg))

        except Exception as e:
            state["validation_status"] = ProcessingStatus.FAILED.value
            state["error_log"].append(f"Validation failed: {e}")
            # Continue with empty results
            state["validated_requirements"] = []
            state["rejected_candidates"] = []
            state["needs_review"] = []
            state["validation_status"] = ProcessingStatus.COMPLETED.value

        # Record timing
        elapsed = time.time() - start_time
        state["stage_timings"]["validation"] = elapsed

        return state

    def _human_review_node(
        self, state: DocumentIngestionPipelineState
    ) -> DocumentIngestionPipelineState:
        """
        Human-in-the-loop stage - pause for review.
        """
        state["awaiting_human_review"] = True
        state["pipeline_status"] = PipelineStatus.PAUSED.value

        # Log pause
        needs_review = state.get("needs_review", [])
        msg = f"Pipeline paused: {len(needs_review)} requirements need human review"
        state["messages"].append(AIMessage(content=msg))

        return state

    def _integration_node(
        self, state: DocumentIngestionPipelineState
    ) -> DocumentIngestionPipelineState:
        """
        Stage 4: Integrate validated requirements into the graph.
        """
        state["current_stage"] = PipelineStage.INTEGRATION.value
        state["integration_status"] = ProcessingStatus.IN_PROGRESS.value

        start_time = time.time()

        try:
            validated = state.get("validated_requirements", [])
            results = []

            # Only integrate accepted requirements
            to_integrate = [
                v for v in validated
                if v.get("validation_status") == ValidationStatus.ACCEPTED.value
            ]

            for req in to_integrate:
                try:
                    result = self._integrate_requirement(req)
                    results.append(result)
                except Exception as e:
                    results.append({
                        "requirement_id": req.get("suggested_rid", "unknown"),
                        "success": False,
                        "error_message": str(e),
                    })
                    state["error_log"].append(f"Integration failed for {req.get('suggested_rid')}: {e}")

            state["integration_results"] = results
            state["integration_status"] = ProcessingStatus.COMPLETED.value

            # Log success
            success_count = sum(1 for r in results if r.get("success", False))
            msg = f"Integration complete: {success_count}/{len(to_integrate)} requirements integrated"
            state["messages"].append(AIMessage(content=msg))

        except Exception as e:
            state["integration_status"] = ProcessingStatus.FAILED.value
            state["error_log"].append(f"Integration stage failed: {e}")
            state["integration_results"] = []
            state["integration_status"] = ProcessingStatus.COMPLETED.value

        # Record timing
        elapsed = time.time() - start_time
        state["stage_timings"]["integration"] = elapsed

        return state

    def _integrate_requirement(self, validated_req: Dict[str, Any]) -> Dict[str, Any]:
        """
        Integrate a single validated requirement into the graph.
        """
        candidate = validated_req.get("candidate", {})
        rid = validated_req.get("suggested_rid", f"R-{id(validated_req):08x}")

        # Build MERGE query for requirement
        cypher = """
        MERGE (r:Requirement {rid: $rid})
        SET r.name = $name,
            r.text = $text,
            r.type = $type,
            r.goBDRelevant = $gobd_relevant,
            r.source = $source,
            r.createdAt = datetime()
        RETURN r.rid AS rid
        """

        params = {
            "rid": rid,
            "name": candidate.get("text", "")[:100],
            "text": candidate.get("text", ""),
            "type": candidate.get("requirement_type", "functional"),
            "gobd_relevant": candidate.get("gobd_relevant", False),
            "source": "document_ingestion_pipeline",
        }

        # Execute query
        self.neo4j.execute_query(cypher, params)

        # Create relationships
        relationships_created = []
        for rel in validated_req.get("suggested_relationships", []):
            rel_type = rel.get("type", "RELATES_TO_OBJECT")
            target_id = rel.get("target_id")

            if not target_id:
                continue

            if "OBJECT" in rel_type:
                rel_cypher = f"""
                MATCH (r:Requirement {{rid: $rid}})
                MATCH (bo:BusinessObject {{boid: $target_id}})
                MERGE (r)-[:{rel_type}]->(bo)
                """
            else:
                rel_cypher = f"""
                MATCH (r:Requirement {{rid: $rid}})
                MATCH (m:Message {{mid: $target_id}})
                MERGE (r)-[:{rel_type}]->(m)
                """

            try:
                self.neo4j.execute_query(rel_cypher, {"rid": rid, "target_id": target_id})
                relationships_created.append(rel_type)
            except Exception:
                pass  # Skip failed relationships

        return {
            "requirement_id": rid,
            "success": True,
            "created_node": True,
            "created_relationships": relationships_created,
        }

    def _error_handler_node(
        self, state: DocumentIngestionPipelineState
    ) -> DocumentIngestionPipelineState:
        """
        Handle pipeline errors.
        """
        errors = state.get("error_log", [])
        msg = f"Pipeline encountered {len(errors)} errors:\n" + "\n".join(f"- {e}" for e in errors[:10])
        state["messages"].append(AIMessage(content=msg))
        return state

    def _finalize_node(
        self, state: DocumentIngestionPipelineState
    ) -> DocumentIngestionPipelineState:
        """
        Finalize pipeline and generate report.
        """
        state["completed_at"] = datetime.utcnow().isoformat()
        state["pipeline_status"] = PipelineStatus.COMPLETED.value

        # Generate summary
        validated = state.get("validated_requirements", [])
        accepted = len([v for v in validated if v.get("validation_status") == ValidationStatus.ACCEPTED.value])
        needs_review = len(state.get("needs_review", []))
        rejected = len(state.get("rejected_candidates", []))
        integrated = len([r for r in state.get("integration_results", []) if r.get("success")])

        summary = f"""
Pipeline Complete!
==================
Document: {state.get('document_path')}
Duration: {sum(state.get('stage_timings', {}).values()):.1f}s

Results:
- Chunks processed: {len(state.get('chunks', []))}
- Candidates extracted: {len(state.get('candidates', []))}
- Accepted: {accepted}
- Needs Review: {needs_review}
- Rejected: {rejected}
- Integrated to Graph: {integrated}

Errors: {len(state.get('error_log', []))}
"""

        state["messages"].append(AIMessage(content=summary))

        return state

    def process_document(
        self,
        document_path: str,
        options: Optional[ProcessingOptions] = None,
    ) -> Dict[str, Any]:
        """
        Process a document through the full ingestion pipeline.

        Args:
            document_path: Path to the document file
            options: Processing options (uses defaults if not provided)

        Returns:
            Dictionary with pipeline results
        """
        from pathlib import Path

        # Update options if provided
        if options:
            self.options = options

        path = Path(document_path)

        # Initialize state
        initial_state = DocumentIngestionPipelineState(
            messages=[],
            document_path=document_path,
            document_type=path.suffix.lower().lstrip("."),
            processing_options=self.options.to_dict(),
            document_metadata={},
            chunks=[],
            document_processing_status=ProcessingStatus.PENDING.value,
            document_processing_error=None,
            candidates=[],
            extraction_status=ProcessingStatus.PENDING.value,
            extraction_stats={},
            validated_requirements=[],
            rejected_candidates=[],
            needs_review=[],
            validation_status=ProcessingStatus.PENDING.value,
            integration_results=[],
            integration_status=ProcessingStatus.PENDING.value,
            awaiting_human_review=False,
            human_decisions=[],
            current_stage="starting",
            pipeline_status=PipelineStatus.RUNNING.value,
            error_log=[],
            started_at=datetime.utcnow().isoformat(),
            completed_at=None,
            stage_timings={},
        )

        # Run the pipeline
        final_state = self.graph.invoke(
            initial_state, config={"recursion_limit": 100}
        )

        # Build report
        return self._build_report(final_state)

    def process_document_stream(
        self,
        document_path: str,
        options: Optional[ProcessingOptions] = None,
    ):
        """
        Process a document and yield state updates for streaming.

        Args:
            document_path: Path to the document file
            options: Processing options

        Yields:
            Dictionary containing current state of the workflow
        """
        from pathlib import Path

        if options:
            self.options = options

        path = Path(document_path)

        initial_state = DocumentIngestionPipelineState(
            messages=[],
            document_path=document_path,
            document_type=path.suffix.lower().lstrip("."),
            processing_options=self.options.to_dict(),
            document_metadata={},
            chunks=[],
            document_processing_status=ProcessingStatus.PENDING.value,
            document_processing_error=None,
            candidates=[],
            extraction_status=ProcessingStatus.PENDING.value,
            extraction_stats={},
            validated_requirements=[],
            rejected_candidates=[],
            needs_review=[],
            validation_status=ProcessingStatus.PENDING.value,
            integration_results=[],
            integration_status=ProcessingStatus.PENDING.value,
            awaiting_human_review=False,
            human_decisions=[],
            current_stage="starting",
            pipeline_status=PipelineStatus.RUNNING.value,
            error_log=[],
            started_at=datetime.utcnow().isoformat(),
            completed_at=None,
            stage_timings={},
        )

        return self.graph.stream(
            initial_state, config={"recursion_limit": 100}
        )

    def _build_report(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Build a structured report from pipeline state."""
        validated = state.get("validated_requirements", [])
        accepted = [v for v in validated if v.get("validation_status") == ValidationStatus.ACCEPTED.value]
        needs_review = state.get("needs_review", [])
        rejected = state.get("rejected_candidates", [])
        integrated = [r for r in state.get("integration_results", []) if r.get("success")]

        return {
            "document_path": state.get("document_path"),
            "document_metadata": state.get("document_metadata", {}),
            "pipeline_status": state.get("pipeline_status"),
            "started_at": state.get("started_at"),
            "completed_at": state.get("completed_at"),
            "stage_timings": state.get("stage_timings", {}),
            "summary": {
                "chunks_processed": len(state.get("chunks", [])),
                "candidates_extracted": len(state.get("candidates", [])),
                "accepted_count": len(accepted),
                "needs_review_count": len(needs_review),
                "rejected_count": len(rejected),
                "integrated_count": len(integrated),
            },
            "extraction_stats": state.get("extraction_stats", {}),
            "validated_requirements": validated,
            "rejected_candidates": rejected,
            "integration_results": state.get("integration_results", []),
            "errors": state.get("error_log", []),
        }


# =============================================================================
# Factory Function
# =============================================================================

def create_document_ingestion_supervisor(
    neo4j_connection: Neo4jConnection,
    options: Optional[ProcessingOptions] = None,
) -> DocumentIngestionSupervisor:
    """
    Create a document ingestion supervisor with configuration from config file.

    Args:
        neo4j_connection: Active Neo4j connection
        options: Optional processing options

    Returns:
        Configured DocumentIngestionSupervisor instance
    """
    config = load_config("llm_config.json")

    # Use document_ingestion config if available
    doc_config = config.get("document_ingestion", config.get("agent", {}))

    # Build options from config if not provided
    if options is None:
        options = ProcessingOptions(
            extraction_mode=doc_config.get("extraction_mode", "balanced"),
            min_confidence=doc_config.get("min_confidence", 0.6),
            auto_integrate=doc_config.get("auto_integrate", False),
            parallel_extraction=doc_config.get("parallel_extraction", True),
            max_candidates=doc_config.get("max_candidates", 1000),
            duplicate_similarity_threshold=doc_config.get("duplicate_similarity_threshold", 0.95),
            chunking_strategy=doc_config.get("chunking_strategy", "legal"),
            max_chunk_size=doc_config.get("max_chunk_size", 1000),
            chunk_overlap=doc_config.get("chunk_overlap", 200),
        )

    return DocumentIngestionSupervisor(
        neo4j_connection=neo4j_connection,
        llm_model=doc_config.get("model", "gpt-4o"),
        processing_options=options,
    )
