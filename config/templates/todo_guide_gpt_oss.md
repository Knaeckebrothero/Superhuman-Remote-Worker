# Todo Crafting Guide

**You MUST read this file before calling `next_phase_todos`.** The tool will reject your call if you haven't.

---

## Core Principle: Short Phases, Substantive Output

**Target: 3-5 todos per tactical phase.** Each todo must produce real content, not scaffolding.

- **Simple tasks**: 3-4 todos
- **Standard tasks**: 5 todos
- **Complex tasks**: 5 todos max — split further if you need more

Each phase ends with a strategic review. Shorter phases mean earlier error detection.

---

## Forbidden Todo Patterns

These patterns produce empty output. Never create todos like these:

| FORBIDDEN (produces skeletons) | REQUIRED (produces substance) |
|---|---|
| "Create skeleton/outline for chapter X" | "Write complete chapter X with full prose (minimum 1500 words)" |
| "Add headings for section Y" | "Write section Y covering [topic] with evidence from [source]" |
| "Set up file structure for output" | "Write output/report.md section 1: [topic] (800-1200 words)" |
| "Prepare template for analysis" | "Write analysis of [topic] using data from [source file]" |
| "Draft initial structure" | "Write first draft of [deliverable] with all required content" |
| "Verify files exist in output/" | "Read output/chapter3.md and verify it contains 1000+ words of prose, not just headings" |

**The test**: If completing a todo could result in a file under 100 words, the todo is wrong. Rewrite it to require substantive content.

---

## Todo Specificity Rules

Every todo must be specific enough that you know exactly when it's done.

| Vague (fails) | Specific (works) |
|---|---|
| "Check all citations" | "Verify citations 1-10 against source documents in documents/" |
| "Write the analysis" | "Write section 2.1: Market Overview (600-800 words) using findings from phase 2 research" |
{% if has_tool("kb_write") -%}
| "Research the topic" | "Web search for 'renewable energy policy EU 2025', record key findings using kb_write" |
{% else -%}
| "Research the topic" | "Web search for 'renewable energy policy EU 2025', record key findings in notes/research_notes.md" |
{% endif -%}
| "Process the documents" | "Extract text from documents/report.pdf pages 1-15 using read_file" |
| "Review the output" | "Read output/chapter3.md, verify it has 1000+ words, all claims cited, no placeholder text" |

### What Makes a Good Todo

1. **Names the specific artifact** — file path, section number, page range
2. **Names the specific tool** — "use read_file," "use web_search," "use write_file"
3. **Has a measurable outcome** — word count, content description, verification criteria
4. **Completable in 1-3 tool calls** — if it needs more, split it
5. **Produces substance** — the result must be real content, not structure

---

## Phase Design Patterns

### 1. Research Phase (first, for unfamiliar topics)

Example todos:
{% if has_tool("kb_write") -%}
- "Web search for 'topic X state of the art 2025' and record key findings using kb_write(type='learning')"
- "Read documents/brief.pdf pages 1-10 and record key themes using kb_write(type='learning')"
- "Record research findings as knowledge notes using kb_write"
{% else -%}
- "Web search for 'topic X state of the art 2025' and record key findings in notes/research_notes.md"
- "Read documents/brief.pdf pages 1-10 and record key themes in notes/research_notes.md"
- "Record research findings in notes/research_notes.md"
{% endif -%}

### 2. Execution Phase (produce one section or artifact)

Example todos:
- "Write output/chapter2.md section 2.1: Market Analysis (800-1200 words) using sources from documents/market_*.pdf"
- "Write output/chapter2.md section 2.2: Competitor Landscape (600-800 words) citing findings from phase 3"
- "Add citations to all claims in output/chapter2.md using cite_web and cite_document"
- "Read output/chapter2.md and verify: all sections contain prose (not placeholders), word count 800-1200, all claims cited"

### 3. Batch Processing Phase (repetitive operations)

Example todos:
- "Process documents/input_01.pdf through input_05.pdf: extract key findings to output/findings.md"
- "Apply formatting template to output/chapter_01.md through output/chapter_05.md"
- "Verify all 5 processed items: read each file, confirm content exceeds 100 words and contains real analysis"

### 4. Integration Phase (combine and cross-reference)

Example todos:
- "Read all chapter files in output/ and check for consistency in terminology"
- "Write output/conclusion.md (400-600 words) summarizing results and tying back to objectives"
- "Final read-through of output/report.md: check flow, fix cross-references, verify completeness"

### 5. Verification Phase (confirm quality before done)

Example todos:
- "Compare output/ file list against required deliverables in instructions.md"
- "Read output/report.md sections 1-3: verify each contains 500+ words of prose, no placeholder text"
- "Read instructions.md one final time and confirm every requirement is addressed"

**Verification todos must produce evidence.** "Verified — looks good" is NOT evidence. "Read output/chapter3.md — contains 1,847 words, covers all 4 required topics, 6 citations present" IS evidence.

{% if has_tool("delegate_work") -%}
### 7. Delegation Phase (parallel independent subtasks)

Purpose: Split work across subagents when tasks are independent and benefit from parallelism.

Example todos:
- "Delegate parallel research: delegate_work with 3 tasks — topic A, topic B, topic C"
- "Review delegation results: check each child's git diff, approve or send feedback"
- "Merge and reconcile: resolve conflicts between child outputs, update plan.md"

Delegate when: 2+ independent research topics, analyzing separate subsystems, writing unrelated sections.
Do it yourself when: Sequential work, tightly coupled tasks, fewer than 2 parallel tracks.
{% endif -%}

---

## Web Search Mandate

**Before producing domain-specific content, search the web first.** Your training data may be outdated. Web search provides current data, recent publications, domain conventions, and quality benchmarks.

When search is unnecessary: copying/reformatting existing documents, internal workspace operations, purely structural tasks.

---

## Content Quality Checklist

Before finalizing any phase's todos, verify each todo passes ALL of these:

- [ ] Does this todo produce real content (not just headings or placeholders)?
- [ ] Is the expected output measurable (word count, specific content, verification criteria)?
- [ ] Could the completed todo result in a file under 100 words? If yes, the todo is wrong.
- [ ] Does the verification todo check content substance, not just file existence?

---

## Quick Reference

| Phase type | Typical todos | When to use |
|---|---|---|
| Research | 3-4 | Starting a new topic, need current info |
| Execution | 4-5 | Writing/producing a specific section or artifact |
| Batch Processing | 4-5 | Repetitive operations on multiple items |
| Integration | 4-5 | Combining separately-produced parts |
| Verification | 3-4 | Quality check before completion |
{% if has_tool("delegate_work") -%}
| Delegation | 2-3 | Independent subtasks that benefit from parallel execution |
{% endif -%}

**Default to 5 todos.** Go lower (3-4) for focused phases. If you need more than 5, split into two phases.
