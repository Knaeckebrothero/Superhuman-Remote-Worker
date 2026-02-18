# Context Management Evolution: Summarization Tool & Async Context Assembly

> Feature proposal for agent-controlled context compaction, tiered summarization, and async phase preparation.

## Current State: What Works and What's Limited

The existing three-layer context safety system is solid but **reactive** — it only compacts when the agent is already in trouble (hitting token limits). The summarization is a **blunt instrument**: one prompt, one pass, same treatment regardless of what kind of work just happened.

### Architecture Today

```
Layer 0 (HTTP-level)  → ContextOverflowError on actual request
Layer 1 (Pre-request) → Proactive compaction before LLM call
Layer 2 (Emergency)   → Recovery compaction on overflow, single retry
```

Compaction hierarchy (least to most destructive):
1. **Tool result clearing** — replace old tool results with placeholders
2. **Message trimming** — keep recent messages, discard old
3. **Summarization** — LLM-based compression via `ConversationSummary` model

### Key Limitations

| Limitation | Impact |
|-----------|--------|
| **No agent control** | The AI can't decide *when* or *how* to compact. Triggered automatically by thresholds only. |
| **Context loss at transitions** | Strategic-to-tactical compaction has no domain awareness. A research-heavy tactical phase loses nuance from strategic analysis. |
| **Synchronous bottleneck** | Summarization blocks the execute loop. Recursive chunked summarization can take 30-60 seconds. |
| **One-size-fits-all summary** | The structured `ConversationSummary` model is identical whether the agent just finished deep research or mechanical file editing. |

### Relevant Source Files

| File | Role |
|------|------|
| `src/core/context.py` | ContextManager, compaction logic, token counting, summarization pipeline |
| `src/core/workspace_injection.py` | Transient injection of workspace.md, todos, instructions |
| `src/core/phase.py` | Phase transition logic, archive triggers |
| `src/graph.py` (execute node) | Message preparation, safety checks, LLM invocation |
| `config/prompts/summarization_prompt.txt` | Single summarization prompt template |

---

## Proposal: Three Features That Build on Each Other

### Feature 1: Summarization Tool (Agent-Initiated Compaction)

Give the agent a `compact_context` tool it can invoke during execution:

```python
compact_context(
    focus: str = "Preserve research findings about X",
    urgency: str = "normal",  # or "aggressive"
    preserve_patterns: list[str] = ["error messages", "file paths"]
)
```

**Why this matters:** The agent *knows* what's important right now. When it's about to pivot from research to writing, it can say "compress the research phase but keep all citations and findings." The current system can't do that — it treats everything uniformly.

The `focus` parameter becomes additional instruction injected into the summarization prompt. The agent essentially annotates its own memory before compression.

**Behavior:**
- `focus` — free-text instruction appended to the summarization prompt, guiding what to preserve
- `urgency: "normal"` — standard compaction (tool result clearing + summarization if needed)
- `urgency: "aggressive"` — force full summarization regardless of current token count
- `preserve_patterns` — string patterns to match in tool results; matched results are kept verbatim instead of replaced with placeholders

**Integration points:**
- Register in `TOOL_REGISTRY` with `phases: ["strategic", "tactical"]`
- Implementation calls `ContextManager.ensure_within_limits()` with modified prompt
- Returns summary stats to the agent (tokens before/after, messages compacted)

**Design questions:**
- Should the tool block execution until compaction completes, or return immediately and compact in the background?
- Should the agent see the generated summary so it can verify critical info was preserved?
- Rate limiting — prevent the agent from calling this every turn (minimum interval or token delta)?

---

### Feature 2: Tiered Summarization Workflow

Replace the single `ConversationSummary` Pydantic model with a multi-stage pipeline that adapts to the type of work being summarized.

#### Stage 1: Segment Classification

Classify conversation segments by activity type:

```
"This chunk is: debugging | research | file_editing | planning | citation_work"
```

Could be heuristic (tool call patterns) or LLM-classified:
- Lots of `web_search` / `cite_web` calls → research
- Lots of `read_file` / `write_file` calls → file editing
- Error messages + retries → debugging
- `next_phase_todos` / `job_complete` → planning

#### Stage 2: Domain-Specific Summarization

Each activity type gets its own summarization strategy:

| Activity | Preserve | Compress |
|----------|----------|----------|
| **Debugging** | Error traces, root causes, solutions, affected files | Failed attempts, stack traces after resolution |
| **Research** | Findings, source URLs, key quotes, citation IDs | Search queries that returned nothing, intermediate reads |
| **File editing** | File paths, what changed, why, final state | Intermediate drafts, read-before-write content |
| **Planning** | Decisions, rejected alternatives, rationale | Discussion leading to obvious conclusions |
| **Citation work** | Source metadata, annotations, tags, library state | Routine `list_sources` outputs |

