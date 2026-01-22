# Validator Agent Issues

This document describes the issues discovered when first running the validator agent.

**Status:** ✅ ALL ISSUES RESOLVED

---

## Core Problem: Agent Stuck in Strategic Phase - RESOLVED

The validator agent was entering strategic phase but never transitioning to tactical execution because it didn't know where to find requirement data.

**Root Cause:** The validator configuration was never properly implemented for the validator's specific workflow.

**Solution:** Complete refactoring implemented:
1. Requirement data written to `analysis/requirement_input.md` in workspace
2. Instructions updated with clear "First Steps" section
3. `_extract_job_metadata()` handles requirements table rows for polling mode
4. CLI `--requirement-id` flag for direct requirement validation
5. Post-job PostgreSQL update via `output/integration_result.json`

---

## Issue 1: Wrong Attribute Name in Registry - FIXED

**File:** `src/tools/registry.py:383`

**Status:** FIXED

Changed `context.neo4j_conn` to `context.has_neo4j()` and updated warning message to reference `neo4j_db`.

---

## Issue 2: Requirement Data Not Extracted from Requirements Table - FIXED

**File:** `src/agent.py` - `_extract_job_metadata()`

**Status:** FIXED

**Solution:** Modified `_extract_job_metadata()` to detect requirements table rows (have `text` but no `prompt`) and wrap them as `requirement_data`. This allows the existing `_setup_job_workspace()` code to write the requirement to `analysis/requirement_input.md`.

---

## Issue 3: Requirement Data Not Written to Workspace - FIXED

**File:** `src/agent.py` - `_setup_job_workspace()`

**Status:** FIXED

**Solution:** Added code in `_setup_job_workspace()` to check for `requirement_data` in metadata and write it to `analysis/requirement_input.md` using `_format_requirement_as_markdown()` helper method.

---

## Issue 4: Generic Strategic Todos Don't Guide Validator - FIXED

**File:** `configs/validator/instructions.md`

**Status:** FIXED

**Solution:** Updated validator instructions with clear "First Steps" section that tells the agent to:
1. Read `analysis/requirement_input.md` first
2. Check for metamodel in `documents/`
3. Explore the existing graph before integrating

The instructions now provide explicit guidance rather than relying on generic strategic todos.

---

## Issue 5: Instructions May Not Match Actual Workflow

**File:** `configs/validator/instructions.md`

**Problem:** The instructions describe a 4-phase workflow but may not align with how data actually flows into the agent's workspace, what tools are available, or how the polling mechanism works.

**Needs:** Review and update instructions to match the actual architecture.

---

## Summary

| Issue | Location | Root Cause | Status |
|-------|----------|------------|--------|
| Attribute name typo | `registry.py:383` | `neo4j_conn` vs `neo4j_db` | **FIXED** |
| Data not extracted | `agent.py` | Requirements table columns not handled | **FIXED** |
| Data not written | `agent.py` | No code to write requirement to workspace | **FIXED** |
| Generic todos | Framework default | No validator-specific guidance | **FIXED** |
| Instructions mismatch | `configs/validator/` | Never properly configured for validator workflow | **FIXED** |
| Obsolete tools | `graph_tools.py` | 9 domain tools from old architecture | **REMOVED** |

---

## Issue 6: Obsolete Domain Tools - RESOLVED

**File:** `src/tools/graph_tools.py`

**Status:** REMOVED

The following 9 tools were removed from the codebase:

| Tool | Reason for Removal |
|------|--------------------|
| `find_similar_requirements` | Regex similarity approach didn't work well |
| `check_for_duplicates` | Regex similarity approach didn't work well |
| `resolve_business_object` | Cypher wrapper - agent can use `execute_cypher_query` directly |
| `resolve_message` | Cypher wrapper - agent can use `execute_cypher_query` directly |
| `create_requirement_node` | Cypher wrapper - agent can use `execute_cypher_query` directly |
| `create_fulfillment_relationship` | Cypher wrapper - agent can use `execute_cypher_query` directly |
| `generate_requirement_id` | Cypher wrapper - agent can use `execute_cypher_query` directly |
| `get_entity_relationships` | Cypher wrapper - agent can use `execute_cypher_query` directly |
| `count_graph_statistics` | Cypher wrapper - agent can use `execute_cypher_query` directly |

**Remaining graph tools (3):**
- `execute_cypher_query` - General query execution
- `get_database_schema` - Schema inspection
- `validate_schema_compliance` - Metamodel validation

**Files modified:**
- `src/tools/graph_tools.py` - Removed function definitions and metadata
- `src/tools/description_generator.py` - Removed docstrings
- `configs/validator/config.json` - Updated domain tools list
- `configs/validator/instructions.md` - Updated to use Cypher directly

---

## Architecture Questions - RESOLVED

1. **How should requirement data flow into the validator?**
   - ✅ Written to `analysis/requirement_input.md` in workspace
   - Via CLI: `--requirement-id` fetches from PostgreSQL and writes to workspace
   - Via polling: `_extract_job_metadata()` wraps requirement row as `requirement_data`

2. **What's the validator's actual input?**
   - ✅ A single requirement at a time
   - CLI mode: `--requirement-id <uuid>`
   - Polling mode: picks up pending requirements from `requirements` table

3. **What strategic todos make sense for validator?**
   - ✅ Instructions tell agent to read `analysis/requirement_input.md` first
   - Then check for metamodel in `documents/`
   - Then explore existing graph

4. **How does the validator signal completion?**
   - ✅ Both: Agent calls `job_complete` and writes `output/integration_result.json`
   - CLI reads the output file and updates PostgreSQL requirement record
   - Updates: `neo4j_id`, `status`, `validated_at`
