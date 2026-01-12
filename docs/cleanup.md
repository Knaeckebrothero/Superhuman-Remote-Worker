# Repository Cleanup Plan

**STATUS: IMPLEMENTED**

## Goal

Separate the legacy UI/chain-based system from the new workspace-centric Universal Agent backend. The result is:

1. **`legacy_system/`** - Self-contained folder with Streamlit UI and old agents (can be removed entirely later)
2. **`src/`** - Clean backend with only what's needed for agent/orchestrator containers

---

## Current State Analysis

### Files to Move to `legacy_system/`

```
src/ui/                          → Streamlit dashboard
src/agents/graph_agent.py        → Legacy single-requirement agent
src/agents/document_processor_agent.py
src/agents/requirement_extractor_agent.py
src/agents/requirement_validator_agent.py
src/agents/document_ingestion_supervisor.py
src/workflow.py                  → CLI workflow using legacy agent
config/prompts/                  → Prompts used by legacy chain approach
```

### Files to Keep in `src/`

```
src/agents/universal/            → The Universal Agent
src/agents/shared/               → Merge into src/agents/ (tools, utilities)
src/orchestrator/                → Job orchestration
src/core/                        → Neo4j utils, config, metamodel validator
src/database/                    → PostgreSQL schema files
```

### Files to Delete (unused/redundant)

```
src/agents/creator/              → Already deleted
src/agents/validator/            → Already deleted
run_creator.py                   → Already deleted
run_validator.py                 → Already deleted
```

---

## Target Structure

### Root Level

```
/
├── config/
│   ├── agents/                  # Agent configurations (creator.json, validator.json)
│   │   ├── instructions/        # Agent instruction templates
│   │   └── schema.json          # Config schema
│   └── llm_config.json          # Global LLM settings
│
├── data/                        # Metamodel, seed data
│
├── docker/                      # Dockerfiles
│   └── Dockerfile.agent         # Unified agent Dockerfile
│
├── docs/                        # Documentation
│
├── legacy_system/               # OLD: Streamlit UI + Legacy Agents
│   ├── README.md                # Explains this is deprecated
│   ├── src/
│   │   ├── ui/                  # Streamlit pages
│   │   ├── agents/              # Legacy agents
│   │   │   ├── graph_agent.py
│   │   │   ├── document_processor_agent.py
│   │   │   ├── requirement_extractor_agent.py
│   │   │   ├── requirement_validator_agent.py
│   │   │   └── document_ingestion_supervisor.py
│   │   └── workflow.py
│   ├── config/
│   │   └── prompts/             # Legacy prompts
│   ├── streamlit_app.py
│   ├── requirements_legacy.txt
│   └── run_streamlit.sh
│
├── migrations/                  # Database migrations
│
├── scripts/                     # Utility scripts (app_init.py, etc.)
│
├── src/                         # NEW: Clean backend
│   ├── __init__.py
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── agent.py             # UniversalAgent class
│   │   ├── graph.py             # LangGraph workflow
│   │   ├── state.py             # Agent state
│   │   ├── context.py           # Agent context
│   │   ├── loader.py            # Config loading
│   │   ├── app.py               # FastAPI application
│   │   ├── models.py            # Pydantic models
│   │   ├── tools/               # All tool implementations
│   │   │   ├── __init__.py
│   │   │   ├── registry.py      # Tool registry
│   │   │   ├── context.py       # ToolContext
│   │   │   ├── workspace_tools.py
│   │   │   ├── todo_tools.py
│   │   │   ├── document_tools.py
│   │   │   ├── graph_tools.py
│   │   │   ├── cache_tools.py
│   │   │   ├── citation_tools.py
│   │   │   ├── search_tools.py
│   │   │   ├── completion_tools.py
│   │   │   └── pdf_utils.py
│   │   ├── workspace_manager.py
│   │   ├── todo_manager.py
│   │   ├── context_manager.py
│   │   └── llm_archiver.py
│   │
│   ├── orchestrator/            # Job orchestration
│   │   ├── __init__.py
│   │   ├── app.py               # FastAPI orchestrator
│   │   ├── job_manager.py
│   │   ├── monitor.py
│   │   └── reporter.py
│   │
│   ├── core/                    # Core utilities
│   │   ├── __init__.py
│   │   ├── config.py            # Configuration loading
│   │   ├── neo4j_utils.py       # Neo4j connection
│   │   ├── metamodel_validator.py
│   │   └── document_models.py
│   │
│   └── database/                # Database schema
│       ├── schema.sql
│       └── schema_vector.sql
│
├── tests/                       # Tests for new system only
│
├── run_universal_agent.py       # Agent entry point
├── start_orchestrator.py        # Orchestrator entry point
├── docker-compose.yml
├── docker-compose.dev.yml
├── docker-compose.dbs.yml
├── requirements.txt
└── CLAUDE.md
```

