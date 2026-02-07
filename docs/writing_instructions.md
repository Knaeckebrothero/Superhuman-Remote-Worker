# Agent Instruction Design: Research Findings & Design Rationale

This document captures research and reasoning behind the agent instruction design. It covers
two areas: the "structure first, then prose" writing methodology (and citation discipline),
and broader prompt architecture insights for the phase-alternating agent system.

For detailed prompt optimization analysis specific to reasoning models (gpt-oss-120b), see
the companion report: `docs/Agent Prompt Optimization Report.pdf`.

## The Problem

LLMs writing source-based texts tend to fail in predictable ways:

- **Citation fabrication**: A Deakin University study found that when GPT-4o wrote literature reviews,
  19.9% of citations were completely fabricated (nonexistent papers), and 45.4% of real citations
  contained errors (wrong dates, page numbers, DOIs). Combined: **56% of all citations were
  unreliable.** A University of Washington study found even higher fabrication rates of 78-90%
  without retrieval augmentation.

- **Plausible-sounding fakes**: Fabricated citations often include DOIs that resolve to real but
  unrelated papers, making errors deceptive and hard to catch.

- **Topic-dependent accuracy**: Less-studied topics have dramatically higher fabrication rates
  (6% for well-studied depression vs. 28-29% for less-studied disorders).

- **Attribution drift**: Even with real sources, LLMs may attribute claims to the wrong source
  or misrepresent what a source actually says.

- **The "write first, cite later" trap**: When an LLM writes prose first and then looks for
  citations to support pre-written claims, it cherry-picks sources (whether good or bad) to
  fit the existing text, introducing confirmation bias.

These aren't just prompt engineering problems — research (FACTUM, arXiv:2601.05866) identifies
citation hallucination as a structural limitation: a "scale-dependent coordination failure"
between attention and feed-forward network pathways inside transformer models.

## The Solution: Structure First, Then Prose

The core methodology is borrowed from how experienced academic writers work:

1. Read sources and extract key passages with references
2. Build a bullet-point outline where each point has its evidence attached
3. Create citations during the outline phase
4. Convert the outline to prose section by section

This approach is supported by several established academic writing frameworks.

### Annotated Outline Method

An annotated outline organizes main ideas around the thesis and maps supporting evidence to
each section *before* writing prose. Key principles:

- Organize around **arguments and themes**, not around individual sources
- For each section, identify 2-3 supporting sources with specific page/passage references
- This reveals gaps in argumentation or missing evidence before drafting begins

The benefit: the evidence-to-claim mapping is established before any prose is written, so the
writer never has to hunt for citations to fit pre-written text.

### Synthesis Matrix

A table-based planning tool for multi-source writing:

| Theme / Concept | Source A | Source B | Source C |
|-----------------|----------|----------|----------|
| Compliance requirements | p.12: "immutable audit trails" | Section 2.1: overview | — |
| Data retention | p.15: retention table | — | Art. 17: right to erasure |
| Implementation approach | p.23-24: architecture | Section 3.4: failure rates | — |

This reveals patterns (agreement), contradictions (disagreement), and gaps (missing coverage).
Particularly valuable for literature reviews and multi-source arguments.

### "Cite as You Write" vs. "Write First, Cite Later"

Research and practitioner experience consistently favor citing during planning:

**Cite as you write (recommended):**
- The argument is structurally grounded in evidence from the start
- You finish writing with all citations already placed
- It becomes habit-forming — you develop the reflex of always grounding claims
- Slower initial writing, but more reliable output

**Write first, cite later (risky for AI):**
- More exploratory and creative, appropriate when you don't know what you're doing
- Can produce more engaging prose because it isn't constrained by source placement
- But leads to cherry-picking citations to fit pre-written claims — exactly the failure
  mode that LLMs already exhibit when fabricating sources
- Requires extensive re-reading and editing to trim exploratory branches

For an AI writing agent where accuracy and traceability are paramount, the cite-as-you-write
approach is strongly preferable.

## Key Writing Frameworks Used

### Claim-Evidence-Reasoning (CER)

Every paragraph that includes source material follows this structure:

1. **Claim** — A debatable statement in the writer's own words
2. **Evidence** — A quote, statistic, or sourced data point, introduced with a signal phrase
3. **Reasoning** — 1-2 sentences explaining *why and how* the evidence supports the claim

This ensures the writer's voice dominates while sources serve as supporting evidence.

### Quote Sandwich (ICE Method)

A paragraph-level framework for integrating sources:

