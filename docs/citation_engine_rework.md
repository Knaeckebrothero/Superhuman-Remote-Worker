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
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
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
- During the strategic phase review, agent can recall its own annotations to inform plan updates

### 2. Tags & Collections

Categorize sources for structured browsing and retrieval. Tags are flat labels, collections are hierarchical folders.

**New database tables:**

```sql
CREATE TABLE source_tags (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(source_id, job_id, tag)
);

CREATE INDEX idx_tags_tag ON source_tags(tag);
CREATE INDEX idx_tags_job ON source_tags(job_id);

CREATE TABLE collections (
    id SERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(job_id, name, parent_id)
);

CREATE TABLE collection_sources (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, source_id)
);
```

**Agent tools:**

| Tool | Purpose |
|------|---------|
| `tag_source` | Add one or more tags to a source |
| `search_by_tag` | Find sources matching given tags |
| `create_collection` | Create a named collection (optionally nested) |
| `add_to_collection` | Add a source to a collection |
| `browse_collection` | List sources in a collection |

**Use cases:**
- `tag_source(source_id=3, tags=["GoBD", "retention", "compliance"])`
- `search_by_tag(tags=["GDPR", "erasure"])` — find all sources tagged with both
- `create_collection(name="Legal Framework", description="Regulatory documents")` then add sources to it

### 3. Full-Text Search

The CitationEngine PostgreSQL schema already has a GIN index on `to_tsvector` for the claims column. Extend this to cover source content, annotations, and metadata.

**Schema additions:**

```sql
-- Full-text search on source content (sources already store full text in 'content' column)
CREATE INDEX idx_sources_content_fts ON sources
    USING GIN (to_tsvector('english', content));

-- Full-text search on annotations
CREATE INDEX idx_annotations_content_fts ON source_annotations
    USING GIN (to_tsvector('english', content));
```

**Agent tool:**

| Tool | Purpose |
|------|---------|
| `search_sources` | Full-text keyword search across source content, claims, and annotations |

**Example query behind the tool:**

```sql
SELECT s.id, s.name, s.type, ts_rank(to_tsvector('english', s.content), query) AS rank
FROM sources s, plainto_tsquery('english', 'data retention requirement') AS query
WHERE to_tsvector('english', s.content) @@ query
  AND s.job_id = $1
ORDER BY rank DESC
LIMIT 10;
```

### 4. Vector / Semantic Search

This is the headline feature. Make citation sources searchable by meaning, not just keywords. Builds on the infrastructure planned in [vectorization.md](vectorization.md) but applied specifically to the citation library rather than workspace files.

See [citation_issues.md](citation_issues.md) for the research background on Dense X Retrieval and hybrid retrieval strategies (Section: "Matching Algorithms").

#### Why Vector Search on Citations Specifically?

The [vectorization.md](vectorization.md) plan targets workspace files (notes, plans, outputs). Citation source content is different:

