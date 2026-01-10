# Graph-RAG Implementation Roadmap

**Version:** 1.5
**Based on:** masterplan.md v1.1
**Created:** January 2026
**Updated:** January 2026
**Status:** Phase 5 Complete - Ready for Phase 6

---

## Overview

This document provides a structured implementation roadmap for the Graph-RAG Autonomous Agent System as described in `masterplan.md`. The roadmap is divided into six phases, each building upon the previous one.

### Phase Summary

| Phase | Name | Status | Description | Key Deliverables |
|-------|------|--------|-------------|------------------|
| 1 | Foundation & Infrastructure | **COMPLETE** | Database schemas, shared utilities, project restructure | PostgreSQL schema, directory structure, config |
| 2 | Creator Agent | **COMPLETE** | Document processing and requirement extraction | Creator Agent container, extraction pipeline |
| 3 | Validator Agent | **COMPLETE** | Graph validation and integration | Validator Agent container, fulfillment tracking |
| 4 | Orchestrator | **COMPLETE** | Job management and coordination | CLI interface, monitoring, completion detection |
| 5 | Containerization | **COMPLETE** | Docker deployment setup | Docker Compose, health endpoints, FastAPI |
| 6 | Testing & Hardening | Pending | End-to-end testing and refinement | Integration tests, error recovery validation |

### Dependencies Between Phases

```
Phase 1 (Foundation)
    │
    ├──▶ Phase 2 (Creator Agent)
    │         │
    │         └──▶ Phase 4 (Orchestrator)
    │                   │
    ├──▶ Phase 3 (Validator Agent)────┘
    │                   │
    └──────────────────▶│
                        ▼
              Phase 5 (Containerization)
                        │
                        ▼
              Phase 6 (Testing)
```

---

## Phase 1: Foundation & Infrastructure

**Goal:** Establish the foundational infrastructure, database schemas, and shared utilities required by both agents.

### 1.1 Project Directory Restructure

**Objective:** Reorganize the `src/` directory to match the masterplan architecture.

**Current Structure:**
```
src/
├── agents/
│   ├── document_ingestion_supervisor.py
│   ├── document_processor_agent.py
│   ├── graph_agent.py
│   ├── requirement_extractor_agent.py
│   └── requirement_validator_agent.py
├── core/
│   ├── config.py
│   ├── csv_processor.py
│   ├── document_models.py
│   ├── document_processor.py
│   ├── metamodel_validator.py
│   └── neo4j_utils.py
└── ui/
    └── ...
```

**Target Structure:**
```
src/
├── agents/
│   ├── creator/
│   │   ├── __init__.py
│   │   ├── creator_agent.py
│   │   ├── document_processor.py   # (migrated from core)
│   │   ├── candidate_extractor.py
│   │   ├── researcher.py
│   │   └── tools.py
│   ├── validator/
│   │   ├── __init__.py
│   │   ├── validator_agent.py
│   │   ├── relevance_analyzer.py
│   │   ├── fulfillment_checker.py
│   │   ├── graph_integrator.py
│   │   └── tools.py
│   └── shared/
│       ├── __init__.py
│       ├── context_manager.py
│       ├── checkpoint.py
│       ├── workspace.py
│       └── todo_manager.py
├── core/
│   ├── __init__.py
│   ├── config.py
│   ├── neo4j_utils.py
│   ├── postgres_utils.py         # (new)
│   └── metamodel_validator.py
├── orchestrator/
│   ├── __init__.py
│   ├── job_manager.py
│   ├── dispatcher.py
│   ├── monitor.py
│   └── reporter.py
└── ui/
    └── ...
```

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 1.1.1 | Create new directory structure | `src/agents/creator/`, `src/agents/validator/`, `src/agents/shared/`, `src/orchestrator/` |
| 1.1.2 | Create `__init__.py` files for all new packages | All new directories |
| 1.1.3 | Move document processing code to creator agent | `src/core/document_processor.py` → `src/agents/creator/document_processor.py` |
| 1.1.4 | Update imports throughout codebase | All files importing moved modules |

### 1.2 PostgreSQL Schema Implementation

**Objective:** Create the PostgreSQL database schema for job tracking, requirement caching, and LLM logging.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 1.2.1 | Create SQL migration file with all tables | `migrations/001_initial_schema.sql` |
| 1.2.2 | Implement `postgres_utils.py` with connection management | `src/core/postgres_utils.py` |
| 1.2.3 | Add PostgreSQL connection string to `.env.example` | `.env.example` |
| 1.2.4 | Create database initialization script | `scripts/init_db.py` |

**Schema Tables to Create:**
- `jobs` - Job tracking (as defined in masterplan Section 9.1)
- `requirement_cache` - Shared queue between agents
- `llm_requests` - LLM request logging
- `agent_checkpoints` - Recovery state persistence

**Note:** The Citation Engine tables (`sources`, `citations`) are created automatically by the Citation Engine package when initialized.

**Implementation for `postgres_utils.py`:**

```python
# src/core/postgres_utils.py

import os
from typing import Optional, Any
import asyncpg
from contextlib import asynccontextmanager

class PostgresConnection:
    """PostgreSQL connection manager for shared state."""

    def __init__(self, connection_string: Optional[str] = None):
        self.connection_string = connection_string or os.getenv("DATABASE_URL")
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Establish connection pool."""
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.connection_string,
                min_size=2,
                max_size=10
            )

    async def disconnect(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def acquire(self):
        """Acquire a connection from the pool."""
        async with self._pool.acquire() as conn:
            yield conn

    async def execute(self, query: str, *args) -> str:
        """Execute a query."""
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args) -> list:
        """Fetch multiple rows."""
        async with self.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args) -> Optional[asyncpg.Record]:
        """Fetch a single row."""
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)


# Factory function
def create_postgres_connection(connection_string: Optional[str] = None) -> PostgresConnection:
    """Create a PostgreSQL connection instance."""
    return PostgresConnection(connection_string)
```

### 1.3 Extended Neo4j Metamodel