1. **Introduce** — Use a signal phrase to introduce the source and its authority
2. **Cite** — Present the quote or paraphrase with proper citation
3. **Explain** — Analyze how the evidence connects to the argument

Rule of thumb: borrowed material should be at most 1/3 of the work; the remaining 2/3 should
be the writer's own analysis and synthesis.

### Signal Phrases

Signal phrases mark the boundary between the writer's words and the source's words. Verb
choice matters and should reflect the source's rhetorical stance:

| Verb | Implies |
|------|---------|
| argues | The source is making a debatable claim |
| demonstrates | The source provides convincing proof |
| notes / observes | Neutral reporting |
| suggests | Tentative or qualified claim |
| claims / contends | The writer may not fully agree |
| reports | Factual reporting without interpretation |
| establishes | The source provides definitive evidence |

### Reverse Outline (Post-Draft Verification)

After writing, create a reverse outline by summarizing each paragraph's main idea, then verify:

1. Does each paragraph have a clear claim?
2. Is the claim supported by cited evidence?
3. Is there analysis connecting evidence to claim?
4. Is the citation traceable to a real, provided source?

A color-coding mental model helps: claims in one color, evidence in another, analysis in a third.
This reveals paragraphs that are "all claim, no evidence" or "all evidence, no analysis."

## Citation Discipline Rules

### For AI Writing Agents Specifically

These rules address the known failure modes of LLM-generated citations:

1. **Never cite from memory.** Every citation must trace to a specific passage in a provided
   source or a retrieved web page. No exceptions.

2. **Cite during planning, not after.** Create citations while building the annotated outline.
   This prevents the "write then find citations to fit" failure mode.

3. **Use exact quotes for verification.** The citation tool receives the actual text from the
   source for verification. Paraphrasing happens in prose, not in the citation call.

4. **Never fabricate bibliographic details.** Don't invent author names, dates, journal titles,
   or DOIs. Use available metadata and flag gaps.

5. **Categorize every claim.** Before writing, classify each planned claim as:
   - **(a) Directly stated in a source** → gets a direct citation
   - **(b) Inferred from combining multiple sources** → gets multiple citations
   - **(c) Writer's own analysis/synthesis** → no citation, framed as analysis

6. **Restrict to provided sources.** Only cite sources that have been provided through document
   upload or retrieved via web search. Never cite from training data.

7. **Self-verify after writing.** For each claim in the final text, confirm a direct quote or
   passage from the sources supports it. If not, retract or flag as unsupported.

### Anthropic's Recommendations for Reducing Hallucinations

From Anthropic's official documentation on grounding LLM outputs:

- Allow the model to say "I don't know" — explicitly permit admitting uncertainty
- Use direct quotes for factual grounding — extract word-for-word quotes first, then analyze
- Verify with citations post-generation — "For each claim, find a direct quote that supports it.
  If you can't, remove that claim."
- Chain-of-thought verification — explain reasoning step-by-step before final answers
- External knowledge restriction — only use information from provided documents

## Implementation in the Writer Agent

The research findings map to the writer agent's phase progression:

| Phase | Activity | Framework Applied |
|-------|----------|-------------------|
| Strategic 1 | Read sources, build evidence map | Synthesis matrix concept |
| Tactical 1 | Build annotated outline with citations | Annotated outline method, cite-as-you-write |
| Strategic 2 | Review outline completeness | Gap analysis, ratio check (2:1 analysis to evidence) |
| Tactical 2 | Convert outline to prose | CER pattern, signal phrases, quote sandwich |
| Strategic 3 | Review and verify | Reverse outline, citation audit |

The annotated outline (`output/outline.md`) serves as the bridge between evidence gathering
and prose writing. It is preserved as a reference artifact, providing an auditable chain from
every claim in the final text back to its source.

## Prompt Architecture Insights

The following findings come from research into prompt design for autonomous agents operating
within the phase-alternating architecture. While some findings are specific to reasoning models
(gpt-oss-120b with high reasoning effort), most apply broadly to any LLM-based agent.

Full analysis: `docs/Agent Prompt Optimization Report.pdf`

### System Prompt Design