#### Stage 3: Consolidation

Merge domain-specific summaries into a coherent context document with priority weighting:
- Recent > old
- Decisions > routine actions
- Errors/blockers > successful operations
- Quantitative data (IDs, paths, counts) > qualitative descriptions

**Implementation approach:**
- New Pydantic models per domain (or a union model with optional domain sections)
- `_single_pass_summarize()` becomes a dispatcher that routes segments to domain handlers
- Consolidation pass merges domain outputs into the final `SystemMessage`

---

### Feature 3: Async Context Assembly Pipeline

Instead of "compact the past," flip the paradigm: **build the future context proactively.** When a strategic phase finishes planning the next tactical phase, an async pipeline assembles what that tactical phase will need.

#### Flow

```
Strategic phase completes
  |
  v (async, while phase transition logic runs)
  +-- Summarize relevant history       --> phase_context.md
  +-- Extract key decisions             --> decisions.md
  +-- Pre-render relevant file summaries
  +-- Build "context package" for next phase
       |
       v
  Tactical phase starts with rich, curated context
  instead of raw compacted history
```

#### Context Package Structure

```
workspace/job_<id>/context/
  phase_<n>_context.md      # Curated summary for this phase
  phase_<n>_decisions.md    # Key decisions still in effect
  phase_<n>_file_state.md   # Relevant file summaries
```

This replaces the current approach where the tactical phase inherits the raw (compacted) conversation history. Instead, it starts with a purpose-built context package.

#### Why Async Matters

- **Doesn't block** — phase transition logic runs in parallel with context assembly
- **Forward-looking** — assembles what's *needed*, not just what *happened*
- **Selective** — different phase types need different context packages:
  - Research phase needs citations, source lists, search history
  - Coding phase needs file states, error context, test results
  - Writing phase needs outlines, style decisions, content inventory

#### Implementation Considerations

- Use `asyncio.create_task()` during phase transition in `src/core/phase.py`
- Context package written to workspace files (survives compaction by design)
- New workspace injection type: `create_context_package_messages()` in `workspace_injection.py`
- Race condition handling: tactical phase must wait for context package if assembly isn't complete
- Fallback: if async assembly fails, fall back to current compaction behavior

---

## Combined Flow: All Three Features Working Together

```
1. Agent is in tactical phase, doing research.
   Context is filling up.

2. Agent calls compact_context(
     focus="Preserve all citation data and source URLs",
     preserve_patterns=["cite_web", "search_library"]
   )
   --> Tiered summarization kicks in:
       research segments get detailed preservation,
       routine file reads compress to one-liners

3. Todos complete, strategic phase starts.
   --> Async context assembler kicks off:
       reads plan, workspace, archived phase
       builds context package for next tactical phase

4. Strategic phase plans next tactical work.
   Agent calls compact_context(
     focus="Keep strategic decisions and phase plan"
   )

5. Tactical phase starts with curated context package:
   - Rich summary of what happened
   - Key decisions still in effect
   - Pre-rendered file states for relevant files
   - Clean conversation history (not raw compacted noise)
```

The agent goes from "passively having its memory compressed" to **"actively managing its own context."**

---

## Implementation Priority

| Feature | Effort | Impact | Risk | Dependencies |
|---------|--------|--------|------|-------------|
| **Summarization tool** | Low-medium | High | Low — additive, doesn't break existing safety layers | None |
| **Tiered summarization** | Medium | Medium-high | Low — replaces internals, same interfaces | None (but benefits from Feature 1) |
| **Async context assembly** | High | Very high | Medium — new async coordination, race conditions | Benefits from Features 1 + 2 |

### Suggested Ordering

1. **Summarization tool** — quick win, immediately useful, validates the concept of agent-controlled compaction
2. **Tiered summarization** — improves quality of all compaction (both agent-initiated and automatic)
3. **Async context assembly** — fundamental architecture change, builds on the other two

---

## Open Questions

- **Token budget for summaries:** Should the context package have a fixed token budget, or should it scale with the model's context window?
- **Summary verification:** Should the agent see its own summary to verify critical information was preserved? This costs tokens but prevents silent information loss.
- **Phase-type awareness:** Should the context assembler know about expert types (scholar vs developer) to customize what it preserves?
- **Checkpoint interaction:** How does async context assembly interact with the existing `PhaseSnapshotManager`? Should context packages be included in snapshots?
- **Multi-model summarization:** Could the tiered pipeline use a cheaper/faster model for classification (Stage 1) and the main model for domain summarization (Stage 2)?

