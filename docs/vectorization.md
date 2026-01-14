# Workspace Vector Search - Future Feature

**Status:** Not implemented (infrastructure pending)
**Priority:** Low (enhancement, not core functionality)
**Created:** 2026-01-09

## Overview

This document describes a planned feature to add semantic search capabilities to agent workspaces. The feature would allow agents to search workspace files by meaning rather than exact text matches.

## Motivation

Currently, agents can search workspace files using:
- `list_files(pattern)` - glob patterns for filenames
- `search_files(query, pattern)` - regex/text search within file contents

These are sufficient for exact matches but struggle with:
- Finding conceptually related content when exact wording is unknown
- Discovering relevant sections across many files
- Natural language queries like "GoBD retention requirements"

Semantic search using vector embeddings would address these limitations.

## Proposed Design

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Agent Workspace                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │ chunks/  │  │ notes/   │  │ plans/   │  │ output/  │        │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘        │
│       │             │             │             │               │
│       └─────────────┴─────────────┴─────────────┘               │
│                           │                                      │
│                    ┌──────▼──────┐                              │
│                    │  Embedding  │                              │
│                    │   Model     │                              │
│                    └──────┬──────┘                              │
│                           │                                      │
│                    ┌──────▼──────┐                              │
│                    │  pgvector   │                              │
│                    │  (vectors)  │                              │
│                    └─────────────┘                              │
└─────────────────────────────────────────────────────────────────┘
```

### Database Schema

Would use PostgreSQL with pgvector extension:

```sql
-- Requires: CREATE EXTENSION vector;

CREATE TABLE workspace_embeddings (
    id SERIAL PRIMARY KEY,
    job_id UUID NOT NULL,
    file_path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    embedding vector(384),  -- Dimension depends on model
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(job_id, file_path, chunk_index)
);

CREATE INDEX idx_workspace_embeddings_job ON workspace_embeddings(job_id);
CREATE INDEX idx_workspace_embeddings_vector ON workspace_embeddings
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### Proposed Tools

Three tools were designed for agent use:

#### 1. `semantic_search`
```python
semantic_search(
    query: str,           # Natural language query
    path_filter: str,     # Optional SQL LIKE pattern (e.g., "chunks/%")
    top_k: int            # Max results (default: 10)
) -> str
```

Search workspace files by meaning. Returns ranked results with similarity scores.

**Example:**
```
semantic_search("compliance requirements for data retention")
semantic_search("how to handle personal data", path_filter="notes/%")
```

#### 2. `index_file_for_search`
```python
index_file_for_search(path: str) -> str
```

Manually trigger (re-)indexing of a specific file. Files would normally be indexed automatically on write.

#### 3. `get_vector_index_stats`
```python
get_vector_index_stats() -> str
```

Get statistics about indexed files and chunks for the current workspace.

### Embedding Backend Options

The feature requires an embedding model. Options:

| Option | Pros | Cons |
|--------|------|------|
| **OpenAI API** | High quality, easy setup | Costs money, requires API key, data leaves local |
| **Local sentence-transformers** | Free, private, no API key | Requires GPU or slow on CPU, setup complexity |
| **Ollama embeddings** | Local, integrates with existing LLM setup | Need to run embedding model |
| **Cohere/Voyage API** | High quality embeddings | Costs money, requires API key |

**Recommended for this project:** Local sentence-transformers with `all-MiniLM-L6-v2` model (384 dimensions, fast, good quality).

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')
embedding = model.encode("text to embed")  # Returns 384-dim vector
```

## Implementation Requirements

### Infrastructure
1. PostgreSQL with pgvector extension enabled
2. Embedding model accessible (local or API)
3. Environment variable for embedding config

### Code Changes
1. Create `src/agents/shared/vector_store.py` - Vector store implementation
2. Create `src/agents/shared/tools/vector_tools.py` - Agent tools
3. Update `ToolContext` to accept optional vector store
4. Update tool registry to include vector tools
5. Add vector tool category to agent configs

### Configuration

Would add to `.env`:
```bash
# Vector search (optional)
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
# Or for API:
# OPENAI_API_KEY=sk-...  (already exists, would reuse)
```

Would add to agent config:
```json
{
  "tools": {
    "categories": ["workspace", "todo", "vector", "domain"]
  }
}
```

## Why Not Implemented Yet

1. **No embedding backend deployed** - Homelab doesn't have an embedding model running
2. **pgvector not enabled** - PostgreSQL needs the extension installed
3. **Core functionality first** - The agent needs to work reliably before adding enhancements
4. **Scope creep risk** - Feature is useful but not essential for requirement extraction

## Implementation Plan (Future)

When ready to implement:

1. **Phase 1: Infrastructure**
   - Install pgvector extension in PostgreSQL
   - Deploy embedding model (sentence-transformers or via Ollama)
   - Add schema migration for `workspace_embeddings` table

2. **Phase 2: Backend**
   - Implement `VectorStore` class with embed/search/index methods
   - Add automatic indexing on file write (hook into `write_file` tool)
   - Add chunking logic for large files

3. **Phase 3: Tools**
   - Implement `semantic_search`, `index_file_for_search`, `get_vector_index_stats`
   - Register in tool registry
   - Add to agent configs

4. **Phase 4: Testing**
   - Test with real workspace content
   - Tune similarity thresholds
   - Benchmark query performance

## Related Files (Removed)

The following files contained partial/stub implementation and were removed during cleanup:

- `src/agents/shared/vector.py` - Deleted (Issue #9)
- `src/agents/shared/tools/vector_tools.py` - Deleted (this cleanup)
- `src/database/schema_vector.sql` - Deleted (Issue #9)

## References

- [pgvector GitHub](https://github.com/pgvector/pgvector)
- [sentence-transformers](https://www.sbert.net/)
- [LangChain vector stores](https://python.langchain.com/docs/modules/data_connection/vectorstores/)
