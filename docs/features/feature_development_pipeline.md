---
tags:
  - feature
  - orchestration
  - pipeline
  - experts
aliases:
  - feature dev pipeline
  - feature pipeline
  - feature-development chain
related:
  - "[[verification_phase]]"
  - "[[continuous_improvement_loop]]"
  - "[[subagent_delegation]]"
  - "[[interactive_planning]]"
  - "[[project_knowledge_base]]"
---

# Feature Development Pipeline

> Five-stage agent chain that turns a free-form feature description (from a user session) into a tested implementation. Each stage is a normal job; workspace output of stage N becomes input for stage N+1. Two human checkpoints sit between stages where misalignment is cheapest to catch.

## Problem

Today, building a feature in this codebase looks like:

1. User describes the feature in a Claude Code session.
2. User prompts Claude Code to "write a feature doc, search the web, explore the codebase."
3. User reviews the doc, often asks for revisions.
4. User manually creates a job (`POST /api/jobs`) with the design as input.
5. Developer agent implements; user reviews, resumes with feedback, iterates.

The handoffs are ad-hoc. The *design* step depends on the user remembering to ask for it, and there's no separation between research (gathering options) and synthesis (picking one). Requirements are implicit — no machine-readable acceptance criteria for the developer or a reviewer to compare against. Quality gating is wholly human-driven.

This works for one-off changes but doesn't scale, and it leaves quality on the table: the developer often ships against an under-specified design, and the user catches gaps that a structured pipeline would catch earlier.

## What We Want

```
User session: "I want feature X..."
            ↓ (start_feature_pipeline tool / cockpit button)
┌──────────────────────────────────────────────────────────┐
│ Stage 1: Requirements Engineer (NEW)                     │
│   Output: output/requirements.md (EARS-style criteria)   │
│   Autonomy: review  →  user signs off before stage 2     │
└──────────────────────────────────────────────────────────┘
            ↓
┌──────────────────────────────────────────────────────────┐
│ Stage 2: Scholar (existing)                              │
│   Output: output/research.md                             │
│   May delegate parallel children (web + codebase)        │
│   Autonomy: full                                         │
└──────────────────────────────────────────────────────────┘
            ↓
┌──────────────────────────────────────────────────────────┐
│ Stage 3: Architect (NEW)                                 │
│   Output: output/design.md                               │
│   Autonomy: review  →  user signs off before stage 4     │
└──────────────────────────────────────────────────────────┘
            ↓
┌──────────────────────────────────────────────────────────┐
│ Stage 4: Developer (existing, TDD)                       │
│   Output: code + tests on a feature branch               │
│   Autonomy: review (existing default)                    │
│   On job_complete → Verification Phase auto-spawns Critic│
│                     Critic loops back with feedback      │
└──────────────────────────────────────────────────────────┘
            ↓
        Final PR / cockpit-visible artifact
```

Two new expert configs (Requirements Engineer, Architect). Three existing experts (Scholar, Developer, Critic via Verification Phase). The pipeline itself is job chaining via `POST /api/jobs`, mirroring `continuous_improvement_loop.md`.

## Why a New Doc

`continuous_improvement_loop.md` describes the *orchestration primitive* (chain jobs via REST, pass workspace output forward) for the self-improvement use case. `verification_phase.md` describes the *quality gate primitive* for any single job. This doc layers a *feature-development chain* on top of both, with no new infrastructure beyond two expert configs and a thin orchestrator script.

## Design

### Pipeline Orchestrator

External Python script `feature_pipeline.py` at project root, modeled on the `pipeline.py` from `continuous_improvement_loop.md`. Same machinery: creates jobs via `POST /api/jobs`, polls for completion, reads workspace output files, injects them as the next stage's input.

```
feature_pipeline.py
  --description "Free-form feature description from session"
  --target-repo  <git url or repo name>
  --target-branch feature/<slug>
  --pipeline-id  <uuid>                   # parent_job_id chain root
  [--skip-research]                       # jump from req → design
  [--skip-verification]                   # skip critic at stage 4
  [--auto-approve-checkpoints]            # full autonomy, no pauses
  [--max-tokens 500000]                   # hard budget cap
  [--max-verification-iterations 3]       # cap stage 4 critic loops
```

The script writes a manifest:

```
pipeline_results/<pipeline_id>/
  manifest.json           # stage statuses, job IDs, timestamps
  stage_1_requirements/   # job_id.txt + symlink to job workspace
  stage_2_research/
  stage_3_design/
  stage_4_developer/
  stage_4_verification/   # critic job(s) — may be multiple iterations
```

