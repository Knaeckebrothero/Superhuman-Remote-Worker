# Workspace-Centric Agent Architecture

## Executive Summary

This document proposes replacing the rigid phase-based state machine in the Creator Agent with a **workspace-centric autonomous architecture**. Instead of hardcoded phase transitions, the agent uses a filesystem-like workspace to manage its own context, write intermediate results, and self-direct through complex tasks.

This approach is validated by recent industry research showing that file-based memory outperforms specialized tools, and aligns with LangChain's "Deep Agents" architecture released in December 2025.

---

## Current Problem

### Issue: Rigid Phase-Based State Machine

The Creator Agent currently uses a 5-phase state machine:

```
preprocessing → identification → research → formulation → output
```

**Problems identified:**

1. **No Phase Transition Mechanism** (Critical)
   - System prompt loaded ONCE during initialization with phase="preprocessing"
   - Even if `state["current_phase"]` changes, the SystemMessage never updates
   - LLM sees "Phase: PREPROCESSING" for all 500 iterations
   - No tool exists for the agent to signal "phase complete"

2. **Context Accumulation**
   - All tool results stay in conversation history
   - Context grows until it exceeds limits or degrades performance
   - No mechanism to offload completed work

3. **Inflexible Workflow**
   - Phases are hardcoded in the graph structure
   - Agent cannot adapt to document complexity
   - No support for iterative refinement or backtracking

4. **Text-Based Completion Detection**
   - Relies on matching phrases like "all requirements have been extracted"
   - Fragile and model-dependent
   - No progress-based detection

### Root Cause

The architecture fights against how LLMs naturally work. LLMs are trained on:
- Reading and writing files
- Making plans and checking them off
- Iterative problem-solving with notes

Forcing them into a rigid state machine with invisible phase transitions creates confusion.

---

## Research Findings

### 1. File-Based Memory Outperforms Specialized Tools

