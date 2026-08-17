# `project-onboarding` — Research Synthesis (evidence base for the SKILL.md)

Synthesis of three inline research passes (vendor/agent practice · the empirical
canon + termination · the transfer question + honesty) run 2026-06-25 for the
`project-onboarding` skill. Reframed from the roster's Tier-2 `codebase-onboarding`
entry: SRW's unit of work is a **project** — a *bundle* of datasources (cloud
mounts / Obsidian vaults / shared folders of docs+decks+protocols, SQL/graph/Mongo
databases, *and* optionally a code repo) plus an accumulated knowledge base. Code
is one branch, not the skeleton.

FACT = sourced/quoted in a memo · RECOMMENDATION = synthesis for authoring.

---

## Key numbers (load-bearing)

| Figure | Value | Source |
|---|---|---|
| Dev time spent on **program comprehension** | **~58%** | Xia et al., *IEEE TSE* 2018 (78 pros, 3,148 hrs) |
| Comprehension time (prior IDE-only estimate) | ~70% | Minelli/Mocci/Lanza 2015 |
| Reviewers: **unfamiliar** files take longer | **91%** (798/873) | Bacchelli & Bird, ICSE 2013 |
| MAST **System Design / spec** category (FC1) | **41.8%** (largest of 3) | Cemri et al., MAST, arXiv 2503.13657 |
| MAST FM-1.1 **Disobey Task Specification** | 11.8% | same |
| MAST FM-1.3 **Step Repetition** | 15.7% | same |
| MAST FM-1.5 **Unaware of Termination Conditions** | 12.4% | same |
| MAST — fix spec/role design alone | **+9.4%** task success | same (intervention) |
| MAST — add objective verification | **+15.6%** task success | same (intervention) |
| Mechanical citation-verification → hallucination prevented | **100%** of fabricated-file cases | arXiv 2512.12117 |

The two named termination/spec failure modes (FM-1.1 + FM-1.5) are ~24% of all
MAS failures; with Step Repetition (FM-1.3) the "act-blind / repeat-work /
don't-stop" trio is **≈40%** — and it sits inside the single largest failure
category. **This is the quantitative case for the skill: the dominant agent
failure cluster is exactly the one orientation addresses.**

---

## A. Executive synthesis

An agent dropped into an unfamiliar project is in the measured worst case for
getting it wrong: comprehension already dominates skilled human effort (~58% of
dev time; "code review *is* understanding"), and the penalty for low context is
large and quantified (unfamiliar code is 91% slower to review; the largest agent
failure cluster, MAST FC1 at 41.8%, is spec/design issues rooted in acting
without a correct interpretation of the work). The cure the humans themselves
name is to **acquire context first** — and the agent literature confirms the
cheap up-front spend prevents the expensive flailing spend (Begel & Simon's new
hires "wasted" hours flailing before learning the code; MAST's +9.4%/+15.6%
interventions are exactly "interpret the spec / verify against the objective").

Across seven leading agent systems (Claude Code, the community `/onboard` skill,
obra/superpowers, Cline's Memory Bank, Aider's repo-map, Copilot's "explore a
codebase," Devin's auto-Knowledge) one **domain-general spine** recurs: *gate
(orient read-only before acting) → read the high-signal index docs first,
breadth before depth → build a compact structural map of the units and how they
reference each other → read the recent-change log to find what's active → persist
a structured orientation artifact → confirm understanding before acting.* What's
welded to code is only the *primitives* that execute each step (tree-sitter ASTs,
`import` edges, running the test suite); the **structure ports cleanly** to a
document corpus, a vault, or a database. Cline's six-file Memory Bank (purpose,
context, active focus, patterns, status) is the strongest single proof that the
*output* of orientation is domain-agnostic.

The transfer question — does this generalize beyond code? — comes back
**universal as a procedure, not merely a frame**. The same four-beat skeleton
(inventory → find the source of truth → learn structure/vocabulary → build a map)
is the *named, ordered* procedure in five independent non-code disciplines:
archival collection survey ("read existing documentation, then physically examine
and reconcile against it" — near-isomorphic, and predates software), Watkins'
*First 90 Days* ("accelerate your learning" + stakeholder map + the "action
imperative" anti-pattern), hypothesis-driven consulting onboarding (issue tree,
stop at ~80% certainty), journalism beat-onboarding (learn the jargon, build the
source list), and Klein's data-frame sensemaking (which explains *why* it
transfers — sensemaking is the same data↔frame loop in any medium). The honest
split: the *procedure's* universality rests on mature human-practice literature;
the *autonomous-agent* evidence is still almost all code. So we ship it universal
(the human evidence is overwhelming and medium-agnostic) and instrument the
honesty mechanisms (where agent-specific failure data exists) so the universal
claim is validated on non-code projects rather than assumed.

