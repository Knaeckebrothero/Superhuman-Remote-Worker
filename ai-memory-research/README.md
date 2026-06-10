# AI Memory — Deep Research Briefs

A set of self-contained research prompts for exploring the state of the art in
AI agent memory, so we can refine our own stack from first principles rather
than the intuitions it was originally built on.

Each file is a **standalone deep-research prompt** — it embeds the same shared
context block (our current system) so it can be run independently, in any order,
via the `/deep-research` skill or pasted into any deep-research product
(ChatGPT/Gemini/Perplexity/Claude). Run them in parallel; synthesize after.

## The five streams

| # | File | Stream | Covers your asks |
|---|------|--------|------------------|
| 1 | `01_frontier_labs_and_products.md` | How the big labs + commercial memory products actually do it | "big AI companies", memory files (as products) |
| 2 | `02_academic_sota_and_cognitive_architectures.md` | Research frontier + cognitive foundations | "state of the art", memory files (MemGPT) |
| 3 | `03_retrieval_rag_hybrid_longcontext.md` | The fetch layer: RAG, hybrid search, reranking, long-context | "RAG" |
| 4 | `04_knowledge_graphs_and_temporal_memory.md` | Graph-based & temporal memory | "graphs" |
| 5 | `05_lifecycle_and_evaluation.md` | Write/consolidate/forget + how to *measure* memory quality | (the two you didn't name but need most) |

Streams 1–4 map directly to what you listed; stream 5 covers the operational
lifecycle and **evaluation**, which is the highest-leverage gap — we currently
have no way to tell whether a memory change made things better or worse.

## Shared context (embedded in every brief)

Our current memory stack, for grounding (not the subject of the research):

- **RecallStore** — *implicit* memories auto-extracted from conversation by a
  background "observer" LLM every few turns; stored in **pgvector** with
  **hybrid dense+sparse+recency RRF** search (weights 0.6 / 0.3 / 0.1,
  `rrf_k=50`); TTL "pinning" guarantees new memories are injected for ~10 turns;
  an "assembler" LLM periodically curates/boosts/deprecates. Project-scoped
  (shared across jobs).
- **Knowledge Base** — *explicit*, agent-authored structured notes
  (decision / learning / plan / …) stored in **Neo4j** (typed `Note` nodes +
  edges like `DERIVED_FROM` / `CONTRADICTS` / `SUPERSEDES`) and mirrored to a
  **pgvector** index for hybrid search.
- Embeddings: `qwen3-embedding-8b` (4096-dim). Both memory + KB are
  **auto-injected into every LLM call** as synthetic tool-call/result pairs.

## How to run

- Via skill: `/deep-research` with the contents of one file as the question.
- Or paste a file into any deep-research product.
- Suggested order: 1 → 3 → 4 → 2 → 5 (concrete → conceptual → operational), but
  they're independent.