**Objective:** Update the Neo4j schema and `MetamodelValidator` with new fulfillment relationship types.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 1.3.1 | Update `ALLOWED_RELATIONSHIPS` in MetamodelValidator | `src/core/metamodel_validator.py` |
| 1.3.2 | Add new fulfillment relationships to `metamodell.cql` | `data/metamodell.cql` |
| 1.3.3 | Add new quality gate queries (Q4, Q5) | `data/metamodell.cql` |
| 1.3.4 | Create migration script for existing graphs | `scripts/migrate_metamodel.py` |

**New Relationships to Add:**
- `FULFILLED_BY_OBJECT` (Requirement → BusinessObject)
- `NOT_FULFILLED_BY_OBJECT` (Requirement → BusinessObject)
- `FULFILLED_BY_MESSAGE` (Requirement → Message)
- `NOT_FULFILLED_BY_MESSAGE` (Requirement → Message)
- `SUPERSEDES` (Requirement → Requirement)

**Relationship Properties:**
```cypher
// FULFILLED_BY relationships include:
{
    confidence: FLOAT,        // 0.0-1.0
    evidence: STRING,         // How it's fulfilled
    citationId: STRING,       // Link to Citation Engine
    validatedAt: DATETIME,
    validatedByAgent: STRING  // 'validator' | 'manual'
}

// NOT_FULFILLED_BY relationships include:
{
    gapDescription: STRING,   // What's missing
    severity: STRING,         // 'critical' | 'major' | 'minor'
    remediation: STRING,      // Suggested fix
    citationId: STRING,
    validatedAt: DATETIME,
    validatedByAgent: STRING
}
```

### 1.4 Shared Agent Utilities

**Objective:** Implement shared utilities for context management, checkpointing, and workspace handling.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 1.4.1 | Implement `ContextManager` class | `src/agents/shared/context_manager.py` |
| 1.4.2 | Implement `CheckpointManager` class | `src/agents/shared/checkpoint.py` |
| 1.4.3 | Implement `Workspace` class | `src/agents/shared/workspace.py` |
| 1.4.4 | Implement `TodoManager` class | `src/agents/shared/todo_manager.py` |

**Context Manager Implementation:**

```python
# src/agents/shared/context_manager.py

from typing import List
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.messages.utils import trim_messages, count_tokens_approximately

class ContextManager:
    """Manages context window for long-running agents."""

    COMPACTION_THRESHOLD = 100_000  # tokens
    KEEP_RAW_TURNS = 3

    def __init__(self, llm, workspace):
        self.llm = llm
        self.workspace = workspace

    def create_pre_model_hook(self):
        """Create a pre-model hook for LangGraph agents."""
        def pre_model_hook(state):
            messages = state.get("messages", [])
            token_count = count_tokens_approximately(messages)

            if token_count > self.COMPACTION_THRESHOLD:
                trimmed = trim_messages(
                    messages,
                    strategy="last",
                    token_counter=count_tokens_approximately,
                    max_tokens=self.COMPACTION_THRESHOLD,
                    start_on="human",
                    end_on=("human", "tool"),
                )
                return {"llm_input_messages": trimmed}

            return {"llm_input_messages": messages}

        return pre_model_hook

    def should_compact(self, messages: List[BaseMessage]) -> bool:
        """Check if context needs compaction."""
        return count_tokens_approximately(messages) > self.COMPACTION_THRESHOLD
```

### 1.5 Citation Engine Integration

**Objective:** Add the Citation Engine as a dependency and configure it to share PostgreSQL with the agents.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 1.5.1 | Add Citation Engine to requirements.txt | `requirements.txt` |
| 1.5.2 | Add Citation Engine env vars to `.env.example` | `.env.example` |
| 1.5.3 | Create citation initialization helper | `src/core/citation_utils.py` |
| 1.5.4 | Verify Citation Engine tables are created | `scripts/verify_citations.py` |

**Requirements.txt Addition:**
```
# Citation Engine (local development)
-e ./citation_tool[full]

# Or from git:
# citation-engine[full] @ git+https://github.com/youruser/citation-tool.git@main
```

**Environment Variables:**
```bash
# Citation Engine configuration
CITATION_DB_URL=postgresql://graphrag:password@localhost:5432/graphrag
CITATION_LLM_URL=http://localhost:8080/v1
CITATION_LLM_MODEL=120b-instruct
CITATION_REASONING_REQUIRED=low
```

### 1.6 Configuration Updates

**Objective:** Extend `llm_config.json` with new configuration sections for both agents.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 1.6.1 | Add creator_agent section to config | `config/llm_config.json` |
| 1.6.2 | Add validator_agent section to config | `config/llm_config.json` |
| 1.6.3 | Add context_management section | `config/llm_config.json` |
| 1.6.4 | Update config.py to load new sections | `src/core/config.py` |

**Configuration Template:**
```json
{
  "agent": {
    "model": "120b-instruct",
    "temperature": 0.0,
    "max_iterations": 100,
    "reasoning_level": "high"
  },
  "creator_agent": {
    "research_depth": "thorough",
    "min_confidence_threshold": 0.6,
    "chunk_strategy": "legal",
    "max_chunk_size": 1000,
    "polling_interval_seconds": 30
  },
  "validator_agent": {
    "duplicate_threshold": 0.95,
    "auto_integrate": false,
    "require_citations": true,
    "polling_interval_seconds": 10
  },
  "context_management": {
    "compaction_threshold_tokens": 100000,
    "summarization_trigger_tokens": 128000,
    "keep_raw_turns": 3
  }
}
```

### Phase 1 Acceptance Criteria

- [x] New directory structure created and all imports work
- [x] PostgreSQL schema created and migration runs successfully
- [x] MetamodelValidator updated with new relationship types
- [x] Shared utilities implemented and unit tested
- [x] Citation Engine added as dependency and configured
- [x] Configuration files updated and validated
- [ ] All existing tests still pass (to be verified)

### Phase 1 Implementation Notes

**Completed: January 2026**