The skill's second half is **knowing when to stop** — the opposite failure
(explore forever, never start) is itself an FM-1.5-class failure (12.4%). Theory
supplies a stopping rule: Information Foraging's Marginal Value Theorem (leave the
patch when marginal return flattens), the sensemaking *schematize* step (stop when
you can write the model), and satisficing (set a *task-scoped* aspiration
threshold, accept "good enough"). The operational exit trigger: **you can write
the project map + a located plan.** Orient to *task-sufficiency, not
project-completeness.*

---

## B. The body procedure (the heart)

Six steps. The spine is domain-general; the per-datasource moves in step 3 are
branches, with code as one branch.

1. **Read what's already known — first.** The project knowledge base is the
   accumulated onboarding of every prior job; the prior `notes/`, `datasources.md`,
   and injected memories are too. Search it (`search_knowledge` /
   `get_knowledge_summary`) before re-deriving anything. Reuse-before-rediscover
   (defeats the "Sisyphus Trap" / MAST Step Repetition) — *but gate on freshness*
   (step 3): a stale prior note loses to the live source.
   - *code:* the prior architecture note · *research:* the earlier literature
     synthesis · *docs:* the project's index/MOC note · *data:* the saved data
     dictionary.

2. **Inventory the datasources.** What's actually attached, and what kind of
   project does that make this — a file corpus? a database? a repo? a mix? Don't
   assume it's a repo. (`datasources.md` / the project's datasource list.)

