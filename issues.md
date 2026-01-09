# Universal Agent Issues

Investigation date: 2026-01-08

## Summary: Misimplementation of Workspace Architecture

The issues below stem from a **partial implementation** of the workspace-centric architecture described in `workspace_implementation.md`. The design patterns were understood conceptually but not fully implemented:

| Pattern | Design Intent | What Was Implemented | What Was Missed |
|---------|--------------|---------------------|-----------------|
| **Context Efficiency** | Tools write data to files, return file paths | Tools return concise summaries | Data never persisted to files |
| **Async LangGraph** | Use async for DB operations | Async tools with `asyncpg` | Graph node uses sync `invoke()` |
| **State Propagation** | All nodes return needed state fields | Iteration tracked in process_node | check_node doesn't return iteration |

**Key insight from `workspace_implementation.md` lines 835-856:**
> "If an agent writes a 500-line code file, the chat history should not contain the file content—only the file path (e.g., 'Output saved to /src/main.py')."

This TWO-PART pattern requires:
1. ✅ Return concise summaries (implemented)
2. ❌ Persist full data to workspace (NOT implemented)

The result: agent tools are "context efficient" but the actual data is **lost forever**.

---

## Critical Issues

### 1. Async Tool Sync Invocation Bug

**Location:** `src/agents/universal/graph.py:355-383`

**Problem:** The `tools_node` function is defined as **synchronous** (line 355):
```python
def tools_node(state: UniversalAgentState) -> Dict[str, Any]:
```

And uses synchronous invocation (line 383):
```python
result = tool_node.invoke(state)
```

But some tools are defined as **async functions**, which creates async `StructuredTool` objects:
- `src/agents/shared/tools/cache_tools.py:40` - `async def write_requirement_to_cache`
- `src/agents/shared/tools/vector_tools.py:37` - `async def semantic_search`
- `src/agents/shared/tools/vector_tools.py:102` - `async def index_file_for_search`
- `src/agents/shared/tools/vector_tools.py:139` - `async def get_vector_index_stats`

#### Root Cause: LangChain Tool Decorator Behavior

When you use `@tool` on an `async def` function, LangChain creates an async `StructuredTool` that can **ONLY** be called with `ainvoke()`, not `invoke()`.

```python
# In cache_tools.py - this creates an ASYNC tool
@tool
async def write_requirement_to_cache(...) -> str:
    result = await context.postgres_conn.fetchrow(...)  # async DB call
    ...

# In graph.py - this FAILS for async tools
result = tool_node.invoke(state)  # Can't sync-invoke an async tool!
```

**Error:**
```
StructuredTool does not support sync invocation
```

**Impact:** Agent cannot write requirements to the PostgreSQL cache, rendering the entire Creator → Validator pipeline ineffective. The agent completes but requirements are never persisted.

**Fix Options:**

**Option A (Recommended): Make tools_node async**
```python
# graph.py line 355
async def tools_node(state: UniversalAgentState) -> Dict[str, Any]:
    ...
    result = await tool_node.ainvoke(state)  # Use ainvoke
```

**Option B: Convert async tools to sync**
```python
# cache_tools.py - use synchronous psycopg2 instead of asyncpg
@tool
def write_requirement_to_cache(...) -> str:
    result = context.postgres_conn.execute(...)  # sync DB call
```

Option A is preferred because:
1. The graph already uses `await graph.ainvoke()` at the top level
2. Maintains async DB performance benefits
3. Less invasive change (only graph.py needs modification)

---

### 2. Document Tools Don't Return Full Content

**Location:** `src/agents/shared/tools/document_tools.py`

**Problem:** The document processing tools return summaries but not the actual content.

#### Root Cause: Misimplementation of Context Preservation Pattern

This is a **misimplementation of the context efficiency pattern** from `workspace_implementation.md`.

The design intention (lines 835-856 of workspace_implementation.md):
```python
# GOOD: Write to file, return only confirmation
tool_result = ToolMessage(
    content="Extracted 500 lines. Written to chunks/chunk_001.md"
)
# Agent can always retrieve with read_file("chunks/chunk_001.md")
```

