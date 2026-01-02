# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Citation & Provenance Engine - a structured citation system for AI agents that forces articulation of claim-to-source relationships and enables LLM-based verification. Designed to solve the "hallucination problem" by requiring agents to explicitly register citations with supporting evidence.

## Development Commands

```bash
# Install for development
pip install -e ".[dev]"

# Install with all features (PDF, web, langchain, postgresql)
pip install -e ".[full]"

# Run tests
pytest tests/ -v

# Run single test file
pytest tests/test_engine.py -v

# Run with coverage
pytest tests/ -v --cov=src/citation_engine

# Format code
ruff format src/ tests/

# Lint
ruff check src/ tests/
```

## Architecture

The engine has a layered design:

```
CitationEngine (engine.py)
    │
    ├── Source Registration: add_doc_source(), add_web_source(), add_db_source(), add_custom_source()
    │       └── Content extraction (PyMuPDF for PDFs, BeautifulSoup for web)
    │
    ├── Citation Creation: cite_doc(), cite_web(), cite_db(), cite_custom()
    │       └── Synchronous LLM verification via _verify_citation()
    │
    ├── Database Layer: SQLite (basic mode) or PostgreSQL (multi-agent mode)
    │       └── Schema defined in schema.py
    │
    └── LangChain Tools: create_citation_tools() in tool.py
            └── cite(), register_source(), list_sources(), get_citation_status()
```

**Key design decisions:**
- Synchronous verification (agent waits for verification before continuing)
- Citations are immutable (append-only, use `supersedes` for corrections)
- Sources registered at load time, not lazily on first cite
- Each citation is a separate record, even for same quote backing multiple claims

## Data Flow

1. Agent receives documents → calls `add_*_source()` → content extracted and stored
2. Agent makes claim → calls `cite_*()` → citation stored with `pending` status
3. Verification LLM checks quote exists and supports claim → status updated to `verified`/`failed`
4. Agent receives `CitationResult` with citation ID (e.g., `[1]`) to embed in prose

## Operation Modes

| Mode | Database | Use Case |
|------|----------|----------|
| `basic` (default) | SQLite | Single-agent, local development |
| `multi-agent` | PostgreSQL | Shared citation pool, production |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `CITATION_DB_PATH` | SQLite path (basic mode) |
| `CITATION_DB_URL` | PostgreSQL connection (multi-agent mode) |
| `CITATION_LLM_URL` | Custom LLM endpoint (llama.cpp, vLLM, Ollama) |
| `CITATION_LLM_MODEL` | Model for verification (default: `gpt-4o-mini`) |
| `OPENAI_API_KEY` | OpenAI API key |
| `CITATION_REASONING_REQUIRED` | When `relevance_reasoning` required: `none`, `low` (default), `medium`, `high` |

## Testing Pattern

Tests mock `_verify_citation()` to avoid LLM calls. Use `VerificationResult` directly or ensure `matched_location` is a proper dict (not MagicMock):

```python
from citation_engine.models import VerificationResult

@patch.object(CitationEngine, "_verify_citation")
def test_cite_doc(self, mock_verify, engine, sample_text_file):
    mock_verify.return_value = VerificationResult(
        is_verified=True,
        similarity_score=0.95,
        reasoning="Quote found in source",
        matched_location={"page": 1},  # Must be dict, not MagicMock
    )
    # ... test code
```

## Source Types

| Type | Registration | Content Extraction |
|------|--------------|-------------------|
| `document` | `add_doc_source(file_path)` | PyMuPDF for PDF, direct read for txt/md/json |
| `website` | `add_web_source(url)` | BeautifulSoup, archived at registration |
| `database` | `add_db_source(identifier, content)` | Content provided by caller |
| `custom` | `add_custom_source(name, content)` | Agent-generated artifacts (matrices, analysis) |
