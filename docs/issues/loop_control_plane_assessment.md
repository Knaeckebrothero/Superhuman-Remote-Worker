---
tags:
  - issue
  - assessment
  - project-loop
  - self-improvement
  - knowledge-base
  - orchestration
  - backlog
aliases:
  - loop assessment
  - project loop control plane
  - Better Resavio loop assessment
related:
  - "[[project_self_improvement_loop]]"
  - "[[loop_review]]"
  - "[[loop_optimization]]"
  - "[[loop_run6_deep_dive_forensics]]"
  - "[[loop_campaign_scheduling]]"
  - "[[loop_parallel_execution]]"
  - "[[loop_parallel_stages]]"
  - "[[loop_repo_compounding_v2]]"
  - "[[pluggable_agent_runtimes]]"
  - "[[kb_convergence_ttl_reverification]]"
  - "[[okf_knowledge_base]]"
---

# Project Loop Assessment — The Missing Control Plane

> Better Resavio live-run assessment, 2026-07-14. The loop is not primarily
> short of developers, planning stages, or model intelligence. It is missing a
> deterministic control plane connecting discovery, backlog maintenance,
> selection, execution, and verified outcome.

**Status:** assessment COMPLETE (2026-07-14). The investigation was read-only:
no loop, job, cluster, database, or repository state was changed. The live
figures below are a time-bounded snapshot and should be treated as baselines,
not permanent properties of the system.

**Primary subjects:**

- Planner loop `808d3873-09d8-459e-8ec7-e29e1c251013`, 30/30 jobs,
  2026-07-11 through 2026-07-13.
- Rotation loop `87a9a973-c329-4817-ac18-560c77230067`, still active when
  sampled on 2026-07-14.
- Project `68137e29…`, the Better Resavio / Hotel Rheinland ERP project.

**Central conclusion:** the agents can research, judge, and implement. The
system loses most of their value at the boundaries. It asks each role to
reconstruct current workflow state from a noisy semantic knowledge corpus,
treats technical job completion as semantic role success, and allows stale or
missing outputs to advance the rotation. Campaign scheduling operates after
those boundaries and therefore cannot repair them.

## Contents