**Files Created:**
- `src/agents/creator/__init__.py` - Creator Agent package stub
- `src/agents/validator/__init__.py` - Validator Agent package stub
- `src/agents/shared/__init__.py` - Shared utilities exports
- `src/agents/shared/context_manager.py` - LLM context window management with pre_model_hook
- `src/agents/shared/checkpoint.py` - State persistence and recovery (PostgreSQL + file storage)
- `src/agents/shared/workspace.py` - Working data storage for agents
- `src/agents/shared/todo_manager.py` - Task tracking for autonomous agents
- `src/orchestrator/__init__.py` - Orchestrator package stub
- `src/core/postgres_utils.py` - Async PostgreSQL connection management with job/requirement/checkpoint functions
- `src/core/citation_utils.py` - Citation Engine integration helper
- `migrations/001_initial_schema.sql` - Full PostgreSQL schema (jobs, requirement_cache, llm_requests, agent_checkpoints, candidate_workspace)
- `scripts/init_db.py` - Database initialization script

**Files Modified:**
- `data/metamodell.cql` - Updated to v2.0 with fulfillment relationships and complianceStatus
- `src/core/metamodel_validator.py` - Added FULFILLED_BY_*, NOT_FULFILLED_BY_*, SUPERSEDES relationships; added C4, C5 checks
- `src/core/__init__.py` - Added exports for new utilities
- `config/llm_config.json` - Added creator_agent, validator_agent, context_management, orchestrator sections
- `.env.example` - Added PostgreSQL, Citation Engine, and agent configuration
- `requirements.txt` - Added asyncpg, langgraph-checkpoint-postgres, fastapi, uvicorn, httpx
- `CLAUDE.md` - Updated with new architecture documentation

**New Neo4j Relationships (v2.0):**
- `FULFILLED_BY_OBJECT` (Requirement → BusinessObject)
- `NOT_FULFILLED_BY_OBJECT` (Requirement → BusinessObject)
- `FULFILLED_BY_MESSAGE` (Requirement → Message)
- `NOT_FULFILLED_BY_MESSAGE` (Requirement → Message)
- `SUPERSEDES` (Requirement → Requirement)

**New MetamodelValidator Checks:**
- C4: `check_c4_unfulfilled_requirements` - Identifies requirements with 'open' compliance status
- C5: `check_c5_compliance_status_consistency` - Verifies complianceStatus matches fulfillment relationships

---

## Phase 2: Creator Agent Implementation

**Goal:** Build the Creator Agent capable of processing documents and extracting citation-backed requirements.

### 2.1 Core Creator Agent Structure

**Objective:** Create the main Creator Agent class using LangGraph with durable execution.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 2.1.1 | Create `CreatorAgent` class with LangGraph workflow | `src/agents/creator/creator_agent.py` |
| 2.1.2 | Define `CreatorAgentState` TypedDict | `src/agents/creator/creator_agent.py` |
| 2.1.3 | Implement agent event loop | `src/agents/creator/creator_agent.py` |
| 2.1.4 | Configure PostgresSaver checkpointer | `src/agents/creator/creator_agent.py` |

**State Definition:**
```python
from typing import TypedDict, List, Optional, Annotated
from langgraph.graph.message import add_messages

class CreatorAgentState(TypedDict):
    # Core state
    messages: Annotated[List[BaseMessage], add_messages]
    job_id: str
    job: dict

    # Processing state
    current_phase: str  # preprocessing, identification, research, formulation, output
    document_chunks: List[dict]
    candidates: List[dict]
    current_candidate_index: int

    # Output
    requirements_created: List[str]  # IDs of created requirements

    # Control
    error: Optional[dict]
    should_stop: bool
```

### 2.2 Document Processing Pipeline

**Objective:** Refactor existing document processing code for the Creator Agent.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 2.2.1 | Migrate `DocumentProcessor` to creator module | `src/agents/creator/document_processor.py` |
| 2.2.2 | Add support for legal document chunking strategy | `src/agents/creator/document_processor.py` |
| 2.2.3 | Implement chunk metadata extraction (section hierarchy) | `src/agents/creator/document_processor.py` |
| 2.2.4 | Create workspace storage for chunks | `src/agents/creator/document_processor.py` |

**Code Reuse:**
- Start with existing `src/core/document_processor.py`
- Extend with section hierarchy detection
- Add workspace persistence

### 2.3 Candidate Extractor

**Objective:** Implement the candidate identification phase.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 2.3.1 | Create `CandidateExtractor` class | `src/agents/creator/candidate_extractor.py` |
| 2.3.2 | Implement pattern matching for requirement detection | `src/agents/creator/candidate_extractor.py` |
| 2.3.3 | Implement candidate grouping logic | `src/agents/creator/candidate_extractor.py` |
| 2.3.4 | Create candidate workspace storage | `src/agents/creator/candidate_extractor.py` |

**Code Reuse:**
- Incorporate patterns from `src/agents/requirement_extractor_agent.py`
- Extend with grouping and deduplication

### 2.4 Researcher Component

**Objective:** Implement the research and enrichment phase.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 2.4.1 | Create `Researcher` class | `src/agents/creator/researcher.py` |
| 2.4.2 | Implement web search integration (Tavily) | `src/agents/creator/researcher.py` |
| 2.4.3 | Implement graph search for similar requirements | `src/agents/creator/researcher.py` |
| 2.4.4 | Implement citation creation for research findings | `src/agents/creator/researcher.py` |

**Research Depth Logic:**
```python
def determine_research_depth(self, candidate: dict) -> str:
    """Determine how deep to research based on candidate complexity."""
    if candidate.get("confidence", 0) > 0.8:
        return "quick"  # Clear requirement, minimal research needed
    elif candidate.get("complexity", "medium") == "high":
        return "deep"  # Complex topic, thorough research
    else:
        return "standard"  # Normal research depth
```

### 2.5 Creator Agent Tools