---

## Industry Research & State of the Art

### Anthropic's Context Editing API (Beta)

Anthropic now offers **server-side context management** as a first-class API feature (`context-management-2025-06-27` beta). This is directly relevant — it validates our approach and offers building blocks we could leverage.

**Key capabilities:**
- `clear_tool_uses_20250919` — server-side tool result clearing with configurable thresholds, keep counts, and per-tool exclusions
- `clear_thinking_20251015` — thinking block management for extended thinking models
- Token counting endpoint supports previewing post-edit token counts
- Response includes `context_management.applied_edits` with clearing statistics

**Configuration options we should study:**
```json
{
  "type": "clear_tool_uses_20250919",
  "trigger": { "type": "input_tokens", "value": 30000 },
  "keep": { "type": "tool_uses", "value": 3 },
  "clear_at_least": { "type": "input_tokens", "value": 5000 },
  "exclude_tools": ["web_search"],
  "clear_tool_inputs": false
}
```

The `exclude_tools` and `clear_at_least` parameters are design patterns worth adopting — they map directly to our `preserve_patterns` concept in Feature 1.

**Memory Tool Integration:** Anthropic's docs describe combining context editing with their memory tool. When context approaches the clearing threshold, Claude gets an automatic warning to save important information to memory files before results are cleared. This is essentially what our workspace.md injection already does, but the "pre-clearing warning" concept is valuable — the agent could proactively write to workspace.md before compaction.

**SDK-level compaction:** The Anthropic SDK also offers client-side compaction via `tool_runner` with `compaction_control`. The summary is wrapped in `<summary></summary>` tags and replaces the entire message history. Their default summary prompt structures output into: Task Overview, Current State, Important Discoveries, Next Steps, Context to Preserve. Very similar to our `ConversationSummary` Pydantic model.

