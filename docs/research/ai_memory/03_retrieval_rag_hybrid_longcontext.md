# Deep Research Brief 3 — The Retrieval Substrate: RAG, Hybrid Search, Reranking & the Long-Context Debate

## Context (grounding — not the subject of the research)
I'm refining the retrieval layer beneath an AI agent memory system. Today we
use: `qwen3-embedding-8b` (4096-dim) embeddings; **pgvector** with **HNSW**
(`m=16`, `ef_construction=256` for memory/knowledge, `64` for sources;
query-time `ef_search` left at the pgvector default of 40, untuned);
**Postgres `tsvector`** for the sparse/keyword channel (not BM25/SPLADE); and
**Reciprocal Rank Fusion** combining dense + sparse + recency with weights
0.6 / 0.3 / 0.1 and `rrf_k=50`. **No reranker.** Memories and knowledge notes
are fetched per-turn (top-k) and injected into every LLM call.

## Objective
Deep, technical, **source-grounded** research on how to fetch the right context
reliably — the current best practices in RAG and hybrid retrieval — and on the
honest tradeoffs between retrieval and long-context. Produce concrete,
ROI-ranked recommendations for a pgvector + tsvector + RRF stack.

## Research questions

### Vector RAG state of the art
- **Chunking**: semantic chunking, late chunking, **contextual retrieval**
  (Anthropic's prepend-context approach) — measured impact.
- **Embedding models** (2025–2026): current leaders, dimension vs quality
  tradeoffs, MTEB/MTEB-v2 caveats, when bigger dims stop paying off. How does
  `qwen3-embedding-8b` (4096-d) compare to current options?
- **Index & ANN tuning**: HNSW vs IVF vs DiskANN; the effect of `ef_search` /
  `ef_construction` / `m` on recall vs latency; how much recall we likely lose
  at `ef_search=40` on 4096-dim vectors.

### Hybrid search
- Dense + sparse fusion: **BM25 vs SPLADE vs learned sparse vs `tsvector`** —
  quality differences and when the sparse channel actually matters.
- **Fusion methods**: RRF vs weighted score fusion vs learned fusion; how to
  tune RRF `k` and channel weights; is a static 0.6/0.3/0.1 reasonable?

### Reranking
- Cross-encoder rerankers (Cohere Rerank, **bge-reranker**, Voyage, Jina):
  quality lift vs latency/cost. Is adding a reranker the single highest-ROI
  upgrade for a hybrid stack that currently has none?

### Agentic & iterative retrieval
- Query rewriting/expansion, multi-hop, self-querying, **retrieval-as-a-tool**
  (the agent decides when/what to fetch) vs one-shot auto-injection. Evidence
  for each. Relevance gating to avoid injecting noise every turn.

### Recency, temporal & metadata
- Folding recency/time decay and metadata filters into ranking without harming
  relevance (we currently blend recency as a fixed 10% RRF channel).

### Long-context vs retrieval
- When do 200k–2M-token context windows **replace** retrieval? The honest
  failure modes: "**lost in the middle**", **context rot / degradation**, cost,
  latency. The "context engineering" discipline. Hybrid retrieve-then-fill
  strategies. What the recent evidence actually shows.

### Evaluating retrieval specifically
- recall@k, nDCG, MRR, and end-task metrics; how to build a retrieval eval set
  from production traces (handoff to Brief 5 on full-system eval).

## Source-quality guidance
Prioritize: Anthropic's contextual-retrieval post; vector-DB engineering blogs
(Pinecone, Weaviate, Vespa, Qdrant, pgvector); MTEB and reranker leaderboards;
arXiv; rigorous practitioner write-ups. 2024–2026. Verify benchmark claims and
note evaluation conditions; flag vendor self-benchmarks.

## Tie it back to our system
Give a **specific critique** of our stack and a ranked list of changes:
- Is `tsvector` good enough, or should the sparse channel be BM25/SPLADE?
- Should we add a **reranker** (which one), and where in the pipeline?
- Should we **tune `ef_search`** up, and to what, at 4096-d?
- Is **per-turn auto-injection** the right pattern, or should recall become a
  relevance-gated tool the agent calls?
- Would **contextual retrieval** help our notes/memories?
- Is `qwen3-embedding-8b` still a good choice, or is there a clearly better
  embedding model now?

## Deliverable
1. **Technique survey** with measured impact where available.
2. **Comparative tradeoff tables** (sparse methods; fusion methods; rerankers;
   retrieval vs long-context).
3. **Highest-ROI improvements for a pgvector + tsvector + RRF stack**, ranked,
   each with expected benefit and rough effort.
4. Full citations.