---

## Migration Steps

### Phase 1: Create Legacy System Container

1. Create `legacy_system/` directory structure
2. Move Streamlit UI files:
   - `src/ui/` → `legacy_system/src/ui/`
   - `streamlit_app.py` → `legacy_system/streamlit_app.py`
3. Move legacy agents:
   - `src/agents/graph_agent.py` → `legacy_system/src/agents/`
   - `src/agents/document_*_agent.py` → `legacy_system/src/agents/`
   - `src/agents/requirement_*_agent.py` → `legacy_system/src/agents/`
   - `src/agents/document_ingestion_supervisor.py` → `legacy_system/src/agents/`
4. Move legacy prompts:
   - `config/prompts/` → `legacy_system/config/prompts/`
5. Move workflow:
   - `src/workflow.py` → `legacy_system/src/workflow.py`
6. Create `legacy_system/README.md` explaining deprecation
7. Create minimal `legacy_system/requirements_legacy.txt`
8. Update legacy imports to use relative paths

### Phase 2: Flatten Agent Structure

1. Move `src/agents/universal/*` to `src/agents/`:
   - `agent.py`, `graph.py`, `state.py`, `context.py`, `loader.py`
   - `app.py`, `models.py`
2. Move `src/agents/shared/tools/` to `src/agents/tools/`
3. Move `src/agents/shared/*.py` to `src/agents/`:
   - `workspace_manager.py`, `todo_manager.py`, `context_manager.py`, `llm_archiver.py`
4. Delete empty `src/agents/universal/` and `src/agents/shared/`
5. Update all imports

### Phase 3: Fix Broken Imports

1. Fix `document_tools.py`:
   - Remove imports from deleted `src.agents.creator.*`
   - Implement document processing directly or use simpler approach
   - Could use `pdf_utils.py` + langchain document loaders

2. Fix `search_tools.py`:
   - Remove any remaining imports from old agents

### Phase 4: Update Entry Points

1. Update `run_universal_agent.py`:
   - Imports from `src.agents` instead of `src.agents.universal`
2. Update `start_orchestrator.py` (if needed)
3. Update Docker files

### Phase 5: Update Tests

1. Move legacy-related tests to `legacy_system/tests/` (or delete)
2. Update test imports for new structure
3. Verify all tests pass

### Phase 6: Update Documentation

1. Update `CLAUDE.md` with new structure
2. Update `docs/workspace_implementation.md` status
3. Delete or archive obsolete docs

---

## Import Changes Summary

### Before (old - removed)

```python
from src.agent.universal import UniversalAgent
from src.agent.shared.workspace_manager import WorkspaceManager
from src.agent.shared.tools import load_tools, ToolContext
```

### After (current)

```python
from src.agent import UniversalAgent
from src.agent.workspace_manager import WorkspaceManager
from src.agent.tools import load_tools, ToolContext
```

---

## Files to Fix

### `src/agents/shared/tools/document_tools.py`

**Current broken imports:**

```python
from src.agent.creator.document_processor import CreatorDocumentProcessor
from src.agent.creator.candidate_extractor import CandidateExtractor
```

**Fix options:**
1. **Option A:** Implement document processing using langchain document loaders + `pdf_utils.py`
2. **Option B:** Create standalone `DocumentProcessor` class in `src/agents/tools/`
3. **Option C:** Stub out functionality and let agent use `read_file` + LLM analysis

**Recommendation:** Option A - Use langchain's document loaders which we already have as dependencies.

### `src/agents/shared/tools/search_tools.py`

Check for any remaining broken imports.

---

## Verification Checklist

- [x] Legacy system can run independently (if needed)
- [x] `run_universal_agent.py` works with new structure
- [ ] `pytest tests/` passes
- [ ] Docker build succeeds
- [ ] Agent can process a document end-to-end
- [ ] Orchestrator can coordinate jobs

---