The pattern requires TWO parts:
1. **Persist full content to workspace files** (so agent can retrieve later)
2. **Return concise confirmation with file paths** (to keep context small)

The actual implementation only did half:
```python
# BROKEN: Return preview only, nothing persisted anywhere
return f"""Document Extraction Complete
...
Preview (first 1000 chars):
{result.get('text', '')[:1000]}...
"""
```

The implementer understood that tool results should be concise but **forgot to persist the actual data**.

#### `extract_document_text` (lines 114-149)
- Extracts full text via `processor.extract()` ✓
- Returns only 1000 character preview ✗
- Does NOT persist full text to workspace ✗

```python
# Line 143 - truncates to 1000 chars, full text is lost
{result.get('text', '')[:1000]}...
```

The agent sees:
```
Document Extraction Complete
File: documents/GoBD_example.pdf
Page Count: 44
Total Characters: 104,063
Preview (first 1000 chars): [truncated content]...
```

But has no way to access the remaining 103,063 characters.

#### `chunk_document` (lines 152-210)
- Creates all chunks via `processor.chunk()` ✓
- Returns only first 3 chunk previews (200 chars each) ✗
- Does NOT persist any chunks to workspace ✗

```python
# Lines 199-204 - only iterates first 3 chunks, rest are lost
for i, chunk in enumerate(chunks[:3]):
    text_preview = chunk.get('text', '')[:200]
```

The agent sees:
```
Document Chunking Complete
Total Chunks: 77
First 3 chunks preview: [3 chunks shown]
```

But has no way to access chunks 4-77.

**Impact:** Agent writes `[PLACEHOLDER]` to files because it literally doesn't have access to the content. The preprocessing summary claims "77 chunks" but only 2 chunk files exist in workspace (created manually by the agent based on the 3 previews it received).

**Fix:** Modify tools to implement the FULL pattern:

```python
@tool
def extract_document_text(file_path: str) -> str:
    # ... extraction code ...

    # PERSIST: Write full text to workspace
    output_path = f"extracted/{Path(file_path).stem}.txt"
    workspace.write_file(output_path, result.get('text', ''))

    # CONFIRM: Return concise result with file path
    return f"""Document Extraction Complete
File: {file_path}
Total Characters: {result.get('char_count', 0)}
Full text written to: {output_path}

Use read_file("{output_path}") to access the content."""

@tool
def chunk_document(file_path: str, ...) -> str:
    # ... chunking code ...

    # PERSIST: Write all chunks to workspace
    for i, chunk in enumerate(chunks):
        chunk_path = f"chunks/chunk_{i+1:03d}.txt"
        workspace.write_file(chunk_path, chunk.get('text', ''))

    # CONFIRM: Return concise result with file paths
    return f"""Document Chunking Complete
Total Chunks: {len(chunks)}
Chunks written to: chunks/chunk_001.txt through chunks/chunk_{len(chunks):03d}.txt

Use read_file("chunks/chunk_001.txt") to start processing."""
```

---

### 3. Iterations Counter Not Updating

**Location:** `src/agents/universal/graph.py:294-350` and `run_universal_agent.py:273-280`

**Problem:** Final output shows `Iterations: 0` despite logs showing `iter=67`.

Log output:
```
[LLM] 7c6b3410 | job=444d2524... | iter=67 | 4162ms | no tools
```

Final output:
```
Iterations:   0
```

#### Root Cause: Partial State Updates in Streaming Mode

LangGraph's `astream()` yields **partial state updates** from each node, not the full accumulated state.

**In `check_node` (graph.py:294-350):**
```python
# Completion case - doesn't include iteration!
if _detect_completion(messages):
    logger.info("Completion detected in messages")
    return {"should_stop": True}  # <-- No "iteration" field!

# Normal case - empty dict!
return {}  # <-- No "iteration" field!
```

**In `run_universal_agent.py` (streaming mode):**
```python
async for state in streaming_gen:
    final_state = state  # Keeps ONLY the last partial update
result = final_state or {}
print(f"Iterations:   {result.get('iteration', 0)}")  # Always 0!
```

