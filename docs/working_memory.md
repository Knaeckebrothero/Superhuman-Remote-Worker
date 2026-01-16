# Working Memory (workspace.md) Initialization Issue

## Overview

This document investigates why `workspace.md` is initialized with generic template content rather than meaningful information about the actual workspace (e.g., documents present, job context, etc.).

## Current Behavior

When an agent starts a new job, the workspace.md file is created from a static template without any exploration of the workspace contents. The logs show:

```
2026-01-15 12:56:14 - src.graph - INFO - [job-id] Initializing workspace
2026-01-15 12:56:14 - src.core.workspace - DEBUG - Wrote file: workspace.md
2026-01-15 12:56:14 - src.managers.memory - INFO - Wrote memory: 1417 characters
2026-01-15 12:56:14 - src.graph - INFO - [job-id] Created workspace.md from template
```

The workspace.md gets written immediately without the agent checking what files exist in `workspace/documents/` or adapting to the actual job context.

## Investigation Results

### 1. Where Initialization Happens

**Location**: `src/graph.py:142-184`

The initialization is performed by `create_init_workspace_node()`, which creates a node that:

```python
def init_workspace(state: UniversalAgentState) -> Dict[str, Any]:
    """Initialize workspace.md from template."""
    job_id = state.get("job_id", "unknown")
    logger.info(f"[{job_id}] Initializing workspace")

    workspace_created = not memory_manager.exists()
    if workspace_created:
        memory_manager.write(workspace_template)  # <- WRITES TEMPLATE HERE
        logger.info(f"[{job_id}] Created workspace.md from template")
    else:
        logger.debug(f"[{job_id}] workspace.md already exists")

    # Read workspace into state for system prompt injection
    workspace_memory = memory_manager.read()
    ...
```

This node is part of the **INITIALIZATION sequence**:
```
init_workspace → read_instructions → create_plan → init_todos
```

### 2. What the Initialization Node Does

**Simple answer**: It writes a template directly - it does NOT explore the workspace first.

**The flow**:
1. Check if `workspace.md` already exists (via `memory_manager.exists()`)
2. **If it doesn't exist**: Write the template content directly via `memory_manager.write(workspace_template)`
3. **If it does exist**: Skip writing (resume case)
4. Read the workspace.md content into state for injection into system prompts

**Key issue**: The agent never:
- Lists what files exist in `workspace/documents/`
- Checks what the `document_path` is
- Explores the actual workspace structure
- Adapts the template based on actual files present

### 3. The Workspace Template

**Location**: `src/config/prompts/workspace_template.md`

**Content** (generic placeholder):
```markdown
# Workspace Memory

This file is your persistent memory. It survives context compaction and is included in every system prompt.
Use it to track critical information that you need to remember across the entire task.

**Update this file regularly.** When in doubt, write it down here.

## Status
- **Phase**: (current phase name)
- **Progress**: (brief progress indicator)
- **Blocked**: (any blockers, or "none")

## Accomplishments
## Key Decisions
## Entities
## Notes
```

**Important**: It's a **generic placeholder** with example sections. It's NOT configured per-agent or per-document type.

### 4. How the Template is Loaded

**In `src/agent.py:325-337`**:

```python
def _load_workspace_template(self) -> str:
    """Load the workspace.md template for the nested loop graph."""
    config_dir = Path(__file__).parent / "config" / "prompts"
    template_path = config_dir / "workspace_template.md"

    if not template_path.exists():
        raise FileNotFoundError(f"Workspace template not found: {template_path}")
    return template_path.read_text(encoding="utf-8")
```

The template is **hardcoded to one location**: `src/config/prompts/workspace_template.md`

It's loaded in `process_job()` (lines 251-252) and passed to the graph builder:
```python
workspace_template = self._load_workspace_template()

self._graph = build_nested_loop_graph(
    ...
    workspace_template=workspace_template,
)
```

### 5. Why Agent Doesn't Explore Before Writing

**The architectural constraint**:

The initialization sequence is strictly linear and runs **before** the agent has access to tools:

```python
# In src/agent.py process_job():
# 1. Create workspace for this job
updated_metadata = await self._setup_job_workspace(job_id, metadata, resume=resume)

# 2. Load tools for this job
await self._setup_job_tools()  # <- Tools loaded AFTER workspace setup

# 3. Build graph for this job
workspace_template = self._load_workspace_template()

self._graph = build_nested_loop_graph(
    llm=self._llm,
    llm_with_tools=self._llm_with_tools,
    tools=self._tools,
    ...
    workspace_template=workspace_template,
)
```

