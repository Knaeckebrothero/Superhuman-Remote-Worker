# Loop campaign scheduling — the Critic as planner

**Status**: P0 (dark machinery) + P1 (agent surface + notifications) BUILT
2026-07-09, uncommitted — migration 0050, plan intake endpoint, planner
advance path, router opt-in, `loop_plan` tool with checkpoint-only injection,
planner/member kickoff blocks, both notification kinds; 68 unit tests in
`tests/test_loop_campaign_scheduling.py` (rotation suites unmodified). See "As-built notes" below for deviations —
the biggest: **no `pending_stages` column and no sweeper changes** (recovery
rides member-job context stamps + a `plan_job_id` idempotency guard), and
`loop_user_question` detection happens at advance time (kb_write is an
agent-side dual-write; there is no orchestrator KB-write path to hook).
Design agreed 2026-07-09 — all open questions resolved with the owner same
day (see "Resolved questions" at the bottom). Written from the Better Resavio
loop audit (project `68137e29`, main cluster). Companion to
`docs/features/loop_parallel_stages.md` (shipped) and the not-yet-written
seasons/annealing prompt work (see "Interaction with seasons" below).
**k3d smoke GREEN 2026-07-09** (gemma planner loop `942ef046`, 2-stage
template, 6 jobs, ~5h wall clock): checkpoint tool injection + PLANNER
DUTIES kickoff verified on real jobs; plan intake accepted/rejected
correctly (incl. disposition-required, oversize, missing-note, unknown-job);
plan application → stamped members → stage succession (once via the
sweeper's backstop path); review flip; DISPOSITION DUTY kickoff on the next
checkpoint; ship disposition → history entry + new campaign; both
notification kinds fired in one advance and the bell row persisted
(`loop-942ef0`). Two real bugs found and fixed by the smoke (notes 6+7
below). Cleaned up: loop stopped, jobs/project deleted, pods reaped.
**P2 (cockpit) BUILT + k3d-verified 2026-07-10**, uncommitted — campaign
card + history + scheduling opt-in + live SSE handling; see "P2 as-built
notes". **P0–P2 pushed to develop + deployed 2026-07-10.** **P3 first live
flip (Better Resavio, planner loop `8e832884`) found a release blocker**: the
agent-side loader silently dropped the injected `tools.loop` category, so
checkpoint critics carried PLANNER DUTIES but never owned the `loop_plan`
tool — every cycle degraded to single-job rotation. Root cause + fix (+ the
collateral `communication`/`send_message` discovery) in "P3 first-flip
findings" below. Fix BUILT + tested 2026-07-10, uncommitted. Remaining:
push the fix, then re-observe a full campaign cycle live.

## Motivation

The loop's selection step is **horizon-1 greedy**. Every cycle, the Critic
picks the next increment by comparing candidates on what can reach a provable
state *within a single Developer job*. A mediocre feature that lands in one job
beats a high-value feature that needs five jobs to show anything, every time —
the five-job feature's first increment is unprovable scaffold, and unprovable
scaffold scores zero under an evidence rubric (the live rubric literally
applies a ×0.85 risk *discount*). This is the structural reason the Better
Resavio loop produced 66 backend modules and zero UI in ~35 iterations: nobody
ever declined to build a UI; a UI simply cannot win a horizon-1 auction.

The loop has already invented the missing concept on its own, without framework
support — which is the strongest signal the design is right:

- The iter-23 Critic verdict labels its selection "**iter-24+ Developer
  PRIMARY**" — a multi-job commitment the framework has no way to honor. The
  next Critic may or may not respect it.
- The same verdict pre-registers **5 verification commands** "to be run by the
  next Critic to verify SHIPPED state" — a campaign acceptance check, invented
  ad hoc.
- Multi-job chaining already works through repo + KB (iter-16 shipped Tier-1
  receptionist wiring; iter-24 extends it to Tier-2) — but only when the
  auction happens to re-select the same thread, which is luck, not policy.

Two adjacent pathologies this design also addresses:

- **Free-form verdicts breed ceremony.** The Critic's output is prose, so its
  effort leaks into pseudo-quantitative rubric theater (invented multipliers,
  self-accreting "pinned memory" process rules, fictional gates like
  "Lawyer-budget not allocated"). A schema-constrained plan gives that energy
  exactly one place to go.
- **Analysis starvation.** QA input reaches the Critic only when the rotation
  happens to schedule it and the job survives (at audit time the freshest
  qa-findings were from iter-14, nine iterations stale). The fix here is to
  make the analysis stage a framework *guarantee* rather than a Critic choice.

## Design overview

Today (rotation mode) the loop walks `role_sequence` modulo its length
(`orchestrator/main.py:10651`):

```
[scholar ∥ product-qa] → critic → developer → [scholar ∥ product-qa] → …
```

In **planner mode**, the template keeps providing the *analysis cadence and the
Critic checkpoint*, but the execution slot becomes elastic: the Critic's job
emits a structured **plan**, and the loop expands the slot into the plan's
stage queue before rotating on.

```
                    ┌────────────────────────────────────────────┐
                    ▼                                            │
[scholar ∥ product-qa] → critic ══► developer × K (campaign) ────┘
                          │
                          └── no plan filed → developer × 1 (today's behavior)
```

Rules of the grammar:

- `role_sequence` is unchanged and still validated by
  `services/project_loops.py:validate_role_sequence`. The entry after `critic`
  is the **execution slot**; a plan expands it, no plan runs it as-is (K=1).
  A loop without plans is byte-identical to today — same invariant the
  parallel-stages work held for single-role loops.
- Campaign stages are **single-role, one job at a time**. Any registered
  expert can staff a stage (developer, bughunter, curator, even a critic —
  see "Roster and the sub-critic rule" below) — the Critic's "different agents
  such as testers" verb is roster choice, not new machinery. Parallel
  *execution* stays forbidden (repo single-writer); the analysis fan-out stage
  stays exactly as shipped in 0048.
- The mandatory analysis stage between campaigns is **not plannable**. The
  planner allocates execution; the framework guarantees oversight cadence. A
  planner that could skip its own review is how twelve uninterrupted iterations
  of E-Bike charging happen.

### What the Critic can now say

At campaign end, control returns to a Critic job whose kickoff includes the
previous plan and its acceptance evidence. Its disposition verbs:

- **ship** — evidence checks pass; close the campaign, plan the next one.
- **extend** — promising but unfinished; grant more stages (bounded, see
  guardrails).
- **kill** — dead end; record why in the KB (feeds the tried-and-rejected
  guard already in the kickoff), plan something else.

## The plan object

Filed by the Critic during its job via a new **`loop_plan` tool**
(strategic-phase, critic-role only), validated at call time so the model gets
immediate schema feedback, and re-validated server-side (never trust agent
input):

```jsonc
{
  "initiative": {
    "kb_note_id": "iter-23-critic-verdict-choose-f5-…",   // must exist in project KB
    "title": "F5 Tier-2 receptionist subcommands"
  },
  "stages": [                       // the campaign queue, len 1..K_MAX
    {"role": "developer"},
    {"role": "developer"},
    {"role": "bughunter"}
  ],
  "acceptance": [                   // evidence the closing Critic must check —
    "grep -nE 'rechnung|dsgvo|predicate' repo/src/kurort_engine/__main__.py",
    "cd repo && PYTHONPATH=src ./.venv/bin/pytest tests/ -x"
  ],                                // formalizes the emergent 5-VC pattern
  "disposition": {                  // verdict on the PREVIOUS campaign, if any
    "campaign_id": "…",
    "outcome": "ship" | "extend" | "kill",
    "evidence_checked": true,
    "notes": "…"
  }
}
```

Validation (tool layer + orchestrator, both):

- `stages` length 1..`LOOP_CAMPAIGN_MAX_STAGES` (default **5**).
- Every `role` resolves against the expert registry (critic included — the
  sub-critic rule below governs what a scheduled critic can do, not whether it
  can exist).
- `initiative.kb_note_id` exists in the loop's project KB.
- Plan cost fits the budget with reserve:
  `len(stages) <= remaining_iterations − 2` (room for the closing analysis +
  critic stages). The kickoff already tells the Critic its remaining budget;
  the validator makes overspending impossible rather than merely discouraged.
- `extend` outcome allowed only while `extensions_used < LOOP_CAMPAIGN_MAX_EXTENSIONS`
  (default **2**), and an extension may add at most `K_MAX` further stages.

All three caps (`K_MAX`, extensions, abort threshold) are **per-loop
overridable in the start request**, clamped by config hard ceilings
(`K_MAX ≤ 10`) — they're experiment parameters we expect to tune per loop
during P3, and `model`/`max_iterations`/`role_sequence` already follow this
per-loop pattern. The config ceiling keeps the runaway floor non-negotiable.

### Roster and the sub-critic rule

A plan may schedule **any registered expert**, including another critic — a
mid-campaign critic stage ("build, build, review, build") is legitimate. What
distinguishes the loop's own **checkpoint critic** (the `role_sequence` slot)
from a **scheduled sub-critic** (a campaign stage that happens to be
critic-flavored) is a single capability: **only the checkpoint critic can file
plans.** Enforced with the same defense-in-depth pattern as phase-restricted
tools (schema binding primary, runtime gate backup):

- **Spawn-time**: the `loop_plan` tool is injected only into checkpoint-critic
  jobs; campaign members — whatever their role — never get it bound.
- **Intake-time**: `POST /api/jobs/{id}/loop-plan` accepts only when the
  filing job is the loop's `current_job_id` **and** the loop's current
  `seq_index` is the template's critic slot. A campaign member occupies the
  execution slot, so it's rejected structurally, not by role-string check.

Sub-critics also (like all loop jobs) have no loop-creation surface, so
there's no recursion path — a scheduled critic is just another analysis stage
that writes KB notes for the next checkpoint to weigh.

Transport alternatives considered and rejected: parsing a tagged KB note
(prose parsing, no feedback loop, exactly the fragility the audit flagged in
KB-archaeology handoffs) and `freeze_data` at completion (no mid-job
validation — the Critic only learns its plan was malformed after the job is
over, when nobody can fix it).

## Mechanics

### Schema (one migration, `00xx_loop_campaign_scheduling.sql`)

Two columns on `project_loops`, mirroring the 0048 style (JSONB, CHECK on
type, comment pointing here):

- `pending_stages JSONB NOT NULL DEFAULT '[]'` — the not-yet-spawned remainder
  of the accepted plan's queue. Popped front-first by the advance; empty in
  rotation mode and between campaigns.
- `campaign JSONB` (nullable) — the active campaign's control state:
  `{plan_job_id, initiative_note_id, title, stages_total, stages_done,
  extensions_used, acceptance: […], member_failures}`. Written when a plan is
  accepted, archived into the loop's history (or a retro) at disposition,
  then cleared.

Plus one mode flag: `scheduling TEXT NOT NULL DEFAULT 'rotation'
CHECK (scheduling IN ('rotation','planner'))`. Explicit opt-in per loop; no
inference from plan presence.

### Plan intake

`POST /api/jobs/{job_id}/loop-plan` (agent-authenticated, same channel as the
other job-scoped agent calls). Accepts only if the job is the loop's current
critic-stage job and the loop is `scheduling='planner'`. Stores the validated
plan into `jobs.context.loop_plan` via the atomic `merge_job_context` path
(never direct context assignment). Idempotent: re-filing replaces the plan;
last write before completion wins.

### Advance path (the scarred part — minimal deltas)

All changes live in `_rotate_loop_to_next_stage` (`orchestrator/main.py:10629`)
and are gated on `scheduling='planner'`:

1. **After a critic stage completes**: read `context.loop_plan` from the
   completed job. Present and valid → apply the disposition to `campaign`,
   write the new `campaign` + `pending_stages`, and spawn `pending_stages[0]`.
   Absent → spawn the execution slot from `role_sequence` as today (implicit
   K=1 campaign, no `campaign` row written).
2. **After a campaign-stage job completes**: if `pending_stages` is non-empty,
   pop the front and spawn it (incrementing `campaign.stages_done`); if empty,
   fall through to normal rotation — which lands on the analysis stage, then
   the critic.
3. **Queue pop atomicity**: the pop, the `campaign` update, and the pointer
   write (`current_job_id`) happen in the same `update_project_loop` UPDATE
   that `_writeback_loop_stage` already issues — one row, one write, no new
   torn states. The torn-advance signature stays exactly
   `current_job_id IS NULL ∧ current_stage_jobs = '[]'`; the sweeper's heal
   path learns one new branch: if `pending_stages` is non-empty at heal time,
   respawn from the queue front instead of deriving from `role_sequence`.
   `_derive_loop_counters` continues to read the stamped
   `loop_seq_index`/`loop_remaining` — campaign stages stamp the *slot's*
   seq_index (the queue is an expansion of one slot, not new slots), so
   modulo-based legacy fallback stays coherent.

### Failure semantics

- A failed campaign-stage job increments `campaign.member_failures` alongside
  the loop's existing `consecutive_failures`. At
  `LOOP_CAMPAIGN_ABORT_FAILURES` (default **2**) consecutive campaign-member
  failures, the campaign **aborts early**: flush `pending_stages`, mark the
  campaign `aborted` in its control state, and rotate directly to the analysis
  stage → critic, whose kickoff says so. A wedged initiative can't burn its
  full allocation; the loop's global failure stop-axis
  (`max_consecutive_failures`, `_loop_stop_reason`) is unchanged and still
  supersedes.
- Infra-failed members (0-audit-entry jobs) count toward abort but produce
  retros as today, so the closing Critic can tell "initiative is bad" from
  "cluster was bad" — the audit showed it already reads failure retros
  correctly.

### Budget accounting

Unchanged: every spawned stage decrements `remaining_iterations` by one, so a
K=5 plan visibly costs 5 iterations. The Critic kickoff already carries the
budget line; the planner role block adds the arithmetic explicitly ("a 5-stage
campaign is a third of your remaining budget — spend accordingly"). The
validator's budget-reserve rule (above) makes the hard floor unconditional.

### Kickoff changes (`build_loop_kickoff`)

- **Critic (planner loops)**: planning instructions + the previous campaign's
  plan, acceptance list, and stage retros; the three disposition verbs; the
  budget arithmetic; and one line the audit earned: *"Constraints must be
  real: the only human stakeholder is the operator. If something needs a
  human (legal review, budget, third-party access), file a `user-question`
  KB note and proceed on the best assumption — never park work on a fictional
  trigger."*
- **Campaign members**: a campaign block — *"You are stage 2 of 4 of campaign
  'F5 Tier-2' (initiative: <kb note id>). Previous stage retro: <path>. The
  campaign closes against this acceptance evidence: <list>. You do NOT have to
  reach a provable state this job — you DO have to leave an honest retro of
  actual state."* That last sentence is the verification-repricing this whole
  audit argued for: truthfulness stays absolute, per-job provability becomes
  campaign-scoped.

### Notifications

The cockpit's notification pipeline (`NotificationService` + SSE +
notification-bell, already carrying session events and admin
`user_registered` frames) gains two loop event kinds — no new cockpit
surface, the bell renders `AppNotification`s generically:

- **`loop_campaign_disposition`** — emitted by the advance path when a
  disposition is applied (ship / extend / kill / abort), payload: loop id,
  campaign title, outcome, stages used. Also appended to the loop's timeline
  `actions` strings as today. This is the "what did the loop do overnight"
  signal without KB archaeology.
- **`loop_user_question`** — emitted when a loop job files a KB note tagged
  `user-question` (detected in the orchestrator's KB-write path for loop
  jobs). The tag convention is fixed here; the richer answer-back flow
  (dedicated inbox view, routing the operator's answer into the loop's KB /
  steering) is a **later, separate feature** that folds into the notification
  center — deferred deliberately so it can't block P0.

No email, no push in v1 for either kind.

## Guardrails (summary)

| Guardrail | Value | Enforced by |
|---|---|---|
| Max stages per plan | 5 (`LOOP_CAMPAIGN_MAX_STAGES`) | tool + orchestrator validation |
| Max extensions per campaign | 2 | orchestrator (`extensions_used`) |
| Budget reserve | `len(stages) ≤ remaining − 2` | validation |
| Campaign abort | 2 consecutive member failures | advance path |
| Analysis cadence | analysis stage between campaigns, not plannable | grammar (template-provided) |
| Parallel execution | forbidden (unchanged) | `validate_role_sequence` |
| Roles | any registered expert | validation |
| Plan filing | checkpoint critic only (sub-critics excluded) | tool injection + intake check |
| Cap overrides | per-loop in start request, config hard ceiling | validation |
| No plan filed | execution slot runs as today (K=1) | advance fallback |

## Interaction with seasons (separate, prompt-layer feature)

The sprint/annealing idea (phase-dependent policy: explore → consolidate →
refine, cyclic) is deliberately **not** part of this build — it's pure
`build_loop_kickoff` + role-block wording keyed on the progress ratio the
builder already computes. The two compose without coupling: seasons set the
*policy* ("early season: prefer long exploratory campaigns; late season:
short hardening ones — and spike-mode members may skip spec-lock but never
honesty"), campaigns are the *allocation mechanism* the policy speaks through.
Campaign-scoped labels ("season 2, campaign 3, stage 2/4") also give iteration
artifacts a collision-free namespace — the July-5 counter-reset mess (two
"iter-23 Critic" verdicts in one KB, one correcting the other) becomes
structurally impossible. If seasons ship first, nothing here changes; if this
ships first, seasons drop in as prompt edits.

## What this deliberately does NOT build

- **Scholar ∥ developer overlap.** Technically safe (scholar barely writes the
  repo; execution stays singleton) but the loop is budget-bound, not
  clock-bound, and an overlapped scholar audits the *pre-campaign* product.
  Sequencing analysis after the campaign gives the Critic fresher input at the
  same iteration cost. Revisit only if wall-clock ever matters.
- **Parallel developers.** Repo single-writer stands.
- **Critic-skippable analysis.** See guardrails.
- **Mid-campaign replanning.** The Critic plans only at its own stages. If a
  campaign is going sideways, the abort rule gets control back; members don't
  get to renegotiate scope.

## Rollout phases

- **P0 — machinery** (no behavior change anywhere): migration; plan-intake
  endpoint + validation; queue consumption + campaign state + abort in the
  advance path; sweeper heal branch. Tests at the parallel-stages bar:
  rotation-mode loops byte-identical (regression suite), planner-mode
  spawn/pop/abort/extend/disposition unit tests, torn-advance heal with
  non-empty queue, budget-reserve rejection, real-Postgres barrier drill
  reused. Ship dark behind `scheduling='rotation'` default.
- **P1 — agent surface + notifications**: `loop_plan` tool (registry +
  checkpoint-only injection) + critic/member kickoff blocks + planner
  role-block wording + the two notification kinds emitted backend-side
  (bell display is free — the cockpit renders `AppNotification` generically).
  k3d smoke with a gemma loop: plan filed → queue executes → analysis →
  closing critic sees disposition context, bell shows the disposition
  (plumbing only; note quality is minimax's to prove, per the parallel-stages
  precedent).
- **P2 — cockpit**: campaign chip on the loop panel (initiative title,
  stage 2/4, extensions used), plan/disposition history. Mirrors the
  parallel-stages P2 pattern (`project-loop.component.ts`).
- **P3 — live**: flip a smoke loop on the main cluster to `planner`, watch one
  full campaign cycle, then flip Better Resavio at its next run boundary —
  which is also when the acceptance-criteria field finally gets filled, so the
  planner has a real DoD to plan against.

## Acceptance criteria

1. A `rotation` loop's behavior is bit-identical to today (existing loop test
   suite passes unmodified).
2. On a `planner` loop, a critic job that files a valid 3-stage plan yields
   exactly: 3 sequential execution jobs with campaign kickoff blocks, then the
   analysis stage, then a critic whose kickoff contains the plan's acceptance
   list and the stage retros.
3. A critic job that files nothing yields today's single execution stage.
4. A plan with 6 stages, an unknown role, a missing KB note, or cost >
   `remaining − 2` is rejected at the tool call with a message the agent can
   act on, and never reaches the loop row.
5. Two consecutive campaign-member failures flush the queue and hand control
   to the analysis→critic pair with an `aborted` campaign in the kickoff.
6. Kill/ship/extend dispositions round-trip: the closing critic's plan updates
   `campaign` correctly, and `extend` beyond 2 is rejected.
7. Sweeper drill: a loop torn mid-campaign (pointer NULL, queue non-empty)
   heals by spawning the queue front, not the rotation successor.

## As-built notes (P0, 2026-07-09)

Deviations from the sketch above, discovered while reading the recovery
machinery (same convention as loop_parallel_stages.md's as-built section):

1. **No `pending_stages` column.** A shrinking queue column can't survive the
   tear between "member spawned" and "queue popped" without inventing new
   recovery signatures. Instead the FULL stage list lives in
   `campaign.stages` with a `cursor` (next index to spawn), and every member
   job is stamped `loop_campaign_id` + `loop_campaign_index` at spawn. The
   advance derives "next stage" from the completed member's **stamp**
   (`stamp + 1`), not the row cursor — the same stamp-preferring model
   `_derive_loop_counters` already uses. A stale cursor self-corrects on the
   next write-back (pinned by `test_member_stamp_beats_stale_cursor_…`).
2. **No sweeper changes at all.** The existing heal is re-point-at-newest-job
   + re-run-the-advance; since the planner branch is idempotent (stamps +
   the `plan_job_id` guard: a healed re-run of an already-applied plan
   resumes at the persisted cursor instead of re-applying), every campaign
   tear window heals through the machinery that already exists. The doc's
   sketched "sweeper heal branch" turned out to be unnecessary.
3. **Plan application does ONE extra loop-row write** (campaign + history)
   between the advance claim and the first member spawn. Ordering is
   deliberate: campaign-then-spawn tears heal via the `plan_job_id` re-run;
   the reverse order would strand a spawned member with no campaign to join.
   All other campaign mutations (cursor, member_failures, review/abort flips)
   ride the existing stage-pointer write-back — no new torn states.
4. **Roster validation is role_sequence-lax in P0**: any non-empty role
   string is accepted, exactly the contract rotation mode has for its
   entries (unknown roles fall to `_ROLE_BLOCK_DEFAULT`). The
   expert-registry check lands with the P1 tool layer, where it can give the
   critic a pick-list instead of a rejection.
5. **Two extra columns beyond the sketch**: `campaign_history` (bounded
   archive of disposed campaigns, newest last, capped at 20) and
   `campaign_caps` (per-loop guardrail overrides; `scheduling` and
   `campaign_caps` are start-time-only — not in the update allowlist).
6. **Checkpoint-vs-sub-critic is structural at intake**: the endpoint
   compares the filing job's stamped `loop_seq_index` against the template's
   critic slot — a campaign-member critic carries the execution slot's index
   and is rejected without any role-string inspection.

### P1 as-built notes (same day)

1. **Injection lever = additive tool category.** `ToolsConfig` gained a
   `loop` category (never listed in bundled configs); `create_loop_job` sets
   `config_override.tools.loop = ["loop_plan"]` for checkpoint critics. The
   deep-merge adds the key without touching the expert's other tool lists.
   Checkpoint detection simplified to *planner + role critic + not a campaign
   member* — the planner grammar already guarantees critic-stage uniqueness,
   so no seq_index arithmetic is needed at spawn.
2. **`loop_plan` is available in BOTH phases**, not strategic-only as
   sketched: the live iter-23 Better Resavio critic wrote its verdict in a
   *tactical* phase, so a strategic-only tool would never be reachable at
   decision time. The server-side gates are the real protection.
3. **`loop_user_question` detection moved to advance time.** `kb_write` is an
   agent-side dual-write straight to the stores — the sketched "orchestrator
   KB-write path" doesn't exist. Instead, after each loop job's merge/retro,
   the advance scans `knowledge_index` for `user-question`-tagged notes by
   that job (capped 5, best-effort) and notifies per note.
4. **Bell persistence = `message_log` outbound rows** (the existing
   notification store; the list view joins the job for description/config) +
   `notification_feed.broadcast` SSE kinds. Zero cockpit changes needed for
   P1 — the bell renders the rows generically; live SSE handling of the new
   kinds is P2's chip work.
5. **Kickoff blocks ride `extra_context`**: `build_loop_kickoff` got an
   optional `extra_context` param (a stamped `loop_campaign_id` marks a
   member); `_spawn_campaign_member` passes its campaign into the spawn's
   loop dict, and the rotation passes a pending `campaign_update` the same
   way — required for two-stage templates where the checkpoint critic spawns
   in the same rotation that flips its campaign to `review`.
6. **Smoke-found fix — notification persistence hit `message_log.thread_id
   varchar(12) NOT NULL`.** The first live disposition + user-question events
   fired at exactly the right moment with the right content, but the bell
   row insert passed `thread_id=None` and failed the constraint (the
   best-effort guard kept the advance unharmed; the SSE half succeeded).
   Loop events now thread as `loop-<6 hex>` (11 chars, grouped per loop).
   Two nuances recorded for later: a stored plan currently applies even if
   the critic job itself ends `failed` (defensible — the plan was validated
   at filing — but undecided), and the finale proved both event kinds fire
   in one advance (user-question scan + disposition) as designed.
7. **Smoke-found fix — loop jobs never land `pending_review`.** The first
   live campaign member (gemma) finished without declaring `job_complete`
   (no freeze_data at all); `determine_job_status`'s generic fallback put it
   on the human-review gate, wedging the loop (the advance fires only on
   terminal statuses — same invisible-wedge class as the narrowly-exempted
   Mode-A diff gate). Fix in `services/completion.py`: for loop jobs
   (`job_loop_id` present) both `pending_review` returns map to `completed`
   — retros + the next critic judge quality, F29/empty-merge flags lost
   work, and legitimate pause freezes (version_upgrade, llm_unavailable, …)
   are untouched. This closes the wedge class for rotation loops too, not
   just campaigns.

Files: `orchestrator/database/migrations/app/0050_loop_campaign_scheduling.sql`
(+ `schema_current.sql` regen), `orchestrator/database/postgres.py` (decode,
create, allowlist, campaign JSONB encoding), `orchestrator/services/
project_loops.py` (caps + `planner_slots` + `validate_loop_plan` +
`create_loop_job` extra_context), `orchestrator/main.py`
(`_advance_planner_campaign`, `_spawn_campaign_member`, rotate wiring,
`file_loop_plan` endpoint), `orchestrator/routers/project_loops.py` (start
opt-in), `tests/test_loop_campaign_scheduling.py` (68 tests incl. the P1 +
smoke-fix additions),
`docs/security/endpoint_inventory.txt` (+1: the loop-plan endpoint,
classified `internal:require_internal` by the auth-inventory script).

### P2 as-built notes (cockpit, 2026-07-10)

Zero backend changes — the loop GET already returns the whole row dict, so
the four campaign columns reached the cockpit for free. Four pieces:

1. **Campaign card** in the live loop panel (`project-loop.component.ts`):
   initiative title, status badge (active/review/aborted), per-stage role
   chips with done/current/pending states, a progress line, `extended ×N`
   marker, member-failure warning, and the pre-registered acceptance list
   (the campaign's contract — showing it is the point). "Current" derives
   from `max(stages_done, cursor − 1)` so a stale cursor (torn advance,
   healed later) can never point at an already-finished stage.
2. **Campaign history block** ("Campaigns (N)", newest first, outcome badges
   ship/extend/kill + notes) — rendered for terminal loops too: the run's
   investment record outlives it.
3. **Scheduling opt-in on the start form**: a Rotation/Planner select plus
   `plannerIneligibility()`, a client-side mirror of the `planner_slots`
   grammar (exactly one single-role critic; the cyclically-next stage
   single-role) that renders the problem inline and blocks `start()` —
   saving the doomed round-trip; the server remains the authority.
4. **Live SSE handling** (`notification.service.ts`): `loop_user_question` +
   `loop_campaign_disposition` frames bump the unread count, prepend a bell
   entry whose `thread_id` mirrors the server row's `loop-<6 hex>` key (so a
   REST refresh dedupes against the live entry), and toast the subject.

Tests follow the file's pure-function convention (no TestBed): 20 new specs
across `plannerIneligibility` / `campaignStageState` / `campaignProgressLabel`
+ 3 SSE-kind specs; cockpit suite 876 green. k3d-verified 2026-07-10 against
a seeded planner loop row (status `paused` so the torn-advance sweeper —
running-only — ignores the synthetic row, `current_job_id` NULL for the jobs
FK): card, chip states, history, terminal-state history persistence, the
scheduling select, and the inline planner guard all confirmed in the browser;
seeded project deleted after.

### P3 first-flip findings (Better Resavio, 2026-07-10)

The owner pushed P0–P2, started a fresh planner loop on Better Resavio
(loop `8e832884`, scholar → critic → developer, MiniMax-M3, VM workspace),
and let it run overnight. Symptom: **six jobs, two critic checkpoints, zero
campaigns** — every cycle fell back to the single default developer stage
(`campaign` NULL, `campaign_history` []), i.e. planner scheduling behaved
exactly like rotation.

**Forensics** (srw-auditdb `llm_requests` is the ground truth for what a
model could actually call): both critic jobs had the correct
`config_override` (`tools: {loop: [loop_plan]}`) in the jobs row and the
PLANNER DUTIES kickoff in `context->kickoff_message` — but **zero** of their
LLM requests contained the `loop_plan` tool schema (probe: the schema-only
string `initiative_note_id`; the 84 hits for the bare string `loop_plan` in
iter-2's requests were the *kickoff text*, a probe trap worth remembering).
The critic was being told "you MAY file a campaign plan with the `loop_plan`
tool" while owning no such tool.

**Root cause**: `src/core/loader.py` — the P1 commit added the `loop` field
to the `ToolsConfig` dataclass and the registry wiring
(`create_loop_tools`), but the two `ToolsConfig(...)` construction sites
(`load_agent_config` + `load_agent_config_from_dict`) enumerate category
kwargs explicitly and were never given `loop=tools_data.get("loop", [])` —
the merged override survived YAML/dict parsing and was dropped at dataclass
construction, defaulting to `[]` forever.

**Collateral discovery**: `communication` was missing from the same two
constructor calls, meaning `defaults.yaml`'s `communication: [send_message]`
never bound for ANY worker agent (verified live: 0/249 requests of a fresh
scholar job carried the `send_message` schema). Same bug class, pre-existing.
The fix restores it — worker agents can now actually `send_message`, which
is defaults-intended behavior but a live behavior change to be aware of.

**Fix** (uncommitted): both constructor sites now pass `communication` and
`loop`; regression tests in `tests/test_loop_campaign_scheduling.py`
(`TestAgentLoaderBindsLoopCategory`, red without the fix / green with it),
including a dataclass-fields parity test so the next `ToolsConfig` field
addition cannot silently repeat this. Suite: 72 campaign tests green, full
pytest green except a pre-existing live-DB-dependent local failure.

**Why the P1 k3d smoke missed it**: "tool injection verified" checked the
`config_override` *row content*, and plan intake was exercised by POSTing
the endpoint directly — nobody asserted the schema reached the model. The
new regression tests close the agent half; for live verification the
`llm_requests` schema probe above is the check to repeat.

**Unrelated one-off observed**: the iter-5 critic (`7b83d65f`) initialized
with `task_brief_length: 0` and `task_brief.md` missing from its (reused VM)
workspace — its kickoff never materialized at all. Single occurrence, during
the 19:17Z deploy-rollout window; the other five jobs' briefs were intact.
Watch for recurrence; not chased.

## Resolved questions (owner + assistant, 2026-07-09)

1. **Cap values** — keep 5/2/2 defaults; **per-loop overridable in the start
   request**, clamped by config hard ceilings (K_MAX ≤ 10). Rationale: caps
   are experiment parameters tuned per loop during P3; follows the existing
   per-loop pattern (`model`, `max_iterations`, `role_sequence`).
2. **`user-question` notes** — **simple notification-center integration now**
   (`loop_user_question` bell kind, tag convention fixed in this doc); the
   dedicated inbox + answer-back flow is a later, separate feature folded into
   the notification center. Nothing blocks P0.
3. **Disposition visibility** — **yes, notify**: `loop_campaign_disposition`
   bell kind + loop timeline actions. No email, no push.
4. **Roster scope** — **any registered expert, critic included.** A scheduled
   sub-critic is fine; the capability boundary is plan-filing, not existence —
   only the checkpoint critic gets the `loop_plan` tool (spawn-time injection
   + intake-time seq_index check, defense in depth). Sub-critics have no
   loop-creation surface, so no recursion.
5. **Season coupling** — **constant caps.** Hard limits are the safety layer;
   campaign-length taste (long early, short late) is prompt policy for the
   agents to exercise within the caps. No mechanical coupling to a feature
   that doesn't exist yet.
