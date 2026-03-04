---
tags:
  - feature
  - implementation
  - memory
  - context-management
aliases:
  - memory light
  - memory v1
related:
  - "[[project_knowledge_base]]"
  - "[[memories_mechanism]]"
  - "[[context_management]]"
  - "[[working_memory]]"
---

> **Note:** Memory Light's storage backend is being unified with the project knowledge base. The extraction channels, observer, RRF hybrid search, and injection hook described here remain valid — but the data flows into the knowledge base (`knowledge/` notes + Neo4j + pgvector) rather than a standalone `memories` table. See [[project_knowledge_base]] for the unified design.

# Memory Light — Implementation Roadmap

Minimal viable implementation of the memory system described in [[memories_mechanism]]. No radical architecture changes — just an extra transient message injected before each LLM call, filled with retrieved memories from simple storage backends. Prove the value first, iterate later.

**Comparable systems:** [Hindsight](https://github.com/vectorize-io/hindsight) is the closest existing implementation — PostgreSQL-native with pgvector, 4-channel RRF retrieval, 91.4% on LongMemEval benchmark. Our architecture is similar but integrated directly into the agent's execute loop rather than operating as an external service. Also informed by [Zep/Graphiti](https://arxiv.org/abs/2501.13956) (zero-LLM retrieval, temporal awareness) and [Mnemosyne](https://rand.github.io/mnemosyne/) (FTS5+graph+vector weighting).

## Goal

Enrich every LLM request with 2,000–10,000 tokens of relevant memories retrieved from prior conversation. The agent doesn't know this is happening — memories appear as context, just like `workspace.md` already does. A secondary LLM (local OSS 120B on `LLM_BASE_URL`) runs async every 5 requests to extract new memories from the conversation and store them.

## Design Constraints

- **No changes to the agent's reasoning loop** — memory is injected as infrastructure, not as a tool the agent calls
- **No TTL system yet** — just fill the memory budget, sort by relevance, drop the least relevant when full
- **No separate memory LLM config initially** — reuse the existing LLM config or a dedicated env var for the observer model
- **Storage starts simple** — PostgreSQL (key-value + pgvector) only, no Neo4j graph or regex indices yet
- **Async observer** — runs in background, never blocks the agent's execute loop

## Architecture Overview

```
Agent execute loop (unchanged)
    │
    ├─ Context compaction (existing Layer 1)
    │   └─ ★ compaction summary → memory store (free source)
    ├─ Todo injection (existing)
    ├─ Workspace.md injection (existing)
    ├─ ★ Memory injection (NEW) ← assembled from retrieval
    ├─ Instruction file injection (existing)
    ├─ LLM call
    │
    ├─ Tool execution
    │   ├─ todo_complete → ★ completion notes → memory store (free source)
    │   └─ tool error   → ★ error pattern → memory store (free source)
    │
    ├─ Phase boundary (archive_phase)
    │   └─ ★ archived todos → memory store (free source)
    │
    │   meanwhile, every N turns + on phase boundaries:
    │   ┌────────────────────────────┐
    │   │ Memory Observer (async)    │
    │   │ Reads recent messages      │
    │   │ Extracts memories          │
    │   │ Deduplicates against store │
    │   │ Stores in PostgreSQL       │
    │   └────────────────────────────┘
    │
    └─ Post-response handling
```

## Storage

### PostgreSQL Schema

Extend the existing system PostgreSQL (same database as jobs/requirements). Add to `src/database/schema.sql`:

```sql
-- Requires: CREATE EXTENSION IF NOT EXISTS vector;
-- (pgvector extension for dense embeddings)

CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    agent_id VARCHAR(100),
    content TEXT NOT NULL,
    summary VARCHAR(500),           -- One-line summary for compact display
    memory_type VARCHAR(50) DEFAULT 'factual',  -- factual, procedural, error_solution, vocabulary, relational
    source VARCHAR(50) DEFAULT 'observer',       -- observer, todo, compaction, phase_archive, tool_error
    keywords TEXT[] DEFAULT '{}',   -- Extracted keywords for BM25-style matching
    embedding vector(1536),         -- Dense embedding (text-embedding-3-small)
    sparse_keywords TSVECTOR,       -- PostgreSQL full-text search (sparse retrieval)
    importance FLOAT DEFAULT 0.5,   -- Observer's confidence score (0-1)
    source_turn_start INT,          -- Conversation turn range this was extracted from
    source_turn_end INT,
    source_phase INT,               -- Phase number when extracted
    token_count INT DEFAULT 0,      -- Pre-counted tokens for budget math
    access_count INT DEFAULT 0,     -- How often this memory was injected
    created_at TIMESTAMP DEFAULT NOW(),
    last_accessed TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_memories_job ON memories(job_id);
CREATE INDEX idx_memories_job_importance ON memories(job_id, importance DESC);
CREATE INDEX idx_memories_job_accessed ON memories(job_id, last_accessed DESC);
CREATE INDEX idx_memories_job_type ON memories(job_id, memory_type);
CREATE INDEX idx_memories_keywords ON memories USING GIN(keywords);
CREATE INDEX idx_memories_sparse ON memories USING GIN(sparse_keywords);
-- Vector index: HNSW is the consensus choice over IVFFlat for interactive retrieval.
-- Better speed-recall tradeoff (logarithmic search time vs. linear with IVFFlat).
-- ef_construction=256 for quality indexes (per production recommendations).
-- Cosine distance is the default for most embedding models.
-- At 50M+ vectors, consider DiskANN (via pgvectorscale) for lower RAM requirements.
CREATE INDEX idx_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 256);
```

**Note on `halfvec`**: pgvector 0.7+ supports half-precision vectors (`halfvec`) which halve storage with minimal quality loss. For v1 we use full precision; consider `halfvec` if storage becomes a concern at scale.

### Retrieval Strategy

Each injection cycle runs these queries in parallel and merges results:

1. **Dense vector search** — Embed the current todo + recent context, query top 5 nearest neighbors
2. **Sparse/keyword search** — Extract keywords from current context, query `sparse_keywords` tsvector for top 5 matches
3. **Recency bias** — Also fetch the 3 most recently created memories (they're likely relevant to current work)

**Result fusion via Reciprocal Rank Fusion (RRF):**

RRF is scale-independent — it ignores raw scores and only uses relative rankings. This makes it robust across heterogeneous scoring systems (cosine similarity 0-1 vs. ts_rank 0-25+). No score normalization needed.

Formula: `RRF(document) = SUM(1 / (k + rank_i))` where `k` is a smoothing constant.

```sql
-- Reference implementation (adapted from Supabase hybrid search pattern):
CREATE OR REPLACE FUNCTION memory_hybrid_search(
    query_text text,
    query_embedding vector(1536),
    job_id_param uuid,
    match_count int DEFAULT 10,
    dense_weight float DEFAULT 0.6,
    sparse_weight float DEFAULT 0.3,
    recency_weight float DEFAULT 0.1,
    rrf_k int DEFAULT 50
) RETURNS SETOF memories LANGUAGE sql AS $$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> query_embedding) AS rank_ix
    FROM memories WHERE job_id = job_id_param
    ORDER BY rank_ix LIMIT match_count * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(sparse_keywords, websearch_to_tsquery(query_text)) DESC) AS rank_ix
    FROM memories WHERE job_id = job_id_param AND sparse_keywords @@ websearch_to_tsquery(query_text)
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE job_id = job_id_param
    ORDER BY rank_ix LIMIT match_count
)
SELECT memories.* FROM dense
FULL OUTER JOIN sparse ON dense.id = sparse.id
FULL OUTER JOIN recent ON COALESCE(dense.id, sparse.id) = recent.id
JOIN memories ON COALESCE(dense.id, sparse.id, recent.id) = memories.id
ORDER BY
    COALESCE(1.0 / (rrf_k + dense.rank_ix), 0.0) * dense_weight +
    COALESCE(1.0 / (rrf_k + sparse.rank_ix), 0.0) * sparse_weight +
    COALESCE(1.0 / (rrf_k + recent.rank_ix), 0.0) * recency_weight
    DESC
LIMIT match_count
$$;
```

**Tuning notes:**
- `rrf_k=50` is the standard starting point (Supabase default: 50, ParadeDB default: 60)
- Smaller k (20) = more weight to top-ranked items, sharper discrimination
- Larger k (60) = more uniform weighting, gentler falloff
- Default weights (0.6/0.3/0.1) favor semantic retrieval; tune empirically per use case
- Research shows hybrid search with RRF improves accuracy 8-15% over pure vector or pure keyword
- `FULL OUTER JOIN` ensures documents appearing in only one search channel still surface

**Known limitation of `ts_rank`:** PostgreSQL's `ts_rank` must score every matching document to rank results — no efficient top-k without examining the full match set. For common terms matching many rows, this becomes expensive. Future upgrade: BM25 extensions (ParadeDB `pg_search`, VectorChord-bm25) provide proper TF-IDF with Block-Max WAND optimization. For v1, the memory table is small enough that `ts_rank` is fine.

When the budget is full, older and less-accessed memories are dropped first. No explicit TTL — relevance sorting handles it naturally.

## Free Memory Sources (No Extra LLM Calls)

The observer LLM runs every N turns to extract memories. But there are several places where the system already produces memory-grade content as a byproduct of normal operation. Tapping into these is essentially free — no extra LLM calls, no extra latency, just capturing what already exists.

### 1. Todo Completion Notes

When the agent calls `todo_complete`, it passes `notes` (a list of strings) describing what was done. These are pre-summarized, high-signal insights written by the reasoning model itself. The `TodoManager.complete()` method already stores them on the `TodoItem`.

**Hook point**: `TodoManager.complete()` in `src/managers/todo.py` (line ~224). After `todo.notes.extend(notes)`, also call `memory_manager.store_from_todo(todo)`.

**What we get for free**: Every completed todo becomes a memory. Content = todo description + completion notes. Keywords = extracted from todo text. Importance = high (the agent thought it was worth noting). Zero extra LLM cost.

```python
# In TodoManager.complete(), after notes are added:
if self._memory_manager and notes:
    await self._memory_manager.store(
        content=f"Completed: {todo.content}\nOutcome: {'; '.join(notes)}",
        keywords=extract_keywords(todo.content),  # Simple text extraction
        importance=0.7,
        source_phase=self._phase_number,
    )
```

### 2. Compaction Summaries

When `summarize_and_compact()` runs in `src/core/context.py`, it produces a summary of the conversation being discarded. This summary is stored as a `SystemMessage` in the conversation — but it's also a perfect memory source. It's literally an LLM-generated summary of "what happened" that we're already paying for.

**Hook point**: `ContextManager.summarize_and_compact()` in `src/core/context.py` (line ~1125). After `self._state.summaries.append(summary)`, also store as memory.

**What we get for free**: A high-level narrative of discarded conversation segments. These become long-term memories that survive even after the summary itself gets compacted out of the conversation. The LLM call was already happening — we just also persist the output.

### 3. Phase Archives

The `archive_phase` node in `src/graph.py` calls `todo_manager.archive()`, which writes a markdown file to `archive/todos_phase_{N}_{type}_{ts}.md`. This file contains all todos with their completion notes, status, and stats. It's a structured summary of an entire phase.

**Hook point**: `archive_phase` node in `src/graph.py` (line ~1170). After `archive_path = todo_manager.archive(...)`, read the archived file and extract memories from completed todos.

**What we get for free**: Bulk import of an entire phase's worth of insights. All the completion notes, decisions, and outcomes from every todo in one pass. No LLM needed — just parse the markdown.

### 4. Tool Error Patterns

When tools fail, the error message and the agent's subsequent correction are a classic error-solution pair — exactly the kind of memory the design doc calls out as high-value. The graph already handles tool errors; we just need to capture them.

**Hook point**: The `tools` node in `src/graph.py` processes tool results. When a `ToolMessage` contains an error, store it. When the next successful call to the same tool happens, link it as the solution.

**What we get for free**: "I tried X and it failed because Y, then Z worked" memories. These surface when the agent encounters similar errors in future turns. Programmatic — just match tool name + error status.

### 5. Memory Type Classification (Free from Observer)

The design doc describes memory types (factual, relational, procedural, vocabulary, error-solution). We deferred this to keep things simple, but it's actually free to include — just add a `type` field to the observer's extraction prompt. The LLM is already running; asking it to also classify costs zero extra.

**Change**: Add `type` to the observer extraction prompt output format. Add a `memory_type VARCHAR(50)` column to the schema. Use it as a retrieval signal — e.g., when the agent is in a strategic phase, weight procedural and factual memories higher.

### 6. Deduplication via Embedding Similarity

Before storing a new memory, check if a semantically similar one already exists (cosine similarity > 0.92). If so, update the existing memory's `access_count` and `last_accessed` instead of creating a duplicate. The embedding is already computed for storage — the dedup query is just one extra vector comparison.

**What we get for free**: Prevents the memory store from filling with near-identical entries. Also naturally boosts the importance of recurring observations — if the observer extracts the same insight twice, it means it's important.

**Dedup query:**
```sql
SELECT id, content, 1 - (embedding <=> $1) AS similarity
FROM memories
WHERE job_id = $2 AND 1 - (embedding <=> $1) > 0.92
ORDER BY similarity DESC LIMIT 1;
```

**Upgrade path (Phase 4+): Mem0-style AutoDedup.** Mem0's dedup system sends the new memory + similar candidates to an LLM which decides: **ADD** (genuinely new), **UPDATE** (merge with existing), **DELETE** (superseded by existing), or **SKIP** (redundant). This catches semantic duplicates that cosine similarity misses (e.g., "John lives in NYC" vs "John's residence is New York City"). Cost: one extra LLM call per memory, but only for memories that pass the cosine threshold check. For v1, threshold-based dedup is sufficient.

**Alternative (from Graphiti): Two-phase dedup.** Phase 1: deterministic (exact match + MinHash/Jaccard similarity). Phase 2: LLM only for ambiguous cases. This minimizes LLM calls while still catching edge cases.

## Components

### 1. MemoryManager (`src/managers/memory_manager.py`)

Core service. Handles storage, retrieval, and injection assembly.

```
class MemoryManager:
    __init__(db: PostgresDB, embedding_service: EmbeddingService, config: dict, job_id: str)

    # Storage (with automatic dedup)
    async store(content, summary, keywords, importance, turn_range, phase,
                memory_type="factual", source="observer") -> Optional[UUID]
    async store_batch(memories: list[MemoryRecord]) -> list[UUID]
    async store_from_todo(todo: TodoItem, phase: int)           # Free source: todo completion
    async store_from_compaction(summary: str, phase: int)       # Free source: compaction summary
    async store_from_tool_error(tool_name, error, phase: int)   # Free source: tool failures

    # Deduplication
    async find_similar(embedding: list[float], threshold: float = 0.92) -> Optional[MemoryRecord]
    async merge_or_create(memory: MemoryRecord) -> UUID  # Dedup then store/update

    # Retrieval
    async retrieve(context_text: str, budget_tokens: int = 5000) -> list[MemoryRecord]
    async search_dense(embedding: list[float], limit: int = 5) -> list[MemoryRecord]
    async search_sparse(query_text: str, limit: int = 5) -> list[MemoryRecord]
    async get_recent(limit: int = 3) -> list[MemoryRecord]

    # Assembly
    def assemble_memory_block(memories: list[MemoryRecord], budget_tokens: int) -> str
    def format_memory(memory: MemoryRecord) -> str

    # Lifecycle
    async get_stats() -> dict  # Count, total tokens, access patterns, by source/type
```

### 2. MemoryObserver (`src/services/memory_observer.py`)

Async background process that extracts memories from conversation. Runs every N turns.

```
class MemoryObserver:
    __init__(llm, embedding_model, memory_manager, config)

    # Main loop
    async observe(messages: list, current_turn: int, phase: int)
    def should_observe(current_turn: int) -> bool  # Every N turns

    # Extraction
    async extract_memories(messages_segment: list) -> list[MemoryRecord]
    async generate_embeddings(texts: list[str]) -> list[list[float]]

    # The LLM prompt for extraction
    EXTRACTION_PROMPT = """..."""
```

**Observer extraction prompt** (sent to local OSS 120B):
```
You are a memory extraction system. Given a segment of agent conversation,
extract noteworthy information that would be useful in future turns.

For each memory, output:
- content: The insight (1-3 sentences, self-contained, no references to "the conversation")
- summary: One-line summary (under 100 chars)
- keywords: Relevant terms for search (3-8 keywords)
- importance: How useful is this for future work? (0.0-1.0)
- type: One of: factual, procedural, error_solution, vocabulary, relational

Type definitions:
- factual: A discovered fact about the codebase, data, or domain
- procedural: A process, pattern, or sequence that works (or doesn't)
- error_solution: A problem encountered and how it was resolved
- vocabulary: Domain-specific terminology or naming conventions
- relational: How things connect (A depends on B, X requires Y)

Focus on:
- Decisions made and why
- Facts discovered about the codebase/data/domain
- Mistakes made and corrections applied
- Patterns that worked or failed
- Constraints and requirements discovered

Do NOT extract:
- Routine tool calls with no insight
- Repetitive information already captured
- Raw data or file contents (summarize instead)

Output as JSON array.
```

### 3. Memory Injection Hook (`src/core/memory_injection.py`)

Follows the same pattern as `workspace_injection.py`. Creates a transient fake tool result containing assembled memories.

```
MEMORY_TOOL_CALL_ID_PREFIX = "memory_inject_"

def create_memory_injection_messages(memory_content: str) -> tuple[AIMessage, ToolMessage]:
    """Create transient injection pair for memory content."""
    call_id = f"{MEMORY_TOOL_CALL_ID_PREFIX}{uuid4().hex[:8]}"
    ai_msg = AIMessage(
        content="",
        tool_calls=[{"name": "recall_memories", "args": {}, "id": call_id}]
    )
    tool_msg = ToolMessage(
        content=memory_content,
        tool_call_id=call_id
    )
    return ai_msg, tool_msg

def is_memory_injection_message(message) -> bool:
    """Check if message is a memory injection (for exclusion from summarization)."""
    ...
```

### 4. Graph Integration (`src/graph.py`)

Hook into the existing `_inject_transient_messages()` in the execute node:

```python
# In _inject_transient_messages() — add after workspace injection:

# Memory injection
if self.memory_manager:
    context_text = self._build_retrieval_context(state)  # Current todo + recent
    memories = await self.memory_manager.retrieve(
        context_text=context_text,
        budget_tokens=config.memory.budget_tokens  # default: 5000
    )
    if memories:
        memory_block = self.memory_manager.assemble_memory_block(memories)
        mem_ai, mem_tool = create_memory_injection_messages(memory_block)
        injection_messages.extend([mem_ai, mem_tool])

# Async observer trigger (non-blocking)
if self.memory_observer and self.memory_observer.should_observe(state["turn_count"]):
    asyncio.create_task(
        self.memory_observer.observe(
            messages=state["messages"],
            current_turn=state["turn_count"],
            phase=state.get("phase_number", 0)
        )
    )
```

### 5. Embedding Service

For v1, use OpenAI's `text-embedding-3-small` (cheap, fast, 1536 dimensions). Can swap to local embeddings later.

```
class EmbeddingService:
    __init__(api_key, model="text-embedding-3-small")

    async embed(text: str) -> list[float]
    async embed_batch(texts: list[str]) -> list[list[float]]
```

**Embedding model options (2025-2026 MTEB benchmarks):**

| Model | Dims | Context | Notes | Best For |
|-------|------|---------|-------|----------|
| **OpenAI text-embedding-3-small** | 1536 | 8K | Cheap, fast, good default | v1 (cloud) |
| **OpenAI text-embedding-3-large** | 3072 (MRL to 512) | 8K | Matryoshka: 512-dim outperforms ada-002 at 1536 | Quality upgrade |
| **BGE-M3** | 1024 | 8K | Dense+sparse+ColBERT in one forward pass | Self-hosted, eliminates separate sparse indexing |
| **Nomic Embed Text V2** | 768 | 8K | Open-source, MoE, multilingual | Air-gapped / local |
| **Cohere embed-v4** | 1024 | 128K | Multimodal, best raw MTEB score | Large documents |

**Recommendation for v1:** Start with `text-embedding-3-small` (simplest, cheapest). If switching to self-hosted, **BGE-M3** is the strongest choice — it produces dense AND sparse vectors in a single forward pass, which would eliminate the need for a separate `tsvector` column and simplify the retrieval pipeline significantly.

**Matryoshka Representation Learning (MRL):** OpenAI's `text-embedding-3-large` supports native truncation to lower dimensions post-hoc (e.g., 512-dim). A 512-dim truncated version outperforms `ada-002` at 1536-dim, saving 3x storage. Consider this for Phase 5 (cross-job memory) when the memory table grows large.

### 6. Config Extension (`config/defaults.yaml`)

```yaml
memory:
  enabled: false                    # Opt-in per config
  budget_tokens: 5000              # Max tokens for memory injection
  max_memories_per_injection: 10   # Cap on number of memories injected
  observer_interval: 5            # Run observer every N turns
  observer_model: null            # null = use main LLM, or specify model
  observer_base_url: null         # null = use main LLM_BASE_URL
  embedding_model: text-embedding-3-small
  dense_results: 5                # Top-K for dense vector search
  sparse_results: 5               # Top-K for sparse/keyword search
  recent_results: 3               # Number of recent memories to include
  importance_threshold: 0.3       # Minimum importance to store
  storage: postgres               # Only option for v1
```

## Injected Memory Block Format

What the agent actually sees (as a fake tool result):

```
--- Relevant Memories ---

[1] (importance: 0.9, phase 2)
The users table uses soft deletes via deleted_at column. Always filter
WHERE deleted_at IS NULL in queries unless explicitly looking for deleted records.

[2] (importance: 0.8, phase 3)
Neo4j MERGE with ON CREATE SET is faster than separate MATCH+CREATE for
this schema. Reduces Cypher query time by ~40%.

[3] (importance: 0.7, phase 1)
User preference: always use async/await pattern, never use .then() chains.
Code style follows the existing patterns in src/database/.

[4] (importance: 0.6, current)
The JWT interceptor in Angular needs to skip the /auth/login endpoint
to avoid circular token refresh. See HTTP_INTERCEPTORS in app.config.ts.

--- End Memories (4 items, ~1,200 tokens) ---
```

## Implementation Phases

### Phase 1: Storage + Manual Test (1-2 days)

**Goal**: Get memories in and out of PostgreSQL. Test retrieval quality manually.

- [x] Add `memories` table to `src/database/schema.sql` (with `memory_type` and `source` columns)
- [x] Add `pgvector` extension to PostgreSQL init (`orchestrator/init.py`)
- [x] Create HNSW index with `ef_construction=256`, cosine distance (research consensus over IVFFlat)
- [x] Implement `RecallStore` with `store()`, `search_dense()`, `search_sparse()`, `get_recent()`
- [x] Implement `memory_hybrid_search()` SQL function (RRF-based, see Retrieval Strategy section)
- [x] Implement `EmbeddingService` (OpenAI text-embedding-3-small)
- [x] Implement `find_similar()` for deduplication (cosine > 0.92 = merge, don't create)
- [x] Add `memory` section to `config/defaults.yaml` (disabled by default)
- [x] Write tests: store a memory, retrieve by vector, retrieve by keyword, hybrid RRF retrieval, dedup check
- [ ] Manual test: insert 10 fake memories, verify retrieval ranking with hybrid search

### Phase 2: Injection Hook + Free Sources (1-2 days)

**Goal**: Memories appear in the agent's context. Free sources start populating the store without any LLM observer.

- [x] Implement `memory_injection.py` (transient message pair, same pattern as workspace)
- [x] Add memory injection to `_inject_transient_messages()` in `src/graph.py`
- [x] Add `is_memory_injection_message()` to summarization exclusion in `workspace_injection.py`
- [x] Add `recall_store` field to `ToolContext` (with sync-safe `queue_memory()` / `drain_pending_memories()`)
- [x] Adjust `injection_overhead_tokens` calculation to include memory budget
- [x] **Free source: todo completion** — `todo_complete` queues memory via `context.queue_memory()`, flushed in `audited_tools`
- [x] **Free source: compaction summaries** — Stored in execute node after `ensure_within_limits()`
- [x] **Free source: phase archives** — Stored in `archive_phase` node before archiving
- [x] **Free source: tool errors** — Stored in `audited_tools` node for tool error results
- [ ] Test: run agent without observer, verify free sources populate memories and they appear in injection

This phase is important because **the free sources alone may already provide significant value**. Todo notes and compaction summaries are high-quality, pre-summarized content. Running the agent with just these sources (no observer LLM) is a valid baseline to measure against.

### Phase 3: Observer (1-2 days)

**Goal**: Add LLM-based extraction for insights that free sources miss. The observer fills gaps — it doesn't need to catch everything because the free sources already handle structured outputs.

- [x] Implement `MemoryObserver` with extraction prompt (including `type` classification)
- [x] Wire observer into graph execute node (async, non-blocking via `asyncio.create_task()`)
- [x] Configure observer to use local OSS 120B model (`observer_model` / `observer_base_url`)
- [x] Add `turn_count` field to `UniversalAgentState` (incremented in execute node)
- [x] Observer deduplicates against existing memories before storing (reuse `find_similar()` via `RecallStore.store()`)
- [ ] Test: run a 20-turn job, verify observer extracts memories not already captured by free sources
- [ ] Test: verify extracted memories appear in subsequent injections
- [x] **Also trigger observer on phase boundaries** — `observe_phase_boundary()` called in `archive_phase` node via `asyncio.create_task()`

### Phase 4: Tuning + Evaluation (ongoing)

**Goal**: Make it actually useful. Tune retrieval, observe impact on agent decisions.

- [ ] Run A/B comparisons: same job with and without memory injection
- [ ] Run comparison: free sources only vs. free sources + observer (measure observer's marginal value)
- [ ] Tune importance threshold (too low = noise, too high = missing memories)
- [ ] Tune budget (2000 vs 5000 vs 10000 tokens — what helps most?)
- [ ] Tune observer interval (every 5 vs 10 vs phase boundaries only)
- [ ] Tune retrieval weights per `memory_type` — do procedural memories help more than factual in tactical phases?
- [ ] Add memory stats to job metadata (by source: how many from todos, compaction, observer, errors)
- [ ] Add MongoDB audit logging for memory operations (piggyback on archiver)

### Phase 5: Cross-Job Memory (future)

**Goal**: Memories persist across jobs for the same agent/expert type.

- [ ] Add `agent_id` filtering to retrieval queries
- [ ] Allow memories to be scoped: job-only, agent-scoped, or global
- [ ] Cross-job dedup: when starting a new job, existing agent-scoped memories are already available
- [ ] Consider memory decay (reduce importance over time for stale memories)

## Integration Points (Existing Code)

| File | Change | Risk |
|------|--------|------|
| `src/database/schema.sql` | Add `memories` table | None (additive) |
| `orchestrator/database/schema.sql` | Add `memories` table | None (additive) |
| `orchestrator/init.py` | Enable pgvector extension | Low (idempotent) |
| `config/defaults.yaml` | Add `memory:` section | None (disabled by default) |
| `src/core/loader.py` | Initialize MemoryManager if enabled | Low |
| `src/graph.py` | Memory injection + observer trigger + phase archive hook | Medium (core loop) |
| `src/core/context.py` | Exclude memory injections from summarization + compaction memory hook | Low |
| `src/tools/context.py` | Add `memory_manager` field | None (optional field) |
| `src/managers/todo.py` | Call `store_from_todo()` in `complete()` | Low (3 lines) |
| `src/graph.py` (tools node) | Capture tool errors as memories | Low (conditional check) |

The highest-risk change is the graph.py injection hook, but it follows the exact same pattern as the existing workspace/instruction injection — just one more entry in the list. The free source hooks are all 3-5 line additions in existing methods with early-return guards (`if not self._memory_manager: return`).

## Dependencies

- **pgvector** PostgreSQL extension (for dense vector search, HNSW indexing)
- **OpenAI embeddings API** or local embedding model (for generating vectors; see embedding model options above)
- **Local OSS 120B model** on `LLM_BASE_URL` (for the observer — already available)
- **tiktoken** (already used for token counting)

**Optional future dependencies:**
- **ParadeDB pg_search** or **VectorChord-bm25** — proper BM25 ranking to replace `ts_rank` at scale
- **pgvectorscale** — DiskANN indexing for 50M+ vectors (lower RAM than HNSW)
- **BGE-M3** (local) — unified dense+sparse embeddings, eliminates separate tsvector column

## What This Does NOT Include (Yet)

These are all described in [[memories_mechanism]] but intentionally deferred:

- TTL system (memories don't expire, just get out-ranked by relevance)
- Knowledge graph storage (Neo4j) — but `memory_type: relational` memories are tagged for future migration
- Regex/rule-based triggers (e.g., "inject Neo4j memories before cypher calls")
- Context buffer paradigm (non-conversational input assembly)
- Perception system (visual attention / sensor data)
- Distiller (proactive conversation compression — though JetBrains research shows our existing observation masking may be the better primary strategy)
- Multi-tier observer (cheap model per turn + deep scan at phase boundaries)
- Cross-job memory sharing (Phase 5, but schema already supports `agent_id` scoping)
- Memory feedback loops (reinforcement/decay based on usage — AgeMem shows RL-learned memory management is the upgrade path)
- Mem0-style AutoDedup (LLM decides ADD/UPDATE/DELETE/SKIP — upgrade from threshold-based dedup)
- Multi-graph decomposition (MAGMA's semantic/temporal/causal/entity graphs — start with flat storage, add when hitting retrieval limits)
- Foresight signals (EverMemOS's predictions about future memory relevance at creation time)
- BM25 extensions (ParadeDB pg_search or VectorChord-bm25 — replace `ts_rank` when the memory table grows large)
- Temporal awareness / bi-temporal tracking (Zep/Graphiti pattern — "when it happened" vs "when we learned it")
- BGE-M3 unified embeddings (dense+sparse+ColBERT in one model — eliminates separate tsvector column)

Start simple. The free sources (todo notes, compaction summaries, phase archives, tool errors) may already provide most of the value. The observer fills the gaps. If injecting 5,000 tokens of retrieved memories measurably improves decision quality, everything else follows naturally.

## References

- [Hindsight](https://github.com/vectorize-io/hindsight) — PostgreSQL-native memory, 91.4% LongMemEval, closest comparable implementation
- [Supabase Hybrid Search](https://supabase.com/docs/guides/ai/hybrid-search) — reference SQL for RRF-based hybrid search
- [Jonathan Katz: Hybrid Search with pgvector](https://jkatz05.com/post/postgres/hybrid-search-postgres-pgvector/) — RRF implementation patterns
- [ParadeDB: Hybrid Search Missing Manual](https://www.paradedb.com/blog/hybrid-search-in-postgresql-the-missing-manual) — BM25 + RRF in PostgreSQL
- [JetBrains: Efficient Context Management (Dec 2025)](https://blog.jetbrains.com/research/2025/12/efficient-context-management/) — observation masking > summarization
- [Mem0 pgvector Docs](https://docs.mem0.ai/components/vectordbs/dbs/pgvector) — AutoDedup pattern
- [pgvector HNSW Guide (AWS)](https://aws.amazon.com/blogs/database/optimize-generative-ai-applications-with-pgvector-indexing-a-deep-dive-into-ivfflat-and-hnsw-techniques/) — indexing best practices
- [Embedding Models Comparison (MTEB)](https://elephas.app/blog/best-embedding-models) — model selection guide
- [Zep/Graphiti: Temporal Knowledge Graph](https://arxiv.org/abs/2501.13956) — zero-LLM retrieval, bi-temporal model
- [Mnemosyne](https://rand.github.io/mnemosyne/) — 70/20/10 weighting (vector/keyword/graph), sub-ms retrieval