The graph is built with the template **before** execution starts. The `init_workspace` node:
- Runs as the first graph node
- Has no LLM interaction
- Has no tools available
- Simply writes the static template

### Files Involved

| File | Role |
|------|------|
| `src/graph.py:142-184` | `create_init_workspace_node()` - writes template |
| `src/managers/memory.py` | `MemoryManager.write()` - filesystem write operation |
| `src/config/prompts/workspace_template.md` | The actual template being written |
| `src/agent.py:325-337` | `_load_workspace_template()` - loads template from file |
| `src/agent.py:251-262` | Where template is passed to graph builder |
| `configs/creator/config.json` | Config (but NO workspace template override available) |

## The Problem Statement

**workspace.md is initialized from a static template without any context about**:
- What documents exist in the job's workspace
- What the `document_path` metadata contains
- The actual structure and content being processed
- Whether this is a Creator or Validator agent with different needs

**The template is generic and the same for all agents/jobs**, making workspace.md less useful as an adaptive memory system at initialization.

## Potential Solutions

### Option 1: Make Initialization an LLM Turn

**Approach**: Have `init_workspace` node call the LLM with tools enabled

**Implementation**:
- Convert `init_workspace` from a simple function node to an LLM node
- Let it list workspace files, read document metadata, explore structure
- Have it write an informed workspace.md based on actual findings

**Pros**:
- Provides best context from the start
- Agent understands workspace before planning

**Cons**:
- Adds latency (extra LLM call + tool executions)
- Increases token usage
- Complicates initialization logic

### Option 2: Two-Phase Initialization (Recommended)

**Approach**: Keep simple template write in init, add exploration as first outer loop task

**Implementation**:
- Keep current fast `init_workspace` node (writes template)
- In the first outer loop iteration, agent naturally explores workspace
- Agent updates workspace.md with findings as part of normal flow
- Either:
  - Add "explore workspace" to initial todo list
  - Or let agent discover need to explore when reading instructions

**Pros**:
- Clean separation of concerns
- Fast initialization
- Agent explores when it has full context and tools
- Fits naturally into nested loop architecture

**Cons**:
- workspace.md starts empty, populated after first loop
- Agent might not explore if not guided

### Option 3: Smart Template Rendering

**Approach**: Pass job metadata to template and pre-populate with known facts

**Implementation**:
- Make workspace_template.md a Jinja2 template
- Pass job metadata (document_path, agent_id, job_id, etc.) to renderer
- Pre-populate workspace.md with available metadata at init

**Example template**:
```markdown
# Workspace Memory

## Job Context
- **Job ID**: {{ job_id }}
- **Agent**: {{ agent_id }}
- **Document**: {{ document_path }}
- **Started**: {{ start_time }}

## Status
...
```

**Pros**:
- No extra LLM calls
- Workspace.md has basic context immediately
- Fast initialization

**Cons**:
- Still no file exploration
- Limited to metadata already in state
- Adds templating complexity

### Option 4: Make Exploration Part of read_instructions

**Approach**: After reading instructions.md, explore workspace before planning

**Implementation**:
- Add exploration step between `read_instructions` and `create_plan`
- Node calls LLM with tools to explore workspace
- Updates workspace.md with findings
- Then proceeds to planning with full context

**Pros**:
- Agent has context before creating plan.md
- Still part of initialization sequence
- Logical flow: read task → explore workspace → plan

**Cons**:
- Extends initialization phase
- Blurs line between init and execution

## Recommendation

**Option 2 (Two-Phase Initialization)** is the cleanest architecturally:

1. Keeps init fast and deterministic (current behavior)
2. Lets the agent naturally explore in its first outer loop turn
3. Fits the nested loop architecture (initialization → outer strategic loop)
4. Agent updates workspace.md as part of normal memory management

**Implementation steps**:
1. Keep current `init_workspace` node as-is
2. Add guidance to instructions.md or system prompt: "First, explore your workspace and update workspace.md with findings"
3. Optionally add "Explore workspace and documents" to initial todo list in `init_todos` node
4. Agent will naturally populate workspace.md in first outer loop iteration

This approach respects the architecture while solving the problem with minimal changes.

## Alternative: Quick Win with Option 3

If you want immediate improvement without architectural changes:

1. Convert `workspace_template.md` to Jinja2 template
2. Update `_load_workspace_template()` to accept metadata
3. Render template with job_id, document_path, agent_id at init
4. Agent still explores later, but workspace.md has basic facts from start

This can be done in ~30 lines of code changes and gives immediate value.
