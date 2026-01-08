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

### High-Level Design

```
┌─────────────────────────────────────────────────────────────────────┐
│                     CREATOR AGENT (Unified Prompt)                  │
│                                                                     │
│  "You are a requirement extraction agent. Your workspace contains   │
│   instructions.md with detailed guidance. Use your tools to read    │
│   it, make a plan, and execute step by step."                       │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                            TOOL GROUPS                              │
├──────────────────┬──────────────────┬──────────────────────────────┤
│  Planning Tools  │  Workspace Tools │  Domain Tools                │
│                  │                  │                              │
│  • write_plan    │  • read_file     │  • extract_document_text     │
│  • read_plan     │  • write_file    │  • chunk_document            │
│  • add_task      │  • list_files    │  • web_search                │
│  • complete_task │  • delete_file   │  • query_similar_requirements│
│  • get_progress  │  • search_files  │  • write_requirement_to_db   │
│                  │  • create_folder │                              │
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
│  ├── instructions.md          # How to extract requirements         │
│  ├── plan.json                # Current task list with status       │
│  ├── progress.md              # Running log of what's been done     │
│  │                                                                  │
│  ├── input/                                                         │
│  │   └── document.pdf         # Source document                     │
│  │                                                                  │
│  ├── chunks/                  # Document chunks                     │
│  │   ├── chunk_001.md         # "## Article 3.1 - Retention..."     │
│  │   ├── chunk_002.md                                               │
│  │   └── manifest.json        # Chunk metadata                      │
│  │                                                                  │
│  ├── candidates/              # Requirement candidates              │
│  │   ├── candidates.md        # List of all candidates found        │
│  │   └── candidates.json      # Structured candidate data           │
│  │                                                                  │
│  ├── requirements/            # Individual requirement workspaces   │
│  │   ├── req_001/                                                   │
│  │   │   ├── draft.md         # Requirement text + reasoning        │
│  │   │   ├── research.md      # Web/graph research findings         │
│  │   │   ├── citations.json   # Source citations                    │
│  │   │   ├── validation.md    # Self-review notes                   │
│  │   │   └── final.json       # Finalized requirement data          │
│  │   ├── req_002/                                                   │
│  │   └── ...                                                        │
│  │                                                                  │
│  └── output/                  # Final outputs                       │
│      ├── summary.md           # Human-readable summary              │
│      ├── requirements.json    # All requirements for DB insert      │
│      └── insert.cypher        # Neo4j queries (if needed)           │
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

#### 3. Planning Tools

```python
@tool
def write_plan(tasks: List[str]) -> str:
    """Create or replace the execution plan.

    Args:
        tasks: List of task descriptions in order

    Example:
        write_plan([
            "Extract and chunk the document",
            "Identify requirement candidates from each chunk",
            "Research candidate 1: GoBD retention requirement",
            "Formulate and validate requirement 1",
            "Write requirement 1 to database",
            ...
        ])
    """

@tool
def read_plan() -> str:
    """Read the current plan with task status.

    Returns plan as:
    1. [x] Extract and chunk the document
    2. [x] Identify requirement candidates from each chunk
    3. [ ] Research candidate 1: GoBD retention requirement  <- CURRENT
    4. [ ] Formulate and validate requirement 1
    ...
    """

@tool
def complete_task(task_number: int, notes: Optional[str] = None) -> str:
    """Mark a task as complete, optionally with notes."""

@tool
def add_task(task: str, after_task: Optional[int] = None) -> str:
    """Add a new task to the plan. Inserted after specified task or at end."""

@tool
def get_progress() -> str:
    """Get summary: X of Y tasks complete, current task, time elapsed."""
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

### Agent's Perspective

```
1. Agent starts, reads system prompt
2. Calls read_file("instructions.md") - learns what to do
3. Calls write_plan([...]) - creates 12-step plan
4. Calls extract_document_text("input/document.pdf")
5. Calls write_file("chunks/chunk_001.md", ...) - saves chunk
6. Calls complete_task(1, "Document extracted, 8 chunks created")
7. [Context compaction happens - old tool results summarized]
8. Calls read_plan() - sees task 2 is next
9. Calls read_file("chunks/chunk_001.md") - loads first chunk
10. Identifies candidates, writes to candidates/candidates.md
11. Calls complete_task(2)
12. ... continues through plan ...
13. Calls read_plan() - all tasks complete
14. Calls write_file("output/summary.md", final_summary)
15. Signals completion
```

### Human's Perspective (Debugging)

