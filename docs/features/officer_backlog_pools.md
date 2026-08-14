---
tags:
  - feature
  - plan
  - orchestration
  - officers
  - projects
  - self-improvement
aliases:
  - backlog pools
  - work categories
  - researcher/tester/executor
  - auto-pull
related:
  - "[[officer_post]]"
  - "[[centurion]]"
  - "[[officer_knowledge_plane]]"
  - "[[officer_supervision_surface]]"
  - "[[officer_message_routing]]"
  - "[[unified_orchestrator_tool_surface]]"
  - "[[loop_unified_engine]]"
  - "[[loop_campaign_scheduling]]"
  - "[[loop_parallel_stages]]"
  - "[[project_self_improvement_loop]]"
---

# Officer backlog pools — category-typed capacity, expert-per-ticket, machine liveness

> The continuous loop starved every kind of work that pytest cannot grade — a month of
> iterations produced a tested backend with no UI, no theme, no hosting story. This feature
> replaces role rotation with a **backlog the officer curates** and **capacity the machine
> fills**: slots are grouped into three pools by *work category* (researcher / tester /
> executor), tickets carry the category and optionally the expert, and a dumb orchestrator
> tick auto-fills any free slot from the officer's ready queue. The officer controls *what*
> runs; machinery controls *when*. Hard gates shrink to two — truthful completion reports
> and deliverable contracts — and "verifiable" is repriced per category: citations for
> research, reproduction evidence for testing, screenshots and running deployments for
> execution. Tests are demoted to regression rails; they are never the score.

## Status

