# CitationEngine Rework — Implementation Roadmap

**Parent doc:** [citation_engine_rework.md](citation_engine_rework.md)
**Created:** 2026-02-08

This document breaks down the rework into concrete, file-level implementation tasks.

---

## Current State Summary

**CitationEngine** (`CitationEngine/src/citation_engine/`):
- `engine.py` (~2100 lines) — Main class, all source/citation CRUD, LLM verification
- `models.py` (~300 lines) — Dataclasses: Source, Citation, CitationResult, enums
- `schema.py` (~340 lines) — SQLite + PostgreSQL DDL, migration framework
- `tool.py` (~720 lines) — LangChain tool wrappers (separate from agent tools)
- Dual-mode: SQLite (basic) and PostgreSQL (multi-agent)
- `content_hash` already computed (SHA-256) on source registration but not used for dedup
- `schema_migrations` table exists for versioning (only v1 applied)
- No search, annotations, tags, or embedding functionality

**Agent layer** (`src/tools/citation/sources.py`):
- 6 tools: `cite_document`, `cite_web`, `list_sources`, `get_citation`, `list_citations`, `edit_citation`
- All tactical-only (phase metadata in `CITATION_TOOLS_METADATA`)
- `ToolContext` lazily creates `CitationEngine(mode="multi-agent")` with job_id as session_id
- `_source_registry` caches path/url → source_id to avoid re-registration
- `edit_citation` is async and goes through the agent's `PostgresDB`, NOT through CitationEngine

**Agent DB** (`src/database/`):
- Has its own `sources` and `citations` tables in `schema.sql` with `job_id UUID` FK
- `CitationsNamespace` in `postgres_db.py` handles `edit_citation` directly
- CitationEngine and agent DB are partially overlapping — both have source/citation tables

---

## Phase 1: Shared Source Library & Annotations/Tags

**Goal:** Refactor sources to shared (cross-job) model, add annotations and tags. No new infrastructure needed.

### 1.1 Schema Migration in CitationEngine

**File:** `CitationEngine/src/citation_engine/schema.py`

Add migration v2:
- [ ] Remove `job_id` column from `sources` table (both SQLite and PostgreSQL)
- [ ] Add `UNIQUE` constraint on `sources.content_hash` (dedup key)
- [ ] Create `job_sources` join table:
  ```sql
  job_sources(job_id, source_id, added_at) — PK(job_id, source_id)
  ```
- [ ] Create `source_annotations` table:
  ```sql
  source_annotations(id, source_id FK, job_id, annotation_type, content, page_reference, created_at, created_by)
  ```
- [ ] Create `source_tags` table:
  ```sql
  source_tags(id, source_id FK, job_id, tag, created_at) — UNIQUE(source_id, job_id, tag)
  ```
- [ ] Add FTS indexes (`'simple'` config) on `sources.content` and `source_annotations.content`
- [ ] Write data migration: populate `job_sources` from existing `sources.job_id` before dropping column

**SQLite considerations:** SQLite doesn't support `DROP COLUMN` in older versions. Migration should recreate the table (`CREATE TABLE sources_new ... INSERT INTO sources_new SELECT ... DROP TABLE sources ... ALTER TABLE sources_new RENAME TO sources`).

### 1.2 Source Deduplication in CitationEngine

**File:** `CitationEngine/src/citation_engine/engine.py`

Modify `_register_source()` (the internal method called by all `add_*_source()` methods):
- [ ] Before INSERT, check if `content_hash` already exists in `sources`
- [ ] If exists: reuse the existing `source.id`, create `job_sources` link only
- [ ] If new: INSERT source, then create `job_sources` link
- [ ] Update `add_doc_source()`, `add_web_source()`, `add_db_source()`, `add_custom_source()` to pass job_id for the join table

Modify all query methods to use `job_sources` join:
- [ ] `list_sources()` — `JOIN job_sources ON ...` filtered by job_id
- [ ] `get_source()` — optionally verify job ownership via `job_sources`
- [ ] `get_citations_for_source()` — still works (citations have source_id directly)
- [ ] `list_citations()` — filter by job_id on citations table (unchanged)

### 1.3 New Models

**File:** `CitationEngine/src/citation_engine/models.py`

- [ ] Add `AnnotationType` enum: `note`, `highlight`, `summary`, `question`, `critique`
- [ ] Add `Annotation` dataclass: `id`, `source_id`, `job_id`, `annotation_type`, `content`, `page_reference`, `created_at`, `created_by`
- [ ] Add `Tag` dataclass (or just use `str` — tags are simple strings)

### 1.4 Annotation & Tag Methods in CitationEngine

