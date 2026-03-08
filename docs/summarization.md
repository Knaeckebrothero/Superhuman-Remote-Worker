# Conversation Summarization & Context Compaction

Design document for the agent's context management system. Covers the current implementation, industry research findings (March 2026), and planned improvements.

## Current Implementation

**File:** `src/core/context.py` (ContextManager class)

### Three-Layer Defense

| Layer | Trigger | Action |
|-------|---------|--------|
| Layer 0 | HTTP token-limit error | `ReasoningChatOpenAI` catches `ContextOverflowError` |
| Layer 1 | Pre-request token check | `execute` node triggers compaction proactively |
| Layer 2 | Overflow despite L1 | Emergency compaction + retry |

### Compaction Pipeline (in order of aggressiveness)

1. **Tool result clearing** — Replace old ToolMessage content with `[Result processed]` placeholder. Keeps the N most recent results intact (`keep_recent_tool_results`, default 15). Anthropic calls this "the safest, lightest touch form of compaction."

2. **Tool result truncation** — Truncate remaining long tool results to `max_tool_result_length` (default 5000 chars).

3. **Message trimming** — Keep system messages + first HumanMessage + last N conversation messages. Uses `find_safe_slice_start()` to avoid orphaning ToolMessages from their parent AIMessage.

4. **LLM summarization** — Structured output via `AuxiliaryLLM.chain(SummarizeTask(...))`. Produces a `ConversationSummary` Pydantic model. Injected as `SystemMessage("[Summary of prior work]\n...")`. Old summaries are rolled into new ones (rolling summary pattern).

5. **Progressive compaction** — If `force=True` and still too large after summarization, retry with progressively smaller `keep_recent` windows (half → quarter → 2 → 1).

6. **Emergency truncation** — Last resort. Truncate ALL tool results to 2000 chars, then 500 chars if still over.

### Observation Masking

During summarization formatting (`_format_messages_for_summary`):
- Last 10 ToolMessages: include truncated content (first 300 chars)
- Older ToolMessages: placeholder only (`[Tool 'X' result omitted (N chars)]`)
- Workspace injection messages: skipped entirely (re-injected fresh)
- Prior summary SystemMessages: included so context rolls forward
- HumanMessages: truncated to 500 chars
- AIMessages with tool_calls: show tool names only
- AIMessages with content: truncated to 300 chars

### Summarization Prompt

**File:** `config/prompts/summarization_prompt.txt`

