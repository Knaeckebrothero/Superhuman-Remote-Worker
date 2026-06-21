---
tags:
  - agent-architecture
  - experts
  - product
  - defaults
---

# Default Expert Roster

> **Status**: Research + recommendation, 2026-06-21. **No code changes yet.** Defines which bundled experts SRW should ship *by default in every deployment*, structured as a tight always-on **core** plus **opt-in tiers**. Builds on the shipped experts substrate ([[global_expert_management]]) and the skills layer ([[agent_skills]]). The roster recommendation is **pending decision**; the proposed first concrete build is a new `writer` worker expert.
>
> **Method**: a `deep-research` harness run (113 agents, ~4.5M tokens, 30 sources fetched, 146 claims extracted → 25 verified → **22 confirmed / 3 refuted** via 3-vote adversarial verification) surveyed multi-agent frameworks, coding-agent products, deep-research systems, and the role-specialization literature — then synthesized against SRW's architecture. Descriptive claims (what each system *ships*) are strongly sourced; roster *placements* are reasoned synthesis on top (confidence noted per call).

## Motivation

SRW ships bundled experts (`config/experts/`) that every deployment inherits regardless of where it runs. The question this doc answers: **what should that default set be?** Some roles you always want no matter the deployment — a researcher, a builder, a reviewer. Others are domain-specific and should be opt-in. We currently ship **7** experts (`scholar`, `developer`, `critic`, `curator`, `designer`, `designer-interactive`, `bughunter`); this doc re-assesses that set against external evidence and proposes a deliberate core + tiered catalog.

The goal is **balanced general-purpose** — roughly even coverage across software engineering, research/knowledge work, and writing/design — not a software-company roster and not a research-only roster.

## TL;DR

- **Ship a small core, not a big roster.** The strongest evidence (the MAST failure taxonomy) says multi-agent systems fail 41–86.7% of the time, gains over a single strong agent are "often minimal," and failures cluster in **role/system design**, not coordination. Breadth is a liability; **investing in a few sharply-specified roles is the win** (improving role specs alone gave ChatDev +9.4% with no new agents).
- **Recommended core (5):** `scholar` (research) · `developer` (build) · `critic` (review) · **`writer`** (synthesize — *new*) · **`assistant`** (general-purpose session — *new*).
- **Move to opt-in:** `curator`, `designer`, `bughunter`.
- **Don't ship as experts:** orchestrator / planner / supervisor / router / triage / memory-manager — these are framework mechanics SRW already owns.
- **Biggest practical gap:** 6 of the current 7 are *worker* experts; the **session** side is nearly empty. A "deploy-anywhere" default needs at least one strong interactive role.
- This validates the platform's **capability-grants (deny-by-default) model** as the correct lever: tight per-role tool restriction + clear authority beats policing personas (role *disobedience* is a rare 1.5% failure mode).

## Headline finding: a few well-specified roles beat many

Do not optimize for roster size. The most rigorous source surveyed — **MAST** (Multi-Agent System Failure Taxonomy, arXiv 2503.13657; UC Berkeley, NeurIPS 2025 D&B; 1,642 traces across 7 frameworks incl. MetaGPT/ChatDev/AG2/Magentic-One, κ=0.88) — reports:

- **41–86.7% failure rate** across 7 SOTA open-source multi-agent systems; performance gains on popular benchmarks are "**often minimal**."
- Failures cluster in **System/role Design (41.8%)** > Inter-Agent Misalignment (36.9%) > Task Verification (21.3%).
- **Role *disobedience* is rare (≈1.5%).** The dominant failure modes are task-disobedience, step-repetition, and unawareness-of-termination.
- Intervention evidence: **improving role *specifications* alone raised ChatDev success +9.4%** (same prompt, same model) by fixing authority (CEO final say), i.e. *no extra agents*.

Independent corroboration: OpenHands ships a **single unified default agent** (`CodeActAgent`) and its team publicly argues single-agent systems shouldn't be dismissed.

**Implications for SRW:**

1. Keep the **default core tight** (~4–5). Let the opt-in tiers and user-defined experts ([[global_expert_management]]) carry breadth.
2. Spend effort on **role specification quality** (persona + workflow + authority) and **least-privilege tool/capability scoping** — which is exactly what capability grants enable.
3. Don't add a role unless it earns its place by precedent *and* isn't already covered by a framework mechanic.