**Objective:** Define the tool set available to the Creator Agent.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 2.5.1 | Create `extract_document_text` tool | `src/agents/creator/tools.py` |
| 2.5.2 | Create `chunk_document` tool | `src/agents/creator/tools.py` |
| 2.5.3 | Create `web_search` tool | `src/agents/creator/tools.py` |
| 2.5.4 | Create `cite_document` tool | `src/agents/creator/tools.py` |
| 2.5.5 | Create `cite_web` tool | `src/agents/creator/tools.py` |
| 2.5.6 | Create `query_similar_requirements` tool | `src/agents/creator/tools.py` |
| 2.5.7 | Create `write_requirement_cache` tool | `src/agents/creator/tools.py` |
| 2.5.8 | Create workspace read/write tools | `src/agents/creator/tools.py` |

### 2.6 Requirement Cache Integration

**Objective:** Implement writing requirements to the PostgreSQL cache.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 2.6.1 | Create `RequirementCacheWriter` class | `src/agents/creator/cache_writer.py` |
| 2.6.2 | Implement requirement validation before write | `src/agents/creator/cache_writer.py` |
| 2.6.3 | Implement duplicate detection against existing cache | `src/agents/creator/cache_writer.py` |

### 2.7 Creator Agent Prompts

**Objective:** Create system prompts for each Creator Agent phase.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 2.7.1 | Create preprocessing phase prompt | `config/prompts/creator_preprocessing.txt` |
| 2.7.2 | Create identification phase prompt | `config/prompts/creator_identification.txt` |
| 2.7.3 | Create research phase prompt | `config/prompts/creator_research.txt` |
| 2.7.4 | Create formulation phase prompt | `config/prompts/creator_formulation.txt` |

### Phase 2 Acceptance Criteria

- [x] Creator Agent can start and poll for jobs
- [x] Document processing extracts text and creates chunks
- [x] Candidate extraction identifies potential requirements
- [x] Research phase queries web and graph for context
- [x] Requirements are written to PostgreSQL cache with status 'pending'
- [x] Citations are created for all claims
- [x] Agent checkpoints state and can resume from failure
- [ ] Unit tests cover all major components (to be verified)

### Phase 2 Implementation Notes

**Completed: January 2026**

**Files Created:**
- `src/agents/creator/creator_agent.py` - Main CreatorAgent class with LangGraph workflow, CreatorAgentState TypedDict, durable execution with PostgresSaver checkpointing
- `src/agents/creator/document_processor.py` - CreatorDocumentProcessor with enhanced legal document chunking, section hierarchy detection, cross-reference extraction
- `src/agents/creator/candidate_extractor.py` - CandidateExtractor with modal verb patterns, GoBD/GDPR indicator detection, entity extraction
- `src/agents/creator/researcher.py` - Researcher component with Tavily web search integration, Neo4j similar requirement queries, research depth determination
- `src/agents/creator/tools.py` - CreatorAgentTools providing 13 tools for document processing, research, citation, and cache operations
- `src/agents/creator/cache_writer.py` - RequirementCacheWriter with validation, duplicate detection, batch writing
- `src/agents/creator/__init__.py` - Package exports for all Creator Agent components

**Prompts Created:**
- `config/prompts/creator_preprocessing.txt` - Preprocessing phase prompt (document extraction, chunking)
- `config/prompts/creator_identification.txt` - Identification phase prompt (candidate extraction)
- `config/prompts/creator_research.txt` - Research phase prompt (web search, graph queries)
- `config/prompts/creator_formulation.txt` - Formulation phase prompt (requirement writing)

**Files Modified:**
- `src/agents/__init__.py` - Added Creator Agent exports to main agents module

**Key Features Implemented:**
- **LangGraph Workflow**: 5-node state machine (initialize → process → tools → check → finalize)
- **Context Management**: pre_model_hook integration for 100K token compaction threshold
- **Document Processing**: PDF, DOCX, TXT, HTML support with legal/technical/general chunking strategies
- **GoBD Detection**: Pattern-based detection for 20+ GoBD indicators across 6 categories (retention, traceability, immutability, documentation, accessibility, completeness)
- **Research Depth**: Automatic depth determination (quick/standard/deep) based on candidate attributes
- **Workspace Integration**: Working data persistence via PostgreSQL candidate_workspace table
- **Tool Set**: 13 tools including extract_document_text, chunk_document, identify_requirement_candidates, assess_gobd_relevance, web_search, query_similar_requirements, cite_document, cite_web, write_requirement_to_cache

**Architecture Notes:**
- Creator Agent designed for long-running operations (days/weeks)
- Polling pattern: Agent polls `jobs` table for pending work
- Output goes to `requirement_cache` table with status='pending'
- Validator Agent (Phase 3) will poll `requirement_cache` for pending requirements

---

## Phase 3: Validator Agent Implementation

**Goal:** Build the Validator Agent capable of validating requirements and integrating them into Neo4j.

### 3.1 Core Validator Agent Structure

**Objective:** Create the main Validator Agent class using LangGraph with durable execution.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 3.1.1 | Create `ValidatorAgent` class with LangGraph workflow | `src/agents/validator/validator_agent.py` |
| 3.1.2 | Define `ValidatorAgentState` TypedDict | `src/agents/validator/validator_agent.py` |
| 3.1.3 | Implement requirement polling with `SKIP LOCKED` | `src/agents/validator/validator_agent.py` |
| 3.1.4 | Configure PostgresSaver checkpointer | `src/agents/validator/validator_agent.py` |

**State Definition:**
```python
class ValidatorAgentState(TypedDict):
    # Core state
    messages: Annotated[List[BaseMessage], add_messages]
    job_id: str
    requirement_id: str
    requirement: dict

    # Processing state
    current_phase: str  # understanding, relevance, fulfillment, planning, integration, documentation
    related_objects: List[dict]
    related_messages: List[dict]
    fulfillment_analysis: dict
    planned_operations: List[dict]

    # Output
    validation_result: dict
    graph_changes: List[dict]

    # Control
    error: Optional[dict]
```

### 3.2 Relevance Analyzer

**Objective:** Implement the relevance assessment phase.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 3.2.1 | Create `RelevanceAnalyzer` class | `src/agents/validator/relevance_analyzer.py` |
| 3.2.2 | Implement domain relevance checking | `src/agents/validator/relevance_analyzer.py` |
| 3.2.3 | Implement business object discovery queries | `src/agents/validator/relevance_analyzer.py` |
| 3.2.4 | Implement relevance decision tree | `src/agents/validator/relevance_analyzer.py` |