**Functional specification over persona narrative.** System prompts should define what the agent
*does*, not who it *is*. Motivational language ("you are one of the most sophisticated AI
systems") wastes tokens on every turn without improving performance. Instead, the system prompt
should specify: identity, memory model, instruction hierarchy, and behavioral guardrails.

**Instruction hierarchy.** The system prompt should explicitly declare a priority order for
potentially conflicting instructions:

1. **System Prompt** — Immutable core rules, overrides everything
2. **Phase Directive** — Current phase constraints (strategic vs. tactical)
3. **Workspace Injection** — workspace.md as the current state of truth
4. **User/Tool Messages** — Dynamic context from tool results and user input

This prevents "instruction drift" where directives in file-based instructions (instructions.md)
or user messages override system-level rules. It also provides defense against prompt injection
from document content or tool results.

**Container pattern.** Research on prompt injection defense and instruction fidelity favors a
hybrid structure: **Markdown for static instructions** (identity, rules, definitions) and
**XML tags for dynamic data** (`<phase_directive>`, `<current_todos>`, `<memory_bank>`). XML
tags create a hard boundary between system instructions and injected content, preventing the
model from treating external data as instructions.

### Meta-Cognitive Guardrails

Three guardrails that improve agent reliability:

1. **No Manual Chain-of-Thought.** For reasoning models (o-series, DeepSeek R1, gpt-oss-120b),
   explicit "think step by step" instructions are redundant — the model already performs
   internal reasoning via its latent thought trace. Adding manual CoT instructions acts as
   noise, consuming tokens and potentially confusing the model's internal scheduler.

2. **Hallucination Check.** Before using a file path, variable, or entity ID in a tool call,
   the agent should verify it exists in workspace.md or recent tool outputs. This prevents
   "Internal Simulation Hallucination" — where the model *thinks* about using a tool and
   conflates the plan with actual execution.

3. **Overflow Protection.** When context approaches the limit, the agent should prioritize
   consolidating volatile state into workspace.md before attempting further work. The directive:
   "Context Critical. Consolidate all volatile state into workspace.md immediately."

### Phase-Specific Prompting

**Strategic phase = Architect.** The strategic prompt should enforce the agent's role as a
planner, not an executor. Key constraints:

- **No execution**: Ban write_file, run_command, and other execution tools. Only allow
  information-gathering tools (read_file, list_files, git tools) and state management
  (next_phase_todos).
- **Output constraint**: The only valid exit is a structured `next_phase_todos` call. This
  forces the agent to crystallize its reasoning into a discrete, machine-readable artifact.
- **Review-Reflect-Adapt workflow**: Review via git diff → Reflect by updating workspace.md →
  Adapt plan.md → Plan the next set of todos.

**Tactical phase = Engineer with tunnel vision.** The tactical prompt should enforce focused
execution without replanning:

- **Tunnel vision**: "Execute the items in the todo list sequentially. Do not deviate. Do not
  replan. If a task is impossible, mark it as blocked and proceed or terminate the phase."
- **Atomicity**: Commit work frequently. Each todo completion is a checkpoint.
- **Read→Execute→Verify→Mark workflow**: Select the highest-priority pending task, perform it
  with tools, verify the result (syntax check, test, re-read), then mark complete.
- **Stop if the plan requires change**: If execution reveals that the plan is wrong, don't
  fix it in tactical mode — finish or block the phase and let strategic mode handle it.

**Preventing mode bleeding.** The success of the phase alternation model depends on strict
separation. "Mode bleeding" occurs when strategic planning leaks into tactical execution
(over-thinking simple tasks) or tactical execution leaks into strategic planning (making
changes during review). The prompts must reinforce this boundary explicitly.

### Memory Architecture

**The "Read-Once" Problem.** The agent reads instructions.md (expert instructions) via a tool
call during its first turn. After context compaction, this content — which appeared as a tool
result — is lost or heavily summarized. The agent effectively "forgets" its operating manual.

**Memory Pinning solution.** The first strategic phase should include a mandatory extraction
step: read instructions.md and write the core constraints, style guidelines, and output format
rules into workspace.md under a `## Pinned Instructions` section. Since workspace.md is
injected every turn (not stored in conversation history), these instructions survive infinite
context compaction cycles. This migrates critical instructions from *transient context* (tool
output) to *persistent context* (workspace injection).

**workspace.md as Single Source of Truth.** The prompt should instruct the agent: "If a fact
is not in workspace.md, it does not exist." This forces the agent to treat memory maintenance
(updating workspace.md) as a primary action rather than an afterthought. Important because
the model's internal thought trace is not visible in the context window for reasoning models —
workspace.md becomes the only persistence mechanism for reasoning artifacts.

### Context Compaction

**Structured over narrative summarization.** When compacting conversation history, structured
JSON summaries preserve more actionable information than narrative prose:

- **Narrative weakness**: "The user asked for a web app, and the agent started coding."
  (Loses file paths, library versions, error codes.)
- **Structured strength**: JSON forces preservation of specific entities.

Recommended fields: `summary`, `key_decisions`, `tasks_completed`, `current_blockers`,
`state_changes` (files created/modified), `pinned_instructions` (rules extracted from files).

**Don't summarize persistent files.** Since workspace.md and plan.md are re-read every turn,
summarizing their *content* is wasteful. The compaction prompt should only summarize *actions
taken on them* (e.g., "Updated workspace.md with new entity IDs"), not reproduce their content.

### Transition Handoffs

The transition between phases is the highest-risk point for context loss ("Context Rot"):

- **Strategic → Tactical**: The summary should be forward-looking: "Plan updated. Objectives
  for this run: [List]. Context refreshed."
- **Tactical → Strategic**: The summary should be backward-looking: "Execution finished.
  Results: [Artifacts produced]. Returning to Strategic for review."

### Configuration Recommendations

These recommendations are primarily for gpt-oss-120b but provide useful baselines:

| Parameter | Recommendation | Rationale |
|-----------|----------------|-----------|
| Reasoning Effort | High (Strategic) / Medium (Tactical) | Reduce latency and over-thinking on simple tactical tasks. Keep high for strategic planning. |
| Temperature | 0.0 | Essential for deterministic code generation and strict adherence to YAML todo schema. |
| Context Limit | 128k (soft limit: 100k) | Reserve 20k tokens for reasoning trace and output. Trigger compaction at 100k to prevent "Lost in the Middle" degradation. |
| Timeout | 600s | Reasoning traces can be long. Do not lower or the agent may be cut off mid-thought. |

## Sources

- [Scribbr - Integrating Sources](https://www.scribbr.com/working-with-sources/integrating-sources/)
- [Scribbr - Signal Phrases](https://www.scribbr.com/working-with-sources/signal-phrases/)
- [Coates Library, Trinity University - Integrating Sources](https://lib.trinity.edu/integrating-sources-in-the-text-of-your-paper/)
- [Purdue OWL - Signal and Lead-in Phrases](https://owl.purdue.edu/owl/research_and_citation/using_research/quoting_paraphrasing_and_summarizing/signal_and_lead_in_phrases.html)
- [Writing Codidact - Write and Cite vs Write First](https://writing.codidact.com/posts/34762/34768)
- [Scribbr - Research Paper Outline](https://www.scribbr.com/research-paper/outline/)
- [UAGC Writing Center - Synthesis Matrix](https://writingcenter.uagc.edu/synthesis-matrix)
- [Purdue OWL - Reverse Outlining](https://owl.purdue.edu/owl/general_writing/the_writing_process/reverse_outlining.html)
- [Writing Mindset - CER Paragraph](https://www.writingmindset.org/blog/claim-evidence-reason-paragraph)
- [Piedmont Virginia Community College - Quote Sandwich](https://libguides.pvcc.edu/quote-sandwich)
- [Berkeley Student Learning Center - Sandwiching](https://slc.berkeley.edu/writing-worksheets-and-other-writing-resources/sandwiching-three-steps-delicious-argument)
- [UNC Writing Center - Evidence](https://writingcenter.unc.edu/tips-and-tools/evidence/)
- [StudyFinds - ChatGPT Hallucination Problem](https://studyfinds.org/chatgpts-hallucination-problem-fabricated-references/)
- [PsyPost - AI-Generated Citations Study](https://www.psypost.org/study-finds-nearly-two-thirds-of-ai-generated-citations-are-fabricated-or-contain-errors/)
- [Nature - OpenScholar](https://www.nature.com/articles/d41586-026-00347-9)
- [UW News - OpenScholar](https://www.washington.edu/news/2026/02/04/in-a-study-ai-model-openscholar-synthesizes-scientific-research-and-cites-sources-as-accurately-as-human-experts/)
- [Stanford Legal RAG Hallucinations Study](https://dho.stanford.edu/wp-content/uploads/Legal_RAG_Hallucinations.pdf)
- [FACTUM - Citation Hallucination Detection (arXiv:2601.05866)](https://arxiv.org/abs/2601.05866)
- [Anthropic - Reduce Hallucinations](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)
- [Agent Prompt Optimization Report](docs/Agent%20Prompt%20Optimization%20Report.pdf) — Internal report on optimizing phase-alternating agent prompts for gpt-oss-120b (42 references)
