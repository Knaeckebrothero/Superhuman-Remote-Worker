# Multi-Agent Document Ingestion Pipeline

## Executive Summary

This document describes a multi-agent system for processing large documents (e.g., 100-page legal/compliance texts) and extracting, validating, and integrating requirement candidates into the existing Neo4j knowledge graph.

**The Problem:** Users want to upload large documents and have the system automatically:
1. Extract potential requirements from unstructured text
2. Validate each candidate against the existing knowledge graph
3. Integrate verified requirements into the FINIUS metamodel

**The Solution:** A four-stage pipeline using specialized LangGraph agents:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        DOCUMENT INGESTION PIPELINE                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌───────────┐ │
│  │   Document   │    │ Requirement  │    │ Requirement  │    │ Existing  │ │
│  │  Processor   │───▶│  Extractor   │───▶│  Validator   │───▶│   Graph   │ │
│  │    Agent     │    │    Agent     │    │    Agent     │    │   Agent   │ │
│  └──────────────┘    └──────────────┘    └──────────────┘    └───────────┘ │
│        │                    │                   │                  │        │
│        ▼                    ▼                   ▼                  ▼        │
│   Chunked Text      Requirement List     Validated List      Final Report  │
│   + Metadata        + Confidence         + Graph Links       + Integration │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Implementation Status

> **Status: IMPLEMENTED** (January 2026)

The pipeline has been fully implemented following this design document. Below is the mapping from design to implementation:

### Implemented Files

| Design Component | Implemented File | Status |
|-----------------|------------------|--------|
| Document Processor Agent | `src/agents/document_processor_agent.py` | ✅ Complete |
| Requirement Extractor Agent | `src/agents/requirement_extractor_agent.py` | ✅ Complete |
| Requirement Validator Agent | `src/agents/requirement_validator_agent.py` | ✅ Complete |
| Supervisor/Orchestrator | `src/agents/document_ingestion_supervisor.py` | ✅ Complete |
| Document Processing Core | `src/core/document_processor.py` | ✅ Complete |
| Data Models | `src/core/document_models.py` | ✅ Complete |
| Streamlit UI | `src/ui/document_ingestion.py` | ✅ Complete |
| Configuration | `config/llm_config.json` → `document_ingestion` | ✅ Complete |
| Prompts | `config/prompts/{extraction,validation,supervisor}_system.txt` | ✅ Complete |

### Key Implementation Details

**Data Models (`src/core/document_models.py`):**
- `DocumentChunk`, `DocumentMetadata` - Document processing types
- `RequirementCandidate`, `RequirementType` - Extraction types
- `ValidatedRequirement`, `RejectedCandidate`, `ValidationStatus` - Validation types
- `ProcessingOptions`, `PipelineReport` - Configuration and reporting

**Document Processing (`src/core/document_processor.py`):**
- `DocumentExtractor` - PDF (pdfplumber), DOCX (python-docx), TXT, HTML extraction
- `DocumentChunker` - Legal/technical/general chunking strategies with boundary detection
- `DocumentProcessor` - High-level processor combining extraction + chunking
- Language detection (German/English) and document type classification

**Agents:**
- All agents use LangGraph `StateGraph` with `TypedDict` state
- Tools decorated with `@tool` from `langchain_core.tools`
- Streaming support via `.process_*_stream()` methods
- Factory functions: `create_*_agent()` for configuration-driven instantiation

**Supervisor Pattern:**
- Conditional routing based on stage completion status
- Human-in-the-loop support (pauses when `needs_review` items exist and `auto_integrate=False`)
- Error handling with graceful degradation (logs errors, continues pipeline)
- Stage timing and audit trail

### Usage

**Programmatic:**
```python
from src.agents import create_document_ingestion_supervisor
from src.core import create_neo4j_connection

conn = create_neo4j_connection()
conn.connect()

supervisor = create_document_ingestion_supervisor(conn)
result = supervisor.process_document("path/to/document.pdf")

print(f"Candidates: {result['summary']['candidates_extracted']}")
print(f"Integrated: {result['summary']['integrated_count']}")
```

**Streamlit UI:**
1. Run `streamlit run main.py`
2. Connect to Neo4j on the Home page
3. Navigate to "Document Ingestion" page
4. Upload a document (PDF, DOCX, TXT)
5. Configure settings in sidebar
6. Click "Process Document"
7. Review results and export report

### Dependencies Added

```
pdfplumber>=0.10.0          # PDF extraction
python-docx>=1.1.0          # DOCX extraction
beautifulsoup4>=4.12.0      # HTML parsing
langdetect>=1.0.9           # Language detection
```

### Design Decisions Made