**Code Reuse:**
- Adapt logic from `src/agents/requirement_validator_agent.py`
- Extend with explicit decision tree

### 3.3 Fulfillment Checker

**Objective:** Implement the fulfillment analysis phase.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 3.3.1 | Create `FulfillmentChecker` class | `src/agents/validator/fulfillment_checker.py` |
| 3.3.2 | Implement per-object fulfillment analysis | `src/agents/validator/fulfillment_checker.py` |
| 3.3.3 | Implement gap detection logic | `src/agents/validator/fulfillment_checker.py` |
| 3.3.4 | Implement citation creation for fulfillment evidence | `src/agents/validator/fulfillment_checker.py` |

### 3.4 Graph Integrator

**Objective:** Implement graph mutation and integration.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 3.4.1 | Create `GraphIntegrator` class | `src/agents/validator/graph_integrator.py` |
| 3.4.2 | Implement requirement node creation | `src/agents/validator/graph_integrator.py` |
| 3.4.3 | Implement fulfillment relationship creation | `src/agents/validator/graph_integrator.py` |
| 3.4.4 | Implement transaction safety (rollback on failure) | `src/agents/validator/graph_integrator.py` |
| 3.4.5 | Implement metamodel validation before commit | `src/agents/validator/graph_integrator.py` |

**Code Reuse:**
- Start with existing `src/agents/graph_agent.py`
- Add transaction wrapping
- Integrate metamodel validation

### 3.5 Validator Agent Tools

**Objective:** Define the tool set available to the Validator Agent.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 3.5.1 | Create `execute_cypher_query` tool | `src/agents/validator/tools.py` |
| 3.5.2 | Create `get_database_schema` tool | `src/agents/validator/tools.py` |
| 3.5.3 | Create `find_similar_requirements` tool | `src/agents/validator/tools.py` |
| 3.5.4 | Create `validate_schema_compliance` tool | `src/agents/validator/tools.py` |
| 3.5.5 | Create `create_requirement_node` tool | `src/agents/validator/tools.py` |
| 3.5.6 | Create `create_relationship` tool | `src/agents/validator/tools.py` |
| 3.5.7 | Create `cite_database` tool | `src/agents/validator/tools.py` |
| 3.5.8 | Create requirement cache read/update tools | `src/agents/validator/tools.py` |

### 3.6 Requirement Cache Integration

**Objective:** Implement reading from and updating the PostgreSQL cache.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 3.6.1 | Create `RequirementCacheReader` class | `src/agents/validator/cache_reader.py` |
| 3.6.2 | Implement `SKIP LOCKED` polling | `src/agents/validator/cache_reader.py` |
| 3.6.3 | Implement status update methods | `src/agents/validator/cache_reader.py` |
| 3.6.4 | Implement retry count tracking | `src/agents/validator/cache_reader.py` |

### 3.7 Validator Agent Prompts

**Objective:** Create system prompts for each Validator Agent phase.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 3.7.1 | Create understanding phase prompt | `config/prompts/validator_understanding.txt` |
| 3.7.2 | Create relevance phase prompt | `config/prompts/validator_relevance.txt` |
| 3.7.3 | Create fulfillment phase prompt | `config/prompts/validator_fulfillment.txt` |
| 3.7.4 | Create integration phase prompt | `config/prompts/validator_integration.txt` |

### Phase 3 Acceptance Criteria

- [x] Validator Agent can start and poll for pending requirements
- [x] Relevance analysis determines requirement applicability
- [x] Fulfillment checker identifies gaps and fulfilled relationships
- [x] Graph integrator creates nodes and relationships correctly
- [x] All graph changes pass metamodel validation
- [x] Failed validations result in rollback
- [x] Agent updates requirement cache status appropriately
- [x] Citations created for validation decisions
- [ ] Unit tests cover all major components (to be verified)

### Phase 3 Implementation Notes

**Completed: January 2026**

**Files Created:**
- `src/agents/validator/validator_agent.py` - Main ValidatorAgent class with LangGraph workflow, ValidatorAgentState TypedDict, SKIP LOCKED polling, durable execution support
- `src/agents/validator/relevance_analyzer.py` - RelevanceAnalyzer with domain keyword matching, entity resolution, decision tree logic
- `src/agents/validator/fulfillment_checker.py` - FulfillmentChecker with per-entity analysis, gap detection, GoBD-specific fulfillment criteria
- `src/agents/validator/graph_integrator.py` - GraphIntegrator with transaction safety, metamodel validation before commit, rollback on failure
- `src/agents/validator/tools.py` - ValidatorAgentTools providing 12 tools for graph queries, duplicate detection, entity resolution, and integration
- `src/agents/validator/cache_reader.py` - RequirementCacheReader with SKIP LOCKED polling, status updates, retry tracking
- `src/agents/validator/__init__.py` - Package exports for all Validator Agent components

**Prompts Created:**
- `config/prompts/validator_understanding.txt` - Understanding phase prompt (requirement parsing)
- `config/prompts/validator_relevance.txt` - Relevance phase prompt (domain matching, duplicates)
- `config/prompts/validator_fulfillment.txt` - Fulfillment phase prompt (gap detection)
- `config/prompts/validator_integration.txt` - Integration phase prompt (graph mutations)

**Files Modified:**
- `src/agents/__init__.py` - Added Validator Agent exports to main agents module

**Key Features Implemented:**
- **LangGraph Workflow**: 5-node state machine (initialize → process → tools → check → finalize)
- **Relevance Decision Tree**: Domain keyword matching (core, compliance, technical categories)
- **Fulfillment Analysis**: Per-entity status (fulfilled, partial, not_fulfilled), gap severity levels
- **Graph Integration**: Transaction-safe node/relationship creation with metamodel validation
- **Relationship Types**: FULFILLED_BY_OBJECT, FULFILLED_BY_MESSAGE, NOT_FULFILLED_BY_OBJECT, NOT_FULFILLED_BY_MESSAGE
- **Cache Integration**: SKIP LOCKED polling, status transitions (pending → validating → integrated/rejected/failed)
- **Retry Handling**: Configurable max retries, stale validating release

