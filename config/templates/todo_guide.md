# Todo Crafting Guide

**You MUST read this file before calling `next_phase_todos`.** The tool will reject
your call if you haven't. This guide teaches you how to create effective, focused todos.

---

## Core Principle: Short Phases, Tight Focus

**Target: 3-7 todos per tactical phase.** Adapt based on task complexity:
- **Simple, well-defined tasks**: 3-5 todos (or skip planning for trivial work)
- **Standard tasks**: 5 todos (the default)
- **Complex, multi-step tasks**: 5-7 todos (split further if you need more)

Each tactical phase ends with a strategic review. More frequent reviews mean:
- Earlier detection of wrong directions
- Better-adapted plans based on what you actually learned
- Less wasted work if priorities shift

A phase should represent one coherent unit of work — "research the topic," "write section 3,"
"verify all citations in chapter 2" — not an entire project stage.

---

## Todo Specificity Rules

Every todo must be specific enough that you know *exactly* when it's done.

### Vague → Specific Examples

| Vague (fails) | Specific (works) |
|---|---|
| "Check all citations" | "Verify citations 1-10 against source documents in documents/" |
| "Write the analysis" | "Write section 2.1: Market Overview using findings from phase 2 research" |
{% if has_tool("kb_write") -%}
| "Research the topic" | "Web search for 'renewable energy policy EU 2025', record key findings using kb_write" |
{% else -%}
| "Research the topic" | "Web search for 'renewable energy policy EU 2025', record key findings in notes/research_notes.md" |
{% endif -%}
| "Process the documents" | "Extract text from documents/report.pdf pages 1-15 using read_file" |
| "Review the output" | "Compare output/chapter3.md against requirements 4.1-4.3 from instructions.md" |
| "Improve the quality" | "Add 3 supporting citations to section 2 from the sources identified in phase 3" |
| "Handle edge cases" | "Add error handling for empty input in output/script.py lines 45-60" |

### What Makes a Good Todo

1. **Names the specific artifact** — file path, section number, page range, citation IDs
2. **Names the specific tool** — "use read_file," "use web_search," "use write_file"
3. **Has a measurable outcome** — "produces X," "updates Y to contain Z," "verifies N items"
4. **Completable in 1-3 tool calls** — if it needs more, split it

### The Specificity Test

Before finalizing each todo, ask: "Could I verify this is done by checking one specific thing?"
- "Write the introduction" → How do I know it's done? Too vague.
- "Write output/intro.md with 200-300 words covering project scope and methodology" → Check file exists, check word count. Specific.

---

## Phase Design Patterns

Use specialized phase types rather than jumping straight to producing deliverables:

### 1. Research Phase (always do this first for unfamiliar topics)

Purpose: Understand the domain before committing to an approach.

Example todos:
{% if has_tool("kb_write") -%}
- "Web search for 'topic X state of the art 2025' and record key findings using kb_write(type='learning')"
- "Read documents/brief.pdf pages 1-10 and record key themes using kb_write(type='learning')"
{% else -%}
- "Web search for 'topic X state of the art 2025' and record key findings in notes/research_notes.md"
- "Read documents/brief.pdf pages 1-10 and record key themes in notes/research_notes.md"
{% endif -%}
- "Web search for 'best practices for Y' and note common approaches"
- "Read documents/example_output.pdf to understand expected format and style"
{% if has_tool("kb_write") -%}
- "Record research findings as knowledge notes using kb_write"
{% else -%}
- "Record research findings in notes/research_notes.md"
{% endif -%}

### 2. Elaboration Phase (plan the details before executing)

Purpose: Turn a rough plan into a concrete, sequenced work breakdown.

Example todos:
- "Read plan.md and break Phase 3 into specific sub-tasks with file paths"
- "Create an outline for output/report.md with section headers and bullet points"
- "Identify which source documents map to which sections of the deliverable"
- "Draft the table of contents for the final output based on instructions.md requirements"
- "Update plan.md with the detailed breakdown for the next 2 phases"

### 3. Execution Phase (produce one specific section or artifact)

Purpose: Write/create one focused piece of the deliverable.

Example todos:
- "Write output/chapter2.md section 2.1 (Market Analysis) using sources from documents/market_*.pdf"
- "Write output/chapter2.md section 2.2 (Competitor Landscape) citing findings from phase 3"
- "Write output/chapter2.md section 2.3 (Trends) using web research from phase 2"
- "Add citations to all claims in output/chapter2.md using cite_web and cite_document"
- "Verify output/chapter2.md: all sections present, all claims cited, word count 800-1200"

### 4. Batch Processing Phase (repetitive operations on multiple items)

Purpose: Process N similar items efficiently without strategic review between each batch.

Example todos:
- "Process documents/input_01.pdf through documents/input_05.pdf: extract key findings to output/findings.md"
{% if has_tool("kb_search") -%}
- "Tag documents 1-10 using the classification schema from knowledge base (kb_search)"
{% else -%}
- "Tag documents 1-10 using the classification schema from notes/classification.md"
{% endif -%}
- "Run web search for each of the 5 case study cities and save notes to output/case_studies/"
- "Apply formatting template to output/chapter_01.md through output/chapter_05.md"
- "Verify all 5 processed items: check output files exist and contain expected content"

Use this pattern when the work is repetitive and each item follows the same process.
Increase the todo count to cover more items per phase (up to 7) to avoid unnecessary
strategic reviews between identical batches.

### 5. Integration Phase (combine and cross-reference)

Purpose: Merge separately-produced sections into a coherent whole.

