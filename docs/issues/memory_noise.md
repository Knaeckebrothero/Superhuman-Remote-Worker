# Memory Light: Noise & Redundancy Issues

**Date:** 2026-03-09
**Status:** Open
**Component:** Memory Light (RecallStore, observer, free channels)

---

## Summary

Analysis of 1,476 stored memories across 5 recent jobs reveals significant noise in the Memory Light system. Low-value memories (framework mechanics, raw tool errors, duplicate todo completions) dominate retrieval, crowding out genuinely useful domain insights. The most-accessed memories in the largest job (838 memories, 3,232 audit entries) are tool_error entries accessed 600-700+ times each.

---

## Data Snapshot

**Total memories:** 1,476 across 5 jobs

| Source | Type | Count |
|--------|------|-------|
| observer | factual | 604 |
| observer | procedural | 438 |
| observer | error_solution | 260 |
| tool_error | error_solution | 93 |
| observer | relational | 62 |
| observer | vocabulary | 17 |
| compaction | factual | 2 |

**Notably absent:** `todo` and `phase_archive` sources show 0 in the breakdown because dedup (0.92 cosine threshold) merges them into existing observer memories, inflating access_count and importance of those entries instead.

---

## Issue 1: Todo Memories Stored Twice (and Unnecessary)

**Channels affected:** `todo` (src/tools/core/todo.py:219-229), `phase_archive` (src/graph.py:1430-1445)

The same content is stored in two places:

1. **`todo_complete` tool** — fires on every todo completion with notes, importance **0.7**, source `"todo"`
2. **`archive_phase` node** — re-iterates ALL completed todos at phase boundary, importance **0.5**, source `"phase_archive"`

Both produce identical content: `f"Completed: {todo.content}\nOutcome: {'; '.join(todo.notes)}"`

**Why this is noise:** The agent already has the full todo list in context at all times (todos.yaml is loaded every turn). Storing "you completed todo X" as a retrievable memory adds nothing — the agent knows what it did. The todo notes are typically terse ("done", "verified", "deployed successfully") and rarely contain genuine reusable insights.

**Impact:** Dedup catches the identical text (cosine > 0.92) and merges, but this still inflates access_count and importance of existing memories, distorting the ranking for genuinely useful entries.

### Proposed Fix

- **Remove `phase_archive` channel entirely** — fully redundant with `todo` channel.
- **Gate the `todo` channel** — only store if notes exceed a minimum length threshold (e.g., > 50 chars) suggesting actual insight rather than "done". Or remove it entirely and let the observer extract genuine insights from todo completions if they're noteworthy.

---

## Issue 2: Tool Errors Stored Raw Without Solutions

**Channel:** `tool_error` (src/graph.py:2342-2362)

Every tool failure is stored verbatim:
```python
content=f"Tool '{tool_name}' failed: {content[:500]}",
keywords=[tool_name, "error"],
importance=0.6,
source="tool_error",
memory_type="error_solution",
```

**Problems:**
- **Mislabeled as `error_solution`** — there's no solution, just the raw error text.
- **Importance 0.6 is too high** — puts these on par with genuinely useful procedural knowledge.
- **Keywords are too generic** — `["shell_execute", "error"]` matches almost every retrieval query involving shell work, causing constant injection.
- **Volume:** 93 tool_error memories, many accessed 600-700+ times in the GPT OSS deploy job. The top 10 most-accessed memories across all jobs are predominantly tool_error entries.

**Example of the noise:**
```
Tool 'shell_execute' failed: [Shells: default | server | server2 | srv | srv-105 | srv-105-check]
Exit code: 125
--- stdout ---
>   --name gpt-oss-20b
```
This tells the agent nothing actionable — it's a raw shell dump with no diagnosis or fix.

### Proposed Fix

**Option A (preferred): Remove the channel entirely.** The observer already extracts genuine `error_solution` memories when it sees errors + resolutions in conversation context. The observer version includes the diagnosis and fix, not just the raw error.

**Option B: Reduce importance to 0.3 and add a minimum content quality gate.** Only store if the error message contains recognizable error patterns (not just exit codes and shell state dumps). Relabel as `memory_type="factual"` since there's no solution component.

---

## Issue 3: Observer Extracts Framework Mechanics

**Channel:** `observer` (src/services/auxiliary.py, prompt: config/prompts/memory_extraction_prompt.txt)

The observer LLM extracts memories about the agent framework's own internal mechanics — things that are either system-enforced or always available in context:

