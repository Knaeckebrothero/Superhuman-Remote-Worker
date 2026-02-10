# Todo Crafting Guide

**You MUST read this file before calling `next_phase_todos`.** The tool will reject
your call if you haven't. This guide teaches you how to create effective, focused todos.

---

## Core Principle: Short Phases, Tight Focus

**Target: 5 todos per tactical phase.** Not 10, not 15 — five.

Why? Each tactical phase ends with a strategic review. More frequent reviews mean:
- Earlier detection of wrong directions
- Better-adapted plans based on what you actually learned
- Less wasted work if priorities shift

A phase should represent one coherent unit of work — "research the topic," "write section 3,"
"verify all citations in chapter 2" — not an entire project stage.

---

## Todo Specificity Rules

Every todo must be specific enough that you know *exactly* when it's done.

### Bad → Good Examples

| Bad (vague) | Good (specific) |
|---|---|
| "Check all citations" | "Verify citations 1-10 against source documents in documents/" |
| "Write the analysis" | "Write section 2.1: Market Overview using findings from phase 2 research" |
| "Research the topic" | "Web search for 'renewable energy policy EU 2025', summarize top 5 results to workspace.md" |
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

Don't jump straight to producing deliverables. Use specialized phase types:

### 1. Research Phase (always do this first for unfamiliar topics)

Purpose: Understand the domain before committing to an approach.

Example todos:
- "Web search for 'topic X state of the art 2025' and save top 5 results to workspace.md"
- "Read documents/brief.pdf pages 1-10 and list key themes in workspace.md"
- "Web search for 'best practices for Y' and note common approaches"
- "Read documents/example_output.pdf to understand expected format and style"
- "Summarize research findings in workspace.md under '## Research Summary'"

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

### 4. Integration Phase (combine and cross-reference)

Purpose: Merge separately-produced sections into a coherent whole.

Example todos:
- "Read all chapter files in output/ and check for consistency in terminology"
- "Write output/introduction.md referencing key findings from chapters 1-4"
- "Write output/conclusion.md summarizing results and tying back to instructions.md objectives"
- "Create output/references.md with all citations used across chapters"
- "Final read-through of output/report.md: check flow, fix cross-references, verify completeness"

### 5. Verification Phase (confirm quality before declaring done)

Purpose: Systematic quality check before signaling completion.

Example todos:
- "Compare output/ file list against required deliverables in instructions.md"
- "Verify output/report.md sections 1-3: all required topics covered per instructions.md"
- "Verify output/report.md sections 4-6: all required topics covered per instructions.md"
- "Check all citations resolve to real sources in the citation library"
- "Read instructions.md one final time and confirm every requirement is addressed"

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

### When NOT to Search

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
5. Update workspace.md with research findings and candidate cities for case studies

**Strategic Phase 2** — Review research, elaborate plan with specific sections

**Tactical Phase 2 — Case Study Research** (5 todos):
1. Web search "[City A] sustainable transport initiatives" — save results
2. Web search "[City B] public transit transformation" — save results
3. Web search "[City C] cycling infrastructure policy" — save results
4. For each city, create case_study_notes_[city].md with key facts and sources
5. Update workspace.md with case study status

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

## Common Anti-Patterns

**Avoid these when creating todos:**

1. **The kitchen-sink phase**: 15 todos covering research, writing, AND verification.
   → Split into 3 phases of 5 todos each.

2. **The vague todo**: "Work on the analysis section."
   → Which section? What sources? What's the expected output?

3. **The meta-todo**: "Update workspace.md with progress," "Commit current work."
   → The system handles this at phase boundaries. Don't waste todos on bookkeeping.

4. **The premature execution**: Jumping to "Write chapter 1" before researching.
   → Add a research phase first. You'll write better content with better sources.

5. **The monolith phase**: One phase to write an entire document.
   → One phase per section or logical unit. Short phases, frequent reviews.

6. **The perfectionist loop**: "Review and improve output" as a recurring todo.
   → Be specific: "Check section 3 against requirement 4.2" or "Add 2 citations to section 5."

---

## Quick Reference

| Phase type | Typical todos | When to use |
|---|---|---|
| Research | 5 | Starting a new topic, need current info |
| Elaboration | 3-5 | Planning detailed work from a rough outline |
| Execution | 5 | Writing/producing a specific section or artifact |
| Integration | 5 | Combining separately-produced parts |
| Verification | 3-5 | Quality check before completion |

**Default to 5 todos.** Only go lower (3-4) for very focused phases like verification
of a small section. Never exceed 10 — if you think you need more, split into two phases.
