# Deep Research Brief 5 — Memory Lifecycle (Extract → Consolidate → Forget) & How to Evaluate Memory Systems

## Context (grounding — not the subject of the research)
I'm refining the operational write-path and measurement of an AI agent memory
system. Today: a background **observer** LLM extracts memories every ~5 turns;
an **assembler** LLM consolidates/curates every ~7 turns; new memories are
**TTL-pinned** for ~10 turns (guaranteed injection) then fall into a
retrieval-only pool; semantic **dedup** fires at cosine ≥ 0.85; importance is a
single 0–1 score; storage is **pgvector** (memories) + **Neo4j** (explicit
notes). Crucially, **we have no evaluation at all** — we cannot currently tell
whether any change to this pipeline makes memory better or worse.

## Objective
Research two tightly-linked things: (a) the **operational lifecycle** of memory —
what to remember, how to consolidate, and how to forget — and (b) **how to
rigorously evaluate** a memory system. Evaluation is the priority: it's the
capability we most lack and the precondition for improving anything else
safely. Produce a concrete "first eval harness to build" recommendation.

## Research questions

### Lifecycle — what to remember
- Salience/importance scoring; what gets extracted vs dropped; **trigger
  policies** (fixed every-N-turns vs event-driven vs end-of-session vs
  reflection-driven). Precision/recall tradeoffs of aggressive vs conservative
  extraction.

### Lifecycle — extraction methods
- LLM fact/triple extraction, structured-output schemas, prompt patterns;
  single-pass vs multi-pass; cost vs quality; hallucinated-memory risk.

### Lifecycle — dedup & conflict resolution
- Semantic dedup thresholds and pitfalls; merging vs appending vs
  **superseding**; detecting and resolving **contradictions**; "update-in-place
  vs new-version" policies; entity/fact resolution.

### Lifecycle — consolidation / reflection
- Turning raw episodes into abstracted, durable knowledge; periodic background
  passes; **sleep-time** consolidation; hierarchical summarization; how often,
  and how to avoid summary drift / information loss.

### Lifecycle — forgetting & decay
- TTL vs decay functions vs usage-based retention; capacity management; avoiding
  unbounded growth and recall noise; **principled forgetting** research and
  whether forgetting improves end-task accuracy (not just token savings).

### Evaluation — benchmarks
- **LOCOMO**, **LongMemEval**, **MSC (Multi-Session Chat)**, **DMR (Deep Memory
  Retrieval)**, **PerLTQA**, and any 2025–2026 successors. For each: what it
  measures, dataset construction, known criticisms (e.g. published critiques of
  LOCOMO), and which best mirrors a long-running multi-session agent.

### Evaluation — metrics & methodology
- Retrieval recall, answer accuracy, **temporal reasoning**, consistency over
  sessions, latency, token cost, "memory efficiency". How to **A/B a memory
  change**; offline eval harnesses; **LLM-as-judge** pitfalls and how to
  calibrate it; building a bespoke eval set from production traces.

### Evaluation — failure taxonomy
- Catalogue the failure modes to test for: hallucinated memories, stale-fact
  errors, retrieval misses, over-retrieval/noise injection, contradiction
  survival, privacy/leakage across users or projects.

## Source-quality guidance
Prioritize: benchmark papers + leaderboards; eval methodology from Mem0/Zep/Letta
(**scrutinize self-reported numbers**); arXiv; rigorous practitioner posts on
building memory eval harnesses. 2024–2026. Cite everything; note dataset sizes
and evaluation conditions.

## Tie it back to our system
- Given observer@5 / assembler@7 / TTL@10 / dedup@0.85 / single importance
  score — which of these knobs are **proven to matter**, and what would good
  defaults look like?
- What **eval harness should we stand up first** (smallest thing that gives a
  trustworthy signal), and **which benchmark** most resembles our long-running,
  project-scoped, multi-session agent use case?
- What are the **highest-confidence lifecycle changes** we could make
  (e.g. event-driven extraction, importance at write-time, real consolidation,
  a forgetting policy)?

## Deliverable
1. **Lifecycle best-practices** (per stage: trigger → extract → dedup/resolve →
   consolidate → forget), with the evidence behind each.
2. **Evaluation playbook**: benchmark comparison table, metric set, A/B
   methodology, LLM-as-judge guidance.
3. **"First eval harness to build"** — a concrete, minimal, trustworthy starting
   point tailored to our system.
4. **Prioritized lifecycle improvements**, ranked by confidence × impact.
5. Full citations.
