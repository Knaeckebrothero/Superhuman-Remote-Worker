# Validator Agent Issues

This document describes the issues discovered when first running the validator agent.

**Status:** These are architectural issues requiring proper configuration refactoring, not quick code fixes.

---

## Core Problem: Agent Stuck in Strategic Phase

The validator agent enters strategic phase but never transitions to tactical execution. It repeatedly asks for "the requirement text" even though the requirement data should be available.

**Symptom:**
```
I'm ready to start the Understanding phase, but I still need the actual requirement text you'd like validated.

Could you please share the full requirement (or a representative excerpt) you want me to process?
```

**Root Cause:** The validator configuration (instructions, toolsets, strategic todos, workspace setup) was never properly implemented for the validator's specific workflow. The agent doesn't know:
1. Where to find the requirement data
2. How the requirement data gets into its workspace
3. What its actual job is

**Required:** Complete refactoring of the validator agent configuration.

---

## Issue 1: Wrong Attribute Name in Registry

**File:** `src/tools/registry.py:383`

**Error:**
```
AttributeError: 'ToolContext' object has no attribute 'neo4j_conn'
```

**Cause:** The code checked `context.neo4j_conn` but the `ToolContext` class uses `neo4j_db` as the attribute name.

**Needs:** Change to `context.has_neo4j()` or `context.neo4j_db`

---

## Issue 2: Requirement Data Not Extracted from Requirements Table

**File:** `src/agent.py` - `_extract_job_metadata()`

**Problem:** When the validator polls the `requirements` table, it gets rows with columns like `text`, `name`, `type`, `priority`, `gobd_relevant`, etc. However, `_extract_job_metadata()` only looks for fields like `requirement_data` and `requirement_id` which don't exist in the requirements table schema.

**Cause:** The metadata extraction was designed for the `jobs` table (used by creator), not the `requirements` table (used by validator).

**Requirements table columns:**
- `text` - the actual requirement text
- `name` - requirement name (e.g., "GoBD-11-RecordDetail")
- `type` - functional/non_functional/constraint/compliance
- `priority` - high/medium/low
- `gobd_relevant` - boolean
- `gdpr_relevant` - boolean
- `source_document` - where it came from
- `source_location` - location in document
- `mentioned_objects` - business objects mentioned
- `mentioned_messages` - messages mentioned
- `reasoning` - extraction reasoning
- `confidence` - extraction confidence score

**Needs:** Either modify `_extract_job_metadata()` to handle requirements table, or refactor the architecture.

---

## Issue 3: Requirement Data Not Written to Workspace

**File:** `src/agent.py` - `_setup_job_workspace()`

**Problem:** Even if the requirement data were extracted, there's no code to write it to the workspace. The agent has no way to know what requirement it's supposed to validate.

**Cause:** `_setup_job_workspace()` only handles document paths (for creator agent). There's no code to write requirement data for the validator.

**Needs:** A mechanism to inject requirement data into the validator's workspace (e.g., write to `analysis/requirement_input.md` or similar).

---

## Issue 4: Generic Strategic Todos Don't Guide Validator

**File:** `src/config/strategic_todos_initial.yaml` (framework default)

**Problem:** The default strategic todos are generic and don't tell the validator agent where to find its input or what to do.

**Default todos (too generic for validator):**
1. Explore workspace and populate workspace.md
2. Read instructions.md and create plan
3. Divide plan into phases
4. Create todos for first tactical phase

**Cause:** No validator-specific strategic todos exist in `configs/validator/`.

**Needs:** Validator-specific strategic todos that:
1. Tell the agent where to find the requirement data
2. Guide it through the validation workflow
3. Reference the correct tools and files

---

## Issue 5: Instructions May Not Match Actual Workflow

**File:** `configs/validator/instructions.md`

**Problem:** The instructions describe a 4-phase workflow but may not align with how data actually flows into the agent's workspace, what tools are available, or how the polling mechanism works.

**Needs:** Review and update instructions to match the actual architecture.

---

## Summary

| Issue | Location | Root Cause |
|-------|----------|------------|
| Attribute name typo | `registry.py:383` | `neo4j_conn` vs `neo4j_db` |
| Data not extracted | `agent.py` | Requirements table columns not handled |
| Data not written | `agent.py` | No code to write requirement to workspace |
| Generic todos | Framework default | No validator-specific guidance |
| Instructions mismatch | `configs/validator/` | Never properly configured for validator workflow |
| Obsolete tools | `graph_tools.py` | 9 domain tools potentially from old architecture |

---

## Issue 6: Potentially Obsolete Domain Tools

**File:** `src/tools/graph_tools.py`

**Problem:** The following validator domain tools may be obsolete - they appear to be from the old phase-based agent system without workspace integration:

| Tool | Stated Purpose | Concern |
|------|----------------|---------|
| `find_similar_requirements` | Find requirements with 70%+ similarity | May not work with current architecture |
| `check_for_duplicates` | Check for 95%+ duplicate requirements | May not work with current architecture |
| `resolve_business_object` | Find matching BusinessObject in graph | May not work with current architecture |
| `resolve_message` | Find matching Message in graph | May not work with current architecture |
| `create_requirement_node` | Create new Requirement node | May not work with current architecture |
| `create_fulfillment_relationship` | Create fulfillment relationships | May not work with current architecture |
| `generate_requirement_id` | Generate new R-XXXX ID | May not work with current architecture |
| `get_entity_relationships` | Get relationships for an entity | May not work with current architecture |
| `count_graph_statistics` | Get node/relationship counts | May not work with current architecture |

**Needs:** Review each tool to determine:
1. Does it still work with the current Neo4j schema?
2. Does it integrate properly with the workspace-based workflow?
3. Should it be refactored or removed?

---

## Architecture Questions to Resolve

1. **How should requirement data flow into the validator?**
   - Written to a file in workspace?
   - Injected into the initial message?
   - Available via a tool?

2. **What's the validator's actual input?**
   - A single requirement at a time (from polling)?
   - Multiple requirements in batch?
   - The full job with all its requirements?

3. **What strategic todos make sense for validator?**
   - Should it start by reading requirement from a known location?
   - Should the initial message contain the requirement?

4. **How does the validator signal completion?**
   - Update status in requirements table?
   - Call job_complete?
   - Both?