**Examples found in the database:**
- *"The Todo Crafting Guide must be read before calling next_phase_todos — the tool will reject the call if the guide hasn't been read first."* (access_count: 103, importance: 1.0)
- *"workspace.md has a strict two-tier structure: PROTECTED sections must be preserved verbatim..."* (access_count: 44, importance: 0.9)
- *"Phase transition follows a pattern: tactical phase completes, then strategic phase begins with review..."* (access_count: 529, importance: 0.8)

**Why this is noise:**
- The todo guide enforcement is a hard-coded tool rejection — the agent will discover it if it forgets, and the guide itself is always available as an instruction file.
- workspace.md structure is documented in the workspace template, injected at job start.
- Phase transition patterns are baked into the graph topology — the agent can't deviate regardless.

These memories consume budget_tokens on every injection, displacing domain-specific knowledge that the agent actually needs.

### Proposed Fix

Add explicit exclusions to the observer prompt's "What NOT to Extract" section:

```
## What NOT to Extract

- Routine tool calls with predictable outcomes (file reads, simple searches)
- Raw data or file contents — distill the insight instead
- The agent's internal planning monologue ("I will now..." or "Let me think...")
- Information from system injection messages (workspace content, memory blocks)
- **Framework mechanics and system-enforced rules** — the agent's own tool constraints
  (e.g., "must read file X before tool Y"), phase transition patterns, workspace.md
  structure rules, todo crafting guidelines, and other behaviors that are enforced by
  the system regardless of whether the agent remembers them.
- **Todo completion logistics** — what todos were completed and in what order. The agent
  already has the full todo list in context. Only extract if the *outcome* reveals a
  non-obvious insight about the domain or task.
- **Information already pinned in workspace.md** — constraints, hard rules, and
  requirements that the agent wrote to its own persistent memory. Extracting these as
  separate memories creates redundant retrieval hits.
```

---

## Issue 4: No Importance Floor on Retrieval

**Code:** `recall_store.py:445-500` (hybrid_search), vector DB SQL function `memory_hybrid_search()`

The retrieval pipeline has zero filtering by importance. A 0.5 importance shell error competes equally with a 0.95 critical deployment insight — ranking is purely by RRF score (vector similarity + keyword match + recency). This means a low-value memory that happens to keyword-match well will outrank a high-value memory that's semantically relevant but uses different terms.

### Proposed Fix

Add an importance floor to the hybrid search SQL:

```sql
WHERE job_id = $3 AND importance >= $8  -- new parameter: importance_threshold
```

Default threshold: **0.4** (filters out the bottom tier without losing useful 0.5+ memories).

Alternatively, incorporate importance as a **fourth RRF channel** or as a post-retrieval re-ranking multiplier:

```python
final_score = rrf_score * (0.5 + 0.5 * importance)  # importance scales from 0.5x to 1.0x
```

This preserves all candidates but systematically demotes low-importance entries.

---

## Issue 5: Redundant Factual Memories (No Dedup Across Phases)

The observer extracts the same fact in multiple phases because it sees the fact reinforced in conversation:

**Example — "GPU 0 is locked" extracted 5+ times:**
- *"Do NOT modify or restart anything on GPU 0"* (phase 0, importance 1.0, access: 63)
- *"GPU 0 is locked for embedding stack — must NOT be modified"* (phase 0, importance 1.0, access: 143)
- *"Server 10.18.2.105 has 3x NVIDIA L40S GPUs. GPU 0 is locked"* (phase 0, importance 1.0, access: 86)
- *"Hard constraint: GPU 0 and embedding stack must not be touched"* (phase 6, importance 0.95, access: 0)
- *"Hard operational constraints discovered: GPU 0..."* (phase 4, importance 0.95, access: 28)

The dedup threshold (0.92 cosine) catches near-identical text but not semantic duplicates with different wording. These all say the same thing but consume 5 retrieval slots.

### Proposed Fix

**Option A:** Lower the dedup cosine threshold from 0.92 to **0.85** to catch semantic duplicates.

**Option B:** Add a post-retrieval dedup pass that clusters retrieved memories by semantic similarity and picks the highest-importance representative from each cluster.

**Option C:** Have the observer prompt include a "current memories" context so it can avoid re-extracting known facts. (Trade-off: increases observer prompt size and cost.)

---

## Priority Order

| # | Fix | Impact | Effort |
|---|-----|--------|--------|
| 1 | Update observer prompt exclusions | High — reduces noise at source | Low |
| 2 | Remove/gate tool_error channel | High — eliminates top noise source | Low |
| 3 | Remove phase_archive channel | Medium — eliminates duplication | Trivial |
| 4 | Add importance floor to retrieval | Medium — improves ranking | Low |
| 5 | Gate todo channel (min note length) | Medium — reduces low-value entries | Low |
| 6 | Semantic dedup improvements | Medium — reduces redundancy | Medium |