## Future Considerations

### After Cleanup
- Delete `legacy_system/` entirely once new system is stable
- Consider renaming `UniversalAgent` to just `Agent` since there's only one
- Consider moving `config/agents/` to `src/agents/config/` for better locality

### Optional Enhancements (not in scope)
- Vector search integration (Phase 8 in workspace_implementation.md)
- Tool discovery/lazy loading (Phase 7 in workspace_implementation.md)

---

# Internal Legacy Code Cleanup

**STATUS: PENDING**

This section identifies legacy code **within** the Universal Agent implementation that should be cleaned up to avoid confusion. The current architecture uses a **nested loop graph** (`src/agent/graph.py`) with managers (`src/agent/managers/`), but remnants of the old architecture remain.

---

## High Priority Issues

### 1. Dual TodoManager Implementations

| Version | Location | Lines | Status |
|---------|----------|-------|--------|
| Legacy | `src/agent/core/todo.py` | ~1,431 | DEPRECATED |
| New | `src/agent/managers/todo.py` | ~200 | Preferred |

**Problem:** `src/agent/agent.py:27` imports the **legacy** version:
```python
from .core.todo import TodoManager  # This is the LEGACY version!
```

**Impact:** The legacy version has async/await, phase tracking, and reflection tasks that don't align with the new nested loop architecture.

**Fix:** Update import to use `from .managers import TodoManager`

---

### 2. Dead Phase Transition Code

**File:** `src/agent/core/transitions.py`

**Status:** Entire module is deprecated (lines 3-14 contain deprecation notice)

**Problem:** Still actively imported and used in `src/agent/tools/todo_tools.py:25-30`:
```python
from ..core.transitions import PhaseTransitionManager, TransitionResult, TransitionType
```

**Why it's dead:** The new nested loop graph (`src/agent/graph.py`) handles phase transitions structurally via graph nodes. It never calls `get_last_transition_result()`.

**Fix:** Remove the module and all references to it.

---

### 3. Dead Callback Pattern in todo_tools.py

**Location:** `src/agent/tools/todo_tools.py`

**Dead code sections:**
- Lines 35-65: Global state variable `_last_transition_result`
- Lines 236-251: Creates `PhaseTransitionManager`, stores transition result
- Lines 251, 323: Calls to `_set_transition_result()`

**Code pattern that's never consumed:**
```python
# todo_tools.py lines 35-65
_last_transition_result: Optional[TransitionResult] = None

def get_last_transition_result() -> Optional[TransitionResult]:
    """LEGACY: Used by old graph.py..."""

def _set_transition_result(result: TransitionResult) -> None:
    """LEGACY: Used by old graph.py..."""
```

**Impact:** Maintains complex state in module globals that serves no purpose. Creates confusion about how phase transitions work.

**Fix:** Remove all transition-related code from todo_tools.py.

---

## Medium Priority Issues

### 4. Deprecated Context Classes

**File:** `src/agent/core/context.py:82-128`

**Classes:**
- `ProtectedContextConfig` (lines 87-98)
- `ProtectedContextProvider` (lines 102-127)

**Status:** Both marked DEPRECATED in docstrings. `get_protected_context()` raises `DeprecationWarning` and returns `None`.

**Problem:** Still exported in `src/agent/__init__.py` (lines 32-33, 73-74).

**Fix:** Remove classes and their exports.

---

### 5. Import/Export Confusion

**File:** `src/agent/__init__.py`

**Problem:** Exports both versions of TodoManager:
```python
from .core.todo import TodoManager as LegacyTodoManager  # line 15
from .managers import TodoManager  # line 20
```

**Impact:** Unclear which to use without checking CLAUDE.md.

**Fix:** Remove `LegacyTodoManager` export after updating all imports.

---

### 6. Orphaned Configuration Settings

**Files:**
- `src/config/creator.json` (lines 95-102)
- `src/config/validator.json` (lines 95-102)

**Orphaned settings:**
```json
"protected_context_enabled": true,
"protected_context_plan_file": "main_plan.md",
"protected_context_max_chars": 2000,
"protected_context_include_todos": true
```

**Impact:** Settings are loaded during config parsing but the feature is disabled (ProtectedContextProvider returns None).

**Fix:** Remove these settings from config files and the schema.

---

## Summary Table