The final state from streaming is just `{"should_stop": True}` from `check_node`, which doesn't contain `iteration`.

**Why logs show `iter=67`:**
The LLM archiver (`src/agents/shared/llm_archiver.py:245`) receives the iteration from `process_node` during execution, but this value doesn't make it to the final return.

**Impact:** User cannot track actual progress or iteration count.

**Fix Options:**

**Option A (Recommended): Include iteration in check_node returns**
```python
# graph.py check_node
iteration = state.get("iteration", 0)

if _detect_completion(messages):
    return {"should_stop": True, "iteration": iteration}

# At end of function
return {"iteration": iteration}  # Always return iteration
```

**Option B: Accumulate state in streaming mode**
```python
# run_universal_agent.py
accumulated_state = {}
async for state in streaming_gen:
    accumulated_state.update(state)  # Merge all updates
result = accumulated_state
```

**Option C: Use `get_state()` after streaming**
```python
# After streaming completes, get full accumulated state
final_state = await graph.aget_state(thread_config)
result = final_state.values
```

Option A is simplest since it fixes the root cause in one place.

---

## Medium Issues

### 4. Path Resolution Bug (FIXED)

**Location:** `src/agents/shared/tools/document_tools.py`

**Status:** Fixed on 2026-01-08

**Problem:** Document tools received relative paths like `documents/GoBD_example.pdf` but passed them directly to the processor without resolving relative to workspace.

**Fix Applied:** Added `resolve_path()` helper function that uses workspace manager's `get_path()` method.

---

### 5. Resume Flag Completely Ignored

**Location:** `run_universal_agent.py:214` and `src/agents/universal/agent.py`

**Problem:** The `--resume` CLI flag exists but is **completely ignored**.

#### Root Cause: Parameter Accepted But Not Used

In `run_universal_agent.py`:
```python
async def run_single_job(
    ...
    resume: bool = False,  # Accepted as parameter
    ...
):
    # ... resume is NEVER used in this function!
```

The `resume` flag is:
1. ✅ Parsed from CLI (line 180)
2. ✅ Passed to `run_single_job()` (line 375)
3. ❌ Never actually checked or used

In `agent.py`, `_setup_job_workspace()` (lines 364-372):
```python
# Always overwrites instructions.md, regardless of resume
self._workspace_manager.initialize()  # Creates fresh workspace
self._workspace_manager.write_file("instructions.md", instructions)  # Overwrites
```

