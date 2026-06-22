---
tags:
  - agent-architecture
  - skills
  - product
  - defaults
---

# Default Skill Roster

> **Status**: Research + recommendation, 2026-06-21. **First build shipped 2026-06-22: `verify-before-done`** — authored as a bundled skill (`config/skills/verify-before-done/`) and bound `phase:tactical` on the worker experts (`defaults` → developer/critic/curator/bughunter, plus the overriders `scholar` + `designer`). Its stronger enforcement tiers (**B** read-gate, **C** trace-gate) are deferred follow-ups — see [Enforcement model & follow-ups](#enforcement-model--follow-ups). Defines which bundled **skills** SRW should ship *by default in every deployment*, structured as a tight always-on **universal core** plus **opt-in** and **expert-dedicated** tiers. Builds on the shipped skills substrate ([[agent_skills]]) and is the skills-side companion to [[default_expert_roster]]. Remaining Tier-1 builds: `systematic-debugging` and `brainstorming` (`planning-and-decomposition` already ships as `todo-guide`).
>
> **Method**: a `deep-research` harness run (107 agents, ~4.2M tokens, 6 search angles, 24 sources fetched, 119 claims extracted → 25 verified → **24 confirmed / 1 refuted** via 3-vote adversarial verification) surveyed Anthropic's official skills, Claude Code's `superpowers` ecosystem, OpenAI Codex, Cursor, Cline, community "awesome" collections, and the multi-agent failure literature — then synthesized against SRW's architecture. Descriptive claims (what each system *ships*) are strongly sourced (primary repos verified via live GitHub API, official docs, peer-reviewed MAST); roster *tier placements* are reasoned synthesis on top (confidence noted per call).

## Motivation

SRW now ships bundled skills (`config/skills/`) that every deployment inherits regardless of where it runs (see [[agent_skills]] for the shipped engine: storage, the fenced Layer-1 menu, `use_skill`, expert bindings, scripts). The question this doc answers: **what should that default set be?**

A skill is reusable "how to do X well" procedural knowledge the agent loads on demand — distinct from *tools* (the agent already has `web_search`, `read_file`, code execution, citations) and from *experts/personas* (the role the agent plays). The baseline ships to **every** client — orgs, businesses, individual/home users — so the set must be **near-universal**: a home user might have the agent write code (debugging helps), anyone might do knowledge work (research/analysis/writing helps). Explicitly **out of scope**: narrow domain verticals (e.g. "write a medical diagnostic letter", industry compliance). Per [[default_expert_roster]], the goal is **balanced general-purpose**, not a software-company roster.

## TL;DR

- **There are two layers, and the answer lives in the second.** What vendors ship as *defaults* is document/artifact verticals; what the ecosystem *converges on* as universally valuable **procedures** lives in the community/competitor layer. Shipping a procedural baseline is a **differentiated product decision, not "copy what Anthropic ships."**
- **Anthropic's `anthropics/skills` is 17 skills, all doc/artifact verticals** (`docx`, `pdf`, `pptx`, `xlsx`, `canvas-design`, `mcp-builder`, …) with **zero** generic procedural skills. The pre-built installable defaults are literally pptx/xlsx/docx/pdf.
- **The procedural roster is validated by `superpowers` + Cline + Cursor**, which independently ship/codify the same tight set — and grounded by the **MAST** failure taxonomy, which tells you *which* procedures matter.
- **Recommended universal core (4):** `systematic-debugging` · `verify-before-done` · `planning-and-decomposition` · `brainstorming`. Each validated by **2+ independent ecosystems AND** a MAST failure mode.
- **Opt-in (4):** `test-driven-development` · `code-review` · `codebase-onboarding` · `sub-agent-delegation`.
- **Expert-dedicated:** scholar → research-methodology + citation; developer → dev bundle; critic → receiving-code-review; writer → long-form writing (*weakest validation*); assistant → clarification-and-scoping.
- **Meta — `skill-creator`:** the one cross-vendor procedural *default* (Anthropic + Codex). Lets every client author their own; aligns with SRW's user-authored-skills goal.
- **SRW already ships 2 of these** — `todo-guide` (the planning skill, `before_tool: next_phase_todos` enforced gate) and `research-guide` (scholar research, `phase: tactical`). This extends a pattern that is already right.

## Headline finding: a two-layer split

The single most important discovery is that **"what to ship" has a different answer depending on which layer you look at.**

**Layer 1 — what vendors ship as DEFAULTS — is document/artifact verticals.** Anthropic's official [`anthropics/skills`](https://github.com/anthropics/skills) repo is exactly **17 skills** across four categories (Creative & Design, Development & Technical, Enterprise & Communication, Document Skills): `algorithmic-art`, `brand-guidelines`, `canvas-design`, `claude-api`, `doc-coauthoring`, `docx`, `frontend-design`, `internal-comms`, `mcp-builder`, `pdf`, `pptx`, `skill-creator`, `slack-gif-creator`, `theme-factory`, `web-artifacts-builder`, `webapp-testing`, `xlsx`. **None** is a generic `systematic-debugging`, `code-review`, `planning`, or `research-methodology` skill. The [docs' "Pre-built Agent Skills"](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview) section lists exactly PowerPoint/Excel/Word/PDF — all verticals. (A code-review skill exists only in a *separate* Anthropic plugin repo; a debugging skill was merely *proposed*, issue #267.)

**Layer 2 — what the broader ecosystem CONVERGES on as universally valuable PROCEDURES — is where the candidate roster is strongly validated**, and it lives one layer down:

- [**obra/superpowers**](https://github.com/obra/superpowers/) (accepted into Anthropic's *official* Claude Code plugin marketplace, Jan 2026) ships a concrete named `SKILL.md` roster covering nearly every candidate: `systematic-debugging`, `verification-before-completion`, `test-driven-development`, `brainstorming`, `writing-plans`, `executing-plans`, `requesting-code-review`, `receiving-code-review`, `dispatching-parallel-agents` (sub-agent delegation), `subagent-driven-development`, `using-git-worktrees`, plus meta-skills. It is presented as a **complete methodology with gating** ("Mandatory workflows, not suggestions") — not a loose collection.
- [**cline/clinerules**](https://github.com/cline/clinerules) bundles the same procedures as Markdown rule files: code-review, testing-strategy, codebase-onboarding, research, continuous-improvement (retrospective), memory-bank, … **Caveat:** it is a mixed community grab-bag that also contains many narrow verticals — it validates that these procedures are *authored*, not that any subset is objectively best. **Must curate, not adopt wholesale.**
- **Cursor** codifies four candidates as first-class built-in modes: [Plan Mode](https://cursor.com/blog/plan-mode) (planning = "the single most impactful change"), [TDD and Debug Mode](https://cursor.com/blog/agent-best-practices) (hypothesis + evidence + root-cause), and version-controlled Rules.

**The one procedural skill all vendors ship by DEFAULT is the meta-skill `skill-creator`** (Anthropic + OpenAI Codex both bundle it; Codex adds `plan` + `skill-installer`). For a platform whose product goal is *user-authored* skills, that is a strong signal to include it.

> **Consequence:** choosing the procedural roster below is defensible and differentiated — but it is explicitly **not** "copy what Anthropic ships." It is "ship what the ecosystem proved valuable, that Anthropic left to the community."

## Which procedures matter — the MAST grounding

Prioritization rests on the **MAST** failure taxonomy (Multi-Agent System Failure Taxonomy, [arXiv 2503.13657](https://arxiv.org/abs/2503.13657); 1,642 traces, κ=0.88 inter-annotator). Failures cluster:

- **System Design 44.2%** > **Inter-Agent Misalignment 32.3%** > **Task Verification 23.5%** — so **~76% of failures originate upstream of verification**, in design/coordination/specification.
- Concrete, measurable modes map straight onto skills:
  - *Disobey Task Specification* 11.8% + *Unaware of Termination Conditions* 12.4% → **planning / spec / knowing-when-done**
  - *Fail to Ask for Clarification* 6.8% → **clarification-and-scoping**
  - *No/Incomplete Verification* 8.2% + *Incorrect Verification* 9.1% (= 17.3%) → **verify-before-done**
- The paper's own interventions: improved role specification **+9.4%**, high-level objective verification **+15.6%**.
- **Caveat (load-bearing):** even structural interventions leave absolute multi-agent task-completion low (max measured +15.6%). Skills *mitigate named failure modes*; they are **not a performance silver bullet**.

This is why the universal core is debugging / verify / plan / brainstorm rather than, say, summarization or git-workflows: those four sit on the highest-frequency failure modes *and* have the broadest cross-context validation.

## Recommended roster

Applying [[default_expert_roster]]'s "few well-specified > many" rule (Anthropic: too many or overlapping capabilities *actively degrade* performance by distracting the agent). Each Tier-1 entry is validated by **2+ independent ecosystems AND** grounded in a MAST failure mode.

### Tier 1 — Universal core (ship with every deployment)

| Skill | Body covers | Validation | SRW mapping |
|---|---|---|---|
| **`systematic-debugging`** | Hypothesis → instrument → reproduce → isolate root cause **before** fixing; one change at a time | superpowers + Cursor Debug Mode + community `SKILL.md` | model-invoked via `use_skill` |
| **`verify-before-done`** | Run verification commands, confirm output, gather evidence **before** any success/completion claim | superpowers `verification-before-completion` + MAST FM-3.2/3.3 (17.3%) | tactical `todo_complete` gate + `check_goal` node — **candidate for an *enforced* binding** |
| **`planning-and-decomposition`** | Research → clarify → plan with file paths/owners → await approval; break work into verifiable units | Cursor ("single most impactful") + superpowers `writing-plans` + MAST FM-1.1/1.5 (24%) | **already shipped** as `todo-guide`, bound `before_tool: next_phase_todos` (enforced) |
| **`brainstorming`** | Structured divergent exploration (generate many options, defer judgment) before converging | superpowers `brainstorming` | model-invoked; the one **non-code** creative procedure — serves writers/researchers/analysts |

*Why exactly these four:* `systematic-debugging` and `verify-before-done` are the two most universal — a home user writing code and a knowledge worker checking an analysis both need them. `planning-and-decomposition` is Cursor's "most impactful" practice and sits on a combined 24% of MAST failures. `brainstorming` is the lone creative skill that is **not** code-specific, which keeps the core balanced for non-engineering deployments.

### Tier 2 — Nice-to-have (opt-in per deployment)

| Skill | Body covers | Validation | SRW mapping |
|---|---|---|---|
| **`test-driven-development`** | Write tests from input/output pairs first; write code to pass *without modifying tests*; iterate until green | Cursor + Anthropic CC best-practices ("strongest pattern") + Cline | developer expert |
| **`code-review`** (giving) | Review a diff for bugs / security / performance; structured findings | superpowers `requesting-code-review` + Cline `code-review.md` + Anthropic plugin | developer expert |
| **`codebase-onboarding`** | Map architecture, tech stack, key patterns of an unfamiliar repo before changing it | Cline `codebase-onboarding.md` | repository-datasource flow |
| **`sub-agent-delegation`** | Split independent work to parallel agents; review + merge each child | superpowers `dispatching-parallel-agents` | `delegate_work` + delegation-branch merge in `services/completion.py` |

These are heavily validated but **dev/coordination-leaning** rather than truly universal, so they are off-by-default for a knowledge-work/home deployment and on for a dev shop. (The Tier-2-vs-Tier-3 boundary for TDD and code-review is a judgment call — see [Open decisions](#open-decisions).)

### Tier 3 — Expert-dedicated (bound to the persona, not globally menu-listed)

| Expert | Skill | Note |
|---|---|---|
| **scholar** | `research-methodology` + citation discipline | **already shipped** as `research-guide` (`phase: tactical`) → `cite_web` / `kb_write`; the most concrete existing mapping |
| **developer** | dev bundle (debug / TDD / review / onboarding) | delivered as **enforced phase bindings**, not optional menu entries |
| **critic** | `receiving-code-review` + verification rigor | → strategic verdict tools |
| **writer** | long-form / technical writing & editing | ⚠️ **weakest validation in the roster** — no vendor ships a generic *prose procedure*, only artifact tools (docx, doc-coauthoring). Spike before committing. → `webdav_*` export |
| **assistant** | `clarification-and-scoping` | directly justified by MAST FM-2.2 (6.8%) + FM-1.5 (12.4%); the session side is SRW's thinnest per [[default_expert_roster]] |

### Meta

| Skill | Body covers | Validation | SRW mapping |
|---|---|---|---|
| **`skill-creator`** | Create/modify skills, run evals, optimize a description for trigger accuracy | **the one cross-vendor procedural default** (Anthropic + Codex) | the Cockpit skill editor + Slice-1 CRUD already exist; this is the *in-agent* authoring counterpart |

## Prior art: SRW already ships two of these

Worth stating plainly: **SRW is already ~2/8 of the way to this roster.** [[agent_skills]] Slice 3 migrated:

- **`todo-guide`** → bound `before_tool: next_phase_todos` (enforced gate). This **is** the `planning-and-decomposition` skill, already in production behind the enforced-binding mechanism.
- **`research-guide`** → bound `phase: tactical`. This **is** the scholar `research-methodology` skill.

And the **enforced-gate pattern** SRW built is exactly the model `superpowers` validates ("mandatory workflows, not suggestions"). So this roster is not a from-scratch build — it extends a substrate and a pattern that are already correct.

## Authoring rubric — "what makes a good skill"

Distilled from the primary best-practices sources ([`skill-creator/SKILL.md`](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md), [writing-tools-for-agents](https://www.anthropic.com/engineering/writing-tools-for-agents), [Codex skills](https://developers.openai.com/codex/skills), [Cursor best-practices](https://cursor.com/blog/agent-best-practices)). Use this to author every roster entry.

1. **One job per skill, kept tight.** Instructions-first; scripts only for *deterministic* behavior.
2. **Progressive-disclosure budgets** (soft guidance, not hard caps — only `name` ≤64 char / `description` ≤1024 char are enforced):
   - **L1** metadata (name + description) ~100 tokens, *always* in context
   - **L2** `SKILL.md` body **< 500 lines / < 5k tokens**, loaded on trigger
   - **L3** references/scripts/assets, loaded on demand. Reference files > 300 lines get a table of contents; repeated helper code moves to `scripts/` (executed, source never enters context).
3. **The third-person `description` *is* the trigger text.** State *what it does + when to use it*, then test trigger accuracy against it.
4. **Avoid rigid ALL-CAPS ALWAYS/NEVER.** Anthropic calls this a "yellow flag" — explain *why* (theory-of-mind); the model is smart.
5. **Few well-specified > many.** Add a skill only on an *observed* recurring need; version-control them.

## Open decisions

The research deliberately surfaced four questions the sources cannot settle — they are product/packaging calls:

1. **TDD & `code-review`: Tier 2 or Tier 3?** The most-validated candidates, but software-specific. A knowledge-work/home deployment may want them off by default; a dev shop wants them always-on.
2. **Do coding-validated skills transfer to pure knowledge-work?** This is the central **unmeasured** premise — *every* strong validation source (superpowers, Cursor, Cline) is a **coding** ecosystem. No source measures `systematic-debugging`'s value for non-code research/writing. Plausible (the procedures are general) but it is extrapolation. **Recommended de-risk:** run one SRW eval job with a draft `systematic-debugging` skill on a *non-coding* task before committing it to the core.
3. **Model-invoked vs. enforced/gated, per skill?** Evidence supports both. Likely mix: *enforce* `verify-before-done` (and keep the existing enforced `planning`), leave `brainstorming` / `systematic-debugging` model-invoked. Tune on SRW's own eval set, per Anthropic's "match your evaluation tasks" guidance.
4. **Build `writer` first?** It is the planned new writer expert ([[default_expert_roster]]) but the **weakest-validated** roster entry — no vendor ships a generic prose *procedure*. A spike is warranted before baseline commitment.

## Enforcement model & follow-ups

Skills can be delivered at three escalating levels of enforcement. Authoring the `SKILL.md` (the guidance) is independent of which level it is bound at — and `verify-before-done`, the first build, deliberately ships at the lowest level, with the higher two captured here as follow-ups. The same ladder applies to any skill where compliance matters.

| Tier | Mechanism | Status for `verify-before-done` |
|---|---|---|
| **A — Guidance** | Author the `SKILL.md`; bind `phase:tactical` so the body auto-injects whenever the agent is doing tactical work (where completion happens). Portable; no orchestrator changes. | **✅ Shipped 2026-06-22** (worker experts). |
| **B — Read-gate** | The *existing* `before_tool` enforce binding (as `todo-guide` uses on `next_phase_todos`): the action is refused until the agent has read the skill. | **Available, not used here** — forces *reading*, not *doing*. |
| **C — Trace-gate** | Orchestrator-side check at `check_goal` / the completion gate: reject `goal_achieved` / `todo_complete` unless a workspace tool actually ran in the current tactical phase **and** its output is referenced in the completion payload; on failure, re-inject the body and force another loop. | **Deferred** — new infra, its own design doc. |

**The key distinction (B vs C):** "the agent read the skill" ≠ "the agent performed verification." B only proves the body was in context; the agent can read it and still claim success without running anything. C is the only tier that checks the *behavior*. The verify-before-done research argues C is necessary (it cites a "compliance gap" where models promise to follow a process in text but skip the execution) — and C is *general* completion-integrity infra that would harden every skill, which is why it belongs in its own design doc rather than riding on this one.

**Before building C, measure.** The headline statistic motivating C (near-0% process compliance when self-controlled → 75%+ behind a deterministic gate) traces to effectively a single source plus two arXiv IDs not independently confirmed. Instrument our own runs first: across completed jobs, how often did the agent emit `goal_achieved` without a fresh verification tool call in that tactical phase? High rate → C is justified; if Tier-A guidance already moves it, C may be unnecessary. Same measure-before-building discipline applied to the rate-limit knobs.

**Already covered:** the research's "recursion trap" (an over-rigid gate causing infinite repair-verify loops) is already mitigated by SRW's fingerprint-based loop detection (hard caps: tactical = rewind, strategic = freeze), so C inherits that backstop rather than needing its own.

## Considered but not elevated (no silent drops)

From the original candidate list, the following were **folded** or **deprioritized**, with reasons:

- **spec/requirements-writing** → folded into `planning-and-decomposition`.
- **web-research** → it is a *tool* SRW already has, not a procedure; the *methodology* lives in the scholar `research-guide`.
- **summarization & synthesis**, **data-analysis**, **git/version-control workflows** → single-ecosystem or tool-not-skill; not elevated to the core. Reasonable opt-in candidates later.
- **reflection/retrospective** → Cline ships `continuous-improvement-protocol`; viable Tier-2 add but not core.
- **reading/navigating large codebases** → captured as Tier-2 `codebase-onboarding`.

**One claim was killed (0-3 adversarial vote):** a search result asserting "the Claude API skill is Anthropic's *only* bundled skill" — false; the repo ships 17.

## Confidence & caveats

- **Landscape facts are high-confidence:** all rest on primary sources (vendor GitHub repos verified via live GitHub API in June 2026, official docs, the peer-reviewed MAST paper); all 24 underlying claims passed 3-0 adversarial votes.
- **Roster tiering is MEDIUM-confidence synthesis:** the sources prove each procedure *exists* and is shipped/recommended by 1–3 ecosystems; **no source prescribes this exact tiering** for a general-purpose platform. The Tier-1 universality argument extrapolates coding-context validation to knowledge-work (the research question's own premise).
- **Weakest point:** the `writer` skill — no source ships a generic prose-quality procedural skill.
- **Time-sensitivity:** the `SKILL.md` standard was released Dec 2025; `superpowers` entered Anthropic's marketplace Jan 2026; community repo contents drift. The 17-skill Anthropic count and the superpowers/Cline rosters are accurate as of June 2026 live checks.

## Sources

Primary (verified, load-bearing):

- Anthropic official skills repo — <https://github.com/anthropics/skills>
- Anthropic Agent Skills overview/docs — <https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview>
- `skill-creator` SKILL.md — <https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md>
- Anthropic, "Writing tools for agents" — <https://www.anthropic.com/engineering/writing-tools-for-agents>
- Anthropic, "Effective context engineering for AI agents" — <https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>
- OpenAI Codex skills — <https://developers.openai.com/codex/skills>
- obra/superpowers — <https://github.com/obra/superpowers/>
- cline/clinerules — <https://github.com/cline/clinerules>
- Cursor agent best-practices / Plan Mode / Debug Mode — <https://cursor.com/blog/agent-best-practices>, <https://cursor.com/blog/plan-mode>, <https://cursor.com/blog/debug-mode>
- MAST failure taxonomy — <https://arxiv.org/abs/2503.13657>
- SKILL.md open standard — <https://agentskills.io>

Secondary/community (corroboration): VoltAgent/awesome-agent-skills, karanb192/awesome-claude-skills, addyosmani/agent-skills, the superpowers write-ups (fsck.com, builder.io, marcnuri.com), composio top-skills.

## Next steps

1. **Decide** the four open questions (especially #2 — the knowledge-work transfer spike — is cheap and de-risks the whole premise).
2. **Draft the Tier-1 four** as real `config/skills/<name>/SKILL.md` bundles in the existing house style (Jinja `has_tool` conditionals, graceful tool-name fallback), using the authoring rubric above.
3. **Wire bindings** per decision #3 (enforce `verify-before-done`; model-invoke the rest) via the `instruction_files` `skill:` field ([[agent_skills]]).
4. Revisit `writer` (decision #4) after a spike.