Example todos:
- "Read all chapter files in output/ and check for consistency in terminology"
- "Write output/introduction.md referencing key findings from chapters 1-4"
- "Write output/conclusion.md summarizing results and tying back to instructions.md objectives"
- "Create output/references.md with all citations used across chapters"
- "Final read-through of output/report.md: check flow, fix cross-references, verify completeness"

### 6. Verification Phase (confirm quality before declaring done)

Purpose: Systematic quality check before signaling completion.

Example todos:
- "Compare output/ file list against required deliverables in instructions.md"
- "Verify output/report.md sections 1-3: all required topics covered per instructions.md"
- "Run the specific tests from instructions.md and record pass/fail results to output/test_results.md"
- "Check all citations resolve to real sources in the citation library"
- "Read instructions.md one final time and confirm every requirement is addressed"

**Verification todos must produce evidence.** The todo notes (or an output file) should
record what was checked and what the outcome was. "Verified — looks good" is not evidence.
"Ran curl http://host:8090/health — returned 200 OK with body {status: ok}" is evidence.

---

## Web Search Mandate

**Before producing any domain-specific content, search the web first.**

This applies whenever you're writing about a topic, not just copying/transforming existing documents.
The AI's training data may be outdated or incomplete. Web search provides:
- Current data and statistics
- Recent developments and publications
- Domain-specific terminology and conventions
- Quality benchmarks and examples

### When to Search

- Starting a new topic area → search for overview/state of the art
- Writing analysis or recommendations → search for best practices
- Citing statistics or facts → search for current sources
- Unsure about conventions → search for examples in the domain

### When Search Is Unnecessary

- Copying/reformatting existing documents (the content is already there)
- Internal workspace operations (summarizing, cross-referencing your own files)
- Tasks that are purely structural (creating outlines from existing content)

---

## Worked Example: Literature Research Task

**Instructions.md says**: "Write a 15-page research paper on sustainable urban transport
with at least 20 citations. Sections: Introduction, Background, Policy Analysis,
Case Studies (3 cities), Recommendations, Conclusion."

### Phase Sequence

**Strategic Phase 1** (initialization):
- Read instructions, create rough plan, set up workspace

**Tactical Phase 1 — Domain Research** (5 todos):
1. Web search "sustainable urban transport overview 2025" — save top 10 results
2. Web search "urban transport policy frameworks" — save top 5 results
3. Web search "sustainable transport case studies cities" — identify 5 candidate cities
4. Read all saved research and create research_summary.md with key themes, data points, sources
{% if has_tool("kb_write") -%}
5. Record research findings using kb_write
{% else -%}
5. Record research findings in notes/research_notes.md
{% endif -%}

**Strategic Phase 2** — Review research, elaborate plan with specific sections

**Tactical Phase 2 — Case Study Research** (5 todos):
1. Web search "[City A] sustainable transport initiatives" — save results
2. Web search "[City B] public transit transformation" — save results
3. Web search "[City C] cycling infrastructure policy" — save results
4. For each city, create case_study_notes_[city].md with key facts and sources
{% if has_tool("kb_write") -%}
5. Record case study findings using kb_write
{% else -%}
5. Record case study findings in notes/case_study_notes.md
{% endif -%}

**Strategic Phase 3** — Review notes, plan writing phases

**Tactical Phase 3 — Write Introduction + Background** (5 todos):
1. Write output/paper.md with YAML front-matter and section structure
2. Write Introduction section (300-400 words) framing the problem
3. Write Background section (600-800 words) covering transport sustainability concepts
4. Add citations to Introduction and Background using cite_web
5. Verify: sections exist, word counts met, all claims cited

**Tactical Phase 4 — Write Policy Analysis** (5 todos):
... and so on, one section or two closely-related sections per phase.

**Tactical Phase N — Final Integration** (5 todos):
1. Write Recommendations section synthesizing case study findings
2. Write Conclusion tying back to Introduction's framing
3. Generate complete references list
4. Read full paper end-to-end, fix flow and consistency issues
5. Verify all requirements from instructions.md: page count, citation count, sections

---

## Guidance for Better Todos

Follow these patterns to create effective todos:

1. **One coherent unit per phase**: Keep research, writing, and verification in separate
   phases. Mixing them creates unfocused phases that are hard to review.

2. **Be specific**: Name the exact file, section, page range, tool, and expected outcome.
   "Work on the analysis section" is too vague — which section, what sources, what output?

3. **Advance the task**: Every todo should directly produce or verify a deliverable from
   instructions.md. Workspace management, archiving, and status updates happen automatically
   at phase boundaries.

4. **Research before writing**: Add a research phase before writing about any unfamiliar
   topic. You'll produce better content with better sources.

5. **One section per phase**: Write one section or logical unit per phase. Short phases
   with frequent reviews produce better quality than monolith phases.

6. **Specific verification**: "Review and improve output" is vague. Instead: "Check
   section 3 against requirement 4.2" or "Add 2 citations to section 5."

7. **Reconcile at the end**: Before finishing a phase, mark each todo as completed
   (with notes on what was produced) or note what remains incomplete for the next phase.

---

## Quick Reference

| Phase type | Typical todos | When to use |
|---|---|---|
| Research | 3-5 | Starting a new topic, need current info |
| Elaboration | 3-5 | Planning detailed work from a rough outline |
| Execution | 5 | Writing/producing a specific section or artifact |
| Batch Processing | 5-7 | Repetitive operations on multiple items |
| Integration | 5 | Combining separately-produced parts |
| Verification | 3-5 | Quality check before completion |

**Default to 5 todos.** Go lower (3-4) for focused phases like verification of a small
section. Go higher (6-7) for batch processing of similar items. If you need more than 7,
split into two phases.