**File:** `CitationEngine/src/citation_engine/engine.py`

New public methods:
- [ ] `annotate_source(source_id, content, annotation_type="note", page_reference=None) -> Annotation`
  - Validate source exists and belongs to current job (via `job_sources`)
  - Insert into `source_annotations` with job_id from context
- [ ] `get_annotations(source_id, annotation_type=None) -> list[Annotation]`
  - Filter by source_id + job_id, optionally by type
  - Order by created_at DESC
- [ ] `tag_source(source_id, tags: list[str]) -> list[str]`
  - UPSERT tags (INSERT ... ON CONFLICT DO NOTHING)
  - Return current tag list for source
- [ ] `remove_tags(source_id, tags: list[str]) -> list[str]`
  - DELETE matching tags
  - Return remaining tag list
- [ ] `get_tags(source_id) -> list[str]`
  - Return all tags for source in current job

### 1.5 Update CitationEngine Package Exports

**File:** `CitationEngine/src/citation_engine/__init__.py`

- [ ] Export new models: `Annotation`, `AnnotationType`
- [ ] Ensure `annotate_source`, `get_annotations`, `tag_source`, `remove_tags`, `get_tags` are accessible

### 1.6 Agent Tool Wrappers

**File:** `src/tools/citation/sources.py`

Add new tools to `CITATION_TOOLS_METADATA`:
- [ ] `annotate_source` — calls `engine.annotate_source()`, phases: `["strategic", "tactical"]`
- [ ] `get_annotations` — calls `engine.get_annotations()`, phases: `["strategic", "tactical"]`
- [ ] `tag_source` — calls `engine.tag_source()` / `engine.remove_tags()`, phases: `["strategic", "tactical"]`

Implement tool functions inside `create_source_tools()`:
- [ ] `annotate_source(source_id: int, content: str, type: str = "note", page: str | None = None) -> str`
- [ ] `get_annotations(source_id: int, type: str | None = None) -> str`
- [ ] `tag_source(source_id: int, tags: str, action: str = "add") -> str` — `tags` is comma-separated, `action` is "add" or "remove"

### 1.7 Update Phase Metadata for Existing Tools

**File:** `src/tools/citation/sources.py`

- [ ] Change all existing citation tools from `"phases": ["tactical"]` to `"phases": ["strategic", "tactical"]`

### 1.8 Register New Tools

**File:** `src/tools/registry.py`

- [ ] Add new tool metadata entries to `TOOL_REGISTRY` (via `get_citation_metadata()`)
- [ ] Alternatively add a `literature` category to tool config — or keep them in `citation` category

**File:** `config/defaults.yaml`

- [ ] Add new tools to `tools.citation` list (or create `tools.literature` section)

### 1.9 Reconcile Agent DB Schema

**File:** `src/database/queries/postgres/schema.sql`

The agent has its own `sources` and `citations` tables that partially duplicate CitationEngine's. Two options:

**Option A (recommended):** Remove agent-side `sources`/`citations` tables entirely. The agent's `edit_citation` tool (`CitationsNamespace` in `postgres_db.py`) should call CitationEngine instead of going through its own DB.

**Option B:** Keep both and accept the duplication. Simpler short-term but creates drift.

- [ ] Decide on option and document the decision
- [ ] If Option A: migrate `edit_citation` to go through CitationEngine, remove `CitationsNamespace` from `postgres_db.py`, drop redundant tables from agent schema

### 1.10 Tests

**File:** `CitationEngine/tests/`

- [ ] Test shared source dedup: register same file in two jobs, verify single source row + two `job_sources` rows
- [ ] Test annotations CRUD: create, retrieve, filter by type
- [ ] Test tags CRUD: add, remove, get, uniqueness constraint
- [ ] Test FTS on source content (PostgreSQL integration test)
- [ ] Test migration v2 from v1 schema (both SQLite and PostgreSQL)

**File:** `tests/`

- [ ] Test new agent tools: `annotate_source`, `get_annotations`, `tag_source`
- [ ] Test phase availability: verify citation tools work in strategic phase

---

## Phase 2: Vector Search Infrastructure

**Goal:** Enable pgvector, implement embedding service and semantic chunking. No agent-facing tools yet.

### 2.1 PostgreSQL pgvector Setup

**File:** `docker-compose.dev.yaml` (and any production compose files)

- [ ] Add pgvector extension to PostgreSQL image (use `pgvector/pgvector:pg16` or add `CREATE EXTENSION` to init)
- [ ] Verify pgvector works: `SELECT '[1,2,3]'::vector;`

### 2.2 Embeddings Table Schema