- **Source documents are large** (full PDFs, web pages) — chunking and indexing is essential
- **Queries are claim-shaped** — the agent wants to find "which source supports this claim?" not "which file mentions X"
- **Cross-job reuse potential** — the same regulatory document may be cited across many jobs
- **Hybrid retrieval is critical** — legal/compliance text needs keyword precision (section numbers, article references) alongside semantic matching (see [citation_issues.md § Matching Algorithms](citation_issues.md#step-2-matching-algorithms-connecting-claims-to-sources))

#### pgvector Setup

PostgreSQL supports vector search via the [pgvector](https://github.com/pgvector/pgvector) extension. Already in `requirements.txt` as a dependency but not enabled.

**Infrastructure changes:**

```sql
-- Enable extension (requires superuser, done once)
CREATE EXTENSION IF NOT EXISTS vector;

-- Source content embeddings (chunked)
CREATE TABLE source_embeddings (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_text TEXT NOT NULL,
    embedding vector(384),   -- all-MiniLM-L6-v2 dimensions
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(source_id, chunk_index)
);

CREATE INDEX idx_source_embeddings_source ON source_embeddings(source_id);
CREATE INDEX idx_source_embeddings_job ON source_embeddings(job_id);
CREATE INDEX idx_source_embeddings_vector ON source_embeddings
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

**Embedding strategy:**
- Chunk source content on registration (when `add_doc_source` / `add_web_source` is called)
- Use configurable embedding backend (env vars, same as [vectorization.md](vectorization.md) plan):
  - `EMBEDDING_MODEL` — model name (default: `sentence-transformers/all-MiniLM-L6-v2`)
  - `EMBEDDING_API_URL` — optional remote endpoint (OpenAI-compatible)
  - `EMBEDDING_API_KEY` — optional API key
- Store chunk text alongside embedding for retrieval without join

**Agent tools:**

| Tool | Purpose |
|------|---------|
| `search_literature` | Semantic search across all source content in current job |
| `find_evidence` | Given a claim, find the best-matching source passages (for the [academic workflow](citation_issues.md#option-e-academic-workflow--plan-with-citations-refresh-before-writing--recommended)) |

**`find_evidence` is the key tool.** From [citation_issues.md § Pattern 3](citation_issues.md#pattern-3-the-claim-to-source-tool):

> `find_evidence(claim: str)` → `(source_id, quote, confidence_score)`

This enables the agent to write a claim and then find supporting evidence, rather than the current workflow of finding evidence first and hoping to remember it later.

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

### 5. Cross-Job Source Reuse (Future)

Currently sources are isolated per job via `job_id` FK. A full literature management system should allow the agent to reference sources from previous jobs — the same way a researcher's Zotero library persists across papers.

**Possible approach:**
- Introduce a `library` concept separate from per-job sources
- When the agent registers a source, check by `content_hash` if it already exists in the library
- If so, create a lightweight reference (join table) rather than duplicating content
- Agent can "import" sources from previous jobs into the current job

This is a larger architectural change and should come after the core features above are stable.

---

## Implementation Roadmap

### Phase 1: Annotations & Tags (Low complexity)

New tables + new agent tools. No new infrastructure needed.

1. Add `source_annotations`, `source_tags`, `collections`, `collection_sources` tables to schema
2. Implement `annotate_source`, `get_annotations`, `tag_source`, `search_by_tag` tools
3. Register tools in `TOOL_REGISTRY` under a new `literature` category
4. Add full-text indexes on source content and annotations

### Phase 2: Vector Search Infrastructure

Depends on deploying an embedding model. Builds on [vectorization.md](vectorization.md) infrastructure plan.

1. Enable `pgvector` extension in PostgreSQL (docker-compose update)
2. Add `source_embeddings` table
3. Implement embedding service (configurable backend: local sentence-transformers or API)
4. Hook into `add_doc_source` / `add_web_source` to auto-embed on registration
5. Implement chunking strategy for source content

### Phase 3: Search Tools

1. Implement `search_literature` (semantic search over source content)
2. Implement `find_evidence` (claim-to-source matching for the [academic workflow](citation_issues.md#option-e-academic-workflow--plan-with-citations-refresh-before-writing--recommended))
3. Implement hybrid retrieval (vector + full-text + RRF fusion)
4. Test with real compliance documents

### Phase 4: Integration with Citation Workflow

Connect the new literature management features to the existing citation workflow improvements from [citation_issues.md](citation_issues.md):

- `find_evidence` feeds directly into `cite_document` — agent finds evidence semantically, then creates a verified citation
- Annotations serve as the agent's "reading notes" during the planning phase
- Tags/collections help the agent organize sources during multi-document research jobs
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

## Open Questions

1. **Where should the code live?** Annotations/tags/search could live in CitationEngine (making it a reusable library) or in the Graph-RAG agent layer (`src/tools/`, `src/database/`). CitationEngine is cleaner but increases scope of the separate repo.

2. **Embedding model choice.** Local sentence-transformers (free, private, 384d) vs OpenAI API (higher quality, costs money, 1536d) vs configurable (more code). The [vectorization.md](vectorization.md) doc recommends local `all-MiniLM-L6-v2`.

3. **Cross-job source sharing.** How much isolation do we want? Strict per-job isolation is simpler and matches the current FK model. Shared library is more powerful but adds complexity (permissions, versioning, deduplication).

4. **Strategic vs tactical phase access.** Current citation tools are tactical-only. Literature management tools (annotations, search, browsing) might be useful in strategic phases too — the agent could review its library while planning the next phase.

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