- [1. Executive assessment](#1-executive-assessment)
- [2. Scope, method, and evidence integrity](#2-scope-method-and-evidence-integrity)
- [3. Architecture: designed versus operated](#3-the-architecture-as-designed-versus-as-operated)
- [4. Direct answers to the owner questions](#4-direct-answers-to-the-owner-questions)
- [5. Live evidence](#5-live-evidence)
- [6. Root-cause analysis](#6-root-cause-analysis)
- [7. Campaign scheduling](#7-campaign-scheduling-assessment)
- [8. Root-cause ranking](#8-consolidated-root-cause-ranking)
- [9. Target control-plane contracts](#9-target-control-plane-contracts)
- [10. Recommended roadmap](#10-recommended-roadmap)
- [11. Evaluation framework](#11-evaluation-framework)
- [12. What not to optimize](#12-what-not-to-optimize-yet)
- [13. Historical-document crosswalk](#13-historical-document-crosswalk)
- [14. Open design questions](#14-open-design-questions)
- [15. Recommended next decision](#15-recommended-next-decision)
- [16. Source map](#16-source-map)

---

## 1. Executive assessment

The earlier working question was whether the planner loop needed more than one
Developer per turn. The evidence says that is the wrong optimization target.

A single Developer has already demonstrated that it can carry a substantial,
coherent initiative in one long job. The strongest live example implemented
the `q64_checkout` slice, added source/data files and acceptance tests, and ran
the relevant verification in roughly 4 h 52 min. It used 638 LLM calls and
66.2 M tokens. That is expensive and insufficiently bounded, but it proves the
execution capacity exists.

The weak Developer turns were generally not weak because they needed four more
Developers. They were weak because they:

- received no authoritative pointer to the Critic's selection;
- rediscovered an old or unrelated verdict through semantic search;
- inherited a missing or stale `task_brief.md` story;
- spent much of the job negotiating phase/completion machinery;
- stopped at a specification or diagnostic handoff;
- or completed while the resulting branch failed to merge.

The loop should therefore optimize for this invariant:

> A stage is valuable only when it advances an explicit, current initiative and
> produces a verified outcome traceable to that initiative.

Jobs completed, Developers spawned, campaign length, proposal count, and lines
changed are supporting observations. None is the primary success metric.

The target architecture is a typed flow:

```text
Product direction
      ↓
Candidate backlog → Backlog snapshot → Selection decision
                                           ↓
                                    Execution mission
                                           ↓
                                    Outcome evidence
                                           ↓
                              Continue / close / block / reprice
```

The KB remains valuable as durable evidence and background knowledge. It
should no longer be the control plane that decides what the next role is doing.

---

## 2. Scope, method, and evidence integrity

The assessment combined four independent passes:

1. **Live loop forensics:** application DB loop/job state, audit DB LLM and tool
   records, vector/OKF knowledge state, Gitea history, job branches, and merged
   artifacts.
2. **Scholar/Critic/backlog analysis:** role prompts, seeded todo scaffolds, KB
   tools and limits, note lifecycle, retrieval behavior, and live proposal and
   verdict behavior.
3. **Execution analysis:** Developer job traces, strategic/tactical phase
   mechanics, completion tools, workspace/task-brief initialization, job
   budgets, and product outcomes.
4. **Architecture review:** the original loop design, the F-series run log,
   optimization and compounding plans, campaign scheduling, parallel-execution
   analysis, and the current implementation.

### Evidence categories

This document uses three kinds of statements:

- **Observed:** directly present in code, configuration, database rows, audit
  records, or Git history.
- **Inferred:** the most likely causal explanation joining multiple observed
  facts. Inferences are labelled where they are not mechanically proven.
- **Recommended:** a proposed design direction, not an implemented decision.

### Measurement caveats

- Audit token totals sum the prompt as billed/reported on every request. They
  measure workload and cost, not unique information content.
- “Changed-file occurrences” counts a path again when later merges change it.
- Product-code delta is an imperfect value proxy. A specification, migration,
  or operational fix may be valuable without changing `repo/src`; the stronger
  metric is mission-linked verified outcome.
- Live-loop state continued to evolve after the snapshot.
- No secret values or private connection material are reproduced here.

---

## 3. The architecture as designed versus as operated

| Stage | Intended behavior | Observed behavior | Consequence |
|---|---|---|---|
| Direction | Stable goal and Definition of Done steer the loop | Broad “better than Resavio” goal; explicit acceptance criteria and user steering empty | Each job reinvents a local definition of progress |
| Scholar | Research and create distinct, grounded candidates | Full multi-phase research job; often creates setup/evidence notes; duplicates recur | High cost without a maintained backlog delta |
| KB | Shared blackboard carries useful state | Queue, memory, logs, retros, runtime folklore, evidence, and archive share one corpus | Current workflow state competes with more than a thousand active notes and 2,479 across all statuses |
| Critic | Compare all open candidates and choose the next action | No deterministic candidate snapshot; generic multi-phase verifier reconstructs a sample via tools | Selection coverage is unknown and biased by retrieval |
| Handoff | Developer implements the chosen action | Kickoff says only “Implement the Critic's chosen action” | Developer must rediscover its task semantically |
| Developer | Make one coherent, verified increment | Can deliver deeply, but may follow stale state or spend the job on workflow rituals | Product outcome rate is much lower than job completion rate |
| Advance | Rotate after the required role output succeeds | Rotates after terminal job status; role artifact and merge may be absent | Missing upstream output becomes stale downstream work |
| Planner | Schedule a multi-job initiative when useful | Optional overloaded tool; all accepted plans were one-stage; review can remain open | Campaigns resemble rotation and add disposition state |

Relevant implementation surfaces:

- Role duties and kickoff: `orchestrator/services/project_loops.py:355-671`.
- Loop-job context construction: `orchestrator/services/project_loops.py:674-815`.
- Merge and advance: `orchestrator/main.py:11916-12055` and
  `orchestrator/main.py:12521-12623`.
- KB listing and search: `src/tools/knowledge/knowledge_tools.py:1186-1381`.
- Completion: `src/tools/core/job.py:72-257` and `src/core/phase.py:761-875`.

---

## 4. Direct answers to the owner questions

### 4.1 What does the Scholar actually do?

The loop-specific prompt tells Scholar to:

- ground itself in the product/domain;
- check tried and rejected work;
- propose several genuinely distinct approaches;
- write each as an active `plan` note tagged `proposal`;
- and avoid self-filtering.

That is sensible guidance, but the selected expert is a full research agent,
not a backlog-maintenance node. Its initial scaffold requires scope,
deliverable, constraint, and quality notes; an exploration focus; a multi-phase
`plan.md`; KB review; and 5–7 tactical todos. See
`config/experts/scholar/strategic_todos_initial.yaml`.

“Scholar always proposes five” is not a contract. The live five-item batches
were emergent behavior. A manual classification of freeze text and Git
artifacts found that, in the 30-job planner loop, only Scholar iterations 1, 4,
and 10 clearly produced new proposal batches. The other seven Scholar slots
were dominated by setup notes, evidence refreshes, research clusters, or
incomplete handoffs. This is not mechanically queryable because no typed
Scholar result exists and later merges may contain copied/stale proposal-named
artifacts.

The Scholar is told to consider existing work, and it can search the KB, but it
has no required reconcile operation. It is not required to return a structured
delta saying which candidates were added, enriched, merged, or found to be
duplicates. Live duplicates included `q64_checkout`, HHV GasteCard, and
Pedelec Kurbeitrag across later iterations.

**Assessment:** Scholar is an idea/research generator with optional hygiene,
not a reliable backlog groomer.

### 4.2 Does the Critic see only the newest Scholar suggestions?

Not literally, but it also does not receive all open work.

The Critic prompt says it must choose among **ALL** open proposals and QA
findings. No complete candidate set is injected. The Critic can self-serve:

- `kb_search` searches active notes, defaults to 10 results, and returns
  200-character previews;
- `kb_list` supports type/tag/status filters but has no pagination;
- the backend defaults to the 50 most recently modified matches;
- passive semantic injection contributes only a small relevance-ranked set.

Therefore an older proposal is discoverable if the Critic deliberately asks
the right query. Complete coverage is neither supplied nor measurable. The
newest, most similar, or most frequently narrated state is more likely to be
seen.

**Assessment:** the claim “Critic sees only the newest five” is too absolute;
the practical problem is worse than a fixed-five design because the evaluated
set is implicit and unreproducible.

### 4.3 Does the Scholar account for what already exists?

Sometimes behaviorally, never structurally. Exact duplicate handling exists,
but semantic variants can fork into new notes. Semantic ADD/UPDATE/SUPERSEDE/
DISCARD adjudication is applied by the automatic curator, not as a hard gate on
the Scholar's deliberate proposal writes.

Plan notes have a three-cycle TTL. Decisions, learnings, retrospectives, and
sources are effectively durable until explicitly retired. As the corpus grows,
an old product idea can be harder to retrieve than recent operational folklore
about completion, workspaces, or tool use.

### 4.4 What happens to good candidates that are not selected?

The Critic is told to mark **every** non-selected proposal and QA finding
`superseded`. The prompt then says ranking is not rejection, but the state
transition makes them indistinguishable in practice.

This collapses distinct meanings:

- valuable but deferred;
- duplicate of another candidate;
- rejected on product grounds;
- temporarily blocked;
- attempted but execution failed;
- genuinely obsolete or replaced.

A compliant Critic destroys the active backlog's deferred-candidate semantics
and normal-search visibility each cycle, although the superseded records remain
auditable. A noncompliant Critic leaves stale active notes. The older proposal
to mechanize this mass supersede would make the active-backlog information loss
deterministic rather than solve it.

### 4.5 Is one deep Developer enough?

For a coherent implementation, often yes. One single-writer Developer avoids
cross-job handoff loss and merge fragmentation and can use parallel read-only
subagents for exploration, test design, and review.

Multiple jobs are justified when there is a semantic boundary:

- an independent acceptance or Product-QA gate;
- a role or expertise change;
- deliberate failure isolation;
- a fresh-context checkpoint;
- or genuinely independent work whose merge policy is explicit.

Developer count should be a consequence of the initiative plan, not a KPI.

---

## 5. Live evidence

### 5.1 Planner loop summary

Loop `808d3873-09d8-459e-8ec7-e29e1c251013` ran for approximately 48.98 hours
and exhausted its 30-job budget:

- 10 Scholar, 10 Critic, and 10 Developer jobs;
- 27 completed and 3 failed jobs;
- 29 merge records landed and one completed job's squash merge failed;
- the loop ended with `consecutive_failures=0` despite the merge failure;
- its final campaign remained in `review` when the budget ended.

The loop model was MiniMax-M3 and the workspace backend was VM. Its goal was a
broad ERP-better-than-Resavio direction; explicit acceptance criteria and user
prompt were empty.

### 5.2 LLM cost by role

| Role | LLM calls | Prompt tokens | Completion tokens | Total tokens |
|---|---:|---:|---:|---:|
| Scholar | 1,150 | 110,003,245 | 1,510,916 | 111,514,161 |
| Critic | 901 | 94,557,112 | 1,121,048 | 95,678,160 |
| Developer | 1,606 | 142,441,664 | 1,759,568 | 144,201,232 |
| **Total** | **3,657** | **347,002,021** | **4,391,532** | **351,393,553** |

Scholar and Critic together consumed 59% of the total tokens. These are roles
whose loop-level contract is supposed to be “produce candidate delta” and
“choose next work,” yet both run as general-purpose multi-phase jobs.

Prompt sizes were already in a dangerous regime:

- Critic median 98,079 tokens/call; p95 224,157; maximum 268,753.
- Developer median approximately 76,455; p95 209,329; maximum 282,455.
- Scholar median approximately 74,836; p95 261,709; maximum 341,934.

The run recorded 5,555 tool invocations; 485 were failed or lacked a matching
completion in the audit representation. Completion/phase vocabulary dominated
the response history:

- `TRANSITION_REJECTED` appeared in 261 LLM responses;
- `mark_complete` in 1,219;
- `job_complete` in 871;
- exact `stale-kickoff` / `stale kickoff` language in 310.

Only six actual tool results contained `TRANSITION_REJECTED`. A small number of
real transition failures became a large, repeatedly re-reasoned operational
story.

The terminal `freeze_data.notes` fields show that the story had already become
cross-role policy. Across the 41 jobs in the planner and sampled rotation runs:

- 28 notes mentioned `seal`;
- 18 mentioned the exact value `0.42`;
- 9 mentioned `task_brief`;
- 4 mentioned a stale kickoff;
- 21 matched `missing`, `not shipped`, or `pending`.

`seal` / `0.42` occurred in 9/7 Critic notes, 11/6 Developer notes, and 8/5
Scholar notes. This field-specific count is deliberately narrower than a search
over the entire freeze JSON and is reproducible.

The application DB nevertheless reported `total_requests=0` and
`total_tokens_used=0` on every job row. Audit data knew the cost; loop
observability did not.

### 5.3 What reached the product repository

Across 29 successful squash merges, Git history showed:

- 810 changed-file occurrences and +77,611/−9,531 lines overall;
- only 6 changed-file occurrences in `repo/src` and `repo/tests`, totaling
  +1,160/−32 lines;
- 11 specification paths and +1,538 lines;
- +57,081/−2,510 lines of knowledge/process material;
- +6,890/−6,773 lines in repeatedly changed `task_brief.md`,
  `instructions.md`, and `plan.md`.

At the baseline immediately before the first loop squash merge, the jobs
repository contained 1,238 knowledge files and 1,790 archive files. That
baseline was recorded after the loop started and includes a UI-research import
made while Scholar job 1 was running. Relative to it, the planner run added net:

- 238 knowledge files;
- 187 archive files;
- 30 retros;
- 11 spec files;
- 1 source file and 2 test files.

Only three Developer slots changed product code or tests. Five stopped at
specification-only work, one produced process/knowledge material only, and one
completed but failed to merge. A code-only metric would undercount legitimate
specification work, but the ratio still demonstrates that technical job
activity and durable product delivery are very different quantities.

### 5.4 Knowledge corpus snapshot

At the final fact-check snapshot the project knowledge index contained:

- 2,479 notes total;
- 1,083 active notes;
- approximately 31.68 million content characters in total and 16.12 million in
  active notes;
- 457 active `learning` notes;
- 353 active `retrospective` notes;
- only 24 active `plan` notes.

Learning plus retrospective notes therefore occupied 810 of 1,083 active
notes, or 74.8% of the active corpus. Forty-three notes carried a `proposal`
tag across all statuses, but only seven were active. Several active plans were
operational phase plans rather than product backlog candidates, so even “24
plans” overstates the structured backlog.

The same corpus contains multiple generations of goals, verdicts, “current”
state, completion recipes, old job IDs, attempted initiatives, and product
evidence. Semantic similarity cannot reliably assign workflow authority among
them.

### 5.5 Current rotation loop counterexample

The newer loop `87a9a973-c329-4817-ac18-560c77230067` uses rotation rather
than planner scheduling. It reproduces the same defects, which is important:
the problems are not caused by `loop_plan`. It was still running its eleventh
job at the final snapshot and had already accumulated 2,017 LLM requests and
209,778,916 tokens, so those figures are lower bounds.

Its first three completed Developer slots showed three very different outcomes:

1. One spent a full job re-verifying work that had already shipped because it
   followed a stale kickoff.
2. Developer `bc0a3595…` spent roughly 4 h 52 min, 638 LLM calls, and 66.2 M
   tokens implementing the coherent `q64_checkout` slice with source/data files
   and acceptance tests.
3. Developer `515d5472…` spent 90 LLM calls and 9.16 M tokens but changed no
   source, tests, or specs after adopting an old “authoritative brief missing”
   narrative.

For the third case, the current Critic had selected an F-12 systemic-import
initiative at iteration 8. The iteration-9 Developer searched for an
iteration-9 verdict, retrieved older iteration-15 and unrelated job notes, and
followed stale job state. Its current kickoff was present in the audit
requests, but that kickoff still contained no authoritative decision pointer or
exact execution mission.

This is direct evidence that semantic retrieval cannot safely serve as the
execution queue.

### 5.6 Cross-loop repository pollution snapshot

A second repository measurement used baseline `28255f68` and current main
`090e6456`. It spans the planner loop **and the first ten jobs of the current
rotation loop**, so it is not a planner-only delta:

- 904 files changed, +113,458/−2,390 lines;
- 18 code/test-like files, +3,439/−25;
- 886 other files, +110,019/−2,365;
- largest categories: `knowledge/` 425 files, `archive/` 258,
  `documents/` 51, `output/` 45, and `retros/` 40.

Current main contained 2,038 `archive/` files, 1,577 `knowledge/` files, 664
`knowledge_iter6_check/` files, 465 `documents/` files, 140 `output/` files,
and 129 `retros/` files. These are partly legitimate evidence and history, but
their scale and placement demonstrate that the jobs repository has become both
product tree and orchestration workspace.

---

## 6. Root-cause analysis

### 6.1 Direction is broad but not operationally stable

The broad goal is appropriate for an open-ended improvement system, but the
loop has no stable product strategy or capability map between that goal and an
individual initiative. When acceptance criteria are empty,
`build_loop_kickoff()` tells each job to infer reasonable ones and write them
to the KB.

That produces local, model-generated definitions of progress. Combined with
“make ONE solid, verifiable increment” and a Critic rubric containing
“implementation size,” it biases selection toward horizon-one work that is
easy to prove, even when the strategic/tactical Developer could carry a larger
objective.

This assessment does **not** recommend a fixed “software is done” percentage or
automatic goal-met stop. It recommends:

- a versioned product north star;
- a capability/gap map;
- initiative-level acceptance evidence;
- and an explicit statement of which product outcome the current initiative
  advances.

### 6.2 The KB is useful memory but not a backlog

The KB supports durable findings, semantic search, links, status, and TTLs. It
does not supply the semantics a backlog needs:

- complete enumeration and pagination;
- stable candidate identity;
- product capability or value dimension;
- dependencies;
- last considered cycle;
- prior scores and dispositions;
- attempt history;
- selected/in-progress ownership;
- duplicate/alias relationships;
- or explicit exclusion reasons.

The statuses `active`, `resolved`, `superseded`, and `archived` cannot express
the lifecycle of product work. TTL/convergence can remove stale notes; it
cannot decide that a valuable unselected candidate remains deferred.

The corpus is simultaneously being used as:

1. durable product knowledge;
2. candidate queue;
3. current workflow state;
4. per-job execution log;
5. retrospective archive;
6. cross-role operational memory.

These uses have different authority, retention, and retrieval requirements.

### 6.3 Scholar is optimized for exploration, not backlog health

Scholar repeatedly pays a full research bootstrap cost. It can add valuable
evidence, but the loop lacks a contract saying whether the backlog became
better.

A valid Scholar outcome should be a structured delta:

- candidates added;
- candidates enriched;
- duplicates merged;
- evidence refreshed;
- gaps identified;
- or explicit `no_change` with a reason.

Producing no new idea can be a successful Scholar outcome if it materially
improves existing candidates. Producing five semantically duplicate notes is
not successful merely because five files exist.

#### Product-QA and parallel candidate production

The built Product-QA path is a useful counterweight: Scholar searches outward
for opportunities while Product-QA audits inward for broken, missing, or
unusable product surfaces. The barriered `scholar ∥ product-qa` stage is safer
than pipelining whole loop generations because both are analysis-only candidate
producers and Critic runs after the barrier.

It does not solve the control-plane problem. Product-QA currently writes
`qa-finding` plans into the same untyped KB, and Critic must rediscover those
alongside Scholar proposals. Give Product-QA the same typed CandidateDelta,
snapshot generation, dedupe rules, and stage-output validation. Parallel
fan-out may improve evidence breadth or wall time; it cannot substitute for a
complete candidate snapshot, authoritative selection, or exact Developer
mission.

### 6.4 Critic is a generic quality gate acting as a selector

The Critic expert's instructions require a generic five-step review:

1. extract criteria;
2. gather evidence;
3. analyze every criterion;
4. force at least three flaws;
5. render a report and verdict.

It inherits the generic strategic bootstrap: read all task files, create four
governance notes, create `plan.md`, review project knowledge, and plan 3–7
tactical phases. This is appropriate for an independent deliverable review,
not for a bounded backlog-selection decision.

The actual selection is buried inside millions of tokens of generic work and
completion ceremony. Even a stronger model would still be paying the wrong
workflow tax.

### 6.5 Selection-to-execution handoff is probabilistic

The Developer role block contains only “Implement the Critic's chosen action.”
Loop job context contains the loop ID, role, iteration, counter stamps, and
kickoff. It has no authoritative:

- candidate ID;
- verdict/decision ID;
- mission text;
- scope boundaries;
- initiative acceptance criteria;
- previous attempt;
- or backlog snapshot generation.

The Developer must search a noisy corpus and infer which verdict is current.
This is the single highest-leverage defect because it can waste the entire
most-expensive stage after the Critic reasoned correctly.

Planner-run mismatches were not isolated to the current-loop example:

- iteration-14 Critic selected `avv_kaskade`; iteration-15 Developer produced a
  channel-manager minimum-stay specification;
- iteration-8 Critic rendered a q64 disposition; iteration-9 Developer pursued
  unrelated F4 CLI wrappers;
- iteration-17 Critic completed without a usable verdict; iteration-18
  Developer selected a residual CLI-handler fix on its own.

Some of those outputs may still have intrinsic value. The defect is that their
relationship to the intended current selection cannot be proven.

### 6.6 Attempt outcome is confused with initiative value

The `q64_checkout` history is the clearest case study:

- it was proposed and selected;
- multiple attempts hit workspace, phase, or disposition problems;
- the planner later killed it;
- a subsequent single Developer completed it successfully.

The “kill” encoded execution failure as product judgment. A useful backlog
must distinguish:

- candidate value;
- selection state;
- individual execution attempts;
- blockers;
- and final product acceptance.

Otherwise the loop learns “bad idea” from “bad run.”

### 6.7 Deep execution exists, but it is not deliberately bounded

The strategic/tactical system provides depth through repeated phases, not a
declared “20k-step” envelope. The effective defaults configure 200 tool calls
per phase (`config/defaults.yaml:233`) and a seven-day outer job timeout
(`src/utils/config.py:257`), while the job as a whole has no durable maximum
for:

- phases;
- total LLM or tool calls;
- total tokens;
- wall time below the broad outer timeout;
- or wrap-up reserve.

Counters used by the tool node are process-local closure state rather than
checkpointed graph state. One audited tactical phase exceeded its nominal cap
and overlapping transition evidence existed; the exact concurrency/resume
mechanism was not proven, but the budget is not durably auditable.

The correct conclusion is not “make Developer smaller.” It is “make a deep
Developer intentional and bounded.”

### 6.8 Completion mechanics consume the agents' attention

`mark_complete` writes `output/completion.json`; it does not finish a phase or
job. `job_complete` stores final intent in a process-global dictionary and
finalization occurs through later todo/phase transitions. The model-facing
instructions and historical memories do not consistently preserve that
distinction.

If a model completes the last todo before calling `job_complete`, normal phase
validation can issue `[TRANSITION_REJECTED]` and route back into execution.
Those messages remain in context and have been summarized into shared memory.

The deliverable validator introduces another bad affordance: when declared
deliverables are missing, high-confidence completion is rejected and the tool
explicitly suggests lowering confidence below 0.5 to acknowledge the issue.
The live corpus turned `0.42` into a repeated completion recipe. Partial work
needs an explicit `partial` or `blocked` outcome, not a confidence escape hatch.

Additional contradictions include:

- todo prompts asking for 3–7 items while validation requires at least five;
- progress detection counting completion tools but omitting some real coding
  and test actions;
- and transition counters that are not durable across process lifecycle.

### 6.9 Task-brief and workspace truth are not guaranteed

Resume/reattach paths can return before the fresh-initialization path writes
`task_brief.md`. Graph initialization tolerates a missing brief by substituting
empty content, then still tells the model the brief is saved.

The observed one-off empty brief during a deployment rollout and the later
stale-brief narratives demonstrate that this boundary must fail closed. Before
the first LLM request, the system should prove:

- current job brief exists and matches current context;
- expected repository revision is present;
- loop/run/generation stamps match;
- and reserved seed files were regenerated after reuse.

### 6.10 Terminal job status is mistaken for role success

`_advance_project_loop()` derives failure primarily from the job status/result
error. A technically completed role resets consecutive failures even if its
required semantic output is absent.

Consequences observed or supported by the implementation:

- a failed Scholar can be followed by a Critic with no fresh candidates;
- a Critic without a usable current selection can be followed by Developer;
- a completed Developer with no product outcome can count as success;
- merge failure is logged while the loop continues;
- an execution role with an empty merge is flagged but does not gate advance;
- and budget exhaustion can end a planner loop with campaign review unresolved.

The active rotation provided the complete first failure chain: Scholar
iteration 10 failed with `Tool batch repeatedly timed out — workspace may be
wedged`; the loop advanced anyway; while still processing, Critic iteration 11
emitted an active zero-candidate/disposition-style intermediate note in a
non-planner loop. It had not produced a terminal Critic result at the snapshot.
This is exactly the point at which deterministic stage validation should have
stopped or retried the chain.

Job status must remain orchestrator-owned, but loop-stage outcome needs a
separate semantic classification.

### 6.11 Runtime artifacts pollute product and future control state

The squash-merge path merges the job branch rather than a role-specific output
contract. Live analysis jobs repeatedly landed:

- `task_brief.md` and `instructions.md`;
- `plan.md`;
- `archive/` todos and retros;
- `output/` completion/review files;
- tool documentation/state;
- evidence and scraped documents;
- and, in at least one case, `.worktrees` material.

These are not merely cosmetic diffs. They overwrite global-looking files that
future jobs read as instructions or state. `.gitignore` cannot protect files
that are already tracked.

Analysis roles should persist their typed output and approved KB/backlog
changes, not merge a general-purpose workspace tree. Developer merges should
exclude reserved runtime paths server-side.

### 6.12 Cross-run state has no strong generation boundary

Jobs are stamped with loop ID and iteration, but the shared KB and memory
contain old “current” verdicts, conflicting iteration labels, job IDs,
campaign-disposition duties, and completion experiences. The current rotation
loop even exhibited planner/disposition language despite not being a planner
loop.

The mechanism is partly semantic retrieval and partly persistent memory. The
effect is the same: an observation from another run can acquire current
authority because it is similar and strongly worded.

Every control artifact needs server-issued run, cycle, role, and source-output
identity. Operational memories should be scoped or quarantined by role/run and
must never outrank typed current state.

### 6.13 Economics and observability reward motion

The app DB's zero token/request totals hide the actual 351.4 M-token planner
run. The UI cannot currently answer the questions that matter:

- Which role output was accepted or invalid?
- Which exact selection did a Developer receive?
- Did a merge land?
- Did acceptance evidence pass?
- How many tokens and minutes bought that accepted outcome?
- How much repository churn was runtime/process material?

Without an outcome denominator, cheaper models, more parallelism, shorter
jobs, and prompt-cache gains can all make the wrong workflow run more
efficiently.

---

## 7. Campaign scheduling assessment

### 7.1 Chronology: the loader defect was real and is fixed

The first P3 live flip used planner loop `8e832884…`. Its first six jobs
produced no campaigns. The app DB correctly contained
`tools: {loop: [loop_plan]}` and the Critic kickoff advertised the tool, but the
audit DB showed that no Critic model request contained the schema.

The cause was `src/core/loader.py`: `ToolsConfig` had gained a `loop` field,
but both explicit `ToolsConfig(...)` construction sites omitted it, so the
merged override defaulted back to an empty list. The same constructors omitted
`communication`, which meant the defaults-intended `send_message` tool was
also unbound for workers. The deployed repair added both categories and a
dataclass-parity regression test.

That diagnosis should remain in the historical record because it explains the
first zero-campaign run. It does not explain the later runs.

For the first six post-fix planner Critics:

- 537 main-agent LLM requests were audited;
- every main-agent request carried the `loop_plan` schema;
- four subagent requests intentionally did not carry the checkpoint-only tool;
- response text mentioned `loop_plan` 167 times;
- exactly one structured call was emitted, by Critic `5d7a3d4c…`;
- that successful call requested a single `developer` stage.

Several Critics narrated a plan in prose and at least one called the tool
“blocked,” despite no actual `loop_plan` rejection or unavailable-tool result.
That false belief was subsequently visible in project memory/notes. The first
six Critics' 541 total responses also mentioned `mark_complete` in 255 rows,
`job_complete` in 153, and `TRANSITION_REJECTED` in 54, showing how planning
competed with the completion state machine for attention.

Later Critics did successfully file additional plans, so this is not evidence
that MiniMax can never invoke the tool. It is evidence that an optional,
multi-purpose structured call is not a reliable control boundary.

### 7.2 What is now proven healthy

The original tool-binding bug is fixed and deployed. After the loader repair,
`loop_plan` schemas reached the Critic's model requests. The orchestration path
has successfully:

1. accepted a plan;
2. spawned a campaign member;
3. merged/completed that member;
4. moved the campaign to review;
5. and later recorded ship/kill dispositions.

Campaign machinery is therefore not wholly broken.

### 7.3 What the full planner run did

The full run accepted three campaign plans:

1. a one-stage Developer campaign for `q64_checkout`;
2. a later kill disposition for q64 plus a one-stage Developer campaign for a
   BitV20 fix;
3. a one-stage Developer audit/cleanup campaign after a disposition-only
   attempt with empty stages was rejected.

At iteration 29, Critic attempted a two-stage `[developer, critic]` campaign.
Budget-reserve arithmetic rejected it because only two iterations remained.
Subsequent one-stage attempts were also rejected. The loop exhausted its
budget with the last accepted campaign still in review.

All accepted campaigns therefore had exactly one Developer stage. The final
campaign remained `review`; campaign history contained two disposed campaigns,
also one Developer stage each. Campaign scheduling never yielded a multi-job
campaign in this run. This was not validator truncation: the only structured
plan in the first-six-Critic audit explicitly requested one stage, while the
late two-stage request was rejected by remaining-budget rules.

### 7.4 Why campaigns are secondary

The optional tool is cognitively overloaded:

- choose an initiative;
- reference an opaque KB note ID;
- define stages and budgets;
- pre-register acceptance;
- and, when applicable, dispose of a prior campaign in the same call.

Some Critics reasoned about disposition in prose without persisting it. The
first q64 campaign remained unresolved from iteration 6 until iteration 20.

Tool ergonomics should be improved if campaigns remain, but a perfectly forced
multi-stage plan would still operate on an incomplete backlog and feed a
Developer through an unreliable mission handoff. It could schedule the wrong
work more reliably.

### 7.5 Proper role of campaigns

Campaigns should be an optional execution topology after initiative selection,
used only for meaningful stage boundaries. They should not be the mechanism by
which a Critic communicates the selected objective at all.

If retained:

- planning should be a dedicated forced structured node;
- prior-campaign disposition should be separate from new-plan creation;
- initiative identity should be supplied by the orchestrator, not recalled as
  an opaque model argument;
- review needs a timeout/budget-exhaustion fallback;
- and stage count must not be treated as success.

### 7.6 External-pattern reconciliation (secondary evidence)

The live forensics are the basis of this assessment. Established agent-design
patterns independently point in the same direction:

- Anthropic's [tool-design guidance](https://www.anthropic.com/engineering/writing-tools-for-agents)
  recommends clear workflow-oriented tools, meaningful context, and avoiding
  opaque low-level identifiers where the model can use semantic names. That
  supports removing the required `initiative_note_id` recall from planning and
  supplying initiative identity from orchestrator state.
- LangChain's [plan-and-execute pattern](https://www.langchain.com/blog/planning-agents)
  separates a dedicated planner from executors and performs replanning after
  observing execution. That supports a forced structured planning/selection
  step and a separate verification/disposition step rather than offering
  `loop_plan` as one optional tool inside a general Critic job.

These patterns do not prove that a particular model or prompt will work here.
They explain why simplifying and separating the control contracts is a more
credible intervention than prompt hardening alone. The proposed canaries still
have to measure the result on this harness.

---

## 8. Consolidated root-cause ranking

| Priority | Root cause | Why it outranks the others |
|---|---|---|
| P0 | No authoritative selection → mission handoff | Can waste the entire Developer job even when Critic made a good decision |
| P0 | No role-output contract or outcome-aware advance | Missing upstream outputs silently become stale downstream work |
| P0 | Completion/task-brief/workspace truth is not atomic | Agents can start or finish against false control state |
| P0 | Runtime artifact and cross-run state pollution | Old process state becomes future instructions and semantic authority |
| P1 | No first-class backlog or candidate lifecycle | Scholar and Critic cannot reliably preserve, compare, or revisit work |
| P1 | Generic heavy Scholar/Critic workflows | Analysis costs 59% of tokens while its small contract remains probabilistic |
| P1 | Initiative and execution attempt are conflated | Infrastructure failure is learned as bad product strategy |
| P1 | Deep execution has no durable job envelope | Useful depth and runaway behavior use the same mechanism |
| P2 | Context, caching, curator, and accounting inefficiency | Large cost multiplier, but optimizing it first preserves bad decisions |
| P3 | Optional campaign/tool ergonomics | Relevant only after selection and handoff are trustworthy |

---

## 9. Target control-plane contracts

The exact persistence technology remains open. The semantic contracts should
not.

### 9.1 ProductDirection / CapabilityMap

This is a versioned steering contract, not an automatic “percent complete” or
loop-stop score:

```text
direction_id / revision
north_star
operator_constraints[]
capabilities[{capability_id, desired_user_outcome, evidence, current_gap}]
strategic_questions[]
effective_at
updated_by = operator | verified_research
```

The operator owns the north star and constraints. Scholar and Product-QA may
propose evidence-backed capability/gap updates; a version change is explicit
and does not silently rewrite existing initiative acceptance.

### 9.2 Candidate

Suggested fields:

```text
candidate_id
title
problem_statement
product_capability
product_direction_revision
expected_value
evidence_links[]
acceptance_evidence[]
scope_estimate
risk
dependencies[]
status
duplicate_of
source_run / source_job
created_at / updated_at
last_considered_cycle
attempt_history[]
```

Recommended lifecycle:

```text
open → selected → in_progress → implemented
```

Additional states:

- `deferred`
- `blocked`
- `rejected`
- `duplicate`

Reserve `superseded` for a candidate genuinely replaced or made obsolete by a
newer one. A failed execution does not change candidate value by itself: it is
recorded as an `ExecutionAttempt` / `DeliveryOutcome` in `attempt_history`,
while the candidate remains `open`, `blocked`, or `deferred` according to the
verified disposition.

### 9.3 Initiative

An Initiative is the durable selected investment. It prevents a large objective
from being rediscovered or replaced after every execution mission:

```text
initiative_id
candidate_id / candidate_revision
product_direction_revision
objective
status = planned | active | blocked | closed | product_rejected
acceptance_evidence[]
mission_ids[]
attempt_ids[]
opened_by_decision_id
closed_by_disposition_id
```

### 9.4 BacklogSnapshot

```text
snapshot_id
run_id / cycle
candidate_revisions[{candidate_id, revision, content_hash}]
coverage_rule
excluded_candidates[{id, reason}]
generated_at
```

If the backlog becomes too large for one model call, the orchestrator may make
a deterministic shortlist. Coverage and exclusion must remain explicit and
reproducible. Candidate IDs alone are insufficient because later edits would
change what the Critic supposedly evaluated; the snapshot must freeze a
normalized payload or immutable candidate revisions/content hashes.

### 9.5 ScholarResult

```text
snapshot_id
added[]
updated[]
merged_duplicates[]
evidence_refreshed[]
coverage_gaps[]
no_change_reason
```

Product-QA uses the same result shape for `qa-finding` candidate deltas, with
its producer role recorded server-side.

### 9.6 SelectionDecision

```text
decision_id
snapshot_id
candidate_id / candidate_revision
product_direction_revision
rationale / scores
initiative_objective
developer_mission
scope_boundaries[]
acceptance_evidence[]
execution_envelope
```

### 9.7 VerificationDisposition

Disposition of the previous mission/attempt is a separate contract from the
next selection:

```text
disposition_id
mission_id / attempt_id
acceptance_results[]
evidence[]
outcome = close | continue | block | reprice | reject
candidate_status_after
reason
```

### 9.8 ExecutionMission

This is stored on loop control state and injected directly into the Developer
brief:

```text
mission_id
run_id / cycle
initiative_id / candidate_id / decision_id
objective
scope_in[] / scope_out[]
acceptance_evidence[]
previous_attempt
expected_repository_revision
execution_envelope
```

Semantic KB retrieval may enrich this mission. It may not select or replace it.

### 9.9 ExecutionAttempt

An attempt records execution mechanics without changing candidate value:

```text
attempt_id
initiative_id / mission_id
run_id / job_id
starting_repository_revision
execution_envelope
started_at / ended_at
technical_status
delivery_outcome_id
```

### 9.10 DeliveryOutcome

```text
outcome_id
attempt_id / mission_id
outcome = shipped | verified_existing | partial | blocked | failed
changed_artifacts[]
verification_evidence[]
merge_status / merged_sha
remaining_work[]
attempt_notes[]
```

This semantic outcome is separate from orchestrator-owned final job status. A
job can be technically `completed` while its contract is `invalid`, or while a
valid contract reports the outcome `blocked`.

At the generic stage boundary, keep validity and outcome as separate axes:

```text
contract_status = valid | invalid
outcome = accepted | partial | blocked | failed
merge_status = merged | empty | merge_failed | skipped
```

A valid `blocked` or `partial` result is useful control information. An invalid
result is a schema/authority failure even if the job process ended cleanly.

---

## 10. Recommended roadmap

### Phase 0 — Safety rails and trustworthy measurement

Before another long run:

1. Propagate and validate the existing server-issued `loop_id` and
   `loop_iteration` across structured outputs, KB/OKF metadata, retros, and
   events; add only the missing snapshot/cycle-generation linkage needed to
   reject cross-run state.
2. Persist input references, output references, merge status, token use, and
   wall time for every stage.
3. Surface run/cycle attribution, merge outcome, and cost in loop
   observability.

Acceptance:

- every job maps to one run/cycle/role without parsing prose;
- merge failure and empty execution output are distinct and queryable;
- app DB and audit usage can be reconciled per job;
- the baseline report can join job, input reference, merge, tokens, and wall
  time without interpreting model prose.

Phase 0 cannot classify semantic validity before Phase 1 defines the contracts.
Treat Phases 0 and 1 as one release if partial telemetry would otherwise be
mistaken for an outcome gate. Do not begin with aggressive cost targets; first
make the denominator honest.

### Phase 1 — Minimum deterministic contract spine

Implement the highest-leverage slice without waiting for a full backlog UI:

1. Require forced structured Scholar, Critic, and Developer results.
2. Store Critic selection and exact execution mission on loop state.
3. Inject mission, scope, acceptance evidence, and previous attempt directly
   into Developer context and `task_brief.md`.
4. Validate that Developer completion references the current mission.
5. Retry an invalid upstream role once with validation feedback; after the
   bounded retry, pause/fail visibly rather than reuse old output.
6. Gate accepted delivery on merge success and outcome evidence.
7. Before the first LLM request, fail closed unless the current brief,
   run/generation stamp, and expected repository revision are present.
8. Persist loop-result intent durably rather than relying on process-global
   `_final_phase_data`.
9. Add a minimum server-side reserved-path merge guard so contract/control
   files cannot be accepted as product output.

Required tests:

- failed Scholar does not spawn a Critic pretending fresh candidates exist;
- Critic without a current decision does not spawn Developer;
- decision from another run/generation is rejected;
- Developer kickoff contains the exact current mission;
- completion for another mission is invalid;
- merge failure prevents accepted delivery;
- missing/stale brief or revision fails before inference;
- a process restart cannot lose the typed result;
- the **pre-seeded contract canary** runs three cycles with 100%
  selection-to-Developer linkage and zero stale handoffs.

This slice alone would have prevented the 9.16 M-token stale iteration-9
Developer. The pre-seeded contract canary deliberately uses a known candidate
set; the broader integrated backlog/runtime canary is defined in Section 11.3.

### Phase 2 — Real backlog beside the KB

Do not purge or replace the KB wholesale. Add structured backlog state and link
candidates to KB evidence.

1. Establish the first versioned ProductDirection/CapabilityMap. The operator
   owns the north star and constraints; verified Scholar/Product-QA evidence may
   propose explicit revisions, never silently redefine them.
2. Implement the candidate and Initiative lifecycles above.
3. Produce deterministic/paginated BacklogSnapshots.
4. Make Scholar and Product-QA reconcile/groom before proposing bounded net-new
   work.
5. Add semantic dedupe/alias handling to deliberate candidate writes.
6. Make Critic a lean forced selector over the snapshot.
7. Separate persistent Initiative from the next bounded ExecutionMission.
8. Preserve deferred candidates and attach failed attempts rather than killing
   the candidate.

Required tests:

- more than 50 open candidates cannot be hidden by `kb_list` limits;
- every candidate is included or explicitly excluded;
- deferred candidates remain eligible;
- non-selected candidates are not mass-superseded;
- a fixed, human-adjudicated duplicate/non-duplicate fixture measures
  dedupe precision/recall; uncertain matches require explicit merge
  confirmation rather than destructive automatic collapse;
- decision references the exact snapshot evaluated.

### Phase 3 — Outcome state machine instead of unconditional rotation

Once typed state exists, Scholar need not run before every selection and
Developer need not follow every Critic terminal status.

Recommended default:

```text
GROOM/DISCOVER → SELECT → EXECUTE → VERIFY/DISPOSITION
       ↑                              │
       └──── when backlog needs it ───┘
```

Post-execution disposition should distinguish:

- accepted/close;
- useful but incomplete/continue same initiative;
- blocked/defer or request user input;
- failed attempt/retain evidence and reprice;
- product-rejected/choose alternative;
- backlog thin/stale/request Scholar.

Verification and next-selection may use the same Critic model, but they should
be separate structured steps.

Required transition tests cover every `VerificationDisposition` outcome:

- `close` resolves the current initiative before another selection;
- `continue` creates a new mission under the same candidate without semantic
  rediscovery;
- `block` preserves the candidate and records the blocker;
- `reprice` returns the candidate to an explicit priority state with evidence;
- `reject` records product-level rejection separately from failed attempts;
- no disposition may silently create or replace the next selection.

### Phase 4 — Workspace and completion integrity

Phase 1 supplies the minimum fail-closed brief, durable result, and merge guard
needed by the contract spine. This phase completes the harness-wide redesign:

1. Replace the `mark_complete`/`job_complete` handshake with one unambiguous,
   durable, restart-safe finalization operation.
2. Replace the confidence-below-0.5 bypass with explicit `partial` and
   `blocked` outcomes.
3. Validate/regenerate the current brief and seed files on every attach/resume
   before the first LLM request.
4. Make role-aware persistence/merge rules:
   - Scholar/Critic: structured output plus approved backlog/KB updates;
   - Developer: product artifacts and tests;
   - reserved runtime paths excluded server-side.
5. Prevent `task_brief.md`, `plan.md`, `archive/`, `output/`, `tools/`, and
   `.worktrees/` from landing as incidental product changes.
6. Scope or quarantine completion/tool-failure memories by role and run.

Required tests:

- missing deliverable cannot become accepted via confidence `0.42`;
- partial work is preserved and classified explicitly;
- restart after completion intent does not lose finalization;
- missing/stale brief fails before inference;
- analysis workspace cannot contaminate product `main`;
- merge allowlist holds even when forbidden files are already tracked.

### Phase 5 — Deliberately bounded deep execution

One deep single-writer Developer remains the default. Add a durable job-wide
envelope:

- maximum phases;
- total calls;
- token budget;
- wall-time budget;
- checkpoint cadence;
- forced wrap-up reserve;
- explicit extension policy.

Persist the counters in graph state so they survive restart/preemption. Allow
parallel read-only subagents for exploration, test design, and review while
keeping one production-code writer.

Envelope tests must prove:

- phase/call/token counters survive checkpoint and process restart;
- the wrap-up reserve remains available when the main budget is exhausted;
- analysis roles cannot exceed configured phase ceilings;
- extensions are explicit, bounded, and audit-attributed.

Create lean loop-specific Scholar and Critic configurations:

- one or two bounded analysis phases;
- no generic project bootstrap and repeated meta-note ceremony;
- only role-relevant tools;
- one forced structured output.

Then address context and cost multipliers already documented in F35–F39:

- trim old write-side tool arguments as well as results;
- compact before saturation;
- stabilize prompt prefixes for provider caching;
- reduce curator reconstruction/noise;
- restore correct token/request accounting;
- benchmark per-role models after the role workflows are lean.

### Phase 6 — Re-evaluate campaign scheduling

Only after the contract spine and backlog pass their canary:

1. make planning a dedicated forced structured step;
2. separate disposition from new-plan creation;
3. derive initiative identity from typed state;
4. close or pause unresolved review at budget exhaustion;
5. compare one deep Developer with staged execution on matched initiatives.

Evaluate verified acceptance, rework, stale-state incidence, tokens, wall time,
and merge conflicts. Do not evaluate campaign success by Developer/stage count.

---

## 11. Evaluation framework

### 11.1 North-star metric

Report two separate efficiency rates rather than combining unlike units:

```text
accepted verified outcomes per million tokens
accepted verified outcomes per wall-hour
```

Every accepted outcome must be attributable to the intended current
initiative and mission.

The numerator must be trustworthy before optimizing the denominator.

### 11.2 Required metrics

**Direction/backlog**

- candidate additions versus enrichments/merges;
- semantic duplicate rate;
- backlog coverage and explicit exclusions;
- candidate age and last-considered cycle;
- deferred-candidate retention;
- candidate attempt history;
- capability-map coverage.

**Selection/handoff**

- percentage of decisions tied to a BacklogSnapshot;
- selection-to-mission consistency;
- stale or cross-run selection incidence;
- percentage of Developer jobs with exact candidate, decision, and acceptance
  references;
- invalid upstream output rate.

**Execution/outcome**

- accepted, partial, blocked, failed, and invalid Developer outcomes;
- acceptance evidence pass rate;
- merge success and main-revision traceability;
- attempts per implemented candidate;
- tokens/time per accepted outcome;
- envelope overruns and completion retries.

**Hygiene/economics**

- Scholar/Critic share of total tokens;
- prompt median/p95/max and compaction timing;
- cache ratio;
- process-to-product artifact ratio;
- forbidden runtime artifacts merged;
- KB active-note composition and retrieval precision;
- app DB versus audit accounting agreement.

Campaign count and number of Developers are explicitly **not** success metrics.

### 11.3 Integrated backlog/runtime canary

This is broader than the Phase-1 pre-seeded contract canary. Use a cloned
project snapshot rather than the live project and include:

- 75 candidates to exceed current list caps;
- semantic duplicates;
- deferred and previously failed candidates;
- stale verdicts from another run/generation;
- one intentionally failed Scholar;
- one Critic with invalid output;
- one missing/stale task brief;
- one merge failure;
- one large initiative suited to a deep Developer.

Required canary outcome:

- zero cross-run stale selections;
- 100% typed handoffs;
- invalid upstream output blocks downstream execution;
- complete deterministic backlog coverage;
- zero forbidden runtime artifacts merged;
- at least two accepted Developer outcomes across three valid opportunities;
- bounded analysis and execution envelopes;
- complete cost attribution by stage outcome.

Only after this passes should a long unattended run tune models, caching,
prompts, or campaign topology.

---

## 12. What not to optimize yet

- Do not force more Developers or longer campaigns.
- Do not make `loop_plan` ergonomics the first project.
- Do not rely on prompt wording as a correctness boundary.
- Do not increase semantic-search top-k and call it a backlog.
- Do not automatically supersede every unselected candidate.
- Do not add more curator notes or memory layers.
- Do not choose a stronger model for every role before slimming the role
  workflows and measuring cost per accepted outcome.
- Do not introduce pipeline parallelism; it multiplies stale handoffs and
  generation races before those boundaries are fixed.
- Do not introduce an automatic “product complete” percentage or require the
  owner to specify the entire final system in advance.
- Do not use code lines, job completion, or campaign stages as the primary
  value metric.
- Do not purge the KB wholesale. Quarantine operational folklore and migrate
  durable candidates deliberately.
- Do not rewrite `orchestrator/main.py` opportunistically. Add narrow services
  around control state and contracts where they remove real duplication.
- Do not run another 90-job unattended experiment without semantic stage gates,
  a durable envelope, and usable cost accounting.

The existing parallel-execution concept reached the same null hypothesis:
parallelism improves throughput, not decision quality or token efficiency, and
the serial Developer remains the likely long pole. That analysis is reinforced,
not overturned, by these live runs.

---

## 13. Historical-document crosswalk

This assessment is a synthesis of a later operating state. Older documents are
valuable evidence but contain claims that should not be read as current
invariants.

### `project_self_improvement_loop.md`

- Its status/build language is historical; the loop is now deployed and has
  extensive live-run evidence.
- “Coordinate ONLY through the KB” should be superseded for control flow:
  current decisions and missions belong in typed loop state; the KB remains
  durable evidence/background.
- Its open question about whether KB coordination occurs is answered: it does,
  but current scale and mixed semantics make it unreliable as a queue.

### `loop_review.md`

- Early evidence that one Scholar produced distinct proposals and one Critic
  read them was true for those runs, not a durable guarantee.
- F1, F3, F14, F18, F22, F23, F25, F31–F40 remain useful evidence. This document
  groups them under the control-plane diagnosis rather than assigning new
  F-numbers.
- The concern about a fixed project DoD remains valid. The recommendation here
  is a stable product direction plus per-initiative acceptance, not an
  auto-stop denominator.

### `loop_optimization.md`

- The 2026-07-01 statement that architecture was not the problem described the
  then-known VM/compounding failures. With those layers repaired, the current
  bottleneck is the selection/handoff/outcome architecture.
- “Mechanically supersede every losing proposal” should be retired. It conflicts
  with a durable backlog and would deterministically discard deferred value.
- Several items marked unimplemented have since landed; treat the document as a
  chronological plan, not current status truth.
- Per-role model selection remains sensible, but only after role contracts and
  phase scope are corrected.

### `kb_convergence_ttl_reverification.md`

- TTL and semantic convergence improve the knowledge substrate.
- They do not implement selection lifecycle, backlog coverage, dependencies,
  or attempt history.
- The observed 1,083 active notes, 74.8% learning/retrospective, prove
  that convergence is not equivalent to grooming.

### `project_knowledge_base.md` and `okf_knowledge_base.md`

- `project_knowledge_base.md` is a historical architecture snapshot that still
  calls Neo4j canonical and itself the single authoritative reference.
- The current `okf_knowledge_base.md` architecture is files-canonical: OKF/git
  is durable truth and the Postgres/vector index is disposable/rebuildable;
  Neo4j is optional/dormant for this workload.
- New backlog design must choose its authority against the current OKF model,
  not accidentally revive the superseded Neo4j assumption.

### `loop_repo_compounding_v2.md`

- The merge step fixed earlier non-compounding, but `.gitignore` is insufficient
  once runtime files are tracked.
- “Flag and continue” is unsafe when the missing merge is the next role's
  required input.
- Role-aware merge/persistence policy is now needed.

### `loop_campaign_scheduling.md`

- The documented loader blocker is fixed and a complete 30-job planner run now
  exists.
- Its horizon-one diagnosis remains insightful.
- Campaign scheduling is not the primary bottleneck because initiative
  selection and Developer handoff remain probabilistic.

### `loop_parallel_execution.md`

- Its null hypothesis is supported: quality, compounding, and token economics
  are the bottlenecks; more concurrency would accelerate motion before
  reliable progress.
- Its requirement for generation identity applies even more strongly to the
  current sequential loop.

### `loop_parallel_stages.md`

- Barriered Scholar ∥ Product-QA is a useful, lower-risk candidate-production
  topology and addresses the prior new-feature-only blind spot.
- Both producers still write into the same untyped KB; the feature is not a
  substitute for CandidateDelta contracts, a deterministic snapshot, or
  outcome-aware advancement.
- Its remaining full-run quality verification should follow, not precede, the
  contract spine.

---

## 14. Open design questions

1. **Where should structured backlog state live?** App DB tables provide strong
   lifecycle/query semantics; files-canonical OKF provides auditable project
   history. A hybrid is plausible, but there must be one authority.
2. **What is the right initiative granularity?** Large enough to preserve
   strategic continuity, small enough to attach concrete acceptance evidence.
3. **How is product direction maintained?** The operator may provide a north
   star while Scholar/QA maintain a capability map; avoid both total owner
   specification and free-form redefinition every cycle.
4. **When does an invalid role retry versus pause?** A bounded same-role retry
   is appropriate for transient/format failure; repeated invalid control output
   should pause visibly rather than skip.
5. **What is the merge boundary?** Structured analysis output may belong in
   DB/OKF while product changes land in the jobs repo. Define reserved paths and
   authority explicitly.
6. **Should verification and selection share one model/config?** They may share
   expertise but should remain separate contracts and turns.
7. **Which model belongs on each lean role?** Re-benchmark after prompt/context
   shape is corrected; current MiniMax cost reflects harness behavior as much
   as model capability.
8. **How should legacy KB folklore be quarantined?** Preserve audit history
   while preventing old tool/completion/workspace instructions from automatic
   injection into new runs.
9. **When is a campaign worth its additional boundary?** Require a stated
   semantic reason and compare it with a one-Developer baseline.

---

## 15. Recommended next decision

Do not start with a campaign rewrite or a new model experiment. Approve one
small design/implementation slice:

> Persist a typed Critic selection and exact ExecutionMission, inject it into
> Developer, validate a typed DeliveryOutcome, and refuse to advance on missing
> or stale role output.

Run that slice on the three-cycle **pre-seeded contract canary**, which does not
depend on fresh Scholar output. In the same release, define and gate the typed
`ScholarResult` before enabling the ordinary Scholar→Critic rotation. This has
the shortest path to proving whether the rest of the loop's intelligence can
compound when the handoff is no longer probabilistic without pretending the
Scholar boundary is already safe.

If it succeeds, build the typed backlog and simplify Scholar/Critic. If it does
not, the new stage outcomes will at least identify the actual failing layer
without spending another hundreds of millions of tokens on inference from
prose and repository debris.

---

## 16. Source map

Primary local sources:

- [`../features/project_self_improvement_loop.md`](../features/project_self_improvement_loop.md)
- [`../loop_review.md`](../loop_review.md)
- [`../features/loop_optimization.md`](../features/loop_optimization.md)
- [`loop_run6_deep_dive_forensics.md`](loop_run6_deep_dive_forensics.md)
- [`../features/loop_campaign_scheduling.md`](../features/loop_campaign_scheduling.md)
- [`../features/loop_parallel_execution.md`](../features/loop_parallel_execution.md)
- [`../features/loop_parallel_stages.md`](../features/loop_parallel_stages.md)
- [`../features/loop_repo_compounding_v2.md`](../features/loop_repo_compounding_v2.md)
- [`../features/kb_convergence_ttl_reverification.md`](../features/kb_convergence_ttl_reverification.md)
- [`../features/project_knowledge_base.md`](../features/project_knowledge_base.md)
- [`../features/okf_knowledge_base.md`](../features/okf_knowledge_base.md)
- `orchestrator/services/project_loops.py`
- `orchestrator/main.py`
- `src/tools/knowledge/knowledge_tools.py`
- `src/services/knowledge_store.py`
- `src/tools/core/job.py`
- `src/core/phase.py`
- `src/graph.py`
- `src/agent.py`
- `config/experts/scholar/`
- `config/experts/critic/`
- `config/templates/strategic_todos_initial.yaml`

Secondary external pattern references:

- [Anthropic — Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [LangChain — Plan-and-Execute Agents](https://www.langchain.com/blog/planning-agents)

Live evidence sources were the application, audit, and vector/knowledge stores,
plus the project Gitea repository and job branches. Investigation queries and
connection details are intentionally not embedded in this document.