**Architecture Notes:**
- Validator Agent refactored from existing `RequirementValidatorAgent` and `RequirementGraphAgent`
- Reuses existing Neo4j tools (execute_cypher_query, get_database_schema, etc.)
- Adds new tools for graph mutations (create_requirement_node, create_fulfillment_relationship)
- Polling pattern matches Creator Agent: polls `requirement_cache` table for pending work

---

## Phase 4: Orchestrator & Integration

**Goal:** Build the Orchestrator to manage jobs and coordinate the agents.

### 4.1 Job Manager

**Objective:** Implement job lifecycle management.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 4.1.1 | Create `JobManager` class | `src/orchestrator/job_manager.py` |
| 4.1.2 | Implement job creation with document storage | `src/orchestrator/job_manager.py` |
| 4.1.3 | Implement job status queries | `src/orchestrator/job_manager.py` |
| 4.1.4 | Implement job cancellation | `src/orchestrator/job_manager.py` |

### 4.2 Monitor

**Objective:** Implement health monitoring and stuck state detection.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 4.2.1 | Create `Monitor` class | `src/orchestrator/monitor.py` |
| 4.2.2 | Implement completion detection logic | `src/orchestrator/monitor.py` |
| 4.2.3 | Implement stuck state detection | `src/orchestrator/monitor.py` |
| 4.2.4 | Implement agent health checking | `src/orchestrator/monitor.py` |

**Completion Detection:**
```python
def check_job_completion(self, job_id: str) -> bool:
    """
    A job is complete when:
    1. Creator has finished (creator_status == 'completed')
    2. All requirements in cache have been processed
    """
    job = self.get_job(job_id)

    if job.creator_status != 'completed':
        return False

    pending_count = self.count_requirements(
        job_id=job_id,
        status=['pending', 'validating']
    )

    return pending_count == 0
```

### 4.3 Reporter

**Objective:** Implement job result aggregation and reporting.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 4.3.1 | Create `Reporter` class | `src/orchestrator/reporter.py` |
| 4.3.2 | Implement job summary generation | `src/orchestrator/reporter.py` |
| 4.3.3 | Implement requirement statistics | `src/orchestrator/reporter.py` |
| 4.3.4 | Implement citation summary | `src/orchestrator/reporter.py` |

### 4.4 CLI Interface

**Objective:** Create command-line interface for the Orchestrator.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 4.4.1 | Create `start_orchestrator.py` CLI | `start_orchestrator.py` |
| 4.4.2 | Create `job_status.py` CLI | `job_status.py` |
| 4.4.3 | Create `list_jobs.py` CLI | `list_jobs.py` |
| 4.4.4 | Create `cancel_job.py` CLI | `cancel_job.py` |

**CLI Usage:**
```bash
# Start a new job
python start_orchestrator.py \
  --prompt "Our US company is expanding to Europe. Review GDPR requirements." \
  --document-path ./data/gdpr.pdf \
  --context '{"domain": "car_rental", "region": "EU"}'

# Check job status
python job_status.py --job-id <uuid>

# List all jobs
python list_jobs.py

# Cancel a job
python cancel_job.py --job-id <uuid>
```

### 4.5 Integration Testing

**Objective:** Test the end-to-end flow with both agents.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 4.5.1 | Create integration test with sample document | `tests/integration/test_full_pipeline.py` |
| 4.5.2 | Create mock LLM for deterministic testing | `tests/mocks/mock_llm.py` |
| 4.5.3 | Verify requirement flows from Creator to Validator | `tests/integration/test_full_pipeline.py` |

### Phase 4 Acceptance Criteria

- [x] Jobs can be created via CLI
- [x] Job status can be queried
- [x] Completion detection works correctly
- [x] Stuck state detection triggers appropriate actions
- [x] Job reports summarize results correctly
- [ ] End-to-end flow works with sample document (to be verified with integration tests)

### Phase 4 Implementation Notes

**Completed: January 2026**

**Files Created:**
- `src/orchestrator/job_manager.py` - JobManager class for job lifecycle management (creation, status, cancellation, document storage)
- `src/orchestrator/monitor.py` - Monitor class with JobCompletionStatus enum, HealthStatus dataclass, StuckJobInfo dataclass, completion detection, stuck job detection, agent health checking
- `src/orchestrator/reporter.py` - Reporter class with JobSummary, RequirementStatistics, LLMStatistics dataclasses, text/JSON report generation, citation summaries
- `start_orchestrator.py` - CLI to create new jobs with document upload, optional wait for completion
- `job_status.py` - CLI to check job status, progress, rejected/failed requirements, full reports
- `list_jobs.py` - CLI to list jobs with filtering, daily statistics
- `cancel_job.py` - CLI to cancel jobs with optional workspace cleanup

**Files Modified:**
- `src/orchestrator/__init__.py` - Added exports for all orchestrator components

**Key Features Implemented:**
- **JobManager**: Job creation with document storage, status queries (using job_summary view), cancellation, workspace cleanup
- **Monitor**:
  - Completion detection (creator completed + no pending requirements)
  - Stuck job detection (configurable threshold, component identification)
  - Agent health checking via HTTP endpoints
  - Progress tracking with ETA calculation
  - wait_for_completion with async callback support
  - Stuck requirement reset functionality
- **Reporter**:
  - RequirementStatistics: counts by status, priority, GoBD/GDPR relevance
  - LLMStatistics: token usage, request counts, per-agent breakdown
  - Citation summary aggregation
  - Text report generation (human-readable)
  - JSON report generation (API-friendly)
  - Daily statistics for dashboards
- **CLI Interfaces**:
  - All CLIs support JSON output mode
  - start_orchestrator: --wait flag for synchronous completion
  - job_status: --report, --progress, --rejected, --failed views
  - list_jobs: --status filter, --stats for daily statistics
  - cancel_job: --cleanup for workspace removal, --force to skip confirmation

