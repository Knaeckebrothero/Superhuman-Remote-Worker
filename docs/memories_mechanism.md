# Associative Memory Mechanism

## Concept

A persistent memory system that works like human associative recall. The agent never explicitly decides to remember or retrieve — it simply works on its task. A separate **observer system** watches the conversation, extracts noteworthy information, and stores it. On the retrieval side, relevant memories are injected into the agent's context based on what it's currently doing.

The agent is completely unaware it's being observed or that memories are being surfaced. It just sees them appear, like how `workspace.md` already works.

## Architecture

### Two Independent Systems

```
┌─────────────────────────────────────┐
│           Agent (unchanged)         │
│                                     │
│  execute → tools → check_todos → …  │
│         ▲                           │
│         │ inject relevant memories  │
└─────────┼───────────────────────────┘
          │
    ┌─────┴─────┐
    │  Memory    │
    │  Store     │
    │ (vectors)  │
    └─────┬─────┘
          │
┌─────────┼───────────────────────────┐
│         │ store new memories        │
│         ▼                           │
│     Observer System                 │
│  (separate model, runs async)       │
│                                     │
│  Periodically scans conversation    │
│  Extracts noteworthy information    │
│  Writes memories to store           │
└─────────────────────────────────────┘
```

### Observer (Storage Side)

A separate, lightweight process that periodically reads the agent's conversation and extracts memories worth keeping.

**Characteristics:**
- Runs asynchronously, does not block or slow the agent
- Can use a smaller/cheaper model (not the same LLM doing the work)
- Scans conversation every N turns or on phase boundaries
- Agent has zero awareness of being observed — no tool overhead, no "should I store this?" deliberation

**What to extract:**
- Mistakes made and corrections applied
- Domain facts discovered during research
- Tool usage patterns that worked or failed
- User preferences and conventions
- Schema/data insights from databases
- Recurring problems and their solutions

**Storage format:**
Each memory is a small paragraph with:
- Content (the actual insight, 1-3 sentences)
- Embedding (vector for semantic search)
- Source metadata (job ID, phase, timestamp)
- Topic tags (for keyword-based fallback matching)

**Key insight on embedding:** The embedding should capture *when this memory would be useful again*, not just what happened. A memory like "Neo4j MERGE is faster than CREATE+MATCH for this schema" should surface when the agent is about to write Cypher, not just when someone mentions Neo4j.

### Retrieval (Injection Side)

Relevant memories are surfaced into the agent's context based on its current activity.

**Trigger sources:**
- Current todo description and phase context
- Recent tool calls and their results
- Current workspace.md and plan.md content
- Activity patterns (e.g., starting a phase, writing to a database, doing research)

**Matching strategies:**
1. **Semantic similarity** — Embed current context, query memory store for nearest neighbors
2. **Keyword/topic matching** — Fast fallback using topic tags
3. **Activity-pattern triggers** — Certain actions (e.g., writing Cypher, starting a strategic phase) trigger recall from relevant categories

**Injection mechanism:**
- Similar to existing `workspace.md` transient injection (`src/core/workspace_injection.py`)
- A small "Relevant memories" block injected alongside workspace.md
- Only appears when match confidence exceeds a threshold
- Carries 2-3 most relevant memories per turn, stays silent otherwise

**Async retrieval:**
- Runs in the background during the `execute` node
- While the LLM generates its current response, a parallel process embeds the context and fetches memories for the *next* turn

## Design Principles

1. **Separation of concerns** — The agent works. The observer watches. Neither knows about the other's internals.
2. **No agent modification** — Existing agent code stays untouched on the execution side. Memory is infrastructure, not a feature the agent learns to use.
3. **Cost efficiency** — Observer uses a cheap/fast model. Retrieval is a vector query. The expensive model only sees the final injected memories.
4. **Signal over noise** — Better to surface nothing than to surface irrelevant memories. High retrieval threshold by default.
5. **Passive by design** — Like human memory: you don't decide to remember, it just happens. You don't decide to recall, it just surfaces.

## Open Questions

- **Storage backend:** pgvector in existing PostgreSQL? Dedicated vector store? SQLite with embeddings for simplicity?
- **Observer frequency:** Every N turns? On phase transitions? On tool errors? Adaptive?
- **Memory lifecycle:** Do memories decay over time? Can they be consolidated/merged? Is there a cap?
- **Cross-job memory:** Should memories from one job be available to other jobs? Scoped by agent config/expert type?
- **Feedback loop:** If a surfaced memory leads to better outcomes, should it be reinforced? If ignored repeatedly, should it decay?
