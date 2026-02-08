# CitationEngine Rework: Literature Management & Semantic Search

**Status:** Planning
**Created:** 2026-02-08
**Related:** [Citation Workflow Issues](citation_issues.md), [Workspace Vector Search](vectorization.md)

## Vision

Evolve CitationEngine from a citation-tracking tool into a full **literature management system** for the agent, comparable to tools like Citavi or Zotero. The agent should be able to build, annotate, organize, and semantically search a personal library of sources across jobs.

### What Citavi/Zotero Provide That We Don't

| Feature | Citavi/Zotero | CitationEngine Today |
|---------|--------------|---------------------|
| Store sources with metadata | Yes | Yes (sources table) |
| Create citations | Yes | Yes (citations table) |
| **Annotations/comments on sources** | Yes | No |
| **Highlights / key passages** | Yes | No |
| **Tags / keywords** | Yes | No |
| **Collections / folders** | Yes | No (only job_id isolation) |
| **Full-text search across library** | Yes | Partial (tsvector on claims only) |
| **Semantic / vector search** | No (keyword only) | No |
| **Cross-job source reuse** | N/A | No (sources are per-job) |
| **Reading notes / summaries** | Yes | No |
| Bibliography export | Yes | Yes (harvard, ieee, bibtex, apa) |

---

## Feature Areas

### 1. Annotations & Comments

Allow the agent to attach notes to sources and citations. This is how a researcher builds understanding over time — not just "I cited this" but "here's why this matters and how it connects."

**New database table:**

```sql
CREATE TABLE source_annotations (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,  -- Per-job: same source can have different annotations per job
    annotation_type TEXT NOT NULL DEFAULT 'note',  -- 'note', 'highlight', 'summary', 'question', 'critique'
    content TEXT NOT NULL,
    page_reference TEXT,          -- Optional: specific page/section
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_by TEXT               -- Agent ID
);

CREATE INDEX idx_annotations_source ON source_annotations(source_id);
CREATE INDEX idx_annotations_job ON source_annotations(job_id);
CREATE INDEX idx_annotations_type ON source_annotations(annotation_type);
```

**Agent tools:**

| Tool | Purpose |
|------|---------|
| `annotate_source` | Add a note, highlight, summary, question, or critique to a source |
| `get_annotations` | Retrieve all annotations for a source (optionally filtered by type) |

**Use cases:**
- Agent reads a PDF, highlights key passages with `annotate_source(source_id, type="highlight", content="...", page="12")`
- Agent writes a summary after reading: `annotate_source(source_id, type="summary", content="This paper argues...")`
- Agent flags a question for later: `annotate_source(source_id, type="question", content="Does this contradict Source [3]?")`
- All tools are available in both strategic and tactical phases — the agent can review its library while planning

### 2. Tags

Flat tags for categorizing sources. Simple, no hierarchy — the agent can use search + tags together for any level of organization. Collections/folders were considered but dropped: an agent doesn't need visual folder structure, tags + search cover the same use cases with less complexity.

**New database table:**

```sql
CREATE TABLE source_tags (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,  -- Per-job: same source can have different tags per job
    tag TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(source_id, job_id, tag)
);

CREATE INDEX idx_tags_tag ON source_tags(tag);
CREATE INDEX idx_tags_job ON source_tags(job_id);
```

**Agent tool:**

| Tool | Purpose |
|------|---------|
| `tag_source` | Add or remove tags on a source |

