---
tags:
  - feature
  - plan
  - orchestration
  - officers
  - projects
  - self-improvement
status: implemented-B1-B7-audit-blocked
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
  - "[[officer_control_plane_post_implementation_audit]]"
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

**BUILT BUT RELEASE-BLOCKED (updated 2026-08-17).** **B1–B7 have landed; BP-05/BP-06
and the BP-07/BP-08/BP-10 correctness tranche are complete locally and undeployed**, and
the feature is not yet safe or operable end to end. The tick is built, mounted, dormant
(`auto_pull` ships
off) and verified on its happy path; the sitrep and
the cockpit card both render capacity, ready depth, open breakers and stalled claims; a
pool can be provisioned from the UI; and **every loop carries the category doctrine whether
or not it has an officer**, so the evidence repricing reaches the plain `standard` loops
that produced the original failure. **B1–B7 are all built.** O6 released the Resavio
officer successfully with `auto_pull=false`; the committed live-fire scorecard remains in
progress at [[officer_backlog_pools_resavio_livefire]]. What remains includes the deferred
digest lines and completion of that acceptance run, including its outstanding hygiene and
live observations. More importantly, the supported post API cannot currently enable
`auto_pull`, and the authority/atomicity/liveness findings in
[[officer_control_plane_post_implementation_audit]] block doing so out of band. Keep the
safe database default off until that audit's release gates pass.
The six §13 defaults are **decided** — no open question, no pending approval. Two of those
decisions changed the design: the ready floor scales with pool capacity rather than being a
constant, and there are **no per-ticket budget caps** (the officer is the brake; §3 records
the residual gap). See
[Implementation start-here](#implementation-start-here--the-as-built-substrate-2026-08-14)
for the substrate as actually built, with anchors — that section exists so a cold session
can begin B1 without re-deriving yesterday's landings.

**Design provenance: PROPOSED (2026-08-13).** Designed in session with the Legate;
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
`job_message_routes` for the `waiting_for_reply` claim interactions. **B1+ was
unblocked** after the six §13 defaults landed; the later O6 release began the live
acceptance with `auto_pull=false`.

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
- **Floors apply to backlog health, not dispatch.** The officer must keep at least as many
  *ready* tickets in a pool as that pool has slots (Legate, 2026-08-15: floor = pool
  capacity, so a 1-slot pool needs ≥1 and a 10-slot pool needs ≥10 — if every agent lands
  at once, each finds work waiting); he must never manufacture tickets to keep slots busy.
  *A floor breach wakes the officer (kanban order-point replenishment), and the digest
  reports the self-filed-ticket ratio per pool so gaming is visible within a day*
  **[R-org][X]**.
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
  post-row-locked 409. Gains an optional `category` key; slots sharing a category form a
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
| executor   | developer, designer, general-worker, writer      | **no** (singleton, §5.5) |

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
  - **Bounded by posture, not by a cap** (revised B1, 2026-08-15): §13.4 rejected
    per-ticket budget and wall-clock caps for *every* category, so the research round's
    "smaller default budget enforced through `budget_exceeded`" does not ship. The block
    carries the discipline in words instead: answer the question and STOP; document what
    was learned and what remains unknown; **extension is an officer decision (a follow-up
    ticket), never a worker decision.** That last clause is what makes a timebox mean
    anything without a timer — a worker that may extend its own spike has no bound at all.
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
  ChatDev's designer assets did **[R-fw]**. **No per-ticket budget or wall-clock cap**
  (Legate, 2026-08-15): the industry norm is per-item caps (Copilot's hard 59-minute
  session, Devin's per-session ACU limits **[R-fw]**), and this design deliberately
  departs from it — *the officer is the brake*. Noticing and correcting a runaway job is
  his job, and as of the supervision surface he can actually do it: `suspected_stuck`
  liveness on the sitrep, audit/log reads, and steer/cancel. The residual gap, stated
  plainly: a job burning tokens *fast but visibly active* trips no stall detector, so the
  only mechanical backstop is the century spend ceiling (§5.1) — which is optional and
  unset by default. A "this job has run > N hours" soft notification is a **separate,
  deferred feature**, deliberately outside the officer/loop concept: it belongs to job
  supervision generally, not to backlog pools.

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
   decommissioned, or an **optional** daily worker-spend ceiling is exhausted: one
   `usage_ledger.query_usage(scope_project_id=…)` call, the same query the officer card's
   `spend_today` makes; fail-open like the officer's own ceiling; exceeded → skip + digest
   line **[A1][X]**. **Unset by default and never required** (Legate, 2026-08-15) — no
   global default exists, because a MiniMax researcher pool and a sol executor pool differ
   by more than an order of magnitude in burn, so any global number is either useless or a
   footgun. Settable per century (`config_override.officer.worker_spend_ceiling_daily`)
   and per slot (an optional `spend_ceiling_daily` on the slot spec — "per worker" in the
   Legate's words; the slot already carries model and backend, so the cost knob belongs
   beside them). With no ceiling set and no per-ticket caps (§3), the century has no
   mechanical spend brake by design — the officer is the brake. A KB/pgvector outage →
   skip cleanly this tick; infra failures never feed breakers **[X]**.
2. **Compute free capacity** per pool. **[X]** The capacity and executor-serialization
   predicate must equal the claim predicate — **all non-terminal statuses** (`created`,
   `processing`, `waiting`, `paused`, `pending_review`) — not the funnel's
   `('created','processing')`: a paused executor must still occupy the singleton, or its
   redispatch races a second executor into the same story. Temporary under-use converges
   downward; two executors never converge.
3. **Fetch eligible tickets** for categories with free slots: `status='active' AND tags @>
   ARRAY['ready','category:<X>']`, keyset-paged by the total priority/age/note-id order,
   with no counts pass (the counts query belongs to ordinary backlog display, not each tick
   scan page) **[A2]**. The page size is transport only: scan until enough eligible rows
   exist for the decision or exhaustion is proven; source failures are explicitly
   unavailable. **Eligible additionally means unclaimed under one-shot semantics (§5.3).** Re-check
   `ready` by note_id immediately before stamping (vector-DB fetch and app-DB insert cannot
   share a transaction; the residual window is accepted — the guidance lane steers
   post-dispatch) **[X]**.
4. **Dispatch at most one job per pool per tick** through the **authoritative Officer Post
   admission + internal spawn path** **[A1]**. Manual REST and tick dispatch prepare
   payload/grants outside the transaction, then call the same final helper. It locks
   `project_officers` and the current thread, revalidates live incarnation/hold/auto-pull/
   roster/category/lineage, checks all-non-terminal capacity and ticket generation, and
   performs `create_job(conn=...)` before commit. The tick then mirrors the loop spawn:
   `db.create_job(created_by_thread_id=<officer thread>, user_id=<thread owner>,
   wake_on_complete=True)` as a born-paused strict-preflight job. The existing jobs row
   carries `provisioning_preflight` and an `officer_preflight` freeze while repository and
   cloud setup run. A lease/token CAS atomically changes it to `created` and clears the
   freeze only after every mandatory step succeeds; only then may `_trigger_dispatch`
   nudge the queue. Manual strict Officer creation uses the same boundary. This runs
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
   infra outages remain paused with `failure_class=infrastructure` and are excluded by the
   terminal-history query while retry/recovery remains visible **[X]**. An open breaker pauses **only its
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

- A ticket generation is **claimed** iff `officer_ticket_claims` contains its exact
  `(project_id, ticket_note_id, ready_generation_at)` identity. The claim is independent
  of job status and survives physical job deletion. Dispatch consumes readiness.
- **Re-arm is explicit**: the officer re-sets `ready` after reviewing the outcome; the
  tick's eligibility query compares the server-observed ready authorization against the
  newest consumed ledger generation. (Implementation: the ready re-arm writes a
  `ready_at` value in the note's frontmatter/index row, set by `kb_update`, so "re-ready"
  is one officer action.) Equal/older values remain consumed. Job DELETE is audit only and
  never re-arms; a deleted non-terminal predecessor continues to block a newer generation,
  so cancel or finish it first.
- This single change is also the **item-level dead-letter queue** **[R-fw]**: a failed
  ticket stays parked until the officer looks — SQS's DLQ pattern without new machinery —
  and it defuses the reindex-resurrection quirk (§4).
- **Claim+create is atomic** **[A1][X]**: both the officer's manual path and the tick perform
  durable generation validation + claim INSERT + lineage capacity count + exact job INSERT
  in **one transaction holding the stable durable-post lock** (via connection-aware
  `create_job`). Migration 0162 adds the durable generation/job uniqueness backstops. The
  fail-closed secondary backstop remains a partial unique index on
  `((context->>'ticket_note_id'))` over claim-bearing jobs, so a racing double-claim fails
  the second INSERT instead of double-working the ticket.
- **Rolling upgrade is fail-closed:** migration 0162 locks `jobs` across strict provenance
  backfill and jobs-trigger installation. A pre-lock writer is backfilled; an old ticket
  INSERT after commit is explicitly rejected because it has no matching claim. Old/direct
  DELETE remains supported and trigger-audits the observed terminal/non-terminal status.
  Raw public/internal/tool context cannot supply claim identity or Officer provenance.
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
  # worker_spend_ceiling_daily: 15.0   # OPTIONAL, unset by default (§5.1); a slot may
  #                                    # carry its own spend_ceiling_daily instead
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
  ready backlog holds **at least as many tickets as that pool has slots** — a 2-slot line
  needs 2 ready, a 10-slot pool needs 10. The floor scales with the troops you were given,
  so a fuller pool demands a deeper queue rather than a busier officer.
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

## Implementation start-here — the as-built substrate (2026-08-14)

Everything below was verified against the tree on 2026-08-15. File anchors are given as
symbol names, not line numbers (line numbers drift; grep the symbol).

**Migrations.** App migration **0162** now owns durable ticket claims (the next free app
number is **0163**; B3 took 0160 for `uq_jobs_active_ticket_claim` and 0161 is the runtime
actor credential boundary); next free **vector** migration is **0021** (B2 took 0020
for `knowledge_index.ready_at`). The original "v1 needs no migration" claim was wrong in
both directions: readiness needed a queryable timestamp rather than a tag, and the
double-claim backstop needed an index. Regenerate `schema_current.sql` and bump `APP_CURRENT_MIGRATION_HEAD`
(`tests/test_infrastructure_metering_migrations.py`) whenever a migration lands.

**The post row (B3's home for `auto_pull` + breaker state).** `project_officers` per
[[officer_post]] §3, with helpers in `orchestrator/database/postgres.py`:
`get_project_officer`, `get_or_create_project_officer` (self-heals a missing post),
`merge_project_officer_config` (deep merge, runtime keys stripped — the PATCH substrate,
so `auto_pull` and `worker_spend_ceiling_daily` land here), `merge_project_officer_state`,
`get_project_officer_lineage` / `get_officer_capacity_lineage` (**the O2 lineage the tick's
capacity count must use** — jobs from prior incarnations otherwise stop counting),
`append_project_officer_while_vacant` / `drain_project_officer_while_vacant` (the ring
pattern to copy for breaker/ramp state), `merge_project_officer_communication_policy`.
Config-vs-runtime split per §10 is already enforced by these helpers' shapes: kit/policy on
the row, live counters in the thread's `officer_state`.

**Dispatch admission was made authoritative by the 2026-08-15 post-safety checkpoint.**
`orchestrator/services/officer_admission.py` exposes preparation plus one connection-aware
final transaction used by both manual REST and tick dispatch. It locks the durable post
and current thread, revalidates the snapshot, counts **all non-terminal statuses** over the
complete incarnation lineage, validates the claim generation, stamps provenance and keeps
the transaction open through `create_job(conn=…)`. The dedicated tick enumeration query is
`project_officers JOIN threads`; watchdog/runtime enumeration remains separate.
The follow-up lifecycle checkpoint puts the no-force decommission gate under that same
post lock and counts the same all-non-terminal full lineage, so job insertion and
no-force retirement cannot both succeed. Commission continuity and job-completion routing
also make post-locked exact-incarnation decisions; a losing commission's config update is
vacancy/generation fenced. These changes were not part of the earlier O6 tranche; they were
subsequently deployed and passed a bounded disposable lifecycle/configuration gate on
2026-08-16. The later BP-05 ledger implementation is local and not deployed.
Its final review repair makes the jobs trigger ledger-first and null-safe, preserves the
source-less admission shape of genuine pre-0162 backfilled jobs during unrelated context
merges, forbids provenance removal from a live claimed job, and returns claim-retention
truth from the same transaction that deletes the job.
The internal spawn mirrors `_spawn_loop_job` (`db.create_job` + `provision_job_repo` +
`_trigger_dispatch`), with `_provision_officer_ticket_repo` /
`_enforce_officer_ticket_grants` in `main.py` as the injected adapters so the service
never imports main.

**Liveness (§5's stale-claim ages, free).** `orchestrator/services/job_liveness.py`:
`compute_job_liveness(...)` and the batch `compute_jobs_liveness(...)` — the batch form was
built project-scoped specifically for this feature. States: `active | waiting | paused |
suspected_stuck | unavailable | terminal` (a sixth, `terminal`, was added during E3),
each with `reasons[]` and `last_activity_at`. Thresholds: `JOB_LIVENESS_STALL_MINUTES`
(default 30), heartbeat freshness 180 s. **`jobs.updated_at` is display-only** — never use
it for claim age. Consumers already wired: sitrep, `/api/jobs/{id}/progress`,
`/api/stats/stuck`. §5.3's "claimed-but-stalled" line should be one more consumer, not a
second computation.

**Evidence (§3's close-checklists, free).** `orchestrator/services/job_evidence.py`:
manifest recorded at completion into `jobs.context.evidence_manifest` (server entries for
the completion report + deliverable-check, worker-declared entries resolved in the job's
Gitea repo, pinned to the completion head sha, measured, published under opaque `ev_*`
ids); `parse_manifest`, `public_manifest`, `read_evidence_entry`. Officer-facing tools:
`get_job_completion_report`, `list_job_evidence`, `read_job_evidence`. Kinds v1:
completion_report, deliverable_check, test_report, screenshot, change_summary. Bounds:
256 KiB paginated text, 5 images/job, 2000 diff lines, 8 MiB binary. **This is what an
executor close-checklist reads** — the officer never browses files (K3 ceiling).

**Claim interactions with waiting jobs (changed for the better).** `job_message_routes`
(0159) means a job in `waiting_for_reply` now has a *bounded* wait: the officer SLA
(default 15 min) escalates to the user and the total blocking timeout (24 h default)
resumes the job with an explicit no-answer system reply. §5.3's stale-claim worry is
therefore mechanically bounded for the message case; the remaining unbounded cases are
`paused` (orphan/human) and `pending_review`. Claim predicate stays all-non-terminal, and
`waiting_for_reply` is one of those states — a waiting executor still holds its singleton,
which is correct.

**Tool surface (if B adds any officer tool).** Descriptors live in
`src/shared/orch_surface/jobs/{control,inspection}.py` (43 today); each carries `group`,
`plane` (`job_control | job_observability | job_evidence | job_workspace`),
`caller_defaults`, `grant`, `phases`. Adding one means: descriptor + `orchestrator/mcp/
capabilities.py` contract + MCP schema revision bump + regenerate
`tests/fixtures/job_surface_caller_defaults.json` (`UPDATE_JOB_SURFACE_POLICY_SNAPSHOT=1`)
+ `scripts/generate-job-surface.py` (TS + `docs/generated/job_tool_catalogue.md`) + add the
name to `config/experts/centurion/config.yaml` (the drift pin
`tests/test_centurion_supervision_grant.py` fails otherwise) + regenerate
`tests/fixtures/config_tool_grants.json`. **Most of B needs no new tool** — the tick is
server-side and the officer curates through existing `kb_*` tools.

**Two ceilings B must respect.** (1) `registry.apply_officer_tool_ceiling` (K3) strips the
whole `job_workspace` plane and every object-plane category for `officer.enabled is True`
sessions, under any override — a backlog tool that needed file access would be stripped,
which is the intended answer, not a bug to work around. (2) The officer lane stamps
`X-MCP-Scope: project:<uuid>` and the plain session lane deliberately does not
(`make_bound_handler`) — server-side fencing is `_scope_permits_project`.

**B2's gaps are now closed (2026-08-15).** `kb_update` takes `remove_tags`/`set_tags`,
machine tags no longer feed `search_doc`, every write path folds case, workers cannot set
or clear `ready`/`parallel-safe`, `fetch_backlog` filters by tag containment and returns
`ready_at`, and `render_backlog_block` marks claimed tickets. **What B3 consumes:**
`fetch_backlog(vector_db, project_id, require_tags=[READY_TAG, category_tag(c)])` for
eligibility, `classify_ticket(row["tags"])` to resolve category/expert (skip on
`problems`), `row["ready_at"]` compared against the newest durable consumed generation
for one-shot semantics — **and a NULL `ready_at` is not dispatchable**, never "ready since
forever". Officer close instrument is still `kb_update(status='resolved'|'archived')`; the
re-ready action is `kb_update(add_tags=['ready'])`, which stamps a fresh `ready_at` even
when the tag was already present.

**Live-fire preconditions/status.** O6 was satisfied on 2026-08-15: the Resavio officer was
released successfully through `POST /api/projects/{id}/officer/release` with
`auto_pull=false`, and the run is recorded in
[[officer_backlog_pools_resavio_livefire]]. The KB hygiene pair remains tracked by that
in-progress run: retire the 0.92-confidence "No renderer available" RecallStore belief on
project `68137e29`, and run `docker/assert-browser-stack.sh` once on a live workspace.

## 11. Build delta (slices)

**Pre-B3 gates outside this document:** officer_post O2; officer knowledge K1–K3;
supervision E1–E3 plus E4 or the explicit recon-only disposition fallback; message routing
M2–M4 for any worker that can block on `send_message`. Do not duplicate those mechanisms in
the tick.

- **B1 — `work_categories.py` — DONE (2026-08-15).** Shipped as **two** pure modules, not
  one: `orchestrator/services/work_categories.py` (categories, membership map,
  `CATEGORY_DEFAULT_EXPERT`, `KNOWN_EXPERTS`, `allows_parallel`, `_CATEGORY_BLOCKS` +
  `close_checklist`, `classify_ticket`/`resolve_expert`, `role_to_category`) and
  **`src/shared/backlog_tags.py`** (the machine-tag namespace: `ready`, `parallel-safe`,
  `category:`, `expert:`; `normalize_tag`, `strip_officer_tags`, `strip_machine_tags`,
  `category_values`/`expert_values`). The split is forced by the import direction —
  `src/` never imports `orchestrator/`, and B2's worker-side stripping lives in
  `src/tools/knowledge/knowledge_tools.py`. Two copies of the officer-only tag list would
  be a privilege-escalation hole (a tag the tick honours and the stripper misses), so the
  namespace has exactly one home, reachable from both sides. `classify_ticket` returns
  `problems[]` rather than resolving ambiguity: two `category:` tags or a misspelled
  expert make a ticket non-dispatchable with a sitrep reason, never a coin flip — a typo
  caught here would otherwise surface at agent boot as a job failure and chain-trip the
  pool breaker. Tests: `tests/test_work_categories.py` (43), including doctrine pins on
  the load-bearing sentences and a brace-safety pin (the blocks sit beside
  `_ROLE_BLOCK_DEFAULT`, which goes through `.format()`).
- **B2 — ticket plumbing — DONE (2026-08-15).** `remove_tags`/`set_tags` on `kb_update`
  (both backends; Neo4j gets the add/remove **diff**, since Cypher has no "set" and
  `set_tags` must collapse to attach/detach); lowercase normalization of **every** tag at
  both write paths (`kb_write` + the reindexer join `kb_update`, which always folded —
  B1's "human tags keep their case" was wrong and a real regression against a pinned
  test: `tags @>` matches by exact string, so a preserved-case tag is an unfindable tag);
  machine-tag exclusion from `search_doc`; worker-side officer-tag stripping, *reported*
  in the tool result rather than silently dropped; `fetch_backlog(require_tags=…)` bound
  as the trailing parameter with `tags @>` containment, rows now carrying `tags` +
  `ready_at`; claimed-marker in `render_backlog_block(claims=…)`.
  - **`ready_at` needed vector migration 0020** (the doc's "v1 needs no migration" was
    about *app* migrations). Three-state on the agent write path — `ready=True` stamps,
    `False` clears, `None` leaves alone — because the re-ready action is a tag-only
    change that lands in `upsert_note`'s metadata-only branch, and because a content edit
    on a ready ticket must NOT bump the timestamp (that re-arms a claimed ticket and puts
    a second job on live work). The reindex path takes an absolute `ready_at` instead: a
    file replay is not an authorization event. Round-trips through OKF frontmatter so a
    vault rebuild does not park the whole queue, and fails **closed** (ready tag + NULL
    `ready_at` = not dispatchable) if it is ever missing.
  - Test updates: four positional-slot pins in `test_kb_convergence` /
    `test_knowledge_store` / `test_kb_index_chunking` were written as `[-1]`/`[-2]` and
    had already been chased once by `priority`; all four are now absolute, with the
    reason recorded. New suites: `tests/test_backlog_ticket_plumbing.py` (28) plus
    filter/claim coverage in `tests/test_project_backlog.py`.
- **B3 — claim funnel + tick — DONE (2026-08-15), admission boundary hardened by the
  post-safety checkpoint.** `orchestrator/services/officer_admission.py` (shared
  post-locked preparation/finalization for manual and tick paths), `orchestrator/services/
  officer_backlog.py` (the tick), app migration **0160** (`uq_jobs_active_ticket_claim`),
  `create_job(conn=…)`, `ticket=` on `POST /api/jobs`, `run_when_leader` mount, log line
  `officer=<id8> pool=<name> dispatched=<note>/<job8> | skip=<reason>`. Tests:
  `tests/test_officer_backlog_tick.py` (44).
  - **The in-flight predicate moved to all-non-terminal for BOTH paths, not just the
    tick.** The endpoint counted `('created','processing')`. Sharing one helper with two
    predicates would have let the officer hand-dispatch into a slot a paused job still
    owns — the same double-executor failure, reached through the direct path. A slot now
    means "occupied until the job is actually done"; the officer gets a truthful 409 and
    can resume or cancel the stalled job. Deliberate tightening, fail-safe direction.
  - **The claim is re-read under the lock**, so a racing replica is a quiet skip rather
    than a unique-index stack trace. Only the app-side half can be closed this way —
    `ready_at` lives in the vector DB and cannot join the transaction, which is the
    residual window §5 already accepts.
  - **Three bugs a mocked test could not have caught**, found by reading the schema:
    `runner_kind="officer"` violates `jobs_runner_kind_check` (accepts user | lifecycle |
    service) — and `lifecycle` is not merely legal but *correct*, being the class whose
    grants raise the autonomy ceiling to full; `autonomy: "full"` belongs in
    `config_override`, not `context`, because that is what both grant PEPs read; and
    `list_officer_threads` did not select `user_id`, so every tick job would have been
    created **ownerless** and failed at dispatch with no grants to resolve. The tick's
    grant check is therefore `_enforce_officer_ticket_grants` (runner_kind=lifecycle,
    raising `GrantDenied`) rather than `_enforce_job_create_grants` (runner_kind=user,
    raising HTTPException — which would both deny the exemption and throw a 422 inside a
    background loop).
  - **Migration 0160 carries exactly one statement.** A multi-statement `.notx.sql` is
    sent as a simple query, Postgres wraps that in an implicit transaction, and
    `CONCURRENTLY` refuses to run there — the `COMMENT ON INDEX` moved into the file
    header instead of costing a second migration. It also scopes to
    `(project_id, ticket)`: note ids are slugs unique only within a project, so a global
    index would let one project's claim block another's.
  - `_SPEC_KEYS` gains `category` and `spend_ceiling_daily` here rather than in B4 —
    without the first, a categorized roster cannot be provisioned and the tick is dead
    code; without the second, §13.3's per-slot ceiling would be a config key that does
    nothing. `usage_ledger.query_usage` gained a `ref_ids` set form so the per-slot
    ceiling can actually be costed (the job set is in the app DB, the events are in the
    audit DB, so no join is available).
  - Stale-claim detection reads `updated_at` deliberately and only here: this is the
    control-plane question "has anything touched this row", not the liveness verdict —
    the sitrep still gets its reading from `compute_jobs_liveness`.
  - **BP-06 semantic completeness (2026-08-16):** `BacklogCursor` and
    `_scan_eligible_tickets` page the cross-store eligibility path until sufficient or
    exhausted; exact ready depth scans to exhaustion and omits unavailable pools.
    Migration `0021_kb_backlog_keyset_index.notx.sql` completes the
    `(project_id, priority, created_at, note_id)` order. Breaker outcomes and stale/oldest
    claims use dedicated app-database queries whose semantic predicates precede limits.
    The executor predicate likewise moves into SQL before `LIMIT 1`, and slot spend has no
    arbitrary newest-job ceiling. BP-12's polling optimization remains separate.
  - **Not built:** the stale-claim list and breaker state are written to `officer_state`
    for B4/B6 to render; nothing surfaces them yet.
  - **k3d verification (2026-08-15), and what it caught.** Migration 0160 applied and the
    index proved out against real rows (double non-terminal claim refused; the same slug
    in another project allowed; a terminal prior claim not blocking a re-arm). A
    synthesized officer post — a thread row with `auto_pull` and a `researcher` pool, no
    live agent, since the tick never talks to the officer's agent — then drove the whole
    path end to end. Every stamp landed correctly on the created job
    (`runner_kind=lifecycle`, owner set, `config_override.autonomy=full`, ticket claim,
    category, slot, researcher contract in the kickoff), confirming the three
    schema-caught fixes in the live system. **One design bug only the live run could
    find:** the provisioning adapter passed `loop_floor=True` unconditionally, copied
    from the loop — that flag raises unless the project's cloud baseline is provisioned,
    so the first dispatch sealed itself with "project loop requires a provisioned cloud
    folder". Executors deliver under `projects/<slug>/` and genuinely need the baseline;
    a researcher's deliverable is a KB note and a tester's is issue tickets, so requiring
    it for them would make research and critique undispatchable on any project without a
    cloud folder — the infrastructure-shaped version of exactly the bias this feature
    exists to remove. `loop_floor` is now executor-only. Re-arming the ticket afterwards
    also confirmed one-shot claims live: while the failed job held the claim the tick
    refused to re-dispatch ("claimed at …"), and a fresh `ready_at` released it.
- **B4 — slots + surfacing — DONE (2026-08-15).** Precedence law in
  `_officer_slot_category` / `_compose_category_kickoff` (`main.py`): the slot's category
  supplies the contract, a `work_category` argument naming a different one is **named in
  the kickoff and logged, never refused** — warn-not-forbid, with the one forbidden
  outcome being a silent contradiction between the contract the worker reads and the slot
  it occupies. `capacity_lines(ready_by_pool=…, oldest_claim_age_hours=…)` renders
  `researchers 1/2 (ready 4), testers 0/1 (ready 0, BELOW FLOOR) … Oldest open claim 27h`;
  `pool_status_lines()` renders the policies the tick enforces (open breaker + cause +
  tickets, claimed-but-stalled with "NOT released automatically", and an explicit
  "Auto-pull: OFF" so idleness is never a mystery). `ready_depth_by_pool()` deliberately
  reuses the tick's own keyset `fetch_backlog → ticket_claim_states → eligible_tickets`
  path to exhaustion rather than counting `ready` tags: a depth the tick reads as zero
  would have the officer waiting for dispatches that never come. A KB or claim-database
  outage omits the number instead of reporting a zero nobody measured. Tests:
  `tests/test_officer_pool_surfacing.py`.
  - **Predicate drift caught here.** `_capacity_section` carried its own inlined
    `IN ('created','processing')` count. B3 widened admission to all-non-terminal, so
    that copy would have shown the officer a free slot the funnel then refused with a
    409. It now goes through the shared `count_in_flight_by_slot`, which is the whole
    reason that helper exists.
  - Legacy rosters and the flat-cap path render byte-identically — pinned.
  - The `ready_by_category` naming in the original sketch became **`ready_by_pool`**:
    two pools can share a category (a MiniMax and a frontier executor line), and the
    officer steers slots, not categories.
- **B5 — prompts & doctrine — DONE (2026-08-15).** `build_loop_kickoff` now composes
  `category_block(role) + slimmed identity block`, so **every loop gets the doctrine,
  officer or not** — this is the slice that actually reaches the Better Resavio failure
  mode. `_ROLE_BLOCKS` slimmed of what the contract now says (scholar's isolated-repo
  paragraph, product-qa's 3-7/severity/absence-evidence/no-blocking-issues list,
  developer's validate-your-own-work). All pinned strings in `test_project_loops`,
  `test_kb_convergence` and `test_project_backlog` survive unchanged. The two incentive
  edits shipped with new pins: "ONE solid, **verifiable** increment" → "ONE solid
  increment with **EVIDENCE APPROPRIATE TO THE WORK** … Do not pick the work whose
  evidence is easiest to produce", and the critic rubric's "evidence quality" → "evidence
  **APPROPRIATE TO THE CLAIM** … A ticket is not weaker because its evidence would be a
  screenshot rather than a test". §8's charter posture ships as a `<backlog_doctrine>`
  block in `config/experts/centurion/persona.txt`.
  - **The critic is exempt from contract composition, deliberately.**
    `role_to_category("critic")` is `tester` — right for the expert, wrong here: the
    loop's critic SELECTS, and prepending the tester contract would tell it to file 3-7
    issue tickets, contradicting its duty on the same screen. Categories describe work;
    selection is orchestration. `_ROLE_CONTRACT_EXEMPT` names it.
  - **The blocks became context-free.** They said "This *ticket* is a SPIKE" and "deliver
    THIS ticket" — true for an officer dispatch, a small lie on every loop kickoff, which
    has no ticket. Caught by rendering the composed output rather than by a test.
  - Doctrine is pinned **against the machinery**, not as string presence: the floor
    wording must match "the pool's slot count", the breaker window must match
    `BREAKER_OPEN_MINUTES`, and the claim rules must match one-shot semantics
    (`tests/test_officer_pool_surfacing.py::TestBacklogDoctrineMatchesTheMachinery`).
    Doctrine that disagrees with the code is worse than no doctrine.
- **B6 — cockpit — DONE (2026-08-15).** **A category can now be provisioned from the UI**,
  which is what made the whole feature reachable: `OfficerSlotSpec`/`SlotDraft`/
  `buildSlotsSpec`/`draftFromPost`/`STARTER_SLOT_DRAFT`/`addSlot` gain `category`, and the
  kit editor gains a per-row **Pool** select. `get_project_officer_summary` gains
  `kit[].ready_depth` / `kit[].below_floor` and a `backlog` block (auto_pull, breakers,
  stale_claims, spend ceiling); `kitChips` renders category, ready depth, `BELOW FLOOR`
  and `BREAKER OPEN` with a warning-styled chip. Absent `ready_depth` renders as nothing,
  never `ready 0`. Tests: 62 in `project-officer.component.spec.ts` (12 new), full cockpit
  suite 2164 green.
  - **A THIRD copy of the in-flight query lived here** with the stale
    `IN ('created','processing')` predicate — the officer card would have shown the Legate
    a free slot the funnel refuses. Now through `count_in_flight_by_slot`. That is three
    consumers found (sitrep, card, endpoint); the shared helper earned itself.
  - **`tsc --noEmit -p tsconfig.json` does not typecheck app sources** — it is
    solution-style. It passed while `addSlot()` was constructing a `SlotDraft` without
    `category`, and 2164 vitest tests passed too; only the Angular compiler in the running
    pod caught it (`TS2741`, CrashLooping cockpit). **Use `tsconfig.app.json`.**
  - Verified in a browser on k3d, which caught what specs could not: the per-row hint
    repeated verbatim for every slot, so the explanation moved above the rows once.
  - **Do NOT run prettier on this repo.** It is configured in `package.json` but not in
    CI, and the committed sources are not prettier-clean — `--write` reformatted three
    whole files (753 lines) around a ~150-line change. Reverted and re-applied by hand.
  - Not built: the digest lines (dispatches/day, spend/day, re-ready counts, self-filed
    ratio) — they need per-day aggregates nothing records yet, and the self-filed ratio in
    particular is the Goodhart guard §8 promises, so it deserves its own slice rather than
    a guess at the query.
- **B7 — `writer` expert — DONE (2026-08-15, subagent-built).** `config/experts/writer/`
  on the `general-worker` precedent (`$extends: worker_base` + persona.txt, no `tools:`
  override, no strategic/tactical); `writer` added to `CATEGORY_EXPERTS[EXECUTOR]`;
  app-guide roster row; `config_tool_grants.json` regenerated (grants byte-identical to
  `general-worker`, 64 tools); `MANAGED_SEEDS` untouched.
  - **Executor-ONLY, unlike every other multi-category expert.** A writer is handed the
    findings — it does not go and get them. Making it a researcher too would produce
    exactly the confusion the category/expert split exists to prevent: a research
    deliverable that happens to read well.
  - The persona's load-bearing rule is **do not fill a gap with plausible prose**. Fluent
    text hides missing knowledge better than code does — nobody can see the difference
    from the page — so an honest "unknown, and here is what would settle it" is the
    required output where a developer would get a failing test instead.
  - Deliberate absences, both inherited rather than restated: **no shell** (nothing here
    is built or run) and **no delegation** (a document has one voice; a fanned-out draft
    reads like four people wrote it). Verified against the regenerated grants.
  - §13.6 said the writer ships *after* the first acceptance week, on the grounds that
    week one proves categories and the tick rather than roster width. Building it early
    does not change that: it stays out of the acceptance kit unless the Legate puts it in.

## 12. Acceptance — Resavio century (dev)

**In progress:** O6 release itself succeeded with `auto_pull=false` on the deployed earlier
tranche. This section remains the wider acceptance contract; see the committed live log in
[[officer_backlog_pools_resavio_livefire]]. The subsequent Officer Post transaction and
commission-configuration checkpoint was deployed and passed a separate disposable gate on
2026-08-16. BP-05's durable ledger is committed in HEAD but not claimed deployed; BP-06's
semantic-pagination checkpoint is the newer local, uncommitted, not-deployed change.

Pre-requisites: officer_post O1–O6 done (incl. O2 lineage capacity), knowledge-plane K1–K3,
supervision E1–E3 and the chosen disposition-evidence path, message-routing M2–M4, and the
Resavio officer released; KB hygiene — retire the 0.92-confidence "No renderer available" RecallStore
belief (the render stack was fixed 2026-07-17; the project's memory still says agents are
blind) and run `assert-browser-stack` once on a live workspace.

In order:

1. PATCH the kit to the §6 roster + `auto_pull=true`; charter carries the Demo Definition.
2. Officer triages the pool: categories + four-field briefs + `ready` on at least
   slot-count tickets per pool (2 for the 2-slot researcher pool, 1 each for testers and
   executors in the §6 kit). Card shows per-pool utilization, ready depth against the
   capacity floor, and policies.
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

## 13. Decisions — all six settled (Legate, 2026-08-15)

Nothing here is open. **B1 is cleared to build.**

1. **`auto_pull` ships off**, flipped per century. The first century to get it is Resavio
   during acceptance, under supervision — a century whose officer has not triaged a
   backlog yet must not start pulling whatever happens to be tagged.
2. **The ready floor is the pool's slot count, not a constant** — 1-slot pool ⇒ ≥1 ready,
   10-slot pool ⇒ ≥10. The Legate's reasoning is the right one: if every agent in a pool
   lands at once, each must find a ticket waiting. The floor therefore scales with the kit
   and needs no separate tuning knob. Floor-breach wakes use a durable episode plus the
   existing session-wake outbox: attempted, durably queued, delivered, and failed are
   separate outcomes. The per-pool six-hour policy debounce starts only at durable queue
   success; transient retry backoff is independent, and project/incarnation/pool/episode
   deduplication makes duplicate tick replicas converge on one outbox event. Note for B6:
   because a bigger pool now demands a deeper queue, the
   self-filed-ticket ratio in the digest matters *more*, not less — the Goodhart pressure
   scales with the floor.
3. **No spend ceiling by default, never required.** Optional per century, and optionally
   per slot ("per worker"). Unset means no mechanical spend brake — accepted deliberately
   (§5.1).
4. **No per-ticket budget or wall-clock caps at all** — a deliberate departure from the
   industry norm, on the grounds that *having an officer is the reason the caps are not
   needed*: managing workers, including runaway ones, is his job (§3 states the residual
   gap honestly). A "job has run > N hours" soft notification was considered and ruled
   **out of scope for this feature** — it is general job supervision, filed separately if
   ever wanted.
5. **Stale-claim threshold 4 h** (renders "claimed-but-stalled" on card + sitrep); a
   `pending_review` claim **pages after 24 h** — that lane has a known dead zone, and a
   silently stranded review is exactly the invisibility this feature exists to end.
6. **`writer` expert ships later**, after the first acceptance week. B7 stays in the doc
   as the recipe; the first week proves categories and the tick, not roster width.

Settled earlier by the research round: breaker semantics (job-failures-only, per-pool —
§5.6); category storage (tags + GIN `@>`, no column — §4); executor parallelism (singleton
+ named-write-surfaces `parallel-safe` — §5.5).

## 14. Decision log

- **2026-08-16 (BP-06)** — Fixed candidate windows are no longer correctness boundaries.
  Cross-store eligibility uses stable keyset pages with explicit exhausted/lower-bound/
  unavailable states; ready depth is exact, while breaker distinct outcomes and stale/oldest claims
  are database-native predicates. Real PostgreSQL/pgvector plans were measured at 10k
  rows. `auto_pull` remains false and no live-fire gate is claimed.

- **2026-08-15 (Legate)** — all six §13 defaults settled; see §13. Two shaped the design:
  the ready floor became **capacity-scaled** (pool slot count, so every simultaneously
  free agent finds a ticket) rather than a flat 2, and **per-ticket budget/wall-clock caps
  were rejected outright** — the officer's whole purpose is to notice and correct runaway
  work, so hard caps would be redundant scaffolding; the long-running-job notification
  that would soften that was explicitly scoped out as a separate feature. Spend ceilings
  are optional at century and slot level, unset by default, which leaves a century with no
  mechanical spend brake by design.

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
