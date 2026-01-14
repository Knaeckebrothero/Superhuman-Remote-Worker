# Creator Agent Issues Assessment

This document summarizes the issues identified causing the Creator Agent to get stuck in the preprocessing phase, looping 250+ iterations without making progress.

## Summary

The agent ran for 250 iterations (hitting the 500 recursion limit) while remaining in the "preprocessing" phase with 0 requirements created. The root cause is a combination of prompt engineering issues and missing control flow logic.

---

## Issue 1: LLM Not Calling Tools

**Severity:** Critical
**Location:** `src/agents/creator/creator_agent.py` lines 331-366

### What's Happening
- The LLM responds with text (explaining what it *would* do) instead of actually calling tools
- The graph routes: `process` → `check` → `process` → `check` (infinite loop)
- Each iteration, the LLM sees the same messages and generates similar non-tool responses

### Why It Happens
- The system prompt (`config/prompts/creator_preprocessing.txt`) describes tools but doesn't *enforce* their use
- Some LLM models need explicit instruction like "You MUST call a tool now"
- The continuation prompt added earlier is too generic: `"Continue with the task. Use the available tools to make progress..."`

### Proposed Fix
Update prompts to be more forceful about tool usage:
```
## CRITICAL: You MUST Use Tools
You MUST call tools to make progress. DO NOT just describe what you would do.
- FIRST: Call `extract_document_text` with the document path
- THEN: Call `chunk_document` to split the document into chunks
```

---

## Issue 2: No Phase Transition Mechanism

**Severity:** Critical
**Location:** `src/agents/creator/creator_agent.py` lines 197-214, 280-281, 307

### What's Happening
- The system prompt is loaded ONCE during `_initialize_node()` with phase="preprocessing"
- Even if `state["current_phase"]` changes to "identification", the SystemMessage in `state["messages"]` still says "PREPROCESSING"
- The LLM keeps seeing "Phase: PREPROCESSING" for all 500 iterations

### Expected Behavior
- When preprocessing completes (document extracted and chunked), phase should change to "identification"
- The system prompt should update to reflect the new phase
- Phase-specific prompts exist (`creator_identification.txt`, etc.) but are never loaded after initialization

### Proposed Fix
Add dynamic system prompt updates when phase changes:
```python
def _update_system_prompt(self, state: CreatorAgentState, new_phase: str) -> None:
    """Update the system prompt when phase changes."""
    new_prompt = load_prompt(f"creator_{new_phase}.txt")
    # Find and replace the SystemMessage in state["messages"]
    for i, msg in enumerate(state["messages"]):
        if isinstance(msg, SystemMessage):
            state["messages"][i] = SystemMessage(content=new_prompt)
            break
    state["current_phase"] = new_phase
```

---

## Issue 3: Completion Detection Relies on Text Matching

**Severity:** High
**Location:** `src/agents/creator/creator_agent.py` lines 386-413

### What's Happening
- The `_check_completion_node` looks for specific phrases like "all requirements have been extracted" or "processing complete"
- If the LLM uses different wording, completion is never detected
- There's no fallback to check actual progress (e.g., are there chunks? are there candidates?)

### Current Code
```python
completion_indicators = [
    "all requirements have been extracted",
    "processing complete",
    "no more candidates",
    "finished processing",
]
if any(ind.lower() in content.lower() for ind in completion_indicators):
    state["current_phase"] = "output"
    state["should_stop"] = True
```

### Proposed Fix
Add progress-based completion detection:
```python
# Check actual progress, not just text matching
if state["current_phase"] == "preprocessing":
    if state.get("document_chunks"):
        # Document has been chunked, move to identification
        self._update_system_prompt(state, "identification")

elif state["current_phase"] == "identification":
    if state.get("candidates"):
        # Candidates identified, move to research
        self._update_system_prompt(state, "research")
```

---

## Issue 4: No Consecutive Non-Tool-Call Detection

**Severity:** Medium
**Location:** `src/agents/creator/creator_agent.py` `_process_node`

