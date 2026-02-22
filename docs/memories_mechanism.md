---
tags:
  - agent-architecture
  - context-management
  - knowledge-management
aliases:
  - associative memory
  - observer memory system
  - context distillation
  - cognitive architecture
  - context buffer
  - TTL memory
  - perception system
  - visual attention
related:
  - "[[obsidian]]"
  - "[[context_management]]"
  - "[[working_memory]]"
  - "[[agent_improvements]]"
---

# Cognitive Memory Architecture

## Research Context

This architecture is informed by recent (2025-2026) research on memory systems for LLM agents:

**Surveys & Taxonomies:**
- ["Memory in the Age of AI Agents"](https://arxiv.org/abs/2512.13564) (Dec 2025, 45+ authors) — proposes a three-axis taxonomy: **forms** (token-level, parametric, latent), **functions** (factual, experiential, working), **dynamics** (formation, evolution, retrieval). The definitive survey; see also the [paper list](https://github.com/Shichun-Liu/Agent-Memory-Paper-List).
- [Continuum Memory Architecture](https://arxiv.org/abs/2601.09913) (Jan 2026) — formal requirements for memory beyond RAG: persistent storage, selective retention, associative routing, temporal chaining, and consolidation into higher-order abstractions.
- [ICLR 2026 MemAgents Workshop](https://openreview.net/forum?id=U51WxL382H) — dedicated workshop signaling agent memory is now a first-class research problem.

**Production Frameworks:**
- [Zep/Graphiti](https://arxiv.org/abs/2501.13956) — bi-temporal knowledge graph (Neo4j), hybrid BM25+vector+graph retrieval with **zero LLM calls during retrieval**. 94.8% DMR benchmark, 300ms p95 latency.
- [Letta Context Repositories](https://www.letta.com/blog/context-repositories) (Feb 2026) — git-based memory versioning. Agents restructure their own context as files. Branch/merge for multi-agent memory.
- [Mem0](https://arxiv.org/abs/2504.19413) — vector + graph hybrid, AutoDedup (LLM decides ADD/UPDATE/DELETE/SKIP). 91% lower latency than OpenAI memory.
- [Mnemosyne](https://rand.github.io/mnemosyne/) — Rust-based, FTS5+graph+vector (20/10/70 weighting), sub-millisecond retrieval. Built for Claude Code via MCP.
- [Hindsight](https://github.com/vectorize-io/hindsight) — PostgreSQL-native, 4-channel RRF retrieval, 91.4% LongMemEval. Closest to our planned implementation stack.
- [Cognee](https://github.com/topoteretes/cognee) — hybrid graph+vector, 0.93 human-like correctness on HotPotQA.

**Key Papers:**
- [MAGMA](https://arxiv.org/abs/2601.03236) (Jan 2026) — represents memories across **4 orthogonal graphs** (semantic, temporal, causal, entity). 45.5% higher reasoning accuracy, 95%+ token reduction.
- [EverMemOS](https://arxiv.org/abs/2601.02163) (Jan 2026) — engram-inspired lifecycle with "Foresight" signals predicting future relevance at memory creation time. SOTA on LongMemEval.
- [SimpleMem](https://arxiv.org/abs/2601.02553) (Jan 2026) — 26.4% F1 improvement while reducing tokens by 30x through semantic compression and online synthesis.
- [A-Mem](https://arxiv.org/abs/2502.12110) (NeurIPS 2025) — Zettelkasten-inspired linked notes where new memories trigger updates to existing ones.
- [MemR3](https://arxiv.org/abs/2512.20237) — autonomous retrieval with reflective reasoning and a global evidence-gap tracker.
- [ACT-R-Inspired Memory](https://dl.acm.org/doi/10.1145/3765766.3765803) — vector-based activation with temporal decay and probabilistic noise, mimicking human memory dynamics.

**Context Management:**
- [JetBrains Research](https://blog.jetbrains.com/research/2025/12/efficient-context-management/) (Dec 2025) — observation masking outperforms LLM summarization in 4/5 settings on SWE-bench. Validates our `keep_recent_tool_results` approach.
- [Context-Folding / FoldAgent](https://arxiv.org/abs/2510.11967) — RL-trained context compression achieving sub-linear growth (O(log n)). 58% on SWE-bench Verified with only 32K tokens.
- [GCC (Git-Context-Controller)](https://arxiv.org/abs/2508.00031) — versioned file-based agent memory (workspace.md ~ main.md, plan.md ~ commit.md, archive/ ~ branches). 48% on SWE-bench-Lite.
- [Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — "find the smallest set of high-signal tokens." Validates Just-In-Time loading and structured note-taking patterns.

**Visual Perception:**
- [Look, Focus, Act (LFA)](https://arxiv.org/html/2507.15833v1) (Jul 2025) — foveated vision transformers with 94% token reduction. Validates peripheral→focused pipeline.
- [D2Snap](https://arxiv.org/html/2508.04412v1) — text-only DOM descriptions (63%) nearly match screenshots (65%); hierarchy is the key structural feature.
- [Visual Perception Tokens](https://arxiv.org/abs/2502.17425) — models autonomously generate "look here" tokens. 2B model outperforms 7B baseline by 20%.
- [OmniParser V2](https://www.microsoft.com/en-us/research/articles/omniparser-v2-turning-any-llm-into-a-computer-use-agent/) — structured visual parsing that turns any LLM into a computer-use agent.
- [Screen2AX](https://arxiv.org/abs/2507.16704) — accessibility trees from screenshots, outperforms OmniParser V2 for grounding.

**Key meta-findings from the research:**
1. **Forgetting is as important as remembering** — utility-based deletion yields 10%+ performance gains (confirmed across multiple studies)
2. **No single retrieval modality is sufficient** — every top system uses hybrid retrieval (vector + keyword + graph)
3. **Temporal awareness is a key differentiator** — bi-temporal models (Zep/Graphiti) outperform static stores
4. **Observation masking > summarization** — simple masking beats sophisticated LLM summarization for context management (JetBrains)
5. **Context quality > context quantity** — intelligent 32K beats naive 1M (Context-Folding, SimpleMem)
6. **Text descriptions match or beat raw images** for agent visual tasks (D2Snap, Screen2AX)

## Concept

A cognitive architecture modeled on how human memory actually works. Humans don't remember the exact keystrokes they typed an hour ago — they remember *what they built, why, and how to use it*. The details are gone, but the moment something related comes up, the relevant knowledge resurfaces. The rest is noise that passed through working memory and was discarded.

This system has **three parallel processes**, all invisible to the agent:

1. **Observer** — Watches the conversation, extracts noteworthy information, stores it across multiple backends (vectors, knowledge graph, keyword indices). This is long-term memory formation.
2. **Distiller** — Continuously condenses the conversation history, stripping noise while preserving narrative coherence. This is forgetting — the most important part of memory.
3. **Retrieval** — Injects relevant memories into context based on current activity. This is recall.

The agent is completely unaware of any of this. It just works on its task. Memories appear when relevant, old conversation fades to summaries, and the context window stays lean. Like how `workspace.md` already works, but for the entire cognitive layer.

**The key insight:** With the memory mechanism handling precise recall, the conversation itself doesn't need to carry that burden. 50k tokens is enough to describe the entire architecture of Facebook if you strip the noise and speak at the right level of abstraction. An engineer working on a single feature doesn't need the exact details of unrelated features in their working memory — and if they do, those details should be surfaced by the memory system, not sitting in the conversation taking up space.

## Architecture

### Three Parallel Systems

```
┌──────────────────────────────────────────────────────────────┐
│                     Agent (unchanged)                        │
│                                                              │
│  execute → tools → check_todos → …                          │
│         ▲                          ▲                         │
│         │ inject memories          │ replace old messages    │
│         │                          │ with distilled versions │
└─────────┼──────────────────────────┼─────────────────────────┘
          │                          │
    ┌─────┴─────────────────────┐    │
    │      Retrieval Layer       │    │
    │  (fan-out across stores)   │    │
    │                            │    │
    │  ┌──────┐ ┌──────┐ ┌────┐ │    │
    │  │Dense │ │Sparse│ │ KG │ │    │
    │  │Vector│ │Vector│ │Neo4j│ │    │
    │  └──────┘ └──────┘ └────┘ │    │
    │  ┌──────┐ ┌──────────┐    │    │
    │  │Keyword│ │Regex/Rule│    │    │
    │  │ Index │ │  Index   │    │    │
    │  └──────┘ └──────────┘    │    │
    └─────┬─────────────────────┘    │
          │                          │
┌─────────┼──────────────┐  ┌────────┼──────────────────────┐
│         │ store +      │  │        │ condense history     │
│         ▼ index        │  │        ▼                      │
│   Observer System      │  │   Distiller System            │
│  (large-context model) │  │  (runs async, continuous)     │
│                        │  │                               │
│  Extracts memories     │  │  Strips noise from old turns  │
│  Classifies + routes   │  │  Preserves narrative + intent │
│  to storage backends   │  │  Replaces verbose history     │
│                        │  │  with compressed summaries    │
└────────────────────────┘  └───────────────────────────────┘
```

### Observer (Storage Side)

A dedicated process that ingests the agent's full conversation and extracts memories worth keeping. Unlike a lightweight summarizer, the observer is a **large-context specialized model** — chosen specifically for its ability to hold the entire conversation in context at once, reason about it holistically, and produce structured memory outputs.

**Model selection:**
- Use models with giant context windows: Gemini 2.5 Pro (1M tokens), GPT-4.1 (1M tokens), or similar
- These models are not doing the agent's primary work — they're running as a parallel filter/extractor
- The cost profile is different from the agent's reasoning model: the observer trades reasoning depth for breadth of context ingestion
- Can run a cheaper/smaller model for high-frequency low-stakes extraction, and escalate to the large-context model for periodic deep scans

**Characteristics:**
- Runs asynchronously, does not block or slow the agent
- Ingests the full conversation history (not just recent turns) — leveraging the large context window to see patterns across the entire session
- Can process phase boundaries, full tool call chains, and multi-step reasoning traces in a single pass
- Agent has zero awareness of being observed — no tool overhead, no "should I store this?" deliberation

**What to extract:**
- Mistakes made and corrections applied
- Domain facts discovered during research
- Tool usage patterns that worked or failed
- User preferences and conventions
- Schema/data insights from databases
- Recurring problems and their solutions
- **Entity relationships** (for knowledge graph storage) — e.g., "this API endpoint requires this auth method", "this table relates to that domain concept"
- **Structural patterns** (for regex/rule indexing) — e.g., recurring code patterns, naming conventions, config structures

**Memory classification and routing:**
The observer doesn't just extract — it classifies each memory and routes it to the appropriate storage backend(s). A single insight may be stored in multiple backends simultaneously:

| Memory Type | Primary Store | Example |
|-------------|---------------|---------|
| Factual knowledge | Dense vector + keyword index | "The `users` table uses soft deletes via `deleted_at`" |
| Entity relationships | Knowledge graph (Neo4j) | "ServiceA depends on ServiceB via gRPC" |
| Procedural patterns | Regex/rule index + sparse vector | "Always run migrations before seeding" |
| Domain vocabulary | Keyword index + dense vector | "In this codebase, 'expert' means agent configuration" |
| Error-solution pairs | Dense vector + knowledge graph | "TimeoutError on Neo4j → increase `max_connection_lifetime`" |

**Storage format:**
Each memory is a structured record with:
- Content (the actual insight, 1-3 sentences)
- Memory type classification (factual, relational, procedural, vocabulary, error-solution)
- Source metadata (job ID, phase, timestamp, turn range)
- Topic tags (for keyword-based matching)
- Confidence score (observer's assessment of how reliable/useful this memory is)
- **Scope** (determines visibility — see Memory Scoping below)

**Memory Scoping:**

Memories exist at different scopes, forming a hierarchy that mirrors how work is organized:

| Scope | Lifetime | Visible To | Example |
|-------|----------|-----------|---------|
| **Job** | Single job | Only the job that created it | "The `orders` table has a nullable `shipped_at` column" |
| **Project** | Across jobs in a project | All jobs belonging to the same project | "This codebase uses snake_case for DB columns and camelCase in the API layer" |

A **project** is one level above a job — it groups related jobs that contribute to a larger goal. For example, building a feature might span multiple jobs (one for the backend, one for the frontend, one for tests), each producing its own PR. Project-scoped memories let later jobs benefit from what earlier jobs discovered, without leaking knowledge into unrelated work.

**How scoping works:**
- The observer tags each memory with a scope at creation time. Job-specific details (intermediate findings, trial-and-error results, schema quirks relevant only to the current task) stay job-scoped. Broader insights (codebase conventions, architectural patterns, domain knowledge, user preferences) are promoted to project scope.
- During retrieval, the query fans out across both the current job's memories *and* the parent project's memories, with job-scoped results ranked slightly higher (more specific = more relevant).
- Project-scoped memories accumulate across jobs, giving the agent institutional knowledge that grows with the project. A job started in week 5 of a project benefits from everything learned in weeks 1-4.

**Note:** Projects are not yet implemented in the orchestrator. The current system only tracks jobs. When the project entity is added, the memory schema will need a `project_id` column alongside `job_id`, and retrieval queries will need to union across both scopes. The scoping model is designed to be additive — job-bound memories work standalone and project scope is layered on top.

Depending on classification, the memory is then indexed into one or more backends:
- **Dense vector embedding** (pgvector / dedicated vector store) — for semantic similarity search
- **Sparse vector embedding** (BM25 / SPLADE) — for keyword-aware retrieval that complements dense vectors
- **Knowledge graph nodes/edges** (Neo4j) — for entity relationships and traversal queries
- **Keyword index** (inverted index / PostgreSQL full-text search) — for exact term matching
- **Regex/rule index** — for pattern-based retrieval (e.g., "surface all memories about Neo4j when the agent calls `execute_cypher_query`")

**Key insight on embedding:** The embedding should capture *when this memory would be useful again*, not just what happened. A memory like "Neo4j MERGE is faster than CREATE+MATCH for this schema" should surface when the agent is about to write Cypher, not just when someone mentions Neo4j.

**Multi-graph decomposition (from MAGMA):**
Rather than storing all memories in a single flat store, consider representing each memory across multiple orthogonal graphs — each capturing a different dimension of the memory:

| Graph | What It Captures | Query Example |
|-------|-----------------|---------------|
| **Semantic** | Meaning, topic similarity | "What do I know about authentication?" |
| **Temporal** | When things happened, causal ordering | "What did I do right before phase 3 failed?" |
| **Causal** | Cause-effect chains | "What caused the timeout error?" |
| **Entity** | Relationships between concrete things | "What depends on the users table?" |

MAGMA showed 45.5% higher reasoning accuracy by separating these dimensions rather than entangling them. The key insight: **different queries need different graph traversals**. A semantic query ("how does auth work?") and a temporal query ("what changed since yesterday?") should follow different retrieval paths, not both hit the same vector index.

**Foresight signals (from EverMemOS):**
At memory creation time, the observer can also generate **foresight signals** — predictions about when and how a memory will be useful in the future. Rather than just storing "the API rate-limits to 100 req/min", also store a foresight: "relevant when: agent is about to make batch API calls or implement retry logic." This front-loads retrieval intelligence into the memory itself, making programmatic recall more precise without needing an LLM in the retrieval loop.

### Retrieval (Injection Side)

Relevant memories are surfaced into the agent's context based on its current activity.

**Trigger sources:**
- Current todo description and phase context
- Recent tool calls and their results
- Current workspace.md and plan.md content
- Activity patterns (e.g., starting a phase, writing to a database, doing research)

**Matching strategies (fan-out across all backends):**
Retrieval queries all available backends in parallel and merges results using reciprocal rank fusion (RRF) or a learned re-ranker. Note: Zep/Graphiti achieves 94.8% accuracy with **zero LLM calls** in its retrieval path — the research confirms that programmatic hybrid retrieval is not just cheaper but competitive with LLM-mediated retrieval:

1. **Dense vector similarity** — Embed current context, query vector store for nearest neighbors (semantic recall)
2. **Sparse vector / BM25** — Keyword-aware retrieval that catches exact terms dense vectors may miss (e.g., specific error codes, function names)
3. **Knowledge graph traversal** — When the agent is working with known entities, traverse the graph for related memories (e.g., "agent is modifying ServiceA → recall all memories about ServiceA's dependencies")
4. **Keyword/topic matching** — Fast exact-match lookup via inverted index or PostgreSQL full-text search
5. **Regex/rule triggers** — Deterministic rules that fire on specific tool calls or patterns (e.g., "before any `execute_cypher_query` call, surface Neo4j-tagged memories")
6. **Activity-pattern triggers** — Certain actions (e.g., starting a strategic phase, encountering an error) trigger recall from relevant categories

**Injection mechanism:**
- Similar to existing `workspace.md` transient injection (`src/core/workspace_injection.py`)
- A small "Relevant memories" block injected alongside workspace.md
- Only appears when match confidence exceeds a threshold
- Carries 2-3 most relevant memories per turn, stays silent otherwise

**Async retrieval:**
- Runs in the background during the `execute` node
- While the LLM generates its current response, a parallel process embeds the context and fetches memories for the *next* turn

### Distiller (Context Hygiene)

The third parallel system. While the observer handles *remembering*, the distiller handles **forgetting** — arguably the more important half. Without active forgetting, the conversation accumulates noise until the context window is full and emergency compaction kicks in (the current Layer 1/2 system). The distiller prevents this by continuously condensing old conversation, keeping the context lean by design rather than by panic.

**The human analogy:** You coded a function two hours ago. You don't remember the exact variable names, the trial-and-error, the syntax errors you fixed. You remember: "I built a parser that takes XML and returns a dict, it handles nested elements, it's in `utils/parser.py`." That's 30 tokens instead of 3,000. And if you need the exact details again, you look at the file — or in our case, the memory system surfaces them.

**How it works:**

The distiller runs asynchronously alongside the conversation. It operates on a sliding window — messages older than a threshold (e.g., N turns back from the current position) become candidates for distillation. Recent messages are never touched; the agent needs full fidelity for its current work.

**Two distillation strategies (can be combined):**

1. **Message compression** — Keep the same number of messages but strip noise from each. Remove verbose tool outputs, collapse repetitive reasoning, replace raw data with summaries. A 2,000-token tool result becomes a 200-token summary of what it contained.

2. **Message consolidation** — Merge multiple messages into fewer ones. 100 messages of back-and-forth become 30 that capture the same narrative arc. "Tried approach A, it failed because X. Switched to approach B, it worked. Key decision: used async instead of sync because of Y."

**What gets stripped (noise):**
- Raw tool outputs that have already been processed (the agent already acted on them)
- Trial-and-error sequences where only the final outcome matters
- Verbose error tracebacks (keep the error type and fix, discard the stack)
- Repetitive reasoning ("let me think about this..." → just the conclusion)
- Intermediate search results that led to a final answer
- Full file contents that were read but only a small part was relevant

**What gets preserved (signal):**
- Decisions made and their reasoning ("chose X over Y because Z")
- Outcomes of actions ("this worked" / "this failed because...")
- Current state and progress ("phases 1-3 complete, working on phase 4")
- Constraints discovered ("the API rate-limits to 100 req/min")
- The narrative thread — what the agent is doing and why

**Interaction with the memory system:**
This is what makes the two systems complementary. The distiller can be aggressive about stripping details *because* the observer has already extracted and stored them. The distiller doesn't need to preserve the fact that "Neo4j MERGE is faster than CREATE+MATCH" in the conversation — that's in the memory store and will be injected when relevant. The conversation just needs to say "optimized the Cypher queries" and move on.

```
Before distillation (messages 40-55, ~8,000 tokens):
  [40] Agent: Let me read the schema file
  [41] Tool result: <500 lines of SQL schema>
  [42] Agent: I see the users table has soft deletes. Let me check...
  [43] Agent: I'll try a JOIN approach first
  [44] Tool result: <query failed, error traceback>
  [45] Agent: That didn't work because of the nullable FK. Let me try...
  [46-53] <more trial and error>
  [54] Agent: Got it working with a LEFT JOIN + COALESCE
  [55] Tool result: <successful query output>

After distillation (~800 tokens):
  [40-55 summary] Explored the database schema. Key finding: users table
  uses soft deletes (deleted_at). After testing several JOIN strategies,
  settled on LEFT JOIN + COALESCE to handle nullable foreign keys.
  Wrote working query for user activity report.
```

**Third strategy: Context Folding (from FoldAgent research):**

3. **Sub-trajectory folding** — Rather than compressing the full conversation linearly, the agent can "fold" into temporary sub-trajectories for subtasks, then summarize and return to the main thread. This achieves **sub-linear context growth** — O(log n) instead of O(n). FoldAgent demonstrated 58% on SWE-bench Verified with only 32K tokens (tasks normally requiring 327K tokens). The key insight: context doesn't need to grow proportionally with work done. Our phase boundaries are a natural folding point — each phase transition is a "fold" where the previous phase's work is compressed into a summary.

**Distillation triggers:**
- **Distance-based**: Messages older than N turns from the head are candidates
- **Token-budget**: When total conversation exceeds a target budget (e.g., 50k tokens), distill the oldest undistilled segment
- **Phase boundaries**: When the agent transitions between strategic/tactical phases, the previous phase's conversation is a natural distillation candidate
- **Coordinated with observer**: The distiller should wait until the observer has processed a segment before condensing it — never discard what hasn't been remembered yet

**Model selection:**
- Can share the same large-context model as the observer (batch the work: extract memories, then produce the condensed version)
- Or use a dedicated summarization model — this is a simpler task than memory extraction, so a cheaper model may suffice
- The distiller sees the full conversation but only outputs the condensed replacement

**Relationship to existing compaction (Layer 1/2):**
The existing emergency compaction in `src/core/context.py` becomes a **last resort** rather than the primary mechanism. With continuous distillation:
- Layer 1 (pre-request check) rarely fires because the context stays within budget
- Layer 2 (emergency recovery) becomes a true safety net for edge cases
- The distiller replaces the blunt "summarize everything" approach with a nuanced, ongoing process

**Research validation (JetBrains, Dec 2025):** JetBrains compared observation masking (replacing older tool outputs with placeholders) vs. LLM summarization on SWE-bench Verified (500 instances, models 32B-480B). **Observation masking outperformed summarization in 4/5 settings** — 52% cost reduction + 2.6% solve rate boost. Summarization added 13-15% longer runs and 7%+ overhead from summary generation API calls. Implication: our existing `keep_recent_tool_results` pattern (masking) should be the **primary** defense, with LLM summarization reserved for deeper consolidation at phase boundaries. The hybrid approach — masking as default + selective summarization when needed — is the recommended strategy.

**Target context budget:**
The goal is to keep the active conversation within a **target token budget** (configurable, e.g., 40-60k tokens) at all times, regardless of how many turns have elapsed. This budget is split:
- ~70% conversation history (distilled)
- ~15% workspace.md + plan.md (current state)
- ~10% injected memories (from retrieval)
- ~5% system prompt + instructions

Within this budget, the agent can run indefinitely without degradation. A 500-turn job should have roughly the same context quality as a 50-turn job — just with more aggressive distillation of older segments.

---

## Context Buffer System (Evolution)

The architecture above still thinks in terms of "conversation with memory bolted on." The observer extracts, the distiller compresses, the retriever injects — but the fundamental unit is still a chat history that the model attends over. This section proposes the next evolution: **throw out conversation as the primary structure entirely**.

### The Insight

Do we even need a system prompt? Do we even need a conversation history?

A human engineer working on a feature doesn't carry a transcript of everything they've said and done today. They carry a **bag of relevant information** at varying levels of freshness: who they are (permanent), what project they're on (days), what feature they're building (hours), what they just tried (minutes). When they sit down to write code, their brain assembles the right context on the fly — it doesn't replay the morning's standup word-for-word.

Current LLM agents work the opposite way. They carry the full conversation, hope attention finds the relevant parts, and panic-summarize when the window fills up. The memory system (above) improves this by adding recall and forgetting. But the Context Buffer takes it further: **the context window is just an attention buffer. Fill it with whatever is most useful right now. It doesn't need to look like a conversation.**

### Architecture

```
┌───────────────────────────────────────────────┐
│              Context Buffer                    │
│         (assembled fresh each turn)            │
│                                                │
│  ┌──────────────────────────────────────────┐  │
│  │ Layer 0: Identity         TTL: ∞        │  │
│  │ "You are a software engineer..."         │  │
│  │ Tool definitions, output format rules    │  │
│  └──────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────┐  │
│  │ Layer 1: Mission          TTL: ~30 req   │  │
│  │ Job description, goals, constraints      │  │
│  │ Expert persona, domain knowledge         │  │
│  └──────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────┐  │
│  │ Layer 2: Project State    TTL: ~15 req   │  │
│  │ Plan.md summary, phase progress          │  │
│  │ Architecture decisions made so far       │  │
│  └──────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────┐  │
│  │ Layer 3: Recalled Memory  TTL: ~10 req   │  │
│  │ Retrieved from vector/graph/keyword      │  │
│  │ "The auth guard uses JWT, see src/..."   │  │
│  │ "User said reuse parts from llm_chat/"   │  │
│  └──────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────┐  │
│  │ Layer 4: Working Context  TTL: ~5 req    │  │
│  │ Current todo, recent tool results        │  │
│  │ Files just read, errors just hit         │  │
│  └──────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────┐  │
│  │ Layer 5: Immediate        TTL: 1 req     │  │
│  │ Current tool call result                 │  │
│  │ The actual prompt / next action needed   │  │
│  └──────────────────────────────────────────┘  │
│                                                │
│              ↓ assembled input ↓               │
│         ┌──────────────────────┐               │
│         │    LLM (base or      │               │
│         │    instruct model)   │               │
│         └──────────┬───────────┘               │
│                    │ output                     │
│                    ↓                            │
│         Next action / tool call / text          │
└───────────────────────────────────────────────┘
         ↑                          │
         │                          │
   ┌─────┴──────────┐    ┌─────────▼────────┐
   │ Memory Stores   │    │ Memory Writer    │
   │                 │    │ (every N turns)  │
   │ vector search ──┤    │                  │
   │ keyword match ──┤    │ Extracts new     │
   │ graph traverse ─┤    │ memories from    │
   │ regex rules   ──┤    │ recent output    │
   │                 │    │                  │
   │ NO LLM needed   │    │ Uses LLM to     │
   │ for retrieval    │    │ generate, but   │
   │                 │    │ only every few   │
   │                 │    │ turns            │
   └─────────────────┘    └──────────────────┘
```

### TTL-Layered Context

Every piece of information in the buffer has a **time-to-live (TTL)** measured in request cycles. Each turn, the assembler rebuilds the context from scratch by pulling from all sources, respecting TTLs, and fitting within the token budget.

| Layer | Content | TTL | Refresh |
|-------|---------|-----|---------|
| **Identity** | Persona, role, output format, tool definitions | Permanent | Never changes |
| **Mission** | Job description, goals, expert knowledge, constraints | ~30 requests | Refreshed on phase transitions |
| **Project State** | Plan summary, phase progress, architecture decisions | ~15 requests | Refreshed by memory writer after each phase |
| **Recalled Memory** | Relevant memories surfaced by search | ~10 requests | Refreshed every turn by retrieval layer |
| **Working Context** | Current todo, recent outcomes, files touched | ~5 requests | Decays naturally as new work arrives |
| **Immediate** | Current tool result, next action prompt | 1 request | Replaced every turn |

**TTL mechanics:**
- Each memory/fragment carries a `created_at_turn` and `ttl` value
- On each turn, the assembler filters out anything where `current_turn - created_at_turn > ttl`
- Expired items aren't deleted from storage — they just stop being included in the assembled context
- If an expired memory gets re-retrieved (because it's relevant again), it gets a fresh TTL
- Higher layers (identity, mission) have their TTL reset automatically — they're always present

**Token budget enforcement:**
The assembler works top-down. Identity and Mission always fit (they're compact and permanent). Then it fills in Project State, Recalled Memory, Working Context in order, stopping when the budget is full. If something has to be cut, lower-TTL items go first — the most ephemeral context is the most expendable.

### Memory Generation (No LLM for Retrieval)

This is a critical distinction from the conversational approach: **retrieval is entirely programmatic, no LLM in the loop.**

The retrieval side is pure search:
- **Vector similarity**: Embed the current working context, query for nearest neighbors
- **Keyword/BM25**: Extract keywords from the current todo and recent output, search the index
- **Graph traversal**: If the agent is touching known entities (a file, a service, a table), walk the graph for related memories
- **Regex/rule triggers**: Deterministic rules fire on tool names, error patterns, file paths

None of this needs an LLM. It's search infrastructure — fast, cheap, deterministic. The expensive LLM only runs on the **generation side**: every N turns (e.g., every 5-10 requests), a model scans the recent conversation segment and produces new memory fragments to store. This is the observer from the previous architecture, but now it's the *only* place an LLM is used outside the main agent.

```
Turn 1-5:   Agent works. Retrieval is vector/keyword search (no LLM).
Turn 5:     Memory writer (LLM) scans turns 1-5, extracts 3 new memories.
Turn 6-10:  Agent works. New memories are now retrievable. No LLM for retrieval.
Turn 10:    Memory writer scans turns 6-10, extracts 2 new memories.
...
```

### Beyond Conversational Inference

This approach fundamentally changes what the model's input looks like. It's no longer a conversation:

```
Traditional (conversation-shaped):
  system: "You are a helpful assistant..."
  user: "Please implement the auth guard"
  assistant: "Let me check the existing code..."
  tool_result: <file contents>
  assistant: "I see the pattern. Let me..."
  ...200 more messages...
  assistant: <next response>

Context Buffer (information-shaped):
  [IDENTITY] You are a software engineer. You output tool calls or text.
  [MISSION] Build an auth guard for the Angular cockpit. JWT-based.
  [STATE] Phase 3 of 5. Phases 1-2 complete: set up routing, created models.
  [MEMORY] The advanced_llm_chat folder has a similar auth implementation.
  [MEMORY] User prefers reusing existing patterns over writing from scratch.
  [MEMORY] The interceptor pattern in Angular uses HTTP_INTERCEPTORS token.
  [WORKING] Current todo: implement the JWT interceptor.
  [WORKING] Just read auth.service.ts — has login/logout but no token refresh.
  [IMMEDIATE] Last tool result: <contents of interceptor.ts>
  → Generate next action.
```

This is closer to how you'd prompt a **base model** or an **instruct model** than how you'd run a chat. The input is a structured bag of information — identity, state, memories, immediate context — and the model generates the most likely useful next action based on attention over all of it. There's no conversation to maintain, no history to compress, no messages to distill. Each turn is a fresh assembly of the most relevant information available.

**Why this might work better than conversational inference:**
- Attention is spent on *relevant* information, not on reconstructing context from a long chat history
- No wasted tokens on "let me think about this" or "as I mentioned earlier" — the model just sees the facts
- The model doesn't need to maintain coherence with 200 previous messages — it just needs to produce the right next action given the current state
- Context quality is constant regardless of how long the job has been running — turn 500 gets the same quality input as turn 5
- Works with base/instruct models, not just chat-tuned models — opens up the model selection space

**Why this might be risky:**
- Chat-tuned models are trained on conversation. Giving them non-conversational input might degrade output quality.
- Some reasoning benefits from seeing the chain of thought that led to the current state — a summary might lose important nuance
- Tool call formatting may depend on the conversational structure (assistant → tool_call → tool_result pattern)
- Need to validate that the assembled context actually produces coherent tool-calling behavior

### Incremental Migration Path

These two approaches aren't mutually exclusive. They can be adopted incrementally:

**Stage 1 (Current system + memory):** Keep the conversation. Add the observer and retrieval layer from the first architecture. This is the lowest-risk starting point — add memory on top of what already works.

**Stage 2 (Conversation + distiller):** Add the distiller to aggressively compress old conversation. The context is still conversation-shaped, but much leaner. Memory handles recall of stripped details.

**Stage 3 (Hybrid buffer):** Start replacing older distilled conversation segments with structured memory blocks. The recent conversation stays conversational (last ~10 turns), but everything older is replaced with assembled memory layers. The model sees: `[MEMORY BLOCKS] + [RECENT CONVERSATION] + [IMMEDIATE]`.

**Stage 4 (Full context buffer):** Drop the conversation entirely. Every turn is a fresh assembly. The model never sees "what it said before" — it sees the current state of the world and generates the next action. This is the most radical step and needs the most validation.

Each stage can be evaluated independently. If Stage 3 works well enough, Stage 4 might not be necessary. If Stage 4 works, it's a fundamentally different (and potentially more efficient) way to run an agent.

---

## Perception System (Sensory Layer)

The systems above handle memory and context management — what the agent *knows* and *remembers*. But there's a missing piece: **what the agent sees right now**. A human engineer doesn't just have memories and a task list — they have eyes on the screen, peripheral awareness of their environment, and the ability to direct their focus. The perception system gives the agent the same capability.

### The Human Visual Attention Model

When you look at your screen, your brain does something remarkable in milliseconds:

1. **Peripheral scan** — You see everything at low resolution. Desktop background, open windows, taskbar, terminal output. You don't process it all, but you're *aware* it's there.
2. **Attention selection** — Your task ("style this button") activates a filter. Your visual cortex automatically highlights anything button-shaped in your field of view, suppressing everything else.
3. **Focused inspection** — Your eyes zero in on the button. But not all of it at once. First shape: "Is it round? Yes." Then elevation: "Does it look raised? No shadow, no hover effect." Then edges: your focus narrows to just the border, checking contrast.
4. **Q&A loop** — Each observation triggers a question from the reasoning brain, which redirects the eyes. "Is the shape right?" → eyes check shape → "yes" → "Is it elevated?" → eyes check shadow → "no" → "What does elevated mean here?" → reasoning pulls up design requirements from memory → "needs box-shadow and hover transform" → eyes check CSS → ...

The reasoning brain never processes raw pixels. It asks questions, and the visual system returns answers at the right level of detail. The key insight: **you don't need an expensive multimodal reasoning model to see. You need a cheap vision model that the reasoning model can direct.**

**Research validation:**
- **Look, Focus, Act (LFA)** implements exactly this pattern as foveated vision transformers — dense patches at the gaze point, sparse patches in the periphery. Achieves **94% reduction in ViT tokens** (324 uniform patches → 20 foveated patches), 7-8x training speedup. Empirically confirms that replicating high-resolution processing across the full visual field would require ~1000x more computation.
- **D2Snap** demonstrates that for GUI agents, **text-only descriptions (63% success) nearly match screenshots (65% success)**, and hybrid DOM text (73%) beats both. Crucially, **hierarchy is the key structural feature** — tree-structured descriptions outperform linearized alternatives. This validates our "descriptions over pixels" principle.
- **Visual Perception Tokens** takes this further — models autonomously generate "look here" tokens as part of normal text generation, giving the LLM direct control over its own visual attention. A 2B model with perception tokens outperformed a 7B baseline by 20%.
- **Screen2AX** generates accessibility trees from screenshots, outperforming OmniParser V2 for visual grounding. The production reality is "accessibility tree first, vision fallback" — structured text handles 95% of cases cheaply.
- **Foveated vision research** (Frontiers in Neuroscience, 2025) provides empirical evidence that agents with constrained visual processing actually **learn better representations** than those with full-field high resolution.

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  Reasoning Model                         │
│                                                          │
│  "I need to style this button"                          │
│       │                                                  │
│       │ focus_request("button", detail="shape")         │
│       ▼                                                  │
│  ┌──────────────────────────────────────────────────┐   │
│  │              Perception Controller                │   │
│  │                                                   │   │
│  │  Receives focus requests from reasoning model     │   │
│  │  Routes to appropriate vision pipeline            │   │
│  │  Returns descriptions, not raw images             │   │
│  └──────┬──────────────────────┬─────────────────────┘   │
│         │                      │                         │
│    ┌────▼────────┐    ┌───────▼──────────┐              │
│    │ Peripheral   │    │ Focused          │              │
│    │ Vision       │    │ Vision           │              │
│    │              │    │                  │              │
│    │ Small/fast   │    │ Detailed model   │              │
│    │ model        │    │ (VisionHelper)   │              │
│    │              │    │                  │              │
│    │ "Screen has  │    │ "Button is 40px  │              │
│    │  a terminal, │    │  round, #3B82F6  │              │
│    │  browser,    │    │  background, no  │              │
│    │  file mgr"   │    │  box-shadow, no  │              │
│    │              │    │  hover state"    │              │
│    └──────────────┘    └──────────────────┘              │
│         │                      │                         │
│         ▼                      ▼                         │
│    Feeds into              Feeds into                    │
│    Layer 4: Working        Layer 5: Immediate            │
│    Context (ambient)       (focused observation)         │
└─────────────────────────────────────────────────────────┘
          │
          ▼
    Memory Stores
    (visual observations become memories too)
```

### Two-Level Vision Pipeline

Just like human vision has peripheral and foveal processing, the perception system has two tiers:

**Level 1: Peripheral / Ambient (cheap, always-on)**

A small, fast model (or even rule-based screen parsing) provides a low-resolution description of the current environment. This runs automatically and feeds into the Working Context layer of the context buffer.

What it captures:
- What's on screen (terminal, browser, IDE, file manager)
- General layout ("browser shows a form with three inputs and a submit button")
- Visible text at a glance (terminal prompt, page title, error banner)
- State changes ("new error appeared in terminal", "page finished loading")

This is the equivalent of peripheral vision — you don't think about it, you just know it's there. The agent doesn't explicitly request this; it's injected as ambient context. Most of the time the agent ignores it. But when it sees "error appeared in terminal," attention shifts.

**Level 2: Focused / Directed (detailed, on-demand)**

When the reasoning model needs to examine something closely, it uses focus tools to direct the vision system. This is where the existing `VisionHelper` and `DocumentRenderer` infrastructure (`src/services/`) comes in.

Focus tools (parallel tool-calling capable):

| Tool | Purpose | Example |
|------|---------|---------|
| `focus_region` | Direct attention to a screen area | `focus_region("top-right quadrant")` |
| `get_shape` | Describe shape/layout of an element | `get_shape("submit button")` → "40px rounded rectangle" |
| `get_text` | Extract text content from a region | `get_text("error banner")` → "TypeError: undefined is not..." |
| `get_style` | Describe visual styling | `get_style("submit button")` → "blue bg, white text, no shadow" |
| `compare_visual` | Compare current state to a reference | `compare_visual("button", reference="design_mockup.png")` |
| `describe_scene` | Full description of current view | `describe_scene()` → detailed layout description |

**The key: the reasoning model never sees raw images.** It sees text descriptions at the level of detail it asked for. This means:
- The reasoning model doesn't need to be multimodal — any text model can work
- Vision costs are proportional to attention, not to screen resolution
- The reasoning model controls its own "eye movements" through tool calls

### The Focus Loop

This mirrors the human visual attention cycle:

```
Reasoning: "I need to check if the button styling matches the design spec."
    │
    ├→ get_shape("submit button")
    │   └→ "Rounded rectangle, 40x12px, centered in form footer"
    │
    ├→ Reasoning: "Shape looks right. What about elevation?"
    │
    ├→ get_style("submit button")
    │   └→ "bg: #3B82F6, text: white, border: none, box-shadow: none"
    │
    ├→ Reasoning: "No box-shadow. Design spec says elevated buttons
    │   need shadow. Let me check hover state too."
    │
    ├→ focus_region("submit button", state="hover")
    │   └→ "No hover state change detected. Cursor changes to pointer."
    │
    └→ Reasoning: "Button needs box-shadow and hover transform.
        Adding to my working context. Next todo: fix button elevation."
```

At no point did the reasoning model process a screenshot. It asked 3 targeted questions through the vision pipeline and got text descriptions back. Total vision cost: 3 small-model calls. If the button had looked correct, it would have stopped after the first call.

### Visual Observations as Memory

Everything the perception system observes feeds back into the memory stores. This is crucial — visual observations aren't disposable. They become part of the agent's experience:

- **Ambient observations** → Working Context (short TTL, ~5 requests) and optionally stored as memories by the observer if noteworthy
- **Focused observations** → Immediate context (TTL: 1) but also indexed into memory stores for future recall
- **Visual patterns** → Knowledge graph ("this component always has a shadow", "error banners appear at the top of the page")

Example memory formation from visual input:
```
Observation: "The auth form has no loading state on submit"
    → Stored as: factual memory + keyword-indexed under "auth", "form", "loading"
    → Later, when building another form, retrieval surfaces:
       "Previous form (auth) was missing loading state — remember to add one"
```

### Environment State Injection

Beyond active visual inspection, the system maintains a passive model of the current environment. This is the "I can see files on my desktop background" equivalent — you're not looking at them, but you know they're there.

**What constitutes "environment state":**
- Open terminal sessions and their recent output
- Current working directory and visible files
- Running processes / services (dev server, database, etc.)
- Browser state (if applicable): current URL, page title, visible elements
- IDE state: open files, cursor position, visible diagnostics
- System notifications or alerts

This state is captured by lightweight, non-LLM methods where possible:
- Terminal output: just read the buffer (text, no vision needed)
- File listings: `ls` / glob (already have these tools)
- Process state: `ps` / port checks
- Browser state: Playwright's DOM inspection (text-based, not screenshot)

Only when the agent needs to *understand visual layout* does it escalate to the vision pipeline. Reading text from a terminal doesn't need a vision model. Checking if a button's shadow looks right does.

### Integration with Existing Infrastructure

The perception system builds on what already exists in the codebase:

| Existing | Used For |
|----------|----------|
| `VisionHelper` (`src/services/vision_helper.py`) | Level 2 focused vision — generates text descriptions from images |
| `DocumentRenderer` (`src/services/document_renderer.py`) | Renders documents/pages as images for vision processing |
| `DescriptionCache` (`src/services/description_cache.py`) | Caches vision descriptions to avoid redundant calls |
| `read_file` with `describe` param | Already supports visual Q&A on documents |
| `browse_website` tool | Playwright-based browser automation — can capture screenshots |
| Multimodal config (`llm.multimodal`) | Already distinguishes between models that see images vs. need descriptions |

The new pieces to build:
- **Perception Controller**: Routes focus requests to the right vision pipeline
- **Ambient Scanner**: Lightweight background process for peripheral state
- **Focus Tools**: `focus_region`, `get_shape`, `get_text`, `get_style`, `compare_visual`, `describe_scene`
- **Environment State Collector**: Aggregates terminal, file, process, browser state into the context buffer

### Multi-Model Vision Stack

Different visual tasks need different models, optimized for cost and speed:

| Tier | Model Type | Use Case | Cost |
|------|-----------|----------|------|
| **Ambient** | Tiny/fast (gpt-4o-mini, or rule-based screen parsing) | Peripheral awareness, layout description, text extraction | Very low |
| **Descriptive** | Mid-tier vision model | Focused descriptions, style analysis, element identification | Low |
| **Analytical** | Full vision model (gpt-4o, Gemini Pro) | Complex visual comparisons, design spec validation, visual debugging | Medium |
| **None** | Text-only extraction | Terminal output, DOM inspection, file contents | Zero (no vision model) |

The reasoning model never chooses the tier directly. The Perception Controller routes based on the type of focus request:
- `get_text()` → text extraction, no vision model needed
- `get_shape()` → ambient tier sufficient
- `get_style()` → descriptive tier
- `compare_visual()` → analytical tier

**Future direction: Autonomous visual attention.** Visual Perception Tokens research shows models can learn to generate attention-directing tokens as part of their normal text output — the model itself decides when and where to re-examine an image without explicit tool calls. This would remove the need for the Perception Controller routing layer entirely — the reasoning model and visual attention would be integrated.

**Practical shortcut: Structured visual parsing.** For GUI/web tasks, tools like OmniParser V2 and Screen2AX can convert screenshots into structured text representations (DOM-like trees, accessibility trees) that any text-only LLM can reason about. This bypasses the multi-model vision stack entirely for the majority of visual tasks, reserving actual vision models only for cases where layout/style/design judgment is needed.

## Design Principles

1. **Separation of concerns** — The agent reasons. The observer remembers. The distiller forgets. The retriever recalls. The perception system sees. None of them know about each other's internals.
2. **No agent modification** — Existing agent code stays untouched on the execution side. Memory, distillation, and perception are infrastructure, not features the agent learns to use.
3. **Right model for the right job** — The agent uses the best reasoning model. The observer/distiller use the best context-ingestion models. The perception system uses the cheapest model that can answer the question. Don't force one model to do everything.
4. **Multi-backend redundancy** — No single retrieval method is sufficient. Dense vectors miss exact terms. Keywords miss semantic similarity. Graphs miss unstructured insights. Use all of them and fuse results. (Confirmed by every top-performing system in the 2025-2026 research: Zep, Mnemosyne, Hindsight all use 3+ retrieval channels.)
5. **Forgetting is as important as remembering** — An agent that remembers everything but never forgets will drown in its own context. The distiller is not a cost optimization — it's a cognitive necessity. Strip the noise so the signal can breathe. (Research confirms: utility-based deletion yields 10%+ performance gains over exhaustive storage.)
6. **Signal over noise** — Better to surface nothing than to surface irrelevant memories. Better to distill aggressively than to carry dead weight. High retrieval threshold and low context budget by default.
7. **Passive by design** — Like human memory: you don't decide to remember, it just happens. You don't decide to forget, it just fades. You don't decide to recall, it just surfaces. You don't decide to see your environment, it's just there.
8. **Indefinite operation** — With distillation + memory, there is no fundamental limit on conversation length. A 1,000-turn job should work as well as a 50-turn job. The context window is a budget, not a wall.
9. **Attention is cheap, processing is expensive** — Don't send a full screenshot to a reasoning model. Send a text question to a vision model. The reasoning model directs attention; specialized models do the perceiving. Cost scales with curiosity, not with screen resolution. (LFA research: 94% token reduction through foveated attention.)
10. **Descriptions over pixels** — The reasoning model works in text. Visual information enters the system as text descriptions, never as raw images. This decouples reasoning capability from multimodal capability entirely. (D2Snap: text-only descriptions nearly match screenshot performance; hierarchy is the key feature, not pixels.)
11. **Context quality over context quantity** — Intelligent management of a small context window beats naively filling a large one. A 32K window with context folding outperforms 327K of raw conversation on SWE-bench. The goal is the smallest set of high-signal tokens, not the largest possible context.
12. **Temporal awareness** — Memories are not static facts. Knowledge evolves over time — what was true in phase 1 may be outdated by phase 5. Bi-temporal tracking (when it happened vs. when we learned it) enables accurate point-in-time reasoning and prevents stale memories from overriding fresh observations.
13. **Memories evolve** — New observations should trigger updates to existing memories, not just create new ones. A memory system that only appends will accumulate contradictions. Deduplication, merging, and supersession are not optimizations — they're correctness requirements. (Validated by A-Mem, Mem0 AutoDedup, and Graphiti's two-phase dedup.)

## Open Questions

- **Storage backend mix:** pgvector in existing PostgreSQL for vectors + existing Neo4j for knowledge graph? Or dedicated services (Qdrant/Weaviate for vectors, separate Neo4j instance for memory graph)? *Leaning toward*: PostgreSQL-native (Hindsight proves 91.4% accuracy with pgvector alone). Neo4j for graph traversal as an upgrade path.
- **Observer frequency:** Every N turns? On phase transitions? On tool errors? Adaptive? Two-tier: cheap model on every turn + large-context deep scan on phase boundaries?
- **Memory lifecycle:** Do memories decay over time? Can they be consolidated/merged? Is there a cap per backend? *Research direction*: ACT-R temporal decay model (activation = f(time, access frequency, noise)). Mnemosyne uses activity-based boosting with link decay. EverMemOS consolidates MemCells into thematic MemScenes.
- **Cross-job memory:** ~~Should memories from one job be available to other jobs?~~ *Resolved*: Yes — via project-scoped memories (see Memory Scoping in the Observer section). Jobs within the same project share project-scoped memories. Remaining question: should there be an even broader scope (agent-level or global) for knowledge that transcends projects?
- **Feedback loop:** If a surfaced memory leads to better outcomes, should it be reinforced? If ignored repeatedly, should it decay? *Research direction*: AgeMem (Jan 2026) trains memory management via RL — the agent learns its own retention/forgetting policy through step-wise GRPO.
- **Fusion strategy:** How to merge results from 5+ retrieval backends? RRF (simple, no training)? Learned re-ranker (better but needs data)? Weighted scoring with tunable per-backend weights? *Current best practice*: RRF with k=50-60 as default (used by Supabase, ParadeDB, Hindsight). Mnemosyne uses explicit 70/20/10 weighting (vector/keyword/graph). MAGMA uses query-adaptive routing. Start with weighted RRF, tune weights empirically.
- **Graph schema for memories:** What node/edge types in the knowledge graph? How to prevent the memory graph from becoming a disconnected mess over time? *Research direction*: MAGMA's 4 orthogonal graphs (semantic, temporal, causal, entity) prevent entanglement. Graphiti's two-phase dedup (deterministic + LLM) prevents duplicate entities.
- **Observer model failover:** If the large-context model is unavailable or rate-limited, fall back to chunked processing with a smaller model? Or skip the deep scan entirely?
- **Distillation granularity:** Compress at the message level (shrink each message) or segment level (merge N messages into one summary)? Or adaptive based on content type?
- **Distillation fidelity validation:** How to verify the distilled version doesn't lose critical information? Run a check against the memory store to confirm key facts were captured before discarding?
- **Observer-distiller coordination:** Should they share a model and batch the work (extract memories, then distill, in one pass)? Or run independently? Shared pass is cheaper but couples the systems.
- **Context budget tuning:** What's the optimal target? Too aggressive and the agent loses narrative thread. Too loose and context fills up. Adaptive budget based on task complexity?
- **Chat vs. base models for context buffer:** Do chat-tuned models degrade on non-conversational input? Should we test with instruct/base models that don't expect conversation structure?
- **TTL calibration:** How to determine optimal TTLs per layer? Static config? Adaptive based on task type? Learn from which memories the agent actually uses vs. ignores?
- **Tool call format in non-conversational input:** Current tool-calling relies on the assistant→tool_call→tool_result message pattern. Does this break when the input is a flat context buffer? Need to test with function-calling APIs.
- **Memory writer frequency:** Every 5 turns? Every 10? Adaptive based on information density? Too frequent = wasted LLM calls. Too infrequent = memories lag behind the conversation.
- **Hybrid buffer boundary:** In Stage 3 (hybrid), where's the cutoff between "recent conversation" and "memory blocks"? Last 5 turns? Last 10? Based on token count rather than turn count?
- **Ambient scan frequency:** How often does peripheral vision update? Every turn? Only when the agent calls a tool that changes screen state? Event-driven (file watcher, terminal output listener)? *Research direction*: Roboflow's gating pattern — a cheap detector (YOLO11 at 200+ FPS) runs continuously and only escalates to expensive models when something interesting is detected.
- **Vision model selection:** Which models for which tier? Can the ambient tier be rule-based (DOM parsing, terminal buffer reading) instead of a vision model at all? *Answer from research*: Yes. D2Snap and Screen2AX demonstrate that structured text parsing (DOM trees, accessibility trees) outperforms vision models for most GUI tasks. Vision models reserved for layout/style judgment only.
- **Focus tool granularity:** How specific can focus requests be? "Top-right quadrant" vs. "the submit button" vs. "the border-radius of the submit button"? Does the Perception Controller need to interpret natural-language focus requests? *Research direction*: ARGUS grounds each reasoning step to a spatial region via bounding-box chain-of-thought. ShowUI's UI-guided token selection identifies which regions are redundant vs. informative.
- **Visual memory retention:** How long do visual observations persist in memory? A layout description from 50 turns ago is probably stale. Should visual memories have shorter default TTLs than factual memories?
- **Screen capture mechanism:** How to capture what the agent "sees"? Playwright for browsers, terminal buffer for shells — what about native desktop apps, remote sessions, or headless environments? *Research direction*: Screen2AX generates accessibility metadata from any screenshot. OmniParser V2 provides structured parsing for arbitrary UIs.
- **Perception cost budget:** Vision API calls cost money. Should there be a per-turn or per-job budget for perception? Auto-throttle ambient scanning when costs exceed threshold? *Research data point*: LFA achieves 94% token reduction through foveated attention — cost scales with curiosity, not screen resolution.
- **Embedding model choice:** OpenAI text-embedding-3-large (Matryoshka, truncatable to 256-dim)? BGE-M3 (produces dense+sparse+ColBERT in one pass)? Nomic Embed Text V2 (open-source, runs locally)? *Trade-off*: BGE-M3 eliminates separate sparse indexing but requires local inference. OpenAI embeddings are easiest to start with.
- **BM25 vs. ts_rank:** PostgreSQL's native `ts_rank` must score every matching document — it scales poorly for common terms. BM25 extensions (ParadeDB pg_search, VectorChord-bm25) provide proper TF-IDF with Block-Max WAND optimization. Worth adding as the memory store grows.
- **Multi-graph vs. single-store:** MAGMA's orthogonal graphs are powerful but complex. For memory light, start with flat storage + typed memories. Add graph decomposition when the single-store approach hits retrieval quality limits.

## Related

- [[obsidian]] - Obsidian-style knowledge workspace with Zettelkasten note-taking
- [[context_management]] - Context window management and compaction strategies
- [[working_memory]] - Working memory implementation for the agent
- [[vectorization]] - Vector embedding approaches for semantic search
- [[agent_improvements]] - General agent improvement proposals
- [[vision_helper]] - Existing VisionHelper service for image descriptions
- [[browser_tools]] - Playwright-based browser automation and screenshot capture
- [[memory_light]] - Implementation roadmap for the first buildable version

## External References

**Surveys & Foundational:**
- [Memory in the Age of AI Agents (Survey, Dec 2025)](https://arxiv.org/abs/2512.13564) — [Paper List](https://github.com/Shichun-Liu/Agent-Memory-Paper-List)
- [Continuum Memory Architecture (Jan 2026)](https://arxiv.org/abs/2601.09913)
- [ICLR 2026 MemAgents Workshop](https://openreview.net/forum?id=U51WxL382H)

**Memory Architectures:**
- [MAGMA: Multi-Graph Agentic Memory (Jan 2026)](https://arxiv.org/abs/2601.03236)
- [EverMemOS: Self-Organizing Memory OS (Jan 2026)](https://arxiv.org/abs/2601.02163)
- [SimpleMem: Efficient Lifelong Memory (Jan 2026)](https://arxiv.org/abs/2601.02553)
- [A-Mem: Zettelkasten-Inspired Memory (NeurIPS 2025)](https://arxiv.org/abs/2502.12110)
- [MemR3: Reflective Reasoning Retrieval (Dec 2025)](https://arxiv.org/abs/2512.20237)
- [ACT-R-Inspired Memory for LLMs (2025)](https://dl.acm.org/doi/10.1145/3765766.3765803)
- [AgeMem: RL-Learned Memory Management (Jan 2026)](https://arxiv.org/html/2601.01885v1)

**Production Frameworks:**
- [Zep/Graphiti (Temporal KG)](https://arxiv.org/abs/2501.13956) — [GitHub](https://github.com/getzep/graphiti)
- [Letta/MemGPT](https://www.letta.com/) — [Context Repositories](https://www.letta.com/blog/context-repositories)
- [Mem0](https://arxiv.org/abs/2504.19413) — [Graph Memory](https://mem0.ai/blog/graph-memory-solutions-ai-agents)
- [Mnemosyne (Rust, Claude Code MCP)](https://rand.github.io/mnemosyne/)
- [Hindsight (PostgreSQL-native, 91.4% LongMemEval)](https://github.com/vectorize-io/hindsight)
- [Cognee (Graph+Vector)](https://github.com/topoteretes/cognee)

**Context Management:**
- [JetBrains: Observation Masking vs Summarization (Dec 2025)](https://blog.jetbrains.com/research/2025/12/efficient-context-management/)
- [Context-Folding / FoldAgent (Oct 2025)](https://arxiv.org/abs/2510.11967) — [GitHub](https://github.com/sunnweiwei/FoldAgent)
- [GCC: Git-Context-Controller (Jul 2025)](https://arxiv.org/abs/2508.00031) — [GitHub](https://github.com/theworldofagents/GCC)
- [Anthropic: Effective Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [LLMLingua-2: Prompt Compression (Microsoft)](https://llmlingua.com/llmlingua2.html)
- [InfiniteICL: Context as Parameter Updates (ACL 2025)](https://arxiv.org/abs/2504.01707)

**Visual Perception:**
- [Look, Focus, Act: Foveated Vision (Jul 2025)](https://arxiv.org/html/2507.15833v1)
- [D2Snap: DOM Downsampling for Agents (Aug 2025)](https://arxiv.org/html/2508.04412v1)
- [Visual Perception Tokens (Feb 2025)](https://arxiv.org/abs/2502.17425)
- [OmniParser V2 (Microsoft, Feb 2025)](https://www.microsoft.com/en-us/research/articles/omniparser-v2-turning-any-llm-into-a-computer-use-agent/)
- [Screen2AX: Accessibility Trees from Screenshots (Jul 2025)](https://arxiv.org/abs/2507.16704)
- [ShowUI: UI-Guided Visual Token Selection (CVPR 2025)](https://github.com/showlab/ShowUI)
- [ARGUS: Grounded Chain-of-Thought (CVPR 2025, NVIDIA)](https://yunzeman.github.io/argus/)

**Benchmarks:**
- [BEAM: Beyond A Million Tokens (Oct 2025)](https://arxiv.org/abs/2510.27246)
- [LongMemEval, LoCoMo, DMR](https://github.com/Shichun-Liu/Agent-Memory-Paper-List) — standard memory benchmarks