**File:** `CitationEngine/src/citation_engine/schema.py`

Add migration v3 (depends on v2):
- [ ] `CREATE EXTENSION IF NOT EXISTS vector;`
- [ ] Create `source_embeddings` table:
  ```sql
  source_embeddings(id, source_id FK, job_id, chunk_index, chunk_text, embedding vector, created_at)
  UNIQUE(source_id, job_id, chunk_index)
  ```
- [ ] Add HNSW index: `USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)`

**Note:** pgvector is PostgreSQL-only. SQLite basic mode won't support vector search. The embedding methods should raise `NotImplementedError` in basic mode.

### 2.3 Embedding Service

**File:** `CitationEngine/src/citation_engine/embeddings.py` (new file)

- [ ] `EmbeddingService` class:
  ```python
  class EmbeddingService:
      def __init__(self, model: str, api_url: str | None, api_key: str | None)
      def embed(self, text: str) -> list[float]
      def embed_batch(self, texts: list[str]) -> list[list[float]]
      @property
      def dimension(self) -> int
  ```
- [ ] OpenAI-compatible API by default (works with OpenAI, Ollama, vLLM, LiteLLM)
- [ ] Env vars: `CITATION_EMBEDDING_MODEL`, `CITATION_EMBEDDING_URL`, `CITATION_EMBEDDING_KEY`
- [ ] Fallback: raise clear error if no API key and no custom URL configured

### 2.4 Semantic Chunking

**File:** `CitationEngine/src/citation_engine/chunking.py` (new file)

- [ ] `SemanticChunker` class:
  ```python
  class SemanticChunker:
      def __init__(self, embedding_service: EmbeddingService, threshold: float = 0.5, window_size: int = 3)
      def chunk(self, text: str) -> list[str]
  ```
- [ ] Algorithm:
  1. Split text into sentences (use regex or nltk sentence tokenizer)
  2. Create overlapping windows of N sentences
  3. Embed each window
  4. Compute cosine similarity between adjacent windows
  5. Split where similarity drops below threshold
  6. Merge very small chunks (< min_chunk_size) with neighbors
- [ ] Configurable parameters: `window_size`, `similarity_threshold`, `min_chunk_size`, `max_chunk_size`
- [ ] Fallback: if embedding service unavailable, fall back to fixed-size splitting (e.g., 512 tokens with 64 overlap)

### 2.5 Auto-Embed on Source Registration

**File:** `CitationEngine/src/citation_engine/engine.py`