**Configuration Used (from llm_config.json orchestrator section):**
- `job_timeout_hours`: 168 (7 days)
- `stuck_detection_minutes`: 60
- `max_requirement_retries`: 5
- `completion_check_interval_seconds`: 30

**Architecture Notes:**
- All orchestrator components use async/await for PostgreSQL operations
- Monitor uses job_summary view for efficient status aggregation
- Reporter reuses count_requirements_by_status from postgres_utils
- CLI scripts use argparse with rich help text and examples
- ANSI colors for status display in list_jobs

---

## Phase 5: Containerization & Deployment

**Goal:** Package the system for Docker/Kubernetes deployment.

### 5.1 FastAPI Applications

**Objective:** Create FastAPI apps for each agent with health endpoints.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 5.1.1 | Create Creator Agent FastAPI app | `src/agents/creator/app.py` |
| 5.1.2 | Create Validator Agent FastAPI app | `src/agents/validator/app.py` |
| 5.1.3 | Create Orchestrator FastAPI app | `src/orchestrator/app.py` |
| 5.1.4 | Implement `/health`, `/ready`, `/status` endpoints | All apps |

**Health Endpoint Implementation:**
```python
from fastapi import FastAPI
from datetime import datetime

app = FastAPI(title="Creator Agent")
start_time = datetime.utcnow()

@app.get("/health")
async def health():
    uptime = (datetime.utcnow() - start_time).total_seconds()
    return {
        "status": "healthy",
        "agent": "creator",
        "uptime_seconds": uptime
    }

@app.get("/ready")
async def ready():
    db_connected = await check_db_connection()
    return {
        "ready": db_connected,
        "database": "connected" if db_connected else "disconnected"
    }

@app.get("/status")
async def status():
    return {
        "agent": "creator",
        "current_job_id": current_job_id,
        "state": current_state,
        "last_activity": last_activity.isoformat(),
        "metrics": {
            "jobs_processed": jobs_processed_count,
            "requirements_created": requirements_created_count,
            "uptime_hours": uptime_hours
        }
    }

@app.post("/shutdown")
async def shutdown():
    global shutdown_requested
    shutdown_requested = True
    return {"status": "shutdown_initiated"}
```

### 5.2 Dockerfiles

**Objective:** Create Dockerfiles for each component.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 5.2.1 | Create Creator Agent Dockerfile | `docker/Dockerfile.creator` |
| 5.2.2 | Create Validator Agent Dockerfile | `docker/Dockerfile.validator` |
| 5.2.3 | Create Orchestrator Dockerfile | `docker/Dockerfile.orchestrator` |
| 5.2.4 | Create base Python image with shared dependencies | `docker/Dockerfile.base` |

**Example Dockerfile:**