> Source: [Context editing - Claude API Docs](https://platform.claude.com/docs/en/build-with-claude/context-editing)

### Anthropic's Context Engineering Guide

Anthropic's engineering blog identifies compaction as one of the core strategies, with the key insight:

> "The art of compaction lies in the selection of what to keep versus what to discard, as overly aggressive compaction can result in the loss of subtle but critical context whose importance only becomes apparent later."

Their recommended approach:
1. Start by maximizing **recall** (capture everything relevant)
2. Iterate to improve **precision** (eliminate the superfluous)
3. Tool result clearing is the "safest lightest touch form of compaction"

They also highlight **structured note-taking** as a complementary pattern — agents maintain persistent notes outside the context window. Example: Claude playing Pokemon maintains tallies across thousands of steps without explicit prompting. This validates our workspace.md approach.

**Multi-agent sub-architectures:** Rather than one agent managing everything, specialized sub-agents handle focused tasks with clean context windows, returning only "condensed, distilled summaries (often 1,000-2,000 tokens)." This is relevant to the async context assembler — a sub-agent could build the context package.

> Source: [Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

### JetBrains Research: Observation Masking vs Summarization

Empirical research from JetBrains comparing compaction strategies reveals a surprising finding:

> **Observation masking outperformed LLM summarization in 4 of 5 test scenarios** while achieving over 50% cost reduction.

Key findings:
- **Observation masking** (replacing old tool outputs with placeholders while keeping agent reasoning/actions) preserves decision continuity without processing verbose historical outputs
- **LLM summarization** caused **trajectory elongation** — agents ran ~15% longer because summaries "hide signs indicating that the agent should already stop"
- Summaries can mask failure signals, causing agents to keep working on already-solved or unsolvable problems

**Implication for our design:** This suggests our compaction hierarchy is correct — tool result clearing (observation masking) should be the primary mechanism, with LLM summarization as a last resort. The tiered summarization (Feature 2) should be designed to avoid this elongation trap by explicitly preserving completion/failure signals.

> Source: [Efficient Context Management - JetBrains Research Blog](https://blog.jetbrains.com/research/2025/12/efficient-context-management/)

### Google ADK: Sliding Window Compaction

Google's Agent Development Kit implements a sliding window approach:
- Compaction triggers after N completed events (`compaction_interval`)
- Configurable `overlap_size` ensures continuity between windows
- Custom `LlmEventSummarizer` allows model selection and prompt customization
- Runner handles compaction automatically in the background

**Relevant pattern:** The overlap concept ensures context continuity — when summarizing events 6-9, event 6 (already in previous summary) is included again. This prevents "summary boundaries" from becoming information gaps. We should consider a similar overlap mechanism.

> Source: [Context Compaction - Google ADK Docs](https://google.github.io/adk-docs/context/compaction/)

### Manus: File-Based Dual Representation

Manus (the autonomous agent) uses a novel dual-representation pattern:
- **Full version**: Complete raw content stored in the filesystem
- **Compact version**: File path references used after the agent has processed results

As context fills, stale results are swapped for compact counterparts. The agent can retrieve the full version later if needed. This is essentially what our tool result clearing does (replacing with "[Result processed - see workspace if needed]"), but Manus makes it more explicit — the placeholder contains the actual file path for retrieval.

**KV-Cache optimization:** Manus treats KV-cache efficiency as a first-class concern, caching system instructions and older tool results across turns. They use task-level model routing (Claude for coding, Gemini for multimodal) rather than one model for everything.

**Hierarchical action space:** Rather than binding many tools, Manus uses fewer than 20 atomic functions and offloads complex operations to a sandbox. This keeps tool definitions stable and cache-friendly.

> Source: [Context Engineering in Manus](https://rlancemartin.github.io/2025/10/15/manus/)

### Recursive Language Models (Prime Intellect)

The most radical approach: instead of summarizing context, **delegate it to sub-processes.**

> "Rather than directly ingesting its (potentially large) input data, the RLM allows an LLM to use a persistent Python REPL to inspect and transform its input data, and to call sub-LLMs from within that Python REPL."

Key idea: The main model never holds the full data. It writes Python scripts that query, filter, and process data externally. Sub-LLMs handle focused analysis and return structured results.

**Implication for our design:** The async context assembler (Feature 3) could use a similar pattern — instead of the main agent building its own context package, a sub-process (or sub-LLM) could analyze the phase history and produce a curated context document. The main agent never touches the raw history.

> Source: [Recursive Language Models - Prime Intellect](https://www.primeintellect.ai/blog/rlm)

### Phil Schmid: Context Engineering Hierarchy

Phil Schmid's framework establishes a clear preference order:

> **Raw > Compaction > Summarization** — only summarize when compaction no longer yields enough space.

Additional patterns:
- **Context rot**: Performance degrades as context fills up, even within technical limits. Most models have an "effective context window" well below their advertised maximum.
- **Context pollution**: Too much irrelevant info actively hurts reasoning.
- **Context confusion**: LLMs struggle to distinguish instructions from data from structure.

**Design principle:** "Share memory by communicating, don't communicate by sharing memory" — adapted from Go concurrency patterns. Sub-agents should receive focused context packages, not full shared state.

**Agent-as-Tool pattern:** Instead of complex multi-agent hierarchies, treat sub-agents as deterministic tools via MapReduce: `call_planner(goal="...")` → harness spins up temporary sub-agent loop → returns structured JSON. This flattens complexity.

> Source: [Context Engineering for AI Agents: Part 2](https://www.philschmid.de/context-engineering-part-2)

---

## Revised Design Insights from Research

The research confirms our direction but suggests important refinements:

### 1. Observation Masking First, Always

JetBrains' finding that observation masking beats LLM summarization in most cases means Feature 1 (`compact_context` tool) should default to aggressive tool result clearing rather than summarization. The `urgency: "normal"` mode should only clear tool results. `urgency: "aggressive"` adds summarization on top.

### 2. Pre-Clearing Workspace Writes

Borrowing from Anthropic's memory tool integration: before compaction runs, the agent should get a signal to write critical information to workspace.md. The `compact_context` tool naturally enables this — the agent calls it when ready, meaning it has already (or can first) write important context to files.

### 3. Avoid Summary-Induced Elongation

The JetBrains trajectory elongation finding is critical. Our tiered summarization (Feature 2) must explicitly preserve:
- Completion signals ("all requirements met", "tests passing")
- Failure signals ("approach X failed because Y", "blocked on Z")
- Phase boundary markers

Without these, the agent may loop unnecessarily after compaction.

### 4. Overlap Windows at Phase Boundaries

Google ADK's overlap concept should apply to our phase transitions. When building a context package for phase N+1, include a summary of the last few events from phase N — not just a clean break.

### 5. Dual Representation for Tool Results

Manus's approach of storing both full and compact versions is more explicit than our current placeholder text. Instead of `"[Result processed - see workspace if needed]"`, we could use `"[Cleared: read_file('src/agent.py') → see workspace/tools/read_file_results.md#L42]"` — giving the agent a direct retrieval path.

### 6. Sub-Agent for Context Assembly

The RLM and multi-agent patterns suggest Feature 3 (async context assembly) should use a dedicated sub-LLM rather than the main agent. A cheaper, faster model (Haiku, Flash) can analyze the phase archive and produce a context package without consuming the main agent's context or budget.