| Category | File Path | Lines | Status | Risk Level |
|----------|-----------|-------|--------|------------|
| Deprecated Module | `core/todo.py` | 1-25 | DEPRECATED (in use) | HIGH |
| Deprecated Module | `core/transitions.py` | 1-15 | DEPRECATED (in use) | HIGH |
| Dead Code | `todo_tools.py` | 35-65 | Global state (unused) | MEDIUM |
| Dead Code | `todo_tools.py` | 251, 323 | Transition callbacks | MEDIUM |
| Deprecated Classes | `core/context.py` | 82-128 | No-op stubs | MEDIUM |
| Orphaned Config | `creator.json` | 95-102 | Never used | LOW |
| Orphaned Config | `validator.json` | 95-102 | Never used | LOW |
| Import Confusion | `agent/agent.py` | 27 | Wrong import | MEDIUM |
| Export Confusion | `agent/__init__.py` | 15, 20 | Dual exports | MEDIUM |

---

## Cleanup Order

Recommended cleanup sequence:

1. **Fix imports first** - Update `src/agent/agent.py` to use new TodoManager
2. **Remove dead code in todo_tools.py** - Delete transition-related code
3. **Delete deprecated modules** - Remove `core/todo.py` and `core/transitions.py`
4. **Clean up context.py** - Remove deprecated classes
5. **Update __init__.py** - Remove legacy exports
6. **Clean config files** - Remove orphaned settings
7. **Update tests** - Fix any tests that depend on legacy code

---

## Files to Delete

After cleanup, these files can be removed entirely:
- `src/agent/core/todo.py`
- `src/agent/core/transitions.py`

## Tests to Update

These test files reference legacy code:
- `tests/test_todo_tools.py` - Uses legacy `src/agent/core/todo.py`
- `tests/test_phase_transitions.py` - Uses legacy `src/agent/core/transitions.py`
- `tests/test_guardrails_integration.py` - Uses deprecated phase transition code

---

# Additional Findings (Deep Scan)

**STATUS: PENDING**

Additional deprecated, dead, and legacy code found during deep scan.

---

## Code Quality Issues

### 1. Unused Import

**File:** `src/agent/core/context.py:20`

```python
from typing import Any, Callable, Dict, List, Optional, Tuple
```

**Issue:** `Tuple` is imported but never used in the file.

**Fix:** Remove `Tuple` from the import statement.

**Risk Level:** LOW

---

### 2. Duplicate WorkspaceConfig Classes

**Locations:**
- `src/agent/core/workspace.py:23`
- `src/agent/core/loader.py:32`

**Issue:** Two different `WorkspaceConfig` classes exist with different structures:

| Location | Fields |
|----------|--------|
| `workspace.py` | Different structure (workspace-focused) |
| `loader.py` | `structure`, `instructions_template`, `initial_files` |

**Impact:** Both are exported via `src/agent/__init__.py` and `src/agent/core/__init__.py`, causing potential import confusion.

**Fix:** Consolidate into a single `WorkspaceConfig` class or rename one to avoid collision.

**Risk Level:** MEDIUM

---

### 3. Legacy Workspace Persistence in TodoManager

**File:** `src/agent/core/todo.py`

**Lines:** 146, 161, 168, 185-190

**Issue:** The deprecated `TodoManager` has a `_legacy_workspace` attribute and associated methods (`load_todo()`, `save_todo()`) that are never called in the current codebase.

```python
# Line 146
self._legacy_workspace = legacy_workspace  # optional, for backwards compat

# Line 169
logger.debug("No legacy workspace - using session-scoped mode")
```

**Impact:** Dead code path that adds complexity without benefit.

**Fix:** Will be resolved when `core/todo.py` is deleted entirely.

**Risk Level:** MEDIUM (tied to HIGH priority item)

---

### 4. Global State Anti-Pattern in Tools

**File:** `src/agent/tools/todo_tools.py:35-37`

**Issue:** Uses module-level global variable for inter-module communication:

```python
_last_transition_result: Optional[TransitionResult] = None
```

**Why it's bad:**
- Hard to test (requires global state manipulation)
- Hidden data flow between tools and graph
- Race conditions possible in async contexts
- Violates explicit dependency injection pattern used elsewhere

**Fix:** Remove when transition code is cleaned up. New architecture uses graph state, not globals.

**Risk Level:** MEDIUM

---

## Orphaned Modules

### 5. CSV Processor Module

**File:** `src/core/csv_processor.py`

