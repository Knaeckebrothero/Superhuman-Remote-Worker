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
