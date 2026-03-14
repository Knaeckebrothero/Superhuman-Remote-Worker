---
tags:
  - context-management
  - agent-architecture
  - knowledge-management
  - memory
  - research
aliases:
  - context engineering
  - memory ideas
  - retrieval innovations
related:
  - "[[memories_mechanism]]"
  - "[[memory_light]]"
  - "[[project_knowledge_base]]"
  - "[[context_management]]"
  - "[[working_memory]]"
---

# Context Engineering & Memory: Ideas Collection

> **Purpose:** Unified reference for context engineering techniques, agent memory architectures, and retrieval innovations — cross-referenced against what this project already has, what's planned, and what's new from external research. This document is a living ideas collection, not a spec.
>
> **Date:** 2026-03-14
>
> **Existing project docs:**
> - [[memories_mechanism]] — Full cognitive architecture (observer, distiller, context buffer, perception)
> - [[memory_light]] — Implemented v1 (PostgreSQL + pgvector, RRF, free sources, observer)
> - [[project_knowledge_base]] — Neo4j knowledge graph + retrieval messages design
> - [[context_management]] — Industry comparison of context management approaches
> - [[working_memory]] — workspace.md initialization and injection patterns
> - `docs/issues/memory_noise.md` — Production noise analysis from real jobs

---

## Part 1: Context Engineering as a Discipline

### 1.1 Origin and Definition

"Context engineering" gained traction after **Andrej Karpathy** endorsed it (June 2025):

> "Context engineering is the delicate art and science of filling the context window with just the right information for the next step."

The key distinction: **prompt engineering** is what you write *inside* the context window. **Context engineering** is how you decide *what fills* the window — the entire information architecture surrounding the LLM at inference time. Context engineering is the superset.

As one practitioner put it: *"Most agent failures are not model failures anymore — they are context failures."*

### 1.2 The Four Core Strategies (LangChain Taxonomy)

The most cited taxonomy (LangChain, July 2025):

| Strategy | What It Does | Our Equivalents |
|----------|-------------|-----------------|
| **Write** | Store information outside the window for later | workspace.md, plan.md, archive/, knowledge base |
| **Select** | Retrieve relevant information into the window | Memory injection, instruction files, tool docs |
| **Compress** | Reduce tokens while retaining signal | Observation masking, compaction, summarization |
| **Isolate** | Split context across specialized components | Sub-agents (claude_code tool), phase alternation |

