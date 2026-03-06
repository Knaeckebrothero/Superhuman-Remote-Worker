# Todo Crafting Guide

**You MUST read this file before calling `next_phase_todos`.** The tool will reject
your call if you haven't. This guide teaches you how to create effective, focused todos
for exploration and idea generation.

---

## Core Principle: Breadth Over Depth

**Target: 5-7 todos per tactical phase.** Each todo should explore one question or
produce one artifact. Don't spend more than one phase deep-diving a single topic.

Each tactical phase ends with a strategic review where you assess coverage:
- Which exploration modes have you used? (web, codebase, logs, experiments)
- Which aspects of the task are still unexplored?
- Are you going deep on one thing instead of scanning broadly?

A phase should represent one coherent exploration sweep — "scan web for caching patterns,"
"audit error handling in src/tools/," "run latency benchmark" — not an entire research program.

---

## Todo Specificity Rules

Every todo must be specific enough that you know *exactly* when it's done.

### Vague → Specific Examples

| Vague (fails) | Specific (works) |
|---|---|
| "Research caching" | "Web search for 'Redis vs in-memory caching Python LangGraph', summarize top 5 approaches to notes/caching_research.md" |
| "Look at the codebase" | "Read src/tools/research/ and search_files for 'retry' to find error handling patterns. Write idea if gaps found." |
| "Check performance" | "Run shell benchmark: time python -c 'from src.core.loader import load_config; load_config(\"defaults\")' — record result in output/experiments/003_config_load/" |
| "Explore logging" | "Query job audit trail via curl to orchestrator API for jobs with status=failed, look for common error patterns" |
| "Write up findings" | "Write output/ideas/007_retry_backoff.md with Problem, Proposal, Evidence, Effort from retry pattern analysis" |
| "Run an experiment" | "Create output/experiments/004_embedding_comparison/: benchmark sentence-transformers vs openai embeddings on 100 chunks from documents/" |

### What Makes a Good Exploration Todo

1. **Names the specific question** — "What are the tradeoffs of X?" not "Look into X"
2. **Names the exploration mode** — web search, codebase read, log query, or experiment
3. **Names the output** — idea artifact, experiment result, note file, or workspace.md update
4. **Completable in 2-4 tool calls** — search, read results, write finding

### The Completion Test

Before finalizing each todo, ask: "What artifact does this produce?"
- "Research error handling" → No artifact named. Too vague.
- "Search for error handling patterns in src/tools/, write idea to output/ideas/005_error_patterns.md if gaps found, or note dead end in workspace.md" → Clear artifact. Specific.

---

## Phase Design Patterns

### 1. Web Exploration Phase

Purpose: Scan external sources for techniques, tools, papers, and patterns.

Example todos:
- "Web search 'LangGraph state management patterns 2025' — save top 5 results to notes/state_patterns.md"
- "Web search 'Python async retry strategies' — compare approaches, note tradeoffs"
- "Search papers for 'retrieval augmented generation optimization' — download top 2 to documents/"
- "Extract full content from [specific URL found in prior search] — summarize key techniques"
- "Write idea artifacts for any actionable findings from web research"
- "Update workspace.md with sources discovered and dead ends"

### 2. Codebase Archaeology Phase

Purpose: Dig through the repository for patterns, gaps, and improvement opportunities.

Example todos:
- "Map directory structure of src/tools/ — read each __init__.py to understand tool categories"
- "Search for 'except Exception' across src/ — catalog overly broad exception handling"
- "Read src/core/context.py and src/core/workspace_injection.py — look for optimization opportunities"
- "Use git_log(max_count=30) to identify most frequently changed files — check for code churn patterns"
- "Run shell: ruff check src/ 2>&1 | head -50 — catalog lint issues by category"
- "Write idea artifacts for each finding with specific file:line references"

### 3. Log & Data Analysis Phase

Purpose: Mine job execution data for patterns and failure modes.

Example todos:
- "Query orchestrator API for recent failed jobs — categorize failure reasons"
- "Read workspace files from 3 recent jobs — look for common patterns in workspace.md structure"
- "Use SQL tools to query citation success/failure rates across jobs"
- "Analyze token usage patterns — which tool categories consume the most context?"
- "Write idea artifacts for operational improvements based on data patterns"

### 4. Experiment Phase

Purpose: Run controlled tests to validate hypotheses from exploration.

Example todos:
- "Create output/experiments/NNN_title/setup.md describing hypothesis and methodology"
- "Run benchmark: [specific command] — capture output to experiments/NNN_title/results.md"
- "Compare approach A vs approach B using [specific metric] on [specific data]"
- "Document results with actual numbers — include commands.sh for reproducibility"
- "Write idea artifact if experiment supports the hypothesis, or note dead end if not"

### 5. Synthesis Phase

Purpose: Review accumulated findings and generate remaining idea artifacts.

Example todos:
- "Review notes/ for findings not yet written as idea artifacts — write remaining ideas"
- "Cross-reference ideas in output/ideas/ against task description — identify coverage gaps"
- "Check for related ideas that could be combined into a single stronger proposal"
- "Update workspace.md with final ideas index and coverage assessment"
- "Self-assess: list aspects of the task not yet explored, note as open questions in workspace.md"

---

## Citation Discipline

Every factual claim in an idea artifact must cite a source:
- `cite_web` for web sources
- `cite_document` for documents with page/section reference
- If you can't cite it, label it as an assumption, not a finding

---

## Quick Reference

| Phase type | Typical todos | When to use |
|---|---|---|
| Web Exploration | 5-7 | Starting new topic, need external context |
| Codebase Archaeology | 5-7 | Examining repository for patterns and gaps |
| Log & Data Analysis | 4-6 | Mining execution data for operational insights |
| Experiment | 3-5 | Validating a hypothesis with a benchmark or PoC |
| Synthesis | 4-6 | Reviewing findings, writing remaining ideas |

**Default to 5 todos.** Go higher (6-7) for broad web exploration sweeps. Go lower
(3-4) for focused experiments. If you need more than 7, split into two phases.
