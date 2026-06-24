---
tags:
  - feature
  - orchestration
  - projects
  - experts
  - self-improvement
  - autonomy
aliases:
  - project loop
  - project cycle
  - autonomous project loop
  - self-improvement cycle
  - innovation cycle (productized)
related:
  - "[[verification_phase]]"
  - "[[subagent_delegation]]"
  - "[[feature_development_pipeline]]"
  - "[[automations]]"
  - "[[project_knowledge_base]]"
  - "[[projects]]"
  - "[[continuous_improvement_loop]]"
---

# Project Self-Improvement Loop

> Turn the README's four-agent "self-improving loop" from a vision into a feature you can start from a project. A user creates a project, gives it a goal, attaches datasources, picks a model, sets a budget, and presses **Start**. The orchestrator then runs jobs **one at a time, continuously** — rotating Scholar → Critic → Execution — until the iteration count or deadline runs out. Agents coordinate through the **project knowledge base as a shared blackboard**. No human in the loop; you set the direction, the system iterates overnight.

## Status

**Phases 1–3 built and k3d-verified (2026-06-23/24); uncommitted on `develop`.** Phase 4 (token-budget stop) and the Critic goal-met early-stop are deferred. The loop is usable **end-to-end from the cockpit**: create a project, set a goal, attach datasources, open the **Loop** tab, press Start. v1 remains **experimental** — the design and rationale below are the original plan; per-phase build status, the as-built deviations, and the k3d verification are in [Implementation Roadmap](#implementation-roadmap). The biggest open risk is unchanged and now sharpened by a real run: **whether the agents actually use the KB blackboard** (the gemma smoke run wrote zero KB notes — see [Open Questions](#open-questions)). Issues and optimizations found during real test runs are logged in [`loop_review.md`](../loop_review.md).

## Problem

The README's flagship milestone is the self-improving loop: Scholar generates ideas, Critic filters, Developer builds, Curator distils — "the loop never stops." Today every **edge** of that cycle exists as an orchestrator lifecycle hook (scholar pre-research `_spawn_scholar_subjob`, the critic verification loop `_trigger_verification_on_complete`, curator `_trigger_curation_final_pass`), and the four expert roles all ship. What's missing is the **continuous driver** that strings them into a self-running cycle aimed at a goal.

The closest existing design is `automations.md`'s "Self-Improving Feature Loop" — but that's expressed as generic event-trigger rules (v0.5, dormant: `event_filter` column unused, no `event_dispatcher.py`) and is user-scoped template plumbing, not a turnkey project experience. The older `continuous_improvement_loop.md` is stale (names experts that don't exist, calls for an unbuilt `pipeline.py`).

We want the turnkey version: **create project → set goal → attach datasources → Start the loop → let it run**, bounded by a budget, observable and killable from the cockpit.

## What the research says

A deep, adversarially-verified literature sweep (Reflexion, MAST, the Darwin-Gödel Machine, the independent evaluation of Sakana's "The AI Scientist", broadcast-blackboard work; 15 claims confirmed / 10 refuted) produced findings that **validate our core shape and sharpen four design points**. Each item below is tagged with its design consequence.

**Validated as-is:**

- **KB-as-blackboard is the right coordination model.** Blackboard designs (agents read all prior work, build incrementally, self-select whether to act) beat master-slave task-assignment by 13–57% relative and can replace per-agent memory (Salemi et al., ICLR 2026 submission; LbMAS preprint). → *Keep the single shared project KB, read-at-start / write-at-end.*
- **The propose→critique→execute rotation is a real anti-stall mechanism.** Reflexion sustained +22% on AlfWorld across 12 trials where a non-reflective ReAct baseline plateaued at trials 6–7. The mechanism is reflective feedback carried forward between trials. → *Our per-job KB read/write IS that reflective buffer — but only if the Critic's critique is written back as actionable text the next job reads, not a bare pass/fail.*
- **A consecutive-failure cap targets the dominant failure mode.** The single largest documented cause of loop failure is **compounding execution errors, not bad ideas** — 42% of AI Scientist experiments failed purely on coding errors. → *Keep the consecutive-failure cap; treat execution failure as the expected steady state, not an exception.*
- **Explicit, multi-criteria termination is correct.** Don't rely on a single self-declared "done." Combine a hard cap, a goal-judgment stop, and a fallback (LbMAS: max-iteration OR a "decider" agent, with a tie-break). → *Our max-iteration + run-until-date + consecutive-failure caps are the hard stops; add the Critic as the "decider" and a deterministic fallback.*

**Sharpened by the research (the valuable part):**

1. **Never let the loop self-declare victory by self-grading — anchor "done" to external acceptance criteria.** LLM self-evaluation of novelty/progress is near-random: the AI Scientist marked **100%** of its ideas "novel," misclassifying textbook techniques, because it keyword-matched instead of grounding against prior work. → *The project needs a machine-checkable-as-possible **Definition of Done / acceptance criteria** stored in the KB. The Critic checks the goal against THAT, grounded in the KB and datasources — never against the agent's own confidence.*
2. **A separate Critic does not automatically filter — it must verify the goal, not the surface.** MAST (1,642 traces) found verifiers "perform only superficial checks… such as checking if the code compiles or if there are leftover TODOs." Goal-level verification added **+15.6%**; better role-spec prompts **+9.4%** (both partial, not total). → *Give the Critic an explicit goal-level rubric tied to the project's acceptance criteria, plus a strong role spec. Expect partial gains.*
3. **Pure retry can't escape a wrong-region-of-search-space — add diversity pressure and an archive.** Reflexion can "succumb to non-optimal local minima." The DGM hit **50% vs 23%** on SWE-bench by keeping an **archive of diverse past states** instead of greedily keeping latest-best, because "many paths to innovation traverse lower-performing nodes" and one bad self-modification otherwise blocks all future progress. → *The KB must persist a **lineage/archive** of approaches including abandoned/tried-and-rejected ones, so the loop branches back from a dead end instead of being permanently degraded, and the Scholar is told what's already been tried so it proposes genuinely distinct candidates.*
4. **Guard the specific high-frequency loop failure modes.** MAST's top individual modes are **step repetition 15.7%** (#1), reasoning-action mismatch 13.2%, **termination-unawareness 12.4%**, disobey-spec 11.8%. → *Concrete guardrail checklist: (a) cross-job repetition detection (don't re-propose an idea already in the KB's tried list), (b) every job re-reads and restates the goal + acceptance criteria, (c) stop conditions re-checked and stated in every kickoff since agents are frequently "unaware of termination conditions."*

**Caveats we are explicitly accepting** (from the report):

- **Evidence is concentrated in the coding domain and on bounded benchmarks, not true multi-day unattended runs.** Generalization to overnight knowledge-work / prose execution is a reasoned inference, not a measured result. v1 is an experiment, scoped accordingly.
- **Goodhart / reward-hacking is real for self-improvement specifically.** The DGM was caught faking test logs; Reflexion logs flaky-test false-positives. Any self-grading reward is an attack surface → acceptance criteria should be as external/grounded as the goal allows (real tests for code; datasource-checkable facts for research).
- **Refuted, do not rely on:** a clean "41–86% multi-agent failure rate" headline, the "8%-chars/iteration = stagnation" framing, a specific "+13.4-pt Critic-Actor" gain, and several blackboard-topology specifics. Treat "which exact write-back topology is best" and "self-selection is THE cause" as open.

Full report (sources, votes, open questions) archived alongside this initiative; the four research **open questions** are folded into [Open Questions](#open-questions) below — they are exactly the things our overnight runs should measure.

## Concept

```
            ┌──────────────────────── project_loops row (running) ───────────────────────┐
            │  goal · acceptance_criteria · model · remaining_iterations · run_until       │
            │  role_sequence=[scholar,critic,<execution>] · seq_index · consec_failures    │
            └───────────────────────────────────┬─────────────────────────────────────────┘
                                                 │ spawn next job (bare), inject kickoff
                                                 ▼
   ┌──────────┐  read KB   ┌──────────┐        ┌────────────┐
   │ SCHOLAR  │ ─blackboard─►│  CRITIC  │ ──────►│ EXECUTION  │   (developer | writer | default | …)
   │ propose  │            │ select/  │        │ build/ship │
   │ N distinct│ write KB  │ verify vs │ write  │ self-verify│ write KB
   │ approaches│◄──────────│ acceptance│  KB    │ then done  │──────┐
   └──────────┘            │ = decider │        └────────────┘      │
        ▲                  └──────────┘                              │
        │                                                           job reaches terminal
        │     _advance_project_loop() hook: iterations-- ;          │
        └───── check stops ; rotate role ; spawn next  ◄────────────┘
              (or STOP: budget exhausted / goal met / failure cap)
```

- **One job at a time, sequentially.** Structurally enforced: the next job is spawned only when the current one reaches terminal state. Deterministic KB hand-off (job N's writes land before N+1 reads), and one loop ≈ 1–2 concurrent agents (plus any delegation bursts), which fits a single MiniMax subscription's ~6–7 agents.
- **The KB is the only shared state.** Agents leave notes for each other; every project job already gets the KB injected each call (`project_knowledge_base.md`). No new memory system.
- **Role rotation, configurable.** Default `[scholar, critic, developer]`; swap `developer` for `default` (General Secretary, all tools — writes prose today) or a future `writer` to make it a general autonomous-project engine, not a coding toy.
- **Budget-bounded, not goal-gated.** A goal is recommended but optional; the loop is bounded by `max_iterations` AND/OR `run_until` date, plus a `consecutive_failure` cap. The goal steers; the budget stops.

## Design

### Data model — `project_loops`

A new table (migration `0035_project_loops.sql`). Per the [DB migration runbook](../db_migration.md); `schema.sql` stays frozen.

```sql
CREATE TABLE project_loops (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id            UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id              UUID REFERENCES users(id) ON DELETE SET NULL,
    status                TEXT NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running','paused','stopped','completed','failed')),

    -- Steering
    goal                  TEXT,                 -- snapshot of projects.goal at start (editable)
    acceptance_criteria   TEXT,                 -- Definition of Done — what the Critic checks against
    user_prompt           TEXT,                 -- optional extra steering (the "part-user" half)
    model                 TEXT,                 -- → config_override.llm.model on every job
    role_sequence         JSONB NOT NULL        -- e.g. ["scholar","critic","developer"]
                          DEFAULT '["scholar","critic","developer"]'::jsonb,

    -- Budget / stop conditions
    max_iterations        INT,                  -- NULL = unbounded by count (must then have run_until)
    remaining_iterations  INT,
    run_until             TIMESTAMPTZ,          -- NULL = unbounded by time (must then have max_iterations)
    max_consecutive_failures INT NOT NULL DEFAULT 3,

    -- Live state
    seq_index             INT NOT NULL DEFAULT 0,   -- position in role_sequence
    current_job_id        UUID REFERENCES jobs(id) ON DELETE SET NULL,
    total_jobs_run        INT NOT NULL DEFAULT 0,
    consecutive_failures  INT NOT NULL DEFAULT 0,
    last_error            TEXT,
    stop_reason           TEXT,                 -- why it ended (budget|deadline|failures|goal_met|user)

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One active loop per project (v1 simplification)
CREATE UNIQUE INDEX project_loops_one_active
    ON project_loops (project_id) WHERE status IN ('running','paused');
```

A CHECK enforces "at least one of `max_iterations` / `run_until` is set" so a loop can never be unbounded on both axes — a hard floor under runaway.

### The advance hook — rides the existing completion fan-out

No new engine, no polling. When a job reaches terminal state, `complete_job` already fans out to lifecycle hooks at `orchestrator/main.py:9610-9633`. Add one more, beside `_trigger_curation_final_pass`:

```python
# orchestrator/main.py, in the completion fan-out
await _advance_project_loop(job, result, actions)
```

```python
async def _advance_project_loop(job, result, actions):
    loop_id = (job.get("context") or {}).get("loop_id")
    if not loop_id:
        return
    loop = await get_project_loop(loop_id)
    if not loop or loop["status"] != "running":
        return                                   # paused/stopped → do nothing
    if str(job["id"]) != str(loop["current_job_id"]):
        return                                   # idempotency: only the current job advances

    failed = bool(result.get("error")) or job.get("status") == "failed"
    consec = loop["consecutive_failures"] + 1 if failed else 0
    remaining = loop["remaining_iterations"] - 1 if loop["remaining_iterations"] is not None else None

    stop = _loop_stop_reason(loop, remaining, consec)   # budget | deadline | failures | None
    if stop:
        await set_project_loop_stopped(loop_id, stop, consec)
        actions.append(f"project loop {loop_id} stopped: {stop}")
        return

    next_index = (loop["seq_index"] + 1) % len(loop["role_sequence"])
    next_role  = loop["role_sequence"][next_index]
    kickoff    = build_loop_kickoff(loop, role=next_role, iteration=loop["total_jobs_run"] + 1)
    child = await _create_loop_job(loop, role=next_role, kickoff=kickoff)
    await advance_project_loop_row(loop_id, seq_index=next_index, current_job_id=child["id"],
                                   remaining=remaining, consecutive_failures=consec,
                                   total_jobs_run=loop["total_jobs_run"] + 1)
    actions.append(f"project loop {loop_id} → {next_role} job {child['id']}")
```

**Safety-net tick (thin, optional Phase 2):** a 60s sweep (mirror `cron_dispatcher.py`) that finds `running` loops whose `current_job_id` is terminal-but-didn't-advance (a missed hook) or stuck/offline past a timeout, and nudges/cancels. Core advance does not need it; it's belt-and-suspenders for unattended overnight reliability.

### Loop jobs run "bare"

The loop **is** the orchestration, so each spawned job must NOT trigger the per-job lifecycle hooks — otherwise they fight the loop (a loop job would auto-spawn its own verification critic that can resume it, and scholar-pre-research would prepend a second scholar). Every loop job is created with:

```python
config_override = {
    "llm": {"model": loop["model"]},
    "verification": {"enabled": False},   # the explicit Critic role IS the gate
    "scholar":      {"enabled": False},   # the explicit Scholar role IS the research
    "autonomy": "full",                   # never pause mid-loop for human review
}
context = {"loop_id": loop_id, "loop_role": role, "loop_iteration": n}
```

`curator` is left **off** in v1 too (agents self-write to the KB per the kickoff contract); a periodic curator pass can be added to the rotation later if KB hygiene needs it.

### Kickoff assembler — `build_loop_kickoff()`

Part-system, part-user. Assembled per job, **role-aware**, and (research findings 1, 2, 4) goal-anchored + termination-aware:

```
You are running as one step in a CONTINUOUS, UNATTENDED improvement loop on this project.
Other agents run before and after you. You coordinate ONLY through the project knowledge
base — it is your shared memory. READ IT FIRST, WRITE BACK what matters before you finish.

PROJECT GOAL:        «loop.goal»
DEFINITION OF DONE:  «loop.acceptance_criteria»          ← what "finished" actually means
LOOP BUDGET:         iteration «n»; «remaining» left / runs until «run_until».
                     Do not assume you must finish the whole goal — make ONE solid increment.

Before doing anything: restate the goal in one line and check the KB for (a) what's already
done, (b) what's been TRIED AND REJECTED (do not re-propose it), (c) the current open backlog.

YOUR ROLE THIS ITERATION: «role-specific block»

When done, write to the KB: what you did, what you learned, what should happen next, and
(if you closed/changed an approach) move it in the tried/lineage record.

«optional user_prompt steering»
```

Role-specific blocks (research-tuned):

- **Scholar (init / iteration 0 gets the "propose a range" variant):** "Propose several *genuinely distinct* approaches toward the goal — not variations of one. Check the KB's tried/rejected list first so you don't repeat a dead end. Write each as a `proposal` note with a one-line thesis and why it's different."
- **Critic (the decider):** "Select/prioritise among open proposals **against the Definition of Done**, not your own confidence. Verify claimed progress at the *goal* level, not surface checks (don't approve because it compiles / has no TODOs). Write a `verdict` note: chosen next action + explicit rationale + acceptance check. If the goal's Definition of Done is genuinely met, say so explicitly and why (this is the goal-met stop signal)."
- **Execution (configurable):** "Implement the Critic's chosen action. **Validate your own work before declaring done** (for code: run/test it; for writing/research: check it against the acceptance criteria and datasources). Record what you shipped and any follow-ups in the KB."

### KB blackboard — structured note kinds

The KB already does hybrid-search retrieval (bounded top-K, so per-job context stays bounded — research finding 10). The loop just standardises note **kinds** so agents leave legible trails for each other (research findings 3, 4):

| Kind | Written by | Purpose |
|------|-----------|---------|
| `definition_of_done` | set at start (user/AI) | the acceptance criteria the Critic checks against |
| `proposal` | Scholar | a distinct candidate approach |
| `verdict` | Critic | chosen action + rationale + acceptance check |
| `tried` / `rejected` | Critic / Execution | lineage of attempted+abandoned approaches (so they're not re-proposed) |
| `progress` | Execution | what shipped this iteration + follow-ups |
| `lesson` | any | reflective text the next job should read |

This is convention layered on the existing KB, not new storage. The `tried`/lineage record is the research-backed archive (finding 3) that lets the loop branch back instead of being blocked by one bad iteration.

### Stop conditions, the decider, and the fallback

Hard stops (re-checked every advance): `remaining_iterations <= 0` → `budget`; `now >= run_until` → `deadline`; `consecutive_failures >= max_consecutive_failures` → `failures`. Soft stop: the Critic explicitly signals the Definition of Done is met → `goal_met` (the "decider," research finding/termination). On any stop, set status, write `stop_reason`, and leave the KB + job history intact for human review (the deterministic fallback — surface state, don't silently vanish).

### Guardrails (the MAST checklist, research finding 4)

- **Repetition:** within a job, the existing fingerprint-based stuck detection already applies. Across jobs, the kickoff forces a read of the `tried`/`rejected` list before proposing — cross-job repetition guard without new infra.
- **Termination awareness:** every kickoff states the remaining budget and that one increment ≠ whole goal.
- **Spec drift:** every kickoff makes the agent restate the goal + Definition of Done before acting.
- **Cost runaway:** bounded on both axes by construction (the CHECK); the model is pinned per loop; one-job-at-a-time caps concurrency. (Token-budget stops are deferred — they need the LLM gateway, which is dark on dev; cycle-count + wall-clock + failure-cap are the v1 stops.)

### API surface

```
POST   /api/projects/{id}/loop          # start: body = {model, role_sequence?, max_iterations?,
                                         #   run_until?, acceptance_criteria?, user_prompt?,
                                         #   max_consecutive_failures?}. Spawns iteration 0 (scholar).
GET    /api/projects/{id}/loop          # current loop state + counters + current job
POST   /api/projects/{id}/loop/pause    # let current job finish; don't spawn next
POST   /api/projects/{id}/loop/resume   # spawn next from where it paused
POST   /api/projects/{id}/loop/stop     # terminal; current job finishes naturally
GET    /api/projects/{id}/loop/jobs     # the loop's spawned jobs (WHERE context->>'loop_id' = …)
```

Reuses Keycloak/MCP auth + `require_project_member`. The loop's jobs are normal project jobs — they already render in the project job list and are dispatched by the existing auto-assign loop; the controller only creates jobs, it never manages agents.

### Cockpit — "Loop" tab on the project

Thin, because the spawned jobs already have rich detail views. The tab needs: model picker (from `/api/models`), role-sequence editor (default `scholar→critic→developer`, presets for "Build" / "Write" / "Research"), `max_iterations` + `run_until` inputs, optional acceptance-criteria + steering textareas, **Start / Pause / Stop**, and a live readout: status, iteration k of N (or time left), current role + job link, consecutive-failure count, and a feed of the loop's jobs + recent KB notes.

## What exists vs. what's new

| Component | Status | Source |
|-----------|--------|--------|
| Four expert roles (scholar, critic, developer, curator) | Exists | `config/experts/` |
| General-execution role for non-code goals | Exists | `config/defaults.yaml` (General Secretary); `writer` planned in `default_expert_roster.md` |
| Project goal + KB + datasource attach | Exists | `projects.goal` (`schema.sql:354`), `project_knowledge_base.md`, `link_datasource_to_project` |
| KB blackboard tools (`kb_write`/`kb_search`, injected each call) | Exists | `src/tools/knowledge/`, `src/core/knowledge_injection.py` |
| Job creation with project + model + config_override | Exists | `JobCreate` (`main.py:4231`); model via `config_override.llm.model` |
| Completion-hook fan-out (where advance plugs in) | Exists | `complete_job` → `main.py:9610-9633` |
| Auto-assign dispatch of created jobs | Exists | orchestrator dispatcher |
| `project_loops` table + helpers | ✅ Built | migration `0035_project_loops.sql`; `postgres.py` (create/get/get_active/list/update + `claim_project_loop_advance` + `list_running_project_loops`) |
| `_advance_project_loop` hook (+ `_spawn_loop_job`, `_resume_project_loop`) | ✅ Built | `orchestrator/main.py` (in the completion fan-out) |
| `build_loop_kickoff` + bare-job creation | ✅ Built | `orchestrator/services/project_loops.py` |
| Start/get/pause/resume/stop/jobs API | ✅ Built | `orchestrator/routers/project_loops.py` |
| Safety-net sweeper | ✅ Built (Phase 2) | `orchestrator/services/project_loop_sweeper.py` |
| Cockpit Loop tab | ✅ Built (Phase 3) | `cockpit/.../project-detail/project-loop.component.ts` + `api.service.ts`/`api.model.ts` + project-detail wiring |

## Implementation Roadmap

```
Phase 1 — Headless loop (the keystone; verifiable on k3d)
├─ Migration 0035_project_loops.sql + postgres.py CRUD/advance helpers
├─ build_loop_kickoff() + _create_loop_job() (bare config_override)
├─ _advance_project_loop() in the completion fan-out
├─ POST /api/projects/{id}/loop (+ get/pause/resume/stop)
└─ Verify: start a loop on a throwaway research goal ("research & summarise X,
   5 iterations"), watch scholar→critic→execution rotate via kubectl logs +
   the project job list; confirm it STOPS on budget and writes stop_reason.

Phase 2 — Reliability + steering quality
├─ Safety-net sweep (missed-hook / stuck-job detection)
├─ Definition-of-Done plumbing + Critic decider goal-met stop
├─ tried/rejected lineage convention + repetition guard in kickoff
└─ Verify: overnight run on a small real goal; inspect for drift/repetition.

Phase 3 — Cockpit
├─ Loop tab (model, role-sequence, budgets, Start/Pause/Stop, live readout)
└─ Role-sequence presets (Build / Write / Research)

Phase 4 — Hardening (post-experiment, as needs surface)
├─ Token-budget stop (gated on LLM gateway being enabled)
├─ Periodic curator pass in the rotation (KB hygiene)
├─ Multiple concurrent loops per project / cross-project pooling limits
└─ Optional: Scholar fan-out via delegate_work for real proposal diversity
```

Phase 1 is the whole bet — once a loop rotates roles and stops cleanly from the API, the rest is wrappers. Match the project's Plan→Develop→Verify loop: verify each phase on k3d before it ships.

### Build status (2026-06-24)

**Phases 1–3 built and k3d-verified; uncommitted on `develop`.**

| Phase | Status |
|-------|--------|
| 1 — headless loop | ✅ done, k3d-verified |
| 2 — reliability (sweeper + atomic-claim advance) | ✅ done, k3d-verified |
| 3 — cockpit Loop tab | ✅ done, k3d-verified |
| 4 — token-budget stop · goal-met early-stop · curator pass · scholar fan-out | ⏳ deferred |

### As-built deviations from the plan

- **GET `/loop` returns the latest loop** (active → else most-recent terminal), not active-only, so the cockpit can show the morning-after outcome (status + stop_reason). `pause`/`resume`/`stop` still target the *active* loop.
- **Atomic advance claim** (`claim_project_loop_advance` — a conditional `current_job_id → NULL` UPDATE) was added so the completion hook and the Phase-2 sweeper can both call `_advance_project_loop` without double-spawning. Not in the original plan; required once the sweeper existed.
- **`_resume_project_loop`** re-runs the advance that was suppressed if the in-flight job reached a terminal state while the loop was paused.
- **Datasources (Option A)** — `create_loop_job` explicitly links the project's datasources to each loop job (mirrors the cockpit picker; resolution stays explicit-only), giving the execution role repository continuity. Backend auto-attach for project jobs ("Option B") was considered and deferred.
- **`run_until` in the UI** is entered as a "time limit (hours)" number → `now + hours`, avoiding a date picker while still covering "run for a week".
- **`tried/rejected` + repetition guard** shipped inside the Phase-1 kickoff (not a separate Phase-2 item).
- **Cockpit copy is plain English** (not transloco) except the tab label — i18n-able later; acceptable for an experimental power-user surface.
- **Goal-met early-stop deferred** — needs a grounded agent→loop channel (a KB marker or a new agent tool); the KB path depends on agents actually using the KB, which is still unproven (see Open Questions). Budget-bound is the safe primary stop.

### Verification (k3d, `gemma-4-moe`)

- **Phase 1** — live run, `max_iterations=3`: scholar→critic→developer all completed, budget auto-stop (`stop_reason=budget`, `remaining_iterations` 3→2→1→0, `current_job_id` cleared); manual stop endpoint → `stop_reason=user`.
- **Phase 2** — synthesized a wedged loop (running loop + terminal current job): the sweeper recovered it in one tick with no double-spawn; the NULL-current-job case was logged for attention, not falsely recovered.
- **Phase 3** — `ng build` clean, 663/663 vitest pass, Playwright walkthrough: Loop tab renders native-styled, `GET /loop`→200, model picker populated, outcome banner shown, 0 console errors.
- **Cluster-model gotcha** — a loop job's model must be keyed on the *target* cluster. k3d only has `gemma-4-moe`; `minimax/minimax-m3` exists on the dev cluster, not k3d, and wrong-model jobs insta-fail (~20s). Pick a model keyed on the cluster you run against.
- **KB blackboard unproven** — across the gemma run the agents wrote **zero** KB notes (the tools were available; gemma is weak at tool-use). Confirming that agents actually use the KB with a capable model is the make-or-break next test (Open Questions #4).

## Resolved Design Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| Driver: generic event-automation vs. dedicated controller | **Dedicated** `project_loops` + advance hook | Turnkey one-button UX; far more controllable for an experiment; doesn't block the generic v0.5 event system later |
| How advance fires | **Completion hook**, not polling | The orchestrator already fans out on terminal jobs; "job done → advance" is ~50 lines beside existing hooks |
| Concurrency | **One job at a time** | Deterministic KB hand-off; fits a single subscription's agent budget; one bad job can't fork chaos |
| Iteration counting | **Per job** | Matches "job finishes → iterations−1"; role = `sequence[total % len]` |
| Execution role | **Configurable** (`developer` default; `default`/`writer` for prose) | Makes it a general autonomous-project engine, not a coding toy |
| Scholar breadth | **One scholar proposes N distinct approaches**, not N scholars | Fresh-context parallel scholars duplicate; real diversity via prompt now, `delegate_work` later |
| Loop jobs & lifecycle hooks | **Run bare** (verification/scholar/curator off) | The loop is the orchestration; auto-hooks would fight it |
| "Done" signal | **External acceptance criteria + Critic decider**, never self-grade | Research: self-evaluation of progress is near-random (AI Scientist 100%-novel) |
| Unbounded loops | **Forbidden** — CHECK requires iterations or deadline | Hard floor under runaway; the $-runaway incidents are the cautionary tale |
| State carrier | **Project KB blackboard** (structured note kinds + tried/lineage) | Already injected each call; blackboard beats master-slave; archive beats hill-climbing |

## Open Questions

These are exactly the things the research **could not settle** — so our overnight runs are the experiment that answers them:

1. **Does sequential one-at-a-time actually beat parallel fan-out for this workload?** No verified claim covers continuous single-goal loops. Measure in our own harness before optimising.
2. **How many candidate proposals, and is a separate Critic better than executor self-critique?** Candidate-count is unpinned; the one head-to-head Critic-vs-self-critique number was *refuted*. We default to a separate Critic (supported by MAST role-spec/verification gains) and treat N as a tunable.
3. **What automated progress signal distinguishes real progress from wandering/false victory** over a multi-day run on an open-ended (non-code) goal? Unsolved in the literature; for code we lean on tests, for knowledge-work the acceptance criteria + Critic decider are our best current proxy — watch them.
4. **Do the agents actually use the KB blackboard — and how should it bound its growth?** The k3d smoke run (`gemma-4-moe`) wrote **zero** KB notes across a full scholar→critic→developer run — likely gemma's weak tool-use (the tools were available and the kickoff instructs their use), but it means the core coordination channel is **unverified**. This is the #1 thing a real run with a capable model must confirm; if a capable model also under-uses the KB, the levers are a harder-enforced kickoff (make a KB write the required first/last step) or a curator pass that force-records each iteration. Separately, on growth: Reflexion used a fixed window; our read-bounding (hybrid top-K) + structured note kinds is a design choice to measure against context blowup and cost.
5. **(ours)** First real overnight target — start with a research/writing goal (cheaper, lower blast radius, no auto-merge needed) before a build goal like "build us a CMS" (which additionally needs a writable project repo + a critic-gated auto-merge policy, deferred to Phase 4).

## Risks

- **Generalization untested.** The evidence is coding-domain and benchmark-bound; an overnight prose/research loop is a reasoned bet, not a proven one. v1 is explicitly experimental.
- **Goodhart.** Self-improvement loops have been caught gaming their own success signals. Keep acceptance criteria as external/grounded as the goal allows; the Critic's verdict is an attack surface precisely where we most want to trust it.
- **Cost.** Bounded on both axes by construction, but token cost is invisible until the gateway is enabled — watch wall-clock and job counts on the first runs.
- **Drift remains the headline failure mode.** Mitigated, not eliminated, by acceptance criteria + tried-list + restate-goal kickoffs. Expect to iterate the prompts after the first night — which is exactly the plan.

## Related

- [`loop_review.md`](../loop_review.md) — **living log of issues + optimizations found during real test runs** (cost, KB hygiene, grounding, role bleed); the running companion to this plan.
- `[[verification_phase]]` — the critic verdict tools and the developer↔critic loop this generalises; loop Critic reuses the same `approve`/`return_with_feedback` stance, minus the auto-trigger.
- `[[subagent_delegation]]` — `delegate_work`, the Phase-4 path to real Scholar proposal diversity.
- `[[automations]]` — the generic event-trigger driver this is a turnkey, project-scoped specialisation of; shares the runaway-guard philosophy (`max_chain_depth`, the $-runaway incident).
- `[[feature_development_pipeline]]` — the human-checkpointed cousin (one feature, gated); this is the unattended, budget-bounded sibling.
- `[[project_knowledge_base]]` — the blackboard substrate.
- `[[continuous_improvement_loop]]` — the stale predecessor design this supersedes.
