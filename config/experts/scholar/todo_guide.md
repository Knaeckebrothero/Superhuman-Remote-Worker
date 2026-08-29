# Todo Crafting Guide

**You MUST read this file before calling `next_phase_todos`.** The tool will reject
your call if you haven't. This guide teaches you how to create effective, focused todos
for exploration and idea generation.

---

## Core Principle: Breadth Over Depth

**Target: cover the whole investigation in ONE tactical phase (10-20 todos).** A phase
boundary costs a full planning cycle, so prefer a larger phase over several small ones.
Each todo should explore one question or
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
{% if has_tool("kb_write") -%}
3. **Names the output** — idea artifact, experiment result, note file, or knowledge note (kb_write)
{% else -%}
3. **Names the output** — idea artifact, experiment result, or note file (e.g., notes/research_log.md)
{% endif -%}
4. **Completable in 2-4 tool calls** — search, read results, write finding

### The Completion Test

Before finalizing each todo, ask: "What artifact does this produce?"
- "Research error handling" → No artifact named. Too vague.
{% if has_tool("kb_write") -%}
- "Search for error handling patterns in src/tools/, write idea to output/ideas/005_error_patterns.md if gaps found, or record dead end via the kb_write tool (type=learning, tag=dead-end)" → Clear artifact. Specific.
{% else -%}
- "Search for error handling patterns in src/tools/, write idea to output/ideas/005_error_patterns.md if gaps found, or record dead end to notes/dead_ends.md" → Clear artifact. Specific.
{% endif -%}

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
{% if has_tool("kb_write") -%}
- "Record sources discovered via the kb_write tool (type=source) and dead ends via the kb_write tool (type=learning, tag=dead-end)"
{% else -%}
- "Record sources discovered to notes/research_log.md and dead ends to notes/dead_ends.md"
{% endif -%}

### 2. Codebase Archaeology Phase

Purpose: Dig through the repository for patterns, gaps, and improvement opportunities.

Example todos:
- "Map directory structure of src/tools/ — read each __init__.py to understand tool categories"
- "Search for 'except Exception' across src/ — catalog overly broad exception handling"
- "Read src/core/context.py and src/core/workspace_injection.py — look for optimization opportunities"
- "Run `git log -n 30` via run_command to identify most frequently changed files — check for code churn patterns"
- "Run shell: ruff check src/ 2>&1 | head -50 — catalog lint issues by category"
- "Write idea artifacts for each finding with specific file:line references"

### 3. Log & Data Analysis Phase

Purpose: Mine job execution data for patterns and failure modes.

Example todos:
- "Query orchestrator API for recent failed jobs — categorize failure reasons"
{% if has_tool("kb_search") -%}
- "Search knowledge base (kb_search) for patterns across recent jobs — look for common decisions and learnings"
{% else -%}
- "Review notes/ and archive/ for patterns across recent phases — look for common decisions and learnings"
{% endif -%}
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

{% if has_tool("delegate_agent") -%}
### 5. Fan-Out Research Phase (`reader` children)

Purpose: Fan independent research threads out to `reader` children; keep your own context for synthesis.

DEFAULT for separable work: when a phase covers 2+ independent questions, sources, or topics,
structure it as a fan-out phase. A sequential exploration phase for separable threads is the
exception and needs a reason (threads depend on each other, or there is only one narrow topic).

Structure: 1-2 fan-out todos followed by 2-3 synthesis todos that process the returned results.
A fan-out todo means calling `delegate_agent` several times in a SINGLE turn — one `reader`
per question; the calls run in parallel. Readers return compact evidence-backed answers and
do not write files. You author every artifact and knowledge note yourself.

Example fan-out todo:
- "Fan out 3 `reader` children in ONE turn: (1) web research on Redis caching patterns in Python agent systems, (2) web research on in-memory caching with LRU/TTL strategies, (3) codebase search for existing caching in src/. Each brief is self-contained and returns 3-5 findings with citations"

Example synthesis todos (executed after the results return):
- "Cross-compare the three subagent result sets — identify overlapping recommendations and contradictions"
- "Write synthesis artifact output/ideas/013_caching_comparison.md from the returned findings, with tradeoff matrix"
- "Update knowledge base: consolidated caching learnings (kb_write), record dead ends from subagent results"
- "Update plan.md with coverage assessment — which caching aspects still need exploration?"

Rules for fan-out todos:
- Each spawned task needs: specific question, where to look (sources + exploration mode), expected return format
- Tasks must be independent — no task should need another task's results
- 2-5 `reader` children per fan-out turn (never 1 — just do it yourself)
- Write the fan-out todo FIRST in the phase, synthesis todos AFTER

When NOT to use a fan-out phase:
- The research threads depend on each other (use sequential todos instead)
- You only have one topic to explore (use a normal exploration phase)
- You need to experiment iteratively (fan-out is for parallel reading/research, not sequential experimentation)
{% endif -%}

### {% if has_tool("delegate_agent") %}6{% else %}5{% endif %}. Synthesis Phase

Purpose: Review accumulated findings and generate remaining idea artifacts.

Example todos:
- "Review notes/ for findings not yet written as idea artifacts — write remaining ideas"
- "Cross-reference ideas in output/ideas/ against task description — identify coverage gaps"
- "Check for related ideas that could be combined into a single stronger proposal"
{% if has_tool("kb_write") -%}
- "Record final coverage assessment via the kb_write tool (type=state, tag=coverage)"
{% else -%}
- "Record final coverage assessment to notes/coverage.md"
{% endif -%}
{% if has_tool("kb_write") -%}
- "Self-assess: list aspects of the task not yet explored, record as open questions via the kb_write tool (type=question)"
{% else -%}
- "Self-assess: list aspects of the task not yet explored, record as open questions to notes/open_questions.md"
{% endif -%}

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
{% if has_tool("delegate_agent") -%}
| Fan-Out Research | 1-2 fan-out + 2-3 synthesis | 2+ independent topics/sources — the DEFAULT for separable research |
{% endif -%}
| Synthesis | 4-6 | Reviewing findings, writing remaining ideas |

**Default to one phase covering the investigation.** The counts above are the shape of
the work inside a phase, not a reason to split: a second phase is worth its planning
cycle only when the next questions genuinely cannot be written until the first batch
lands. If findings invalidate the plan mid-phase, invoke `request_replan` instead.