### What's Happening
- If the LLM responds without tool calls 10+ times in a row, the agent should recognize it's stuck
- Currently, it just keeps adding generic continuation prompts and looping
- No escalation mechanism to force tool usage

### Proposed Fix
Track consecutive non-tool responses and escalate prompts:
```python
# In CreatorAgentState, add:
consecutive_non_tool_calls: int = 0

# In _process_node, after LLM response:
if not (hasattr(response, "tool_calls") and response.tool_calls):
    state["consecutive_non_tool_calls"] = state.get("consecutive_non_tool_calls", 0) + 1

    if state["consecutive_non_tool_calls"] > 3:
        # Escalate: provide explicit tool call instruction
        escalation = HumanMessage(content=f"""
You have not called any tools in {state["consecutive_non_tool_calls"]} responses.
You MUST call a tool NOW to make progress.

For the current phase ({state["current_phase"]}), call:
- extract_document_text(file_path="{state['document_path']}")
""")
        state["messages"].append(escalation)
else:
    state["consecutive_non_tool_calls"] = 0
```

---

## Issue 5: Async Tool in Sync Context (Potential)

**Severity:** Low (needs verification)
**Location:** `src/agents/creator/tools.py` line 408

### What's Happening
```python
@tool
async def write_requirement_to_cache(...)  # This is async
```
All other tools are synchronous. This might cause issues with LangGraph's ToolNode depending on how it handles mixed async/sync tools.

### Proposed Fix
Either make all tools async, or convert `write_requirement_to_cache` to sync with `asyncio.run()` internally.

---

## Additional Observations

### Document Path Handling
The document path flows correctly through:
1. CLI argument `--document-path ./data/GoBD_example.pdf`
2. Stored in PostgreSQL `jobs.document_path`
3. Retrieved by `get_job()`
4. Set in `state["document_path"]`
5. Passed to LLM in user message

Verify the path is correct:
```bash
podman-compose exec postgres psql -U graphrag -d graphrag \
  -c "SELECT id, document_path FROM jobs ORDER BY created_at DESC LIMIT 1;"
```

### LLM Model Considerations
- ~~Some models are better at tool calling than others~~
- ~~The current model (`gpt-oss-120b` via `ai.h4ll.app`) may need more explicit instructions~~
- ~~Consider testing with a known good tool-calling model (e.g., GPT-4, Claude) to isolate the issue~~

**RESOLVED (2026-01-07):** Tool calling format is NOT an issue. The llama.cpp server correctly handles the OpenAI-compatible tool call format conversion. Extensive testing confirmed tool calls work reliably - the issue is elsewhere (likely prompt engineering or control flow logic).

### 502 Bad Gateway Errors
- The final crash was due to LLM backend errors (502 Bad Gateway)
- The OpenAI client retries automatically but eventually fails
- Consider adding agent-level retry logic with longer delays

---

## Implementation Priority

1. **Fix Issue 1** (Tool usage enforcement) - Highest impact, easiest to implement
2. **Fix Issue 4** (Non-tool-call detection) - Prevents infinite loops
3. **Fix Issue 2** (Phase transitions) - Enables multi-phase workflow
4. **Fix Issue 3** (Progress-based completion) - More robust completion detection
5. **Fix Issue 5** (Async tool) - Low priority, may not be causing issues

---

## Testing Strategy

After implementing fixes:

1. Run with verbose logging:
   ```bash
   python run_creator.py --document-path ./data/GoBD_example.pdf \
     --prompt "Extract GoBD requirements" --stream --verbose
   ```

2. Watch for:
   - Tool calls being made (look for `extract_document_text`, `chunk_document`)
   - Phase transitions in logs
   - Requirements being written to cache

3. Check database for progress:
   ```bash
   podman-compose exec postgres psql -U graphrag -d graphrag \
     -c "SELECT COUNT(*) FROM requirement_cache WHERE job_id = '<job-uuid>';"
   ```
