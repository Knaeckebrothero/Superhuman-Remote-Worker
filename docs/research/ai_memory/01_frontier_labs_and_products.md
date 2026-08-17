# Deep Research Brief 1 — How Frontier Labs & Commercial Memory Products Implement AI Memory

## Context (grounding — not the subject of the research)
I'm refining the long-term memory system of a multi-tier AI agent platform
(autonomous + interactive agents running long, multi-phase jobs). Our current
stack: an **implicit** memory store ("RecallStore") that auto-extracts memories
from conversation into **pgvector** with hybrid dense+sparse+recency **RRF**
search and TTL-based pinning; plus an **explicit** agent-authored **Knowledge
Base** stored in **Neo4j** (typed nodes + edges) and mirrored to pgvector.
Embeddings are `qwen3-embedding-8b` (4096-dim). Both are auto-injected into
every LLM call. We built this from intuition and want to benchmark it against
how the people with the most users and resources actually do memory.

## Objective
Produce a detailed, **source-grounded** survey of how leading AI labs and
dedicated memory-layer products implement long-term memory for assistants and
agents — the real mechanisms (extraction, storage, retrieval, injection,
forgetting), the user-facing features, the limits, and the lessons. Separate
**documented fact** from **inference/reverse-engineering** from **marketing**.

## Research questions

### Frontier labs / consumer assistants
- **OpenAI / ChatGPT**: the two-part memory ("saved memories" vs "reference
  chat history"). How is each implemented (what triggers a save, where stored,
  how retrieved, how injected, how it interacts with the system prompt)? User
  controls, limits, known failure modes. What's documented vs reverse-engineered?
- **Anthropic / Claude**: the **memory tool** (model-directed, file/tool-based),
  **Projects**, and **context editing / context management** (clearing stale
  tool results, the memory+context beta). How does the memory tool actually
  work (who decides what to write, format, retrieval)? Also Claude Code's
  `CLAUDE.md` / memory-file approach.
- **Google / Gemini**: personalization, "personal context", recall across
  chats — mechanism and controls.
- **Microsoft Copilot**: memory & personalization.
- Briefly: **Meta AI, Perplexity, xAI Grok** — any documented memory.

### Coding agents (memory-files paradigm)
- **Cursor** (rules / memories), **Claude Code** (`CLAUDE.md`), **Devin**,
  **Replit**, **Windsurf**, **GitHub Copilot**: how do coding agents persist
  project knowledge and preferences? File-based rules vs learned memory.

### Dedicated memory products / open source
For each, capture architecture (vector / graph / hybrid), extraction approach,
retrieval, forgetting/decay, claimed benchmark results, and license/pricing:
- **Mem0**, **Zep / Graphiti**, **Letta (MemGPT)**, **Cognee**,
  **LangMem (LangChain)**, **Supermemory**, **Memobase**, **MemoryOS**,
  and any other notable 2024–2026 entrants.

### Cross-cutting
- The **"agentic memory" vs "automatic RAG injection"** axis: does the model
  read/write its own memory via tools/files, or does an external pipeline
  extract+inject behind its back? Who chose which, and what's the stated
  rationale and tradeoff?
- What is **converging** across systems? What is **contested**? What do these
  systems / their users most commonly complain about (staleness, privacy,
  over-remembering, wrong recall)?

## Source-quality guidance
Prioritize: official docs, engineering/research blogs, API references, product
changelogs, the products' own GitHub. Reputable reverse-engineering (e.g.
Simon Willison, latent.space/swyx) is acceptable **if labeled as such**.
Prioritize 2024–2026; flag anything older as possibly stale. Cite every claim;
explicitly mark marketing claims and unverified benchmark numbers.

## Tie it back to our system
- Map each system onto our design (auto-injected pgvector RecallStore + Neo4j
  KB). Where are we **aligned, ahead, or behind**?
- Which specific mechanism is worth adopting (e.g. Claude's context-editing,
  ChatGPT's two-tier save vs chat-history reference, a memory tool)?
- Are we an outlier in auto-injecting *everything every turn*? What do others do
  instead (on-demand recall as a tool, budget-based, relevance-gated)?

## Deliverable
A structured report:
1. **Per-system breakdown table** (system → storage → extraction → retrieval →
   injection → forgetting → user controls → documented vs inferred).
2. Narrative deep-dives on the 4–6 most instructive systems.
3. **Patterns & takeaways** (converging / contested / common complaints).
4. **What to consider adopting**, mapped to our architecture, ranked by ROI.
5. Full citations.