Tag-based filtering is handled through the unified `search_library` tool (see [Section 4](#4-unified-search)).

### 3. Full-Text Search Infrastructure

Extend the existing tsvector indexes to cover source content and annotations. Uses `'simple'` text search config (language-agnostic) since sources may be German, English, or mixed.

**Schema additions:**

```sql
CREATE INDEX idx_sources_content_fts ON sources
    USING GIN (to_tsvector('simple', content));

CREATE INDEX idx_annotations_content_fts ON source_annotations
    USING GIN (to_tsvector('simple', content));
```

### 4. Unified Search

All search functionality is exposed through a **single `search_library` tool** instead of separate keyword/semantic/evidence tools. This keeps the tool surface small while giving the agent full flexibility.

#### The `search_library` Tool

```python
search_library(
    query: str,                    # Natural language query or keyword
    mode: str = "hybrid",          # "hybrid" | "keyword" | "semantic"
    tags: list[str] | None = None, # Filter by tags (AND logic)
    source_type: str | None = None,# Filter: "document" | "website" | ...
    scope: str = "content",        # "content" | "annotations" | "all"
    top_k: int = 10,               # Max results
) -> SearchResults
```

**Return format — explainable labels, not raw scores:**

```
Evidence: HIGH — 3 sources support this, strongest match from "GoBD-Handbuch.pdf" p.42
  [1] "Aufbewahrungsfrist beträgt 10 Jahre..." (GoBD-Handbuch.pdf, p.42) — HIGH
  [2] "Retention period for tax documents..." (GDPR-Compliance-Guide.pdf, p.15) — MEDIUM (paraphrase)
  [3] "§ 147 AO defines retention..." (AO-Commentary.pdf, p.201) — MEDIUM (related statute)
```

Evidence labels with reasons:
- **HIGH** — verbatim or near-verbatim match, multiple corroborating sources
- **MEDIUM** — paraphrase, related but not exact, or single source only
- **LOW** — tangentially related, weak similarity, or source may be outdated

#### Why One Tool?

The prior art (PaperQA2, scienceOS) shows that fewer, more powerful tools work better for agents. Three separate search tools (`search_sources`, `search_literature`, `find_evidence`) would force the agent to choose which to call — when in practice it almost always wants hybrid search. The `mode` parameter covers edge cases where the agent explicitly wants keyword-only or semantic-only.

The common `find_evidence(claim)` pattern from [citation_issues.md](citation_issues.md) is simply `search_library(query=claim, mode="hybrid")`.

#### Vector Search Infrastructure

PostgreSQL supports vector search via the [pgvector](https://github.com/pgvector/pgvector) extension. Already in `requirements.txt` as a dependency but not enabled.

See [citation_issues.md](citation_issues.md) for research background on Dense X Retrieval and hybrid retrieval strategies.

**Schema:**

```sql
-- Enable extension (requires superuser, done once)
CREATE EXTENSION IF NOT EXISTS vector;

-- Source content embeddings (chunked)
-- Dimension depends on configured model: text-embedding-3-small = 1536, all-MiniLM-L6-v2 = 384
CREATE TABLE source_embeddings (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,  -- Per-job: allows different chunking/embedding per job
    chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_text TEXT NOT NULL,
    embedding vector,             -- Dimension set at insert time, varies by model
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(source_id, job_id, chunk_index)
);

CREATE INDEX idx_source_embeddings_source ON source_embeddings(source_id);
CREATE INDEX idx_source_embeddings_job ON source_embeddings(job_id);

-- HNSW index: better than IVFFlat for our scale (hundreds to low thousands of chunks)
-- IVFFlat needs ~4000+ rows to be effective; HNSW works well at any scale
CREATE INDEX idx_source_embeddings_vector ON source_embeddings
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

**Embedding strategy:**
- Chunk source content on registration (when `add_doc_source` / `add_web_source` is called)
- Use `content_hash` to skip re-embedding unchanged sources (incremental indexing)
- Configurable embedding backend via env vars (see [Design Decisions #2](#design-decisions)):
  - `CITATION_EMBEDDING_MODEL` — model name (default: `text-embedding-3-small`)
  - `CITATION_EMBEDDING_URL` — optional custom endpoint (OpenAI-compatible, e.g. Ollama)
  - `CITATION_EMBEDDING_KEY` — API key (defaults to `OPENAI_API_KEY`)
- Store chunk text alongside embedding for retrieval without join
- Vector dimension is not hardcoded — varies by model (1536 for text-embedding-3-small, 384 for MiniLM, etc.)

**Chunking strategy: semantic chunking.**
Uses the embedding model itself to detect topic shifts. Embed overlapping windows of text, split where cosine similarity between adjacent windows drops below a threshold. This naturally respects paragraph boundaries, section breaks, and topic changes without parsing document structure. Slower than fixed-size splitting (needs embedding calls at index time), but indexing only happens once per source and is not latency-sensitive.

#### Hybrid Retrieval

As noted in [citation_issues.md](citation_issues.md), pure vector search isn't enough for compliance work. Combine:

1. **Dense retrieval** (pgvector) — semantic similarity for paraphrased or conceptually related content
2. **Sparse retrieval** (tsvector/BM25) — keyword precision for regulatory codes, section numbers, entity names

Fuse results using Reciprocal Rank Fusion (RRF):

```python
def hybrid_search(query: str, job_id: str, top_k: int = 10):
    # 1. Vector search
    embedding = embed(query)
    vector_results = pgvector_search(embedding, job_id, top_k=top_k * 2)

    # 2. Full-text search
    fts_results = fulltext_search(query, job_id, top_k=top_k * 2)

    # 3. Reciprocal Rank Fusion
    return rrf_merge(vector_results, fts_results, top_k=top_k)
```

**Reranking** (future enhancement): A cross-encoder reranker could be added as an optional post-RRF step to further improve precision. Skipped for v1 — hybrid search is already a large improvement over the current state (no search at all). Can be revisited if precision proves insufficient.

### 5. Shared Source Library

Sources are shared across jobs; all project-specific metadata is per-job.

#### Shared Source Library Schema

The `sources` table becomes a global library. When the agent registers a source, CitationEngine checks `content_hash` — if the source already exists, it reuses it instead of duplicating. A `job_sources` join table links jobs to their sources.

**Schema changes to existing `sources` table:**

```sql
-- sources table: REMOVE job_id FK, add deduplication
CREATE TABLE sources (
    id SERIAL PRIMARY KEY,
    type source_type NOT NULL,        -- document | website | database | custom
    identifier TEXT NOT NULL,         -- path/URL
    name TEXT NOT NULL,
    version TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE, -- Dedup key
    metadata JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_sources_identifier ON sources(identifier);
CREATE INDEX idx_sources_content_hash ON sources(content_hash);

-- Join table: which jobs use which sources
CREATE TABLE job_sources (
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (job_id, source_id)
);
```

**Per-job tables remain scoped:** `citations`, `source_annotations`, `source_tags`, `source_embeddings` all keep their `job_id` FK. This means:
- The same PDF can be used by 5 different jobs without storing the content 5 times
- Each job has its own annotations, tags, and citations for that source
- Deleting a job removes its metadata but preserves the source for other jobs

**Impact on existing tools:**
- `list_sources` filters via `job_sources` join instead of `sources.job_id`
- `add_doc_source` / `add_web_source` check `content_hash` first, create `job_sources` link if source exists
- `get_citation` still works — citations already reference `source_id` directly

---

## Implementation Roadmap

All core logic lives in **CitationEngine**. Agent layer only adds thin tool wrappers.

### Phase 1: Schema Rework & Annotations/Tags

Refactor sources to shared library model, add literature management tables. No new infrastructure beyond PostgreSQL.

1. **Shared source library:** Remove `job_id` from `sources` table, add `content_hash UNIQUE`, create `job_sources` join table
2. **Migrate existing data:** Update existing sources to populate `content_hash`, create `job_sources` entries from current `sources.job_id` values
3. Add `source_annotations` and `source_tags` tables
4. Add full-text indexes (`'simple'` config) on source content and annotations
5. Implement CitationEngine methods: `annotate_source()`, `get_annotations()`, `tag_source()`
6. Implement agent tools as thin wrappers (`annotate_source`, `get_annotations`, `tag_source`), register under `literature` category in `TOOL_REGISTRY`
7. Update all existing citation tools to use `job_sources` join instead of `sources.job_id`
8. Mark all literature + citation tools as available in all phases (remove tactical-only restriction)

### Phase 2: Vector Search Infrastructure

Depends on deploying an embedding model. Builds on [vectorization.md](vectorization.md) infrastructure plan.

1. Enable `pgvector` extension in PostgreSQL (docker-compose update)
2. Add `source_embeddings` table with HNSW index
3. Implement configurable embedding service in CitationEngine (OpenAI-compatible API by default, custom endpoint via env vars)
4. Hook into `add_doc_source` / `add_web_source` to auto-embed + chunk on registration
5. Use `content_hash` to skip re-embedding unchanged sources (incremental indexing)
6. Implement semantic chunking (sliding window similarity, see [Design Decision #7](#design-decisions))

### Phase 3: Unified Search Tool

1. Implement `search_library()` in CitationEngine with `mode` parameter (hybrid/keyword/semantic)
2. Implement hybrid retrieval backend (vector + full-text + RRF fusion)
3. Implement explainable evidence labels (HIGH/MEDIUM/LOW with reasons)
4. Add `search_library` agent tool wrapper with tag/type/scope filters
5. Test with real compliance documents (mixed German/English)

### Phase 4: Integration with Citation Workflow

Connect the new literature management features to the existing citation workflow improvements from [citation_issues.md](citation_issues.md):

- `search_library` feeds directly into `cite_document` — agent finds evidence semantically, then creates a verified citation
- Annotations serve as the agent's "reading notes" during any phase
- Tags help the agent organize sources during multi-document research jobs
- The [academic workflow](citation_issues.md#option-e-academic-workflow--plan-with-citations-refresh-before-writing--recommended) (plan-refresh-write) benefits from semantic search: agent can refresh context by searching for relevant passages rather than re-reading entire documents

---

## Prior Art & Lessons from Existing Systems

Research into existing projects that have built similar literature management / citation-aware systems for AI agents reveals several recurring patterns and hard-won lessons.

### Research Agent Systems

**[OpenScholar](https://arxiv.org/abs/2411.14199)** (UW / Allen AI) — An open-source LLM for scientific literature synthesis backed by 45M papers and 236M passage embeddings. Key finding: GPT-4o hallucinated citations 78-90% of the time, while OpenScholar essentially eliminated citation hallucination by coupling a domain-specific datastore with iterative self-feedback. **Lesson: raw retriever + reranker pipeline is critical — similarity search alone is not enough.**

**[PaperQA2](https://github.com/Future-House/paper-qa)** (FutureHouse) — First AI agent to achieve superhuman performance on scientific literature search. Uses a multi-LLM architecture with separate models for answer generation, contextual summarization, and agent reasoning. Default storage is OpenAI embeddings + Numpy vector DB (surprisingly effective for local use). Has **Zotero integration** via `paperqa.contrib.ZoteroDB`. Includes a **citation graph traversal tool** that checks papers cited *by* a relevant paper. **Lesson: start simple (Numpy vector DB works), iterative refinement loop matters more than initial retrieval quality, citation graph traversal is a powerful tool most systems miss.**

**[GPT Researcher](https://github.com/assafelovic/gpt-researcher)** — Autonomous deep research agent using planner/execution/publisher agent architecture. **Lesson: citation management is easier when baked into the architecture from the start, not bolted on afterward.**

### AI-Enhanced Reference Managers

**[Anara](https://anara.com/)** — Reference manager combining traditional Citavi/Zotero-style features (import, format, bibliography) with AI agents. Has a `@Research` agent that searches personal library + academic databases + web simultaneously. Every AI-generated claim links to exact passages via clickable highlighting. **Lesson: users need both traditional library management (tags, folders, bibliography) AND AI-powered semantic search — neither alone is sufficient.**

**[scienceOS](https://www.scienceos.ai/)** — AI reference manager built on 230M+ papers from Semantic Scholar with 800M full-text snippets. Multi-tool AI agent with simultaneous access to search, PDF, and library tools. Library size limit of 4,000 items. **Lesson: scaling full semantic indexing over user-uploaded content is resource-intensive; the agent having simultaneous access to multiple tools is critical.**

**[LLM-RAG-Zotero](https://github.com/deulofeu1/LLM-RAG-Zotero)** — Open-source bridge that turns an existing Zotero library into an LLM knowledge base via RAGFlow. Supports incremental processing (only new/changed items). **Lesson: building on existing reference managers reduces adoption friction; incremental sync is essential — full re-indexing does not scale.**

### Agent Memory Systems

**[Mem0](https://docs.mem0.ai/open-source/features/graph-memory)** — Persistent memory layer combining vector search + graph database (Neo4j/Memgraph). Extracts entities and relationships from every memory write. Uses LLMs during updates to detect conflicts between new and existing knowledge. **Lesson: vector + graph enables multi-hop reasoning that pure vector search cannot achieve.**

**[Zep / Graphiti](https://github.com/getzep/graphiti)** ([paper](https://arxiv.org/abs/2501.13956)) — Temporal knowledge graph for agent memory with a **bi-temporal model**: every graph edge tracks both when an event occurred and when it was ingested. Hybrid search (embeddings + BM25 + graph traversal) achieves P95 latency of 300ms *without* LLM calls at retrieval time. **Lesson: temporal tracking (when was this source added? is it still current?) is essential; avoiding LLM calls during retrieval is critical for performance.**

**[A-MEM](https://arxiv.org/abs/2502.12110)** ([GitHub](https://github.com/agiresearch/A-mem)) — **Zettelkasten-inspired** memory system where the agent dynamically organizes memories into interconnected knowledge networks. Each memory unit is enriched with LLM-generated keywords, tags, and contextual descriptions. New memories can trigger updates to existing memories. **Lesson: structured memory units (not raw text blobs) are essential; the Zettelkasten approach naturally supports citation-like attribution because each "note" maintains provenance.**

**[Letta / MemGPT](https://github.com/letta-ai/letta)** — Stateful agents with tiered memory: core (always in context), archival (pgvector), recall (conversation history). Agents self-manage their memory using tools. **Lesson: the core/archival split maps naturally to "current working set" vs. "full library." Letting the agent manage its own memory outperforms external heuristics.**

### Citation-Aware RAG Building Blocks

**[R2R](https://github.com/SciPhi-AI/R2R)** (SciPhi) — Production agentic RAG using **PostgreSQL + pgvector as unified backend**. Hybrid search: full-text + vector similarity + Reciprocal Rank Fusion. Emits `CitationEvent` objects during streaming. Supports both HNSW (faster queries) and IVF-Flat (faster builds). **Lesson: PostgreSQL + pgvector is a viable single-database solution for citation management + vector search + metadata. This validates our planned approach.**

**[Tensorlake Citation-Aware RAG](https://www.tensorlake.ai/blog/rag-citations)** — Stores spatial anchors (bounding boxes) from document parsing all the way through to final citation. ~10-15% additional storage overhead for citation metadata. **Lesson: citation support must be designed into the ingestion pipeline, not added afterward. Bounding box metadata is lightweight but enables "click to verify."**

**[Buzzi.ai Citation Architecture](https://www.buzzi.ai/insights/ai-document-retrieval-rag-citation-architecture)** — Separates **retrieval confidence** (retriever scores, reranker margins) from **answer groundedness** (claim coverage by citations, contradiction signals). Uses explainable labels (High/Medium/Low evidence) instead of opaque scores. **Lesson: two separate confidence dimensions; explainable labels are more actionable than raw numeric scores.**

**[Anthropic Citations API](https://docs.anthropic.com/en/docs/build-with-claude/citations)** — Native citation support in Claude API with automatic sentence-level chunking. 15% recall improvement over prompt-based approaches. **Lesson: model-native citation beats prompt engineering; sentence-level granularity is the right default.**

**[rag-citation](https://github.com/rahulanand1103/rag-citation)** — Lightweight, non-LLM citation library using spaCy NER + SentenceTransformers (similarity threshold 0.88). Includes hallucination detection via entity matching. **Lesson: you don't always need an LLM for citation management — embeddings + NER can handle many cases at a fraction of the cost.**

### Cross-Cutting Patterns

| Pattern | Evidence | Relevance to CitationEngine |
|---------|----------|-----------------------------|
| **Hybrid search is table stakes** | R2R, Buzzi.ai, Zep, every production system | Validates our planned RRF fusion approach |
| **Citations must be designed at ingestion** | Tensorlake, Anthropic | We should embed+index on `add_doc_source`, not later |
| **Separate generation from validation** | NVIDIA NIM, OpenScholar, [citation_issues.md](citation_issues.md) | Aligns with our existing verification pipeline |
| **Temporal metadata matters** | Zep/Graphiti bi-temporal model | Track when sources were added, last accessed, updated |
| **Agent self-managed memory > external heuristics** | Letta, A-MEM | Let the agent tag/annotate/organize, don't automate it |
| **Zettelkasten-style structured notes** | A-MEM | Our annotations should be rich (typed, linked, tagged) not flat text |
| **pgvector is the sweet spot for PostgreSQL shops** | R2R, Letta | We're already on PostgreSQL — no need for a separate vector DB |
| **Prompt-only citation fails at scale** | OpenScholar (78-90% hallucination) | Validates our tool-based approach |
| **Incremental indexing, not full re-index** | LLM-RAG-Zotero | Use `content_hash` to skip unchanged sources |

### What Consistently Did NOT Work

- **Prompt-only citation** — Relying solely on prompt engineering for citation accuracy fails at scale
- **Keyword-only search** — Insufficient for semantic citation discovery
- **Full re-indexing on every update** — Must support incremental/delta processing
- **Single retrieval strategy** — Pure vector search consistently underperforms hybrid approaches
- **Opaque confidence scores** — Explainable labels (High/Medium/Low with reasons) beat raw numbers

---

## Design Decisions

1. **Where does the code live?** Core logic + database models live in **CitationEngine**. The agent layer (`src/tools/`) only implements thin tool wrappers that call CitationEngine methods — same pattern as the existing citation tools. CitationEngine owns the schema, the embedding service, the search logic.

2. **Embedding model.** **Configurable from the start.** Default to an OpenAI-compatible model (e.g. `text-embedding-3-small`), but allow a custom endpoint URL + model name for self-hosted models (Ollama, vLLM, etc.). Env vars:
   - `CITATION_EMBEDDING_MODEL` — model name (default: `text-embedding-3-small`)
   - `CITATION_EMBEDDING_URL` — optional custom endpoint (OpenAI-compatible API)
   - `CITATION_EMBEDDING_KEY` — API key (defaults to `OPENAI_API_KEY`)

3. **Cross-job source sharing.** **Sources are shared across jobs; metadata is per-job.** The `sources` table becomes a shared library (no `job_id` FK), deduplicated by `content_hash`. A `job_sources` join table links jobs to their sources. Citations, annotations, tags, and embeddings remain scoped to a job. See [updated schema below](#shared-source-library-schema).

4. **Phase access.** **All literature/citation tools are available in all phases.** No strategic/tactical filtering for these tools — the agent should be able to search, annotate, and cite at any time.

5. **Tool consolidation.** **One unified `search_library` tool** with `mode`, `tags`, `scope` parameters instead of 3 separate search tools. **Tags only, no collections** — the agent doesn't need visual folder hierarchy; tags + search cover the same use cases with less complexity. Total new tools: `annotate_source`, `get_annotations`, `tag_source`, `search_library` (4 new tools, down from the original 10).

6. **Reranking.** **Skip for v1.** Hybrid search (vector + FTS + RRF) is already a major improvement. Reranking can be added later as an optional post-RRF step if precision proves insufficient.

7. **Chunking strategy.** **Semantic chunking.** Use the embedding model to detect topic shifts via sliding window similarity. Naturally respects document structure without explicit parsing. Slower at index time but indexing only happens once per source.

8. **Confidence output.** **Explainable labels** (HIGH/MEDIUM/LOW with reasons) instead of raw similarity scores. Follows Buzzi.ai's finding that labeled evidence is more actionable for both agents and humans.

## Open Questions

No major open questions remain. All design decisions have been resolved — see [Design Decisions](#design-decisions) above.

Minor implementation details to figure out during development:
- Semantic chunking parameters: window size, overlap, similarity drop threshold
- Exact evidence label thresholds (what cosine similarity = HIGH vs MEDIUM vs LOW)
- Whether `search_library` with `scope="annotations"` should also return the source metadata or just the annotation text

---

## References

### Internal
- [Citation Workflow Issues & Research Findings](citation_issues.md) — current problems and academic research on citation approaches
- [Workspace Vector Search](vectorization.md) — planned pgvector infrastructure for workspace files

### Research Agent Systems
- [OpenScholar](https://arxiv.org/abs/2411.14199) — open-source scientific literature LLM (UW / Allen AI)
- [PaperQA2](https://github.com/Future-House/paper-qa) — superhuman scientific literature search agent (FutureHouse)
- [GPT Researcher](https://github.com/assafelovic/gpt-researcher) — autonomous deep research agent

### AI-Enhanced Reference Managers
- [Anara](https://anara.com/) — AI reference manager with agentic search and source tracing
- [scienceOS](https://www.scienceos.ai/) — AI reference manager built on Semantic Scholar
- [LLM-RAG-Zotero](https://github.com/deulofeu1/LLM-RAG-Zotero) — Zotero-to-RAG bridge via RAGFlow
- [Citavi](https://www.citavi.com/) — reference management and knowledge organization
- [Zotero](https://www.zotero.org/) — open-source reference management

### Agent Memory Systems
- [Mem0](https://docs.mem0.ai/open-source/features/graph-memory) — vector + graph persistent memory for agents
- [Zep / Graphiti](https://github.com/getzep/graphiti) — temporal knowledge graph for agent memory ([paper](https://arxiv.org/abs/2501.13956))
- [A-MEM](https://arxiv.org/abs/2502.12110) — Zettelkasten-inspired agentic memory ([GitHub](https://github.com/agiresearch/A-mem))
- [Letta / MemGPT](https://github.com/letta-ai/letta) — stateful agents with self-editing tiered memory
- [Cognee](https://github.com/topoteretes/cognee) — ECL pipeline for AI memory

### Citation-Aware RAG
- [R2R](https://github.com/SciPhi-AI/R2R) — production agentic RAG with PostgreSQL + pgvector unified backend
- [Tensorlake Citation-Aware RAG](https://www.tensorlake.ai/blog/rag-citations) — spatial anchors for document citations
- [Buzzi.ai Citation Architecture](https://www.buzzi.ai/insights/ai-document-retrieval-rag-citation-architecture) — dual confidence model blueprint
- [Anthropic Citations API](https://docs.anthropic.com/en/docs/build-with-claude/citations) — native model-level citation support
- [rag-citation](https://github.com/rahulanand1103/rag-citation) — lightweight non-LLM citation library

### Infrastructure
- [pgvector](https://github.com/pgvector/pgvector) — PostgreSQL vector similarity search extension
- [sentence-transformers](https://www.sbert.net/) — local embedding models
- [Graphlit Agent Memory Survey](https://www.graphlit.com/blog/survey-of-ai-agent-memory-frameworks) — comprehensive comparison of memory frameworks
