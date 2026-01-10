# Graph-RAG Autonomous Agent System
## Comprehensive Masterplan

**Version:** 1.1
**Created:** January 2026
**Status:** Design Document - Ready for Implementation Roadmap

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Vision & Goals](#2-vision--goals)
3. [System Architecture Overview](#3-system-architecture-overview)
4. [The Two-Agent Model](#4-the-two-agent-model)
5. [The Creator Agent](#5-the-creator-agent)
6. [The Validator Agent](#6-the-validator-agent)
7. [The Orchestrator (Main Application)](#7-the-orchestrator-main-application)
8. [Shared Infrastructure](#8-shared-infrastructure)
9. [Database Architecture](#9-database-architecture)
10. [Context Window Management](#10-context-window-management)
11. [Long-Running Task Patterns](#11-long-running-task-patterns)
12. [Citation & Provenance System](#12-citation--provenance-system)
13. [Neo4j Schema & Metamodel](#13-neo4j-schema--metamodel)
14. [Deployment Architecture](#14-deployment-architecture)
15. [Resource Tracking & Observability](#15-resource-tracking--observability)
16. [Future Scaling Considerations](#16-future-scaling-considerations)
17. [Appendices](#17-appendices)

---

## 1. Executive Summary

### 1.1 What We're Building

A **two-agent autonomous system** for requirement extraction, validation, and graph integration. The system is designed to run for extended periods (hours, days, or even weeks) processing complex documents like legal texts (GDPR, GoBD), regulatory frameworks, and compliance requirements. The agents work asynchronously in a producer-consumer pattern, creating well-researched, citation-backed requirements and validating them against a company's existing knowledge graph.

### 1.2 Core Philosophy

This system is built on several key principles:

1. **Quality Over Speed**: The agents should take as much time as needed to produce thorough, well-reasoned outputs. A requirement that takes 30 minutes to properly research and document is preferable to one produced in 30 seconds without adequate backing.

2. **Citation-First Design**: Every claim, decision, and requirement must be traceable to its source. The system uses a structured citation engine that verifies claims against source material.

3. **Autonomous Operation**: Agents should be capable of running indefinitely with minimal human intervention, managing their own context, breaking down large tasks, and recovering from failures.

4. **Resource Utilization**: The system is designed to utilize home lab GPU infrastructure that would otherwise sit idle. Lower priority processing fills gaps between interactive AI usage.

5. **Traceable Decisions**: Every agent decision should be documented with reasoning, sources, and citations. An auditor should be able to understand why any requirement exists and how it was validated.

### 1.3 The Two Agents

| Agent | Purpose | Primary Output |
|-------|---------|----------------|
| **Creator Agent** | Extracts and formulates requirements from source documents | Well-documented requirements in shared cache |
| **Validator Agent** | Validates requirements against company knowledge graph | Integrated requirement nodes with relationships |

### 1.4 Current State vs. Target State

**Current State (Implemented):**
- Four-stage document ingestion pipeline (Document Processor → Extractor → Validator → Graph Agent)
- Sequential processing within a single Streamlit session
- Basic LangGraph agents for each stage
- Metamodel compliance validation
- Citation Engine (fully implemented as separate pip package in `citation_tool/`)

**Target State:**
- Two autonomous agents running as separate containers
- Asynchronous producer-consumer pattern via shared PostgreSQL cache
- Long-running background processing (hours/days/weeks)
- Full citation integration with verification
- Main orchestrator coordinating jobs across agents
- Comprehensive resource and observability tracking

### 1.5 Existing Code Reuse Strategy

**Note:** The existing codebase contains working implementations that should be reused. The detailed mapping of which components map to which agent is deferred to the implementation roadmap, but the general strategy is:

| Existing Component | Target Location |
|-------------------|-----------------|
| `DocumentProcessor` | Creator Agent (document chunking) |
| `RequirementExtractorAgent` | Creator Agent (candidate extraction) |
| `RequirementValidatorAgent` | Partially reused in both agents |
| `GraphAgent` | Validator Agent (Neo4j integration) |
| `MetamodelValidator` | Validator Agent (compliance checking) |
| `Neo4jConnection` | Shared infrastructure |
| Citation Engine | Shared infrastructure (pip package) |

The implementation roadmap will specify exactly which functions to reuse, refactor, or replace.

---

## 2. Vision & Goals

### 2.1 Primary Goals

#### Goal 1: Autonomous Requirement Creation
The Creator Agent should be able to receive a document (e.g., the GDPR) with a prompt (e.g., "Our US company is expanding to Europe") and:
- Break down the document into logical chunks
- Identify potential requirements and their implications
- Research each requirement thoroughly using web search and knowledge bases
- Create properly formulated, citation-backed requirements
- Handle documents of any size without context window limitations

#### Goal 2: Intelligent Validation & Integration
The Validator Agent should take requirements from the Creator and:
- Determine if requirements are relevant to the company's situation
- Explore the Neo4j graph to find related business objects and messages
- Create appropriate relationships (FULFILLS, DOES_NOT_FULFILL, RELATES_TO)
- Validate metamodel compliance before committing changes
- Document all decisions with sources and reasoning

#### Goal 3: Long-Running Autonomous Operation
The system should be capable of:
- Running for days or weeks without human intervention
- Managing context windows through compaction and summarization
- Checkpointing progress to recover from failures
- Utilizing GPU resources efficiently (lower priority, filling idle gaps)
- Processing jobs incrementally while remaining responsive

#### Goal 4: Complete Traceability
Every aspect of the system should be auditable:
- Which source document led to which requirement
- What reasoning was used to make each decision
- Which external sources were consulted
- What the exact state of the graph was before and after integration

### 2.2 Design Constraints

| Constraint | Description |
|------------|-------------|
| **Infrastructure** | Self-hosted 120B parameter LLM on home lab with 30TB storage |
| **Priority** | Lower priority than interactive AI usage; fills gaps |
| **Speed** | Not a priority; thoroughness is paramount |
| **Reliability** | Must handle failures gracefully; checkpoint everything |
| **Observability** | Full logging of all LLM requests and tool calls |

### 2.3 What This Is NOT

- **Not a real-time system**: We're not building a chatbot that responds in milliseconds
- **Not a one-shot pipeline**: We're not processing documents end-to-end in a single session
- **Not a simple classifier**: Requirements aren't just extracted; they're researched, contextualized, and properly documented
- **Not constrained to single requirements**: A "requirement" can be a high-level concept that needs multiple graph nodes to represent

---

## 3. System Architecture Overview

### 3.1 Communication Pattern: Paper-Stack System

Before diving into the architecture, it's important to understand the communication philosophy. The agents communicate through a **paper-stack polling pattern** rather than direct messaging:

**How It Works:**
- Each agent runs an event loop that polls the database for work
- Creator Agent checks the `jobs` table for jobs where `creator_status != 'completed'`
- Validator Agent checks the `requirement_cache` table for requirements where `status = 'pending'`
- When an agent finishes a task, it "puts the sheet on the paper stack to its right" (writes to the appropriate table)
- The other agent picks up work from its "desk to the left" when it becomes available
- If there's nothing to do, the agent waits and checks periodically

**Advantages:**
- Decoupled: Agents don't need to know about each other's existence
- Resilient: One agent's crash doesn't affect the other
- Scalable: Multiple agents can poll the same queue
- Auditable: All state transitions are recorded in the database
- Simple: No message queuing infrastructure needed (PostgreSQL is sufficient)

**Polling Interval:**
- Agents poll every 10-30 seconds when idle
- Immediately poll again after completing a task
- Use PostgreSQL `FOR UPDATE SKIP LOCKED` to prevent multiple validators grabbing the same requirement

### 3.2 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              ORCHESTRATOR (Main Application)                         │
│                                                                                      │
│  ┌──────────────────────────────────────────────────────────────────────────────┐   │
│  │  Job Management: Create jobs, track status, coordinate agents, mark complete │   │
│  └──────────────────────────────────────────────────────────────────────────────┘   │
│                                          │                                           │
│               ┌──────────────────────────┴──────────────────────────┐               │
│               │                                                      │               │
│               ▼                                                      ▼               │
│  ┌────────────────────────┐                          ┌────────────────────────┐     │
│  │     CREATOR AGENT      │                          │    VALIDATOR AGENT     │     │
│  │     (Container)        │                          │     (Container)        │     │
│  │                        │                          │                        │     │
│  │  • Document processing │                          │  • Graph exploration   │     │
│  │  • Requirement extract │                          │  • Relevance check     │     │
│  │  • Web search/research │                          │  • Relationship build  │     │
│  │  • Citation creation   │                          │  • Metamodel validate  │     │
│  │  • Candidate output    │                          │  • Graph integration   │     │
│  └───────────┬────────────┘                          └───────────┬────────────┘     │
│              │                                                    │                  │
│              │              SHARED INFRASTRUCTURE                 │                  │
│              │                                                    │                  │
│              ▼                                                    ▼                  │
│  ┌───────────────────────────────────────────────────────────────────────────────┐  │
│  │                         PostgreSQL (Shared Cache)                              │  │
│  │  ┌─────────────┐  ┌─────────────────┐  ┌─────────────┐  ┌─────────────────┐   │  │
│  │  │    Jobs     │  │  Requirements   │  │  Citations  │  │   LLM Requests  │   │  │
│  │  │   Table     │  │     Cache       │  │    Store    │  │      Log        │   │  │
│  │  └─────────────┘  └─────────────────┘  └─────────────┘  └─────────────────┘   │  │
│  └───────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                      │
│  ┌───────────────────────────────────────────────────────────────────────────────┐  │
│  │                          Neo4j (Knowledge Graph)                               │  │
│  │  ┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐    │  │
│  │  │    Requirements     │  │   BusinessObjects   │  │      Messages       │    │  │
│  │  └─────────────────────┘  └─────────────────────┘  └─────────────────────┘    │  │
│  └───────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                      │
│  ┌───────────────────────────────────────────────────────────────────────────────┐  │
│  │                          Optional: MongoDB (Archive)                           │  │
│  │                    Full LLM request/response storage for analysis              │  │
│  └───────────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.3 Component Responsibilities

| Component | Responsibility |
|-----------|----------------|
| **Orchestrator** | Job creation, agent coordination, status tracking, completion detection |
| **Creator Agent** | Document processing, requirement extraction, research, citation creation |
| **Validator Agent** | Graph analysis, relevance checking, relationship creation, integration |
| **PostgreSQL** | Shared requirement cache, citation storage, job tracking, LLM logging |
| **Neo4j** | Knowledge graph storage, company model, requirement relationships |
| **MongoDB** (optional) | Full LLM request/response archive for analysis |

### 3.4 Communication Flow

The agents do **not** communicate directly with each other. All communication happens through shared database state:

**Job Initiation:**
1. User submits job via Orchestrator API (FastAPI endpoint)
2. Orchestrator creates job record in PostgreSQL with `status = 'created'`
3. Creator Agent's event loop detects new job and picks it up

**Requirement Flow:**
1. Creator Agent processes document, extracts requirements
2. Creator Agent writes each requirement to `requirement_cache` with `status = 'pending'`
3. Validator Agent's event loop detects pending requirements
4. Validator Agent updates status to `'validating'`, processes, then updates to `'integrated'` or `'rejected'`

**Database-as-Queue Pattern:**
```sql
-- Validator Agent picks up next pending requirement
SELECT * FROM requirement_cache
WHERE status = 'pending'
  AND job_id = $current_job_id
ORDER BY created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

This decoupled architecture allows:
- Independent scaling of agents
- Failure isolation (one agent's crash doesn't affect the other)
- Progress tracking through database state
- Easy addition of more agents in the future

---

## 4. The Two-Agent Model

### 4.1 Why Two Agents?

The division into Creator and Validator serves several purposes:

**Separation of Concerns:**
- Creation requires document understanding, research skills, and formulation abilities
- Validation requires graph analysis, business context understanding, and integration skills

**Different Tool Requirements:**
- Creator needs: Document parsing, web search, citation tools
- Validator needs: Cypher queries, metamodel validation, graph mutation tools

**Asynchronous Processing:**
- Creator may produce requirements faster than Validator can process them (or vice versa)
- Decoupling allows each to work at its own pace
- Creates a natural buffer for work-in-progress

**Failure Isolation:**
- A Creator crash doesn't lose validated requirements
- A Validator crash doesn't lose extracted requirements
- PostgreSQL cache provides durability

### 4.2 Producer-Consumer Pattern

```
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│  Creator Agent  │ ───▶ │   PostgreSQL    │ ───▶ │ Validator Agent │
│   (Producer)    │      │ Requirement     │      │   (Consumer)    │
│                 │      │    Cache        │      │                 │
│ Creates reqs    │      │ status: pending │      │ Validates reqs  │
│ at own pace     │      │ status: validat │      │ at own pace     │
└─────────────────┘      │ status: done    │      └─────────────────┘
                         └─────────────────┘
```

The requirement cache table serves as the queue:
- Creator inserts with `status = 'pending'`
- Validator updates to `status = 'validating'` when picked up
- Validator updates to `status = 'integrated'` or `status = 'rejected'` when done

### 4.3 Autonomy Levels

Both agents should operate at a high level of autonomy:

**Level 1 - Task Execution** (Current State):
- Agent receives specific instructions
- Executes tools as directed
- Reports results

**Level 2 - Self-Directed Planning** (Target State):
- Agent receives high-level goal
- Creates its own plan
- Manages its own todo list
- Decides when to research vs. when to conclude

**Level 3 - Adaptive Operation** (Future State):
- Agent learns from feedback
- Adjusts approach based on past successes/failures
- Requests human input when confidence is low

For this system, we target **Level 2** autonomy where agents:
- Receive a job (document + prompt for Creator, requirement for Validator)
- Create their own execution plan
- Work through the plan iteratively
- Manage their own context and state
- Signal completion when satisfied with output quality

---

## 5. The Creator Agent

### 5.1 Purpose & Role

The Creator Agent is responsible for transforming source documents into well-documented, citation-backed requirements. It operates as a researcher and analyst, not just an extractor.

**Key Metaphor:** Think of the Creator Agent as a diligent research assistant who:
- Reads the entire document carefully
- Identifies relevant sections
- Cross-references with external sources
- Takes thorough notes with citations
- Produces a polished set of requirements

### 5.2 Input/Output Specification

**Input:**
```json
{
  "job_id": "job_12345",
  "document_path": "/path/to/gdpr.pdf",
  "prompt": "Our US company is expanding to Europe. What requirements apply?",
  "context": {
    "company_domain": "car_rental",
    "existing_compliance": ["GoBD"],
    "priority_areas": ["data_privacy", "record_retention"]
  }
}
```

**Output:**
- Requirements written to PostgreSQL cache (not returned directly)
- Job status updates
- Activity logs

### 5.3 Processing Phases

The Creator Agent works through several distinct phases:

#### Phase 1: Document Preprocessing

**Goal:** Understand the document structure and create a working plan.

**Activities:**
- Extract text from PDF/DOCX/TXT using the existing `DocumentProcessor`
- Detect document type (legal, technical, policy, etc.)
- Identify major sections, chapters, articles
- Create a chunking strategy appropriate for the document type
- Write chunks to agent's working storage

**Output:**
- Document metadata
- List of chunks with section hierarchy
- Initial processing plan stored in agent's workspace

**Duration:** 5-30 minutes depending on document size

#### Phase 2: Candidate Identification

**Goal:** Identify potential requirement candidates from the document.

**Activities:**
- For each chunk, identify requirement-like statements
- Use pattern matching for modal verbs ("must", "shall", "required")
- Detect obligation phrases and constraint patterns
- Group related candidates that might form a single requirement
- Write candidate list to agent's workspace

**Key Decision Point:** The agent must decide how granular to be. Options:
1. One requirement per obligation statement
2. Grouped requirements by topic
3. Hierarchical requirements (parent with children)

**Output:**
- List of requirement candidates with:
  - Raw text from document
  - Location (page, section)
  - Initial classification (functional, compliance, constraint)
  - Grouping suggestions

**Duration:** 30 minutes - 2 hours depending on document complexity

#### Phase 3: Research & Enrichment

**Goal:** For each candidate, research thoroughly and gather supporting information.

**Activities (per candidate):**
- Determine if additional context is needed
- Use web search to find:
  - Official guidance documents
  - Implementation examples
  - Common interpretations
  - Related regulations
- Query the knowledge graph for:
  - Similar existing requirements
  - Related business objects
  - Potentially affected messages
- Create citations for all sources consulted

**Research Depth Decision:**
The agent should assess each candidate and decide:
- **Quick validation** (5 min): Clear, straightforward requirement
- **Standard research** (15-30 min): Needs clarification or context
- **Deep research** (1+ hour): Complex topic requiring multiple sources

**Output:**
- Research notes with citations
- Entity mentions identified
- Relationship suggestions

**Duration:** 15 minutes - 1+ hours per candidate

#### Phase 4: Requirement Formulation

**Goal:** Transform candidates into properly structured requirements.

**Activities (per candidate):**
- Determine if candidate should become:
  - A single requirement
  - Multiple related requirements
  - Part of an existing requirement
  - Discarded (not actually a requirement)
- Formulate requirement text following conventions
- Add all required metadata
- Create citations linking claims to sources
- Determine confidence level

**Requirement Structure:**
```json
{
  "candidate_id": "cand_001",
  "text": "The system must encrypt personal data at rest using AES-256 or equivalent",
  "name": "Personal Data Encryption at Rest",
  "type": "compliance",
  "gobd_relevant": false,
  "gdpr_relevant": true,
  "priority": "high",
  "source_document": "gdpr.pdf",
  "source_location": {"article": "32", "paragraph": "1.a"},
  "citations": ["cite_001", "cite_002"],
  "mentioned_objects": ["PersonalData", "EncryptionService"],
  "mentioned_messages": ["CustomerDataMessage"],
  "reasoning": "Article 32(1)(a) requires 'pseudonymisation and encryption'...",
  "confidence": 0.85,
  "research_notes": "Cross-referenced with ENISA guidelines...",
  "created_at": "2026-01-02T10:30:00Z",
  "status": "pending"
}
```

**Duration:** 10-30 minutes per requirement

#### Phase 5: Duplicate & Overlap Check

**Goal:** Ensure requirements don't duplicate each other or existing requirements.

**Activities:**
- Compare all newly created requirements against each other
- Check for semantic overlap using similarity measures
- Query Neo4j for similar existing requirements
- Merge or link requirements that are essentially the same
- Note relationships between related requirements

**Output:**
- Deduplicated requirement list
- Relationship suggestions (REFINES, DEPENDS_ON, TRACES_TO)

**Duration:** 15-30 minutes for final review

#### Phase 6: Output & Handoff

**Goal:** Write finalized requirements to the shared cache.

**Activities:**
- Validate each requirement against output schema
- Verify all citations are properly formed
- Write requirements to PostgreSQL cache with `status = 'pending'`
- Update job progress in jobs table
- If all candidates processed, mark phase as complete

### 5.4 Tool Requirements

The Creator Agent needs access to:

| Tool | Purpose | Implementation |
|------|---------|----------------|
| `extract_document_text` | PDF/DOCX/TXT extraction | Existing `DocumentProcessor` |
| `chunk_document` | Intelligent chunking | Existing `DocumentChunker` |
| `web_search` | External research | Tavily or equivalent |
| `cite_document` | Create document citations | Citation Engine |
| `cite_web` | Create web citations | Citation Engine |
| `query_similar_requirements` | Check for duplicates | PostgreSQL + Neo4j |
| `write_requirement_cache` | Output requirements | PostgreSQL |
| `read_workspace` | Read agent's working files | Local filesystem |
| `write_workspace` | Write agent's working files | Local filesystem |
| `update_todo_list` | Track progress | Agent state |

### 5.5 Context Management Strategy

Given that the Creator Agent may run for hours or days, context management is critical.

**Workspace Approach:**
The agent maintains a workspace directory for the current job containing:
- `document_chunks/` - Extracted document chunks
- `candidates/` - Identified requirement candidates
- `research/` - Research notes per candidate
- `requirements/` - Finalized requirements
- `todo.json` - Current task list
- `progress.json` - Phase and iteration tracking

**Context Window Strategy:**
- Keep current task context in LLM context window
- Store all intermediate results in workspace files
- Use "just-in-time" retrieval for specific information
- Summarize completed phases to free context space
- Checkpoint state to enable recovery

**Compaction Triggers:**
- When context exceeds 80k tokens, compact older phases
- Keep last 3 tool calls in raw form
- Summarize earlier tool calls to notes
- Always keep current task and immediate context raw

### 5.6 Error Handling & Recovery

**Checkpoint Strategy:**
- Save state after each candidate is processed
- Checkpoint includes:
  - Current phase
  - Candidates processed/remaining
  - Requirements created
  - Context summary

**Recovery Approach:**
- On restart, load checkpoint
- Resume from last completed candidate
- Reread relevant workspace files
- Continue processing

**Failure Modes:**
| Failure | Recovery |
|---------|----------|
| LLM timeout | Retry with exponential backoff |
| Web search failure | Skip source, note in reasoning |
| Document parse error | Mark chunk as failed, continue |
| Citation verification fail | Flag for human review |

---

## 6. The Validator Agent

### 6.1 Purpose & Role

The Validator Agent is responsible for taking requirements from the shared cache and determining their relevance and integration approach for the company's specific context. It's the bridge between abstract requirements and concrete business impact.

**Key Metaphor:** Think of the Validator Agent as a compliance analyst who:
- Reviews each requirement against company context
- Explores the knowledge graph to understand current state
- Determines what's already fulfilled vs. what's missing
- Documents gaps and recommendations
- Updates the graph with new requirements and relationships

### 6.2 Input/Output Specification

**Input:**
The Validator continuously polls the PostgreSQL cache for requirements with `status = 'pending'`.

**Processing Trigger:**
```sql
SELECT * FROM requirement_cache
WHERE status = 'pending'
  AND job_id = $current_job_id
ORDER BY created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

**Output:**
- Updated Neo4j graph with new requirement nodes and relationships
- Requirement cache entry updated with validation results
- Validation report with reasoning and citations

### 6.3 Processing Phases

#### Phase 1: Requirement Understanding

**Goal:** Fully understand what the requirement is asking.

**Activities:**
- Read the requirement from the cache
- Load associated citations
- Understand the domain context
- Identify key concepts and entities mentioned

**Output:**
- Parsed requirement understanding
- List of concepts to investigate

**Duration:** 2-5 minutes

#### Phase 2: Relevance Assessment

**Goal:** Determine if this requirement applies to the company.

**Activities:**
- Query Neo4j for relevant business objects
- Check if the domain/concepts exist in the company model
- Assess if the requirement's scope matches company operations
- Consider geographic, industry, and regulatory applicability

**Decision Tree:**
```
Is the domain relevant to our company?
├── No → Mark as "not_relevant", add comment, done
└── Yes → Continue
    │
    Does the company have related business objects?
    ├── No → Might need to CREATE new objects (note for review)
    └── Yes → Continue to fulfillment check
```

**Output:**
- Relevance decision (relevant, partially_relevant, not_relevant)
- Reasoning with citations
- List of related business objects/messages

**Duration:** 10-30 minutes

#### Phase 3: Fulfillment Analysis

**Goal:** Determine current fulfillment status.

**Activities:**
- For each related business object, analyze:
  - Does it already implement what the requirement asks?
  - Are there existing requirements that cover this?
  - What's the gap between current state and requirement?
- Query for existing relationships
- Research company documentation if available

**Analysis Categories:**
```
FULFILLED:      Current state meets requirement
PARTIALLY_MET:  Some aspects are covered, gaps exist
NOT_FULFILLED:  Requirement is not addressed
UNKNOWN:        Cannot determine from available data
```

**Output:**
- Fulfillment assessment per related object
- Gap analysis
- Notes on what needs to change

**Duration:** 20-60 minutes

#### Phase 4: Relationship Planning

**Goal:** Determine what graph changes are needed.

**Activities:**
- Decide on requirement node creation:
  - Single node for simple requirements
  - Multiple nodes for complex requirements
  - Link to existing requirement if duplicate
- Plan relationships:
  - Which BusinessObjects should be linked
  - What relationship types (FULFILLS, DOES_NOT_FULFILL, IMPACTS, RELATES_TO)
  - Any Requirement-Requirement relationships
- Pre-validate against metamodel

**Relationship Types (Extended):**
```
Requirement → BusinessObject:
  - IMPACTS_OBJECT: General impact/relevance
  - RELATES_TO_OBJECT: Informational connection
  - FULFILLED_BY_OBJECT: Business object implements requirement
  - NOT_FULFILLED_BY_OBJECT: Gap exists

Requirement → Requirement:
  - REFINES: More specific version
  - DEPENDS_ON: Prerequisite relationship
  - TRACES_TO: Traceability link
  - SUPERSEDES: Newer requirement replacing old
```

**Output:**
- Proposed graph operations (nodes, relationships)
- Metamodel compliance pre-check results

**Duration:** 15-30 minutes

#### Phase 5: Integration Execution

**Goal:** Execute the planned graph changes.

**Activities:**
- Create requirement node(s) if needed
- Create relationships
- Run metamodel validation
- If validation fails:
  - Analyze violations
  - Attempt correction
  - If cannot correct, flag for human review

**Transaction Safety:**
```
BEGIN TRANSACTION
  - Create requirement node
  - Create relationships
  - Run compliance check
  IF compliance_passed:
    COMMIT
  ELSE:
    ROLLBACK
    - Log violations
    - Flag for review
```

**Output:**
- Graph changes committed
- Validation report
- Any warnings or issues

**Duration:** 5-15 minutes

#### Phase 6: Documentation & Cleanup

**Goal:** Document all decisions and update tracking.

**Activities:**
- Write validation reasoning to requirement cache
- Update requirement status to 'integrated' or 'rejected'
- Create any needed notes or flags
- Update job progress
- Clean up workspace

**Output:**
- Updated cache entry
- Validation report with citations
- Job progress update

### 6.4 Tool Requirements

| Tool | Purpose | Implementation |
|------|---------|----------------|
| `execute_cypher_query` | Graph queries | Existing Neo4j tool |
| `get_database_schema` | Understand graph structure | Existing tool |
| `find_similar_requirements` | Duplicate detection | Similarity search |
| `validate_schema_compliance` | Metamodel validation | Existing MetamodelValidator |
| `create_requirement_node` | Graph mutations | New tool needed |
| `create_relationship` | Graph mutations | New tool needed |
| `cite_database` | Create graph citations | Citation Engine |
| `web_search` | Additional research | Tavily |
| `read_requirement_cache` | Get requirements to validate | PostgreSQL |
| `update_requirement_cache` | Update validation status | PostgreSQL |

### 6.5 Context Management Strategy

The Validator Agent processes requirements one at a time, which naturally limits context growth.

**Per-Requirement Context:**
- Requirement text and metadata
- Related citations
- Graph query results
- Validation analysis

**Workspace Structure:**
- `current_requirement/` - Working files for current requirement
- `validation_history/` - Summary of validated requirements
- `graph_state/` - Cached schema and common queries
- `todo.json` - Current task list

**Context Reset:**
After each requirement is processed:
- Summarize the validation into history
- Clear current requirement context
- Start fresh with next requirement

### 6.6 Error Handling & Recovery

**Checkpoint Strategy:**
- Save state after each requirement is validated
- On restart, check for any 'validating' status requirements (interrupted)
- Reset those to 'pending' and reprocess

**Transaction Safety:**
All graph mutations happen in transactions:
- If any step fails, entire change is rolled back
- Requirement remains in 'pending' state for retry
- After N retries, escalate to human review

---

## 7. The Orchestrator (Main Application)

### 7.1 Purpose & Role

The Orchestrator is the entry point and coordinator for the entire system. It doesn't do the work itself but manages jobs and coordinates the agents.

### 7.2 Responsibilities

| Responsibility | Description |
|----------------|-------------|
| **Job Creation** | Accept user input, create job record, store documents |
| **Agent Dispatch** | Send initial request to Creator Agent |
| **Progress Tracking** | Monitor job status, agent health, completion |
| **Completion Detection** | Determine when both agents are done |
| **Results Aggregation** | Compile final report when job completes |
| **Health Monitoring** | Detect stuck agents, trigger recovery |

### 7.3 Job Lifecycle

```
┌─────────────┐
│   CREATED   │ ← User submits document + prompt
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  PROCESSING │ ← Creator Agent working on document
│   (CREATOR) │
└──────┬──────┘
       │
       ▼ (First requirements appear in cache)
┌─────────────┐
│  PROCESSING │ ← Both agents working
│    (BOTH)   │   Creator: still extracting
└──────┬──────┘   Validator: processing requirements
       │
       ▼ (Creator finishes)
┌─────────────┐
│  PROCESSING │ ← Only Validator still working
│ (VALIDATOR) │
└──────┬──────┘
       │
       ▼ (All requirements processed)
┌─────────────┐
│  COMPLETED  │ ← Job done, report available
└─────────────┘
```

### 7.4 Job Table Schema

```sql
CREATE TABLE jobs (
    id UUID PRIMARY KEY,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),

    -- Input
    prompt TEXT NOT NULL,
    document_path TEXT NOT NULL,
    document_name TEXT NOT NULL,
    context JSONB,

    -- Status
    status VARCHAR(50) NOT NULL DEFAULT 'created',
    creator_status VARCHAR(50) DEFAULT 'pending',
    validator_status VARCHAR(50) DEFAULT 'pending',

    -- Progress
    requirements_created INT DEFAULT 0,
    requirements_validated INT DEFAULT 0,
    requirements_integrated INT DEFAULT 0,
    requirements_rejected INT DEFAULT 0,

    -- Timing
    started_at TIMESTAMP,
    creator_finished_at TIMESTAMP,
    completed_at TIMESTAMP,

    -- Results
    error_log JSONB DEFAULT '[]',
    final_report JSONB
);
```

### 7.5 CLI Interface

For the initial implementation, the Orchestrator is a CLI application:

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

### 7.6 Completion Detection Logic

The Orchestrator periodically checks job status:

```python
def check_job_completion(job_id: str) -> bool:
    """
    A job is complete when:
    1. Creator has finished (no more requirements being created)
    2. All requirements in cache have been processed by Validator
    """
    job = get_job(job_id)

    # Check Creator status
    if job.creator_status != 'completed':
        return False

    # Check if any pending requirements remain
    pending_count = count_requirements(
        job_id=job_id,
        status=['pending', 'validating']
    )

    if pending_count > 0:
        return False

    # Job is complete!
    return True
```

### 7.7 Health Monitoring

The Orchestrator watches for stuck states:

| Condition | Detection | Action |
|-----------|-----------|--------|
| Agent not responding | No activity for 30 minutes | Ping agent, restart if needed |
| Requirement stuck in 'validating' | Same status for 1 hour | Reset to 'pending', log warning |
| Job stuck in 'processing' | No progress for 6 hours | Alert, consider manual intervention |

---

## 8. Shared Infrastructure

### 8.1 Shared Tools & Utilities

Both agents share certain functionality that should be implemented once:

#### 8.1.1 Citation Engine

The Citation Engine (as designed in `citation-engine-design.md`) is shared across both agents:

**Mode:** Multi-Agent (PostgreSQL)

**Usage:**
- Creator Agent: `cite_document()`, `cite_web()` for research citations
- Validator Agent: `cite_database()` for graph-based citations

**Verification:** Synchronous verification before citation is stored

#### 8.1.2 Web Search

Both agents may need to search the web:
- Creator: Research requirements, find guidance documents
- Validator: Clarify requirement interpretation, find examples

**Implementation:** Tavily API wrapper with rate limiting

#### 8.1.3 LLM Client

Both agents use the same LLM infrastructure:

**Configuration:**
```python
{
    "base_url": os.getenv("LLM_BASE_URL"),  # Local llama.cpp or vLLM
    "model": "120b-instruct",
    "api_key": os.getenv("OPENAI_API_KEY"),  # Compatible API
    "max_tokens": 4096,
    "temperature": 0.0
}
```

**Request Priority:**

**Note:** LLM request priority is handled externally by the home lab infrastructure and is **not part of this project's scope**. The LLM server deployment includes a priority scheduler that:
- Gives high priority to interactive user requests
- Gives low priority to background agent requests
- Agents always submit with low priority to yield to interactive use

This scheduling is implemented at the LLM server level (vLLM/llama.cpp deployment), not within the agent code.

### 8.2 Shared Configuration

Configuration is loaded from `config/llm_config.json`:

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
    "max_chunk_size": 1000
  },
  "validator_agent": {
    "duplicate_threshold": 0.95,
    "auto_integrate": false,
    "require_citations": true
  },
  "citation_engine": {
    "mode": "multi-agent",
    "verification_required": true,
    "reasoning_required": "low"
  },
  "context_management": {
    "compaction_threshold_tokens": 100000,
    "summarization_trigger_tokens": 128000,
    "keep_raw_turns": 3
  }
}
```

### 8.3 Directory Structure

```
src/
├── agents/
│   ├── creator/
│   │   ├── __init__.py
│   │   ├── creator_agent.py      # Main Creator Agent class
│   │   ├── document_processor.py # Document handling
│   │   ├── candidate_extractor.py # Requirement identification
│   │   ├── researcher.py         # Web/DB research
│   │   └── tools.py              # Creator-specific tools
│   │
│   ├── validator/
│   │   ├── __init__.py
│   │   ├── validator_agent.py    # Main Validator Agent class
│   │   ├── relevance_analyzer.py # Relevance checking
│   │   ├── fulfillment_checker.py # Gap analysis
│   │   ├── graph_integrator.py   # Neo4j mutations
│   │   └── tools.py              # Validator-specific tools
│   │
│   └── shared/
│       ├── __init__.py
│       ├── context_manager.py    # Context compaction/summarization
│       ├── checkpoint.py         # State persistence
│       ├── workspace.py          # File-based workspace
│       └── todo_manager.py       # Task tracking
│
├── core/
│   ├── __init__.py
│   ├── config.py                 # Configuration loading
│   ├── neo4j_utils.py            # Neo4j connection (existing)
│   ├── postgres_utils.py         # PostgreSQL connection
│   ├── metamodel_validator.py    # Metamodel validation (existing)
│   └── document_processor.py     # Document extraction (existing)
│
├── citation/
│   ├── __init__.py
│   ├── engine.py                 # CitationEngine class
│   ├── sources.py                # Source registration
│   ├── verification.py           # Citation verification
│   └── storage.py                # PostgreSQL storage
│
├── orchestrator/
│   ├── __init__.py
│   ├── job_manager.py            # Job CRUD operations
│   ├── dispatcher.py             # Agent dispatch logic
│   ├── monitor.py                # Health monitoring
│   └── reporter.py               # Report generation
│
└── ui/ (future)
    └── ...
```

---

## 9. Database Architecture

### 9.1 PostgreSQL Schema

PostgreSQL serves as the shared state between agents:

```sql
-- Jobs table (managed by Orchestrator)
CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    prompt TEXT NOT NULL,
    document_path TEXT NOT NULL,
    document_name TEXT,
    context JSONB,

    status VARCHAR(50) NOT NULL DEFAULT 'created',
    creator_status VARCHAR(50) DEFAULT 'pending',
    validator_status VARCHAR(50) DEFAULT 'pending',

    requirements_created INT DEFAULT 0,
    requirements_validated INT DEFAULT 0,
    requirements_integrated INT DEFAULT 0,
    requirements_rejected INT DEFAULT 0,

    started_at TIMESTAMPTZ,
    creator_finished_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    error_log JSONB DEFAULT '[]'::jsonb,
    final_report JSONB
);

-- Requirement cache (shared between Creator and Validator)
CREATE TABLE requirement_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES jobs(id),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- From Creator
    candidate_id TEXT NOT NULL,
    text TEXT NOT NULL,
    name TEXT,
    requirement_type VARCHAR(50),
    gobd_relevant BOOLEAN DEFAULT FALSE,
    priority VARCHAR(20),

    source_document TEXT,
    source_location JSONB,

    mentioned_objects TEXT[],
    mentioned_messages TEXT[],

    reasoning TEXT,
    research_notes TEXT,
    confidence FLOAT,

    -- Status
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending: waiting for Validator
    -- validating: Validator processing
    -- integrated: Added to graph
    -- rejected: Not relevant or duplicate
    -- review_needed: Requires human attention

    -- From Validator
    validation_result JSONB,
    relevance_assessment TEXT,
    fulfillment_status VARCHAR(50),
    graph_rid TEXT,  -- Requirement ID if integrated
    validation_notes TEXT,
    validated_at TIMESTAMPTZ,

    UNIQUE(job_id, candidate_id)
);

-- Citations table (shared citation storage)
CREATE TABLE citations (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Context
    job_id UUID REFERENCES jobs(id),
    requirement_cache_id UUID REFERENCES requirement_cache(id),
    agent_id VARCHAR(50),  -- 'creator' or 'validator'

    -- Citation content
    claim TEXT NOT NULL,
    verbatim_quote TEXT,
    quote_context TEXT NOT NULL,
    quote_language VARCHAR(10),
    relevance_reasoning TEXT,

    -- Source reference
    source_id INT REFERENCES sources(id),
    locator JSONB NOT NULL,

    -- Metadata
    confidence VARCHAR(20) DEFAULT 'high',
    extraction_method VARCHAR(50) DEFAULT 'direct_quote',

    -- Verification
    verification_status VARCHAR(20) DEFAULT 'pending',
    verification_notes TEXT,
    verified_at TIMESTAMPTZ
);

-- Sources table (registered source documents)
CREATE TABLE sources (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    type VARCHAR(50) NOT NULL,  -- document, website, database, custom
    identifier TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT,
    content TEXT,  -- Extracted text for verification
    content_hash TEXT,  -- SHA-256 hash
    metadata JSONB,

    UNIQUE(type, identifier)
);

-- LLM request log (for analysis and debugging)
CREATE TABLE llm_requests (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    job_id UUID REFERENCES jobs(id),
    agent_id VARCHAR(50),

    request_type VARCHAR(100),
    model VARCHAR(100),
    prompt_tokens INT,
    completion_tokens INT,
    total_tokens INT,

    latency_ms INT,
    success BOOLEAN,
    error_message TEXT,

    -- Optional: Full request/response stored in MongoDB
    mongo_request_id TEXT
);

-- Agent checkpoints (for recovery)
CREATE TABLE agent_checkpoints (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    job_id UUID NOT NULL REFERENCES jobs(id),
    agent_id VARCHAR(50) NOT NULL,

    phase VARCHAR(100),
    iteration INT,
    state JSONB NOT NULL,
    context_summary TEXT,

    UNIQUE(job_id, agent_id)
);

-- Indexes
CREATE INDEX idx_requirement_cache_job_status ON requirement_cache(job_id, status);
CREATE INDEX idx_requirement_cache_status ON requirement_cache(status);
CREATE INDEX idx_citations_job ON citations(job_id);
CREATE INDEX idx_citations_requirement ON citations(requirement_cache_id);
CREATE INDEX idx_llm_requests_job ON llm_requests(job_id);
```

### 9.2 Neo4j Schema (Extended)

Building on the existing FINIUS metamodel, we extend for fulfillment tracking:

**Existing Node Types:**
- `Requirement` - with properties: rid, name, text, type, priority, status, source, goBDRelevant
- `BusinessObject` - with properties: boid, name, description, domain, owner
- `Message` - with properties: mid, name, description, direction, format, protocol

**New Properties for Requirement:**
```
gdprRelevant: BOOLEAN       - GDPR applicability
complianceStatus: STRING    - 'open', 'partial', 'complete'
sourceJobId: STRING         - Link to PostgreSQL job (for tracking)
sourceRequirementId: UUID   - Link to requirement_cache record
createdByAgent: STRING      - 'creator' or 'manual'
validatedByAgent: STRING    - 'validator' or 'manual'
validatedAt: DATETIME       - When validation occurred
```

**Requirement Tagging for Change Tracking:**

Each requirement in the `requirement_cache` table stores a `source_requirement_id` (UUID). When the Validator Agent creates nodes and relationships in Neo4j, it tags them with this ID. This is **not** for rollback purposes, but for:

1. **Traceability:** The agent can revisit all changes made during validation of a specific requirement
2. **Audit:** Understanding which requirement led to which graph changes
3. **Review:** Allowing the agent to "recapture his work" and verify everything is correct

```sql
-- In requirement_cache table
source_requirement_id UUID PRIMARY KEY

-- In Neo4j Requirement node
sourceRequirementId: "uuid-of-requirement-cache-entry"
```

**Extended Relationship Types:**
```
Requirement → BusinessObject:
  - IMPACTS_OBJECT (existing)
  - RELATES_TO_OBJECT (existing)
  - FULFILLED_BY_OBJECT (new) - BO implements requirement
  - NOT_FULFILLED_BY_OBJECT (new) - BO does not implement (gap)

Requirement → Message:
  - IMPACTS_MESSAGE (existing)
  - RELATES_TO_MESSAGE (existing)
  - FULFILLED_BY_MESSAGE (new)
  - NOT_FULFILLED_BY_MESSAGE (new)

Requirement → Requirement:
  - REFINES (existing)
  - DEPENDS_ON (existing)
  - TRACES_TO (existing)
  - SUPERSEDES (new) - Newer requirement replaces older
```

### 9.3 MongoDB (Optional Archive)

For full LLM request/response archiving:

```javascript
// Collection: llm_archive
{
  "_id": ObjectId,
  "postgres_request_id": 12345,  // Link to llm_requests table
  "job_id": "uuid",
  "agent_id": "creator",
  "timestamp": ISODate,

  "request": {
    "model": "120b-instruct",
    "messages": [...],
    "temperature": 0.0,
    "tools": [...]
  },

  "response": {
    "content": "...",
    "tool_calls": [...],
    "finish_reason": "stop"
  },

  "metadata": {
    "prompt_tokens": 5000,
    "completion_tokens": 1500,
    "latency_ms": 15000
  }
}
```

This is optional but valuable for:
- Debugging agent behavior
- Fine-tuning prompt engineering
- Cost/performance analysis
- Compliance auditing

---

## 10. Context Window Management

### 10.1 The Challenge

Running agents for hours or days creates a fundamental tension:
- Agents need full context to make good decisions
- Context windows are limited (even 200k tokens fill up)
- Earlier context may become less relevant
- Cost scales with context size

As Andrej Karpathy puts it: "LLMs are like a new kind of operating system. The LLM is like the CPU and its context window is like the RAM." Just as an operating system curates what fits into RAM, we need "context engineering" to manage what fits into the LLM context window.

### 10.2 LangGraph Built-in Solutions

LangGraph provides several mechanisms for context management that we should leverage:

**1. Pre-Model Hook Pattern:**
LangGraph's `create_react_agent` supports a `pre_model_hook` parameter that runs before each LLM invocation:

```python
from langchain_core.messages.utils import trim_messages, count_tokens_approximately
from langgraph.prebuilt import create_react_agent

def pre_model_hook(state):
    """Trim messages before sending to LLM."""
    trimmed_messages = trim_messages(
        state["messages"],
        strategy="last",  # Keep most recent messages
        token_counter=count_tokens_approximately,
        max_tokens=100000,  # Target 100k tokens for context
        start_on="human",
        end_on=("human", "tool"),
    )
    # Return under llm_input_messages to preserve original history in state
    return {"llm_input_messages": trimmed_messages}

agent = create_react_agent(
    model, tools,
    pre_model_hook=pre_model_hook,
    checkpointer=checkpointer
)
```

**2. Summarization Node Pattern:**
For longer-running agents, use LangGraph's `SummarizationNode`:

```python
from langmem.short_term import SummarizationNode

summarization_node = SummarizationNode(
    token_counter=count_tokens_approximately,
    model=model,
    max_tokens=100000,
    max_summary_tokens=16000,  # Allocate up to 16k for summaries
    output_messages_key="llm_input_messages",
)

# Agent with automatic summarization
agent = create_react_agent(
    model, tools,
    pre_model_hook=summarization_node,
    state_schema=StateWithContext,
    checkpointer=checkpointer
)
```

**Key Design Decision: Non-Destructive Trimming**
- Return trimmed messages under `llm_input_messages` key (NOT `messages`)
- This preserves the full history in graph state for recovery and audit
- Only the LLM sees the trimmed version

### 10.3 Context Management Strategy

We adopt a tiered approach based on industry best practices:

#### Tier 1: Active Context (Always in LLM context)
- Current task and immediate goal
- Last 3 tool calls with full details
- Current requirement being processed
- Active todo list

#### Tier 2: Compressed Context (Summarized in LLM context)
- Earlier phases summarized to key points
- Previous requirements compressed to one-line each
- Research notes compressed to findings only

#### Tier 3: Retrieved Context (Fetched on demand)
- Full document chunks (read when needed)
- Complete research notes (read when needed)
- Previous requirement details (query when needed)
- Historical tool results (query when needed)

### 10.3 Compaction Implementation

```python
class ContextManager:
    """
    Manages context window for long-running agents.

    Uses a combination of:
    - Sliding window for recent items
    - LLM-based summarization for older items
    - File-based storage for full details
    """

    COMPACTION_THRESHOLD = 100_000  # tokens
    SUMMARIZATION_THRESHOLD = 128_000  # tokens
    KEEP_RAW_TURNS = 3

    def should_compact(self, current_tokens: int) -> bool:
        """Check if context needs compaction."""
        return current_tokens > self.COMPACTION_THRESHOLD

    def compact_context(self, messages: List[Message]) -> List[Message]:
        """
        Compact older messages while preserving recent ones.

        Strategy:
        1. Keep system prompt unchanged
        2. Keep last N turns unchanged
        3. Summarize older turns
        4. Store full versions to disk
        """
        # Split messages
        system_msg = messages[0]
        recent_msgs = messages[-self.KEEP_RAW_TURNS * 2:]  # *2 for user+assistant pairs
        older_msgs = messages[1:-self.KEEP_RAW_TURNS * 2]

        if not older_msgs:
            return messages

        # Store full version to disk
        self.save_to_workspace(older_msgs, "compacted_history")

        # Generate summary
        summary = self.summarize_messages(older_msgs)

        # Return compacted context
        return [
            system_msg,
            SystemMessage(content=f"[Context Summary]\n{summary}"),
            *recent_msgs
        ]

    def summarize_messages(self, messages: List[Message]) -> str:
        """Use LLM to summarize a sequence of messages."""
        prompt = """Summarize the following agent interaction history.
Focus on:
- Key decisions made
- Important findings
- Current state and progress
- Any unresolved issues

Keep the summary concise but preserve critical information."""

        # Call LLM with lower-priority summarization request
        response = self.llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=self.format_messages(messages))
        ])

        return response.content
```

### 10.4 Just-in-Time Retrieval

Instead of keeping everything in context, agents fetch information when needed:

```python
class WorkspaceRetrieval:
    """
    Provides just-in-time retrieval of information from workspace.
    """

    def get_requirement_details(self, candidate_id: str) -> dict:
        """Retrieve full details for a specific requirement candidate."""
        path = self.workspace / "candidates" / f"{candidate_id}.json"
        return json.loads(path.read_text())

    def get_research_notes(self, candidate_id: str) -> str:
        """Retrieve research notes for a candidate."""
        path = self.workspace / "research" / f"{candidate_id}.md"
        if path.exists():
            return path.read_text()
        return ""

    def get_document_chunk(self, chunk_id: str) -> str:
        """Retrieve a specific document chunk."""
        path = self.workspace / "document_chunks" / f"{chunk_id}.txt"
        return path.read_text()
```

### 10.5 Context Rot Prevention

Key failure modes and mitigations:

| Failure Mode | Description | Mitigation |
|--------------|-------------|------------|
| Context Burst | Sudden large tool output floods context | Truncate large outputs, store full version to workspace |
| Context Conflict | Contradictory information persists | Prioritize recent over summarized |
| Context Poisoning | Error in summary propagates | Keep recent turns raw for correction |
| Context Noise | Irrelevant details crowd context | Aggressive summarization with focus prompts |

---

## 11. Long-Running Task Patterns

### 11.1 LangGraph Durable Execution

LangGraph 1.0 provides built-in durable execution that is ideal for our long-running agents:

**Key Features:**
- Automatic checkpointing at every "super-step" (after each node execution)
- Checkpoints are machine-independent and can resume on any server
- Built-in fault tolerance: if a node fails, the graph can resume from the last successful step
- PostgresSaver for production-grade persistence

**Checkpointer Configuration:**
```python
from langgraph.checkpoint.postgres import PostgresSaver

# Production checkpointer using PostgreSQL
checkpointer = PostgresSaver(
    connection_string="postgresql://user:pass@localhost:5432/graphrag"
)

# Compile agent with checkpointer
agent = workflow.compile(checkpointer=checkpointer)
```

**Resumable Execution:**
```python
# Resume from checkpoint using thread_id
config = {"configurable": {"thread_id": job_id}}

# Agent automatically resumes from last checkpoint
result = agent.invoke({"messages": [...]}, config=config)
```

### 11.2 Task Queue Architecture

Both agents use a polling pattern for their work:

**Creator Agent Event Loop:**
```python
async def creator_event_loop():
    while not shutdown_requested:
        # Check for new jobs
        job = await get_pending_job()
        if job:
            await process_job(job)
        else:
            await asyncio.sleep(30)  # Poll every 30 seconds
```

**Validator Agent Event Loop:**
```python
async def validator_event_loop():
    while not shutdown_requested:
        # Check for pending requirements using SKIP LOCKED
        requirement = await get_pending_requirement()
        if requirement:
            await validate_requirement(requirement)
        else:
            await asyncio.sleep(10)  # Poll every 10 seconds
```

### 11.3 Checkpointing Strategy

Every agent operation is checkpointed via LangGraph:

```python
class CheckpointManager:
    """
    Manages agent state persistence for recovery.
    """

    def save_checkpoint(
        self,
        job_id: str,
        agent_id: str,
        phase: str,
        iteration: int,
        state: dict,
        context_summary: str
    ):
        """Save current agent state to PostgreSQL."""
        self.db.execute("""
            INSERT INTO agent_checkpoints
            (job_id, agent_id, phase, iteration, state, context_summary)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (job_id, agent_id)
            DO UPDATE SET
                phase = EXCLUDED.phase,
                iteration = EXCLUDED.iteration,
                state = EXCLUDED.state,
                context_summary = EXCLUDED.context_summary,
                created_at = NOW()
        """, job_id, agent_id, phase, iteration, json.dumps(state), context_summary)

    def load_checkpoint(self, job_id: str, agent_id: str) -> dict | None:
        """Load the last checkpoint for recovery."""
        result = self.db.execute("""
            SELECT phase, iteration, state, context_summary
            FROM agent_checkpoints
            WHERE job_id = $1 AND agent_id = $2
        """, job_id, agent_id)

        if result:
            return {
                "phase": result["phase"],
                "iteration": result["iteration"],
                "state": json.loads(result["state"]),
                "context_summary": result["context_summary"]
            }
        return None
```

### 11.4 Error Handling and Retry Patterns

LangGraph provides built-in retry policies that we should leverage:

**Per-Node Retry Configuration:**
```python
from langgraph.graph import StateGraph
from langgraph.pregel import RetryPolicy

# Configure retry policy for specific nodes
retry_policy = RetryPolicy(
    max_attempts=3,
    initial_interval=60,  # 1 minute
    max_interval=900,     # 15 minutes
    backoff_multiplier=2.0,
)

# Apply to nodes that may fail transiently
workflow.add_node(
    "web_search",
    web_search_node,
    retry_policy=retry_policy
)
```

**Multi-Level Error Handling:**
Implement error handling at three levels:

1. **Node Level:** Typed error objects into state
   ```python
   def tool_node(state):
       try:
           result = execute_tool(state)
           return {"result": result}
       except TransientError as e:
           return {"error": {"type": "transient", "message": str(e)}}
       except PermanentError as e:
           return {"error": {"type": "permanent", "message": str(e)}}
   ```

2. **Graph Level:** Conditional edges to error handlers
   ```python
   workflow.add_conditional_edges(
       "process",
       lambda state: "error_handler" if state.get("error") else "continue",
       {"error_handler": "handle_error", "continue": "next_step"}
   )
   ```

3. **Application Level:** Circuit breakers and escalation
   ```python
   class CircuitBreaker:
       def __init__(self, failure_threshold=5, reset_timeout=300):
           self.failures = 0
           self.threshold = failure_threshold
           self.reset_timeout = reset_timeout
           self.last_failure = None

       def record_failure(self):
           self.failures += 1
           self.last_failure = time.time()

       def is_open(self):
           if self.failures >= self.threshold:
               if time.time() - self.last_failure > self.reset_timeout:
                   self.failures = 0
                   return False
               return True
           return False
   ```

**Poison Pill Handling:**
For requirements that repeatedly fail validation:

```python
MAX_REQUIREMENT_RETRIES = 5

async def validate_requirement(requirement):
    if requirement.retry_count >= MAX_REQUIREMENT_RETRIES:
        # Mark as "poison pill" - needs human review
        await update_requirement_status(
            requirement.id,
            status='review_needed',
            notes=f'Failed {MAX_REQUIREMENT_RETRIES} times. Manual review required.'
        )
        return

    try:
        await process_validation(requirement)
        await update_requirement_status(requirement.id, status='integrated')
    except Exception as e:
        await increment_retry_count(requirement.id)
        await update_requirement_status(
            requirement.id,
            status='pending',  # Return to queue for retry
            notes=f'Attempt {requirement.retry_count + 1} failed: {str(e)}'
        )
```

**Recovery Process:**
1. Agent starts up
2. LangGraph automatically checks for existing checkpoint via `thread_id`
3. If checkpoint found:
   - State is restored from PostgreSQL
   - Agent resumes from last successful super-step
4. If not found:
   - Start fresh with initial state

### 11.5 Graceful Shutdown

Agents should handle shutdown signals:

```python
import signal

class AgentRunner:
    def __init__(self):
        self.shutdown_requested = False
        signal.signal(signal.SIGTERM, self.handle_shutdown)
        signal.signal(signal.SIGINT, self.handle_shutdown)

    def handle_shutdown(self, signum, frame):
        """Handle shutdown request."""
        logger.info("Shutdown requested, finishing current task...")
        self.shutdown_requested = True

    async def run(self):
        """Main agent loop."""
        while not self.shutdown_requested:
            task = await self.get_next_task()
            if task:
                await self.process_task(task)
                self.save_checkpoint()
            else:
                await asyncio.sleep(30)  # Poll interval

        logger.info("Shutdown complete")
```

### 11.6 Time Budgets

For extremely long-running jobs, agents can be given time budgets:

```python
class TimeBudget:
    """
    Manages time allocation for long-running tasks.
    """

    def __init__(self, job_config: dict):
        self.max_time_per_candidate = job_config.get(
            "max_time_per_candidate",
            timedelta(hours=2)
        )
        self.max_total_time = job_config.get(
            "max_total_time",
            timedelta(days=7)
        )

    def check_budget(self, start_time: datetime) -> bool:
        """Check if we're within time budget."""
        elapsed = datetime.now() - start_time
        return elapsed < self.max_total_time

    def should_conclude_candidate(self, candidate_start: datetime) -> bool:
        """Check if we should wrap up current candidate."""
        elapsed = datetime.now() - candidate_start
        if elapsed > self.max_time_per_candidate:
            logger.warning("Time budget exceeded for candidate, concluding...")
            return True
        return False
```

---

## 12. Citation & Provenance System

### 12.1 Integration with Citation Engine

The Citation Engine is **already implemented** as a separate, installable Python package located in `citation_tool/`. It should be installed as a dependency, not copied into the codebase. This allows the Citation Engine to be developed separately since it's used in multiple projects.

**Installation in Graph-RAG:**
```bash
# Add to requirements.txt (local path during development)
-e ../citation_tool[full]

# Or from git (for production/team distribution)
citation-engine[full] @ git+https://github.com/youruser/citation-tool.git@main
```

**Configuration via Environment Variables:**

The Citation Engine is configured entirely through environment variables. This allows the same PostgreSQL database to be shared between the agents and the Citation Engine:

```bash
# Database configuration (shared with agents)
CITATION_DB_URL=postgresql://user:pass@localhost:5432/graphrag

# LLM configuration for verification
CITATION_LLM_URL=http://llm-server:8080/v1  # Same as agent LLM
CITATION_LLM_MODEL=120b-instruct

# Behavior
CITATION_REASONING_REQUIRED=low  # none, low, medium, high
```

**Note:** The Citation Engine's PostgreSQL schema (`sources` and `citations` tables) will be created in the same database as the agents' tables. This is by design to allow joins between requirements and their citations.

**Initialization:**
```python
from citation_engine import CitationEngine

# Both agents use multi-agent mode with shared PostgreSQL
# Connection string read from CITATION_DB_URL environment variable
engine = CitationEngine(mode="multi-agent")
```

**Usage in Creator Agent:**
```python
# When citing a document section
result = engine.cite_doc(
    claim="Companies must encrypt personal data at rest",
    source_id=gdpr_source.id,
    quote_context="Article 32(1) specifies security measures including...",
    verbatim_quote="pseudonymisation and encryption of personal data",
    locator={"article": "32", "paragraph": "1", "point": "a"},
    relevance_reasoning="Encryption is explicitly listed as a security measure",
    confidence="high"
)

if result.verification_status == "verified":
    # Citation verified, include in requirement
    requirement.citations.append(result.citation_id)
else:
    # Handle verification failure
    logger.warning(f"Citation failed: {result.verification_notes}")
```

**Usage in Validator Agent:**
```python
# When citing graph state as evidence
result = engine.cite_db(
    claim="The EncryptionService BusinessObject already implements AES-256",
    source_id=neo4j_source.id,
    quote_context="Query results show EncryptionService with algorithm='AES-256'",
    locator={
        "query": "MATCH (bo:BusinessObject {name: 'EncryptionService'}) RETURN bo",
        "result_description": "Node found with algorithm property set to AES-256"
    },
    relevance_reasoning="Direct evidence that requirement is fulfilled"
)
```

### 12.2 Citation Types Used

| Agent | Citation Type | Use Case |
|-------|---------------|----------|
| Creator | `cite_doc` | Original document references |
| Creator | `cite_web` | Research from web sources |
| Validator | `cite_db` | Graph query results |
| Both | `cite_custom` | Agent-generated analysis artifacts |

### 12.3 Verification Pipeline

All citations go through synchronous verification:

1. Agent calls `cite_*()` method
2. Citation Engine stores citation with `status='pending'`
3. Verification LLM checks:
   - Does the quote exist in the source?
   - Does the quote support the claim?
4. Status updated to `verified` or `failed`
5. Result returned to agent
6. Agent decides how to proceed

**On Verification Failure:**
- Agent can reformulate the claim
- Agent can find a different source
- Agent can flag requirement for human review

### 12.4 Provenance Chain

Every requirement should have a complete provenance chain:

```
Source Document (GDPR Article 32)
    │
    ├─[cite_001]─▶ Requirement Candidate
    │               "Companies must encrypt personal data"
    │
    ├─[cite_002]─▶ Research Finding (web search)
    │               "ENISA recommends AES-256 specifically"
    │
    └─[cite_003]─▶ Final Requirement
                    "System must encrypt personal data at rest using AES-256"
                    │
                    └─[cite_004]─▶ Validation Decision
                                    "EncryptionService already implements this"
                                    (graph query citation)
```

---

## 13. Neo4j Schema & Metamodel

### 13.1 Current FINIUS Metamodel

The existing metamodel as defined in `MetamodelValidator`:

**Node Labels:**
- `Requirement`
- `BusinessObject`
- `Message`

**Required Properties:**
- `Requirement.rid` - Unique requirement ID
- `BusinessObject.boid` - Unique business object ID
- `Message.mid` - Unique message ID

**Allowed Relationships:**
```
Requirement → Requirement:
  - REFINES
  - DEPENDS_ON
  - TRACES_TO

Requirement → BusinessObject:
  - RELATES_TO_OBJECT
  - IMPACTS_OBJECT

Requirement → Message:
  - RELATES_TO_MESSAGE
  - IMPACTS_MESSAGE

Message → BusinessObject:
  - USES_OBJECT
  - PRODUCES_OBJECT
```

### 13.2 Extended Metamodel for Fulfillment

To support the Validator Agent's fulfillment tracking, we extend:

**New Relationship Types:**
```
Requirement → BusinessObject:
  - FULFILLED_BY_OBJECT (requirement is implemented by this BO)
  - NOT_FULFILLED_BY_OBJECT (requirement is NOT implemented, gap exists)

Requirement → Message:
  - FULFILLED_BY_MESSAGE
  - NOT_FULFILLED_BY_MESSAGE

Requirement → Requirement:
  - SUPERSEDES (newer requirement replaces older)
```

**New Properties on Requirement:**
```
complianceStatus: 'open' | 'partial' | 'fulfilled'
gdprRelevant: boolean
sourceJobId: string       # Links to PostgreSQL job
createdByAgent: string    # 'creator' | 'manual' | 'import'
validatedAt: datetime
validationNotes: string
```

### 13.3 MetamodelValidator Updates

The `MetamodelValidator` class needs to be updated to recognize new relationship types:

```python
ALLOWED_RELATIONSHIPS = {
    # Existing
    ("Requirement", "REFINES", "Requirement"),
    ("Requirement", "DEPENDS_ON", "Requirement"),
    ("Requirement", "TRACES_TO", "Requirement"),
    ("Requirement", "RELATES_TO_OBJECT", "BusinessObject"),
    ("Requirement", "IMPACTS_OBJECT", "BusinessObject"),
    ("Requirement", "RELATES_TO_MESSAGE", "Message"),
    ("Requirement", "IMPACTS_MESSAGE", "Message"),
    ("Message", "USES_OBJECT", "BusinessObject"),
    ("Message", "PRODUCES_OBJECT", "BusinessObject"),

    # New for fulfillment tracking
    ("Requirement", "FULFILLED_BY_OBJECT", "BusinessObject"),
    ("Requirement", "NOT_FULFILLED_BY_OBJECT", "BusinessObject"),
    ("Requirement", "FULFILLED_BY_MESSAGE", "Message"),
    ("Requirement", "NOT_FULFILLED_BY_MESSAGE", "Message"),
    ("Requirement", "SUPERSEDES", "Requirement"),
}
```

**Fulfillment Relationship Properties:**

The fulfillment relationships should include properties to capture evidence and confidence:

```cypher
// Example: Creating a FULFILLED_BY_OBJECT relationship
MATCH (r:Requirement {rid: $rid}), (bo:BusinessObject {boid: $boid})
CREATE (r)-[rel:FULFILLED_BY_OBJECT {
    confidence: $confidence,          // 0.0-1.0
    evidence: $evidence,              // Text description of how it's fulfilled
    citationId: $citationId,          // Link to Citation Engine citation
    validatedAt: datetime(),
    validatedByAgent: 'validator'
}]->(bo)

// Example: Creating a NOT_FULFILLED_BY_OBJECT relationship (gap)
MATCH (r:Requirement {rid: $rid}), (bo:BusinessObject {boid: $boid})
CREATE (r)-[rel:NOT_FULFILLED_BY_OBJECT {
    gapDescription: $gapDescription,  // What's missing
    severity: $severity,              // 'critical', 'major', 'minor'
    remediation: $remediation,        // Suggested fix
    citationId: $citationId,
    validatedAt: datetime(),
    validatedByAgent: 'validator'
}]->(bo)
```

### 13.4 Extended Metamodel CQL

The following CQL extends the existing `data/metamodell.cql` with fulfillment tracking:

```cypher
////////////////////////////////////////////////////////////////////////
// EXTENDED METAMODEL - Fulfillment Tracking Relationships
////////////////////////////////////////////////////////////////////////

// ── Requirement → BusinessObject (Fulfillment) ─────────────────────────────
// r FULFILLED_BY_OBJECT b    → Business object implements/satisfies requirement
// r NOT_FULFILLED_BY_OBJECT b → Gap exists; BO does not implement requirement

// ── Requirement → Message (Fulfillment) ────────────────────────────────────
// r FULFILLED_BY_MESSAGE m    → Message flow satisfies requirement
// r NOT_FULFILLED_BY_MESSAGE m → Gap exists; message does not satisfy requirement

// ── Requirement → Requirement (Versioning) ─────────────────────────────────
// r1 SUPERSEDES r2 → r1 is a newer version that replaces r2

////////////////////////////////////////////////////////////////////////
// EXTENDED QUALITY GATES
////////////////////////////////////////////////////////////////////////

// Q4: Requirements with fulfillment relationships - compliance overview
// MATCH (r:Requirement)
// OPTIONAL MATCH (r)-[:FULFILLED_BY_OBJECT]->(fb:BusinessObject)
// OPTIONAL MATCH (r)-[:NOT_FULFILLED_BY_OBJECT]->(nfb:BusinessObject)
// RETURN r.rid, r.name, r.complianceStatus,
//        collect(DISTINCT fb.name) AS fulfilled_by,
//        collect(DISTINCT nfb.name) AS gaps
// ORDER BY r.complianceStatus;

// Q5: Gap analysis - all NOT_FULFILLED relationships
// MATCH (r:Requirement)-[rel:NOT_FULFILLED_BY_OBJECT|NOT_FULFILLED_BY_MESSAGE]->(target)
// RETURN r.rid AS requirement,
//        type(rel) AS gap_type,
//        CASE WHEN target:BusinessObject THEN target.boid ELSE target.mid END AS target_id,
//        rel.gapDescription AS description,
//        rel.severity AS severity
// ORDER BY rel.severity DESC;

////////////////////////////////////////////////////////////////////////
// MERGE TEMPLATES - Fulfillment Relationships
////////////////////////////////////////////////////////////////////////

// Create FULFILLED_BY_OBJECT relationship
// MATCH (r:Requirement {rid:$rid}), (b:BusinessObject {boid:$boid})
// MERGE (r)-[rel:FULFILLED_BY_OBJECT]->(b)
//   ON CREATE SET rel.confidence=$confidence,
//                 rel.evidence=$evidence,
//                 rel.citationId=$citationId,
//                 rel.validatedAt=datetime(),
//                 rel.validatedByAgent=$agent;

// Create NOT_FULFILLED_BY_OBJECT relationship
// MATCH (r:Requirement {rid:$rid}), (b:BusinessObject {boid:$boid})
// MERGE (r)-[rel:NOT_FULFILLED_BY_OBJECT]->(b)
//   ON CREATE SET rel.gapDescription=$gapDescription,
//                 rel.severity=$severity,
//                 rel.remediation=$remediation,
//                 rel.citationId=$citationId,
//                 rel.validatedAt=datetime(),
//                 rel.validatedByAgent=$agent;
```

### 13.5 Graph Integrity Rules

The Validator Agent must ensure:

1. **No Contradictory Relationships:**
   - A requirement cannot have both FULFILLED_BY_OBJECT and NOT_FULFILLED_BY_OBJECT to the same BusinessObject

2. **Fulfillment Consistency:**
   - If a requirement has only FULFILLED_BY relationships, set `complianceStatus = 'fulfilled'`
   - If a requirement has both, set `complianceStatus = 'partial'`
   - If a requirement has only NOT_FULFILLED_BY or no fulfillment relationships, set `complianceStatus = 'open'`

3. **Source Traceability:**
   - Every agent-created requirement must have `sourceJobId` set
   - Every validation must update `validatedAt`

---

## 14. Deployment Architecture

### 14.1 Container Communication via FastAPI

Each container exposes a FastAPI application for health checks and administrative operations. The agents run on a k3s cluster and communicate via internal service URLs.

**Agent API Endpoints:**

```python
# Each agent container exposes:

# Health check (for Kubernetes liveness/readiness probes)
GET /health
# Returns: {"status": "healthy", "agent": "creator", "uptime_seconds": 12345}

# Readiness check (confirms database connections are established)
GET /ready
# Returns: {"ready": true, "database": "connected", "neo4j": "connected"}

# Agent status
GET /status
# Returns: {
#   "agent": "creator",
#   "current_job_id": "uuid or null",
#   "state": "idle|processing|error",
#   "last_activity": "2026-01-02T10:30:00Z",
#   "metrics": {
#     "jobs_processed": 5,
#     "requirements_created": 127,
#     "uptime_hours": 48.5
#   }
# }

# Graceful shutdown trigger
POST /shutdown
# Finishes current task, then exits cleanly
```

**Kubernetes Health Check Configuration:**

```yaml
# Example deployment spec for creator-agent
apiVersion: apps/v1
kind: Deployment
metadata:
  name: creator-agent
spec:
  template:
    spec:
      containers:
      - name: creator-agent
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 60
          timeoutSeconds: 10
          failureThreshold: 3
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 30
          timeoutSeconds: 5
          failureThreshold: 2
```

**Note:** The k3s deployment and Kubernetes manifests are handled separately by DevOps. This project focuses on creating proper Docker images with FastAPI health endpoints that can be consumed by Kubernetes.

### 14.2 Container Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Docker Compose Environment                        │
│                                                                             │
│  ┌──────────────────────┐  ┌──────────────────────┐  ┌───────────────────┐  │
│  │   creator-agent      │  │   validator-agent    │  │   orchestrator    │  │
│  │   (Container)        │  │   (Container)        │  │   (Container)     │  │
│  │                      │  │                      │  │                   │  │
│  │   Python + LangGraph │  │   Python + LangGraph │  │   Python + FastAPI│  │
│  │   Volume: /workspace │  │   Volume: /workspace │  │                   │  │
│  └──────────┬───────────┘  └──────────┬───────────┘  └─────────┬─────────┘  │
│             │                         │                        │            │
│             └──────────────┬──────────┴────────────────────────┘            │
│                            │                                                │
│  ┌─────────────────────────┼─────────────────────────────────────────────┐  │
│  │         Shared Network (internal)                                     │  │
│  └─────────────────────────┼─────────────────────────────────────────────┘  │
│                            │                                                │
│  ┌─────────────────────────┼─────────────────────────────────────────────┐  │
│  │                         │                                             │  │
│  │  ┌──────────────────┐   │   ┌──────────────────┐                      │  │
│  │  │   PostgreSQL     │◀──┴──▶│      Neo4j       │                      │  │
│  │  │   (Database)     │       │   (Graph DB)     │                      │  │
│  │  │                  │       │                  │                      │  │
│  │  │   Port: 5432     │       │   Port: 7687     │                      │  │
│  │  │   Volume: pgdata │       │   Volume: neo4j  │                      │  │
│  │  └──────────────────┘       └──────────────────┘                      │  │
│  │                                                                       │  │
│  │  ┌──────────────────┐   ┌──────────────────┐                          │  │
│  │  │   Redis          │   │   MongoDB        │  (Optional)              │  │
│  │  │   (Optional)     │   │   (Archive)      │                          │  │
│  │  │   Port: 6379     │   │   Port: 27017    │                          │  │
│  │  └──────────────────┘   └──────────────────┘                          │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        │ (External)
                                        ▼
                               ┌──────────────────┐
                               │   LLM Server     │
                               │   (vLLM/llama)   │
                               │                  │
                               │   120B Model     │
                               │   Home Lab       │
                               └──────────────────┘
```

### 14.2 Docker Compose Configuration

```yaml
version: '3.8'

services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: graphrag
      POSTGRES_USER: graphrag
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql
    ports:
      - "5432:5432"
    healthcheck:
      test: [ "CMD-SHELL", "pg_isready -U graphrag" ]
      interval: 10s
      timeout: 5s
      retries: 5

  neo4j:
    image: neo4j:5.15
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD}
      NEO4J_PLUGINS: '["apoc"]'
    volumes:
      - neo4jdata:/data
      - neo4jlogs:/logs
    ports:
      - "7474:7474"
      - "7687:7687"
    healthcheck:
      test: [ "CMD", "wget", "-O", "-", "http://localhost:7474" ]
      interval: 10s
      timeout: 5s
      retries: 5

  orchestrator:
    build:
      context: ..
      dockerfile: ../docker/Dockerfile.orchestrator
    environment:
      DATABASE_URL: postgresql://graphrag:${POSTGRES_PASSWORD}@postgres:5432/graphrag
      NEO4J_URI: bolt://neo4j:7687
      NEO4J_USERNAME: neo4j
      NEO4J_PASSWORD: ${NEO4J_PASSWORD}
      LLM_BASE_URL: ${LLM_BASE_URL}
      OPENAI_API_KEY: ${OPENAI_API_KEY}
    volumes:
      - ./data:/app/data
      - ./jobs:/app/jobs
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      neo4j:
        condition: service_healthy

  creator-agent:
    build:
      context: ..
      dockerfile: ../docker/Dockerfile.creator
    environment:
      DATABASE_URL: postgresql://graphrag:${POSTGRES_PASSWORD}@postgres:5432/graphrag
      LLM_BASE_URL: ${LLM_BASE_URL}
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      TAVILY_API_KEY: ${TAVILY_API_KEY}
    volumes:
      - ./data:/app/data
      - creator_workspace:/app/workspace
    depends_on:
      - postgres
      - orchestrator

  validator-agent:
    build:
      context: ..
      dockerfile: ../docker/Dockerfile.validator
    environment:
      DATABASE_URL: postgresql://graphrag:${POSTGRES_PASSWORD}@postgres:5432/graphrag
      NEO4J_URI: bolt://neo4j:7687
      NEO4J_USERNAME: neo4j
      NEO4J_PASSWORD: ${NEO4J_PASSWORD}
      LLM_BASE_URL: ${LLM_BASE_URL}
      OPENAI_API_KEY: ${OPENAI_API_KEY}
    volumes:
      - validator_workspace:/app/workspace
    depends_on:
      - postgres
      - neo4j
      - orchestrator

volumes:
  pgdata:
  neo4jdata:
  neo4jlogs:
  creator_workspace:
  validator_workspace:
```

### 14.3 Development vs. Production

**Development Mode:**
- Orchestrator runs as Python script (not containerized)
- Agents run as containers for isolation
- Direct access to LLM server

```bash
# Start databases
docker compose up -d postgres neo4j

# Run orchestrator locally
python start_orchestrator.py --prompt "..." --document-path ./data/gdpr.pdf

# Agents run as containers
docker compose up creator-agent validator-agent
```

**Production Mode:**
- All components containerized
- Container orchestration (Docker Compose or Kubernetes)
- Health monitoring and auto-restart

### 14.4 Resource Allocation

Given the home lab setup with a 120B model:

| Component | CPU | Memory | GPU |
|-----------|-----|--------|-----|
| PostgreSQL | 2 cores | 4GB | None |
| Neo4j | 2 cores | 8GB | None |
| Creator Agent | 2 cores | 4GB | None |
| Validator Agent | 2 cores | 4GB | None |
| Orchestrator | 1 core | 1GB | None |
| LLM Server | 8+ cores | 48GB+ | All GPUs |

---

## 15. Resource Tracking & Observability

### 15.1 Metrics to Track

**Per Job:**
- Total duration (start to completion)
- Creator phase duration
- Validator phase duration
- Requirements created/validated/integrated/rejected
- Total LLM requests
- Total tokens consumed (prompt + completion)
- Estimated cost (at current token prices)

**Per Agent:**
- Active time vs. idle time
- Requests per hour
- Average latency per request
- Error rate
- Context compactions performed

**Per Requirement:**
- Time from creation to validation
- Number of citations
- Research iterations
- Validation iterations

### 15.2 Logging Strategy

**Structured Logging:**
```python
import structlog

logger = structlog.get_logger()

logger.info(
    "requirement_created",
    job_id=job.id,
    candidate_id=candidate.id,
    confidence=0.85,
    citations_count=3,
    duration_seconds=1847,
)
```

**Log Levels:**
- DEBUG: Tool calls, intermediate results
- INFO: State transitions, completions
- WARNING: Retries, context compactions
- ERROR: Failures requiring attention

### 15.3 LLM Request Logging

Every LLM request is logged:

```python
class LLMLogger:
    def log_request(
        self,
        job_id: str,
        agent_id: str,
        request_type: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        success: bool,
        error_message: str = None,
        mongo_id: str = None  # If full request/response stored
    ):
        self.db.execute("""
            INSERT INTO llm_requests
            (job_id, agent_id, request_type, model, prompt_tokens,
             completion_tokens, total_tokens, latency_ms, success,
             error_message, mongo_request_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """, job_id, agent_id, request_type, model, prompt_tokens,
             completion_tokens, prompt_tokens + completion_tokens,
             latency_ms, success, error_message, mongo_id)
```

### 15.4 Dashboards (Future)

A simple monitoring dashboard showing:
- Active jobs and their status
- Agent activity (requests over time)
- Token consumption trends
- Error rates and types
- Requirement flow (created → validated → integrated)

---

## 16. Future Scaling Considerations

### 16.1 Multiple Agents

The architecture supports scaling to multiple agents:

**Horizontal Scaling:**
- Multiple Creator Agent containers
- Multiple Validator Agent containers
- Jobs distributed across Creator instances
- Requirements distributed across Validator instances (via SKIP LOCKED)

**Load Balancing:**
- Orchestrator assigns jobs round-robin to Creator instances
- Validators self-balance via database polling

### 16.2 Job Prioritization

Future enhancement for priority handling:

```sql
-- Add priority to jobs table
ALTER TABLE jobs ADD COLUMN priority INT DEFAULT 5;

-- Validators pick higher priority first
SELECT * FROM requirement_cache
WHERE status = 'pending'
ORDER BY (SELECT priority FROM jobs WHERE id = requirement_cache.job_id) DESC,
         created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

### 16.3 Scheduler

A future scheduler component could:
- Monitor overall system load
- Scale agents up/down based on queue depth
- Pause low-priority jobs when resources are scarce
- Resume jobs when resources become available

### 16.4 Agent Pool

A pool of generic agents that can be assigned roles:

```python
class AgentPool:
    """
    Manages a pool of agent containers that can be assigned to jobs.
    """

    def __init__(self, max_creators: int, max_validators: int):
        self.max_creators = max_creators
        self.max_validators = max_validators
        self.active_creators = []
        self.active_validators = []

    def assign_creator(self, job_id: str) -> str:
        """Assign an available creator to a job."""
        if len(self.active_creators) < self.max_creators:
            container_id = self.spawn_creator()
            self.active_creators.append((container_id, job_id))
            return container_id
        return None  # All creators busy

    def balance_agents(self):
        """
        Dynamically adjust creator/validator ratio based on queue state.
        """
        pending_creation = self.count_jobs(status='processing_creator')
        pending_validation = self.count_requirements(status='pending')

        # If validation queue is backing up, spawn more validators
        if pending_validation > pending_creation * 10:
            self.scale_validators(+1)
```

---

## 17. Appendices

### 17.1 Glossary

| Term | Definition |
|------|------------|
| **Creator Agent** | Agent responsible for extracting and formulating requirements from documents |
| **Validator Agent** | Agent responsible for validating requirements against the knowledge graph |
| **Orchestrator** | Main application that manages jobs and coordinates agents |
| **Requirement Cache** | PostgreSQL table serving as the shared queue between agents |
| **Citation** | A structured link between a claim and its supporting evidence |
| **Fulfillment** | Whether a requirement is implemented by existing business objects |
| **Metamodel** | The FINIUS schema defining valid graph structures |
| **Context Compaction** | Process of summarizing older context to free LLM token budget |
| **Checkpoint** | Saved agent state for recovery after failures |

### 17.2 Related Documents

| Document | Purpose |
|----------|---------|
| `my_vision.txt` | Original vision document with detailed goals |
| `multi_agent.md` | Current four-stage pipeline implementation |
| `CLAUDE.md` | Project context and commands |
| `citation_tool/README.md` | Citation Engine documentation (already implemented) |
| `citation_tool/docs/` | Full Citation Engine API reference and usage guides |
| `metamodel-compliance-architecture.md` | Metamodel validation design |

### 17.3 Decision Log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Two agents vs. one | Two agents | Clear separation of concerns, independent scaling |
| Communication method | Shared PostgreSQL (paper-stack pattern) | Decoupled, persistent, transactional, no MQ infrastructure needed |
| Agent deployment | Docker containers with FastAPI | Isolation, reproducibility, k8s-ready health checks |
| Context management | LangGraph pre_model_hook + SummarizationNode | Built-in, non-destructive, preserves full history for audit |
| Citation storage | PostgreSQL (shared with agents) | Multi-agent access, transactional verification, same DB |
| Citation Engine | Separate pip package via env vars | Reusable across projects, independent versioning |
| LLM endpoint | OpenAI-compatible | Supports local llama.cpp, vLLM |
| LLM priority | Out of scope (handled by LLM server) | Simplifies agent code, priority at infrastructure level |
| Fulfillment tracking | New relationship types with properties | Clear semantic meaning, graph-native, evidence capture |
| Error recovery | LangGraph RetryPolicy + circuit breakers | Built-in retry, custom escalation for poison pills |
| Checkpointing | LangGraph PostgresSaver | Automatic, resumable, machine-independent |
| Change tracking | Requirement tagging (not rollback) | Traceability and audit without complex rollback |

### 17.4 Open Questions for Future Consideration

1. **How to handle conflicting requirements?**
   - Two requirements that contradict each other
   - Requires human review or priority system

2. **How to handle requirement updates?**
   - Document is re-processed, some requirements changed
   - Versioning strategy needed

3. **How to train agents from feedback?**
   - Human corrections should improve future processing
   - Active learning integration

4. **Multi-document analysis?**
   - Cross-referencing requirements across documents
   - Detecting conflicts or overlaps

5. **UI for monitoring and intervention?**
   - Web dashboard for job status
   - Human review interface for flagged items

### 17.5 References

**LangGraph & Persistence:**
- [LangGraph Persistence Documentation](https://docs.langchain.com/oss/python/langgraph/persistence) - Official LangChain docs
- [LangGraph & Redis: Build Smarter AI Agents](https://redis.io/blog/langgraph-redis-build-smarter-ai-agents-with-memory-persistence/) - Redis
- [LangGraph Persistence with Couchbase](https://developer.couchbase.com/tutorial-langgraph-persistence-checkpoint/) - Couchbase
- [LangGraph State Machines: Managing Complex Agent Task Flows](https://dev.to/jamesli/langgraph-state-machines-managing-complex-agent-task-flows-in-production-36f4) - DEV Community
- [LangGraph 1.0 Release](https://blog.langchain.com/langchain-langgraph-1dot0/) - LangChain Blog

**Context Engineering:**
- [Context Engineering for Agents](https://blog.langchain.com/context-engineering-for-agents/) - LangChain Blog
- [LangGraph Message History Management](https://langchain-ai.github.io/langgraph/how-tos/create-react-agent-manage-message-history/) - Official How-To
- [Short-Term Memory and Summarization in LangGraph](https://deepwiki.com/esurovtsev/langgraph-intro/3.6.1-short-term-memory-and-summarization) - DeepWiki
- [GitHub: langchain-ai/context_engineering](https://github.com/langchain-ai/context_engineering) - Reference Implementation
- [Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) - Anthropic

**Error Handling & Recovery:**
- [Advanced Error Handling Strategies in LangGraph](https://sparkco.ai/blog/advanced-error-handling-strategies-in-langgraph-applications) - SparkCo
- [Error Handling and Retry Policies in LangGraph](https://deepwiki.com/langchain-ai/langgraph/3.7-error-handling-and-retry-policies) - DeepWiki
- [LangGraph Durable Execution](https://docs.langchain.com/oss/javascript/langgraph/durable-execution) - LangChain Docs
- [Enhanced State Management & Retries in LangGraph](https://changelog.langchain.com/announcements/enhanced-state-management-retries-in-langgraph-python) - LangGraph Changelog

**Multi-Agent Orchestration:**
- [Building LangGraph: Designing an Agent Runtime](https://blog.langchain.com/building-langgraph/) - LangChain Blog
- [LangGraph Multi-Agent Orchestration Guide 2025](https://latenode.com/blog/ai-frameworks-technical-infrastructure/langgraph-multi-agent-orchestration/) - Latenode
- [LangChain vs LangGraph 1.0 Comparison 2026](https://www.clickittech.com/ai/langchain-1-0-vs-langgraph-1-0/) - ClickIT

---

## Conclusion

This masterplan describes a comprehensive two-agent autonomous system for requirement extraction and validation. The key design decisions are:

1. **Separation into Creator and Validator agents** with asynchronous producer-consumer communication via PostgreSQL (paper-stack pattern)
2. **LangGraph-native long-running capability** using PostgresSaver checkpointing, pre_model_hook context management, and built-in retry policies
3. **Citation-first design** using the existing Citation Engine package, ensuring every claim is traceable to its source
4. **FastAPI-based container architecture** with Kubernetes-ready health check endpoints
5. **Quality over speed** philosophy allowing thorough research and validation
6. **Extensible architecture** supporting future scaling to multiple agents
7. **Requirement tagging for traceability** (not rollback) - linking graph changes to source requirements

The next steps are to:
1. Refactor the existing codebase to match the new directory structure
2. Implement the PostgreSQL schema for shared state
3. Build the Creator Agent based on the existing document ingestion pipeline
4. Build the Validator Agent based on the existing graph agent
5. Add Citation Engine as dependency (`pip install -e ../citation_tool[full]`)
6. Create Docker deployment configuration with FastAPI health endpoints
7. Implement the Orchestrator CLI
8. Update MetamodelValidator with new fulfillment relationship types

This document should serve as the reference for all implementation decisions going forward. The implementation roadmap will provide the detailed task breakdown, timelines, and file-level refactoring plan.

---

*Document Version: 1.1*
*Status: Design Complete - Ready for Implementation Roadmap*
*Last Updated: January 2026*
