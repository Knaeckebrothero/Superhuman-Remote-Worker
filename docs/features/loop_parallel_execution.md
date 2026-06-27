---
tags:
  - feature
  - concept
  - orchestration
  - projects
  - self-improvement
  - concurrency
aliases:
  - parallel loop
  - loop concurrency
  - parallel project loop
  - loop pipelining
related:
  - "[[project_self_improvement_loop]]"
  - "[[loop_repo_compounding]]"
  - "[[kb_convergence_ttl_reverification]]"
  - "[[project_knowledge_base]]"
  - "[[subagent_delegation]]"
  - "[[loop_review]]"
  - "[[usage_monitoring_and_rate_limiting]]"
---

# Loop Parallel Execution (Concept)

> The [project self-improvement loop](project_self_improvement_loop.md) runs **one job at a time**: Scholar → Critic → Execution, each spawned only when the previous reaches a terminal state. For a three-role turn the wall-clock cost is the **sum** of three job durations. This doc explores giving the loop an optional **parallel** execution mode so roles can overlap and a turn costs closer to the **slowest** stage instead of the sum. **Nothing here is decided** — it is a design/options paper that maps the idea, the architectural blockers, the realistic payoff ceiling, and the open questions, so we can choose a direction (or choose not to) before the next loop test.

## Status

**Concept — exploratory, nothing committed.** This directly takes up **Open Question #1** of the parent loop doc ("Does sequential one-at-a-time actually beat parallel fan-out for this workload?") and turns it from a research note into a concrete design space. No code, no migration, no decision. The point of the doc is to make the trade-offs legible enough to pick an experiment.

## Motivation

The loop is **structurally sequential** — see `_advance_project_loop` in `orchestrator/main.py`: a single `current_job_id` points at the one in-flight job, and the next role is spawned only when that job reaches a terminal state (an atomic `claim_project_loop_advance` guarantees exactly one advance). The parent doc's [Resolved Design Decisions](project_self_improvement_loop.md#resolved-design-decisions) chose this deliberately: *"Deterministic KB hand-off; fits a single subscription's agent budget; one bad job can't fork chaos."*

The cost is latency. With three roles that each take, say, 5–15 minutes, one full turn is 15–45 minutes of wall-clock, and most of that time two of the three agents are idle. For an overnight loop that's bounded by `max_iterations`, fewer-but-faster turns means more improvement per night. The ask: **let the user opt into running a turn's roles concurrently**, with the first turn(s) staggered so the dependent roles have something to consume.

That stagger instinct is exactly right — and it has a name.

## This is software pipelining

The roles form a **producer → consumer chain**: Critic consumes Scholar's proposals, Developer consumes Critic's decision. You can't start all three on the same idea at once — but you *can* keep all three busy if each works on a **different generation** of the idea. That is a CPU/instruction pipeline:

```
SEQUENTIAL (today) — one idea flows through all stages before the next starts
  │── Scholar ──│── Critic ──│── Developer ──│── Scholar ──│── Critic ──│ ...
  wall-clock per completed idea = t_S + t_C + t_D     (the SUM)

PIPELINED (Option A) — stages overlap; each works a different generation
  round T:    Scholar(T)        Critic(T)          Developer(T)
              proposes gen T    judges gen T-1     builds gen T-2's
                                proposals          chosen action
  round T+1:  Scholar(T+1)      Critic(T+1)        Developer(T+1)
  wall-clock per round = max(t_S, t_C, t_D)          (the SLOWEST stage)

  FILL (the user's "head-start"):
  round 0 = Scholar only        (nothing to critique or build yet)
  round 1 = Scholar + Critic     (nothing decided to build yet)
  round 2+ = Scholar + Critic + Developer   ← steady state
```

The staggered fill (`S`, then `S+C`, then `S+C+D`) is the pipeline **fill phase**, and it is the correct way to handle the dependency. So the *scheduling* design is sound. The hard part is elsewhere.

## The crux: roles coordinate through one mutable, unversioned whiteboard

The loop's roles do **not** hand off through explicit per-job wiring. They coordinate through **one shared, mutable, unversioned blackboard — the project KB** (plus git `main` for the execution role). Each job reads "the latest KB state" at start (`assemble_knowledge_block` → injected `kb_search`) and writes at end:

| Role | Reads | Writes |
|------|-------|--------|
| Scholar | tried/rejected lineage | `plan` notes tagged `proposal` |
| Critic | open `proposal` notes + Definition of Done | `decision` tagged `verdict`; flips losers to `superseded` |
| Developer | the `verdict` decision | commits to `main`; `progress` notes |

**Sequential execution is the only thing that guarantees a clean read.** The moment Scholar(T) and Critic(T) run at the same time, two failures appear:

1. **Generation leakage.** KB notes carry no round/generation stamp, and hybrid search is recency-boosted. So when Critic(T) does its KB read, it will happily surface the proposals Scholar(T) is *concurrently writing this round* — they're the freshest, most relevant notes — and mix them with the gen T-1 proposals it's actually supposed to judge. The clean S(T-1) → C(T) handoff dissolves.
2. **Convergence races.** The KB convergence machinery — `AssembleKnowledgeTask` (`src/services/auxiliary.py:488`), the per-job aux task that does the supersede/TTL re-verification from [kb_convergence_ttl_reverification.md](kb_convergence_ttl_reverification.md) — runs **once per job** and mutates shared `knowledge_index` rows. Two or three of those running concurrently will race on supersede/TTL state. That machinery was explicitly designed **single-writer-per-turn**.

So "run them in parallel with a head-start" silently corrupts the loop's coordination channel *unless we first give the KB a notion of generation.* **The scheduler is the easy 20%; KB isolation is the real 80%.**

Git is the easier half: execution roles commit to `main` (`work_on_main=True`, `job_provisioning.py:194`), analysis roles run on throwaway branches and emit only KB notes (`LOOP_ANALYSIS_ROLES = {scholar, critic}`, `project_loops.py:33`). So within a single turn only **one** job (the developer) writes the artifact — no intra-round `main` contention. The constraint is *cross-round*: developer(T) and developer(T+1) must never overlap (one writer to `main`).

## Two ceilings that cap the payoff

Even done correctly, the speedup is bounded — and it's worth being honest about by how much before investing:

1. **The execution stage is intrinsically serial, and probably the long pole.** Only the developer writes `main`, and two developer jobs must not overlap, so the execution stage *cannot* pipeline against itself. The developer is also likely the slowest stage (writes + validates code vs. the critic merely selecting). Pipeline throughput is bounded by the slowest serial stage → **the loop's ceiling is roughly the developer cadence**, no matter how cleverly Scholar and Critic overlap. Concretely: the realistic win is "hide Scholar+Critic latency behind the developer's runtime."

   > If execution is ~60% of a turn's wall-clock, the ceiling is ≈ 1.67×. If it's ~80%, ≈ 1.25×. Not 3×.

2. **Pipelining helps throughput, not latency** — and the fill + generation-skew overhead hurts short loops. A single idea still takes ~3 rounds to go idea → judged → shipped (the developer is always implementing a decision one generation stale relative to what the critic just decided — *expected* in a pipeline, but it loosens the tight S→C→D causal chain). The fill phase (2 partial rounds) plus that skew is pure overhead that only amortizes over **many** iterations. For a 6-iteration test budget it may wash out entirely.

These two points are the reason the "obvious" win (pipeline the one chain) is the *least* attractive option below.

## Design options

Presented as a menu, not a recommendation. Each is a different answer to "what do we actually want — fewer minutes per turn, or more value per turn?"

### Option A — Pipeline the single chain (the original idea)

Barriered rounds: at round boundary, spawn all roles at once; each reads the *previous* generation, writes the *current*; wait for all to finish before the next round. Plus the staggered fill.

- **Needs:** generation-stamped KB notes + generation-scoped reads + assembler serialized to the round barrier + a multi-job advance/barrier replacing the single `current_job_id` + execution-role kept a singleton.
- **Wins when:** stages are balanced and the loop runs many iterations on **one** indivisible goal.
- **Costs:** the most surgery to the part of the system we *just* stabilized (KB convergence), for the **lowest** ceiling (capped by the serial developer). Highest complexity ÷ payoff.

### Option B — Independent tracks (true throughput scaling)

Run **M separate S→C→D chains** on **disjoint sub-goals**. Each chain stays internally **sequential** — so the safe handoff is untouched — and the chains don't depend on each other.

```
track 1:  S → C → D → S → C → D ...    ← internally sequential (safe), own KB namespace + branch
track 2:  S → C → D → S → C → D ...
track 3:  S → C → D → S → C → D ...
throughput ≈ M × sequential     (real linear scaling, IF the goal decomposes)
```

- **Needs:** sub-goal decomposition (who splits the goal? the user, or a planning role?), a per-track KB namespace (the KB already does project scoping; add a track dimension), and — the hard part — **artifact convergence across tracks**: either tracks own disjoint subtrees of `main` (module boundaries, never conflict) or live on separate branches with a periodic integration/merge role.
- **Wins when:** the goal genuinely splits into independent workstreams.
- **Costs:** cross-track merge is the classic parallel-development problem; coarser isolation (per-track namespace/branch) is *easier to reason about* than Option A's per-note generations, but the merge policy is net-new.
- **Why it's attractive:** it's the only option that preserves the safe sequential handoff *and* gives real (not developer-capped) throughput, because each track is just the existing loop.