```dockerfile
# docker/Dockerfile.creator

FROM python:3.11-slim AS base

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY ../requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY ../src ./src/
COPY ../config ./config/
COPY ../citation_tool ./citation_tool/

# Install citation tool
RUN pip install -e ./citation_tool[full]

# Create workspace directory
RUN mkdir -p /app/workspace

# Set environment
ENV PYTHONPATH=/app

# Expose health check port
EXPOSE 8000

# Run the agent
CMD ["uvicorn", "src.agents.creator.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 5.3 Docker Compose

**Objective:** Create Docker Compose configuration for local development.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 5.3.1 | Create main docker-compose.yml | `docker-compose.yml` |
| 5.3.2 | Create docker-compose.dev.yml for development overrides | `docker-compose.dev.yml` |
| 5.3.3 | Create PostgreSQL init script | `docker/init.sql` |
| 5.3.4 | Create .env.docker template | `.env.docker.example` |

### 5.4 Kubernetes Manifests (Optional)

**Objective:** Create Kubernetes manifests for production deployment.

**Note:** As per the user's guidance, full Kubernetes deployment is handled by DevOps. These are reference manifests only.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 5.4.1 | Create Creator Agent deployment manifest | `k8s/creator-deployment.yaml` |
| 5.4.2 | Create Validator Agent deployment manifest | `k8s/validator-deployment.yaml` |
| 5.4.3 | Create ConfigMap for configuration | `k8s/configmap.yaml` |
| 5.4.4 | Create Secret template | `k8s/secrets.yaml.example` |

### Phase 5 Acceptance Criteria

- [x] All components have FastAPI apps with health endpoints
- [x] Docker images build successfully
- [x] Docker Compose brings up full system
- [x] Health endpoints respond correctly
- [x] Containers can communicate via shared network
- [x] Workspace volumes persist correctly

### Phase 5 Implementation Notes

**Completed: January 2026**

**Files Created:**
- `src/agents/creator/app.py` - Creator Agent FastAPI app with /health, /ready, /status, /shutdown, /metrics endpoints
- `src/agents/validator/app.py` - Validator Agent FastAPI app with /health, /ready, /status, /shutdown, /metrics endpoints
- `src/orchestrator/app.py` - Orchestrator FastAPI app with full REST API for job management
- `docker/Dockerfile.base` - Base image with shared dependencies
- `docker/Dockerfile.creator` - Creator Agent container (multi-stage build)
- `docker/Dockerfile.validator` - Validator Agent container (multi-stage build)
- `docker/Dockerfile.orchestrator` - Orchestrator container (multi-stage build)
- `docker/init.sql` - PostgreSQL initialization script for Docker
- `docker-compose.yml` - Production Docker Compose configuration
- `docker-compose.dev.yml` - Development overrides with hot reload and Adminer
- `.env.docker.example` - Docker environment template

**FastAPI Apps Features:**
- **Health Endpoints**: `/health` (liveness), `/ready` (readiness with DB check), `/status` (detailed state)
- **Graceful Shutdown**: `/shutdown` endpoint for clean container stops
- **Prometheus Metrics**: `/metrics` endpoint for monitoring
- **Agent-Specific**: Creator tracks jobs/requirements created; Validator tracks validated/integrated/rejected
- **Orchestrator API**: Full REST API for job creation, listing, status, reports, cancellation

**Docker Configuration:**
- Multi-stage builds for smaller images
- Non-root user for security
- Health checks with configurable intervals
- Shared workspace volume for document storage
- PostgreSQL and Neo4j with persistent volumes
- Service dependencies with health check conditions
- Development mode with source code mounting and hot reload

**Ports:**
- Orchestrator: 8000
- Creator Agent: 8001
- Validator Agent: 8002
- PostgreSQL: 5432
- Neo4j Bolt: 7687
- Neo4j Browser: 7474
- Adminer (dev only): 8080

---

## Phase 6: Testing & Hardening

**Goal:** Comprehensive testing and production hardening.

### 6.1 Unit Tests

**Objective:** Ensure comprehensive unit test coverage.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 6.1.1 | Test Creator Agent components | `tests/agents/creator/` |
| 6.1.2 | Test Validator Agent components | `tests/agents/validator/` |
| 6.1.3 | Test shared utilities | `tests/agents/shared/` |
| 6.1.4 | Test orchestrator components | `tests/orchestrator/` |
| 6.1.5 | Test PostgreSQL utilities | `tests/core/test_postgres_utils.py` |

### 6.2 Integration Tests

**Objective:** Test component interactions.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 6.2.1 | Test Creator Agent → PostgreSQL flow | `tests/integration/test_creator_postgres.py` |
| 6.2.2 | Test Validator Agent → Neo4j flow | `tests/integration/test_validator_neo4j.py` |
| 6.2.3 | Test full pipeline with mock LLM | `tests/integration/test_full_pipeline.py` |
| 6.2.4 | Test checkpoint/recovery flow | `tests/integration/test_recovery.py` |

### 6.3 Error Recovery Testing

**Objective:** Validate error handling and recovery mechanisms.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 6.3.1 | Test LLM timeout recovery | `tests/integration/test_error_recovery.py` |
| 6.3.2 | Test database connection failure recovery | `tests/integration/test_error_recovery.py` |
| 6.3.3 | Test poison pill handling | `tests/integration/test_error_recovery.py` |
| 6.3.4 | Test graceful shutdown | `tests/integration/test_shutdown.py` |

### 6.4 Performance Testing

**Objective:** Validate system behavior under load.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 6.4.1 | Test with large document (100+ pages) | `tests/performance/test_large_document.py` |
| 6.4.2 | Test context management under high token usage | `tests/performance/test_context_management.py` |
| 6.4.3 | Test concurrent requirement processing | `tests/performance/test_concurrent.py` |

### 6.5 Documentation

**Objective:** Update documentation to reflect implementation.

**Tasks:**

| Task ID | Description | Files Affected |
|---------|-------------|----------------|
| 6.5.1 | Update CLAUDE.md with new architecture | `CLAUDE.md` |
| 6.5.2 | Update README.md with deployment instructions | `README.md` |
| 6.5.3 | Create DEPLOYMENT.md guide | `DEPLOYMENT.md` |
| 6.5.4 | Document API endpoints | `docs/api.md` |

### Phase 6 Acceptance Criteria

- [ ] Unit test coverage > 80%
- [ ] All integration tests pass
- [ ] Error recovery mechanisms work correctly
- [ ] System handles large documents without memory issues
- [ ] Documentation is complete and accurate

---

## Appendix A: Existing Code Mapping

This appendix maps existing code to target locations for Phase 2 and 3.

### Creator Agent Code Reuse

| Existing File | Reuse Strategy | Target Location |
|---------------|----------------|-----------------|
| `src/core/document_processor.py` | Copy and extend | `src/agents/creator/document_processor.py` |
| `src/agents/requirement_extractor_agent.py` | Extract patterns, refactor | `src/agents/creator/candidate_extractor.py` |
| `src/agents/document_processor_agent.py` | Extract chunking logic | `src/agents/creator/document_processor.py` |

### Validator Agent Code Reuse

| Existing File | Reuse Strategy | Target Location |
|---------------|----------------|-----------------|
| `src/agents/graph_agent.py` | Major refactor | `src/agents/validator/validator_agent.py` |
| `src/agents/requirement_validator_agent.py` | Extract validation logic | `src/agents/validator/relevance_analyzer.py` |
| `src/core/metamodel_validator.py` | Use as-is with extensions | Shared via `src/core/` |
| `src/core/neo4j_utils.py` | Use as-is | Shared via `src/core/` |

### Shared Code

| Existing File | Status | Notes |
|---------------|--------|-------|
| `src/core/config.py` | Keep and extend | Add new config sections |
| `src/core/neo4j_utils.py` | Keep as-is | Already suitable for sharing |
| `citation_tool/` | Keep as pip package | Install via requirements.txt |

---

## Appendix B: Environment Variables

Complete list of environment variables for the system:

```bash
# Database Configuration
DATABASE_URL=postgresql://graphrag:password@localhost:5432/graphrag
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password

# LLM Configuration
LLM_BASE_URL=http://localhost:8080/v1
OPENAI_API_KEY=sk-xxxxx
LLM_MODEL=120b-instruct

# Citation Engine
CITATION_DB_URL=postgresql://graphrag:password@localhost:5432/graphrag
CITATION_LLM_URL=http://localhost:8080/v1
CITATION_LLM_MODEL=120b-instruct
CITATION_REASONING_REQUIRED=low

# Web Search (Optional)
TAVILY_API_KEY=tvly-xxxxx

# Agent Configuration
CREATOR_POLLING_INTERVAL=30
VALIDATOR_POLLING_INTERVAL=10
MAX_REQUIREMENT_RETRIES=5

# Observability (Optional)
LOG_LEVEL=INFO
MONGODB_URL=mongodb://localhost:27017/graphrag_logs
```

---

## Appendix C: Migration from Current System

Steps to migrate from the current four-stage pipeline to the new two-agent system:

1. **Keep existing UI functional**: The Streamlit UI should continue to work during migration
2. **Parallel operation**: New agents can run alongside existing pipeline initially
3. **Database migration**: Existing requirements in Neo4j don't need migration; new agents add to them
4. **Gradual cutover**: Start with new documents going through new pipeline

---

*Document Version: 1.0*
*Based on: masterplan.md v1.1*
*Created: January 2026*
