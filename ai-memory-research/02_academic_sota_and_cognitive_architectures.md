# Deep Research Brief 2 — Academic State of the Art & Cognitive Architectures for AI Agent Memory

## Context (grounding — not the subject of the research)
I'm refining the long-term memory system of a multi-tier AI agent platform.
Current stack: an **implicit** auto-extracted memory store ("RecallStore") in
**pgvector** with hybrid dense+sparse+recency **RRF** search, TTL pinning, and
a background "observer" (extracts every ~5 turns) + "assembler" (consolidates
every ~7 turns) LLM loop; plus an **explicit** agent-authored **Knowledge Base**
in **Neo4j** + pgvector mirror. We want to understand the conceptual frameworks
and research frontier so our design choices are grounded in more than intuition.

## Objective
Survey the **research frontier and conceptual foundations** of LLM/agent memory:
the canonical systems, the cognitive-science taxonomies as applied to agents,
and the cutting-edge papers (≈2023–2026). Distinguish **proven** results from
**promising-but-speculative** directions.

## Research questions

### Canonical systems (the works everyone cites)
- **MemGPT / Letta** — virtual context management, OS-like paging between
  "main context" and external memory, memory hierarchy, function-callable
  memory edits. What did it prove; what are its limits?
- **Generative Agents (Stanford)** — the memory stream, **reflection**,
  retrieval scored by **importance × recency × relevance**. How is this
  operationalized and what carried over into production systems?
- **Reflexion, Voyager (skill memory), Self-RAG, MemoryBank, RecurrentGPT** —
  what each contributed to the memory conversation.

### Cognitive taxonomy applied to agents
- Working / short-term vs long-term; **episodic vs semantic vs procedural**;
  declarative vs non-declarative. How do real agent frameworks map onto these,
  and does the distinction actually improve systems or is it window dressing?

### Agentic & self-organizing memory
- **A-MEM** (Zettelkasten-style self-organizing notes), self-editing memory,
  "memory as a learned skill", memory-augmented agents that restructure their
  own store. Evidence of benefit.

### Consolidation, reflection & "sleep-time"
- How systems abstract raw episodes into durable knowledge: periodic reflection,
  hierarchical summarization, **sleep-time / background consolidation compute**
  (e.g. Letta's sleep-time agents). What's the evidence it helps?

### Forgetting & decay
- Principled forgetting, importance/retention scoring, capacity management,
  neuroscience-inspired decay (Ebbinghaus forgetting curve, spacing). When does
  forgetting *improve* task performance vs just save tokens?

### Parametric vs non-parametric memory (emerging)
- Storing memory in weights / **memory layers** / KV-cache reuse / "cartridges"
  vs external retrieval. Where is this heading, and is any of it practical now?

### Surveys & open problems
- The best **survey papers** on LLM-agent memory (2024–2026). What is the
  current research consensus, and what are the named open problems?

## Source-quality guidance
Prioritize arXiv, top-venue papers (NeurIPS / ICLR / ACL / EMNLP), highly-cited
surveys, and lab research blogs. Weight by recency **and** citation/replication.
Clearly separate empirically-validated findings from proposals without strong
evaluation. Cite everything with links.

## Tie it back to our system
- Which concepts do we **already embody**? (Hypothesis: observer ≈ extraction,
  assembler ≈ consolidation/reflection, TTL ≈ decay, RRF weights ≈ the
  importance/recency/relevance blend.) Validate or correct this mapping.
- Which **proven** ideas are we missing? (e.g. an explicit episodic/semantic
  split, true reflection passes, sleep-time consolidation, importance scoring at
  write time.)
- Which **frontier** ideas are worth a small pilot vs not yet worth it?

## Deliverable
1. **Canonical-works table** (work → core idea → what it proved → influence).
2. **Taxonomy → implementation map** (cognitive concept → how agents implement
   it → does it demonstrably help).
3. **Frontier ideas worth piloting** (with the evidence behind each).
4. **Open problems** the field hasn't solved.
5. An explicit "**our system vs the concepts**" gap analysis.
6. Full citations.
