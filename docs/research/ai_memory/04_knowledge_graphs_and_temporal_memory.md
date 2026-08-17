# Deep Research Brief 4 — Knowledge Graphs & Temporal Memory for AI Agents

## Context (grounding — not the subject of the research)
I run a graph-based memory layer and want to know whether it's earning its keep
and how to do it properly. Today: an agent-authored **Knowledge Base** stored in
**Neo4j** — typed `Note` nodes plus `Tag`/`Keyword` nodes and typed edges
(`DERIVED_FROM`, `CONTRADICTS`, `SUPERSEDES`, `SUPPORTS`, `ANSWERS`,
`DEPENDS_ON`, …) — **mirrored to a pgvector index** for hybrid search. The graph
is the source of truth; the vector index is a derived search mirror. We have a
**fixed schema**, **no temporal modeling**, and **no community summarization**.
Notes are written by the agent (a `kb_write` tool) and by a background curator.

## Objective
Deep, **source-grounded** research on graph-based and temporal memory:
how graphs are constructed from unstructured/conversational text, how temporal
knowledge graphs model change over time, how such systems are maintained, and —
critically — the **honest evidence** for when graphs beat plain vector retrieval
for memory (vs when they're expensive over-engineering).

## Research questions

### Graph construction from unstructured text
- LLM-driven entity & relation extraction; **schema/ontology-guided vs open**
  extraction; **entity resolution / dedup**; quality control and how extraction
  errors compound over time.

### GraphRAG (Microsoft) and variants
- Community detection (**Leiden**), hierarchical **community summaries**,
  **global vs local** query modes, indexing cost. **LazyGraphRAG**,
  `nano-graphrag`, and other cost-reduced variants. When does GraphRAG measurably
  beat vector RAG, and on what query types?

### Temporal / bi-temporal knowledge graphs (most relevant to memory)
- **Zep / Graphiti**: event time vs ingestion time, **edge invalidation**,
  point-in-time queries, how contradictions are resolved as facts change. This
  is the model we lack — capture it in depth.
- General bi-temporal modeling patterns for an evolving memory store.

### Ontology design
- Fixed vs emergent schemas; how many node/edge types is healthy; when rich
  structure helps retrieval/reasoning vs when it adds noise and maintenance cost.

### Contradiction & invalidation handling
- How graph memory systems supersede/retire stale facts (directly relevant to
  our `CONTRADICTS` / `SUPERSEDES` edges, which we currently set but don't really
  exploit). Best practices for "update vs supersede vs delete".

### Graph + vector hybrid
- Combining graph traversal with vector search — exactly our Neo4j + pgvector
  setup. What are the proven patterns (vector to find entry nodes, graph to
  expand; or graph to constrain, vector to rank)? Who keeps both in sync and how?

### Maintenance, cost & failure modes
- Graph drift, compounding extraction errors, write/latency cost, operational
  burden. When is a graph **overkill** and a well-tuned vector store enough?

### Evidence (scrutinize hard)
- Benchmarks comparing graph vs vector memory (e.g. Zep's LongMemEval/DMR
  claims, GraphRAG evals). Assess methodological rigor and independence;
  separate vendor claims from independent replication.

## Source-quality guidance
Prioritize: Microsoft GraphRAG papers + repo; Zep/Graphiti papers + docs; Neo4j
engineering blogs; arXiv; independent benchmark replications. 2024–2026.
**Be skeptical of vendor self-benchmarks** — note evaluation setup and whether
results were independently reproduced.

## Tie it back to our system
- Should we add **bi-temporal modeling** (valid-time + ingestion-time, edge
  invalidation) to our notes? What would it concretely buy us?
- Should we add **community summarization** over the note graph?
- Are our `CONTRADICTS` / `SUPERSEDES` edges worth keeping if we add a real
  invalidation policy — or dead weight as-is?
- **Is the Neo4j graph earning its keep** given we also maintain a pgvector
  mirror, or could a vector store + lightweight metadata cover our actual query
  patterns? Give a clear-eyed verdict with the conditions under which each answer
  holds.

## Deliverable
1. **Graph-construction & temporal-modeling** best practices.
2. **Graph-vs-vector decision guidance** (which query/memory types justify a
   graph; which don't).
3. **Verified-vs-claimed benchmark table** for graph-memory systems.
4. **Concrete recommendations** for our Neo4j + pgvector setup (add temporal?
   community summaries? simplify? keep?).
5. Full citations.
