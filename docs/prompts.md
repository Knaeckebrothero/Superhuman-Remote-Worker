# Prompt Assembly: Issues and Recommendations

This document analyzes the current prompt assembly system and identifies redundancies that inflate token usage.

## Current Architecture

The agent receives prompts assembled from multiple sources:

| Layer | Source | Injected Via |
|-------|--------|--------------|
| System Prompt | `strategic_system.md` or `tactical_system.md` | `get_phase_system_prompt()` |
| Workspace Memory | `workspace.md` | `{workspace_content}` placeholder |
| Current Todos | `TodoManager.format_for_display()` | `{todos_content}` placeholder |
| Task Instructions | `instructions.md` | First HumanMessage (persists in history) |
| Tool Schemas | LangGraph tool binding | Automatic |

At iteration 44, the prompt contained **28,409 tokens** across 84 messages.

---

## Identified Redundancies

### 1. Tool Information Appears 3+ Times

| Source | Location | Content |
|--------|----------|---------|
| System prompt | `strategic_system.md:27-32` | Brief "Available Tools" list |
| instructions.md | Lines 11-25 | Detailed `<tool_categories>` block |
| LangGraph | Automatic | Full tool schemas with parameters |

**Impact**: ~300 tokens wasted per request.

### 2. workspace.md Duplicates instructions.md Content

The first strategic todo (`strategic_todos_initial.yaml:6-8`) instructs:

```yaml
content: >-
  Explore the workspace and populate workspace.md with an overview
  of the environment, available tools, and any existing context.
```

This causes the agent to **copy** content from instructions.md INTO workspace.md. Then **both** are sent to the LLM:

- `{workspace_content}` in system prompt contains the copied notes
- instructions.md remains as the first HumanMessage

Example of duplicated content in workspace.md Notes:

```markdown
- Available tool categories (as per system prompt): [lists them again]
- Existing context from instructions.md includes:
  - Phases: Document Analysis, Requirement Extraction, Verification.
  - Indicators for obligations, capabilities, constraints, compliance.
  - Keywords for GoBD and GDPR relevance.
  - Requirement types and priority rules.
  - Template for plan.md and add_requirement payload.
```

**Impact**: ~500+ tokens of pure duplication.

### 3. instructions.md Sent Every Iteration

The full 197-line instructions.md is added as a HumanMessage at job init (`graph.py:243-247`):

```python
message = HumanMessage(
    content=f"## Task Instructions\n\n{instructions}\n\n"
    ...
)
```

This persists in conversation history for all iterations, even though:

- Phase 1 instructions aren't needed during Phase 2
- Tool categories duplicate the system prompt
- The planning template is only needed once
- Reference material (GoBD keywords, German patterns) could be read on-demand

**Impact**: ~1,500 tokens in every request, mostly unused after initial planning.

### 4. Todos Referenced Redundantly

- System prompt includes `{todos_content}` with the formatted todo list
- workspace.md Notes section also describes todo state:
  ```markdown
  - `todos.yaml` contains the tactical tasks for Document Analysis (ids 1‑10).
  ```

**Impact**: Minor (~50 tokens), but adds noise.

---

## Root Causes

1. **Strategic todo #1 encourages duplication** — asking the agent to "populate workspace.md with available tools and existing context" causes it to copy instructions.md content

2. **instructions.md is monolithic** — contains all phases, all templates, and all reference material; sent in full regardless of current phase

3. **instructions.md goes in HumanMessage** — persists in conversation history instead of being trimmed or phase-specific

4. **No distinction between reference material and active instructions** — GoBD/GDPR keywords, German patterns, and templates are static reference that could be read on-demand rather than injected every time

---

## Recommendations

### 1. Remove "populate workspace.md with tools/context" Directive

Change `strategic_todos_initial.yaml` todo #1 from:

```yaml
content: >-
  Explore the workspace and populate workspace.md with an overview
  of the environment, available tools, and any existing context.
```

To:

```yaml
content: >-
  Explore the workspace and update workspace.md with the current
  status, key entities, and any blocking issues.
```

The agent doesn't need to copy tool lists or instructions—they're already available.

### 2. Split instructions.md by Phase

Instead of one monolithic file, create phase-specific instruction files:

```
configs/creator/
├── instructions_strategic.md   # Planning guidance only
├── instructions_phase1.md      # Document Analysis specifics
├── instructions_phase2.md      # Requirement Extraction specifics
└── reference/
    ├── gobd_keywords.md        # Read on-demand
    ├── gdpr_keywords.md        # Read on-demand
    └── german_patterns.md      # Read on-demand
```

Inject only the relevant phase's instructions into the HumanMessage.

### 3. Move Reference Material to Workspace Files

Static reference content (GoBD keywords, GDPR indicators, German patterns, templates) should be:

- Copied to workspace at job init (e.g., `tools/reference/`)
- Read by the agent when needed via `read_file`
- NOT injected into every prompt

### 4. Remove Tool Categories from instructions.md

The `<tool_categories>` block in instructions.md is redundant because:

- System prompt already lists key tools
- LangGraph provides full tool schemas
- Agent can read `tools/<tool_name>.md` for details

Remove lines 9-27 from instructions.md entirely.

### 5. Simplify workspace.md Purpose

workspace.md should track **dynamic state only**:

- Current phase and progress
- Accomplishments
- Key decisions made
- Blocking issues
- Important entity references

It should NOT contain:

- Copies of instructions
- Tool listings
- Static reference material

### 6. Consider System Prompt Placement for Instructions

Instead of HumanMessage (which persists in history), consider:

```python
# Option A: Inject phase-specific instructions into system prompt
full_system = get_phase_system_prompt(
    ...
    phase_instructions=get_phase_instructions(phase_number),
)

# Option B: Use a dedicated "context" section that gets refreshed
# rather than accumulated in message history
```

---

## Estimated Token Savings

| Change | Savings per Request |
|--------|---------------------|
| Remove tool duplication | ~300 tokens |
| Remove workspace.md instruction copies | ~500 tokens |
| Phase-specific instructions only | ~800 tokens |
| Move reference material to on-demand | ~400 tokens |
| **Total** | **~2,000 tokens** |

At 44 iterations, this would save ~88,000 tokens per job.

---

## Implementation Priority

1. **Quick win**: Update `strategic_todos_initial.yaml` to not ask for context copying
2. **Medium effort**: Split instructions.md into phase-specific files
3. **Medium effort**: Move reference material to workspace files
4. **Low priority**: Refactor system prompt injection for instructions