**Issue:** This module provides `RequirementProcessor` and `load_requirements_from_env()` for loading requirements from CSV files, but:
- Not used by any agent code
- Only used by legacy scripts (if at all)
- Exports `CSV_FILE_PATH` env var that isn't read by current system

**Evidence:**
- Imported in `src/core/__init__.py` and `src/__init__.py`
- No imports found in `src/agent/` or `src/orchestrator/`

**Fix:** Either integrate into agent workflow or move to `legacy_system/`.

**Risk Level:** LOW

---

## Unused Environment Variables

**File:** `.env.example`

| Variable | Status | Notes |
|----------|--------|-------|
| `CSV_FILE_PATH` | Unused | Only for legacy csv_processor.py |
| `WORKFLOW_MODE` | Unused | Legacy Streamlit UI setting |
| `ANTHROPIC_API_KEY` | Unused | Alternative provider, not implemented |
| `COHERE_API_KEY` | Unused | Alternative provider, not implemented |

**Fix:** Remove from `.env.example` or add implementation if needed.

**Risk Level:** LOW

---

## API Export Issues

### 6. Deprecated Classes in Public API

**File:** `src/agent/__init__.py`

**Lines:** 15, 32-33, 73-74

**Issue:** Deprecated classes are exported as part of the public API:

```python
from .core.todo import TodoManager as LegacyTodoManager  # Line 15

from .core.context import (
    ProtectedContextConfig,   # DEPRECATED - Line 32
    ProtectedContextProvider, # DEPRECATED - Line 33
)

__all__ = [
    # ...
    'ProtectedContextConfig',   # DEPRECATED - Line 73
    'ProtectedContextProvider', # DEPRECATED - Line 74
]
```

**Impact:** External code (or future developers) might adopt deprecated patterns thinking they're current.

**Fix:** Remove from `__all__` and add deprecation warnings, or remove entirely after migration.

**Risk Level:** MEDIUM

---

## Updated Summary Table

| Category | File Path | Lines | Status | Risk Level |
|----------|-----------|-------|--------|------------|
| Deprecated Module | `core/todo.py` | 1-1431 | DEPRECATED (in use) | HIGH |
| Deprecated Module | `core/transitions.py` | 1-564 | DEPRECATED (in use) | HIGH |
| Dead Code | `todo_tools.py` | 35-65, 237-266 | Global state + transitions | MEDIUM |
| Deprecated Classes | `core/context.py` | 82-128 | No-op stubs | MEDIUM |
| Unused Import | `core/context.py` | 20 | `Tuple` unused | LOW |
| Duplicate Class | `workspace.py` + `loader.py` | 23, 32 | Two WorkspaceConfig | MEDIUM |
| Legacy Persistence | `core/todo.py` | 146, 161, 168, 185-190 | `_legacy_workspace` | MEDIUM |
| Global State | `todo_tools.py` | 35-37 | `_last_transition_result` | MEDIUM |
| Orphaned Module | `core/csv_processor.py` | All | Not used by agents | LOW |
| Unused Env Vars | `.env.example` | Multiple | Dead config keys | LOW |
| Deprecated Exports | `agent/__init__.py` | 15, 32-33, 73-74 | In public API | MEDIUM |
| Orphaned Config | `creator.json` | 95-102 | `protected_context_*` | LOW |
| Orphaned Config | `validator.json` | 95-102 | `protected_context_*` | LOW |
| Legacy Tests | `test_phase_transitions.py` | All | Tests deprecated code | LOW |
| Legacy Tests | `test_guardrails_integration.py` | Partial | Uses deprecated transitions | LOW |

---

## Revised Cleanup Order

1. **Fix imports first** - Update `src/agent/agent.py` to use new TodoManager
2. **Remove dead code in todo_tools.py** - Delete transition-related code and global state
3. **Delete deprecated modules** - Remove `core/todo.py` and `core/transitions.py`
4. **Clean up context.py** - Remove deprecated classes and unused `Tuple` import
5. **Consolidate WorkspaceConfig** - Merge or rename duplicate classes
6. **Update __init__.py** - Remove legacy exports from public API
7. **Clean config files** - Remove orphaned `protected_context_*` settings
8. **Clean .env.example** - Remove unused environment variables
9. **Handle csv_processor** - Move to legacy or integrate
10. **Update tests** - Fix or remove tests for deprecated functionality