| Question from Design | Decision |
|---------------------|----------|
| Embedding model for similarity? | String similarity (SequenceMatcher) for MVP; sentence-transformers optional |
| Store candidates before validation? | In-memory (via LangGraph state) |
| Duplicate threshold? | Configurable, default 0.95 |
| Parallel processing? | Sequential in current implementation; parallel_extraction flag for future |

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Agent Specifications](#2-agent-specifications)
3. [Shared State Design](#3-shared-state-design)
4. [Supervisor Orchestration](#4-supervisor-orchestration)
5. [Document Processing Strategy](#5-document-processing-strategy)
6. [Tool Definitions](#6-tool-definitions)
7. [Implementation Roadmap](#7-implementation-roadmap)
8. [Integration Points](#8-integration-points)
9. [Error Handling & Recovery](#9-error-handling--recovery)
10. [Open Questions](#10-open-questions)

---

## 1. Architecture Overview

### 1.1 Design Principles

Following established patterns from the existing `RequirementGraphAgent`:

| Principle | Implementation |
|-----------|----------------|
| **Separation of Concerns** | Each agent has a single responsibility |
| **Shared State** | Agents communicate via `TypedDict` state, not direct messaging |
| **Tool-Based Actions** | All external interactions go through `@tool` decorated functions |
| **Iterative Processing** | Agents can loop with iteration limits |
| **Streaming Support** | All agents support `.stream()` for UI responsiveness |
| **Configuration-Driven** | No hardcoded values; use `config/llm_config.json` |

### 1.2 Multi-Agent Pattern: Supervisor with Handoffs

We use the **Supervisor Pattern** where a coordinator manages specialized agents:

```
                         ┌─────────────────┐
                         │   Supervisor    │
                         │  (Orchestrator) │
                         └────────┬────────┘
                                  │
            ┌─────────────────────┼─────────────────────┐
            │                     │                     │
            ▼                     ▼                     ▼
    ┌───────────────┐    ┌───────────────┐    ┌───────────────┐
    │   Document    │    │  Extraction   │    │  Validation   │
    │   Processor   │    │    Agent      │    │    Agent      │
    └───────────────┘    └───────────────┘    └───────────────┘
                                                      │
                                                      ▼
                                              ┌───────────────┐
                                              │ Graph Agent   │
                                              │  (existing)   │
                                              └───────────────┘
```

**Why Supervisor Pattern?**
- Central control over document processing flow
- Easy to add human-in-the-loop checkpoints
- Clean error recovery (supervisor can retry or skip)
- Supports parallel processing of chunks (scatter-gather)

### 1.3 Processing Flow

```
User uploads document
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│ STAGE 1: Document Processing                                    │
│ ─────────────────────────────────────────────────────────────── │
│ • Extract text from PDF/DOCX                                    │
│ • Detect document structure (sections, headings, clauses)       │
│ • Apply legal-text-aware chunking                               │
│ • Extract metadata (title, date, source, jurisdiction)          │
│ Output: List[DocumentChunk] with positions + context            │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│ STAGE 2: Requirement Extraction                                  │
│ ─────────────────────────────────────────────────────────────── │
│ For each chunk (can parallelize):                               │
│ • Identify requirement-like statements                          │
│ • Classify type (functional, non-functional, constraint, etc.)  │
│ • Detect GoBD relevance indicators                              │
│ • Assign confidence score (0.0-1.0)                             │
│ • Extract referenced entities (business objects, messages)      │
│ Output: List[RequirementCandidate] with provenance              │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│ STAGE 3: Requirement Validation                                  │
│ ─────────────────────────────────────────────────────────────── │
│ For each candidate (sequential - needs graph context):          │
│ • Query Neo4j for similar/duplicate requirements                │
│ • Check if referenced BusinessObjects/Messages exist            │
│ • Validate against metamodel constraints                        │
│ • Web search for regulation context (optional)                  │
│ • Compute validation score + decision                           │
│ Output: List[ValidatedRequirement] with graph_links             │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│ STAGE 4: Graph Integration (Existing Agent)                      │
│ ─────────────────────────────────────────────────────────────── │
│ For each validated requirement:                                 │
│ • Create/merge Requirement node                                 │
│ • Establish relationships to BusinessObjects/Messages           │
│ • Run metamodel compliance validation                           │
│ • Generate traceability report                                  │
│ Output: IntegrationReport with created nodes + relationships    │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼
   Final Report
```

---

## 2. Agent Specifications

### 2.1 Document Processor Agent

**Purpose:** Transform raw documents into structured, LLM-ready chunks.

**Why a separate agent?** Document processing is complex:
- Different formats (PDF, DOCX, TXT, HTML)
- Legal documents have specific structure (articles, sections, paragraphs)
- Need to preserve context across chunk boundaries
- Metadata extraction requires document understanding

```python
class DocumentProcessorState(TypedDict):
    """State for document processing agent."""
    # Input
    document_path: str
    document_type: str  # pdf, docx, txt, html

    # Processing state
    raw_text: str
    detected_structure: Dict[str, Any]  # sections, headings hierarchy
    chunks: List[DocumentChunk]

    # Metadata
    metadata: DocumentMetadata

    # Control
    processing_status: str  # pending, in_progress, completed, failed
    error_message: Optional[str]
```

**Document Chunk Structure:**
```python
@dataclass
class DocumentChunk:
    chunk_id: str                    # Unique identifier
    text: str                        # Chunk content
    start_position: int              # Character offset in original
    end_position: int
    section_hierarchy: List[str]     # ["Chapter 3", "Section 3.2", "Paragraph a"]
    chunk_index: int                 # Sequential order
    overlap_with_previous: int       # Characters shared with prev chunk
    estimated_tokens: int

@dataclass
class DocumentMetadata:
    title: str
    source: str                      # Filename or URL
    document_type: str               # legal, technical, policy, etc.
    detected_language: str
    page_count: int
    extraction_timestamp: datetime
    jurisdiction: Optional[str]      # For legal docs (DE, EU, etc.)
    version: Optional[str]
```

**Tools:**
| Tool | Description |
|------|-------------|
| `extract_pdf_text` | Extract text from PDF with layout awareness |
| `extract_docx_text` | Extract text from Word documents |
| `detect_document_structure` | Identify sections, headings, lists |
| `chunk_document` | Apply chunking strategy with overlap |
| `extract_document_metadata` | Pull title, date, author, etc. |

**Chunking Strategy for Legal Documents:**

Legal texts require special handling (see research findings):

```python
class LegalChunkingStrategy:
    """
    Hierarchical chunking that respects legal document structure.

    Legal documents have nested structure:
    - Chapters/Parts (top level)
    - Articles/Sections (mid level)
    - Paragraphs/Clauses (atomic units)
    - Cross-references that span sections
    """

    def __init__(
        self,
        max_chunk_size: int = 1000,      # Characters
        overlap_size: int = 200,          # For context continuity
        respect_boundaries: bool = True,  # Don't split mid-clause
    ):
        self.max_chunk_size = max_chunk_size
        self.overlap_size = overlap_size
        self.respect_boundaries = respect_boundaries

    def chunk(self, text: str, structure: Dict) -> List[DocumentChunk]:
        """
        1. Parse structure to identify clause boundaries
        2. Create chunks at natural boundaries
        3. Add overlap for cross-reference context
        4. Preserve section hierarchy in chunk metadata
        """
        pass
```

### 2.2 Requirement Extractor Agent

**Purpose:** Identify and classify requirement candidates from document chunks.

**Why a separate agent?** Extraction is domain-specific:
- Requirements have linguistic patterns ("must", "shall", "required to")
- Need to distinguish requirements from definitions, explanations, examples
- Must classify by type and relevance
- Context from surrounding text matters

```python
class RequirementExtractorState(TypedDict):
    """State for requirement extraction agent."""
    # Input
    chunks: List[DocumentChunk]
    document_metadata: DocumentMetadata

    # Processing state
    current_chunk_index: int
    candidates: List[RequirementCandidate]

    # Configuration
    extraction_mode: str              # strict, balanced, permissive
    min_confidence_threshold: float   # 0.0-1.0

    # Control
    iteration: int
    max_iterations: int
    is_complete: bool
```

**Requirement Candidate Structure:**
```python
@dataclass
class RequirementCandidate:
    candidate_id: str                 # Generated ID
    text: str                         # Extracted requirement text
    source_chunk_id: str              # Provenance tracking
    source_position: Tuple[int, int]  # Start, end in original document

    # Classification
    requirement_type: str             # functional, non_functional, constraint, compliance
    confidence_score: float           # 0.0-1.0 extraction confidence

    # GoBD-specific
    gobd_relevant: bool
    gobd_indicators: List[str]        # Keywords that triggered GoBD flag

    # Extracted entities
    mentioned_objects: List[str]      # Potential BusinessObject references
    mentioned_messages: List[str]     # Potential Message references
    mentioned_requirements: List[str] # References to other requirements

    # Context
    section_context: str              # Section where found
    surrounding_context: str          # Sentences before/after
```

**Tools:**
| Tool | Description |
|------|-------------|
| `identify_requirements` | Find requirement-like statements in text |
| `classify_requirement` | Determine type (functional, constraint, etc.) |
| `assess_gobd_relevance` | Check for GoBD indicators |
| `extract_entity_mentions` | Find referenced business objects/messages |
| `compute_extraction_confidence` | Score based on linguistic signals |

**Extraction Heuristics:**

```python
# Linguistic patterns for requirement detection
REQUIREMENT_PATTERNS = {
    "modal_verbs": [
        r"\b(must|shall|should|will|may)\b",
        r"\b(muss|soll|sollte|wird|darf)\b",  # German
    ],
    "obligation_phrases": [
        r"\b(required to|obligated to|necessary to)\b",
        r"\b(verpflichtet|erforderlich|notwendig)\b",
    ],
    "constraint_patterns": [
        r"\b(at least|at most|within|no more than|maximum|minimum)\b",
        r"\b(mindestens|höchstens|maximal|minimal)\b",
    ],
}

# GoBD relevance indicators
GOBD_INDICATORS = [
    "aufbewahrung",           # retention
    "nachvollziehbar",        # traceable
    "unveränderbar",          # immutable
    "buchung",                # booking/posting
    "beleg",                  # document/voucher
    "rechnung",               # invoice
    "archivierung",           # archiving
    "revisionssicher",        # audit-proof
    "ordnungsmäßig",          # proper/compliant
    "prüfbar",                # auditable
]
```

### 2.3 Requirement Validator Agent

**Purpose:** Validate candidates against the existing knowledge graph.

**Why a separate agent?** Validation requires graph context:
- Check for duplicates/similar requirements
- Verify referenced entities exist
- Apply metamodel constraints
- Need iterative querying based on findings

```python
class RequirementValidatorState(TypedDict):
    """State for requirement validation agent."""
    # Input
    candidates: List[RequirementCandidate]

    # Processing state
    current_candidate_index: int
    validated_requirements: List[ValidatedRequirement]
    rejected_candidates: List[RejectedCandidate]

    # Graph context (cached)
    existing_requirements: List[Dict]  # Loaded once, queried locally
    existing_business_objects: List[Dict]
    existing_messages: List[Dict]

    # Validation settings
    duplicate_similarity_threshold: float  # 0.0-1.0
    require_entity_existence: bool         # Strict mode

    # Control
    iteration: int
    max_iterations: int
    is_complete: bool
```

**Validated Requirement Structure:**
```python
@dataclass
class ValidatedRequirement:
    candidate: RequirementCandidate   # Original candidate

    # Validation results
    validation_status: str            # accepted, needs_review, rejected
    validation_score: float           # 0.0-1.0 overall confidence

    # Duplicate detection
    similar_requirements: List[Dict]  # [{rid, name, similarity_score}]
    is_duplicate: bool
    duplicate_of: Optional[str]       # RID if exact duplicate

    # Entity resolution
    resolved_objects: List[Dict]      # [{boid, name, match_type}]
    resolved_messages: List[Dict]     # [{mid, name, match_type}]
    unresolved_entities: List[str]    # Couldn't find in graph

    # Suggested graph operations
    suggested_rid: str                # Generated RID for new requirement
    suggested_relationships: List[Dict]  # Proposed edges

    # Compliance pre-check
    metamodel_valid: bool
    compliance_warnings: List[str]

@dataclass
class RejectedCandidate:
    candidate: RequirementCandidate
    rejection_reason: str             # duplicate, low_confidence, invalid_structure
    rejection_details: str
```

**Tools:**
| Tool | Description |
|------|-------------|
| `find_similar_requirements` | Semantic search in existing requirements |
| `resolve_business_object` | Match mentioned entity to existing BO |
| `resolve_message` | Match mentioned entity to existing Message |
| `check_metamodel_compatibility` | Pre-validate against FINIUS constraints |
| `generate_requirement_id` | Create unique RID following conventions |
| `web_search` | Look up regulatory context (existing tool) |

**Validation Logic:**

```python
async def validate_candidate(
    candidate: RequirementCandidate,
    state: RequirementValidatorState
) -> ValidatedRequirement | RejectedCandidate:
    """
    Validation pipeline for a single candidate.
    """

    # 1. Duplicate Detection
    similar = await find_similar_requirements(
        candidate.text,
        state.existing_requirements,
        threshold=state.duplicate_similarity_threshold
    )

    if any(s['similarity_score'] > 0.95 for s in similar):
        return RejectedCandidate(
            candidate=candidate,
            rejection_reason="duplicate",
            rejection_details=f"Near-exact match: {similar[0]['rid']}"
        )

    # 2. Entity Resolution
    resolved_objects = []
    for obj_mention in candidate.mentioned_objects:
        match = await resolve_business_object(obj_mention, state.existing_business_objects)
        if match:
            resolved_objects.append(match)

    resolved_messages = []
    for msg_mention in candidate.mentioned_messages:
        match = await resolve_message(msg_mention, state.existing_messages)
        if match:
            resolved_messages.append(match)

    # 3. Metamodel Pre-check
    metamodel_check = await check_metamodel_compatibility(candidate)

    # 4. Compute overall validation score
    validation_score = compute_validation_score(
        extraction_confidence=candidate.confidence_score,
        entity_resolution_rate=len(resolved_objects) / max(len(candidate.mentioned_objects), 1),
        metamodel_valid=metamodel_check.valid,
        has_similar=len(similar) > 0
    )

    # 5. Determine status
    if validation_score >= 0.8:
        status = "accepted"
    elif validation_score >= 0.5:
        status = "needs_review"
    else:
        status = "rejected"

    return ValidatedRequirement(
        candidate=candidate,
        validation_status=status,
        validation_score=validation_score,
        similar_requirements=similar,
        is_duplicate=False,
        resolved_objects=resolved_objects,
        resolved_messages=resolved_messages,
        unresolved_entities=[...],
        suggested_rid=generate_rid(),
        suggested_relationships=build_relationships(resolved_objects, resolved_messages),
        metamodel_valid=metamodel_check.valid,
        compliance_warnings=metamodel_check.warnings
    )
```

### 2.4 Integration with Existing Graph Agent

The existing `RequirementGraphAgent` handles Stage 4. We extend it with:

**New Tool: `create_requirement_node`**
```python
@tool
def create_requirement_node(
    requirement: ValidatedRequirement,
    create_relationships: bool = True
) -> str:
    """
    Create a new Requirement node from a validated requirement.

    Uses MERGE to prevent duplicates, creates relationships
    to resolved BusinessObjects and Messages.
    """
    # Generate Cypher using metamodel templates
    cypher = build_merge_cypher(requirement)
    result = self.neo4j.execute_query(cypher)
    return format_creation_result(result)
```

**New Tool: `batch_integrate_requirements`**
```python
@tool
def batch_integrate_requirements(
    requirements: List[ValidatedRequirement],
    dry_run: bool = False
) -> str:
    """
    Integrate multiple validated requirements in a transaction.

    If dry_run=True, returns the Cypher without executing.
    """
    pass
```

---

## 3. Shared State Design

### 3.1 Pipeline State

The supervisor maintains overall pipeline state:

```python
class DocumentIngestionPipelineState(TypedDict):
    """
    Top-level state for the entire ingestion pipeline.
    Managed by the supervisor agent.
    """
    # ─── Input ───────────────────────────────────────────────────
    document_path: str
    document_type: str
    processing_options: ProcessingOptions

    # ─── Stage 1: Document Processing ────────────────────────────
    document_metadata: Optional[DocumentMetadata]
    chunks: List[DocumentChunk]
    document_processing_status: str
    document_processing_error: Optional[str]

    # ─── Stage 2: Requirement Extraction ─────────────────────────
    candidates: List[RequirementCandidate]
    extraction_status: str
    extraction_stats: ExtractionStats

    # ─── Stage 3: Validation ─────────────────────────────────────
    validated_requirements: List[ValidatedRequirement]
    rejected_candidates: List[RejectedCandidate]
    needs_review: List[ValidatedRequirement]
    validation_status: str

    # ─── Stage 4: Integration ────────────────────────────────────
    integration_results: List[IntegrationResult]
    integration_status: str

    # ─── Human-in-the-Loop ───────────────────────────────────────
    awaiting_human_review: bool
    human_decisions: List[HumanDecision]

    # ─── Overall Control ─────────────────────────────────────────
    current_stage: str  # document_processing, extraction, validation, integration
    pipeline_status: str  # pending, running, paused, completed, failed
    error_log: List[str]

    # ─── Audit Trail ─────────────────────────────────────────────
    started_at: datetime
    completed_at: Optional[datetime]
    stage_timings: Dict[str, float]

@dataclass
class ProcessingOptions:
    """User-configurable options for the pipeline."""
    extraction_mode: str = "balanced"        # strict, balanced, permissive
    min_confidence: float = 0.6
    auto_integrate: bool = False             # If False, pause for review
    include_low_confidence: bool = False     # Include needs_review in output
    parallel_extraction: bool = True         # Process chunks in parallel
    max_candidates: int = 1000               # Safety limit

@dataclass
class ExtractionStats:
    total_chunks: int
    processed_chunks: int
    candidates_found: int
    high_confidence: int
    medium_confidence: int
    low_confidence: int
    gobd_relevant: int
```

### 3.2 Message Flow

Agents communicate through state updates, not direct messaging:

```
Supervisor reads pipeline_state
    │
    ├── Determines current_stage
    │
    ├── Dispatches to appropriate agent
    │
    │   ┌─────────────────────────────────┐
    │   │ Agent updates its portion of    │
    │   │ state and returns               │
    │   └─────────────────────────────────┘
    │
    ├── Supervisor validates state transition
    │
    ├── Checks for human-in-the-loop triggers
    │
    └── Routes to next stage or pauses
```

---

## 4. Supervisor Orchestration

### 4.1 Supervisor Agent

```python
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
        llm_model: str,
        config: PipelineConfig
    ):
        self.neo4j = neo4j_connection
        self.config = config

        # Initialize sub-agents
        self.document_processor = DocumentProcessorAgent(...)
        self.extractor = RequirementExtractorAgent(...)
        self.validator = RequirementValidatorAgent(neo4j_connection, ...)
        self.graph_agent = RequirementGraphAgent(neo4j_connection, llm_model)

        # Build supervisor graph
        self.graph = self._build_supervisor_graph()

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
                "error": END,
            }
        )

        # Stage completion edges (all go back to router)
        for stage in ["document_processing", "extraction", "validation", "integration"]:
            workflow.add_edge(stage, "route")

        workflow.add_edge("human_review", "route")
        workflow.add_edge("finalize", END)

        return workflow.compile()
```

### 4.2 Routing Logic

```python
def _determine_next_stage(self, state: DocumentIngestionPipelineState) -> str:
    """
    Determine which stage to execute next based on current state.
    """
    # Check for errors
    if state.get("pipeline_status") == "failed":
        return "error"

    # Stage progression logic
    if state.get("document_processing_status") != "completed":
        return "document_processing"

    if state.get("extraction_status") != "completed":
        return "extraction"

    if state.get("validation_status") != "completed":
        return "validation"

    # Human-in-the-loop check
    needs_review = state.get("needs_review", [])
    if needs_review and not state.get("processing_options", {}).get("auto_integrate"):
        if not state.get("awaiting_human_review"):
            return "human_review"

    if state.get("integration_status") != "completed":
        return "integration"

    return "finalize"
```

### 4.3 Parallel Processing (Scatter-Gather)

For Stage 2 (Extraction), we can process chunks in parallel:

```python
async def _extraction_node_parallel(
    self,
    state: DocumentIngestionPipelineState
) -> DocumentIngestionPipelineState:
    """
    Process document chunks in parallel for faster extraction.
    Uses scatter-gather pattern.
    """
    chunks = state["chunks"]
    batch_size = 10  # Process 10 chunks concurrently

    all_candidates = []

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]

        # Scatter: Launch parallel extraction tasks
        tasks = [
            self.extractor.extract_from_chunk(chunk)
            for chunk in batch
        ]

        # Gather: Collect results
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in batch_results:
            if isinstance(result, Exception):
                state["error_log"].append(str(result))
            else:
                all_candidates.extend(result)

    state["candidates"] = all_candidates
    state["extraction_status"] = "completed"
    state["extraction_stats"] = compute_stats(all_candidates)

    return state
```

---

## 5. Document Processing Strategy

### 5.1 Supported Formats

| Format | Library | Notes |
|--------|---------|-------|
| PDF | `pdfplumber` or `PyMuPDF` | Layout-aware extraction |
| DOCX | `python-docx` | Preserves structure |
| TXT | Built-in | Simple but common |
| HTML | `BeautifulSoup` | For web-sourced docs |

### 5.2 Chunking Configuration

Based on research findings for legal documents:

```python
CHUNKING_PRESETS = {
    "legal": {
        "max_chunk_size": 1000,
        "overlap_size": 200,
        "respect_boundaries": True,
        "boundary_patterns": [
            r"^(?:Article|Section|§)\s+\d+",       # Section headers
            r"^\(\d+\)",                            # Numbered paragraphs
            r"^[a-z]\)",                            # Lettered clauses
        ],
        "preserve_hierarchy": True,
    },
    "technical": {
        "max_chunk_size": 800,
        "overlap_size": 150,
        "respect_boundaries": True,
        "boundary_patterns": [
            r"^#+\s+",                              # Markdown headers
            r"^\d+\.\d+",                           # Numbered sections
        ],
        "preserve_hierarchy": True,
    },
    "general": {
        "max_chunk_size": 500,
        "overlap_size": 100,
        "respect_boundaries": False,
        "boundary_patterns": [],
        "preserve_hierarchy": False,
    },
}
```

### 5.3 Metadata Extraction

```python
def extract_document_metadata(text: str, path: str) -> DocumentMetadata:
    """
    Extract metadata from document content and path.
    """
    # Detect document type from content patterns
    doc_type = detect_document_type(text)  # legal, technical, policy, etc.

    # Detect language
    language = detect_language(text)

    # Extract title (first heading or filename)
    title = extract_title(text) or Path(path).stem

    # For legal docs, try to extract jurisdiction
    jurisdiction = extract_jurisdiction(text) if doc_type == "legal" else None

    return DocumentMetadata(
        title=title,
        source=path,
        document_type=doc_type,
        detected_language=language,
        page_count=estimate_pages(text),
        extraction_timestamp=datetime.now(),
        jurisdiction=jurisdiction,
        version=extract_version(text),
    )
```

---

## 6. Tool Definitions

### 6.1 Document Processing Tools

```python
@tool
def extract_pdf_text(file_path: str, preserve_layout: bool = True) -> str:
    """
    Extract text from a PDF file with optional layout preservation.

    Args:
        file_path: Path to the PDF file
        preserve_layout: If True, attempt to preserve visual layout

    Returns:
        Extracted text content
    """
    pass

@tool
def detect_document_structure(text: str) -> Dict[str, Any]:
    """
    Analyze document text to detect structural elements.

    Returns:
        Dict with:
        - sections: List of section headers with positions
        - hierarchy: Nested structure of sections
        - lists: Detected bullet/numbered lists
        - tables: Detected table regions
    """
    pass

@tool
def chunk_document(
    text: str,
    structure: Dict[str, Any],
    strategy: str = "legal"
) -> List[Dict]:
    """
    Split document into chunks using specified strategy.

    Args:
        text: Full document text
        structure: Output from detect_document_structure
        strategy: One of "legal", "technical", "general"

    Returns:
        List of chunk dictionaries with text and metadata
    """
    pass
```

### 6.2 Extraction Tools

```python
@tool
def identify_requirements(
    text: str,
    mode: str = "balanced"
) -> List[Dict]:
    """
    Identify requirement-like statements in text.

    Args:
        text: Chunk text to analyze
        mode: "strict" (high precision), "balanced", "permissive" (high recall)

    Returns:
        List of potential requirements with positions and confidence
    """
    pass

@tool
def classify_requirement_type(text: str) -> Dict:
    """
    Classify a requirement statement by type.

    Returns:
        Dict with:
        - type: functional, non_functional, constraint, compliance
        - subtype: More specific classification
        - confidence: 0.0-1.0
    """
    pass

@tool
def assess_gobd_relevance(text: str) -> Dict:
    """
    Assess whether a requirement is relevant to GoBD compliance.

    Returns:
        Dict with:
        - is_relevant: bool
        - confidence: 0.0-1.0
        - indicators: List of matched GoBD keywords/patterns
        - category: Which GoBD principle it relates to
    """
    pass
```

### 6.3 Validation Tools

```python
@tool
def find_similar_requirements(
    text: str,
    threshold: float = 0.7
) -> List[Dict]:
    """
    Find existing requirements similar to the given text.

    Uses semantic similarity (embeddings) and keyword matching.

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
    pass

@tool
def resolve_entity_mention(
    mention: str,
    entity_type: str  # "business_object" or "message"
) -> Optional[Dict]:
    """
    Resolve an entity mention to an existing graph node.

    Uses fuzzy matching and synonym detection.

    Returns:
        Dict with matched entity details or None
    """
    pass

@tool
def generate_requirement_id() -> str:
    """
    Generate a unique requirement ID following project conventions.

    Queries Neo4j to find the next available RID.

    Returns:
        New RID like "R-0042"
    """
    pass
```

---

## 7. Implementation Roadmap

### Phase 1: Foundation (Core Infrastructure)

**Goal:** Basic document processing and extraction pipeline

| Task | Description | Dependencies |
|------|-------------|--------------|
| 1.1 | Create `src/agents/document_processor.py` | None |
| 1.2 | Implement PDF/DOCX text extraction | pdfplumber, python-docx |
| 1.3 | Implement legal-aware chunking | 1.1 |
| 1.4 | Create `src/agents/requirement_extractor.py` | 1.3 |
| 1.5 | Implement requirement pattern detection | 1.4 |
| 1.6 | Add extraction tools | 1.5 |
| 1.7 | Basic CLI for testing | 1.6 |

**Deliverable:** Can process a PDF and output requirement candidates

### Phase 2: Validation (Graph Integration)

**Goal:** Validate candidates against existing graph

| Task | Description | Dependencies |
|------|-------------|--------------|
| 2.1 | Create `src/agents/requirement_validator.py` | Phase 1 |
| 2.2 | Implement similarity search | sentence-transformers |
| 2.3 | Implement entity resolution | 2.1 |
| 2.4 | Add metamodel pre-validation | MetamodelValidator |
| 2.5 | Extend graph_agent with creation tools | 2.4 |
| 2.6 | Integration tests | 2.5 |

**Deliverable:** Can validate candidates and identify graph connections

### Phase 3: Orchestration (Supervisor)

**Goal:** Unified pipeline with supervisor agent

| Task | Description | Dependencies |
|------|-------------|--------------|
| 3.1 | Create `src/agents/supervisor.py` | Phase 2 |
| 3.2 | Implement state management | 3.1 |
| 3.3 | Add parallel processing for extraction | 3.2 |
| 3.4 | Implement human-in-the-loop hooks | 3.3 |
| 3.5 | Add progress streaming | 3.4 |
| 3.6 | Error recovery logic | 3.5 |

**Deliverable:** Full pipeline orchestration with streaming

### Phase 4: UI & Polish

**Goal:** Streamlit UI and production readiness

| Task | Description | Dependencies |
|------|-------------|--------------|
| 4.1 | Create `src/ui/document_ingestion.py` | Phase 3 |
| 4.2 | File upload handling | 4.1 |
| 4.3 | Progress visualization | 4.2 |
| 4.4 | Review interface for candidates | 4.3 |
| 4.5 | Batch approval workflow | 4.4 |
| 4.6 | Export/reporting | 4.5 |

**Deliverable:** Complete user-facing document ingestion feature

---

## 8. Integration Points

### 8.1 Configuration

Extend `config/llm_config.json`:

```json
{
  "agent": {
    "model": "gpt-4o",
    "temperature": 0.0,
    "max_iterations": 5,
    "reasoning_level": "high"
  },
  "document_ingestion": {
    "chunking_strategy": "legal",
    "max_chunk_size": 1000,
    "chunk_overlap": 200,
    "extraction_mode": "balanced",
    "min_confidence": 0.6,
    "parallel_workers": 4,
    "auto_integrate": false
  }
}
```

### 8.2 Prompts

New prompts in `config/prompts/`:

- `extraction_system.txt` - System prompt for requirement extractor
- `validation_system.txt` - System prompt for validator
- `supervisor_system.txt` - System prompt for supervisor

### 8.3 Dependencies

Add to `requirements.txt`:

```
pdfplumber>=0.10.0          # PDF extraction
python-docx>=1.1.0          # DOCX extraction
sentence-transformers>=2.2  # Semantic similarity
langdetect>=1.0.9           # Language detection
```

### 8.4 File Structure

```
src/
├── agents/
│   ├── graph_agent.py           # Existing
│   ├── document_processor.py    # NEW
│   ├── requirement_extractor.py # NEW
│   ├── requirement_validator.py # NEW
│   └── supervisor.py            # NEW
├── core/
│   ├── document_chunker.py      # NEW
│   ├── entity_resolver.py       # NEW
│   └── similarity_search.py     # NEW
└── ui/
    └── document_ingestion.py    # NEW
```

---

## 9. Error Handling & Recovery

### 9.1 Error Categories

| Category | Example | Recovery Strategy |
|----------|---------|-------------------|
| **Document Parsing** | Corrupted PDF | Skip document, log error |
| **Extraction Timeout** | Very long chunk | Reduce chunk size, retry |
| **LLM Rate Limit** | API quota exceeded | Exponential backoff |
| **Graph Connection** | Neo4j unavailable | Pause pipeline, retry |
| **Validation Conflict** | Circular reference | Flag for human review |

### 9.2 Checkpointing

Use LangGraph's built-in checkpointing:

```python
from langgraph.checkpoint.memory import MemorySaver

checkpointer = MemorySaver()
graph = workflow.compile(checkpointer=checkpointer)

# Resume from checkpoint after failure
config = {"configurable": {"thread_id": "doc-123"}}
result = graph.invoke(state, config)
```

### 9.3 Graceful Degradation

```python
def handle_extraction_failure(chunk: DocumentChunk, error: Exception) -> List:
    """
    Handle extraction failure for a single chunk.

    Strategy:
    1. Log error with context
    2. Mark chunk as failed
    3. Continue with remaining chunks
    4. Include failure summary in final report
    """
    logger.error(f"Extraction failed for chunk {chunk.chunk_id}: {error}")
    return []  # Return empty candidates, don't stop pipeline
```

---

## 10. Open Questions

### 10.1 Design Decisions Needed

| Question | Options | Recommendation |
|----------|---------|----------------|
| **Embedding model for similarity?** | OpenAI, sentence-transformers local, Neo4j GDS | sentence-transformers (no API cost, runs locally) |
| **Store candidates before validation?** | Neo4j staging area, SQLite, in-memory | In-memory for MVP, Neo4j staging for production |
| **How to handle very long documents?** | Streaming, pagination, background processing | Background processing with progress callbacks |
| **Duplicate threshold tuning?** | Fixed 0.95, configurable, learned | Configurable with sensible default |

### 10.2 Future Enhancements

- **Active learning**: Learn from human review decisions to improve extraction
- **Multi-document analysis**: Cross-reference requirements across document set
- **Version tracking**: Detect changes when re-processing updated documents
- **Batch processing**: Queue multiple documents for overnight processing
- **Export formats**: Generate requirements documentation (DOCX, PDF, CSV)

### 10.3 Performance Considerations

| Concern | Mitigation |
|---------|------------|
| Large PDFs (100+ pages) | Stream processing, pagination |
| Many candidates (1000+) | Batch validation, parallel processing |
| Slow similarity search | Pre-compute embeddings, use vector index |
| LLM latency | Parallel API calls, caching |

---

## References

- [LangGraph Multi-Agent Workflows](https://blog.langchain.com/langgraph-multi-agent-workflows/)
- [LangGraph Supervisor Pattern](https://github.com/langchain-ai/langgraph-supervisor-py)
- [Chunking Strategies for LLM Applications](https://www.pinecone.io/learn/chunking-strategies/)
- [Legal Chunking Research](https://www.researchgate.net/publication/386472016_Legal_Chunking_Evaluating_Methods_for_Effective_Legal_Text_Retrieval)
- [AWS Multi-Agent with LangGraph](https://aws.amazon.com/blogs/machine-learning/build-multi-agent-systems-with-langgraph-and-amazon-bedrock/)