**Source:** [Letta Benchmarks](https://www.letta.com/blog/benchmarking-ai-agent-memory)

> "A simple filesystem-based agent achieves **74.0%** on LoCoMo with GPT-4o mini, significantly above Mem0's reported 68.5% score for their graph variant."

**Why:** LLMs have extensive training data on filesystem operations. They're more proficient with familiar tools like `read_file`, `write_file`, `grep` than specialized memory APIs.

### 2. Deep Agents Architecture (LangChain, December 2025)

**Source:** [GitHub: langchain-ai/deepagents](https://github.com/langchain-ai/deepagents)

LangChain's new architecture uses three core components:

| Component | Purpose |
|-----------|---------|
| **TodoListMiddleware** | `write_todos`, `read_todos` for explicit planning |
| **FilesystemBackend** | Real disk operations for working memory |
| **SubAgentMiddleware** | Spawn specialized agents with isolated context |

> "Deep Agents explicitly plan, delegate, and remember, much like a human project manager."

### 3. Context Engineering Principles

**Source:** [Anthropic Engineering Blog](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

> "Context engineering is effectively the #1 job of engineers building AI agents."

Three key techniques:
1. **Compaction** - Summarize conversation when hitting limits
2. **Structured Note-Taking** - Write persistent notes outside context window
3. **Sub-agents** - Clean context windows, return condensed summaries

### 4. Plan-and-Execute Pattern

**Source:** [LangGraph Tutorial](https://langchain-ai.github.io/langgraph/tutorials/plan-and-execute/plan-and-execute/)

> "First come up with a multi-step plan, then go through that plan one item at a time. After accomplishing a task, revisit the plan and modify as appropriate."

Advantages:
- Explicit long-term planning (even strong LLMs struggle with implicit planning)
- Smaller models for execution, larger for planning
- Dynamic re-planning when circumstances change

### 5. Progressive Disclosure

**Source:** [Letta Blog](https://www.letta.com/blog/benchmarking-ai-agent-memory)

> "Letting agents navigate and retrieve data autonomously enables progressive disclosure—allows agents to incrementally discover relevant context through exploration."

Instead of loading everything upfront, agents discover context as needed, keeping working memory focused.

---

## Proposed Architecture

### Two-Tier Planning Model

The architecture uses a **hybrid approach** combining filesystem-based strategic planning with tool-based tactical execution. This is inspired by how Claude Code and LangChain Deep Agents handle planning:

| Tier | Purpose | Storage | Persistence | Use Case |
|------|---------|---------|-------------|----------|
| **Strategic (Filesystem)** | Long-term plans, phases, research | Markdown files in workspace | Permanent, human-readable | "What are we building and why?" |
| **Tactical (TodoManager)** | Short-term execution steps | In-memory with archive | Session-scoped, archivable | "What are the next 10-20 concrete steps?" |

**Why both?**
- **Filesystem** gives persistence, debuggability, and supports complex planning documents
- **TodoManager** provides attention management (reciting objectives keeps the agent focused) and natural checkpoint boundaries

**Workflow:**
1. Agent creates strategic plan on filesystem (`plans/feature_x.md`) with phases
2. Agent uses TodoManager to add 10-20 todos for current phase
3. Agent executes todos, checking them off
4. When phase complete: `archive_and_reset()` saves todos to `archive/`, clears the list
5. Context compaction happens at this natural boundary
6. Agent loads next phase, adds new todos, continues

### High-Level Design

```
┌─────────────────────────────────────────────────────────────────────┐
│                     UNIVERSAL AGENT (Unified Prompt)                │
│                                                                     │
│  "You are an autonomous agent. Your workspace contains              │
│   instructions.md with detailed guidance. Use your tools to read    │
│   it, make a plan, and execute step by step."                       │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                            TOOL GROUPS                              │
├──────────────────┬──────────────────┬──────────────────────────────┤
│  Todo Tools      │  Workspace Tools │  Domain Tools                │
│  (Tactical)      │  (Strategic)     │  (Task-specific)             │
│                  │                  │                              │
│  • add_todo      │  • read_file     │  • extract_document_text     │
│  • complete_todo │  • write_file    │  • chunk_document            │
│  • list_todos    │  • list_files    │  • web_search                │
│  • get_progress  │  • delete_file   │  • query_similar_requirements│
│  • archive_todos │  • search_files  │  • write_requirement_to_db   │
│                  │                  │  • execute_cypher_query      │
└──────────────────┴──────────────────┴──────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     WORKSPACE (Per-Job Storage)                     │
│                                                                     │
│  Storage Backend: PostgreSQL (JSONB) or Filesystem or Redis         │
│  Optional: pgvector for semantic search over workspace files        │
│                                                                     │
│  /job_<uuid>/                                                       │
│  │                                                                  │
│  ├── instructions.md          # Task-specific guidance (from template)│
│  │                                                                  │
│  ├── plans/                   # Strategic planning documents        │
│  │   ├── plan.md         # High-level plan with phases         │
│  │   └── research_notes.md    # Brainstorming, exploration          │
│  │                                                                  │
│  ├── archive/                 # Archived todo lists (auto-saved)    │
│  │   ├── todos_phase_1_<ts>.md  # Completed phase 1 todos          │
│  │   ├── todos_phase_2_<ts>.md  # Completed phase 2 todos          │
│  │   └── ...                                                        │
│  │                                                                  │
│  ├── tools/                   # Tool documentation (optional)       │
│  │   └── available_tools.md   # Tools the agent can discover        │
│  │                                                                  │
│  ├── documents/               # Input documents                     │
│  │   ├── source.pdf           # Primary source document             │
│  │   └── sources/             # Downloaded web pages, references    │
│  │       └── gobd_info.md                                           │
│  │                                                                  │
│  ├── notes/                   # Agent's working notes               │
│  │   ├── research.md          # Research findings                   │
│  │   └── decisions.md         # Key decisions and reasoning         │
│  │                                                                  │
│  ├── chunks/                  # Document chunks (Creator)           │
│  │   ├── chunk_001.md         # "## Article 3.1 - Retention..."     │
│  │   ├── chunk_002.md                                               │
│  │   └── manifest.json        # Chunk metadata                      │
│  │                                                                  │
│  ├── candidates/              # Requirement candidates (Creator)    │
│  │   ├── candidates.md        # List of all candidates found        │
│  │   └── candidates.json      # Structured candidate data           │
│  │                                                                  │
│  ├── requirements/            # Individual requirement workspaces   │
│  │   ├── req_001/                                                   │
│  │   │   ├── draft.md         # Requirement text + reasoning        │
│  │   │   ├── research.md      # Web/graph research findings         │
│  │   │   ├── citations.json   # Source citations                    │
│  │   │   └── final.json       # Finalized requirement data          │
│  │   └── req_002/                                                   │
│  │                                                                  │
│  └── output/                  # Final outputs                       │
│      ├── summary.md           # Human-readable summary              │
│      ├── requirements.json    # All requirements for DB insert      │
│      └── completion.json      # Job completion status               │
└─────────────────────────────────────────────────────────────────────┘
```

### Component Details

#### 1. Unified System Prompt

Instead of phase-specific prompts that never update, use a single prompt that points to readable instructions:

```
You are the Creator Agent, responsible for extracting requirements from documents.

## Your Workspace
You have a workspace at /job_{job_id}/ with tools to read and write files.
Start by reading `instructions.md` to understand your task.

## How to Work
1. Read instructions.md for detailed guidance
2. Create a plan using write_plan
3. Execute your plan step by step
4. Write findings to files as you go (this clears your context)
5. Mark tasks complete as you finish them
6. When done, write your final output to output/

## Important
- Write intermediate results to files to free up context
- Check your plan frequently with read_plan
- When stuck, re-read instructions.md for guidance
- You can revise your plan at any time
```

#### 2. Instructions Document

Merge all phase prompts into a single comprehensive document:

```markdown
# Requirement Extraction Instructions

## Overview
Your job is to extract well-formed, citation-backed requirements from documents
and prepare them for the Validator Agent.

## Step 1: Document Preprocessing
- Use `extract_document_text` to get document content
- Use `chunk_document` to split into manageable pieces
- Write chunks to `chunks/` folder
- Note document type and compliance relevance in `chunks/manifest.json`

## Step 2: Candidate Identification
For each chunk:
- Look for obligation language (must, shall, required)
- Look for GoBD indicators (Aufbewahrung, Nachvollziehbarkeit, ...)
- Look for GDPR indicators (personal data, consent, ...)
- Write candidates to `candidates/candidates.md`

[... continues with research, formulation, output steps ...]
```

#### 3. Todo Tools (Tactical Execution)

The TodoManager provides short-term task tracking with automatic archiving. Unlike filesystem-based plans, todos are designed for the current execution phase (10-20 concrete steps).

**Key concept:** Todos are like a "notepad page" - you write steps, execute them, then rip off the page (archive) and start fresh for the next phase.

```python
@tool
def add_todo(content: str, priority: int = 0) -> str:
    """Add a task to the current todo list.

    Args:
        content: Task description (concrete, actionable)
        priority: Higher = more important (default 0)

    Example:
        add_todo("Create read_file tool implementation")
        add_todo("Write unit tests for workspace tools", priority=1)
    """

@tool
def complete_todo(todo_id: str, notes: str = "") -> str:
    """Mark a todo as complete.

    Args:
        todo_id: The todo ID (e.g., "todo_1")
        notes: Optional completion notes
    """

@tool
def list_todos() -> str:
    """List all current todos with status.

    Returns:
        ○ [todo_1] Create read_file tool implementation
        ● [todo_2] Write unit tests for workspace tools  <- COMPLETED
        ◐ [todo_3] Add path validation  <- IN PROGRESS
    """

@tool
def get_progress() -> str:
    """Get progress summary for current todos.

    Returns:
        Progress: 5/12 (41.7% complete)
        In progress: 1, Pending: 6, Blocked: 0
    """

@tool
def archive_and_reset(phase_name: str = "") -> str:
    """Archive completed todos and reset for next phase.

    This tool:
    1. Saves current todos to workspace/archive/todos_<phase>_<timestamp>.md
    2. Clears the todo list
    3. Returns confirmation prompting agent to add new todos

    Args:
        phase_name: Optional name for the archived phase (e.g., "phase_1")

    Use this when:
    - Completing a phase of work
    - Before context compaction
    - Transitioning to a different type of task

    Example:
        archive_and_reset("phase_1_workspace_tools")
        # Returns: "Archived 12 todos to archive/todos_phase_1_workspace_tools_20250108.md.
        #           Todo list cleared. Ready for new todos."
    """
```

**Archived todo format** (saved to `workspace/archive/todos_<phase>_<ts>.md`):

```markdown
# Archived Todos: phase_1_workspace_tools
Archived: 2025-01-08T14:30:00

## Completed (10)
- [x] Create WorkspaceManager class
- [x] Implement read_file tool
- [x] Implement write_file tool
...

## Was In Progress (1)
- [ ] Add path validation (stopped mid-task)

## Was Pending (1)
- [ ] Write integration tests

## Notes
- Decided to use pathlib for path handling
- Found edge case with symlinks - documented in notes/decisions.md
```

#### 4. Workspace Tools

```python
@tool
def read_file(path: str) -> str:
    """Read a file from the workspace.

    Path is relative to /job_{job_id}/
    Example: read_file("instructions.md")
    Example: read_file("requirements/req_001/draft.md")
    """

@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file in the workspace.

    Creates parent directories if needed.
    Example: write_file("candidates/candidates.md", "## Candidates\n...")
    """

@tool
def list_files(path: str = "") -> str:
    """List files in a workspace directory.

    Example: list_files("requirements/") -> "req_001/, req_002/, req_003/"
    """

@tool
def search_files(query: str, path: str = "") -> str:
    """Semantic search over workspace files.

    Uses vector embeddings to find relevant content.
    Example: search_files("GoBD retention period")
    """

@tool
def delete_file(path: str) -> str:
    """Delete a file or empty directory from workspace."""
```

#### 5. Storage Backend Options

| Backend | Pros | Cons | Best For |
|---------|------|------|----------|
| **PostgreSQL JSONB** | Already have PG, ACID, easy backup | Not a real filesystem | Simple deployment |
| **Filesystem** | Natural, easy debugging | Needs volume mounts | Development |
| **Redis** | Fast, TTL support | Volatile by default | High-throughput |
| **S3/MinIO** | Scalable, cheap | Latency | Large documents |
| **Hybrid** | Best of both worlds | Complexity | Production |

Recommendation: Start with **PostgreSQL JSONB** since we already have it, add **pgvector** for semantic search.

#### 6. Context Management

The workspace enables aggressive context compaction:

```python
# Before: Keep everything in messages
messages = [
    SystemMessage(...),          # 2K tokens
    HumanMessage("Process..."),  # 500 tokens
    AIMessage("I'll extract.."), # 1K tokens
    ToolMessage(chunk_1),        # 5K tokens  <- KEEPS GROWING
    AIMessage("Found 3..."),     # 2K tokens
    ToolMessage(chunk_2),        # 5K tokens  <- KEEPS GROWING
    # ... 100K+ tokens eventually
]

# After: Agent writes to files, context stays small
messages = [
    SystemMessage(...),                    # 2K tokens
    HumanMessage("Process..."),            # 500 tokens
    AIMessage("Reading instructions..."),  # 200 tokens
    ToolMessage("instructions.md read"),   # 100 tokens  <- SMALL
    AIMessage("Writing plan..."),          # 200 tokens
    ToolMessage("plan.json written"),      # 100 tokens  <- SMALL
    # ... stays under 10K tokens
]
```

When approaching context limits, the compaction hook summarizes:
> "You have processed chunks 1-15, identified 7 candidates, and completed research on candidates 1-3. Current task: Research candidate 4. Your workspace has all details."

---

## How This Solves Current Issues

| Issue | Current Problem | Workspace Solution |
|-------|----------------|-------------------|
| **No Phase Transition** | LLM stuck seeing "PREPROCESSING" forever | No phases - agent reads instructions and self-directs |
| **Context Explosion** | Tool results accumulate forever | Agent writes to files, results don't stay in context |
| **Rigid Workflow** | Hardcoded phase transitions | Agent creates and revises its own plan |
| **Text-Based Completion** | Fragile phrase matching | Agent marks tasks complete, checks plan progress |
| **No Backtracking** | Can't revisit earlier work | Agent can read any file from workspace |
| **Lost Work on Crash** | Context lost on restart | Workspace persists, agent can resume |

---

## Workflow Example

### Strategic Planning Phase (Filesystem)

The agent first creates a strategic plan on the filesystem. This follows the pattern we use ourselves:

```
1. Agent reads instructions.md - understands the task
2. Agent creates plan.md with:
   - Problem statement
   - Brainstormed approaches
   - Chosen approach with reasoning
   - High-level implementation phases
3. Agent refines the plan until satisfied
```

**Example `plan.md`:**

```markdown
# Requirement Extraction Plan

## Problem
Extract GoBD compliance requirements from uploaded document.

## Approaches Considered
1. Sequential chunk processing - simple but may miss cross-references
2. Two-pass approach - first identify, then deep-dive
3. Hierarchical extraction - group by compliance domain

## Chosen Approach
Two-pass approach because:
- Allows building a complete picture before detailed extraction
- Better handles requirements that span multiple sections

## Phases
1. **Document Processing** - Extract text, chunk, identify structure
2. **Candidate Identification** - First pass to find all potential requirements
3. **Research & Formulation** - Deep-dive each candidate, add citations
4. **Output Generation** - Format and write to requirement_cache

## Current Status
- [x] Phase 1: Document Processing (completed 2025-01-08)
- [ ] Phase 2: Candidate Identification (in progress)
- [ ] Phase 3: Research & Formulation
- [ ] Phase 4: Output Generation
```

### Tactical Execution Phase (TodoManager)

For each phase, the agent uses TodoManager for concrete steps:

```
4. Agent reads current phase from plan.md
5. Agent calls add_todo() for each step in current phase:
   - add_todo("Extract text from document")
   - add_todo("Chunk document into sections")
   - add_todo("Write chunks to chunks/ folder")
   - add_todo("Create manifest.json with metadata")
6. Agent executes todos one by one:
   - Calls extract_document_text(...)
   - Calls complete_todo("todo_1", "Extracted 45 pages")
   - Calls write_file("chunks/chunk_001.md", ...)
   - Calls complete_todo("todo_2")
   - ...
7. When all todos complete:
   - Calls archive_and_reset("phase_1_document_processing")
   - Updates plan.md to mark phase complete
   - [Context compaction happens at this natural boundary]
8. Agent loads next phase, adds new todos, continues
```

### Complete Workflow

```
┌─────────────────────────────────────────────────────────────────────┐
│  STRATEGIC PLANNING (Filesystem)                                    │
│                                                                     │
│  1. read_file("instructions.md")                                    │
│  2. write_file("plan.md", brainstormed_plan)            │
│  3. Refine plan until satisfied                                     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PHASE 1 EXECUTION (TodoManager)                                    │
│                                                                     │
│  4. add_todo("Extract text") → add_todo("Chunk") → ...             │
│  5. Execute todos, complete_todo() after each                       │
│  6. archive_and_reset("phase_1")                                   │
│  7. Update plan.md: Phase 1 ✓                           │
│  8. [Context compaction]                                            │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PHASE 2 EXECUTION (TodoManager)                                    │
│                                                                     │
│  9. read_file("plan.md") - see Phase 2 is next          │
│  10. add_todo("Scan chunk 1 for requirements") → ...               │
│  11. Execute todos, complete_todo() after each                      │
│  12. archive_and_reset("phase_2")                                  │
│  13. [Context compaction]                                           │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                            ...
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  COMPLETION                                                         │
│                                                                     │
│  N. All phases complete                                             │
│  N+1. write_file("output/requirements.json", final_output)         │
│  N+2. write_file("output/completion.json", status)                 │
│  N+3. Signal job completion                                         │
└─────────────────────────────────────────────────────────────────────┘
```

### Human's Perspective (Debugging)

```bash
# Watch agent's strategic plan
$ cat /workspace/job_abc123/plan.md

# See current todos (if agent is mid-phase)
$ # TodoManager is in-memory, but you can watch agent's tool calls

# See archived todos from completed phases
$ ls /workspace/job_abc123/archive/
todos_phase_1_document_processing_20250108_143000.md
todos_phase_2_candidate_identification_20250108_150000.md

$ cat /workspace/job_abc123/archive/todos_phase_1_*.md

# Check agent's working notes
$ cat /workspace/job_abc123/research.md

# Resume after crash - workspace has all context
$ python run_agent.py --config creator.json --job-id abc123 --resume
```

---

## Integration with Existing System

### What Changes

| Component | Current | New |
|-----------|---------|-----|
| `CreatorAgentState` | Phase tracking, candidate lists | Simplified: job_id, messages, workspace_path |
| System prompt | Phase-specific, loaded once | Single prompt, points to instructions.md |
| Tools | Domain-only | + workspace tools + planning tools |
| Graph structure | 5 nodes with conditional edges | Simpler: process → tools → check → loop |
| Context management | Accumulate everything | Aggressive compaction, workspace persistence |

### What Stays

| Component | Status |
|-----------|--------|
| LangGraph framework | Keep |
| PostgreSQL checkpointing | Keep |
| Domain tools (extract, chunk, search, etc.) | Keep |
| Validator Agent integration | Keep (reads from requirement_cache) |
| Neo4j integration | Keep |

### Migration Path

1. **Phase 1: Add Workspace Infrastructure**
   - Implement workspace storage (PostgreSQL JSONB initially)
   - Add workspace tools (read_file, write_file, list_files)
   - Add planning tools (write_plan, read_plan, complete_task)

2. **Phase 2: Create Unified Instructions**
   - Merge phase prompts into instructions.md
   - Create new simplified system prompt
   - Test with current graph structure

3. **Phase 3: Simplify Graph**
   - Remove phase-based routing
   - Let agent self-direct via plan
   - Add progress-based completion detection

4. **Phase 4: Add Vector Search**
   - Enable pgvector extension
   - Implement search_files with embeddings
   - Allow agent to search past work

5. **Phase 5: Optimize Context Management**
   - Tune compaction thresholds
   - Add workspace-aware summarization
   - Benchmark token usage

---

## Open Questions for Discussion

### 1. Storage Backend Choice ✅ DECIDED

**Decision:** Simple local filesystem folder.

- **Container mode:** Files stored at `/workspace/` in container filesystem (ephemeral unless a persistent volume is mounted)
- **Dev mode (Python script):** Files stored at `./workspace/` in repository root
- **Production (future):** Mount S3/MinIO/Garage via FUSE (s3fs, rclone, goofys) to `/workspace/` - no code changes needed

**Why:**
- LLMs are extensively trained on filesystem operations
- Easy debugging (`ls`, `cat` files directly)
- S3 integration is a deployment concern, not a code concern - just mount it later
- Garage/MinIO confirmed compatible with FUSE mounts for our use case (simple read/write, no locking)

### 2. Workspace Scope ✅ DECIDED

**Decision:** No shared workspaces. Each agent has its own isolated workspace.

- Each agent (Creator, Validator) has its own workspace on the container filesystem
- Workspaces are isolated per-job - no cross-job references
- Validator does NOT have access to Creator's workspace (communicates via `requirement_cache` table)
- Retention: Handled by container lifecycle (ephemeral) or volume mount persistence

### 3. Planning Granularity ✅ DECIDED

**Decision:** Let the agent decide.

- Agent determines appropriate planning depth based on task complexity
- No enforced sub-plan structure
- Can revisit and adjust if output quality is insufficient after testing

### 4. Context Compaction Strategy ✅ DECIDED

**Decision:** Basic approach for now - auto summarization at 80k tokens.

- Use LangGraph's built-in summarization at ~80,000 token threshold
- Workspace files kept as-is (not summarized)
- Vector search over filesystem can be added later for searching notes
- Revisit after implementation when we can test features in isolation

### 5. Error Recovery ✅ DECIDED

**Decision:** 3 retries on failed tool calls, then stop workflow.

- Retry failed tool calls up to 3 times
- If still failing after retries, stop the workflow and report error
- No undo/versioning needed - agent is the only writer, no parallel write conflicts
- Partial writes not a concern for our use case

### 6. Vector Search Implementation ✅ DECIDED

**Decision:** Vectorize the filesystem (including documents stored there).

- Documents provided to agent are stored on the filesystem
- Filesystem contents are vectorized for semantic search
- Agent can query everything through vector search
- Specific vector DB choice (pgvector vs dedicated) deferred - implement what's simplest first

### 7. Subagent Architecture ✅ DECIDED

**Decision:** No subagents for now.

- Focus on getting the initial single-agent implementation working
- Subagent spawning can be considered as a future optimization

### 8. Compatibility with gpt-oss-120b ✅ DECIDED

**Decision:** Model is confirmed suitable.

- gpt-oss-120b is on par with OpenAI o3-mini, designed for agentic tasks
- Harmony format confirmed working: llama.cpp server handles format conversion
- Other projects using same setup work flawlessly with tool calls
- No need to test with different model first

---

## New Research: Tool Discovery & Context Management

### Tool Search / Lazy Loading Pattern

**Source:** [Anthropic: Advanced Tool Use](https://www.anthropic.com/engineering/advanced-tool-use), [Tool Search Tool Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)

The key breakthrough in Opus 4.5 was removing tool descriptions from the context window and letting the model discover tools on-demand:

> "Tool definitions can consume massive portions of your context window (50 tools ≈ 10-20K tokens). This represents an **85% reduction** in token usage while maintaining access to your full tool library."

**How it works:**
1. Mark tools with `defer_loading: true` - they're not loaded into context initially
2. Provide a "tool search tool" that the model can use to discover tools
3. Model searches for tools using regex or BM25 when needed
4. Only 3-5 relevant tools are loaded per search

**Results:**
- Opus 4: 49% → 74% accuracy
- Opus 4.5: 79.5% → 88.1% accuracy
- A five-server MCP setup went from ~55K tokens to ~8.7K tokens

**Application to our workspace:**

```python
# Instead of loading all tools upfront:
tools = [
    # Always available (small, frequently used)
    read_file,
    write_file,
    read_plan,
    complete_task,

    # Discoverable on-demand (defer_loading: true)
    {
        "name": "extract_document_text",
        "description": "Extract text from PDF/DOCX documents",
        "defer_loading": True,
        ...
    },
    {
        "name": "web_search",
        "description": "Search the web for compliance information",
        "defer_loading": True,
        ...
    },
    # ... other domain-specific tools
]
```

### Lazy Skills: Three-Tiered Progressive Disclosure

**Source:** [Lazy Skills](https://boliv.substack.com/p/lazy-skills-a-token-efficient-approach)

A complementary pattern for managing tool/skill complexity:

| Level | What's Loaded | Token Cost | When |
|-------|---------------|------------|------|
| **Level 1: Metadata** | Name + 1-line description | ~10-20 tokens/tool | Always in system prompt |
| **Level 2: Documentation** | Full usage instructions | ~200-2000 tokens/tool | When agent considers using |
| **Level 3: Executable** | Actual tool code | Variable | When agent invokes |

**Implementation idea:**

```yaml
# skills/extract_document.yaml
---
name: extract_document_text
description: Extract text from PDF/DOCX documents
type: executable
auto_load: false
---
# Full documentation loaded on-demand:
## Usage
Extract text from a document file. Supports PDF and DOCX formats.

## Parameters
- file_path (required): Path to the document
- pages (optional): Specific pages to extract (e.g., "1-5,10")

## Examples
extract_document_text(file_path="input/document.pdf")
extract_document_text(file_path="input/document.pdf", pages="1-3")
```

**Production results:** With 42 skills, only ~2.3 are loaded per conversation (~5.5% of library), yielding 97% token reduction at startup.

---

### Context Compaction Strategies

**Sources:** [Phil Schmid: Context Engineering Part 2](https://www.philschmid.de/context-engineering-part-2), [Factory.ai: Compressing Context](https://factory.ai/news/compressing-context), [Jason Liu: Context Engineering Compaction](https://jxnl.co/writing/2025/08/30/context-engineering-compaction/)

#### Compaction vs. Summarization

| Method | Type | Description | When to Use |
|--------|------|-------------|-------------|
| **Compaction** | Reversible | Strip redundant info that exists elsewhere | First choice |
| **Summarization** | Lossy | Use LLM to compress history | Only when compaction isn't enough |

**Priority order:** Raw → Compaction → Summarization

#### The Key Insight: File Paths, Not File Contents

> "If an agent writes a 500-line code file, the chat history should not contain the file content—only the file path (e.g., 'Output saved to /src/main.py')."

**This is exactly what our workspace enables:**

```python
# BAD: Keep full content in context
tool_result = ToolMessage(
    content="Extracted 500 lines of text:\n\nArticle 3.1 - Retention...[5000 tokens]..."
)

# GOOD: Write to file, return only confirmation
tool_result = ToolMessage(
    content="Extracted 500 lines. Written to chunks/chunk_001.md"
)
```

The agent can always retrieve the content later with `read_file("chunks/chunk_001.md")`.

#### Tool Call Message Filtering

**Source:** [LangGraph: Manage Conversation History](https://langchain-ai.github.io/langgraph/how-tos/create-react-agent-manage-message-history/)

LangGraph provides mechanisms to filter tool messages:

```python
from langchain_core.messages import ToolMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

def pre_model_hook(state):
    """Filter tool results before sending to LLM."""
    messages = state["messages"]

    filtered = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            # Option 1: Remove entirely
            continue
            # Option 2: Summarize if too long
            if len(msg.content) > 1000:
                msg = ToolMessage(
                    content=f"[Tool result: {len(msg.content)} chars - see workspace]",
                    tool_call_id=msg.tool_call_id
                )
        filtered.append(msg)

    return {"llm_input_messages": filtered}

# Use with create_react_agent
graph = create_react_agent(
    model,
    tools,
    pre_model_hook=pre_model_hook,  # <-- Filter before LLM
    checkpointer=checkpointer,
)
```

**Key patterns:**
- `RemoveMessage(id=msg_id)` - Remove specific message
- `RemoveMessage(id=REMOVE_ALL_MESSAGES)` - Clear all history
- `llm_input_messages` key - Send filtered messages to LLM while preserving full state

#### Sliding Window + Workspace

**Source:** [Google ADK: Context Compaction](https://google.github.io/adk-docs/context/compaction/)

Google's ADK uses a sliding window approach:

```python
EventsCompactionConfig(
    compaction_interval=3,  # Trigger every 3 completed events
    overlap_size=1,         # Keep 1 prior event for continuity
    summarizer=LlmEventSummarizer(llm=summarization_llm),
)
```

**How this integrates with workspace:**

1. Every N tool calls, trigger compaction
2. Recent tool calls (last 2-3) stay in raw format
3. Older tool calls get summarized: "Processed chunks 1-15, found 7 candidates"
4. Full details remain in workspace files for retrieval if needed

#### What to Preserve vs. Discard

**Preserve:**
- Open tasks and current plan state
- Decisions made and their reasoning
- Important facts discovered
- Unresolved issues or blockers
- Most recent tool calls (keep "rhythm" intact)

**Discard:**
- Intermediate file contents (agent can re-read)
- Confirmations like "File written successfully"
- Duplicate information
- Superseded drafts

#### Tool Result Clearing (Safest, Lowest Effort)

**Source:** [Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

> "Once a tool has been called deep in the message history, the agent doesn't need to see the raw result again" — called "one of the safest, lightest touch forms of compaction"

**Implementation:**

```python
def clear_old_tool_results(messages: list, keep_recent: int = 5) -> list:
    """Replace old ToolMessage content with placeholder."""
    result = []
    tool_msg_count = sum(1 for m in messages if isinstance(m, ToolMessage))
    seen_tools = 0

    for msg in messages:
        if isinstance(msg, ToolMessage):
            seen_tools += 1
            if seen_tools <= tool_msg_count - keep_recent:
                # Replace content with placeholder
                msg = ToolMessage(
                    content="[Result processed - see workspace if needed]",
                    tool_call_id=msg.tool_call_id
                )
        result.append(msg)
    return result
```

This is orthogonal to workspace - apply it even before workspace files exist.

#### LangMem: Ready-Made Summarization

**Source:** [LangMem Summarization Guide](https://langchain-ai.github.io/langmem/guides/summarization/)

LangChain provides a library specifically for this:

```python
from langmem import summarize_messages

# Automatic summarization when approaching limit
summarized = await summarize_messages(
    messages,
    max_tokens=80000,           # Trigger threshold
    max_summary_tokens=2000,    # Summary size limit
    running_summary=state.get("summary")  # Incremental updates
)
```

**Running Summary Pattern:**
- Store summary in agent state (not just messages)
- Update incrementally as conversation progresses
- OpenAI reports **84% token reduction** in 100-turn tests

#### Token Counting

For accurate threshold detection:

```python
import tiktoken

def count_tokens(messages: list, model: str = "gpt-4") -> int:
    """Count tokens in message list."""
    enc = tiktoken.encoding_for_model(model)
    total = 0
    for msg in messages:
        total += len(enc.encode(msg.content))
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            total += len(enc.encode(str(msg.tool_calls)))
    return total
```

**Threshold recommendations:**
- 128k context → trigger at ~80-100k tokens
- Leave headroom for response generation
- Consider tool definitions in count

#### Recursion Limit (Safety Net)

Even with perfect context management, add a hard limit:

```python
# In graph invocation
config = {"recursion_limit": 50}  # Or appropriate for task complexity
async for event in graph.astream_events(state, config=config):
    ...
```

This prevents infinite loops from bugs or adversarial inputs. Our Creator Agent hit 500 iterations - a lower limit would have failed faster and cleaner.

---

### Programmatic Tool Calling

**Source:** [Anthropic: Advanced Tool Use](https://www.anthropic.com/engineering/advanced-tool-use)

Another advanced pattern: Let the model write code that calls multiple tools, keeping intermediate results out of context:

> "Token savings: 37% reduction (43,588 to 27,297 tokens on complex tasks). Eliminates 19+ inference passes in multi-tool workflows."

**How it works:**
1. Model writes Python code that calls tools
2. Code executes in sandbox
3. Only final results return to context
4. Intermediate tool outputs never enter conversation

**Consideration:** This requires code execution capability. May be overkill for our use case, but worth noting for future optimization.

---

### Revised Architecture with New Insights

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     CREATOR AGENT (Unified Prompt)                       │
│                                                                         │
│  System Prompt: ~1K tokens (points to instructions.md)                  │
│  Core Tools: read_file, write_file, read_plan, complete_task (~500 tok) │
│  Tool Search Tool: Discovers domain tools on-demand (~500 tok)          │
│  Total initial context: ~2K tokens vs. ~20K+ with all tools             │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         TOOL LOADING STRATEGY                            │
│                                                                         │
│  ALWAYS LOADED (Core):              DISCOVERABLE (defer_loading: true): │
│  • read_file, write_file           • extract_document_text              │
│  • read_plan, complete_task        • chunk_document                     │
│  • list_files                      • web_search                         │
│  • tool_search                     • query_similar_requirements         │
│                                    • write_requirement_to_db            │
│                                    • ... (domain-specific tools)        │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      CONTEXT MANAGEMENT PIPELINE                         │
│                                                                         │
│  1. pre_model_hook:                                                     │
│     - Filter ToolMessages older than N turns                            │
│     - Replace large results with "see workspace" pointers               │
│     - Keep recent tool calls intact for "rhythm"                        │
│                                                                         │
│  2. Compaction trigger (every N tasks or at token threshold):           │
│     - Summarize older conversation with LLM                             │
│     - Preserve: plan state, decisions, current task                     │
│     - Discard: file contents, confirmations                             │
│                                                                         │
│  3. Workspace persistence:                                              │
│     - All intermediate results written to files                         │
│     - Agent retrieves on-demand with read_file                          │
│     - Full history available for recovery/debugging                     │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### Updated Open Questions

#### 9. Tool Discovery Implementation ✅ DECIDED

**Decision:** Use Anthropic's tool_search_tool approach if publicly available.

- Prefer Anthropic's implementation (regex/BM25) over custom vector search
- If not publicly disclosed, investigate and potentially implement similar mechanism
- **Note:** This is likely better than vector search for tool discovery

#### 10. Context Compaction Triggers ✅ DECIDED

**Decision:** Follow Anthropic's Opus 4.5 approach - cut out tool usage results.

- Remove tool call results from context after processing (like Anthropic does)
- Start with their proven approach, tweak later based on testing
- Keeps context focused on current work, not historical tool outputs

#### 11. Tool Result Handling ✅ DECIDED

**Decision:** Let the agent decide.

- Agent has autonomy over how to handle tool results
- Web search results can be stored in a dedicated folder (e.g., `web_sources/`)
- Can vectorize the sources table from citation tool for search
- Agent can use vector search over downloaded literature
- Quality of output is what matters, not the specific storage approach

#### 12. Compatibility with Local LLMs ✅ DECIDED

**Decision:** `defer_loading` is a server-side feature, not model-dependent.

- This feature is implemented in the inference server (Claude API, OpenAI API), not the model itself
- For our custom deployment, we need to implement tool discovery on the **server side**
- **TODO:** Implement tool search/lazy loading mechanism in our LangGraph agent infrastructure, not in the model

#### 13. Summarization LLM Choice ✅ DECIDED

**Decision:** Use same model with a custom summarization prompt.

- Keep it simple for initial implementation
- Create a dedicated summarization prompt
- Optimize with different prompts/models after system is running

#### 14. Recursion Limit ✅ DECIDED

**Decision:** No hard limit for now - use environment variable (currently 500-1000).

- Current env var setting: 500-1000 iterations
- Don't artificially limit the agent during development
- Work out appropriate limits after testing reveals actual behavior
- Can add task-type-specific limits later based on empirical data

#### 15. Implementation Phasing ✅ DECIDED

**Decision:** Sequential execution by single agent.

- One agent handles all phases sequentially
- No parallel execution or multiple agents
- Matches the current architecture (Creator Agent processes one job at a time)

---

## References

### Core Architecture
- [LangChain: Context Engineering for Agents](https://blog.langchain.com/context-engineering-for-agents/)
- [LangChain Deep Agents](https://github.com/langchain-ai/deepagents)
- [Anthropic: Effective Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Letta: Benchmarking AI Agent Memory](https://www.letta.com/blog/benchmarking-ai-agent-memory)
- [LangGraph Plan-and-Execute](https://langchain-ai.github.io/langgraph/tutorials/plan-and-execute/plan-and-execute/)

### Tool Discovery & Lazy Loading
- [Anthropic: Advanced Tool Use](https://www.anthropic.com/engineering/advanced-tool-use)
- [Anthropic: Tool Search Tool Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [Lazy Skills: Token-Efficient Agent Capabilities](https://boliv.substack.com/p/lazy-skills-a-token-efficient-approach)
- [Claude Code Issue #12836: Tool Search Support](https://github.com/anthropics/claude-code/issues/12836)

### Context Compaction & Summarization
- [Phil Schmid: Context Engineering Part 2](https://www.philschmid.de/context-engineering-part-2)
- [Factory.ai: Compressing Context](https://factory.ai/news/compressing-context)
- [Jason Liu: Context Engineering Compaction](https://jxnl.co/writing/2025/08/30/context-engineering-compaction/)
- [Google ADK: Context Compaction](https://google.github.io/adk-docs/context/compaction/)
- [LangMem Summarization Guide](https://langchain-ai.github.io/langmem/guides/summarization/)
- [OpenAI Agents SDK Session Memory](https://cookbook.openai.com/examples/agents_sdk/session_memory)

### LangGraph Message Management
- [LangGraph: Manage Conversation History](https://langchain-ai.github.io/langgraph/how-tos/create-react-agent-manage-message-history/)
- [LangGraph: Delete Messages](https://langchain-ai.github.io/langgraphjs/how-tos/delete-messages/)
- [LangGraph Discussion: Message Clearing](https://github.com/langchain-ai/langgraph/discussions/4433)

### Storage & State
- [LangGraph State Management 2025](https://sparkco.ai/blog/mastering-langgraph-state-management-in-2025)
- [Redis + LangGraph](https://redis.io/blog/langgraph-redis-build-smarter-ai-agents-with-memory-persistence/)
- [MongoDB Long-Term Agent Memory](https://www.mongodb.com/company/blog/product-release-announcements/powering-long-term-memory-for-agents-langgraph)

### Agent Architecture Best Practices
- [IBM: What Is AI Agent Memory](https://www.ibm.com/think/topics/ai-agent-memory)
- [Lindy: AI Agent Architecture Guide 2025](https://www.lindy.ai/blog/ai-agent-architecture)
- [ORQ: AI Agent Architecture Core Principles](https://orq.ai/blog/ai-agent-architecture)

---

## Implementation Roadmap

All 15 open questions have been decided. ✅

This roadmap breaks down the workspace-centric architecture into sequential implementation phases. Each phase builds on the previous one, ensuring we have a working system at each step before adding complexity.

### Architecture Decision: Universal Agent

**Key Insight:** Analysis of the current Creator and Validator agents reveals they share nearly identical structures:
- Same 5-node LangGraph pattern (initialize → process → tools → check → finalize)
- Same ContextManager, AsyncPostgresSaver, and shared infrastructure
- Same LLM initialization and tool binding patterns
- Same polling loop patterns

The only differences are:
- **Tools:** Document processing (Creator) vs graph validation (Validator)
- **Prompts:** Different instructions for different tasks
- **Connections:** Creator needs PostgreSQL; Validator needs PostgreSQL + Neo4j
- **Config:** Different sections in llm_config.json

**Decision:** Instead of refactoring the existing Creator Agent, we will build a new **Universal Agent** from scratch that:
1. Uses the workspace-centric architecture from the start
2. Is fully configurable via a JSON configuration file
3. Can be deployed as "Creator", "Validator", or any future agent type by changing the config
4. Eliminates code duplication between agents

Once the Universal Agent is working, we delete the old agents and migrate the application.

```
Current State:
  src/agents/creator/   (phase-based, ~1500 lines)
  src/agents/validator/ (phase-based, ~1600 lines)
  src/agents/shared/    (context, workspace, checkpoint)

Target State:
  src/agents/universal/ (workspace-based, configurable, ~800 lines)
  src/agents/shared/    (tools, context, workspace - enhanced)
  config/agents/creator.json
  config/agents/validator.json
```

---

### Phase 1: Workspace Foundation ✅ COMPLETE

**Goal:** Establish the basic workspace infrastructure that agents will use for persistent storage.

**What we'll do:**

- Define the workspace directory structure and conventions
- Create a `WorkspaceManager` class that handles workspace lifecycle (creation, cleanup, path resolution)
- Implement environment-aware path resolution (container vs. dev mode)
- Add workspace configuration to the existing config system
- Create the base folder structure that gets initialized for each new job

**Key decisions to implement:**

- Container mode uses `/workspace/job_{uuid}/`
- Dev mode uses `./workspace/job_{uuid}/`
- Each job gets an isolated workspace
- Standard subdirectories: `input/`, `chunks/`, `candidates/`, `requirements/`, `output/`

**Deliverable:** A working `WorkspaceManager` that can create, access, and clean up job workspaces.

**Implementation Notes (2025-01-08):**
- Created `src/agents/shared/workspace_manager.py` with `WorkspaceManager` and `WorkspaceConfig` classes
- Added `get_workspace_base_path()` for environment-aware path resolution
- Updated `src/core/config.py` with workspace configuration functions
- Updated `config/llm_config.json` with `workspace` section
- Created `tests/test_workspace_manager.py` with 40 unit tests (all passing)
- Exported new classes from `src/agents/shared/__init__.py`

---

### Phase 2: Workspace Tools ✅ COMPLETE

**Goal:** Implement the filesystem tools that the agent will use to interact with its workspace.

**What we'll do:**

- Implement `read_file` tool - read content from workspace files
- Implement `write_file` tool - write content to workspace files (creates parent dirs as needed)
- Implement `list_files` tool - list contents of workspace directories
- Implement `delete_file` tool - remove files or empty directories
- Implement `search_files` tool - basic text search over workspace files (regex/substring)
- Add path validation to prevent escaping the workspace (security)
- Create a tool registry system for dynamic tool loading
- Integrate tools with LangGraph's tool infrastructure

**Tool Registry Design:**

```python
# src/agents/shared/tools/registry.py
TOOL_REGISTRY = {
    # Workspace tools (strategic planning - filesystem operations)
    "read_file": {"module": "workspace_tools", "function": "read_file"},
    "write_file": {"module": "workspace_tools", "function": "write_file"},
    "list_files": {"module": "workspace_tools", "function": "list_files"},
    "delete_file": {"module": "workspace_tools", "function": "delete_file"},
    "search_files": {"module": "workspace_tools", "function": "search_files"},

    # Todo tools (tactical execution - in-memory with archive)
    "add_todo": {"module": "todo_tools", "function": "add_todo"},
    "complete_todo": {"module": "todo_tools", "function": "complete_todo"},
    "list_todos": {"module": "todo_tools", "function": "list_todos"},
    "get_progress": {"module": "todo_tools", "function": "get_progress"},
    "archive_and_reset": {"module": "todo_tools", "function": "archive_and_reset"},

    # Domain tools (loaded based on config)
    "extract_document_text": {"module": "document_tools", "function": "extract_document_text"},
    "chunk_document": {"module": "document_tools", "function": "chunk_document"},
    "web_search": {"module": "search_tools", "function": "web_search"},
    "execute_cypher_query": {"module": "graph_tools", "function": "execute_cypher_query"},
    ...
}

def load_tools(tool_names: list[str], context: ToolContext) -> list[Tool]:
    """Load tools by name from registry, injecting required context.

    Args:
        tool_names: List of tool names to load (from config)
        context: ToolContext with workspace_manager, todo_manager, db connections

    Returns:
        List of LangChain Tool objects ready to bind to LLM
    """
    ...
```

**Key decisions to implement:**

- All paths are relative to the job workspace root
- Tools return concise confirmations, not full file contents (for context efficiency)
- Path traversal attempts (e.g., `../`) are rejected
- Tool registry enables config-driven tool loading in Phase 5

**Deliverable:** Workspace tools and a tool registry system for dynamic tool loading.

**Implementation Notes (2025-01-08):**
- Created `src/agents/shared/tools/` package with:
  - `__init__.py` - Package exports
  - `context.py` - `ToolContext` class for dependency injection
  - `workspace_tools.py` - LangGraph tool wrappers using `@tool` decorator
  - `registry.py` - `TOOL_REGISTRY`, `load_tools()`, category-based tool loading
- Implemented 8 workspace tools: `read_file`, `write_file`, `append_file`, `list_files`, `delete_file`, `search_files`, `file_exists`, `get_workspace_summary`
- Tools return concise confirmations (not full file contents) for context efficiency
- Large files automatically truncated with `[TRUNCATED]` message
- Tool registry includes placeholder entries for todo tools (Phase 3) and domain tools (Phase 5)
- Updated `src/agents/shared/__init__.py` with new exports
- Created `tests/test_workspace_tools.py` with 40 unit tests (all passing)

---

### Phase 3: Todo Tools (Tactical Execution) ✅ COMPLETE

**Goal:** Implement the TodoManager with archiving for short-term task execution.

**What we'll do:**

- Refactor existing `todo_manager.py` to support the two-tier planning model
- Implement `add_todo` tool - add tasks to the current execution list
- Implement `complete_todo` tool - mark a task as done with optional notes
- Implement `list_todos` tool - display current todos with status
- Implement `get_progress` tool - summary view (X of Y complete)
- Implement `archive_and_reset` tool - save todos to workspace archive, clear list
- Connect TodoManager to WorkspaceManager for archive file storage

**Archive functionality:**

```python
async def archive_and_reset(self, phase_name: str = "") -> str:
    """Archive current todos and reset for next phase.

    1. Format todos as markdown (completed, in_progress, pending sections)
    2. Write to workspace/archive/todos_<phase>_<timestamp>.md
    3. Clear internal todo list
    4. Return confirmation message
    """
    # Generate archive content
    archive_content = self._format_archive()

    # Save to workspace
    filename = f"todos_{phase_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    await self.workspace.write_file(f"archive/{filename}", archive_content)

    # Clear todos
    self._todos = []
    self._next_id = 1

    return f"Archived {len(archived)} todos to archive/{filename}. Todo list cleared."
```

**Key decisions to implement:**

- Todos are session-scoped (not persisted to disk until archived)
- Archive files are human-readable markdown
- `archive_and_reset` is the natural checkpoint for context compaction
- Agent can read old archives from `workspace/archive/` if needed for reference

**Deliverable:** TodoManager with archive functionality that integrates with the workspace filesystem.

**Implementation Notes (2025-01-08):**
- Refactored `src/agents/shared/todo_manager.py` with:
  - Synchronous methods (`add_sync`, `complete_sync`, `start_sync`, `get_sync`, `next_sync`, `list_all_sync`, `get_progress_sync`)
  - `archive_and_reset()` method that writes todos as markdown to `workspace/archive/`
  - `_format_archive_content()` for human-readable markdown archive format
  - Support for both session-scoped mode (in-memory until archived) and legacy persistence mode
- Created `src/agents/shared/tools/todo_tools.py` with 7 LangGraph tool wrappers:
  - `add_todo`, `complete_todo`, `start_todo`, `list_todos`, `get_progress`, `archive_and_reset`, `get_next_todo`
- Updated `src/agents/shared/tools/registry.py` to load todo tools (removed placeholder markers)
- Updated `src/agents/shared/tools/__init__.py` and `src/agents/shared/__init__.py` exports
- Created `tests/test_todo_tools.py` with 41 unit tests (all passing)
- Total: 121 tests passing (40 Phase 1 + 40 Phase 2 + 41 Phase 3)

---

### Phase 4: Instructions & Agent Configuration ✅ COMPLETE

**Goal:** Create the configuration schema and instruction templates that define different agent behaviors.

**What we'll do:**

- Design the agent configuration JSON schema
- Create `creator.json` config with Creator-specific tools, prompts, and settings
- Create `validator.json` config with Validator-specific tools, prompts, and settings
- Analyze existing phase prompts from both agents
- Create `creator_instructions.md` - merged guidance for requirement extraction
- Create `validator_instructions.md` - merged guidance for validation and graph integration
- Create a minimal system prompt template that points the agent to its workspace
- Define the tool registry format (which tools each agent type gets)

**Agent Configuration Schema:**

```json
{
  "agent_id": "creator",
  "display_name": "Creator Agent",

  "llm": {
    "model": "gpt-oss-120b",
    "temperature": 0.0,
    "reasoning_level": "high",
    "base_url": null
  },

  "workspace": {
    "structure": [
      "archive/",
      "documents/",
      "documents/sources/",
      "chunks/",
      "candidates/",
      "requirements/",
      "output/",
      "tools/"
    ],
    "instructions_template": "creator_instructions.md"
  },

  "tools": {
    "workspace": ["read_file", "write_file", "list_files", "delete_file", "search_files"],
    "todo": ["add_todo", "complete_todo", "list_todos", "get_progress", "archive_and_reset"],
    "domain": ["extract_document_text", "chunk_document", "web_search", "cite_document", "write_requirement_to_cache"]
  },

  "todo": {
    "max_items": 25,
    "archive_on_reset": true,
    "archive_path": "archive/"
  },

  "connections": {
    "postgres": true,
    "neo4j": false
  },

  "polling": {
    "enabled": true,
    "table": "jobs",
    "status_field": "creator_status",
    "interval_seconds": 5
  },

  "limits": {
    "max_iterations": 500,
    "context_threshold_tokens": 80000,
    "tool_retry_count": 3
  },

  "context_management": {
    "compact_on_archive": true,
    "summarization_prompt": "Summarize the work completed so far, focusing on decisions made and current progress."
  }
}
```

**Key decisions to implement:**

- One config file per agent type in `config/agents/`
- Instructions are templates copied into each job workspace
- System prompt is generic - domain knowledge lives in instructions.md
- Tool registry maps tool names to implementations in shared tools package

**Deliverable:** Configuration schema, two agent configs (creator.json, validator.json), and instruction templates for both.

**Implementation Notes (2025-01-08):**
- Created `config/agents/` directory structure with `instructions/` subdirectory
- Created `config/agents/schema.json` - JSON Schema for agent configuration validation
- Created `config/agents/creator.json` with:
  - LLM configuration (model, temperature, reasoning_level)
  - Workspace structure for document processing workflow
  - Tools configuration: 8 workspace tools, 7 todo tools, 10 domain tools
  - Polling configuration for jobs table
  - Document processing settings (chunking, extraction mode, confidence threshold)
  - Research settings (web search, graph search)
- Created `config/agents/validator.json` with:
  - LLM configuration matching creator
  - Simplified workspace structure for validation workflow
  - Tools configuration: 8 workspace tools, 7 todo tools, 12 domain tools
  - Polling configuration for requirement_cache table with SKIP LOCKED
  - Validation settings (duplicate threshold, citations, metamodel validation)
- Created `config/agents/instructions/creator_instructions.md`:
  - Merged all 4 Creator phase prompts into comprehensive guidance
  - Documented all available tools by category
  - Detailed instructions for each phase (preprocessing, identification, research, formulation)
  - Planning template for strategic plans
- Created `config/agents/instructions/validator_instructions.md`:
  - Merged all 4 Validator phase prompts into comprehensive guidance
  - Documented all available tools by category
  - Detailed instructions for each phase (understanding, relevance, fulfillment, integration)
  - Cypher query examples and metamodel reference
- Created `config/agents/instructions/system_prompt.md`:
  - Minimal system prompt template with {agent_display_name} and {job_id} placeholders
  - Explains two-tier planning model (strategic filesystem + tactical todos)
  - Context management guidance

---

### Phase 5: Universal Agent Implementation [COMPLETE]

**Goal:** Build a new configurable agent from scratch using the workspace-centric architecture.

**What we'll do:**

- Create `src/agents/universal/` package structure
- Implement `UniversalAgent` class that loads behavior from config file
- Create simplified `UniversalAgentState` with minimal fields (messages, job_id, workspace_path, iteration, error, should_stop)
- Build a simple 4-node graph: initialize → process → tools → check (loops back or exits)
- Implement config-driven tool loading from the tool registry
- Implement config-driven connection setup (postgres always, neo4j optional)
- Create the agent entry point (`run_agent.py`) that accepts `--config` parameter
- Add FastAPI app for HTTP interface (like current agents)
- Implement polling loop that reads table/field from config

**Universal Agent Structure:**

```
src/agents/universal/
├── __init__.py
├── agent.py           # UniversalAgent class
├── state.py           # UniversalAgentState (minimal)
├── graph.py           # LangGraph construction
├── loader.py          # Config and tool loading
├── app.py             # FastAPI application
└── models.py          # Request/response models
```

**Shared Tools Package (enhanced):**

```
src/agents/shared/tools/
├── __init__.py
├── registry.py        # Tool registry and loader
├── workspace_tools.py # read_file, write_file, list_files, etc. (strategic)
├── todo_tools.py      # add_todo, complete_todo, archive_and_reset, etc. (tactical)
├── document_tools.py  # extract_document_text, chunk_document (from Creator)
├── graph_tools.py     # execute_cypher_query, create_requirement_node (from Validator)
├── search_tools.py    # web_search, query_similar_requirements
└── citation_tools.py  # cite_document, cite_web
```

The domain tools are migrated from the old agents:
- `src/agents/creator/tools.py` → `document_tools.py`, `search_tools.py`, `citation_tools.py`
- `src/agents/validator/tools.py` → `graph_tools.py`

The existing `todo_manager.py` is refactored and the tool wrappers moved to `todo_tools.py`.

**Key decisions to implement:**

- Agent uses two-tier planning: filesystem (strategic) + TodoManager (tactical)
- Strategic plans live in `workspace/plans/` as markdown files
- Tactical todos are in-memory, archived to `workspace/archive/` on phase completion
- `archive_and_reset()` is the natural boundary for context compaction
- Completion is detected when strategic plan shows all phases done
- Tools are loaded dynamically based on config `tools.workspace` + `tools.todo` + `tools.domain`
- Single container image works for any agent type

**Deliverable:** A working Universal Agent that can be configured as either Creator or Validator (or any future agent type).

**Implementation Notes (2025-01-08):**
- Created `src/agents/universal/` package with all required modules:
  - `state.py`: Minimal `UniversalAgentState` TypedDict with messages, job_id, workspace_path, iteration, error, should_stop, metadata fields. Uses LangGraph's `add_messages` annotation for automatic message deduplication
  - `loader.py`: Configuration loading with dataclasses (AgentConfig, LLMConfig, WorkspaceConfig, ToolsConfig, TodoConfig, ConnectionsConfig, PollingConfig, LimitsConfig, ContextManagementConfig). Includes `load_agent_config()`, `create_llm()`, `load_system_prompt()`, `load_instructions()`, `resolve_config_path()`
  - `graph.py`: 4-node LangGraph StateGraph (initialize → process → tools → check). Implements `build_agent_graph()` with tool routing, completion detection, and context management. Helper functions `_route_from_process`, `_route_from_check`, `_prepare_messages_for_llm`, `_detect_completion`
  - `agent.py`: `UniversalAgent` class with `from_config()` factory, `initialize()`, `process_job()`, `start_polling()`, `shutdown()`, and `get_status()` methods. Supports both sync and streaming job processing
  - `models.py`: Pydantic models for API (JobStatus, HealthStatus enums, JobSubmitRequest, JobCancelRequest, JobSubmitResponse, JobStatusResponse, HealthResponse, ReadyResponse, AgentStatusResponse, ErrorResponse, MetricsResponse)
  - `app.py`: FastAPI application with lifespan manager, health endpoints (/health, /ready, /status), job endpoints (POST/GET /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel), and metrics endpoint
  - `__init__.py`: Exports UniversalAgent, create_app, UniversalAgentState, and all models
- Migrated domain tools from Creator and Validator to `src/agents/shared/tools/`:
  - `document_tools.py`: extract_document_text, chunk_document, identify_requirement_candidates, assess_gobd_relevance, extract_entity_mentions (from Creator)
  - `search_tools.py`: web_search (Tavily), query_similar_requirements (Neo4j) (from Creator)
  - `citation_tools.py`: cite_document, cite_web (from Creator)
  - `cache_tools.py`: write_requirement_to_cache (Creator → Validator handoff)
  - `graph_tools.py`: 12 Neo4j tools (execute_cypher_query, get_database_schema, find_similar_requirements, check_for_duplicates, resolve_business_object, resolve_message, validate_schema_compliance, create_requirement_node, create_fulfillment_relationship, generate_requirement_id, get_entity_relationships, count_graph_statistics) (from Validator)
- Updated `src/agents/shared/tools/registry.py` to import and register all domain tools with `_load_domain_tools()` helper
- Updated `src/agents/shared/tools/context.py` to add `_job_id` field with property getter/setter for direct job_id override
- Created `run_universal_agent.py` entry point with argparse CLI:
  - API server mode (default): `python run_universal_agent.py --config creator --port 8001`
  - Single job mode: `python run_universal_agent.py --config creator --job-id abc123 --document-path ./doc.pdf`
  - Polling-only mode: `python run_universal_agent.py --config creator --polling-only`
  - Resume mode: `python run_universal_agent.py --config creator --job-id abc123 --resume`
- Created comprehensive unit tests in `tests/test_universal_agent.py` (56 tests covering state, loader, graph, models)
- All tests pass with the venv Python

---

### Phase 6: Context Management [COMPLETE]

**Goal:** Implement the context compaction and error handling mechanisms.

**What we'll do:**

- Implement tool result clearing in a `pre_model_hook`
- Add logic to filter old ToolMessage content (keep recent N, replace older with placeholder)
- Set up auto-summarization trigger at ~80k token threshold
- Create a summarization prompt that preserves critical context (plan state, decisions, current work)
- Implement 3-retry logic for failed tool calls
- Add graceful workflow termination when retries exhausted
- Integrate with LangGraph's message management utilities

**Key decisions to implement:**

- Recent tool results (last 5) stay intact, older ones get placeholder text
- Summarization uses the same model with a dedicated prompt
- Retry logic wraps tool execution, not individual tools
- Failed workflow writes error details to workspace before stopping

**Deliverable:** Context management that keeps the conversation lean while preserving agent effectiveness.

**Implementation Notes (2025-01-08):**
- Created `src/agents/universal/context.py` with comprehensive context management:
  - `ContextConfig`: Configuration dataclass with thresholds and limits
  - `ContextManagementState`: State tracking for compaction operations
  - `ContextManager`: Main class with multi-tier context management strategy:
    - `clear_old_tool_results()`: Replace old tool results with placeholders (safest compaction)
    - `truncate_long_tool_results()`: Truncate results exceeding max_length
    - `trim_messages()`: Keep recent messages, preserve first human message
    - `prepare_messages_for_llm()`: Apply all compaction strategies
    - `summarize_conversation()`: Async LLM-based summarization
    - `summarize_and_compact()`: Full summarization with message reconstruction
    - `create_pre_model_hook()`: LangGraph-compatible hook for automatic compaction
  - `ToolRetryManager`: Retry logic with exponential backoff and failure tracking
  - `write_error_to_workspace()`: Async function to persist error details
  - Token counting: tiktoken for accuracy with approximate fallback
- Created `config/agents/instructions/summarization_prompt.md`: Template for context summarization
- Updated `src/agents/universal/state.py`:
  - Added `context_stats` field for tracking compaction operations
  - Added `tool_retry_state` field for tracking retry attempts
- Updated `src/agents/universal/graph.py`:
  - Integrated `ContextManager` in process_node for automatic compaction
  - Integrated `ToolRetryManager` in tools_node with retry loop
  - Added token limit checking in check_node
  - Error persistence to workspace on non-recoverable failures
  - New function `run_graph_with_summarization()` for advanced summarization support
- Updated `src/agents/universal/loader.py`:
  - Added `load_summarization_prompt()` function
  - Added `get_all_tool_names()` helper function
- Updated `src/agents/universal/__init__.py` with all new exports
- Created `tests/test_context_management.py` with 38 comprehensive unit tests:
  - Tests for ContextConfig and ContextManagementState
  - Tests for token counting (tiktoken and approximate)
  - Tests for ContextManager methods (clearing, truncating, trimming, summarization)
  - Tests for ToolRetryManager (retry logic, backoff, failure tracking)
  - Tests for write_error_to_workspace
  - Integration tests for state fields and package exports
- All 202 tests pass (38 new + 164 existing)

---

### Phase 7: Tool Discovery (Optional Enhancement) [SKIPPED]

**Goal:** Implement server-side tool discovery to reduce initial context usage.

**What we'll do:**

- Research Anthropic's tool_search_tool implementation details
- Define which tools are "core" (always loaded) vs "discoverable"
- Implement a tool registry with metadata (name, description, category)
- Create a `search_tools` tool that finds relevant tools by keyword
- Modify tool loading to defer non-core tools until discovered
- Test impact on context size and agent performance

**Key decisions to implement:**

- Core tools: workspace tools, planning tools, search_tools
- Discoverable tools: domain-specific (document extraction, web search, graph queries)
- Tool search uses simple regex/BM25, not vector search
- This is server-side logic, not model-dependent

**Deliverable:** A tool discovery mechanism that reduces initial prompt size while maintaining full capability access.

**Status Note (2025-01-08):** Skipped as optional enhancement. The existing tool registry already supports categorized tool loading, which provides similar benefits. Can be revisited if context size becomes a bottleneck.

---

### Phase 8: Vector Search Integration [COMPLETE]

**Goal:** Enable semantic search over workspace contents for improved context retrieval.

**What we'll do:**

- Choose vector storage approach (pgvector extension vs. embedded solution)
- Implement automatic vectorization of workspace files when written
- Create embeddings for document chunks, notes, and agent-generated content
- Enhance `search_files` tool to support semantic queries
- Integrate with citation tool's sources table for literature search
- Add vector cleanup when workspace is deleted

**Key decisions to implement:**

- Start with simplest solution (likely pgvector since we already use PostgreSQL)
- Vectorize on write, not on read
- Agent can search both keyword (Phase 2) and semantic (this phase)
- Same embedding model for all content types initially

**Deliverable:** Semantic search capability over all workspace content.

**Implementation Notes (2025-01-08):**
- Added `pgvector>=0.2.0` and `langchain-postgres>=0.0.6` to requirements.txt
- Created `migrations/002_workspace_embeddings.sql`:
  - `workspace_embeddings` table with 1536-dimension vectors
  - HNSW index for fast approximate nearest neighbor search
  - Helper functions: `cleanup_job_embeddings()`, `search_workspace_similar()`
- Created `src/agents/shared/vector.py` with:
  - `VectorConfig`: Configuration for embedding model, chunking, search settings
  - `EmbeddingManager`: Text chunking and OpenAI embedding generation
  - `WorkspaceVectorStore`: PostgreSQL/pgvector storage with search, indexing, cleanup
  - `VectorizedWorkspaceManager`: Wrapper that auto-indexes files on write
  - `create_workspace_vector_store()`: Factory function for initialization
- Created `src/agents/shared/tools/vector_tools.py` with 3 LangGraph tools:
  - `semantic_search`: Natural language search over workspace files
  - `index_file_for_search`: Manual file indexing/re-indexing
  - `get_vector_index_stats`: Index statistics
- Updated `src/agents/shared/tools/context.py`:
  - Added `vector_store` field to `ToolContext`
  - Added `has_vector_store()` method
- Updated `src/agents/shared/tools/registry.py`:
  - Added vector tools to registry with category "workspace"
  - Separate loading path for vector tools (require vector_store)
- Updated package exports in `__init__.py` files
- Created `tests/test_vector_search.py` with 34 unit tests:
  - VectorConfig, EmbeddingManager, WorkspaceVectorStore tests
  - VectorizedWorkspaceManager tests
  - Vector tools and registry tests
  - Package export tests
- All 236 tests pass (34 new + 202 existing)

---

### Phase 9: Testing & Validation

**Goal:** Validate the Universal Agent works correctly as both Creator and Validator.

**What we'll do:**

- Test Universal Agent with `creator.json` config:
  - Run end-to-end document processing
  - Verify requirement extraction quality matches or exceeds old Creator
  - Test workspace file operations and planning tools
  - Measure token usage and iteration counts

- Test Universal Agent with `validator.json` config:
  - Run validation on requirements from cache
  - Verify graph integration works correctly
  - Test Neo4j connection handling
  - Compare validation quality with old Validator

- Cross-cutting tests:
  - Error recovery and resume-from-checkpoint
  - Context compaction under load
  - Polling loop behavior
  - Hot-swapping configs (for dashboard use case)

- Collect metrics:
  - Tokens used per job (compare old vs new)
  - Iterations per job
  - Time to completion
  - Error rates

**Key decisions to implement:**

- Test with actual GoBD/compliance documents
- Run both agent types against same test data for comparison
- Establish baseline metrics for ongoing monitoring
- Document any behavioral differences from old agents

**Deliverable:** Validated Universal Agent that matches or exceeds the quality of the old specialized agents.

---

### Phase 10: Migration & Cleanup

**Goal:** Replace the old Creator and Validator agents with the Universal Agent.

**What we'll do:**

- Update orchestrator to use Universal Agent with appropriate configs
- Update `run_creator.py` to launch Universal Agent with `creator.json`
- Update `run_validator.py` to launch Universal Agent with `validator.json`
- Update Docker configuration:
  - Single agent image instead of two
  - Config file mounted/passed as environment
- Update docker-compose.yml to use new structure
- Migrate any remaining code from old agents to shared tools
- Delete old agent code:
  - `src/agents/creator/` (entire directory)
  - `src/agents/validator/` (entire directory)
- Update imports throughout codebase
- Update CLAUDE.md and documentation
- Update Streamlit dashboard to use Universal Agent

**Migration checklist:**

```
[ ] Orchestrator updated to use Universal Agent
[ ] run_creator.py uses Universal Agent + creator.json
[ ] run_validator.py uses Universal Agent + validator.json
[ ] Docker images consolidated
[ ] docker-compose.yml updated
[ ] Old agent directories deleted
[ ] All imports updated
[ ] Documentation updated
[ ] Dashboard updated
[ ] CI/CD pipelines updated (if any)
```

**Key decisions to implement:**

- Keep `requirement_cache` interface unchanged for backwards compatibility
- Old scripts (run_creator.py, run_validator.py) become thin wrappers
- Dashboard can switch configs without container restart

**Deliverable:** Clean codebase with single Universal Agent, old code removed, all systems working.

**Implementation Notes (2025-01-10):**
- Deleted `run_creator.py` and `run_validator.py` (use `run_universal_agent.py --config creator/validator` instead)
- Created unified `docker/Dockerfile.agent` (replaces Dockerfile.creator and Dockerfile.validator)
- Updated `docker-compose.yml` and `docker-compose.dev.yml` to use unified Dockerfile with `AGENT_CONFIG` env var
- Removed `src/agents/creator/` and `src/agents/validator/` directories
- Updated `src/agents/shared/tools/search_tools.py` to remove dependency on old Researcher class
- Updated `src/agents/__init__.py` to export Universal Agent instead of old Creator/Validator
- Updated `CLAUDE.md` to remove references to deleted files and directories
- Legacy document ingestion pipeline agents retained in `src/agents/` for backwards compatibility

---

### Phase Summary

| Phase | Name | Dependencies | Complexity | Status |
|-------|------|--------------|------------|--------|
| 1 | Workspace Foundation | None | Low | ✅ Complete |
| 2 | Workspace Tools (Strategic) | Phase 1 | Medium | ✅ Complete |
| 3 | Todo Tools (Tactical) | Phase 1 | Medium | ✅ Complete |
| 4 | Instructions & Agent Configuration | None (parallel with 1-3) | Medium | ✅ Complete |
| 5 | Universal Agent Implementation | Phases 1-4 | High | ✅ Complete |
| 6 | Context Management | Phase 5 | Medium | ✅ Complete |
| 7 | Tool Discovery | Phase 5 | Medium (optional) | ⏭️ Skipped |
| 8 | Vector Search | Phases 2, 5 | Medium (optional) | ⏭️ Skipped |
| 9 | Testing & Validation | Phases 1-6 | Medium | ✅ In Progress |
| 10 | Migration & Cleanup | Phase 9 | Low | ✅ Complete |

**Two-Tier Planning:**
- Phase 2 (Workspace Tools) provides **strategic planning** - filesystem for plans, notes, research
- Phase 3 (Todo Tools) provides **tactical execution** - TodoManager with archive_and_reset()
- Together they enable the hybrid approach: strategic plans in markdown, tactical todos in tool

**Parallel work opportunities:**
- Phases 1, 2, 3 can be developed in sequence but tested incrementally
- Phase 4 (configs & instructions) can be done in parallel with Phases 1-3
- Phases 7 and 8 are optional enhancements - can be deferred or skipped entirely
- Phase 10 is straightforward once Phase 9 validates the Universal Agent works

**Critical path:** 1 → 2 → 3 → 5 → 6 → 9 → 10

**MVP path (minimum to replace old agents):** 1 → 2 → 3 → 4 → 5 → 9 → 10

---

### Risk Assessment

| Risk | Mitigation |
|------|------------|
| Universal Agent doesn't match old agent quality | Test extensively in Phase 9 before migration, keep old code until validated |
| Agent fails to follow instructions effectively | Test with smaller tasks first, iterate on instruction documents |
| Context still grows too large | More aggressive tool result clearing, earlier summarization trigger |
| Two-tier planning adds complexity | Keep clear separation: filesystem=strategic, todos=tactical |
| Agent doesn't use archive_and_reset properly | Add instructions, monitor behavior, tune prompts |
| Workspace I/O becomes bottleneck | Profile and optimize hot paths, consider caching |
| Vector search adds latency | Make semantic search optional, fallback to keyword |
| Config schema doesn't cover all use cases | Design schema carefully in Phase 4, allow extension points |
| Tool registry becomes complex | Start simple, add sophistication only when needed |
| Migration breaks orchestrator | Keep requirement_cache and job table interfaces unchanged |
| Dashboard integration issues | Test hot-swapping configs early in Phase 9 |