Modify `_register_source()` (or a post-registration hook):
- [ ] After source INSERT + `job_sources` link, check if `source_embeddings` exist for this source+job
- [ ] If not: chunk source content, embed chunks, INSERT into `source_embeddings`
- [ ] Use `content_hash` to skip re-embedding if source already embedded for another job (reuse chunks)
- [ ] Handle embedding service unavailable gracefully (log warning, skip embedding, don't fail registration)

New public method:
- [ ] `reindex_source(source_id: int) -> int` — Force re-chunk and re-embed a source. Returns chunk count.

### 2.6 Dependencies

**File:** `CitationEngine/pyproject.toml`

- [ ] Add new optional dependency group:
  ```toml
  [project.optional-dependencies]
  vector = ["pgvector>=0.2.0", "httpx>=0.25.0"]  # httpx for embedding API calls
  ```
- [ ] Update `full` group to include `vector`

### 2.7 Tests

**File:** `CitationEngine/tests/`

- [ ] Test `EmbeddingService` with mocked API responses
- [ ] Test `SemanticChunker` with known text (verify chunk boundaries make sense)
- [ ] Test auto-embedding on source registration (mock embedding service)
- [ ] Test `content_hash` skip logic (register same source twice, verify embeddings not duplicated)
- [ ] Integration test: pgvector round-trip (INSERT embedding, SELECT by cosine similarity)

---

## Phase 3: Unified Search Tool

**Goal:** Implement `search_library` with hybrid retrieval (vector + FTS + RRF) and explainable evidence labels.

### 3.1 Search Backend in CitationEngine

**File:** `CitationEngine/src/citation_engine/search.py` (new file)

- [ ] `SearchResult` dataclass:
  ```python
  @dataclass
  class SearchResult:
      source_id: int
      source_name: str
      source_type: str
      chunk_text: str
      page_reference: str | None
      evidence_label: str          # "HIGH", "MEDIUM", "LOW"
      evidence_reason: str         # "verbatim match", "paraphrase", "single source only", etc.
      score: float                 # Internal RRF score (for ordering, not shown to agent)
  ```

- [ ] `SearchResults` dataclass:
  ```python
  @dataclass
  class SearchResults:
      query: str
      results: list[SearchResult]
      overall_label: str           # Aggregate: "HIGH — 3 sources support this"
      mode: str                    # "hybrid", "keyword", "semantic"
  ```

- [ ] `SearchEngine` class (or methods directly on `CitationEngine`):
  ```python
  def search_library(
      query: str,
      mode: str = "hybrid",
      tags: list[str] | None = None,
      source_type: str | None = None,
      scope: str = "content",      # "content" | "annotations" | "all"
      top_k: int = 10,
  ) -> SearchResults
  ```

### 3.2 Keyword Search Implementation

**File:** `CitationEngine/src/citation_engine/search.py`

- [ ] `_keyword_search(query, job_id, top_k, source_type, tags) -> list[RankedResult]`
  - Use `plainto_tsquery('simple', query)` against `sources.content`
  - JOIN `job_sources` for job scoping
  - Optional JOIN `source_tags` for tag filtering (AND logic)
  - Optional WHERE `sources.type = source_type`
  - Return with `ts_rank` scores
- [ ] Scope="annotations": search `source_annotations.content` instead
- [ ] Scope="all": UNION both queries

### 3.3 Semantic Search Implementation

**File:** `CitationEngine/src/citation_engine/search.py`

- [ ] `_semantic_search(query, job_id, top_k, source_type, tags) -> list[RankedResult]`
  - Embed query via `EmbeddingService`
  - `SELECT ... FROM source_embeddings ... ORDER BY embedding <=> $query_vec LIMIT $top_k`
  - JOIN `sources` for metadata, `job_sources` for scoping
  - Optional tag/type filtering

### 3.4 Hybrid Retrieval (RRF)

**File:** `CitationEngine/src/citation_engine/search.py`

- [ ] `_rrf_merge(keyword_results, semantic_results, k=60, top_k=10) -> list[RankedResult]`
  - Standard RRF formula: `score = sum(1 / (k + rank_i))` for each result list
  - Deduplicate by (source_id, chunk_text) — same chunk can appear in both lists
  - Return top_k by fused score

### 3.5 Evidence Labeling

**File:** `CitationEngine/src/citation_engine/search.py`

- [ ] `_label_evidence(results: list[RankedResult]) -> list[SearchResult]`
  - HIGH: RRF score above threshold AND appears in both keyword + semantic results
  - MEDIUM: appears in one list only, or score in middle range
  - LOW: bottom quartile, or only tangentially related
  - Reason strings: "verbatim match", "paraphrase", "related statute", "single source only", "weak similarity"
- [ ] `_overall_label(results: list[SearchResult]) -> str`
  - "HIGH — 3 sources support this, strongest from ..."
  - "MEDIUM — 1 source found, paraphrase match"
  - "LOW — no strong matches found"

### 3.6 Agent Tool Wrapper

**File:** `src/tools/citation/sources.py`

Add to `CITATION_TOOLS_METADATA`:
- [ ] `search_library` — phases: `["strategic", "tactical"]`, defer_to_workspace: True

Implement tool function:
- [ ] `search_library(query, mode="hybrid", tags=None, source_type=None, scope="content", top_k=10) -> str`
  - Call `engine.search_library(...)`
  - Format results as readable text with evidence labels
  - Handle mode="keyword" gracefully when no embeddings exist
  - Handle mode="semantic" gracefully when embedding service unavailable (fall back to keyword)

### 3.7 Register Tool

**File:** `src/tools/registry.py`, `config/defaults.yaml`

- [ ] Add `search_library` to `TOOL_REGISTRY`
- [ ] Add to default config under `tools.citation` (or `tools.literature`)

### 3.8 Tests

**File:** `CitationEngine/tests/`

- [ ] Test keyword search: register sources with known content, search for terms, verify results
- [ ] Test semantic search: mock embedding service, verify cosine similarity ordering
- [ ] Test hybrid RRF: verify results from both lists are merged correctly
- [ ] Test evidence labeling: known scores → expected labels
- [ ] Test tag filtering: sources with/without matching tags
- [ ] Test scope="annotations": search returns annotation content
- [ ] Test fallback: semantic search when embedding service unavailable → keyword only

**File:** `tests/`

- [ ] Test `search_library` agent tool end-to-end
- [ ] Test search with no results (empty library)
- [ ] Test search with mode parameter variations

---

## Phase 4: Integration & Polish

**Goal:** Connect literature management to the citation workflow. Polish the agent experience.

### 4.1 Citation Workflow Integration

**File:** `src/tools/citation/sources.py`

- [ ] Update `cite_document` and `cite_web` to suggest using `search_library` in their tool descriptions for finding evidence before citing
- [ ] Consider: should `cite_document` auto-tag the source based on claim content? (Probably not — let agent decide)

### 4.2 Tool Documentation

**File:** `src/tools/description_manager.py` (auto-generated docs)

- [ ] Ensure all new tools generate proper workspace markdown docs
- [ ] Include usage examples in tool descriptions showing the search → annotate → cite workflow

### 4.3 Config for Expert Agents

**File:** `config/experts/researcher/config.yaml` (and other expert configs)

- [ ] Add new literature tools to researcher config
- [ ] Add new tools to any config that already uses citation tools
- [ ] Consider which experts benefit from `search_library` (probably all that do research)

### 4.4 Cockpit Integration (Optional)

**File:** `cockpit/` (Angular frontend)

- [ ] Display source annotations in job detail view
- [ ] Display tags as chips on source cards
- [ ] Add search interface for the source library (calls orchestrator → CitationEngine)
- [ ] Show evidence labels visually (color-coded: green/yellow/red for HIGH/MEDIUM/LOW)

### 4.5 Documentation

- [ ] Update `CitationEngine/README.md` with new features
- [ ] Update `CitationEngine/docs/api-reference.md` with new methods
- [ ] Update `CLAUDE.md` tool categories to include `literature`
- [ ] Update `config/README.md` if new tool category added

---

## Dependency Graph

```
Phase 1.1 (schema migration)
  ├── 1.2 (source dedup) ── depends on 1.1
  ├── 1.3 (models) ── independent
  ├── 1.4 (engine methods) ── depends on 1.1, 1.3
  ├── 1.5 (exports) ── depends on 1.3, 1.4
  ├── 1.6 (agent tools) ── depends on 1.4, 1.5
  ├── 1.7 (phase metadata) ── independent
  ├── 1.8 (registry) ── depends on 1.6
  ├── 1.9 (reconcile agent DB) ── depends on 1.1
  └── 1.10 (tests) ── depends on all above

Phase 2.1 (pgvector setup) ── independent
  ├── 2.2 (embeddings table) ── depends on 2.1
  ├── 2.3 (embedding service) ── independent
  ├── 2.4 (semantic chunker) ── depends on 2.3
  ├── 2.5 (auto-embed) ── depends on 1.2, 2.2, 2.3, 2.4
  ├── 2.6 (dependencies) ── independent
  └── 2.7 (tests) ── depends on all above

Phase 3.1 (search models) ── independent
  ├── 3.2 (keyword search) ── depends on 1.1 (schema)
  ├── 3.3 (semantic search) ── depends on 2.2, 2.3
  ├── 3.4 (hybrid RRF) ── depends on 3.2, 3.3
  ├── 3.5 (evidence labels) ── depends on 3.4
  ├── 3.6 (agent tool) ── depends on 3.5
  ├── 3.7 (registry) ── depends on 3.6
  └── 3.8 (tests) ── depends on all above

Phase 4 ── depends on all above
```

---

## New Files Summary

| File | Phase | Purpose |
|------|-------|---------|
| `CitationEngine/src/citation_engine/embeddings.py` | 2 | Embedding service (OpenAI-compatible API) |
| `CitationEngine/src/citation_engine/chunking.py` | 2 | Semantic chunking via sliding window similarity |
| `CitationEngine/src/citation_engine/search.py` | 3 | Unified search: keyword, semantic, hybrid, RRF, evidence labels |

## Modified Files Summary

| File | Phase | Changes |
|------|-------|---------|
| `CitationEngine/src/citation_engine/schema.py` | 1, 2 | Migration v2 (shared sources, annotations, tags), v3 (pgvector, embeddings) |
| `CitationEngine/src/citation_engine/models.py` | 1, 3 | New dataclasses: Annotation, AnnotationType, SearchResult, SearchResults |
| `CitationEngine/src/citation_engine/engine.py` | 1, 2 | Dedup logic, annotations/tags methods, auto-embed hook, search_library() |
| `CitationEngine/src/citation_engine/__init__.py` | 1 | New exports |
| `CitationEngine/pyproject.toml` | 2 | `vector` dependency group (pgvector, httpx) |
| `src/tools/citation/sources.py` | 1, 3 | New tool functions + metadata, phase change to all-phases |
| `src/tools/registry.py` | 1, 3 | Register new tools |
| `config/defaults.yaml` | 1, 3 | Add new tools to default config |
| `docker-compose.dev.yaml` | 2 | pgvector-enabled PostgreSQL image |