Each stage block holds the stage's `job_id.txt` and a symlink to the job's workspace, so the user can read artifacts without leaving the pipeline view.

### Inter-Stage Data Flow

| Carrier | Used For |
|---------|----------|
| Workspace files (`output/*.md`) | Stage deliverables — requirements, research, design |
| `context` JSONB on next job | Pipeline metadata: `pipeline_id`, `parent_pipeline_stage`, `previous_stage_job_id`, `target_repo`, `target_branch` |
| `parent_job_id` on next job | Structural — stages 2..4 reference stage N-1; verification critic references stage 4 |
| `instructions` field on next job | Stage-specific brief that says "read `previous_stage_job_id`'s `output/<file>.md` via MCP and proceed" |

Workspace files are the canonical source of stage outputs — they're inspectable in the cockpit, survive pipeline restart, and are human-readable. Context JSONB carries only the wiring metadata.

### Stage 1: Requirements Engineer

**New expert.** `config/experts/requirements_engineer/`.

Role: convert a free-form feature description into a structured requirements document.

Output (`output/requirements.md`):

```markdown
# Feature: <name>

## User Need
<one-paragraph problem statement from the session description>

## Acceptance Criteria (EARS)
1. WHEN <event> THE SYSTEM SHALL <response>
2. WHILE <state> THE SYSTEM SHALL <response>
3. IF <condition> THEN THE SYSTEM SHALL <response>
...

## Out of Scope
- <explicit non-goals>

## Open Questions
- <questions the user must answer before research can proceed>
```

Tools: `web_search` (for terminology / domain knowledge), MCP tools (to look at sibling features in `docs/features/`), workspace edit tools.

Autonomy: `review`. The user must approve the requirements doc before stage 2 starts — this is the cheapest place to catch misalignment.

### Stage 2: Scholar

**Existing expert.** Reuses `config/experts/scholar/` unchanged.

Pipeline-specific input (via `instructions` field on the created job):

> Read `<requirements.md path via MCP>`. Research:
> 1. Best practices for implementing this in our stack (Python/FastAPI/LangGraph backend; Angular frontend).
> 2. Comparable implementations in adjacent open-source projects.
> 3. Current codebase state — relevant existing modules, conflicts, prior art in `docs/features/`.
> Produce `output/research.md` with: options considered, tradeoffs, recommended approach, citations.

Scholar may use `delegate_work` to spawn parallel children (one for web research, one for codebase exploration) — already supported via `subagent_delegation.md`.

Autonomy: `full`. No user pause; research is read-only and cheap to redo if downstream stages find gaps.

### Stage 3: Architect

**New expert.** `config/experts/architect/`.

Role: synthesize requirements + research into an implementation design doc. Distinct from the existing Critic (which is tuned for adversarial review) — the Architect is a creative synthesizer.

Output (`output/design.md`): same shape as the docs in `docs/features/`, including:
- Problem statement (from requirements)
- Concept / Overview
- Architecture sketch
- What exists vs. what's new
- Implementation roadmap (phased)
- Resolved design decisions table
- Open questions

Tools: MCP (read sibling feature docs as exemplars), workspace edit, web_search for terminology.

Autonomy: `review`. Second human checkpoint. The user signs off on the design before the developer starts — the second-cheapest place to catch misalignment, before any code is written.

### Stage 4: Developer

**Existing expert.** `config/experts/developer/` unchanged.

Pipeline-specific input:

> Read `<design.md path via MCP>` and the linked `requirements.md`. Implement on branch `<target-branch>`. Use TDD: red-green-refactor. Land commits per the phased roadmap in the design.

Autonomy: existing default for the developer expert (`review`).

### Stage 4b: Verification Phase

**Existing design** — `verification_phase.md`. No new code on top of that doc.

When the Developer job calls `job_complete`, the auto-trigger spawns a Critic job. The Critic reads `requirements.md` AND `design.md` (not just one) and verifies the implementation against both. On failure, the Developer resumes with feedback. This loop iterates inside stage 4; the pipeline does not advance until verification passes (or the verification-iterations cap is hit).

### Pipeline Trigger

**Primary surface: session tool** — `start_feature_pipeline(description: str, target_repo: str, target_branch: str)`.

Available to interactive sessions (`persistent_graph`). When called, the tool POSTs to a new orchestrator endpoint (`POST /api/feature-pipelines`) which spawns `feature_pipeline.py` as a tracked subprocess, returning the `pipeline_id`. The session continues; the user monitors the pipeline via the cockpit job-hierarchy view (which uses `parent_job_id` from `verification_phase.md`).

