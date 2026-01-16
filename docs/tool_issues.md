# Tool Phase Filtering Issue

## Summary

Tools are NOT dynamically filtered by phase. The agent receives ALL tools in BOTH strategic and tactical phases. Phase differentiation is done entirely via system prompts, making it a "soft" restriction rather than hard enforcement.

## Current Implementation: Prompt-Based Control Only

### How It Works

1. **At job start** (`src/agent.py:550`): All tools are loaded via `load_tools()` with no phase filtering
2. **Tools bound once** (`src/agent.py:569`): `llm.bind_tools(self._tools)` binds all tools for the entire job
3. **Same LLM used in both phases** (`src/graph.py:1080`): `create_execute_node(llm_with_tools, ...)` gets the same pre-bound LLM
4. **System prompt changes per phase** (`src/graph.py:309-315`): `get_phase_system_prompt()` loads either `strategic.txt` or `tactical.txt`

### The Prompts Tell the Agent What to Do

**Strategic prompt** (`src/config/prompts/strategic.txt`):
```
- Do NOT execute domain work (document processing, database operations, etc.)
- Focus on: exploring workspace, maintaining workspace.md, planning, creating todos
```

**Tactical prompt** (`src/config/prompts/tactical.txt`):
```
- Use domain tools for the actual work (document extraction, search, database, citations)
- Do NOT create new todos or modify the plan
```

## Unused Infrastructure

The registry (`src/tools/registry.py`) has phase-filtering functions that are **never called**:

| Function | Purpose | Status |
|----------|---------|--------|
| `filter_tools_by_phase(tool_names, phase)` | Filter tool list by phase | Unused |
| `load_tools_for_phase(tool_names, phase, context)` | Load only phase-appropriate tools | Unused |
| `get_tools_for_phase(phase)` | Get all tools available in a phase | Unused |
| `get_phase_tool_summary()` | Summary of tools by phase | Unused |

Tool metadata defines phase availability but it's not enforced:
```python
# Example from registry.py
"job_complete": {
    "phases": ["strategic"],  # Strategic-only: prevents premature termination
}
```

## Tools by Intended Phase

### Strategic-Only Tools
- `todo_write` / `next_phase_todos` - Create todos for tactical phase
- `job_complete` - Final job completion signal

### Tactical-Only Tools (Domain Tools)
- Document: `extract_document_text`, `chunk_document`, `identify_requirement_candidates`
- Search: `web_search`
- Citation: `cite_document`, `cite_web`, `list_sources`, `get_citation`
- Cache: `add_requirement`, `list_requirements`, `get_requirement`
- Graph: `execute_cypher_query`, `get_database_schema`, `find_similar_requirements`, etc.

### Both Phases
- Workspace: `read_file`, `write_file`, `list_files`, etc.
- Todo: `todo_complete`, `todo_rewind`
- Completion: `mark_complete`

## Why This Matters

The LLM **can technically call any tool in any phase** - it's only guided by the prompt not to. If the LLM ignores the prompt guidance, it could:
- Call domain tools (document extraction, database operations) during strategic phase
- Call `todo_write` or `job_complete` during tactical phase
- Prematurely terminate jobs or skip planning

## Potential Solutions

### Option 1: Dynamic Tool Binding (Recommended)

Re-bind tools to the LLM at each phase transition:

```python
# In execute node or phase transition
phase = "strategic" if state.get("is_strategic_phase") else "tactical"
phase_tools = load_tools_for_phase(all_tool_names, phase, context)
llm_with_phase_tools = llm.bind_tools(phase_tools)
```

**Pros:** Hard enforcement, impossible for LLM to call wrong tools
**Cons:** Slight overhead at phase transitions

### Option 2: Tool Execution Guard

Add validation in the tool node before executing:

```python
def create_audited_tool_node(tools, config):
    def tool_node(state):
        phase = "strategic" if state.get("is_strategic_phase") else "tactical"
        for tool_call in state["messages"][-1].tool_calls:
            if not is_tool_allowed_in_phase(tool_call["name"], phase):
                return error_message(f"Tool {tool_call['name']} not allowed in {phase} phase")
        # ... execute tools
```

**Pros:** Single point of enforcement, minimal code changes
**Cons:** LLM still sees all tools, may attempt invalid calls

### Option 3: Stronger Prompt Engineering

Enhance prompts with explicit tool lists and stronger warnings.

**Pros:** No code changes
**Cons:** Soft enforcement, LLM can still ignore

## Related Files

- `src/tools/registry.py` - Tool registry with unused phase filtering
- `src/agent.py:528-571` - Tool loading at job setup
- `src/graph.py:266-415` - Execute node (uses pre-bound LLM)
- `src/graph.py:1013-1164` - Graph builder
- `src/core/loader.py:589-649` - Phase system prompt loading
- `src/config/prompts/strategic.txt` - Strategic phase prompt
- `src/config/prompts/tactical.txt` - Tactical phase prompt