**PROPOSED (2026-08-13). Nothing implemented.** Designed in session with the Legate;
**refined same day by a six-agent research round** — three codebase audits (**[A1]**
dispatch/claim/tick seams, **[A2]** ticket/tag/KB plumbing, **[A3]** kickoff/config/cockpit
blast radius), two external surveys (**[R-fw]** agent frameworks & fleet products, **[R-org]**
flow theory, spike discipline, design rubrics, judge bias), and one adversarial review
(**[X]**). Markers throughout cite the source; external evidence is collected in
[Prior art & evidence](#prior-art--evidence). The round produced five must-fix design
changes (one-shot claims, claim funnel unification, ready-tag provenance, predicate
alignment, autonomy exemption) — all folded in below.

Sequenced **after** [[officer_post]] (the `project_officers` table, commission/hold/release
lifecycle) — `auto_pull` lives on that row and acceptance runs on the released Resavio
century. **Hard dependency: officer_post's lineage-aware capacity (O2) must land before B3**,
or prior-incarnation jobs stop counting after a recommission and the tick over-dispatches
**[X]**.

**2026-08-14 operating-surface prerequisites:** before B3 enables unattended pulls, the
background officer must have the explicit writable project-KB binding and object-plane
ceiling in [[officer_knowledge_plane]], trustworthy scoped observation (and either bounded
evidence or the explicit tester/recon fallback) in [[officer_supervision_surface]], and the
blocking-message liveness boundary M2–M4 in [[officer_message_routing]]. B1/B2 remain
independent. These additions do not change the one-shot claim model or the six §13 backlog
defaults awaiting the Legate.

**Prerequisites LANDED 2026-08-14** (one pipeline day on develop): unified toolset
reviewed + repaired (`1a2bfbec`, `5fd63947`), officer post O1–O5 (`fa497666`,
`9a3c3934`, `d7cfd716`), knowledge plane K1–K3 (`0c0c5607`), supervision E1–E5
(`4d501a8f`, `7bb1d331`), message routing M1–M4 (`be1d972e`). Additional substrate this
feature can now consume: `compute_jobs_liveness()` (batch, project-scoped) for §5's
stale-claim ages, the evidence manifest for §3's close-checklists, and
`job_message_routes` for the `waiting_for_reply` claim interactions. **B1+ is
unblocked** pending the six §13 defaults and the O6 Resavio release for live
acceptance.

Decisions locked in the design session (amendments from the research round in *italics*):

- **Category is a property of the work; expert is a property of the worker.** A ticket's
  category decides the contract shape ("what does done look like"); the expert config decides
  skills/persona/tools. They compose at dispatch — the same composition law the product-qa
  generalization established. *Supported by MetaGPT's ablation: the win comes from
  structured division of labor plus standardized output artifacts, not role names — keep
  the category count at three and add contract variants, never categories* **[R-fw]**.
- **Three categories, no more**: `researcher` (diverge — ideas, feasibility spikes, design
  direction), `tester` (critique — issues with evidence), `executor` (converge — shipped
  artifacts under deliverable contract). Membership is many-to-many: a developer is a
  researcher when the ticket is a spike and an executor when it's a story.
- **Floors apply to backlog health, not dispatch.** The officer must keep ≥N *ready* tickets
  per pool; he must never manufacture tickets to keep slots busy. *A floor breach wakes the
  officer (kanban order-point replenishment), and the digest reports the self-filed-ticket
  ratio per pool so gaming is visible within a day* **[R-org][X]**.
- **Liveness is a machine property.** A replica-safe tick fills free slots from the ready
  queue; the officer's wake cadence never gates dispatch. *Validated as the exact
  architecture Anthropic names as the fix for the synchronous-supervisor bottleneck; dumb
  rule-based dispatch is the reliable pattern (LLM-driven dispatch misfires in AutoGen and
  CrewAI) — never add "smart" dispatch* **[R-fw]**.
- **Claims are one-shot** *(amended by the red team — the original "non-terminal job =
  claim" design re-dispatched every finished or failed ticket)*: a ticket stamp in **any**
  job status blocks eligibility; only an explicit officer re-ready re-arms it **[X]**.
- **Two hard gates only**: honest completion reports and P1-C deliverable contracts.
  Everything else is evidence the officer weighs. *No auto-close path, ever — termination
  authority outside the worker is the load-bearing lesson of CAMEL/ChatDev* **[R-fw]**.
- **The circuit breaker counts job failures only** — *an honest `goal_achieved=false`
  completion never trips it; punishing honest negatives would tax exactly what gate #1
  demands (watermelon-reporting literature)* **[R-org]**.
- **`ready` and `parallel-safe` are officer-provenance-only tags** *(red team: a
  worker-writable `ready` is worker→dispatcher privilege escalation, including via prompt
  injection)* **[X]**.
- **Cross-family judging**: the officer's model family ≠ his pools' model families, as
  doctrine, not accident. *Self-preference bias tracks self-recognition and even familiar
  style; anonymization alone does not fix it — separation and rubrics do* **[R-org]**.
- **This supersedes campaign scheduling as the planning mechanism for officer-commanded
  projects.** The officer plans multi-job arcs by queueing ticket chains; no `loop_plan`
  emission needed, which retires the P3 blocker (MiniMax critics almost never emitted
  structured plans). Campaign mode remains for legacy non-officer loops.

## 1. Motivation — why the loop starved unverifiable work

Four findings from the Better Resavio forensics, each independently sufficient:

1. **Selection bias upstream of any gate.** The critic never rejected UI work — zero UI
   tickets ever existed (no KB note across 28 iterations matched browser/web/frontend/
   dashboard). Role vocabulary is a prior on imaginable work: scholar/critic/developer are
   all engineering identities, so engineering is what got proposed. `product-qa` was bolted
   on for exactly this reason; pools generalize the fix.
2. **Self-inferred DoD recycles the bias.** With `acceptance_criteria` empty, the kickoff
   told agents to infer a Definition of Done from the KB — and the iter-23 critic codified
   "reachable via CLI entry point" as the acceptance bar. The loop graded itself against its
   own past.
3. **Per-job provability taxed subjective work.** "Make ONE solid, *verifiable* increment"
   (`build_loop_kickoff`) plus a critic rubric ranking on "evidence quality" means a UI
   ticket with a screenshot structurally loses to a backend ticket with forty green tests —
   even though the same agent wrote both the tests and the code, so green tests were never
   independent evidence anyway. The culture this bred is measured: seven consecutive seals
   at 0.42–0.55 confidence ("honest floor"), and a verifier declaring a test suite
   "INFERRED GREEN-by-construction" without running pytest.
4. **The licensed vehicle for unprovable work never fired.** Campaigns were designed so "a
   UI, an integration, a migration" could beat small provable increments — but MiniMax
   critics filed one structured plan across six checkpoints and hallucinated "tool blocked"
   the rest of the time. Planning must live on the capable brain: the officer.

The officer substrate already fixed its own half of the problem (blind reads P0-A/B, the
cap-not-target doctrine replaced by the "never idle century" standing order, deploy-churn
P-G.1) — after those, the Resavio officer ran a full day autonomously with zero idle. What
remains is a work substrate that guarantees divergent work holds capacity.

## 2. The model — slot × ticket × expert, bound by category

Three existing concepts, one new binding:

- **Slot** (exists — `orchestrator/services/officer_slots.py`): named capacity,
  `{count, model, backend}`, stamped server-side at dispatch, enforced with an
  advisory-locked 409. Gains an optional `category` key; slots sharing a category form a
  **pool**. Pool capacity is **non-transferable**: a free researcher slot may never be
  borrowed for executor overflow — per-class capacity allocation is the canonical
  starvation fix, and borrowing dissolves the guarantee silently **[R-org]**.
- **Ticket** (exists — `orchestrator/services/project_backlog.py`): a `feature`/`issue`/
  `idea` note in `knowledge_index`, priority-sorted. Gains a `category:<name>` tag, an
  optional `expert:<config>` tag, and a `ready` tag (officer-provenance-only, §4).
- **Expert** (exists — `config/experts/`). **[A3]** verified every membership-map candidate
  is worker-capable (extends `worker_base` with full tool access): scholar, designer,
  developer, product-qa, bughunter, critic, general-worker. `designer-interactive`,
  `assistant`, and `centurion` extend `session_base` and correctly stay out of the map. A
  `writer` expert (user-facing prose as executor output) is the one missing roster entry.

Default membership map (warn-not-forbid; the officer may override per dispatch):

| category   | experts                                          | may run in parallel |
|------------|--------------------------------------------------|---------------------|
| researcher | scholar, designer, developer, general-worker     | yes                 |
| tester     | product-qa, bughunter, critic                    | yes                 |
| executor   | developer, designer, general-worker, (writer)    | **no** (singleton, §5.5) |

The parallelism column generalizes the old `LOOP_ANALYSIS_ROLES` rule: researchers and
testers write to the KB (conflict-free); executors touch shared project state. Externally,
the split is validated from both directions: parallel agent workers are safe only for
isolated, read-only-ish tasks (Anthropic's research fan-out), and parallel *writers* make
conflicting implicit decisions (Cognition's core argument; MultiDevin restricts fan-out to
"isolated, incremental, objectively verifiable" subtasks) **[R-fw]**. **[A3]** caveat:
legacy loops keep the *name-based* classification (`is_loop_execution_role`,
`LOOP_ANALYSIS_ROLES` at `project_loops.py:70,104` feed fan-out validation and
retro/delivery paths) — category-based parallelism applies to tick dispatches only; do not
"generalize" the name-based checks in place.

**Cross-family judging doctrine** **[R-org]**: the officer's model family must differ from
his pools' families (the §6 example kit — sol officer, MiniMax pools — already complies;
this makes it a stated rule). Self-preference in LLM judges tracks self-recognition and
survives anonymization; family separation plus rubric-anchored judging (§3) are the levers
with evidence behind them.

## 3. The three categories and their contracts

Each category carries a **contract block** — kickoff text defining deliverable shape,
evidence class, and effort ceiling — in a new pure module
`orchestrator/services/work_categories.py` (`_CATEGORY_BLOCKS`), absorbing text scattered
in `_ROLE_BLOCKS` today. Per **[R-fw]** (Anthropic's measured fix for runaway effort),
every block states an explicit effort posture: how big this job is allowed to be, and that
bigger discoveries become follow-up tickets, not scope creep.

- **researcher** — *"Your deliverable is an ANSWER, not a product increment."* Researcher
  tickets are **spikes** in the XP sense, and carry spike discipline **[R-org]**:
  - **Timeboxed**: a smaller default budget than executor tickets, enforced through the
    existing `budget_exceeded` freeze. At expiry the worker documents what was learned and
    what remains unknown and STOPS — extension is an officer decision (a follow-up ticket),
    never a worker decision.
  - **Deliverable = a decision note**: findings + residual unknowns + recommendation,
    written to the KB. Code is disposable evidence — it lives and dies in the job's
    isolated repo and is never delivered to the project cloud folder.
  - **Names its consumer**: the ticket says which decision or follow-up ticket the answer
    feeds, so the research pool cannot drift into producing shelfware notes.
  - Evidence class = citations, measured comparisons, named sources. Design research is
    first-class and has more objective anchors than we ever used: current design systems
    (Material 3, Apple HIG), WCAG success criteria by number, type scales, spacing systems.
  - For A-vs-B comparisons: criterion-by-criterion evidence *before* the verdict, never a
    one-shot "which is better" — order effects alone flip single-pass pairwise judgments
    **[R-org]**.
- **tester** — *"Your output is issue tickets, not file commits."* Absorbs product-qa's
  contract: 3–7 high-leverage findings max, each its own `issue` ticket with severity,
  confidence, evidence (repro ≥2× — or the audit trail for absences: "no UI exists" is a
  HIGH finding even when every unit test passes), smallest useful remediation. "No blocking
  issues found" is a valid outcome. For UI work the critique is **anchored, not vibes**
  **[R-org]**: each finding names the violated anchor — a Nielsen heuristic (the canonical
  published rubric), or a WCAG 2.2 floor: SC 1.4.3 contrast, SC 2.4.7 focus visible,
  SC 2.5.8 target size ≥24×24 CSS px (44pt/48dp per platform practice). An axe-core scan is
  the cheap deterministic floor — it catches ~57% of accessibility issues, which is exactly
  why it layers *under* judgment rather than replacing it.
- **executor** — *"Ship it, prove it appropriately, report it honestly."* Delivers under
  `projects/<slug>/`; the P1-C deliverable contract (`required_deliverables` →
  `services/deliverable_gate.py`) is the only hard check. Evidence appropriate to the
  claim: pytest for logic (regression rails, never the score), **screenshots for UI**,
  a running `docker compose up` + health curl for hosting, style-guide conformance for
  design. Screenshot evidence means a **reproducible capture** — fixed viewport, settled
  fonts, seeded demo data — so the officer compares like with like across iterations
  (visual-regression hygiene; unpinned captures flake on animations/fonts/dynamic content)
  **[R-org]**. UI executor tickets **must reference the researcher-produced style guide**
  they conform to — without that link, style drifts across executor runs exactly as
  ChatDev's designer assets did **[R-fw]**. Tick-dispatched executor jobs carry a default
  per-ticket budget/wall-clock cap (industry norm: Copilot's hard 59-minute session,
  Devin's per-session ACU limits) via the existing budget-freeze machinery **[R-fw]**.

**Close-checklists** **[R-org]**: each category block ends with a short **binary**
checklist the officer runs at ticket close — binary criteria yield the highest judge
agreement, and rubric-anchored judging beats freeform. Examples: researcher — "decision
note at declared slug: y/n; residual unknowns named: y/n; recommendation actionable: y/n".
executor(UI) — "deliverables at declared paths: y/n; screenshots reproducible-capture: y/n;
axe criticals = 0: y/n; style-guide reference honored: y/n". The officer's multimodal pixel
review is **triage anchored to these floors, not terminal QA** — LLM UI evaluation measures
below human evaluators, and repeated critique of the same screen has measured diminishing
returns — so one anchored review per close, and disagreements escalate to the Legate
rather than looping **[R-org]**.

A worked example of the many-to-many point: a ticket "compare rendering approach A vs B for
the calendar view" tagged `category:researcher expert:developer` gets a developer in spike
mode — throwaway code, a decision note with numbers. The follow-up "implement the winner"
tagged `category:executor expert:developer` gets the same expert under the delivery
contract, referencing the decision note.

## 4. Tickets — category on the backlog note

Category, expert pin, and readiness ride the existing `tags TEXT[]` on `knowledge_index`.
**[A2]** verified the round-trip: `kb_write` forwards tags verbatim, frontmatter YAML-quotes
them (colons safe), and the reindexer parses them back unfiltered — but found four
mechanical corrections, folded into B2:

- **Tags are currently add-only** — `kb_update` exposes only `add_tags` (merge, no removal;
  `src/tools/knowledge/knowledge_tools.py:1086-1090`). Without a removal path, `ready` is
  irrevocable and a category change leaves the ticket matching *two* pools. **B2 adds
  `remove_tags` (or `set_tags`)**; both mirrors propagate automatically. Until a ticket has
  exactly one `category:` and at most one `expert:` tag, the tick treats it as ambiguous —
  skip + sitrep line, never pick one.
- **Lowercase is mandated** for machine tags (`kb_update` lowercases, `kb_write` and the
  reindexer preserve case — normalize `lower()` at both write paths).
- **Query form**: a GIN index on tags has existed since vector 0001 (`idx_knowledge_tags`);
  the containment form `tags @> ARRAY['ready','category:researcher']` is indexable, `= ANY`
  is not. The partial backlog index (0014) narrows to per-project active tickets first, so
  60s cadence is comfortably cheap. (The original "dedicated column later" caveat is
  withdrawn.)
- **Machine tags are excluded from search**: agent-side `upsert_note` builds the sparse
  `search_doc` from tags today (the reindex path doesn't — two write paths, two tsvectors);
  B2 excludes the machine namespace (`ready`, `category:*`, `expert:*`, `parallel-safe`)
  from search text so ticket plumbing never pollutes hybrid-search ranking or worker KB
  injection.

Rules:

- **Anyone may file; the officer owns classification.** Workers file tickets uncategorized;
  triage — category, priority, expert pin, splitting a fat ticket into spike + story — is
  the officer's standing backlog-keeper duty.
- **`ready` is not a label, it is a dispatch authorization — so it carries provenance.**
  **[X]** Worker-side kb tooling strips `ready` and `parallel-safe` on write (the S7
  charter provenance mechanism — `_thread_id` discriminates sessions from workers — is the
  precedent); only officer/Legate sessions can set them. Otherwise a worker (or prompt-
  injected web content a researcher summarizes) self-authorizes dispatch onto the VM-backed
  executor slot. The tick additionally validates `expert:` tags against the §2 roster —
  `config_name` is otherwise unvalidated until agent boot, where a typo would chain-trip
  the pool breaker **[A3]**.
- **Ready ≠ well-formed.** A ticket may not be marked ready until it carries the four brief
  fields Anthropic's fan-out work showed prevent duplicated/vague parallel work: objective,
  output format, tool/source guidance, boundaries **[R-fw]**. The category contract
  supplies the output format half by default; the officer's triage supplies the rest.
  Priority stays a label; `ready` gates (this is the one deliberate amendment to
  `project_backlog.py`'s "priority never gates" law).
- **Editing a queued ticket is the cheap steering channel.** Until claimed, `kb_update`
  rewrites description/category/expert freely. After dispatch, the P1-A guidance lane is
  the steering channel; post-dispatch preemption (cancel + requeue) is the expedite lane in
  disguise — rare, logged, officer-justified **[R-org]**.
- **Closing is judgment, never automation.** The officer's close instrument is
  **`kb_update(status='resolved'|'archived')`** — he has the full knowledge toolset via
  session_base, and kb_update performs the same file+index mirror; `close_backlog_ticket`
  is orchestrator-internal (loop verdict mirroring) and unreachable from sessions **[A2]**.
  Close failures must surface in the officer's tool result — never silent best-effort for
  the actor who owns disposition **[X]**. He runs the category's binary close-checklist
  (§3) and reads the worker's **declared, pinned evidence** — the KB report, reproducible
  screenshots, completion report, and deliverable checks — through
  [[officer_supervision_surface]], never an orchestrator summary or arbitrary workspace
  path; the supervisor "translation layer" is a measured failure mode. If evidence is
  insufficient, he delegates a tester/recon job rather than browsing the object plane
  **[R-fw]**.
- **Claimed state is visible.** `render_backlog_block` marks claimed tickets
  ("claimed by job <id8>", GitHub's 👀 analog) even though claim state stays derived,
  never written to the note **[R-fw]**.
- A reindex quirk to know: unrecognized frontmatter `status:` values are coerced back to
  `active` **[A2]** — a hand-edited `status: done` would resurrect a ticket into the pool.
  One-shot claims (§5.3) make the resurrected ticket ineligible anyway; the sitrep flags
  it for the officer.

## 5. The ready queue and the auto-pull tick — the one new mechanism

A **replica-safe** 60s tick (new service `orchestrator/services/officer_backlog.py`),
mounted under `run_when_leader` like the officer watchdog (`orchestrator/services/
leader_election.py`) — **[A1]** the loop sweeper the draft cited is *not* leader-gated, and
leadership here is an optimization, not the guard: dual-leader windows are real and
acknowledged in-tree, so correctness comes from claim atomicity (§5.3), never from
leadership **[X]**.

Per commissioned officer with `auto_pull=true`:

1. **Skip** if the officer is held — checked directly on the thread/officer row
   (`config_override.officer.hold`), not by bouncing off the funnel's 409 **[A1]** — or
   decommissioned, or the century's **daily worker-spend ceiling** is exhausted: one
   `usage_ledger.query_usage(scope_project_id=…)` call, the same query the officer card's
   `spend_today` makes; fail-open like the officer's own ceiling; exceeded → skip + digest
   line **[A1][X]**. (Officer `daily_token_ceiling` bounds only his session turns; without
   this, worker spend is bounded only by slot counts × time — the foraging pump is
   doctrine-legitimate, so the brake must be mechanical.) A KB/pgvector outage → skip
   cleanly this tick; infra failures never feed breakers **[X]**.
2. **Compute free capacity** per pool. **[X]** The capacity and executor-serialization
   predicate must equal the claim predicate — **all non-terminal statuses** (`created`,
   `processing`, `waiting`, `paused`, `pending_review`) — not the funnel's
   `('created','processing')`: a paused executor must still occupy the singleton, or its
   redispatch races a second executor into the same story. Temporary under-use converges
   downward; two executors never converge.
3. **Fetch eligible tickets** for categories with free slots: `status='active' AND tags @>
   ARRAY['ready','category:<X>']`, small LIMIT per category, no counts pass (the counts
   query belongs to ready-depth reporting at officer wake, not the tick) **[A2]**.
   **Eligible additionally means unclaimed under one-shot semantics (§5.3).** Re-check
   `ready` by note_id immediately before stamping (vector-DB fetch and app-DB insert cannot
   share a transaction; the residual window is accepted — the guidance lane steers
   post-dispatch) **[X]**.
4. **Dispatch at most one job per pool per tick** through the **extracted admission
   helper + internal spawn path** **[A1]**: there is no callable "funnel" — `create_worker_job`
   is an HTTP handler, and every server-side precedent (`_spawn_loop_job`, automations)
   bypasses it and its checks. B3 extracts the officer-admission block (hold fence,
   advisory-locked slot count, `admit()`, slot config patch — `main.py:12318-12378`) into a
   helper shared by the endpoint and the tick; the tick then mirrors the loop spawn:
   `db.create_job(created_by_thread_id=<officer thread>, user_id=<thread owner>,
   wake_on_complete=True)` + `provision_job_repo` + `_trigger_dispatch`, running
   `_enforce_job_create_grants` against the owner explicitly. Tick jobs stamp
   `context.ticket_note_id`, `context.work_category`, `context.officer_slot` — and
   **autonomy `full`** **[X]**: on any non-full-autonomy project, completions otherwise
   park in `pending_review` (the lane with a live dead zone), holding claims forever and
   double-reviewing what the officer already reviews. Loop jobs carry exactly this
   exemption (`main.py:16727`); tick jobs get the same, for the same reason: the officer
   IS the reviewer. Expert = the ticket's validated `expert:` pin, else the category
   default (researcher→scholar, tester→product-qa, executor→developer). The kickoff rides
   `context["kickoff_message"]` (ticket body = description; category block + charter
   context in the kickoff) — **never `instructions`, which silently replaces the rendered
   instructions.md template** **[A3]**.
5. **Executor gating**: at most one executor job in flight (non-terminal) across all
   executor pools unless the officer tagged the ticket `parallel-safe` — and a
   `parallel-safe` tag must *name the non-overlapping write surfaces* in the ticket, not
   just assert safety **[R-fw]**. Additionally, the next executor pull requires the
   previous executor claim ticket **dispositioned** (closed or explicitly re-readied), not
   merely terminal **[X]**: the deliverable gate checks existence, never content, so
   without this an executor chain builds on unreviewed work and review debt compounds
   directly into the deliverable. Fail-safe direction: if the officer stops reviewing,
   executors stop.
6. **Pool circuit breaker**: 2 consecutive **job failures** (`status='failed'`) on
   *distinct tickets* in a pool open that pool's breaker for 30 min. Honest
   `goal_achieved=false` completions never count **[R-org]**; tick-side exceptions and
   infra outages log and retry without counting **[X]**. An open breaker pauses **only its
   own pool** (the draft's "skip the officer entirely" contradicted its own acceptance
   criterion — fixed) **[X]**. Breaker and ramp state persist in the thread's
   `officer_state` (runtime plane), harvested to `project_officers.state` at decommission,
   per officer_post's writers-per-direction law — never in tick memory (restart/deploy
   would silently re-arm a chain-burn) **[A1][X]**.

### 5.3 One-shot claims — the claim ledger

**[X]** The original design ("claimed iff a *non-terminal* job carries the stamp") had a
fatal liveness bug: disposition is async and officer-only, so every *terminal* job released
its claim minutes before the officer could close the ticket — the tick would re-dispatch
every completed ticket, and re-burn every failing ticket at breaker cadence forever.
Claims are therefore **one-shot**:

- A ticket is **claimed** iff any job — in **any status, terminal included** — carries its
  `context.ticket_note_id` stamp with `created_at` newer than the ticket's last
  ready-authorization. Dispatch consumes readiness.
- **Re-arm is explicit**: the officer re-sets `ready` after reviewing the outcome; the
  tick's eligibility query compares the ready-authorization timestamp against the newest
  claiming job's `created_at`. (Implementation: the ready re-arm writes a timestamp the
  query can compare — a `ready_at` value in the note's frontmatter/index row, set by
  kb_update — so "re-ready" is one officer action.) Job DELETE removes the claim row and
  is thereby the deliberate manual re-arm, matching how loop claims already die with their
  job rows.
- This single change is also the **item-level dead-letter queue** **[R-fw]**: a failed
  ticket stays parked until the officer looks — SQS's DLQ pattern without new machinery —
  and it defuses the reindex-resurrection quirk (§4).
- **Claim+create is atomic** **[A1][X]**: the draft's "checked inside the lock" was false
  comfort — the funnel's advisory xact lock releases at transaction close, *before* any
  job INSERT, so check-then-insert daylight remains (the in-tree comment overstates the
  lock; a real two-replica double-spawn is on record). The tick performs claim check +
  capacity count + job INSERT in **one transaction holding the lock** (a conn-accepting
  `create_job` variant), and B3 adds the fail-closed backstop: a partial unique index on
  `((context->>'ticket_note_id'))` over claim-bearing jobs, so a racing double-claim fails
  the second INSERT instead of double-working the ticket.
- **The officer's own dispatches claim too** **[X]**: `create_worker_job` gains an optional
  `ticket=<note_id>` parameter routed through the same helper — one claim ledger for tick
  and officer. Without it, the officer manually working the top ready ticket races the
  tick into double-work on the very next cycle.
- **Stale-claim surfacing, never auto-release** **[A1][R-fw]**: paused/pending_review jobs
  free nothing under one-shot semantics, but they can sit for days. Mature queues pair
  every claim with a liveness signal (visibility timeouts, heartbeats); ours is the
  officer: tickets whose claiming job has had no status change for >T (default 4h) render
  as "claimed-but-stalled" on the capacity/sitrep line with the **oldest claim age**, and
  resolving them (resume/cancel the job, then close or re-ready the ticket) joins the
  officer's standing orders. Auto-release is deliberately rejected — it recreates Celery's
  silent-duplicate-execution failure; a second job for a claimed ticket must never exist
  until the officer releases the first.

The officer may still dispatch directly for ad-hoc work — auto-pull is additive. Direct
dispatch **above** pool capacity is an explicit expedite: limit 1 in flight, logged in the
sitrep — unpoliced expedite is how pull systems lose their flow guarantees **[R-org]**.

## 6. Slots — pools with a category

`officer_slots.py` changes, all backward-compatible (`roster_from_meta` passes unknown keys
through, so category-bearing and legacy rosters coexist **[A3]**):

- `_SPEC_KEYS` += `category`; `validate_slots_spec` accepts it (single caller: the
  session-create officer sanitizer). A slot without `category` behaves exactly as today —
  officer-directed only; the tick never fills it.
- **Precedence law** **[A3]** (mirror of "the slot patch always wins over the tool call"):
  the **slot's category decides the contract block**; the **ticket's tags decide the
  expert default**; the **officer's explicit arguments win** over both. A mismatch (e.g.
  direct dispatch of a `category:researcher` ticket into an executor slot) is not refused —
  it is named in the kickoff ("dispatched cross-category by the officer") and logged, so
  warn-not-forbid stays true but silent contradiction is impossible.
- `capacity_lines()` gains a `ready_by_category` argument (it is pure; the ready-depth
  counts come from `fetch_backlog` in the **vector DB**, which the sitrep assembler must be
  plumbed to pass — `_capacity_section` holds only the app-db handle today) **[A3]**:
  `"Capacity: researchers 1/2, testers 0/1, executors 1/1 in use; ready 4R/2T/1E; oldest
  claim 3h."`
- **The officer card computes neither utilization nor ready depth today** **[A3]**:
  `get_project_officer_summary` returns the raw slots spec only. B6 adds
  `slots_in_flight` (the admission's GROUP BY query) + `ready_depth` maps and extends
  `OfficerSummary` in `project-officer.component.ts`. **Policies are rendered, not just
  enforced** **[R-org]**: floor N, breaker state + opened-cause, singleton rule, spend
  ceiling — a policy the tick enforces but the officer can't see invites doctrine drift.
- **Cockpit**: `buildSlotsSpec` silently DROPS unknown fields today, and
  `OfficerSlotSpec`/`SlotDraft`/form rows/chip renderer/spec all lack category — until B6
  lands, a category cannot even be provisioned from the UI **[A3]**.

Example Resavio kit (cross-family rule visible: sol officer, MiniMax pools):

```yaml
officer:
  enabled: true
  auto_pull: true
  worker_spend_ceiling_daily: 15.0   # per-century brake, §5.1
  slots:
    researchers: {count: 2, model: "MiniMax-M3",  backend: "sandbox", category: "researcher"}
    testers:     {count: 1, model: "MiniMax-M3",  backend: "sandbox", category: "tester"}
    executors:   {count: 1, model: "gpt-5.6-sol", backend: "vm",      category: "executor"}
```

## 7. Kickoff composition and the loop prompt edits

`_CATEGORY_BLOCKS` composes with the expert config at spawn: category contract + expert
identity + ticket body + charter context. Channels per path **[A3]**: the tick writes
`context["kickoff_message"]` (§5.4); for direct `create_worker_job` dispatches into a
categorized slot, the funnel appends the slot's category block server-side right after
admission — and never via `instructions`, which replaces the instructions.md template
wholesale.

`_ROLE_BLOCKS` in `project_loops.py` slims accordingly — the hook point is the
`role_block =` resolution (`project_loops.py:778`), composing
`_CATEGORY_BLOCKS[role_to_category(role)]` + a slimmed identity block; campaign/planner
appendices attach after and stay untouched. **[A3]** The composed kickoff must remain a
superset of the currently pinned strings (`tests/test_project_loops.py:272-326` — role
headers, "`issue` note", critic's "backlog pool"/"first-class"/"verdict" language;
`tests/test_kb_convergence.py:436-467` — the preamble; `tests/test_project_backlog.py` —
backlog block) or B5 explicitly lists which pins move to category-block tests.

Two incentive-side prompt edits ship with this feature (they repair every loop, officer or
not): `build_loop_kickoff`'s "make ONE solid, **verifiable** increment"
(`project_loops.py:790-792`) becomes "one solid increment **with evidence appropriate to
the work** — tests for logic, screenshots for UI, a running deployment for hosting,
citations for research"; the critic rubric's "evidence quality" (`project_loops.py:472`)
becomes "evidence **appropriate to the claim**". **[A3]** No existing test pins either
phrase — the edit adds pins for the new wording rather than updating old ones. (The critic
edit and the B5 critic-block slimming touch the same string — one change, not two.)

## 8. Doctrine — floors on backlog health, not dispatch

Charter posture text (officer-edited block, per centurion S7 split-write authority):

- **An empty ready queue is waste; an idle slot with a healthy queue is slack — and slack
  is healthy.** **[R-org]** (Reinertsen: high utilization inflates queues and lead time;
  WIP limits exist to hold utilization *below* 100%. The draft's "idle slots are waste" is
  withdrawn — the congested resource is the officer's **review attention**, and §5.5's
  disposition gate is what protects it.) Your standing duty is the queue: every pool's
  ready backlog holds ≥2 tickets.
- **A floor breach wakes you** — event-driven replenishment (the kanban order-point
  pattern), not a passive card number **[R-org]**: the tick files an officer wake when a
  pool's ready depth crosses below its floor.
- A starved pool is a *signal you act on*, never a quota you pad: solicit that kind of work
  (a foraging researcher ticket is legitimate), or surface it to the Legate in the digest.
  Manufactured filler is a doctrine violation — and it is **watched, not merely forbidden**
  **[R-org][X]** (Goodhart-hardening; the min_todos=5 floor produced padding *because*
  nothing watched it): the digest reports per pool the self-filed-ticket ratio and tickets
  closed as no-value after dispatch.
- **The anti-amplification invariant** **[R-fw]**: worker-filed tickets are invisible to
  the tick until you stamp `ready`. This firewall is what separates the century from
  AutoGPT's plans-spawning-plans; there is no bulk-ready convenience, and there never will
  be. (Acceptance tests it: a tick job files 10 tickets → zero dispatches until triage.)
- Review on declared evidence, close on the checklist, guidance over replans, preemption is
  expedite (rare, logged); escalate on the existing ladder (log → digest → page).

## 9. What stops being gated (and what remains)

Removed / demoted:

- Per-job provability as a selection criterion (§7 edits).
- The critic-as-selector and campaign `loop_plan` machinery for officer-commanded projects.
- Test counts as a quality score anywhere. Tests are regression rails on executor work.

Remaining hard gates, exactly two:

1. **Truthful completion reports** — the campaign honesty contract, generalized: mid-arc
   scaffolding is licensed; claiming working what isn't is the one sin. The breaker's
   job-failures-only rule (§5.6) exists to keep this gate untaxed: honest negative results
   must cost nothing **[R-org]**.
2. **Deliverable contracts** (executor tickets that declare them) — objective existence
   checks, cap-2 bounce via the guidance lane, exactly as P1-C shipped them.

Everything else is evidence the officer weighs — with the checklist making the weighing
structured (§3), cross-family separation making it less biased (§2), and no auto-close
ever making termination authority his alone **[R-fw]**. Above him, the Legate model: the
loop runs free by default; when output disappoints, the user steps in with charter and
ticket edits, not more gates.

## 10. Relationship to existing machinery

- **officer_post** ([[officer_post]]): prerequisite, including **O2 lineage-aware capacity
  before B3** **[X]**. Config vs runtime split per its writers-per-direction law **[A1]**:
  `auto_pull` + `worker_spend_ceiling_daily` live in the row's `config_override` (mirrored
  into thread metadata at commission/PATCH); breaker/ramp/queue state lives in the thread's
  `officer_state` while commissioned (via `merge_thread_officer_state`, like
  digest/ceiling_notice today) and is harvested to the row at decommission. Migration
  numbers: **next free at implementation time** — the draft's "0087" is stale (app
  migrations already exceed 0140) **[A1][A3]**.
- **Unified loop engine** ([[loop_unified_engine]]): untouched. `scheduling='officer'`
  loops keep working; this feature makes "the officer decides what runs next" concrete.
  Legacy loops keep name-based role classification (§2) and gain only the §7 prompt edits.
- **Backlog pipeline** (`project_backlog.py`): extended, not replaced — same notes, same
  index, same close mirror for loop verdicts; officer closes via kb_update **[A2]**; one
  deliberate amendment to the priority-is-a-label law (§4).
- **Verification-repricing** ([[loop_campaign_scheduling]]): honesty contract survives as
  gate #1; the campaign vehicle is superseded on officer projects.
- **Officer knowledge plane** ([[officer_knowledge_plane]]): the KB/backlog is the
  authoritative writable planning plane. The background officer receives no repository,
  project-cloud, workspace-upgrade, or arbitrary file capability.
- **Officer supervision** ([[officer_supervision_surface]]): supplies scoped status/audit/
  messages and the proposed bounded evidence manifest used at disposition. A summary is
  navigation only; absent evidence triggers tester/recon delegation.
- **Worker-message routing** ([[officer_message_routing]]): prevents a blocking worker
  question from indefinitely occupying a one-shot claim and pool slot. Officer-first does
  not ship without unavailable-officer fallback and the total-timeout reconciler.
- **Unified job surface** ([[unified_orchestrator_tool_surface]]): remains the only client/
  descriptor/formatter implementation. The officer receives its control and observability
  subset, not MCP's operator/object catalogue.

## 11. Build delta (slices)

**Pre-B3 gates outside this document:** officer_post O2; officer knowledge K1–K3;
supervision E1–E3 plus E4 or the explicit recon-only disposition fallback; message routing
M2–M4 for any worker that can block on `send_message`. Do not duplicate those mechanisms in
the tick.

- **B1 — `work_categories.py`**: category constants, membership map, `_CATEGORY_BLOCKS`
  (with effort ceilings, spike discipline, anchored-rubric text, close-checklists),
  `role_to_category` for legacy loops. Pure module + unit tests.
- **B2 — ticket plumbing**: `remove_tags`/`set_tags` on `kb_update` **[A2]**; lowercase
  normalization on both write paths; machine-tag exclusion from `search_doc` at write;
  worker-side stripping of `ready`/`parallel-safe` (provenance) **[X]**; `fetch_backlog`
  trailing optional category/ready filters using `tags @>` containment; claimed-marker in
  `render_backlog_block`; ready-authorization timestamp (`ready_at`) on the index row.
  Test updates: `tests/test_project_backlog.py` exact-line pins + positional SQL slots.
- **B3 — claim funnel + tick** (the bulk): extract the officer-admission helper from
  `main.py:12318-12378`; conn-accepting `create_job` variant so claim check + capacity +
  INSERT share one advisory-locked transaction **[A1]**; partial unique index on the ticket
  stamp (fail-closed double-claim backstop); one-shot eligibility query (ready_at vs newest
  claim `created_at`); `ticket=` parameter on `create_worker_job` through the same helper
  **[X]**; internal spawn path mirroring `_spawn_loop_job` (+ `provision_job_repo` +
  `_trigger_dispatch` + explicit grant check); autonomy-`full` stamp (loop-exemption
  precedent `main.py:16727`); non-terminal capacity/serialization predicate; executor
  disposition gate; spend-ceiling check via `query_usage(scope_project_id=…)`; per-ticket
  default budget caps by category; breaker in `officer_state` (job-failures-only,
  per-pool); floor-breach officer wake; stale-claim detection; `run_when_leader` mount.
  Stale-claim classification reuses [[officer_supervision_surface]] E3's shared liveness
  result; the tick does not invent a second `updated_at` threshold.
  Observability: per-tick log line `officer=<id8> pool=<name> dispatched=<note>/<job8> |
  skip=<reason>` **[X]**.
- **B4 — slots**: `category` in `_SPEC_KEYS`/validation; precedence law in admit/kickoff;
  `capacity_lines(ready_by_category=…, oldest_claim_age=…)` + sitrep vector-db plumbing
  **[A3]**.
- **B5 — prompts & doctrine**: §7 edits + new-wording pins; `_ROLE_BLOCKS` slimming with
  the pinned-string superset commitment **[A3]**; charter posture template (§8).
- **B6 — cockpit**: `OfficerSlotSpec`/`SlotDraft`/`buildSlotsSpec`/form/chips + spec gain
  `category` (buildSlotsSpec drops unknown fields today) **[A3]**; `OfficerSummary` +
  `get_project_officer_summary` gain `slots_in_flight`/`ready_depth`/breaker/spend fields;
  digest lines (dispatches/day, spend/day, re-ready counts, self-filed ratio, breaker
  cause).
- **B7 (optional) — `writer` expert**: minimal shape per the `general-worker` precedent
  (config.yaml `$extends: worker_base` + persona.txt; strategic/tactical optional)
  **[A3]**. Test touchpoints: regen `tests/test_config_tool_grants_snapshot.py`
  (`UPDATE_TOOL_GRANTS_SNAPSHOT=1`), add the roster row in
  `config/skills/app-guide/references/experts.md` (pinned), `test_config_tool_names…`
  auto-covers, brace-safety only if prompts contain literal braces; do NOT touch
  `MANAGED_SEEDS`.

## 12. Acceptance — Resavio century (dev)

Pre-requisites: officer_post O1–O6 done (incl. O2 lineage capacity), knowledge-plane K1–K3,
supervision E1–E3 and the chosen disposition-evidence path, message-routing M2–M4, and the
Resavio officer released; KB hygiene — retire the 0.92-confidence "No renderer available" RecallStore
belief (the render stack was fixed 2026-07-17; the project's memory still says agents are
blind) and run `assert-browser-stack` once on a live workspace.

In order:

1. PATCH the kit to the §6 roster + `auto_pull=true`; charter carries the Demo Definition.
2. Officer triages the pool: categories + four-field briefs + `ready` on ≥2 tickets per
   pool. Card shows per-pool utilization, ready depth, and policies.
3. Within one tick of a researcher slot being free, the top ready researcher ticket
   dispatches — verify `ticket_note_id`/`work_category`/`officer_slot` stamps, autonomy
   `full`, the category block in the kickoff message, and the per-tick log line.
4. **One-shot claims**: the researcher job completes → its ticket does NOT re-dispatch on
   subsequent ticks; after the officer closes it, still nothing; a deliberately failed
   ticket parks (no re-dispatch) until the officer re-readies it — then exactly one new
   job.
5. **One ledger**: officer manually dispatches a ready ticket via `create_worker_job
   (ticket=…)` → the tick never double-dispatches it.
6. **Provenance**: a worker job files a ticket tagged `ready` → the tag is stripped/absent;
   the tick dispatches nothing until the officer readies it (anti-amplification test: tick
   job files 10 tickets → 0 dispatches).
7. A `category:researcher expert:designer` ticket ("design direction: cited style guide +
   direction boards") completes with a decision note citing real design systems and WCAG
   SC numbers — no project-cloud delivery.
8. A follow-up `category:executor expert:designer` ticket delivers a styled page under
   `projects/<slug>/` with reproducible-capture screenshots + axe report, referencing the
   style guide; deliverable contract passes; the officer reads the pinned evidence manifest
   (not the project folder) and closes via checklist.
9. **Serialization under pause**: with one executor job paused (kill its agent), a second
   ready executor ticket does NOT dispatch; the sitrep shows the claim age; officer
   resolves (cancel + re-ready) and the next pull proceeds only after disposition.
10. Hold the officer → tick quiet (no dispatches, no 409 spam); release → refills.
    Spend-ceiling drill: set `worker_spend_ceiling_daily` below today's spend → tick skips
    with a digest line.
11. Breaker: two distinct-ticket job failures in one pool (throwaway bad-model slot) →
    that pool pauses 30 min with cause on the card; **other pools keep dispatching**; an
    honest `goal_achieved=false` completion does NOT count toward it.
12. **Blocking-message liveness:** a claimed executor asks a blocking question under
    `officer_first`; the officer answers or escalates the same thread. Repeat with the
    officer held and with both deadlines expired; no job/claim/slot remains stranded.
13. **The meta-acceptance**: across the first supervised week, design/UX tickets are
    selected and shipped *without the Legate forcing them* — the starvation pathology is
    structurally gone, and the officer's digest reads like a staffed studio, not a test
    factory.

## 13. Open questions (Legate)

1. `auto_pull` default at ship: **off, flipped per-century during acceptance** (rec) — or
   on for every new commission?
2. Ready-depth floor default 2 per pool (rec)? Floor-breach wake debounce (rec: once per
   pool per 6h)?
3. `worker_spend_ceiling_daily` default (rec: set per-century at commission, no global
   default — cost profiles differ too much between MiniMax and sol pools).
4. Per-ticket budget-cap defaults per category (rec: researcher < executor; concrete
   numbers at implementation after measuring current job costs).
5. Stale-claim threshold T (rec: 4h) and whether `pending_review` claims page after 24h.
6. `writer` expert in the first wave or after the first acceptance week?

Resolved since the draft by the research round: breaker semantics (job-failures-only,
per-pool — §5.6); category storage (tags + GIN `@>`, no column — §4); executor parallelism
(singleton + named-write-surfaces `parallel-safe` — §5.5, evidence-backed, no longer open).

## 14. Decision log

- **2026-08-13** — Design session: categories researcher/tester/executor; category=work/
  expert=worker split; floors on backlog health not dispatch; auto-pull tick for machine
  liveness; hard gates reduced to truthfulness + deliverable contracts; tests demoted to
  regression rails; evidence repriced per category; campaign planning superseded by officer
  + ready queue on officer projects. Context: month-long UI starvation on Better Resavio
  traced to selection bias (role vocabulary, self-inferred DoD, per-job provability
  pressure, dead campaign mechanism), not gate rejections — no UI ticket ever existed to
  reject.
- **2026-08-13 (same day)** — Six-agent research round. Five must-fix changes adopted:
  **one-shot claims** (terminal jobs held no claim → every finished/failed ticket
  re-dispatched) **[X]**; **one claim ledger** (`ticket=` on create_worker_job; extracted
  admission helper + internal spawn — no callable funnel existed) **[A1][X]**;
  **ready/parallel-safe provenance** (worker-writable ready = privilege escalation)
  **[X]**; **capacity predicate = claim predicate** (paused executors broke the singleton)
  **[X]**; **autonomy-full stamp** (tick jobs otherwise strand in the pending_review dead
  zone) **[X]**. Plus: breaker honesty rule + per-pool scope **[R-org][X]**; spend brake
  via existing per-project usage query **[A1]**; spike discipline + anchored rubrics +
  binary close-checklists **[R-org]**; four-field ready precondition, effort ceilings,
  raw-artifact review, anti-amplification invariant, expedite discipline, slack-not-waste
  doctrine rewording **[R-fw][R-org]**; tag mechanics corrections (remove_tags, lowercase,
  `@>`, search_doc exclusion, kb_update-as-close) **[A2]**; channel/precedence/cockpit
  precision **[A3]**.
- **2026-08-14 (operating-surface integration):** backlog authority is explicitly KB-only;
  the background officer has no project/repository/cloud object plane. Disposition uses
  bounded declared evidence or a tester/recon report. B3 now waits on the shared
  observability/liveness contract and blocking-message fallback/timeout, because a
  `waiting_for_reply` job deliberately retains both its one-shot claim and pool capacity.
  The original six §13 backlog defaults remain the only decisions owned by this document;
  evidence and message-policy defaults live in their respective feature docs.

## Prior art & evidence

Codebase audits: **[A1]** dispatch/claim/tick seams; **[A2]** ticket/tag/KB plumbing;
**[A3]** kickoff/config/cockpit — findings inline above with file:line anchors.

External sources (**[R-fw]** frameworks/fleets, **[R-org]** process/design/judging):

- **Anthropic, "How we built our multi-agent research system"** — four-field subagent
  briefs (objective, output format, tool guidance, boundaries) fix duplicated parallel
  work; explicit effort-scaling rules stop 50-subagent overkill; async supervision named
  as the fix for the slowest-subagent bottleneck. anthropic.com/engineering/built-multi-agent-research-system
- **Cognition** — "Don't Build Multi-Agents" (parallel writers make conflicting implicit
  decisions) and MultiDevin (manager reads full worker trajectories; fan-out only for
  isolated, objectively verifiable subtasks). cognition.com/blog
- **LangChain multi-agent benchmark** — supervisor "translation layer" measurably loses
  information; feed judges raw artifacts. langchain.com/blog/benchmarking-multi-agent-architectures
- **GitHub Copilot coding agent** — issue-as-work-unit, visible claim (👀), draft-PR
  boundary, hard 59-minute session cap, Playwright screenshots in PRs as UI evidence.
  docs.github.com / github.blog
- **MetaGPT (ICLR'24), ChatDev, CAMEL (NeurIPS'23)** — role-SOP ablations (structured
  artifacts > role names); designer-role UI quality unmeasured; worker self-termination
  unreliable → external termination authority. arxiv.org/abs/2308.00352, 2307.07924
- **Queue practice** — SQS visibility timeout + DLQ; Temporal heartbeats; Celery
  visibility-timeout duplicate-execution failure (idempotency requirement); Postgres
  SKIP-LOCKED lease patterns. Claims need liveness signals; ours is officer surfacing, not
  auto-release.
- **Devin ACU caps / Google Jules per-day task caps** — two budget layers (per-item,
  per-fleet) are shipping practice; caps without a queue push queueing outward.
- **AutoGPT self-referential planning failures; CrewAI delegation caps** — the
  anti-amplification firewall (non-agent ready-stamp) and manager-only delegation.
- **Anderson, *Kanban*; Reinertsen, *Principles of Product Development Flow*** — per-class
  WIP allocation as starvation fix; slack vs utilization; order-point replenishment;
  expedite-class discipline; explicit visualized policies; WSJF tie-breaking (considered,
  not adopted for v1).
- **SAFe/XP spike discipline** — timeboxed, knowledge-deliverable, named consuming
  decision, no self-extension.
- **Nielsen Norman 10 usability heuristics; WCAG 2.2 (SC 1.4.3, 2.4.7, 2.5.8); Deque
  axe-core coverage study (~57%); Percy/Chromatic visual-regression hygiene** — the
  anchored-rubric stack for subjective UI work.
- **UICrit (UIST'24); Duan et al. (CHI'24)** — LLM design critique below human evaluators,
  useful as anchored triage; diminishing returns on repeated critique of one screen.
- **Panickssery et al. (NeurIPS 2024); Zheng et al. (judge biases); Wang et al. (order
  effects); familiarity-bias follow-ups** — self-preference tracks self-recognition and
  survives anonymization; rubric-anchored binary criteria + cross-family separation are
  the effective levers.
- **Goodhart/Strathern; watermelon-status literature** — watched floors, free honest
  failure.
