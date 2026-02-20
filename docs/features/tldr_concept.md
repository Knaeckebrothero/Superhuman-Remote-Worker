---
tags:
  - context-management
  - tool-development
  - llm-configuration
  - performance
  - research
---

# TLDR Briefing System - Concept Document

## Problem Statement

Every `read_file` call dumps full content into the main agent's context as a `ToolMessage`. Reading a 2000-line file consumes ~4k tokens. Searching across 10-20 files for a solution burns 40k+ tokens — half the context budget — with most of it being irrelevant noise. Compaction then discards content the agent actually needed.

The expensive reasoning model is doing janitor work (scanning files for relevance) when it should be doing *reasoning*.

### Token Budget Reality (Default Config)

```
Model context:    ~100,000 tokens
System prompt:      8,000-12,000
Workspace.md:       5,000-10,000 (injected fresh each turn)
Todos:              2,000-5,000
Summaries:          3,000-5,000
Instructions:       1,000-2,000
                  ─────────────
Overhead:          19,000-34,000 tokens (fixed cost per turn)
Available:         ~66,000-81,000 tokens (working memory)
```

A single large PDF read (25k words) can consume the majority of available working memory in one tool call. Multiple file reads during investigation easily trigger compaction, which then throws away earlier tool results the agent may still need.

## Core Idea

Offload file reading and information gathering to a cheap, fast model that runs *outside* the main context window. The cheap model reads the raw content, filters for relevance, and returns a compact briefing. The main agent's context only ever sees the summary — not the raw files.

This is the same pattern already used by `VisionHelper` for images: instead of feeding raw image data to an expensive text-only model, a cheap vision model generates a text description. The TLDR system extends this from images to *any content*.

## Implementation Tiers

### Tier 1: Smart Read Tool (Simplest)

A new tool `brief_files` that handles everything in a single call.

```
Agent calls: brief_files(
    query="How does authentication work in this codebase?",
    paths=["src/auth/*.py", "config/auth.yaml"],
    # OR
    glob="**/*auth*",
    max_tokens=2000  # budget for the returned briefing
)

Returns: Structured briefing (~500-2000 tokens) with:
    - Direct answer to the query
    - Key findings with file:line references
    - Suggested deep-reads for follow-up
```

**How it works internally:**
1. Resolve file paths (glob expansion, workspace-relative)
2. Read all matched files server-side (not through the tool system)
3. Build a prompt: query + file contents + "produce a briefing"
4. Call a cheap/fast model (e.g. gpt-4o-mini, haiku)
5. Return the briefing as the tool result

**Token economics:**
- Main agent context: ~500-2000 tokens (just the briefing)
- Cheap model context: 50k-100k tokens (all the raw files) at ~10x lower cost
- Net savings: 30-40k tokens freed in the main context per investigation

**Pros:** Simple, synchronous, fits existing tool patterns, easy to test.
**Cons:** Single-shot — can't follow leads or do multi-hop reasoning.

### Tier 2: Research Agent (Medium Complexity)

A tool `research_brief` that spawns a mini agent loop with a cheap model.

```
Agent calls: research_brief(
    question="Why is the job freezing after phase 3?",
    scope="workspace",  # or "codebase", "documents", specific paths
    depth="thorough"    # or "quick", "exhaustive"
)

Returns: Structured briefing with:
    - Executive summary
    - Evidence chain (file:line references for each finding)
    - Hypotheses ranked by confidence
    - Recommended next steps
```

**How it works internally:**
1. Spawn a lightweight agent (ReAct loop or simple chain) with a cheap model
2. Give it access to: `read_file`, `list_files`, `grep`-equivalent
3. Provide the question + conversation context summary as the system prompt
4. Let it investigate for N steps (configurable, default 5-10)
5. Final step: synthesize findings into a structured briefing
6. Return the briefing to the main agent

**Pros:** Can follow leads, do multi-hop reasoning, much more capable.
**Cons:** Takes longer (multiple LLM calls), more complex implementation, still synchronous (blocks the main agent).

### Tier 3: Async Briefing (Most Ambitious)

The main agent kicks off a background research task and continues working.

```
Agent calls: start_research(
    question="Map all database access patterns in src/",
    callback_label="db_patterns"
)
# Returns immediately: "Research task 'db_patterns' started."

# Agent continues other work...

# Later, results get injected into context when ready
# OR agent explicitly checks:
Agent calls: get_research_result(label="db_patterns")
```

**How it works internally:**
1. Spawn research agent in a background thread/process
2. Main graph execution continues (no blocking)
3. Results stored in a staging area (file on disk, or state field)
4. On next `execute` turn, check for pending results and inject them
5. OR provide an explicit `get_research_result` tool for polling