3. **Find each source's source of truth + conventions — and confirm it's the
   real, current one.** Per type:
   - **Files / vault** → the index / README / MOC; naming + folder conventions.
   - **Database** → the schema and table relationships (`get_table_schema` /
     `list_tables`), the data dictionary (it's the README of a DB); sample a few
     rows to confirm columns mean what they claim.
   - **Repo** → README + entry point + module map; **run the tests** to confirm
     your model is right.
   - **Provenance/freshness check (all types):** a vendored copy or snapshot in a
     `documents/` folder may *differ* from the live source. Confirm which you
     actually have before trusting it — semantic relevance is blind to staleness,
     and the live source wins a disagreement. *(This is the `Code_Repository`
     snapshot trap.)*

4. **Learn the vocabulary and what "done" looks like.** The domain terms, the
   stakeholders, the actual target of *this* task. ("Obscure terms aren't
   understood until after you've worked the material" — so capture them as you go.)

5. **Don't assert what you didn't open.** Every claim about where something lives
   cites a path/handle you actually read this session, cross-checked against the
   project's own index. A label is a hypothesis until you open the container.
   Mark each map entry **confirmed** (opened it) or **assumed** (inferred).

6. **Write the map back, then stop.** Record a compact project map (`kb_write` /
   a `notes/` file) so the next job inherits it — closing the loop with step 1.
   Orientation is **strategic** work: its output feeds the first todo list. Stop
   when you can write the map *and* a located plan — not when you've read
   everything. Orient to the task, not the whole project.

---

## C. Scaffold — the project map (embed in the body)

A fixed-schema artifact the agent fills in and writes back to the KB:

```
# Project map — <project>
- Datasources:   <what's attached, by type>
- Source of truth: <the canonical, current source per area — confirmed>
- Structure:     <the units + how they reference each other>
- Conventions:   <naming, layout, house style, where decisions live>
- Vocabulary:    <domain terms / acronyms / key entities>
- Open questions: <what's still unknown>
- Confirmed vs assumed: <which map entries you opened vs. inferred>
```

(Mirrors Cline's Memory Bank + the `/onboard` artifact — domain-agnostic fields,
cache-friendly. A long per-datasource-type checklist, if it grows, belongs in an
L3 `references/` file, not the body.)

---

## D. Quality bar

- **GOOD:** an actionable map — datasources inventoried, the *right, current*
  source of truth confirmed by opening it, conventions + vocabulary captured,
  prior work reused, written back — produced in bounded time and ending in a
  located plan.
- **SHORTCUT (under-orientation):** acted on a guessed structure never opened →
  the confidently-wrong action (MAST FM-1.1).
- **RUNAWAY (over-orientation):** read everything, mapped the whole project, never
  started (MAST FM-1.5). Both fail.

---

## E. Anti-patterns (→ instead)

- **Assume it's a repo** → inventory the datasources first; code may be one part
  next to a vault and a database.
- **Act on a structure you never opened** → verify by opening; cite the path,
  cross-check the index.
- **Trust a snapshot as the source of truth** → provenance/freshness-check; the
  live source wins.
- **Re-derive what the KB already holds** → read prior notes/KB first; reuse
  before rediscover.
- **Orient forever** → stop when you can write the map + a located plan; orient to
  task-sufficiency.
- **Map the whole project for a corner-sized task** → scope orientation to what
  the task touches.

---

## F. Enforcement & scope recommendation

**Ship model-invoked (universal), one body — same as brainstorming /
systematic-debugging / code-review.** The transfer verdict (universal-as-a-
procedure) supports a single body for all experts: scholar onboards to a corpus,
developer to a repo, assistant to a folder, analyst to a dataset. No single expert
owns it (unlike code-review→critic), so there's no natural binding home.

A `phase:strategic` "orient at the start of a fresh project" binding is tempting —
orientation *is* strategic and precedes the first todo list — but rejected for
now: it would fire on **every** job's strategic phase, including familiar projects
and trivial tasks, and SRW already runs a strategic planning phase + the enforced
`todo-guide` gate there. Force-injecting orientation universally would be noise on
the majority of jobs that don't need it. Model-invoked lets the agent load it
exactly when the project is unfamiliar — the right trigger. (If a dedicated
"scout"/onboarding expert is ever added, revisit a bound variant there.)

Consistent with the roster's "few well-specified > many," and flag-gated like the
other model-invoked skills (`SKILLS_DB_ENABLED`, dev-on / prod-off).

---

## G. Trigger-description draft

**Chosen:** "Use when you're dropped into an unfamiliar project and need to get
your bearings before acting — a code repo, a document corpus or Obsidian vault, a
shared folder of decks and protocols, a database, or a mix. Inventory the
datasources, find and confirm the real source of truth, learn the structure,
conventions, and vocabulary, reuse what prior work already mapped, and build a map
you can act from — then stop. For orienting in an existing body of work before you
start, not for checking your own finished work (that's verify-before-done) or
investigating a research question to produce findings (that's research-guide)."

Alternates:
- "Use at the start of work on an unfamiliar project — repo, document corpus,
  vault, shared folder, or database — to orient before acting: inventory what's
  there, confirm the source of truth, learn the structure and vocabulary, reuse
  prior mapping, and build a map you can plan from. Not self-checking finished
  work (verify-before-done) or answering a research question (research-guide)."
- "Use to get your bearings in an unfamiliar body of work before you start —
  inventory the datasources, find and verify the real source of truth, learn the
  conventions and vocabulary, and write a reusable map — terminating once you can
  plan. Distinct from verify-before-done and research-guide."

The description must trigger on "get up to speed on / orient in / explore this
project|repo|corpus|dataset" and explicitly name the two boundaries
(verify-before-done, research-guide) to avoid misfire.

---

## H. Model-variance note

One body, no per-family variants. The procedure is conceptual (inventory →
source-of-truth → structure/vocab → map → stop), not phrased against any
provider's tool-call dialect; SRW tool names referenced (`search_knowledge`,
`get_table_schema`, `kb_write`, `run_command`) are platform-level, not
family-level. RECOMMENDATION: ship single-body; revisit only if a weaker family
over-orients (fails to terminate) in practice.

---

## I. Real examples worth adapting

- **The community `/onboard` skill** (Claude Code): manifests/README first →
  entry points → representative sample → persisted, SHA-tagged
  `claude-onboard.md` with delta-refresh. The most directly relevant *procedure*;
  steal the persisted-artifact + cache idea. (tommcfarlin.com)
- **Cline Memory Bank**: six domain-agnostic files (projectbrief, productContext,
  activeContext, systemPatterns, techContext, progress), "read ALL at the start of
  EVERY task." Proof the orientation *output* is general; the scaffold's lineage.
- **Aider repo-map**: tree-sitter → def/ref graph → PageRank → token-budgeted map.
  The *idea* (rank units by reference-centrality, budget the map) generalizes;
  substitutes — vault wikilinks, DB foreign keys, doc cross-references.
- **Archival collection survey**: Phase 1 read existing documentation → Phase 2
  physically examine and *reconcile against it, noting whether labels are
  accurate*. The non-code proof of universality — and Phase 2 *is* the
  verify-by-opening honesty mechanism, stated by archivists a century before LLMs.

---

## J. Open questions / weak spots

- **Agent evidence is code-centric.** The procedure's universality is well-
  evidenced in human practice (archival/consulting/onboarding/journalism/
  sensemaking) but under-evidenced in *agent* practice on non-code projects. The
  cheap de-risk: run an SRW onboarding job on a pure document-corpus / database
  project and check the map quality. (Mirrors the roster's central unmeasured
  premise.)
- **The commit-early vs survey-first tunable.** Disciplines disagree: consulting
  commits to a hypothesis early; archival surveys first. The body leans
  survey-first (safer for an autonomous agent that can't easily un-commit), but
  this is a judgment call worth watching.
- **Termination calibration.** "Stop when you can write the map + a plan" is the
  right *rule*; whether weaker model families actually stop (vs. over-orient or
  under-orient) is unmeasured.
- **Overlap with research-guide.** Onboarding (orient to an existing body of work)
  vs. research-guide (investigate a question to produce findings) are adjacent;
  the description must hold the boundary. Watch for trigger collisions in the
  catalog.

---

## K. Sources (one-line notes)

**Agent practice (memo 1)**
- Claude Code best practices (Explore→Plan→Implement→Commit; "ask codebase
  questions"; "separate research from implementation") — primary/vendor.
- Community `/onboard` skill (tommcfarlin.com) — the most concrete onboarding
  procedure + persisted artifact.
- obra/superpowers brainstorming/using-superpowers/writing-plans SKILL.md —
  "explore project context: files, docs, recent commits"; "skills tell you HOW to
  explore."
- Cline Memory Bank (docs.cline.bot) — six domain-agnostic files; read-only Plan
  Mode.
- Aider repo-map (aider.chat) — tree-sitter → def/ref graph → PageRank → budgeted
  map.
- GitHub Copilot "Explore a codebase" — architecture overview ("provide
  evidence") → build → entry points → data flow → commits.
- Devin Knowledge onboarding (docs.devin.ai) — auto-Knowledge from README +
  structure + convention files; "investigate + propose a plan before executing."
- Non-code analogs: APEX-SQL (arXiv 2602.16720, hypothesis-verification against
  the real DB), AWS Bedrock text-to-SQL ("data dictionary as documentation"),
  Obsidian agents (wikilink graph as the reference graph).

**Empirical canon (memo 2)**
- Xia et al., *Measuring Program Comprehension*, IEEE TSE 2018 — ~58%
  comprehension time (78 pros, 3,148 hrs).
- Bacchelli & Bird, ICSE 2013 — "Code Review is Understanding"; 91% say unfamiliar
  code is slower.
- Cemri et al., MAST, arXiv 2503.13657 — FC1 41.8%; FM-1.1 11.8% / FM-1.3 15.7% /
  FM-1.5 12.4%; +9.4%/+15.6% interventions.
- Begel & Simon, *Novice Software Developers, All Over Again*, ICER 2008 —
  newcomers fail on orientation, not capability.
- Pirolli & Card, *Information Foraging* (Psych Review 1999) + *Sensemaking*
  (2005) — scent, MVT stopping rule, schematize step.
- Simon, bounded rationality / satisficing — task-scoped aspiration threshold,
  stop at "good enough."

**Transfer + honesty (memo 3)**
- UF Archival Processing — Surveying; Phillips Collection Archives 101 — the
  near-isomorphic non-code procedure + provenance/original-order.
- Watkins, *The First 90 Days* (summaries) — accelerate learning, stakeholder map,
  "action imperative" anti-pattern.
- McKinsey/consulting hypothesis method (Axiom, Stratechi) — issue tree, ~80%-
  certainty stop.
- Journalism beat onboarding (Reynolds Center, NPF) — jargon + source mapping.
- Klein, Data-Frame Theory of sensemaking — the cognitive substrate; early-anchor
  error is costliest.
- arXiv 2512.12117 — mechanical verification prevents 100% of fabricated-file
  citations (the verify-by-opening basis).
- RAG freshness literature — semantic similarity is blind to staleness;
  timestamp/provenance checks (the stale-source basis).
- KM reuse literature (Lucidea, knowledge-management-tools.net) + agent
  "Sisyphus Trap" — reuse-before-rediscover.