## Recommended default roster

### A) Always-on core (5 roles)

Four worker roles cover the three balanced legs (SWE / research / writing) plus the cross-cutting quality role; one session role closes the interactive gap.

| Role | Type | Maps to current | Precedent (why it's a universal default) | Tool domains | Capability grants |
|---|---|---|---|---|---|
| **scholar** (researcher) | worker | **keep as-is** | Researcher recurs in *every* knowledge system: GPT Researcher's `Researcher`, Magentic-One's `WebSurfer`+`FileSurfer`, Anthropic's parallel research subagents | web research (search/crawl/extract/browse), browser, KB read | `browser`, `datasource_tools` |
| **developer** (executor) | worker | **keep as-is** | Executor/coder is near-universal: MetaGPT `Engineer`, ChatDev `Programmer`, Magentic-One `Coder`+`ComputerTerminal`, and OpenHands' *entire* default agent | files, shell, git | `shell_tools`; optional `vm_workspace`, `datasource_tools` |
| **critic** (reviewer) | worker | **keep as-is** | Generator–critic loop is the best-evidenced quality pattern: ChatDev `Reviewer`, GPT Researcher `Reviewer`/`Reviser`, Anthropic's separated `CitationAgent`. SRW's auto-spawn-on-completion already matches this | files read, web/citations, KB read | read-heavy, **minimal write** |
| **writer** (synthesizer) | worker | **NEW** | The missing writing/communication leg: GPT Researcher ships a dedicated `Writer`+`Publisher`; Anthropic/MetaGPT pipelines terminate in compiled artifacts. SRW has executor+researcher+critic but nothing whose job is *producing the deliverable* | files write, KB read, web/citations, git read | **low** — explicitly no `shell_tools`/`vm_workspace`/`delegation` |
| **assistant** (generalist) | **session** | **NEW** | The most-wanted "deploy-anywhere" default and SRW's thinnest area. Mirrors Claude Code shipping a *generic* built-in core (Explore/Plan/general-purpose) and offering specialists as examples | files, web research, KB | moderate, deployment-tunable |

Per-role notes:

- **`writer` is the highest-value, lowest-risk addition.** It closes the writing leg, carries the smallest capability footprint (no shell, no VM, no delegation → safe for untrusted/showcase deploys), and is directly useful to in-house documentation work. Recommended **first build**.
- **`assistant`** may be thin to implement if `persistent_defaults` already backs bare sessions — the value is a *named, listed* picker entry with a strong general persona, plus a home for the session core.

### The session gap (sharpest practical finding)

Of the current 7 experts, **6 are workers** — only `designer-interactive` extends `persistent_defaults`. For any deployment that leans interactive there is **no general assistant, no research-chat, no coding-chat** session expert. A balanced "no matter where you deploy" default must include at least one strong session role (`assistant`), and arguably session variants of `scholar`/`developer`/`writer` for interactive power users (opt-in).

> **Architecture constraint (verified in code):** an expert's mode is fixed by the base it `$extends` — `defaults` ⇒ worker, `persistent_defaults` ⇒ session — and for DB-backed experts `expert_type` is an **immutable column**. There is **no "one definition, two modes."** Offering a role in both modes means **two expert definitions** (the `designer` / `designer-interactive` pattern). This shapes several recommendations below.

### B) Opt-in tiers (grouped catalog, enable per deployment)

| Tier | Roles | Precedent |
|---|---|---|
| **Software-engineering** | **bughunter** (adversarial QA/tester), `debugger` (Edit-enabled fix workflow) | MetaGPT `QA Engineer`, ChatDev `Tester`, Anthropic `debugger` example |
| **Research / analysis** | `fact-checker`/`verifier`, `citation-specialist` *(only if split out from the core `critic`)* | GPT Researcher `Reviewer`, Anthropic `CitationAgent` |
| **Writing / communication** | `editor`/`outliner`, `publisher`/`formatter` — *but these read more like **skills** of `writer` than full experts* | GPT Researcher `Editor` + `Publisher` |
| **Data** | `data-scientist`, `db-reader`/`query-validator` | Anthropic's two non-coding example subagents |
| **Design** | **designer** | ChatDev `Designer` (domain-specific, not universal) |
| **Knowledge** | **curator** | (no surveyed framework ships a standalone curator — see verdict) |
| **Ops / infra** | *(speculative — weakest cross-framework precedent; add only on demand)* | none strong |

> Prefer expressing fine-grained variants (`editor`, `publisher`, `citation-specialist`) as **skills** ([[agent_skills]]) bound to a core expert rather than as standalone experts, unless a deployment genuinely needs them as separate pickable roles. This keeps the roster tight per the headline finding.

## Re-assessment of the current 7

| Expert | Verdict | Reasoning |
|---|---|---|
| **scholar** | ✅ Keep — **core** | Universal researcher role; strongest cross-framework precedent |
| **developer** | ✅ Keep — **core** | Universal executor role |
| **critic** | ✅ Keep — **core** | Best-evidenced quality pattern; auto-spawn matches precedent |
| **curator** | ↘️ **Move to opt-in** | Partly redundant with SRW's **automatic memory engine** — shipping a persona for a mechanic is the anti-pattern this doc warns against. No surveyed framework ships a standalone curator. Keep for deployments wanting *curated* KB beyond passive extraction. *(confidence: medium)* |
| **designer** | ↘️ **Move to opt-in** (design tier) | Design is a real role (ChatDev `Designer`) but domain-specific, not universal — wrong fit for a *balanced* core. *(confidence: medium)* |
| **designer-interactive** | ⚠️ **Don't merge — de-duplicate** | The research suggested "merge into `designer`," but this is **not feasible**: the two extend different bases (`defaults` vs `persistent_defaults`) and mode is immutable. The pair is the platform-idiomatic way to offer one role in both modes. Action: keep both *or* pick the one mode design actually needs (likely interactive), and factor the shared persona/`design_guide` into a common file instead of duplicating it |
| **bughunter** | ↘️ Keep — **opt-in** (SE tier); **do NOT fold into `critic`** | Different tool domains *and* lifecycle: `critic` is a read-only gate auto-spawned on completion; `bughunter` is an *active* shell+browser executor that produces reproductions. Mirrors Anthropic's deliberate `code-reviewer` (read-only) vs `debugger` (Edit-enabled) split |

**Net effect:** core narrows to `scholar + developer + critic`, **adds** `writer` (+ `assistant`), and moves `curator`/`designer`/`bughunter` to opt-in — landing at a **4–5 role core**, squarely inside the validated range.

## Anti-roles — do **not** ship as experts

These duplicate framework mechanics SRW already owns; a persona would add coordination surface without benefit:

- **orchestrator / supervisor / planner** — SRW's strategic/tactical phase alternation + checkpoint/resume + stuck-detection (`src/graph.py`) already *is* Magentic-One's Orchestrator behavior (Task Ledger / Progress Ledger / re-plan on stall). *(behavioral mapping, not literal — in Magentic-One the Orchestrator is itself an LLM agent; for SRW the behavior is mechanics.)*
- **router / triage** — handled by the dispatcher + `delegation` capability.
- **memory-manager** — the automatic memory engine (Neo4j + pgvector). This is also why `curator` drops from the core.

## Structural validation of the "core + opt-in tiers" shape

Canonical rosters in the wild are **small**, and at least one major framework ships **none** (fully user-defined) — both support a tight core + tiers + user-defined experts rather than a large fixed bundle:

| System | Canonical roles |
|---|---|
| MetaGPT | 5 (Product Manager / Architect / Project Manager / Engineer / QA) |
| ChatDev | ~7 (software-company social roles) |
| Magentic-One | 5 (Orchestrator / WebSurfer / FileSurfer / Coder / ComputerTerminal) |
| GPT Researcher | 7 + Human |
| Anthropic research system | 3 (LeadResearcher / Subagents / CitationAgent) |
| CrewAI | **0 predefined** (role/goal/backstory entirely user-authored) |
| Claude Code | generic built-ins (Explore / Plan / general-purpose); domain personas are **examples to create** |

## Evidence & caveats

### What got refuted (don't rely on these)

The harness killed 3 claims (0-3 votes), all of which would have *over*-supported specialization:

1. A MetaGPT ablation showing role specialization "consistently improves" outcomes.
2. An exact ChatDev "5 named roles (CEO/CTO/programmer/reviewer/tester)" roster framing.
3. **Anthropic's headline "90.2% multi-agent beats single-agent"** result — **do not cite this figure.**

### Confidence & honesty

- The **descriptive** claims (what each framework ships) are strongly sourced (mostly primary: vendor docs, arXiv, source code) and all verified 3-0.
- The **roster recommendations** are synthesis on top — solid where grounded in precedent, **medium confidence** where they depend on SRW specifics (notably the `curator` and `designer` calls).
- **Causal evidence for role specialization is thin and confounded.** ChatDev's +9.4% and its role-removal ablation both also change task hints, so they measure "role + instructions," not persona in isolation. The cleanest signal (MAST) points toward *restraint*.

## Open questions

1. **Session variants:** beyond `assistant`, do we want session variants of `scholar`/`developer`/`writer` in the default set, or leave them opt-in? (Each is a *separate* definition given the immutable-mode constraint.)
2. **Verifier split:** is a separate `fact-checker`/`citation-specialist` worth shipping, or is the core `critic` + existing citation tooling sufficient? Anthropic deliberately split `CitationAgent`; SRW's `critic` already reviews/approves — marginal value here is unmeasured.
3. **Marginal value of the Nth role** on a *phase-based* platform that already has stuck-detection + goal-check: does any role beyond the tight core improve reliability, or mostly add inter-agent-misalignment surface (36.9% of MAST failures)? No surveyed source measures this for an architecture like SRW's.
4. **Per-role model/autonomy defaults:** an expert is persona+tools+workspace+autonomy+**model**. The literature entangles role design with model choice (ChatDev on GPT-4o; Anthropic Opus-lead/Sonnet-subagents), so good per-role model defaults are unresolved.

## Proposed implementation / next steps

Recommended order:

1. **Lock this roster** (review + sign-off on the core, the tier placements, and the current-7 verdicts).
2. **Build `writer`** — new worker expert under `config/experts/writer/` (persona, `$extends: defaults`, low-footprint tool set, no `shell_tools`/`vm_workspace`/`delegation`). Highest value, lowest risk.
3. **Build / name `assistant`** — session expert (`$extends: persistent_defaults`); likely thin if `persistent_defaults` already backs bare sessions.
4. **Re-tier the existing 7** — mark `curator`/`designer`/`bughunter` as opt-in. Mechanism TBD: the configs stay on disk; "opt-in" likely means tags/metadata + which experts a deployment surfaces by default (not deletion). Define how a deployment selects its enabled set (values overlay? a `default_roster` list?).
5. **De-duplicate `designer` / `designer-interactive`** shared prompt content (or collapse to the single mode design needs).

> Re-tiering needs a small decision the codebase doesn't yet encode: **how a deployment expresses "which bundled experts are on by default."** Today all 7 are simply present. Options: a Helm/values `defaultExperts` list, an `enabled`/`tier` field in each expert's YAML surfaced through the picker, or a system-setting. Resolve before step 4.

## Sources

Primary, load-bearing:

- **MAST** — Multi-Agent System failure taxonomy: <https://arxiv.org/abs/2503.13657>
- **GPT Researcher** multi-agent roles (Researcher/Editor/Reviewer/Reviser/Writer/Publisher): <https://docs.gptr.dev/docs/gpt-researcher/multi_agents/langgraph>
- **Magentic-One** (Orchestrator/WebSurfer/FileSurfer/Coder/ComputerTerminal): <https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/magentic-one.html>
- **Claude Code subagents** (generic built-ins; code-reviewer/debugger as examples; "one task" + least-privilege guidance): <https://code.claude.com/docs/en/sub-agents>
- **MetaGPT** (Engineer/QA among 5 roles): <https://arxiv.org/html/2308.00352v7>
- **ChatDev** (software-company social roles incl. Reviewer/Tester/Designer): <https://arxiv.org/html/2307.07924v5>
- **Anthropic** multi-agent research system (LeadResearcher/Subagents/CitationAgent; memory as a mechanic): <https://www.anthropic.com/engineering/multi-agent-research-system>
- **OpenHands** single unified default agent: <https://docs.openhands.dev/openhands/usage/agents>
- **CrewAI** (no predefined roles): <https://docs.crewai.com/en/concepts/agents>

Supporting (generator–critic / reflection literature): Self-Refine (arXiv 2303.17651), Reflexion (arXiv 2303.11366).