### Option C — Stage fan-out (breadth, not speed)

Run **N Scholars in parallel** → one Critic picks among all N proposals → one Developer. Maps onto the existing "propose several genuinely distinct approaches" design; preserves dependency direction (parallel producers, single consumer); lowest concurrent-write risk because parallel scholars only **add** proposals (no supersede races).

- **Reality check:** this barely changes wall-clock (the turn is still `max(scholars) + t_C + t_D`). It buys **breadth per turn**, not speed — a *quality* lever.
- **Overlap with existing plan:** the parent doc already plans Scholar diversity via `delegate_work` sub-agents **inside one Scholar job** ([Phase 4](project_self_improvement_loop.md#implementation-roadmap); [[subagent_delegation]]). That path shares one context and one KB write path — arguably **better** than N parallel scholar *jobs*, which reintroduce concurrent-write coordination. If breadth is the goal, prefer the in-job `delegate_work` route already on the roadmap; list N-parallel-jobs here only for completeness.

### Option D — Keep it sequential (the null hypothesis)

Always worth stating. Parent Open Question #1 is unmeasured: we don't yet *know* sequential is the bottleneck. The honest first move may be to **measure a real sequential run** (per-role durations, % of turn spent in the serial execution stage) and only then decide whether any of A–C clears its complexity bar. The [loop_review.md](../loop_review.md) findings (F-series, through F28) suggest the current headline problems are **compounding/quality** (non-compounding artifact, KB under-use), not wall-clock — speed may be optimizing the wrong axis right now.

### Comparison

| | A — Pipeline chain | B — Independent tracks | C — Stage fan-out | D — Sequential |
|---|---|---|---|---|
| Primary gain | throughput (capped) | throughput (linear) | breadth | — |
| Realistic ceiling | ~1.3–1.8× (developer-bound) | ~M× (if goal splits) | ~1× wall-clock | 1× |
| KB isolation needed | per-note generations (hard) | per-track namespace (medium) | additive only (easy) | none |
| Artifact (`main`) handling | singleton across rounds | cross-track merge policy (hard) | unchanged | unchanged |
| Touches the just-built convergence design? | heavily | lightly | barely | no |
| Fits one indivisible goal? | yes | no (needs decomposition) | yes | yes |

## Shared prerequisite — give the KB a notion of generation

Every same-turn-concurrency option (A, and C to a lesser degree) needs the KB to stop being a single live whiteboard. Sketch of the minimum:

1. **Generation stamp** — tag every KB note with the loop round/generation it was written in. Today the loop has only `loop_iteration` (cumulative) and `seq_index`; there is **no round counter**. A round = one full pass over `role_sequence`, derivable as `total_jobs_run // len(role_sequence)` or an explicit column.
2. **Generation-scoped reads** — the Critic reads `proposal` notes from generation `N-1`, ignoring the gen-`N` notes the Scholar is writing live. Requires `KnowledgeStore` hybrid search to accept a generation filter — it has none today.
3. **Assembler serialization** — `AssembleKnowledgeTask` must not run concurrently across a turn's jobs. Simplest: disable per-job during parallel rounds and run **one** assembler pass at the round barrier (generation-aware).
4. **Execution singleton** — at most one execution-role job in flight (advisory lock or the round barrier).

Option B sidesteps per-note generations with **coarser** isolation (one namespace + one branch per track) but inherits the cross-track artifact-merge problem instead.

## Data-model & code surface (tentative — for sizing only)

Nothing here is proposed for build; it's to gauge blast radius.

- `project_loops`: add `execution_mode TEXT DEFAULT 'sequential' CHECK (... IN ('sequential','parallel'))`; for Option A a round counter; for Option B a tracks descriptor.
- **In-flight tracking:** `current_job_id` is a single FK. Parallel needs a set — but it may be derivable rather than stored: jobs already carry `context->>'loop_id'`, so "in-flight this loop" = `WHERE context->>'loop_id' = $1 AND status NOT IN (terminal)`. The **barrier** = none in-flight.
- **Advance hook:** `_advance_project_loop` (`orchestrator/main.py`) becomes a *barrier-complete* check (spawn next round only when the whole round is terminal) instead of a per-job rotate. The atomic-claim idempotency generalizes to a per-round claim.
- **KB notes:** generation column/tag + a generation filter in `KnowledgeStore.assemble_knowledge_block` / hybrid search.
- **Aux:** gate `AssembleKnowledgeTask` to barrier-time in parallel mode.
- **Cockpit:** a `Sequential | Parallel` toggle in the Loop tab (`project-loop.component.ts`) + `execution_mode` on `ProjectLoopStartRequest`.
- **Budget/rate limits:** N concurrent loop jobs = N× instantaneous token throughput against the LiteLLM gateway's RPM/project limits and daily-quota freeze (see [[usage_monitoring_and_rate_limiting]]) → concurrent failures could trip `max_consecutive_failures` faster; the failure-counting semantics across parallel branches need redefining.

## Open Questions

1. **Is speed even the bottleneck?** Measure a real sequential run first — per-role durations and the execution stage's share of turn time set the ceiling for *every* option here. If execution is 80%+ of the turn, parallelism buys ≤1.25× and probably isn't worth the risk to the convergence design.
2. **Is generation-scoped reading enough**, or does the KB need true per-round **snapshots**? Scoped reads are cheaper; snapshots are stronger but heavier. Untested either way.
3. **Failure semantics across parallel branches.** If Critic(T) fails but Scholar(T) succeeds, does the round advance? What does `consecutive_failures` mean when three jobs fail in one round — 1 or 3? How does the barrier handle a partial round?
4. **Option B's artifact merge.** Disjoint subtrees of `main` (clean but needs the goal to be module-separable) vs. per-track branches + an integration role (general but adds a merge role and conflict handling). Which, and who decides the split?
5. **Does parallel actually beat sequential for *quality*, not just speed?** Pipelining loosens the S→C→D causal tie (the developer implements a generation-stale decision). The loop's current headline problem is *compounding/quality* ([loop_review.md](../loop_review.md)), not latency — could parallelism make the real problem **worse** while fixing a non-problem?
6. **Interaction with the two keystones.** Generation-stamping touches [kb_convergence_ttl_reverification.md](kb_convergence_ttl_reverification.md) (reasoning keystone) and the execution-singleton touches [loop_repo_compounding.md](loop_repo_compounding.md) (artifact keystone). Both were just stabilized — does adding a parallel mode regress them?
7. **Rate-limit & cost blast radius.** Does N× concurrency trip the gateway's RPM/daily freeze, and should parallel mode carry its own concurrency cap independent of `max_iterations`?
8. **Barrier vs. true pipeline.** The doc assumes barriered rounds (simpler, safe). A *true* (unbarriered) pipeline — where a fast Scholar races ahead of a slow Developer — is higher throughput but reintroduces unbounded generation skew and back-pressure. Worth it?

## Risks

- **Regressing the convergence design.** The biggest risk: the supersede/TTL machinery is single-writer-per-turn by construction; a half-built parallel mode corrupts the KB silently (no loud failure), exactly the class of bug that's hardest to catch in an unattended overnight run.
- **Optimizing the wrong axis.** If quality/compounding is the real bottleneck, shipping speed is motion without progress — and adds permanent complexity to the loop's core.
- **Complexity ÷ payoff.** Option A is the intuitive ask but the worst trade (most surgery, developer-capped ceiling). The clean wins (B for speed, C/`delegate_work` for breadth) are *different shapes* than the original request.
- **Goodhart, unchanged.** Parallelism doesn't touch the self-grading attack surface the parent doc flags; it just runs more of it at once.

## A possible first step (not a decision)

If we pursue this at all, the lowest-regret sequence is probably:

1. **Measure** a real sequential run (role durations, execution-stage share) — settles Open Question #1 with data instead of intuition.
2. If speed clears the bar, **lean Option B (independent tracks)** for genuine throughput (it reuses the safe sequential handoff) rather than Option A (pipelining one chain, developer-capped). If *breadth* is what we actually want, take the **`delegate_work` Scholar path already on the parent roadmap**, not orchestrator-level fan-out.
3. Treat **KB generation-stamping** as the shared prerequisite to spec carefully *before* any scheduler change — it's the 80%.

But the deliberate, defensible default remains **Option D**: stay sequential until a measured run shows latency — not compounding — is the thing holding the loop back.

## Related

- [`project_self_improvement_loop.md`](project_self_improvement_loop.md) — the parent design; this doc expands its **Open Question #1** and its **Concurrency** resolved-decision. The sequential advance hook, `project_loops` schema, and bare-job model all live there.
- [`loop_repo_compounding.md`](loop_repo_compounding.md) — git `main` as the compounding artifact; the execution-role-on-`main` rule that forces the execution-singleton constraint.
- [`kb_convergence_ttl_reverification.md`](kb_convergence_ttl_reverification.md) — the supersede/TTL convergence (`AssembleKnowledgeTask`) that assumes one writer per turn; the thing generation-stamping must not break.
- [`loop_review.md`](../loop_review.md) — living log of real-run findings (F1–F28); the source for "is speed even the bottleneck?"
- [[subagent_delegation]] — `delegate_work`, the in-job route to Scholar breadth (Option C's better cousin).
- [[usage_monitoring_and_rate_limiting]] — the gateway RPM/quota limits that bound concurrency.