**Injection strategy options:**
- **A) Explicit polling:** Agent calls `get_research_result` when it wants the results. Simple, agent-controlled.
- **B) Transient injection:** Similar to workspace.md, inject completed briefings as transient messages. Auto-expires after N turns.
- **C) Workspace append:** Write briefing to a `briefings/` directory in the workspace. Agent reads when needed. Simplest, but loses the "automatic injection" benefit.

**Pros:** Non-blocking, allows parallel work, most efficient use of wall-clock time.
**Cons:** Complex orchestration, async injection into LangGraph state is non-trivial, harder to test and debug.

## Existing Infrastructure to Build On

| Component | Location | Relevance |
|-----------|----------|-----------|
| `VisionHelper` | `src/services/vision_helper.py` | Proven pattern: cheap model pre-digests content for expensive model |
| `DescriptionCache` | `src/services/description_cache.py` | Caching pattern for cheap-model outputs |
| `ToolContext` | `src/tools/context.py` | Has LLM config, workspace path, file access — everything the TLDR tool needs |
| Workspace injection | `src/core/workspace_injection.py` | Pattern for injecting transient content into context without permanent storage |
| `PDFReader` | `src/utils/pdf.py` | Already handles large content with pagination and word limits |
| `FileResolver` | `src/core/loader.py` | File lookup with fallback chains |

## Design Decisions

### What triggers it?

**Option A: Explicit tool.** Agent decides when to use `brief_files` vs `read_file`. Gives the agent control over the cost/fidelity trade-off. Agent can still do a raw `read_file` when it needs exact content.

**Option B: Smart mode on read_file.** Add a `tldr` parameter to `read_file` that triggers summarization. Fewer tools to learn, but muddies the tool's responsibility.

**Recommendation:** Option A. Keep `read_file` for raw access, add `brief_files` as a separate tool. Clear separation of concerns. The agent learns when to use each through its tool documentation.

### How much context does the cheap model get?

The cheap model needs enough context to filter for relevance:

1. **Query/question** (always) — what the main agent is looking for
2. **File contents** (always) — the raw material to summarize
3. **Situation summary** (optional) — condensed version of what the main agent has been doing

The situation summary is valuable but adds complexity. Start without it (Tier 1), add it in Tier 2.

### Caching

Briefings should be cached by (query_hash + file_paths_hash + file_mtimes). If the files haven't changed and the query is identical, return the cached briefing. This prevents repeated TLDR calls during retries or phase transitions.

Store cache in `workspace/job_<id>/briefings/` for debuggability.

### Source references

Every briefing must include file:line references so the main agent can do targeted `read_file` follow-ups on specific sections. The briefing acts as a table of contents, not a replacement for the source material.

Format:
```
## Finding: Authentication uses JWT tokens
- `src/auth/jwt.py:45-60` — Token generation with RS256
- `src/auth/middleware.py:12-30` — Request validation middleware
- `config/auth.yaml:1-15` — Token expiry and refresh settings

**To verify:** read_file("src/auth/jwt.py", offset=45, limit=15)
```

### Model selection

The cheap model should be configurable per agent config:

```yaml
tldr:
  model: gpt-4o-mini        # or haiku, or any cheap fast model
  max_input_tokens: 100000   # budget for file content sent to cheap model
  max_output_tokens: 2000    # budget for the briefing
  cache_ttl: 3600            # seconds to cache briefings
```

Falls back to `VISION_MODEL` / `VISION_API_KEY` environment variables (same infra already used by `VisionHelper`).

## Suggested Implementation Path

1. **Start with Tier 1** (`brief_files` tool) — validates the concept, low risk, high immediate value
2. **Add caching** — prevents redundant TLDR calls
3. **Evolve to Tier 2** (`research_brief`) if single-shot proves too limiting — adds multi-hop capability
4. **Tier 3 only if** async parallelism proves necessary — highest complexity, may not be needed if Tier 2 is fast enough

## Success Metrics

- **Context savings:** Average tokens consumed per investigation cycle (before vs after)
- **Investigation quality:** Does the agent find the right information with fewer tool calls?
- **Wall-clock time:** Is the TLDR overhead (cheap model call) worth the context savings?
- **Compaction frequency:** How often does the agent hit compaction thresholds?

## Related

- [[context_management]]
- [[summary_tool]]
- [[citation_engine_roadmap]]
- [[advanced_job_configuration]]
- [[tool_issues]]
- [[obsidian]]
