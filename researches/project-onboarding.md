# Skill Research Prompt — `project-onboarding`

An instance of the [skill research template](./template.md) — the prompt to run
to produce the evidence base for the `project-onboarding` skill.

- **Status:** ✅ **run + built 2026-06-25.** Reframed from the roster's Tier-2
  `codebase-onboarding` entry: SRW's unit of work is a **project** (a datasource
  *bundle* — cloud mounts / Obsidian vaults / shared folders of docs+PPTX+
  protocols, SQL/graph/Mongo databases, *and* code repos — plus a project
  knowledge base and accumulated memories), so "codebase" is the wrong,
  IDE-agent frame. Code is one datasource branch, not the skeleton. Research ran
  inline (three scoped foreground passes — the background harness keeps getting
  orphaned by process restarts) → synthesized to [`project-onboarding.report.md`](./project-onboarding.report.md)
  → authored `config/skills/project-onboarding/SKILL.md`, shipped **model-invoked
  (universal)**. **Transfer verdict: universal *as a procedure*** (stronger than
  debugging's) — the inventory → source-of-truth → structure/vocabulary → map
  skeleton is the *named* procedure in archival survey, *First 90 Days*,
  consulting, journalism, and Klein's data-frame sensemaking; one body serves all
  experts, code is one branch. Two load-bearing halves: orientation is the
  bottleneck (~58% of dev time is comprehension, Xia TSE 2018; MAST FC1 41.8%)
  **and** it must terminate (explore-forever = MAST FM-1.5 12.4%; stop when you
  can write the map + a located plan). Three honesty mechanisms baked in
  (verify-by-opening, provenance/freshness-check the source of truth — the
  `Code_Repository` snapshot trap — and reuse-prior-KB-before-rediscover, gated by
  freshness). Local k3d verified (file parses in-pod via the real `skill_format`
  path; unbound → model-invoked); dev catalog gets it when develop is pushed.
- **Scope:** orienting yourself in an UNFAMILIAR body of work before you act —
  general project onboarding, NOT code-only. Closest existing SRW affordances it
  ties together: the project KB (`search_knowledge`/`kb_write`), `datasources.md`,
  the datasource inventory, and RecallStore memories.
- **Roster:** Tier-2 opt-in (was `codebase-onboarding`),
  [`../docs/features/default_skill_roster.md`](../docs/features/default_skill_roster.md).
- **THE crux (foregrounded):** the transfer question. Onboarding is the roster's
  *best* generalization candidate — "orient before you act" predates software
  (consultants, analysts, new hires, archivists all do it), unlike debugging
  whose core was welded to executable code. Test it, don't assume; find evidence
  either way, the way the debugging RCA-bridge question was tested.
- **Likely enforcement:** model-invoked (rides the catalog; no single expert
  owns it — scholar onboards to a corpus, developer to a repo, assistant to a
  folder). A `phase:strategic` "orient at the start of a fresh project" binding
  is a candidate to weigh — confirm via the research.

## The prompt

```text
You are a research agent. Your job is to produce the complete evidence base
needed to AUTHOR one highly-optimized "Agent Skill" — a SKILL.md procedural guide
that an autonomous AI agent loads on demand. Your output will be handed directly
to whoever writes the skill, so it must be concrete and authoring-ready, not an
abstract survey.

Go out to the internet. Find best practices, concrete procedures, tips & tricks,
failure modes, and real example implementations for the skill named below. Prefer
primary sources (vendor docs, the actual SKILL.md/rule files in public repos,
peer-reviewed papers) over listicles. Cite every non-obvious claim with a URL.
Clearly separate VERIFIED FACT (sourced) from RECOMMENDATION (your synthesis).

═══════════════════════════════════════════════════════════
SKILL UNDER RESEARCH
═══════════════════════════════════════════════════════════
Name:        project-onboarding  (orienting yourself in an UNFAMILIAR body of
             work before you act — a general "get your bearings" skill, NOT a
             code-only "understand the codebase" skill)
Intent:      When an agent is dropped into an unfamiliar project, build a working
             map of it BEFORE acting: inventory what's there, find the
             authoritative source(s) of truth, learn the structure / conventions /
             vocabulary, read what's already been learned about it, and mark what
             you've confirmed vs. assumed — so the first real action is informed,
             not a guess. Closes the gap where the agent charges in on assumptions
             about a structure it never checked, reinvents what a prior worker
             already mapped, trusts a stale or wrong source of truth, or (the
             opposite failure) explores forever and never starts.
A "project" here is deliberately BROADER than a codebase. It is a bundle of
heterogeneous material, of which code is one possible part:
  • a set of CLOUD-STORAGE / shared files — an Obsidian vault, a shared folder of
    PowerPoints, meeting protocols, specs, PDFs, a document corpus;
  • STRUCTURED data — SQL / graph / document databases, datasets;
  • optionally a CODE repository;
  • plus accumulated project memory — prior notes and a knowledge base built up by
    earlier work on the same project.
Must work across domains IF it is to be universal — and whether it genuinely does
is THE key open question (see objective 3). The prior here is the STRONGEST of any
skill so far: "orient before you act" is a general knowledge-work discipline older
than software (new-employee onboarding, a consultant taking a new client, an
analyst handed an unfamiliar dataset, a journalist picking up a beat, an archivist
writing a finding-aid). But test it; don't assume. Candidate instances:
  • Software:  dropped into a repo — read the README, find the entry point, map
               the module/dependency structure, learn the build/test commands,
               trace one request end-to-end, RUN the tests to confirm it works,
               note the conventions — before changing anything.
  • Research:  handed a corpus / set of sources / a literature area — survey what's
               there, find the authoritative / primary sources vs. derivative ones,
               learn the domain vocabulary and the open questions, read any prior
               synthesis — before forming claims.
  • Writing/Docs: handed a shared project folder (decks, meeting protocols, specs,
               an Obsidian vault) — find the index / source-of-truth doc, learn the
               structure and naming conventions, identify the stakeholders and what
               "done" looks like here — before drafting.
  • Analysis:  handed an unfamiliar dataset / database — read the schema and the
               table relationships, find the data dictionary, sanity-check a few
               rows, learn what each field means and how it's populated — before
               running an analysis you'd otherwise misread.
Failure modes it must defeat:
  • Act-before-orient — charging in on assumptions about structure/conventions the
    agent never actually checked (the dominant failure; the source of most "it
    confidently did the wrong thing").
  • Reinventing prior work — re-deriving from scratch what an earlier job already
    mapped and wrote to the project knowledge base / notes; ignoring accumulated
    memory.
  • Trusting the wrong / stale source of truth — treating a snapshot, an old copy,
    or a derivative artifact as authoritative (e.g. an out-of-date vendored copy
    of the real thing) and building on it.
  • Analysis paralysis / over-onboarding — exploring forever, reading everything,
    never converging to a map or starting the actual work. The opposite failure;
    onboarding must TERMINATE.
  • Missing the conventions — violating the project's norms (naming, structure,
    house style, where the source of truth lives) because they were never learned.
  • Hallucinating the map — asserting structure/facts about the project it didn't
    verify (e.g. "the config lives in X") and acting on it.
Empirical grounding to dig into:
  • Bacchelli & Bird (ICSE 2013) "understanding the change is the genuinely hard
    part" — the obvious analogue is "understanding the PROJECT is the hard part."
    Find evidence that comprehension/orientation, not action, is the bottleneck.
  • Developer onboarding / time-to-first-commit / "ramp-up" literature, and the
    cost of context-free action — anything that quantifies how orientation pays
    off (or how skipping it costs).
  • How autonomous coding agents do repo orientation: the "explore the codebase
    first" / "understand before you change" patterns in Cursor, Cline, Aider,
    Devin, Claude Code, and any public "codebase-onboarding" / "explore" rule or
    SKILL.md. Quote the actual steps — then assess how much is code-specific vs.
    a general orient-first move.
  • MAST (arXiv 2503.13657): the System Design / spec cluster — acting without
    grounding context drives Disobey-Task-Specification (FM-1.x) and the broader
    System Design failures (~44%). Find the specific failure-mode IDs and %s where
    acting-before-understanding is the root.
  • Information-foraging / sensemaking theory (Pirolli & Card) — the theory of how
    people efficiently locate and structure unfamiliar information; relevant to a
    DISCIPLINED (not infinite) orientation procedure.
Platform anchors this skill should reference (keep guidance concrete):
  • A PROJECT is a first-class SRW entity bundling DATASOURCES of different kinds:
    cloud-storage mounts (Obsidian vault, shared doc/deck/protocol folders),
    SQL / graph / Mongo databases, and code repositories. The agent should start
    by INVENTORYING what's attached, not assuming it's a repo.
  • The project KNOWLEDGE BASE is the accumulated onboarding of every prior job on
    this project. The first move is to READ it (search_knowledge /
    get_knowledge_summary) and the prior notes/, not to rediscover from scratch —
    and the LAST move is to WRITE the map back (kb_write) so the next job inherits
    it. This read-first / write-back loop is the skill's SRW backbone.
  • `datasources.md` is the in-workspace datasource index; RecallStore memories are
    injected each call. Reference these by name.
  • Per datasource type, the orientation move differs: files → the index / README /
    top-level structure + naming conventions; a DATABASE → the SCHEMA and table
    relationships (get_table_schema / list_tables / query a few rows); a REPO →
    README + entry point + module map + RUN the tests. Make the code path ONE
    BRANCH, not the skeleton.
  • The STALE-SNAPSHOT trap is real and worth a concrete warning: a project may
    contain an out-of-date vendored copy of the real source (an old snapshot in a
    documents/ folder) that DIFFERS from the live source. The skill must tell the
    agent to confirm WHICH source it actually has before trusting it.
  • Termination: onboarding is DONE when the agent has a map it can act from
    (datasources inventoried, source of truth identified, conventions + vocabulary
    learned, map written back) — NOT when it has read everything. Tie this to the
    strategic/tactical phase model: orientation is naturally STRATEGIC (it precedes
    and informs the first todo list), and its output should feed planning.
  • Autonomy: at "never pause" the agent onboards itself with no human to ask, so
    the procedure must be self-contained and must converge.
═══════════════════════════════════════════════════════════

CONTEXT — THE PLATFORM THIS SKILL RUNS ON (so your recommendations actually fit):
• It's a multi-tier orchestration system running FULLY AUTONOMOUS agents (a
  LangGraph state machine) as well as interactive sessions. Autonomous agents
  often run for many steps with NO human in the loop — so the skill must hold up
  with no one watching.
• Agents work in phases: STRATEGIC phases (planning, creating a todo list)
  alternate with TACTICAL phases (executing those todos). A skill can be bound to
  fire automatically in a given phase, gated before a specific tool, or left for
  the agent to invoke by judgment.
• Completion model: the agent never writes final job status itself — it sets a
  "stop + goal_achieved" signal and an orchestrator decides the real outcome.
  A FALSE result is expensive: it ends the job.
• The agent operates in an isolated remote workspace it reaches over SSH: it can
  run commands (tests, builds, curl, scripts), read/write files, search the web,
  and record citations and knowledge notes. Procedures can and should produce
  real artifacts, not just claims.
• It supports a WIDE RANGE of LLMs across providers, with per-model-family prompt
  variants. So: guidance must be model-agnostic and robust, and you should advise
  whether the skill needs per-family wording variants or works as one body.
• Autonomy levels range from "never pause" to "pause every phase." The skill must
  hold up at the "never pause" extreme.

WHAT AN "AGENT SKILL" IS (the format you're writing FOR):
• Open SKILL.md standard (agentskills.io) — a directory: a SKILL.md (YAML
  frontmatter `name` + `description`; markdown body) plus optional references/
  and scripts/. Portable to/from Claude Code and Codex.
• Progressive disclosure: L1 = name+description (~100 tokens, ALWAYS in the
  system prompt — this is also the trigger text the agent matches on); L2 = the
  SKILL.md body, loaded on demand (target <500 lines / <5k tokens); L3 = bundled
  reference files / scripts, pulled only when the body points to them.
• AUTO-INJECTION: a skill can be (a) model-invoked (agent decides to load it),
  (b) phase-injected (auto-loaded in strategic/tactical phases), or (c) an
  ENFORCED gate (the agent is refused a specific action until it has read the
  skill). One of your deliverables is a recommendation on which mode fits this
  skill.
• Authoring rubric to respect in every recommendation: one job per skill;
  instructions-first (scripts only for deterministic steps); the third-person
  `description` states what-it-does + when-to-use and IS the trigger; AVOID rigid
  ALL-CAPS ALWAYS/NEVER — explain WHY instead (the model is capable); keep it
  tight.

RESEARCH OBJECTIVES — find and synthesize:
1. How leading agent systems do project / repo orientation. Pull the ACTUAL
   procedure text where public — Cursor / Cline / Aider "explore the codebase"
   rules, Devin's onboarding, Claude Code "superpowers" and any public
   codebase-onboarding / explore SKILL.md or rule file, plus how IDE agents build
   a repo map (e.g. Aider's repo-map). Quote the steps. Then assess, for each, how
   much is code-specific vs. a general "orient before you act" move that would
   survive on a document corpus or a database.
2. The comprehension-is-the-bottleneck evidence: Bacchelli & Bird (ICSE 2013) and
   any developer-onboarding / ramp-up / time-to-first-commit research that
   quantifies the payoff of orientation (or the cost of skipping it). Plus
   information-foraging / sensemaking theory (Pirolli & Card) for a DISCIPLINED,
   terminating orientation procedure.
3. THE TRANSFER QUESTION (treat as the TOP objective): is there a genuinely
   universal "orient yourself before you act" discipline that applies to RESEARCH,
   WRITING/DOCS, and DATA ANALYSIS — drawn from new-employee onboarding,
   consulting engagement onboarding, archival finding-aids / library science,
   journalism beat-onboarding, sensemaking — or is the only WELL-EVIDENCED,
   step-by-step procedure specifically CODEBASE exploration? This is the crux that
   decides whether the skill ships UNIVERSAL (one body, all experts) or stays
   code-bound. The prior strongly favours universality (orientation predates
   software), but find hard evidence either way, and be explicit about how much of
   any "universal" claim rests on management/library/sensemaking literature vs. on
   actual autonomous-agent practice (where almost all evidence is code).
4. Keeping the agent honest about its map: how to prevent hallucinated structure
   (asserting where things live without checking), trusting a stale/derivative
   source of truth, and reinventing what prior work already recorded. What
   measurably helps — verify-by-opening, cross-checking against an index,
   reading the prior knowledge base first.
5. Termination & scope: how the agent knows onboarding is DONE (has an actionable
   map: inventory + source of truth + conventions + vocabulary, written back),
   avoiding BOTH under-orientation (act-on-guesses) AND over-orientation (read
   everything, never start). How deep is deep enough, and how to scope orientation
   to what the actual task needs rather than mapping the whole project.
6. Cross-domain phrasing: write ONE procedure that holds for a repo AND a document
   corpus AND a database AND a research area without becoming code-specific — and
   say whether the evidence supports one universal body with per-datasource
   branches, or genuinely separate procedures.

DELIVERABLE — return ALL of the following, authoring-ready:
A. Executive synthesis: the strongest, best-supported approach to orienting in an
   unfamiliar project as an autonomous agent (3–6 tight paragraphs, cited).
B. The recommended SKILL.md BODY PROCEDURE — the concrete, ordered steps the skill
   should tell the agent to do (read what's already known first → inventory the
   datasources → for each, find the source of truth + conventions [files / DB /
   repo branches] → learn the vocabulary & what "done" looks like → write the map
   back so the next job inherits it → don't act on guesses: mark confirmed vs.
   assumed), written as domain-generally as the evidence supports, with short
   code / research / docs / data examples per step. This is the heart of the
   output. Make the CODE moves one branch, not the skeleton.
C. A reusable scaffold the body can embed — e.g. a compact "project map" template
   (datasources + source-of-truth + conventions + vocabulary + open questions +
   confirmed-vs-assumed) the agent fills in and writes back to the KB. Keep the
   embedded version tight; push any long per-datasource-type checklist to an L3
   reference file and say so.
D. The quality bar: what makes GOOD orientation (actionable map, right source of
   truth confirmed, prior knowledge reused, terminated on time) vs. a shortcut
   (acted on guesses) or a runaway (read everything, never started) — with a short
   example of each.
E. Anti-patterns section: the failure modes (from the SKILL block / objectives)
   the body should warn against, each with a one-line "instead, do X."
F. Enforcement AND SCOPE recommendation: (i) model-invoked vs. phase-injected vs.
   gated; (ii) — driven by objective 3 — should this ship UNIVERSAL (model-invoked
   for all experts) or be code-bound? Weigh the candidate `phase:strategic`
   "orient at the start of a fresh project" binding against the risk of firing on
   every job (including familiar ones). Recommend and justify against the platform
   context.
G. Trigger-description draft: a candidate third-person `description` line
   (what-it-does + when-to-use) optimized for accurate triggering, plus 2–3
   alternates. It must trigger on "get up to speed on / orient in / explore this
   project|repo|corpus|dataset" WITHOUT misfiring on verify-before-done (checking
   your own finished work) or research-guide (investigating a question to produce
   findings) — name those boundaries.
H. Model-variance note: does this skill need per-model-family wording, or is one
   body robust? Evidence-based.
I. 2–4 real example snippets from the wild (quoted, attributed) worth adapting —
   ideally including at least one actual codebase-onboarding / explore rule or
   SKILL.md, and one non-code orientation procedure (onboarding / consulting /
   sensemaking) that supports the transfer claim.
J. Open questions / weak spots in the evidence, explicitly flagged.
K. Full source list with one-line quality/relevance notes.

GUARDRAILS:
• Cite primary sources; mark FACT vs RECOMMENDATION.
• The transfer question (objective 3 / deliverable F) is THE crux — do NOT
  hand-wave it. The prior favours universality (orientation predates software),
  but say plainly how much of any "universal" claim rests on management / library
  science / sensemaking literature vs. on actual autonomous-agent practice (where
  the evidence is almost entirely code repos).
• Resist drifting into a code-only "understand the codebase" guide. A project is a
  datasource BUNDLE (cloud files / vault / docs, databases, optionally a repo);
  code orientation is ONE branch. If you find yourself writing a repo guide, stop
  and generalize.
• Respect the budgets (body <500 lines / <5k tokens) — recommend what EARNS its
  place; push any long per-datasource-type checklist into an L3 reference file and
  say so.
• Where our platform's mechanics (the project + datasources model, the project
  knowledge base as accumulated onboarding, datasources.md, get_table_schema /
  run_command / kb_write / search_knowledge, the strategic/tactical phase model,
  the orchestrator-decides-status model) change the right answer, say how.
• Make ONBOARDING TERMINATE: the opposite failure (explore forever, never start)
  is as real as acting-on-guesses. Weight both.
```