**Assessment:** We have implementations for all four. The gaps are in sophistication — particularly in *Select* (our retrieval is basic RRF; could be agentic) and *Compress* (we don't have reversible compaction or the Manus-style three-tier priority).

### 1.3 Context Failure Modes

A taxonomy of how context goes wrong:

| Failure Mode | Description | Our Exposure |
|---|---|---|
| **Context Rot** | Performance degrades as window fills, even within limits | Mitigated by phase alternation + compaction |
| **Context Poisoning** | Wrong info enters context and compounds through reuse | Possible via memory noise (see `memory_noise.md`) |
| **Context Distraction** | Irrelevant history overwhelms fresh reasoning | Mitigated by observation masking, but memory injection can re-introduce noise |
| **Context Confusion** | Too many tools or conflicting directives | Partially addressed by phase-specific tool filtering |
| **Context Clash** | Contradictory information creates decision paralysis | Not addressed — knowledge base needs contradiction detection |

---

## Part 2: Production Memory Architectures

### 2.1 Landscape Overview

| System | Storage | Retrieval | Write Strategy | Key Innovation |
|--------|---------|-----------|----------------|----------------|
| **MemGPT/Letta** | Core (in-context) + Recall + Archival | Hierarchical | Agent self-manages via tool calls | OS metaphor: LLM as CPU, context as RAM |
| **Mem0** | Vector + KV + Graph | Hybrid (vector + graph) | LLM extraction + AutoDedup | 91% lower latency than OpenAI memory |
| **Zep/Graphiti** | Neo4j (bi-temporal) | BM25 + vector + graph traversal | Incremental episode ingestion | Zero LLM calls during retrieval |
| **EverMemOS** | MongoDB + Elasticsearch + Milvus | 5 strategies (incl. agentic) | Boundary detection + parallel extraction | Foresight signals with time validity |
| **A-Mem** | Structured notes + embeddings + links | Embedding similarity + graph | Zettelkasten atomic notes | New memories trigger updates to linked memories |
| **MAGMA** | 4 orthogonal graphs | Intent-aware traversal | Dual-stream (fast ingest + async consolidation) | Semantic/temporal/causal/entity decomposition |
| **Our system** | pgvector + (planned) Neo4j | RRF hybrid (dense + sparse + recency) | Free sources + observer LLM | Retrieval messages (write-time query generation) |

### 2.2 What Commercial Products Do

| Product | Memory Approach |
|---------|----------------|
| **ChatGPT** | 4-layer: user profile + conversation history + extracted knowledge + active context. Background extraction + explicit "Remember that..." |
| **Claude Code** | CLAUDE.md (user-written, hierarchical) + auto memory (`~/.claude/projects/.../memory/`). File-based, 200-line index limit |
| **Claude API** | Memory tool (`memory_20250818`): agent manages `/memories` directory with file operations. Combined with server-side context editing for infinite workflows |
| **Cursor** | `.cursorrules` + community "memory bank" patterns. No native memory — relies on file-based context |
| **Windsurf** | Multi-layer: working memory (`activeContext.md`) + task logs + persistent core files + Cascade Memory + codebase semantic index |

**Key insight:** The most successful approaches are file-based and transparent. Claude Code's CLAUDE.md, Letta's filesystem benchmark (74% accuracy, beating Mem0's 68.5%), and Manus's "filesystem as unlimited memory" all point to the same conclusion: **simple, human-readable markdown files that the agent can read/write with familiar tools outperform sophisticated specialized memory systems.**

This validates our workspace.md + plan.md + archive/ approach as fundamentally sound.

### 2.3 Novel Patterns Not in Our Existing Docs

#### Sleep-Time Compute (Google Research)
Shift heavy memory processing to idle periods. During "sleep," a model iteratively calls `rethink_memory(new_memory, target_block, source_block)` to consolidate and compress memories. During "wake," the condensed summary prepends the user's query.

**Results:** 5x token reduction at matched accuracy, +13-18pp accuracy gains.

**Applicability:** Could run between jobs or during agent idle time. A curator that reorganizes the knowledge base while no job is active — essentially our planned curator subjob, but framed as "sleep-time compute."

#### Letta V1: Away from Heartbeat Loops
Letta moved away from the original MemGPT pattern where every action was a tool call. The new architecture uses native reasoning tokens and direct assistant messages, staying "in-distribution" relative to how models are trained. Tool calling is no longer mandatory for every step.

**Applicability:** Validates our approach — we use tool calls for memory operations but don't force every agent action through a memory tool.

#### mem-agent: RL-Trained Memory Management
A 4B parameter model trained with reinforcement learning to decide when to add, update, delete, or retain memories. Uses Python code blocks (not JSON function calls) and Obsidian-style markdown files. Despite being 58x smaller than Qwen3-235B, ranks #2 on md-memory-bench.

**Applicability:** Future direction — instead of heuristic-based importance scoring and dedup thresholds, train a small model to make memory management decisions. This would replace the observer's extraction prompt with a learned policy.

#### AWS AgentCore: Four Memory Types
Distinguishes semantic (facts), episodic (experiences), preference (personalization), and summarization (compression) memory. Includes intelligent consolidation that merges related information and resolves conflicts automatically.

**Applicability:** Our `memory_type` field (factual, procedural, error_solution, vocabulary, relational) partially maps to this. The "consolidation" concept — automatically merging related memories — is something we should add beyond simple cosine dedup.

#### Letta Context Repositories (Feb 2026) — Git-as-Memory
The most architecturally novel development of early 2026. Key concepts:

- **MemFS** (Memory FileSystem): Agent memory organized as a git-backed repository of ~15-25 focused markdown files
- **Git versioning**: Every memory change is versioned with informative commit messages, enabling rollback and changelogs
- **Progressive disclosure**: Agents manage their own context loading by reorganizing file hierarchies, updating frontmatter descriptions, and moving files in/out of `system/` to control what is pinned to context
- **Worktree-based multi-agent concurrency**: Multiple subagents process memory in isolated git worktrees, then merge changes through standard git conflict resolution
- **Three subsystems**: Memory Initialization (bootstraps via concurrent subagent exploration), Memory Reflection (background "sleep-time" review), Memory Defragmentation (periodic reorganization)

**Applicability:** This is strikingly similar to our workspace.md + plan.md + archive/ pattern, but with git versioning as a first-class feature (which we already have via `workspace.git_versioning: true`). The **Memory Defragmentation** concept — periodic reorganization of memory files — is new and valuable. We could run a defrag pass at phase boundaries, consolidating fragmented knowledge across workspace.md, archived todos, and plan.md.

Source: [Letta Context Repositories](https://www.letta.com/blog/context-repositories)

#### Microsoft PlugMem (2026) — Task-Agnostic Memory Module
Converts raw agent interaction history into structured, compact knowledge units. Works unchanged across heterogeneous benchmarks, consistently outperforming both task-agnostic baselines and task-specific memory designs. Key finding: agents achieve better results while using **significantly fewer memory tokens**.

**Applicability:** Validates the principle of structuring memories into compact knowledge units rather than storing raw conversation. Our observer already does this; PlugMem suggests making the structuring more aggressive.

Source: [Microsoft PlugMem](https://www.microsoft.com/en-us/research/blog/from-raw-interaction-to-reusable-knowledge-rethinking-memory-for-ai-agents/)

#### LangChain Deep Agents: Three-Tier Context Compression (Jan 2026)
LangChain's Deep Agents SDK implements:
1. **Tool response offloading**: Responses exceeding 20,000 tokens → filesystem, replaced with file path + 10-line preview
2. **Tool call truncation**: At 85% context window → older tool calls truncated with disk pointers
3. **Summarization fallback**: LLM generates structured summary (session intent, artifacts, next steps); full messages persisted to filesystem

**Applicability:** This is essentially the three-tier compression priority (raw > compaction > summarization) with concrete thresholds. The "20k token offload" threshold and "85% truncation trigger" are useful reference points for our system.

Source: [LangChain Deep Agents Context Management](https://blog.langchain.com/context-management-for-deepagents/)

#### AgeMem: Progressive RL Training for Memory (Jan 2026)
Three-stage progressive RL training: (1) acquire long-term memory storage, (2) learn short-term context management, (3) coordinate both. Uses Step-wise GRPO to address sparse/discontinuous rewards from memory operations.

**Results:** 4.82-8.57pp improvement over memory-augmented baselines on five long-horizon benchmarks.

**Applicability:** Together with mem-agent and MemRL, this represents a clear trend: **RL is replacing heuristics for memory management decisions.** When we have enough job data, training a small memory policy model could replace our hand-tuned importance thresholds, dedup cosine cutoffs, and observer extraction prompts.

Source: [AgeMem Paper](https://arxiv.org/abs/2601.01885)

#### AGENTS.md Research Finding (Feb 2026) — Cautionary
Despite adoption by 60,000+ repositories, research found that **LLM-generated context files actually degrade performance by 3% and increase inference costs by 20%**, while human-written files offer only marginal 4% gains. Recommendation: omit LLM-generated context files; limit human-written ones to non-inferable details.

**Applicability:** This is a cautionary result for auto-generated context. Our workspace.md is agent-written — this suggests we should be careful about how much auto-generated content we inject and ensure it only contains information that can't be derived from the codebase itself. The memory noise analysis (`memory_noise.md`) already identified this problem: observer-extracted framework mechanics are redundant with system-enforced behavior.

Source: [AGENTS.md Research](https://arxiv.org/html/2602.11988v1)

#### GCC v2: Git Context Controller (March 2026)
Revised paper achieving **80%+ success rate on SWE-Bench Verified** (13% relative improvement). Four git-inspired operations: COMMIT (milestone checkpointing), BRANCH (isolated exploration), MERGE (integration of reasoning paths), CONTEXT (hierarchical retrieval). Three-tiered memory hierarchy: high-level planning, commit-level summaries, fine-grained execution traces.

**Applicability:** The BRANCH operation for isolated exploration is interesting — our phase alternation somewhat approximates this, but GCC's explicit branching for "what if" reasoning paths is more flexible. Available as a Claude Code skill.

Source: [GCC Paper](https://arxiv.org/abs/2508.00031)

---

## Part 3: Retrieval Innovations

### 3.1 The Retrieval Messages Idea — External Validation

Our [[project_knowledge_base]] design includes **retrieval messages**: when writing a knowledge note, generate synthetic queries that describe situations where that note should surface. This idea is independently validated by multiple research streams:

| Name | Source | Description |
|------|--------|-------------|
| **Retrieval Messages** | Our design | Curator generates synthetic queries at write time, stored alongside knowledge notes |
| **HyPE** (Hypothetical Promptable Embeddings) | Research | Generate multiple hypothetical queries per chunk at indexing, not at query time — inverse HyDE |
| **Foresight Signals** | EverMemOS | Predictions about future relevance with time validity intervals |
| **Contextual Retrieval** | Anthropic | LLM generates chunk-specific explanatory context before embedding |
| **Answer Clues** | MemoRAG | Global memory model generates "draft answers" that guide precise retrieval |
| **Sleep-time rethinking** | Google | Pre-compute inference-rich summaries tailored to anticipated queries |

**All of these are variants of the same core insight: enrich at write time to improve recall at read time.** The specific form differs — synthetic queries, contextual descriptions, foresight predictions — but the principle is identical.

Our retrieval messages approach is well-positioned. The main question is implementation cost: generating synthetic queries for every knowledge note requires an LLM call at write time. Anthropic reports ~$1.02/M tokens for contextual retrieval using prompt caching; our curator would have similar economics.

### 3.2 Hybrid Search: Beyond Basic RRF

Our current implementation (Memory Light) uses 3-channel RRF: dense vector + sparse/BM25 + recency. Research suggests several enhancements:

#### Two-Stage Retrieval
1. **Stage 1:** Broad recall via RRF (retrieve ~100 candidates)
2. **Stage 2:** Precision via cross-encoder reranking (select top 5-10)

Cross-encoder rerankers (e.g., Cohere Rerank, BGE-reranker) consider query-document interaction jointly rather than independently, catching relevance patterns that embedding similarity misses. Anthropic reports a 67% reduction in retrieval failure rate when adding reranking.

**Gap in our system:** We do RRF but no reranking. Adding a reranker as a second stage would be a significant quality improvement, especially for the knowledge base where precision matters more than recall.

#### Graph-Enhanced Retrieval
Three approaches from research:

| Approach | How It Works | Best For |
|----------|-------------|----------|
| **HippoRAG** | PersonalizedPageRank from query entity seeds through knowledge graph | Multi-hop reasoning ("what depends on X?") |
| **EcphoryRAG** | Extract cue entities from query, traverse co-occurrence graph via weighted centroids | Associative recall from partial cues |
| **Graphiti** | Bi-temporal graph with zero-LLM retrieval: BM25 + vector + direct graph traversal | Real-time agent memory with temporal awareness |

**Applicability:** When we implement the Neo4j knowledge base, graph traversal should be a retrieval channel alongside vector and keyword search. The HippoRAG pattern of using PageRank to rank graph neighborhoods is particularly promising — it would let us answer "what do I know about everything related to X?" without needing the exact terms.

#### Adaptive Retrieval (Self-RAG / Agentic RAG)
Instead of always retrieving, let the agent decide:

1. **Retrieve token:** Should I consult memory at all? (simple queries → skip)
2. **ISREL token:** Is the recalled memory relevant? (filter noise)
3. **ISSUP token:** Is my reasoning grounded in recalled memories? (verify)

**A-RAG** (Feb 2026) exposes hierarchical retrieval interfaces (keyword search, semantic search, chunk read) directly to the agent, letting it autonomously decide which to use and when to stop.

**Gap in our system:** Memory injection is always-on — every turn gets memories injected regardless of need. An adaptive system would save tokens and reduce noise. This connects directly to the memory noise problem documented in `memory_noise.md`.

### 3.3 Write-Time vs Read-Time Enrichment

| Timing | Techniques | Trade-offs |
|--------|-----------|------------|
| **Write-time** | Contextual retrieval, HyPE, retrieval messages, graph extraction, RAPTOR summaries | Higher upfront cost, faster queries, must re-index if strategy changes |
| **Read-time** | HyDE, query decomposition, step-back prompting, Self-RAG, multi-query | Lower storage cost, flexible, higher latency per query |
| **Both** | Contextual embeddings + query reformulation + reranking | Best quality, highest cost |

**Recommendation:** For agent memory, favor write-time enrichment because:
- Memories are read far more often than written (high read:write ratio)
- Write-time cost is amortized across all future recalls
- The observer LLM is already running — enrichment is nearly free if batched

### 3.4 MemoRAG: Global Memory for Retrieval Guidance

MemoRAG uses a lightweight model with KV compression (4-64x) to maintain a "global view" of all memories. When queried, this model generates "answer clues" — draft answers that guide precise retrieval from the full memory store.

**Results:** 40.2 average across 13 datasets vs 29.7-33.3 for traditional RAG.

**Applicability:** This pattern would address a key limitation of our RRF-based retrieval: it can only find memories similar to the current query. A global memory model could generate clues like "the answer involves the authentication system and the JWT configuration" even when the query doesn't mention either — because the global model has seen everything and can make associative leaps.

**Cost consideration:** Requires maintaining a compressed representation of all memories. Could use the planned workspace.md + plan.md as a lightweight approximation — they already serve as a "global view" of the job's state.

---

## Part 4: Context Management Patterns

### 4.1 Manus AI: Six Production Principles

Manus (millions of real users) provides the most concrete engineering advice:

#### KV-Cache Hit Rate as Primary Metric
The "single most important metric for production AI agents." Observations:
- 100:1 input-to-output token ratio is typical
- Claude Sonnet: $0.30/M cached tokens vs $3/M uncached — 10x cost difference
- **Design for stable prefixes:** timestamps in system prompts destroy cache
- **Append-only context:** never mutate earlier parts of the prompt
- **Deterministic serialization:** consistent JSON key ordering

**Gap in our system:** We rebuild the system prompt fresh each turn with current phase info, tool definitions, etc. If the prompt prefix changes every turn, we get zero cache hits. Worth investigating whether we can stabilize the prefix.

#### Logit Masking Instead of Tool Removal
Instead of dynamically adding/removing tools (which breaks KV cache), keep all tools in the prompt but use **state-machine-based logit masking** during decoding to constrain available actions.

**Our system:** We already do phase-specific tool filtering (`filter_tools_by_phase()`), which removes tools from the prompt. If KV-cache optimization becomes important, we'd switch to keeping all tools present but masking at the inference layer. This requires control over the inference server (works with vLLM, not with API providers).

#### Attention Manipulation Through Recitation
Create and update `todo.md` files to "recite objectives into the end of the context," keeping task goals within the model's recent attention span.

**Our system:** We already do this — workspace.md is injected as a transient message near the end of context, and todos are always present. This is a validation of our approach.

#### Preserve Evidence of Failure
Keep failed actions and stack traces visible so models can "implicitly update internal beliefs."

**Our system:** We do the opposite in some cases — memory noise analysis showed that raw tool errors stored as memories were the top noise source (accessed 600-700+ times each). The fix was to reduce/remove the tool_error memory channel. The Manus principle applies to *conversation context*, not to *stored memories*. Errors should be visible in recent conversation but not persisted as high-importance memories.

### 4.2 Three-Tier Compression Priority

From Manus and Phil Schmid:

```
1. Raw (keep as-is)           → most recent tool calls, current reasoning
2. Compaction (reversible)    → replace old results with file paths/URLs the agent can re-fetch
3. Summarization (lossy)      → only when compaction doesn't free enough space
```

**Gap in our system:** We jump from raw to summarization (Layer 1/2 compaction). We don't have a reversible compaction step where old tool results are replaced with references. Adding this intermediate step could significantly reduce how often we need expensive LLM summarization.

**Implementation idea:** When masking old tool results, instead of a generic placeholder, store `"[Result available: re-read file X or re-run command Y]"` — giving the agent an actionable path to recover the information if needed.

### 4.3 Sub-Agent Architecture for Context Isolation

Both Anthropic and Phil Schmid emphasize sub-agents as a context isolation strategy:

- **Anthropic:** Specialized agents handle focused tasks with clean windows, returning condensed summaries (1,000-2,000 tokens)
- **Phil Schmid MapReduce:** Convert "Deep Research" or "Plan Task" to tool calls. The harness spins up a temporary sub-agent and returns structured JSON

**Our system:** We have the `claude_code` tool for delegating heavy work, but we don't use sub-agents for context isolation within the main agent. The phase alternation model serves a similar purpose — each phase transition is a natural "context reset point" where old conversation gets compressed.

### 4.4 Tool Management

Phil Schmid's three-level tool hierarchy:

| Level | Tools | Example |
|-------|-------|---------|
| **Level 1** (Atomic) | ~20 stable core tools | file_write, bash, search |
| **Level 2** (Sandbox) | General bash for CLI | Any CLI command |
| **Level 3** (Code) | Libraries; agent writes scripts | Dynamic tool creation |

**Our system:** We have ~40-50 tools across categories. Phil's recommendation is to keep it under 20. Our phase-specific filtering helps (agents don't see all tools at once), but we should evaluate whether tool count is impacting reasoning quality.

---

## Part 5: Cognitive-Inspired Patterns

### 5.1 Human Memory Analogy

The original question that prompted this document: *"A human sees something and remembers stuff from context."*

This maps to the **ecphory** concept from cognitive science — a partial cue triggers reconstruction of a full memory. Several research systems implement this:

- **EcphoryRAG:** Extract "cue entities" from current context, use them as activation signals to traverse an entity co-occurrence graph. The cue doesn't need to match the memory exactly — it just needs to be related.
- **HippoRAG:** Models the hippocampus-neocortex system. LLM processes perceptions into knowledge (neocortex), knowledge graph serves as hippocampal index, PersonalizedPageRank traverses from query seeds.
- **Associative activation:** When you think about "authentication," related concepts (JWT, session tokens, middleware, login flow) activate automatically. This is graph traversal, not vector similarity.

**The gap:** Our current retrieval (RRF over vectors + keywords + recency) is essentially "search engine memory" — it finds things similar to the query. Human memory is more **associative** — concepts activate related concepts through learned connections. The knowledge graph + graph traversal is the missing piece that would make recall more human-like.

### 5.2 Multi-Graph Decomposition (MAGMA)

Already documented in [[memories_mechanism]], but worth emphasizing the practical implications:

Different queries need different traversal strategies:
- "What do I know about authentication?" → **Semantic** graph (topic similarity)
- "What happened before the deployment failed?" → **Temporal** graph (sequence/causality)
- "What caused the timeout?" → **Causal** graph (cause-effect chains)
- "What depends on the users table?" → **Entity** graph (relationships)

**Implementation consideration:** Full MAGMA (4 separate graphs) is expensive. A pragmatic approach: use Neo4j with typed relationships that encode the graph type. A single graph with relationship types like `CAUSED_BY`, `HAPPENED_BEFORE`, `RELATES_TO`, `DEPENDS_ON` can approximate multi-graph decomposition without the infrastructure complexity.

### 5.3 Memory Consolidation and Evolution

Multiple systems implement memory evolution beyond simple storage:

| Pattern | System | Description |
|---------|--------|-------------|
| **AutoDedup** | Mem0 | LLM decides ADD/UPDATE/DELETE/SKIP for each new memory |
| **Link Evolution** | A-Mem | New memories trigger context updates on linked existing memories |
| **Dual-stream** | MAGMA | Fast ingest + async structural consolidation |
| **Sleep-time** | Google/Letta | Background reorganization during idle |
| **Foresight decay** | EverMemOS | Foresight signals have time validity intervals — they expire |

**Our status:** We have threshold-based cosine dedup (0.92). The next steps, in order of impact:
1. **Post-retrieval clustering** — deduplicate at read time by grouping similar retrieved memories and picking the best representative (addresses `memory_noise.md` Issue 5)
2. **Memory consolidation** — periodically merge related memories into higher-level insights (e.g., 5 memories about "GPU 0 is locked" → 1 definitive memory)
3. **AutoDedup** — LLM-mediated decisions for ambiguous cases (Mem0 pattern)
4. **Link evolution** — when a new memory arrives, update related existing memories' context (A-Mem pattern)

---

## Part 6: Synthesis — What to Build Next

### 6.1 Already Have (Validated by Research)

These patterns are already implemented or designed and confirmed as good approaches by external research:

- **Workspace.md as persistent memory** — validated by Claude Code CLAUDE.md, Manus "filesystem as memory," GCC, Letta filesystem benchmark
- **Transient injection** — validated by Claude Code, Manus "recitation" pattern
- **Observation masking** — validated by JetBrains, Cursor, Warp, OpenHands
- **Phase alternation as context reset** — validated by Context-Folding, RAPTOR hierarchical summarization
- **RRF hybrid search** — validated as minimum viable approach by all production systems
- **Free memory sources** (todo completion, compaction, etc.) — novel to our system, good approach
- **Retrieval messages** — independently validated by HyPE, EverMemOS foresight, Anthropic contextual retrieval

### 6.2 High-Impact Gaps (Ordered by Expected Value)

#### Gap 1: Adaptive Retrieval — Don't Always Inject
**Problem:** Memory is injected every turn regardless of need, contributing to noise.
**Solution:** Before injection, evaluate whether the current turn needs memory. Options:
- Heuristic: skip injection when the current todo is a simple tool call (file read, command execution)
- Learned: use a lightweight classifier or the Self-RAG pattern
- Agent-driven: give the agent a `recall` tool instead of auto-injecting

**Effort:** Medium. **Impact:** High — reduces noise, saves tokens.

#### Gap 2: Reversible Compaction Layer
**Problem:** We jump from raw tool results to LLM summarization with nothing in between.
**Solution:** Add a "compaction" step that replaces old tool results with re-fetchable references: `"[File content: workspace/job_X/output/report.md — re-read if needed]"`
**Effort:** Low-Medium. **Impact:** Medium — reduces summarization frequency and cost.

#### Gap 3: Reranking Stage
**Problem:** RRF alone sometimes surfaces irrelevant memories that keyword-match well.
**Solution:** Add a cross-encoder reranker as Stage 2 after RRF retrieval. Could use a local model (BGE-reranker) or API (Cohere Rerank).
**Effort:** Low. **Impact:** Medium — addresses retrieval precision issues from `memory_noise.md`.

#### Gap 4: Knowledge Graph Retrieval (Graph Traversal Channel)
**Problem:** RRF misses multi-hop and associative relationships.
**Solution:** When the Neo4j knowledge base is built, add graph traversal as a fourth RRF channel. Extract entities from current context, traverse the graph for related notes, and merge results into the RRF fusion.
**Effort:** High (depends on knowledge base implementation). **Impact:** High — enables "human-like" associative recall.

#### Gap 5: Memory Consolidation
**Problem:** Redundant memories accumulate (5 variants of "GPU 0 is locked").
**Solution:** Periodic background job that clusters semantically similar memories and merges them. Could run at phase boundaries or as a "sleep-time" process between jobs.
**Effort:** Medium. **Impact:** Medium — reduces storage noise and improves retrieval signal-to-noise.

#### Gap 6: KV-Cache Optimization
**Problem:** System prompt changes every turn, destroying cache.
**Solution:** Stabilize the prompt prefix (move dynamic content to the end), use append-only context structure, ensure deterministic tool ordering.
**Effort:** Medium. **Impact:** High for cost at scale (10x token cost difference), but requires API provider support for prompt caching.

### 6.3 Interesting But Lower Priority

| Idea | Source | Why Lower Priority |
|------|--------|--------------------|
| RL-trained memory management | mem-agent, Memory-R1 | Requires training infrastructure; heuristics work for now |
| Multi-graph decomposition | MAGMA | Single typed-relationship graph is sufficient initially |
| Global memory model | MemoRAG | workspace.md serves as a lightweight approximation |
| Sleep-time consolidation | Google/Letta | Requires idle-time infrastructure; phase boundaries serve similar purpose |
| Foresight time validity | EverMemOS | Adds complexity; recency decay in RRF approximates this |
| Logit masking for tools | Manus | Only works with self-hosted inference; API providers don't expose this |

---

## Part 7: The Bigger Picture — Context Buffer Evolution

The [[memories_mechanism]] doc describes a 4-stage evolution path. Research confirms this trajectory is sound:

```
Stage 1: Conversation + Memory (current — Memory Light)
    ↓
Stage 2: Conversation + Distiller + Memory (next — add continuous compression)
    ↓
Stage 3: Hybrid Buffer (recent conversation + structured memory blocks)
    ↓
Stage 4: Full Context Buffer (no conversation, just assembled state per turn)
```

**Evidence from research:**
- **Stage 1-2:** This is where most production systems are (ChatGPT, Claude Code, Cursor)
- **Stage 3:** Manus operates here — "filesystem as extended context" with structured state
- **Stage 4:** MemoRAG's global memory model + focused retrieval is closest to this vision. No production system has fully achieved it, but the research (Context-Folding, FoldAgent) shows it's viable at 32K tokens

The key question for each stage transition: **does the benefit of structure outweigh the loss of conversational flow?** Models are trained on conversation, so non-conversational input may degrade output quality. The safest path is gradual: keep recent conversation conversational, replace only older segments with structured memory.

### 7.2 Key Trends (Early 2026)

1. **Git-as-memory** is a major pattern: Letta Context Repositories, GCC, filesystem approaches all treat memory as version-controlled files rather than opaque vector stores

2. **RL is replacing heuristics** for memory management: AgeMem, MemRL, mem-agent, and FoldPO all use reinforcement learning to teach agents when to store, retrieve, summarize, and delete — rather than hand-tuned thresholds

3. **Multi-graph architectures outperform flat stores**: MAGMA's four orthogonal graphs and Zep's temporal knowledge graphs show structured memory with temporal/causal awareness dramatically improves reasoning

4. **Simple beats complex for compression**: JetBrains observation masking often outperforms LLM summarization. Letta's filesystem benchmark (74%) beats Mem0's graph-based approach (68.5%). The theme "less is more" recurs across sources

5. **Context windows are plateauing** — the focus has shifted from bigger windows to better management of existing ones

6. **Auto-generated context can hurt**: AGENTS.md research shows LLM-generated context files degrade performance by 3%. Only inject what the agent can't derive from the codebase itself

### 7.3 LongMemEval Benchmark Landscape (Early 2026)

| System | Score | Key Approach |
|--------|-------|--------------|
| **Supermemory** | ~85.86% | #1 across multiple benchmarks |
| **EverMemOS** | 83.0% | Foresight signals + triple-database |
| **Oracle GPT-4o** | 82.4% | Baseline (full context, no memory system) |
| **TiMem** | 76.88% | 27% lower memory footprint |
| **Letta filesystem** | 74.0% | Simple grep/search_files (no ML retrieval) |
| **Mem0 graph** | 68.5% | Vector + graph hybrid |

The Oracle GPT-4o baseline (82.4%) is worth noting — it suggests that a well-managed context window without any memory system can be competitive. Memory systems need to beat this bar to justify their complexity.

---

## References (New — Not in Existing Docs)

### Context Engineering
- [Karpathy on Context Engineering](https://x.com/karpathy/status/1937902205765607626) (June 2025) — origin of the term
- [LangChain: Context Engineering for Agents](https://blog.langchain.com/context-engineering-for-agents/) (July 2025) — Write/Select/Compress/Isolate taxonomy
- [Martin Fowler: Context Engineering for Coding Agents](https://martinfowler.com/articles/exploring-gen-ai/context-engineering-coding-agents.html) — decision framework
- [arXiv: A Survey of Context Engineering for LLMs](https://arxiv.org/abs/2507.13334) (July 2025, 166 pages) — academic survey
- [Weaviate: Context Engineering](https://weaviate.io/blog/context-engineering) — six pillars framework

### Memory Systems
- [Letta V1 Architecture](https://www.letta.com/blog/letta-v1-agent) — post-MemGPT rearchitecture
- [Letta Context Repositories](https://www.letta.com/blog/context-repositories) (Feb 2026) — git-backed MemFS, worktree concurrency
- [Letta Benchmarking AI Agent Memory](https://www.letta.com/blog/benchmarking-ai-agent-memory) — filesystem beats graph (74% vs 68.5%)
- [Serokell: Design Patterns for Long-Term Memory](https://serokell.io/blog/design-patterns-for-long-term-memory-in-llm-powered-architectures) — comparison matrix
- [Claude API Memory Tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) (`memory_20250818`) — `/memories` directory pattern
- [AWS AgentCore Memory](https://aws.amazon.com/blogs/machine-learning/building-smarter-ai-agents-agentcore-long-term-memory-deep-dive/) — 4 memory types + consolidation
- [Microsoft PlugMem](https://www.microsoft.com/en-us/research/blog/from-raw-interaction-to-reusable-knowledge-rethinking-memory-for-ai-agents/) (2026) — task-agnostic plug-and-play memory module
- [Sleep-Time Compute](https://www.prompthub.us/blog/sleep-time-compute) (Google Research) — idle-time memory processing
- [mem-agent](https://huggingface.co/blog/driaforall/mem-agent-blog) — RL-trained 4B memory manager
- [AgeMem](https://arxiv.org/abs/2601.01885) (Jan 2026) — progressive RL training for memory operations
- [MemRL](https://arxiv.org/abs/2601.03192) (Jan 2026) — self-evolving agents via runtime RL on episodic memory
- [Supermemory Research](https://supermemory.ai/research) — #1 on LongMemEval benchmark

### Retrieval Innovations
- [A-RAG: Scaling Agentic RAG](https://arxiv.org/abs/2602.03442) (Feb 2026) — hierarchical retrieval interfaces
- [Anthropic Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval) — write-time context enrichment
- [HippoRAG](https://arxiv.org/abs/2405.14831) — PersonalizedPageRank on knowledge graphs
- [EcphoryRAG](https://arxiv.org/html/2510.08958v1) — cognitive ecphory-inspired retrieval
- [MemoRAG](https://arxiv.org/abs/2409.05591) — global memory model for retrieval guidance
- [Self-RAG](https://arxiv.org/abs/2310.11511) — adaptive retrieval with self-critique
- [CRAG (Corrective RAG)](https://arxiv.org/abs/2401.15884) — confidence-based retrieval correction
- [RAPTOR](https://arxiv.org/abs/2401.18059) — recursive abstractive processing tree
- [Late Chunking](https://arxiv.org/abs/2409.04701) (Jina AI) — preserve cross-chunk context in embeddings
- [LazyGraphRAG](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/) — defer LLM to query time, NLP-only indexing

### Context Management
- [LangChain Deep Agents Context Management](https://blog.langchain.com/context-management-for-deepagents/) (Jan 2026) — three-tier compression
- [LangChain: Filesystems for Context Engineering](https://blog.langchain.com/how-agents-can-use-filesystems-for-context-engineering/) (Nov 2025) — filesystem as memory interface
- [GCC: Git Context Controller v2](https://arxiv.org/abs/2508.00031) (March 2026) — 80%+ SWE-Bench, git-inspired operations
- [U-Fold: Intent-Aware Context Folding](https://arxiv.org/abs/2601.18285) (Jan 2026) — 71.4% win rate over ReAct
- [AGENTS.md Research](https://arxiv.org/html/2602.11988v1) (Feb 2026) — LLM-generated context files hurt performance

### Production Engineering
- [Manus: Context Engineering Lessons](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) — KV-cache, logit masking, filesystem
- [Phil Schmid: Context Engineering Part 2](https://www.philschmid.de/context-engineering-part-2) — compression priority, MapReduce agents
- [CrewAI Memory](https://docs.crewai.com/en/concepts/memory) — depth-based retrieval with confidence routing

### Benchmarks & Workshops
- [LongMemEval / Supermemory](https://supermemory.ai/research) — memory system benchmark landscape
- [ICLR 2026 MemAgents Workshop](https://sites.google.com/view/memagent-iclr26/) (April 27, 2026) — dedicated workshop on agent memory
- [Mnemosyne MCP Server](https://github.com/rand/mnemosyne) — production MCP memory for Claude Code