```bash
# Watch agent progress
$ watch cat /workspace/job_abc123/plan.json

# See what the agent found
$ cat /workspace/job_abc123/candidates/candidates.md

# Check a specific requirement's research
$ cat /workspace/job_abc123/requirements/req_001/research.md

# Resume after crash - all work is there
$ python run_creator.py --job-id abc123 --resume
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

---

### Phase 1: Workspace Foundation

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

---

### Phase 2: Workspace Tools

**Goal:** Implement the filesystem tools that the agent will use to interact with its workspace.

**What we'll do:**

- Implement `read_file` tool - read content from workspace files
- Implement `write_file` tool - write content to workspace files (creates parent dirs as needed)
- Implement `list_files` tool - list contents of workspace directories
- Implement `delete_file` tool - remove files or empty directories
- Implement `search_files` tool - basic text search over workspace files (regex/substring)
- Add path validation to prevent escaping the workspace (security)
- Integrate tools with LangGraph's tool infrastructure

**Key decisions to implement:**

- All paths are relative to the job workspace root
- Tools return concise confirmations, not full file contents (for context efficiency)
- Path traversal attempts (e.g., `../`) are rejected

**Deliverable:** A complete set of workspace tools that can be added to the agent's toolset.

---

### Phase 3: Planning Tools

**Goal:** Implement the planning and progress tracking tools that enable agent self-direction.

**What we'll do:**

- Define the plan data structure (JSON format in `plan.json`)
- Implement `write_plan` tool - create or replace the task list
- Implement `read_plan` tool - display current plan with task status
- Implement `complete_task` tool - mark a task as done, optionally add notes
- Implement `add_task` tool - insert new tasks into the plan
- Implement `get_progress` tool - summary view (X of Y complete, current task, elapsed time)
- Store plan in the workspace filesystem (not in agent state)

**Key decisions to implement:**

- Plan is stored as `plan.json` in the workspace root
- Task states: pending, in_progress, completed
- Only one task should be in_progress at a time
- Progress notes are appended to a `progress.md` log file

**Deliverable:** Planning tools that allow the agent to create, execute, and track its own work plan.

---

### Phase 4: Instructions & Prompts

**Goal:** Consolidate all phase-specific prompts into a single instructions document and create a simplified system prompt.

**What we'll do:**

- Analyze existing phase prompts (`preprocessing`, `identification`, `research`, `formulation`, `output`)
- Merge all phase guidance into a single `instructions.md` document
- Structure instructions as a reference guide the agent can consult as needed
- Create a minimal system prompt that points the agent to its workspace and instructions
- Place `instructions.md` as a template that gets copied into each new job workspace
- Remove phase-specific prompt loading from the agent initialization

**Key decisions to implement:**

- Instructions are comprehensive but not overwhelming
- Agent reads instructions on-demand rather than having them in system prompt
- System prompt focuses on "how to work" (use workspace, follow plan) not domain details
- Instructions live in `config/workspace/instructions.md` (template)

**Deliverable:** A unified instructions document and simplified system prompt that replace the phase-based prompts.

---

### Phase 5: Agent Refactoring

**Goal:** Remove the phase-based state machine and transition to workspace-centric autonomous operation.

**What we'll do:**

- Simplify `CreatorAgentState` to remove phase tracking fields
- Remove the 5-node graph structure with conditional phase edges
- Replace with a simpler loop: process → tool_call → check_completion → loop/exit
- Change completion detection from text matching to plan-based (all tasks complete)
- Update the agent initialization to set up workspace instead of loading phase prompt
- Ensure existing domain tools (extract_document_text, chunk_document, etc.) still work
- Update the agent entry point to work with new architecture

**Key decisions to implement:**

- Agent self-directs via plan rather than phase transitions
- Completion is detected when plan shows all tasks done
- No hardcoded workflow - agent decides order based on instructions
- Domain tools remain unchanged, just work alongside new workspace tools

**Deliverable:** A refactored Creator Agent that uses workspace and planning tools instead of phase-based state machine.

---

### Phase 6: Context Management

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

---

### Phase 7: Tool Discovery (Optional Enhancement)

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

---

### Phase 8: Vector Search Integration

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

---

### Phase 9: Testing & Validation

**Goal:** Validate the new architecture against real-world document processing tasks.

**What we'll do:**

- Create test cases with sample documents of varying complexity
- Run end-to-end processing and collect metrics (tokens used, iterations, time)
- Compare context usage between old and new architecture
- Verify requirement extraction quality is maintained or improved
- Test error recovery and resume-from-checkpoint scenarios
- Benchmark with different document types and sizes
- Tune recursion limits based on observed behavior
- Document any issues found and iterate on fixes

**Key decisions to implement:**

- Test with actual GoBD/compliance documents
- Measure both efficiency (tokens, time) and quality (extraction accuracy)
- Establish baseline metrics for ongoing monitoring

**Deliverable:** Validated workspace-centric architecture with performance benchmarks.

---

### Phase Summary

| Phase | Name | Dependencies | Complexity |
|-------|------|--------------|------------|
| 1 | Workspace Foundation | None | Low |
| 2 | Workspace Tools | Phase 1 | Medium |
| 3 | Planning Tools | Phase 1 | Medium |
| 4 | Instructions & Prompts | None (parallel with 1-3) | Low |
| 5 | Agent Refactoring | Phases 1-4 | High |
| 6 | Context Management | Phase 5 | Medium |
| 7 | Tool Discovery | Phase 5 | Medium (optional) |
| 8 | Vector Search | Phases 2, 5 | Medium |
| 9 | Testing & Validation | All above | Medium |

**Parallel work opportunities:**
- Phases 1, 2, 3 can be developed in sequence but tested incrementally
- Phase 4 (prompts) can be done in parallel with Phases 1-3
- Phase 7 is optional and can be deferred if Phase 6 provides sufficient context savings

**Critical path:** 1 → 2 → 3 → 5 → 6 → 9

---

### Risk Assessment

| Risk | Mitigation |
|------|------------|
| Agent fails to follow instructions effectively | Test with smaller tasks first, iterate on instruction document |
| Context still grows too large | More aggressive tool result clearing, earlier summarization trigger |
| Planning overhead slows agent down | Monitor planning tool usage, simplify if excessive |
| Workspace I/O becomes bottleneck | Profile and optimize hot paths, consider caching |
| Vector search adds latency | Make semantic search optional, fallback to keyword |
| Breaking change for Validator integration | Keep requirement_cache interface unchanged |