Key design decisions:
- Framed as "handoff to your future self" — the summary is the ONLY context that survives
- Excludes workspace.md and plan.md content (they're persistent, re-injected every call)
- Explicit preservation priority: user corrections > errors/failures > active work > completed work
- Structured JSON output with `identity_anchor` for role/constraint persistence
- Instruction to weight recent messages more heavily

### Summarization Output Schema

```python
class ConversationSummary(BaseModel):
    summary: str              # Narrative overview (max 3 sentences)
    tasks_completed: str      # Bullet list of finished tasks
    tasks_in_progress: str    # Started but unfinished, with current state
    key_decisions: str        # Choices made with reasoning
    current_state: str        # Where the agent is now, what comes next
    blockers: str             # Active blockers with exact error messages
    critical_facts: str       # IDs, paths, URLs, versions that must survive verbatim
    state_changes: str        # Files created, modified, or deleted
    pinned_instructions: str  # Active constraints from instructions
    identity_anchor: dict     # agent_role, current_task, active_constraints
```

### Recursive Summarization

For inputs exceeding `summarization_safe_limit` tokens:
1. Split formatted parts into chunks of `summarization_chunk_size` tokens
2. Summarize each chunk independently (proportional max_summary_length)
3. If combined summaries still exceed safe limit, recurse (max depth 5)
4. Final unification pass merges chunk summaries into one coherent summary

## Research Findings (March 2026)

### Industry Approaches Compared

| Method | Compression | Accuracy | Speed | Inspectable |
|--------|-------------|----------|-------|-------------|
| LLM Summarization | 7-12k chars from any size | ~37% multi-session retention | Slow (LLM call) | Yes |
| Opaque Compression (OpenAI) | 99.3% ratio | 3.43/5 accuracy | Fast | No (vendor-locked) |
| Verbatim Compaction (Morph) | 50-70% ratio | 98% verbatim accuracy | 3300+ tok/s | Yes |
| Observation Masking (JetBrains) | 26-54% token reduction | 95%+ accuracy | No LLM needed | Yes |

**Source:** [Morph — Compaction vs Summarization](https://www.morphllm.com/compaction-vs-summarization)

### Key Findings

**Summarization loses technical details.** Exact file paths become paraphrased or hallucinated, error messages get reworded (losing grep-able strings), config values get rounded, line numbers shift or disappear. This creates a "re-reading loop" where agents search, fill context, summarize, lose details, then re-search — oscillating without progress.

**Source:** Morph analysis, [DEV Community — 10x Context Extension](https://dev.to/amitksingh1490/how-we-extended-llm-conversations-by-10x-with-intelligent-context-compaction-4h0a)

**Observation masking is surprisingly effective.** JetBrains found that simply hiding old tool outputs (keeping tool call metadata visible) "matched the quality of full LLM summarization while using less compute" on SWE-bench. The ACON framework demonstrated 26-54% peak token reduction while preserving 95%+ accuracy. Key insight: reasoning traces matter more than raw tool output data.

**Source:** JetBrains "Complexity Trap" research, ACON framework

**Recursive summarization works but drifts.** The academic paper on recursive summarization found ~10% error rate in generated memories: fabricated facts (2.7%), incorrect relationships (3.2%), missing details (3.9%). However, generated summaries still performed better than fragmented golden annotations for response generation. Two-shot examples brought measurable improvement over zero-shot.

**Source:** [Recursively Summarizing Enables Long-Term Dialogue Memory in LLMs](https://arxiv.org/html/2308.15022v3)

**Tool call/result pairs must be atomic.** ForgeCode's compaction system "maintains tool call/result pairs atomically (never splits them)." When selecting messages for compaction, any tool invocation and its corresponding result must stay together as a unit.

**Source:** [ForgeCode — Context Compaction](https://forgecode.dev/docs/context-compaction/)

**Trigger early, not at the limit.** Will Larson recommends triggering compaction at 80% context capacity, not 100%. Store pre-compaction context as a virtual file so agents can recover dropped information if needed. Sourcegraph's approach: use verbatim compaction first, then if a second compaction is needed, spawn a fresh agent with a task summary (hand-off).

**Source:** Morph article (citing Will Larson)

**Different strategies for different content types.** Coding agents editing files need verbatim compaction (file paths and line numbers must survive). General reasoning chains compress well with summarization. Tool-output-heavy workflows benefit most from observation masking + compaction targeting the bloat source directly.

**Source:** Morph comparison table

**Context engineering principles.** LangChain's context engineering guide identifies four failure modes warranting removal: context poisoning (hallucinations entering memory), context distraction (overwhelming irrelevant information), context confusion (superfluous data influencing responses), context clash (contradictory information). Selective compression of tool outputs at specific nodes, rather than blanket summarization, is recommended.

**Source:** [LangChain — Context Engineering for Agents](https://blog.langchain.com/context-engineering-for-agents/)

**Google ADK uses sliding window with overlap.** Their compaction compresses at intervals (e.g., every 3 completed events) with an overlap parameter that includes previously compacted events in subsequent compressions. This overlap prevents information gaps at chunk boundaries.

**Source:** [Google ADK — Context Compression](https://google.github.io/adk-docs/context/compaction/)

## Gap Analysis: Current vs. Best Practices

### Already Doing Well

| Practice | Status | Notes |
|----------|--------|-------|
| Observation masking | Done | `_format_messages_for_summary` masks old tool outputs |
| Structured output | Done | `ConversationSummary` via `with_structured_output()` |
| Rolling summary merging | Done | Old summaries fed into new summarizations (line 1250) |
| Progressive compaction | Done | Escalating `keep_recent` windows |
| Preservation priority | Done | Prompt explicitly ranks by importance |
| Workspace exclusion | Done | workspace.md/plan.md skipped (re-injected fresh) |
| Safe slicing | Done | `find_safe_slice_start()` prevents orphaned ToolMessages |
| Orphan cleanup | Done | `sanitize_message_history()` removes orphaned ToolMessages |

### Identified Improvements

#### 1. Recency Markers in Formatted Text
**Problem:** The summarization prompt says "weight recent messages more heavily" but the formatted conversation is passed in flat chronological order with no structural emphasis.
**Solution:** Insert explicit section markers like `=== RECENT CONTEXT (HIGHEST PRIORITY) ===` before the last N messages in the formatted text passed to the summarizer.
**Effort:** Small. Change in `_format_messages_for_summary` or `summarize_conversation`.

#### 2. Archive Pre-Compaction Text to Disk
**Problem:** Once summarized, the raw conversation is gone. If the summary loses a critical detail (path, error message, ID), the agent cannot recover it.
**Solution:** Dump the full formatted conversation to `archive/compaction_{n}.md` before summarizing. The agent can then `read_file` the archive if it suspects information loss. This is Will Larson's "virtual file" pattern.
**Effort:** Small. Add a file write in `summarize_and_compact`.

#### 3. Atomic Tool-Call Grouping in Observation Masking
**Problem:** Current masking uses a flat index-based window (last 10 ToolMessages get content). But a single AIMessage may call 3 tools — if only 1 of those 3 results falls in the recent window, the group is split. The summarizer sees 2 masked results and 1 with content, losing the relationship.
**Solution:** Group ToolMessages by their parent AIMessage's tool_call IDs. Apply the recency window to groups, not individual messages. If any result in a group is recent, include all results in that group.
**Effort:** Medium. Requires building a tool-call-ID → group mapping in `_format_messages_for_summary`.

#### 4. Increase Assistant Reasoning Truncation Limit
**Problem:** AIMessage content is truncated to 300 chars. The ACON framework finding: "reasoning traces matter more than raw tool output data." The agent's reasoning about *what to do and why* is often the most valuable part of the conversation for summarization. 300 chars frequently cuts off mid-thought.
**Solution:** Increase AIMessage content truncation from 300 to 800 chars. This is a one-line change.
**Effort:** Tiny.

#### 5. Flatten identity_anchor Schema
**Problem:** The `identity_anchor` field is a nested dict (`{agent_role, current_task, active_constraints}`). Some models struggle with nested JSON in structured output, producing malformed dicts or stringified JSON. The formatting code in `_single_pass_summarize` already has to handle both dict and string cases with fallback logic.
**Solution:** Promote to top-level fields: `agent_role: str`, `current_task: str`, `active_constraints: str`. Eliminates the nested-dict fragility. Requires updating `ConversationSummary`, the summarization prompt, and the formatting code.
**Effort:** Small.

#### 6. Extract Key Facts from Masked Tool Results
**Problem:** When old tool results are masked to `[Tool 'X' result omitted (N chars)]`, all information is lost — including file paths, error messages, and IDs that the summarization prompt explicitly asks to preserve. The summarizer can only preserve what it sees.
**Solution:** For masked tool results, extract a brief "key facts" line: file paths mentioned, error codes, entity IDs. Use a simple regex/heuristic extraction (no LLM call). Example: `[Tool 'read_file' result omitted (45000 chars) | path: src/main.py]`
**Effort:** Medium. Requires a fact extraction helper and changes to the masking logic.

#### 7. Prior Summary Deduplication Instruction
**Problem:** When rolling summaries, the new summary often repeats information from the old summary verbatim, wasting tokens. The recursive summarization paper found this contributes to the ~3.9% "missing details" error — token budget is spent on redundant facts instead of new information.
**Solution:** Add an instruction to the summarization prompt: "The prior summary is included for context continuity. Do NOT repeat facts from it unless they remain actively relevant. Focus new summary tokens on information from the NEW conversation."
**Effort:** Tiny. Prompt-only change.

## Implementation Priority

| # | Improvement | Impact | Effort | Risk |
|---|-------------|--------|--------|------|
| 1 | Recency markers | High | Small | None |
| 2 | Archive pre-compaction | High | Small | Disk usage |
| 3 | Atomic tool-call grouping | Medium | Medium | Edge cases in group boundary |
| 4 | Increase reasoning truncation | Medium | Tiny | Slightly larger summaries |
| 5 | Flatten identity_anchor | Medium | Small | Schema migration (no prod) |
| 6 | Extract key facts from masked results | Medium | Medium | Regex reliability |
| 7 | Prior summary dedup instruction | Low | Tiny | None |

## References

- [Morph — Compaction vs Summarization](https://www.morphllm.com/compaction-vs-summarization)
- [ForgeCode — Context Compaction](https://forgecode.dev/docs/context-compaction/)
- [DEV Community — 10x Context Extension](https://dev.to/amitksingh1490/how-we-extended-llm-conversations-by-10x-with-intelligent-context-compaction-4h0a)
- [LangChain — Context Engineering for Agents](https://blog.langchain.com/context-engineering-for-agents/)
- [Google ADK — Context Compression](https://google.github.io/adk-docs/context/compaction/)
- [Recursive Summarization for Long-Term Dialogue Memory (arXiv)](https://arxiv.org/html/2308.15022v3)
- [LangChain — Memory Overview](https://docs.langchain.com/oss/python/langgraph/memory)