**Impact:**
- Resume flag does nothing - the workspace is always recreated
- Any agent-written notes, plans, or progress in the workspace may be lost
- Historical checkpoint messages might trigger premature completion (see Issue #6)

**Fix:**
```python
async def run_single_job(..., resume: bool = False):
    # If resuming, check for existing workspace
    if resume and job_id:
        workspace_path = get_workspace_base_path() / f"job_{job_id}"
        if workspace_path.exists():
            logger.info(f"Resuming job with existing workspace: {workspace_path}")
            # Skip workspace setup, just load existing
            return await agent.process_job(job_id, metadata, resume=True)

    # Normal flow for new jobs
    ...
```

And in `agent.py`:
```python
async def _setup_job_workspace(self, job_id, metadata, resume=False):
    if resume and self._workspace_manager.path.exists():
        # Validate workspace has required files
        if not (self._workspace_manager.path / "instructions.md").exists():
            # Only write if missing
            instructions = load_instructions(self.config)
            self._workspace_manager.write_file("instructions.md", instructions)
        return metadata

    # Normal setup for new workspace
    ...
```

---

### 6. Premature Completion Detection (FIXED)

**Location:** `src/agents/universal/graph.py:590-650`

**Status:** Fixed on 2026-01-09

**Fix Applied:** Implemented `mark_complete` tool for explicit completion signaling. Updated `_detect_completion()` to:
1. First check if `output/completion.json` file exists (workspace check)
2. Then check for tool output message "Wrote file: output/completion.json"
3. Fall back to reduced phrase matching (fewer, more specific phrases)

Files modified:
- `src/agents/shared/tools/completion_tools.py` (NEW)
- `src/agents/shared/tools/registry.py`
- `src/agents/universal/graph.py`
- `config/agents/creator.json`, `config/agents/validator.json`
- `config/agents/instructions/system_prompt.md`

**Original Problem:** The `_detect_completion()` function checks for completion phrases in recent messages, which can trigger false completions.

#### Root Cause: Message-Based Detection Without Iteration Check

The `_detect_completion()` function (lines 590-650):
```python
def _detect_completion(messages: List[BaseMessage]) -> bool:
    # Check last 10 AI messages for completion phrases
    recent_ai_messages = [m for m in messages[-10:] if isinstance(m, AIMessage)]

    completion_phrases = [
        "task is complete",
        "task has been completed",
        "successfully completed",
        "validation complete",
        "completion.json",
        # ... many more
    ]

    for msg in recent_ai_messages:
        for phrase in completion_phrases:
            if phrase in msg.content.lower():
                return True  # Immediate completion!
```

**Problems:**

1. **No iteration guard**: Doesn't check if ANY new work was done
   - On resume: checkpoint loads messages → last message may say "complete" → immediate exit
   - Result: `Iterations: 0` because completion detected before first iteration

2. **Overly broad phrase matching**: "completion.json" matches even when READING the file
   - Agent: "Let me read completion.json to check status"
   - Detection: "Found 'completion.json' → COMPLETE!"

3. **No file verification**: Doesn't check if `output/completion.json` actually exists

**Example Failure Scenario:**
```
1. First run: Agent reaches iteration 50, says "I'll continue tomorrow"
2. User stops the job manually
3. Resume: Checkpoint loads 50 messages including "task is complete" from iteration 45
4. check_node runs _detect_completion → finds "task is complete" → returns should_stop=True
5. Result: Job exits immediately with Iterations: 0
```

**Impact:** Resumed jobs may exit immediately. False positives from phrase matching.

**Fix Options:**

**Option A (Recommended): Add iteration guard + file check**
```python
def _detect_completion(messages: List[BaseMessage], iteration: int, workspace) -> bool:
    # Must have done at least one iteration
    if iteration < 1:
        return False

    # Check if completion.json actually exists
    if workspace and workspace.file_exists("output/completion.json"):
        return True

    # Only then check messages (with stricter criteria)
    ...
```

**Option B: Check completion.json file only**
```python
def _detect_completion(messages, workspace) -> bool:
    # Only complete if the file was actually written
    if workspace and workspace.file_exists("output/completion.json"):
        try:
            content = workspace.read_file("output/completion.json")
            data = json.loads(content)
            return data.get("status") == "completed"
        except:
            pass
    return False
```

**Option C: Track "last checked iteration" in state**
```python
# In state
"completion_checked_at_iteration": 0

# In check_node
if iteration > state.get("completion_checked_at_iteration", 0):
    if _detect_completion(messages):
        return {"should_stop": True, "completion_checked_at_iteration": iteration}
```

---

## Low Issues

### 7. Candidate Extractor Finding Nothing

**Location:** `src/agents/creator/candidate_extractor.py`

**Symptom:**
```
Identified 0 candidates from 3 sentences
Identified 0 candidates from 2 sentences
```

**Root Cause:** The candidate extractor is running on placeholder text or truncated previews instead of actual document content (see Issue #2).

**Fix:** Will be resolved when Issue #2 is fixed and actual content is available.

---

### 8. No Web Search Fallback Guidance (FIXED)

**Location:** `config/agents/instructions/system_prompt.md`

**Status:** Fixed on 2026-01-09

**Fix Applied:** Updated system_prompt.md with three additions:
1. Phase transition guidance - re-read instructions.md before planning each new phase
2. Source-Based Work section - citation requirements, what not to do, examples
3. Fallback Strategies section - use web_search when extraction fails, never fabricate

**Problem:** System prompt doesn't mention web search as an available tool or suggest fallback strategies.

#### Root Cause: Incomplete System Prompt

The `system_prompt.md` mentions:
- ✅ Reading instructions.md
- ✅ Creating plans
- ✅ Using todos
- ✅ Writing files
- ❌ NO mention of `web_search` tool
- ❌ NO mention of fallback strategies

Full system prompt analysis:
```markdown
# What's IN the system prompt:
- Workspace structure
- Two-tier planning (strategic + tactical)
- Context management tips
- How to handle being stuck (re-read instructions)

# What's MISSING:
- Available domain tools (web_search, extract_document_text, etc.)
- Fallback strategies (if extraction fails, try web search)
- Error recovery guidance
- Tool-specific tips
```

**The creator_instructions.md DOES mention web search** (lines 154-160):
```markdown
## Fallback Strategies
- If document extraction fails, use `web_search` to find the document online
- If a specific requirement is unclear, search for clarification
```

But the agent only sees `instructions.md` if:
1. `instructions.md` is properly written to workspace (Issue #5)
2. Agent actually reads it (not guaranteed)

**Impact:**
- When document extraction fails, agent fabricates requirements from "domain knowledge"
- Agent doesn't know web_search exists unless it reads instructions.md
- No graceful degradation when tools fail

**Fix:** Add to `system_prompt.md`:

```markdown
## Available Tools

### Research Tools
- `web_search(query)` - Search the web for information
- `query_similar_requirements` - Find similar requirements in the knowledge graph

### Document Tools
- `extract_document_text` - Extract text from PDF/DOCX documents
- `chunk_document` - Split documents into processable chunks

### Fallback Strategy
If document extraction fails:
1. Try `web_search` to find the document content online
2. Search for official sources or summaries
3. Document what you found in `notes/research.md`

Never fabricate content - if you can't find information, say so.
```

**Why system prompt matters:**
The system prompt is **always visible** to the agent. Instructions.md requires a tool call to read, which may not happen if:
- Agent is confused about first steps
- Context is limited
- Previous iteration failed before reading instructions

---

## Test Jobs

| Job ID | Date | Status | Issues Encountered |
|--------|------|--------|-------------------|
| `da93d6ff-2a7f-462d-ace8-5732efe513e0` | 2026-01-08 | Completed (synthetic) | Path resolution bug, missing instructions.md |
| `444d2524-5f43-4c53-91d1-863dd9e69247` | 2026-01-08 | Completed (incomplete) | Async tool bug, tools don't persist content |

---

## Recommended Fix Priority

### Tier 1: Pipeline Broken (Agent Can't Do Its Job)

| Priority | Issue | Effort | Impact | Status |
|----------|-------|--------|--------|--------|
| **P0** | #1 Async tool invocation | Low (1 line change) | Unblocks DB writes | **FIXED** |
| **P0** | #2 Document tools persist | Medium (modify 2 tools) | Agent gets actual content | **FIXED** |

### Tier 2: Reliability Issues (Agent Works But Unreliably)

| Priority | Issue | Effort | Impact | Status |
|----------|-------|--------|--------|--------|
| **P1** | #5 Resume flag ignored | Medium | Resume actually works | **FIXED** |
| **P1** | #6 Premature completion | Medium | No false exits | **FIXED** |
| **P2** | #3 Iterations counter | Low (1 line change) | User sees progress | **FIXED** |

### Tier 3: Quality Issues (Agent Works But Suboptimally)

| Priority | Issue | Effort | Impact | Status |
|----------|-------|--------|--------|--------|
| **P2** | #8 Web search guidance | Low (edit prompt) | Better fallback behavior | **FIXED** |
| **P3** | #7 Candidate extractor | None (downstream) | Fixed by #2 | **FIXED** (by #2) |

### Suggested Fix Order

```
#1 (async) → #2 (persist) → Test pipeline works     ✓ DONE
    ↓
#5 (resume) → #6 (completion) → Test resume works   ✓ DONE
    ↓
#3 (iterations) → #8 (guidance) → Polish            ✓ DONE
```

**All issues resolved as of 2026-01-09.**

Total estimated changes:
- `graph.py`: 3 modifications (#1, #3, #6)
- `document_tools.py`: 2 modifications (#2)
- `run_universal_agent.py`: 1 modification (#5)
- `agent.py`: 1 modification (#5)
- `system_prompt.md`: 1 modification (#8)