**Secondary surface: CLI** — direct invocation of `feature_pipeline.py` for power users and debugging.

**Tertiary surface: cockpit button** — "Start feature pipeline" on a session detail page. Same backend as the session tool.

### Human Checkpoints

Two by default — after Requirements and after Design. Implemented via existing `autonomy: review`:

- Stage finishes → `pending_review` status.
- Cockpit shows pipeline-aware approval UI (uses the same approve/resume mechanism as today).
- User approves → pipeline orchestrator detects approval (polling `GET /api/jobs/{id}` for status transition) → submits next stage.
- User resumes with feedback → stage re-runs with feedback → user re-reviews.

For "I trust the pipeline, just run it end-to-end" cases: `--auto-approve-checkpoints` on the orchestrator script, or a per-pipeline setting.

### Cost and Opt-In

A full pipeline is 4–5+ jobs (5+ when verification loops). Worst case: requirements + research + design + dev + verification × N. This is expensive in tokens and wall-clock; the trigger is **always explicit** — no automatic kickoff from any state.

The orchestrator script enforces hard budget caps per pipeline (`--max-tokens`, `--max-verification-iterations`) with sensible defaults (500k tokens total, 3 verification iterations).

## What Already Exists

| Component | Source |
|-----------|--------|
| Scholar, Critic, Developer expert configs | `config/experts/` |
| Subagent delegation primitive (`delegate_work`) | `[[subagent_delegation]]` (implemented) |
| `parent_job_id` column + cockpit hierarchy view | `[[verification_phase]]` (Phase 1) |
| Verdict tools (`approve_job`, `return_job_with_feedback`) | `[[verification_phase]]` (Phase 1) |
| Verification trigger after `job_complete` | `[[verification_phase]]` (Phase 3) |
| Pipeline-script + REST chain pattern | `[[continuous_improvement_loop]]` |
| Autonomy levels with `review` checkpoint | `[[interactive_planning]]` (implemented) |
| Job-context JSONB merge helpers | `merge_job_context()` in `orchestrator/database/postgres.py` |
| MCP job introspection (read sibling job's workspace) | `orchestrator/mcp/` |

## What Needs to Be Built

1. **Requirements Engineer expert** — `config/experts/requirements_engineer/`
   - `config.yaml` (extends `defaults.yaml`, model preset, tool categories)
   - `instructions.md` (EARS criteria template, examples)
   - `persona.md`

2. **Architect expert** — `config/experts/architect/`
   - Same shape as above.
   - Instructions emphasize: synthesis (not critique), output matches `docs/features/` shape, must read both `requirements.md` and `research.md`.

3. **Pipeline orchestrator script** — `feature_pipeline.py` at project root
   - CLI args per the spec above.
   - Polls job status, reads workspace artifacts, chains next stage.
   - Writes manifest under `pipeline_results/<id>/`.
   - Hard budget caps (`--max-tokens`, `--max-verification-iterations`).

4. **Session tool** — `start_feature_pipeline`
   - New tool in `src/tools/pipeline/` (new category).
   - Implementation: HTTP call to `POST /api/feature-pipelines`.

5. **Orchestrator endpoint** — `POST /api/feature-pipelines`
   - Spawns `feature_pipeline.py` as a tracked subprocess.
   - Returns `{pipeline_id, root_job_id}`.
   - Companion: `GET /api/feature-pipelines/{id}` for status.

6. **Cockpit UI**
   - "Start feature pipeline" button on session detail page (calls the session tool path).
   - Pipeline-hierarchy view reusing the `parent_job_id` rendering from `verification_phase.md` Phase 4.
   - Checkpoint approval UI (same as existing pending-review UI, labeled "Approve to continue pipeline").

## Implementation Roadmap

Phases are decoupled — you can stop after any phase and still have something useful.

```
Phase 1: New expert configs (no orchestration yet)
├─ Step 1: Create config/experts/requirements_engineer/
├─ Step 2: Create config/experts/architect/
└─ Step 3: Manual smoke test — submit a one-off job with each expert,
           verify outputs match the templates

Phase 2: Verification phase dependency
└─ Step 4: Land verification_phase.md Phases 1–3 (parent_job_id,
           verdict tools, trigger). Independent design doc; the
           pipeline depends on it for stage 4b.

Phase 3: CLI pipeline orchestrator
├─ Step 5: Write feature_pipeline.py with CLI args
├─ Step 6: Manifest writer + workspace symlink layout
├─ Step 7: Budget caps + convergence handling
└─ Step 8: End-to-end test from CLI with a real feature description

Phase 4: Session-tool trigger
├─ Step 9:  POST /api/feature-pipelines endpoint (spawns the script)
├─ Step 10: start_feature_pipeline tool, register in tools/registry
└─ Step 11: Add to persistent_defaults.yaml allowed tools

Phase 5: Cockpit UI (optional but high-value)
├─ Step 12: Pipeline-hierarchy view (extends parent_job_id rendering)
├─ Step 13: "Start feature pipeline" button on session detail
└─ Step 14: Checkpoint approval UI variant
```

**Dependencies:**

```
Step 1, 2 ──→ Step 3 (configs must exist before smoke test)
Step 3 ──→ Step 5 (smoke-tested experts before chaining them)
Step 4 ──→ Step 5 (verification mechanism must exist for stage 4b)
Step 5–8 ──→ Step 9 (CLI proven before wrapping in an endpoint)
Step 9 ──→ Step 10, 13 (endpoint exists before consumers)
verification_phase Phase 4 ──→ Step 12 (parent_job_id rendering)
```

Phases 1 and 2 can land in parallel. Phase 3 (CLI orchestrator) is the keystone — once it works from a terminal, Phases 4 and 5 are wrappers on top.

## Resolved Design Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| Architect or repurpose Critic for design synthesis? | New Architect expert | Critic is tuned for adversarial review. Synthesis (creative, options-into-design) is a different stance. Don't conflate prompts. |
| Dedicated Tester expert? | No — rely on Verification-Phase Critic | Developer already does TDD red-green-refactor. Critic catches gaps against requirements + design. Adding a third reviewer is feature creep until a real gap surfaces. |
| Trigger surface | Session tool primary, CLI underneath, cockpit button as wrapper | Matches the natural workflow ("describe in session → kick off pipeline from session"). CLI is the implementation backbone; cockpit button is a UI convenience. |
| Inter-stage data flow | Workspace files for content, context JSONB for metadata | Workspace files are inspectable, survive restart, human-readable. JSONB carries only wiring. Matches CI-loop precedent. |
| Human checkpoints | Default: pause after Requirements and after Design | Highest-leverage places to catch misalignment. Both stages are cheap; pausing after Dev would mean catching expensive mistakes late. |
| Architect's `design.md` destination | Pipeline workspace by default; Developer stage may commit it to target repo's `docs/features/` | Keeps design alongside the feature work. Matches how `docs/features/` is populated today. |
| Default opt-in | Always explicit kickoff; no automatic chaining | A 5-job chain is expensive. The user must own the decision to start one. |

## Open Questions

These are smaller and can be resolved during implementation rather than blocking the doc:

1. **EARS strictness** — do we require strict EARS syntax in requirements, or accept structured-but-flexible prose? Likely flexible for v1; tighten if developer/critic struggle with ambiguity.
2. **Scholar config preset** — does Scholar need a `design_research` preset (different model, different prompts) versus general research? Try the existing config first; specialize if outputs feel under-targeted.
3. **Cross-cutting features** — features that span DB + backend + frontend may need multiple parallel developer subagents within stage 4. The `delegate_work` primitive handles this, but the Architect's design needs to flag the parallelization opportunity. TBD whether to encode this in the Architect's template.
4. **Design doc indexing** — once the Architect's `design.md` lands in `docs/features/`, should the Curator (`[[project_knowledge_base]]`) ingest it automatically? Likely yes — frees future pipelines to reference prior designs.
5. **Pipeline-as-a-job vs. pipeline-as-a-script** — v1 is a script for simplicity. If operational issues surface (need to pause/resume the whole pipeline, scale across orchestrator replicas), promote to a first-class orchestrator concept later.
6. **Failure handling between stages** — if Scholar finds nothing useful, does the pipeline stop or proceed? v1: proceed with whatever Scholar produced; the Architect and Verification Phase will catch hollow research downstream.

## Related

- `[[verification_phase]]` — quality gate inside stage 4; the pipeline does not advance until verification passes.
- `[[continuous_improvement_loop]]` — orchestration precedent (job-chain via REST API, workspace-output-as-next-input). Same mechanism, different chain.
- `[[subagent_delegation]]` — primitive Scholar and Developer can use *within* a stage for parallel work.
- `[[interactive_planning]]` — autonomy levels (`review`) power the human checkpoints between stages.
- `[[project_knowledge_base]]` — Curator can ingest the Architect's `design.md` as a project-knowledge note for future pipelines to reference.
