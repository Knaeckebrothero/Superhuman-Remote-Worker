# Codex brief — Gate 3 step 4: routed sweeps + the status-write move

**Date: 2026-08-13 (overnight run). Branch: `develop`, directly. Commit
locally per milestone. DO NOT PUSH — morning review decides.**

## 0. Ground rules (unchanged from the steps-2+3 brief; deltas only)

Everything in `codex_gate3_steps23_brief.md` §0 still binds: §5.4.5 is the
sole design authority (this brief quotes no DDL/predicates — build them from
the doc); the side-effect inventory must be read before touching completion;
narrowed stop rule; explicit `git add` paths only, `HomeLab/` untouched;
baseline first (`./scripts/pytest-fast.sh tests/ -q --tb=line` — note the
script defaults to `-x` when argless, so pass args; expect the exact 11
env-noise failures); implementation-log entries per milestone; snapshot
regeneration in the same commit as any migration; squawk pinned v2.59.0.

Deltas for tonight:
- **Migrations start at 0141** (0140 shipped; 0134–0139 stay reserved).
- **Tilt is UP** (left running since the last soak). File writes in watched
  paths deploy to k3d immediately. Use it; never `tilt trigger srw`, never
  checkout, and `kubectl exec … grep` pod contents before trusting any
  image-dependent smoke.
- **k3d carries preserved residue**: 2 `parked` + 1 `pending` completion
  commands and 2 pending effects from the steps-2+3 soak. Tonight's
  safety-net sweep (M4) may legitimately consume them — that is live fixture
  data, not something to clean by hand. Record what happens to each row.
- The steps-2+3 flag `COMPLETION_COMMANDS_ENABLED` stays exactly as shipped:
  tracked default false, ON only in the local overlay.

## 1. What you are building

Rollout step 4 of §5.4.5 (7), whose content is decision (6) in full plus the
status-write move from the step-0 triage. Read, in this order, before any
code: §5.4.5 decision (6) ("The orphan sweeps must learn about pending
commands"), the step-0 triage classes A–D, the "Authority is finalization
ORDER" rule in decision (2), the revert rules under (7), and
`gate3_adversarial_review.md` for the three synthesizer collisions' evidence.

Two-layer safety model, and it decides what needs a new flag:
- **The routing/deference layers (M1–M3) ride the existing
  `COMPLETION_COMMANDS_ENABLED` flag** — with it off there are no command
  rows, every new predicate is vacuous, and behavior is byte-identical. No
  second gate needed; prove that vacuity with tests.
- **The status-write reorder (M4) gets its OWN default-off flag** (name it
  in the family of the existing one, e.g. `COMPLETION_STATUS_REORDER_ENABLED`
  + a helm value), which **requires** the commands flag: reorder-on without
  commands-on must refuse loudly at startup, not half-activate. Independent
  revertibility is a stated design requirement — during a soak, rollback
  means flag-off-or-drain, and your ops note must say so (add it to the
  §9.1 status section when you update it).

## 2. Milestones

**M1 — route, don't filter (rescuer class 1).** All re-dispatch rescuers —
`recover_orphaned_jobs`, `recover_expired_lease_jobs`, the stale-agent
detector, and the pause-command redispatch sweeps — consume ONE shared
predicate (the 0140 view is its seed; extend it there, never copy it) and
implement the four-row routing table: no command row → today's behavior;
live lease → stand down; **expired lease → resume the finalizer from its
effect rows, never re-dispatch the agent**; past `deadline_at`/attempts →
park + alert. Include the parked-before-status-write hole (parked rows are
excluded from re-dispatch and routed to alert-only) and the reap-action
dedup key `(job_id, attempt)`. Real-PG tests per routing row.

**M2 — concurrent verbs + resume/claim barriers (classes 3+4).**
- Finalizer's Class A status write gains the expected-status CAS so cancel /
  dispatcher preemption / drain / human approve cannot interleave silently.
- The resume/approve/feedback verbs become command-aware: refuse 409
  ("completion finalizing") while the job's newest command is live-or-within-
  deadline; on an expired lease, kick the resume-the-finalizer arm first.
  `queue_job_for_resume` today is `WHERE id = $1` with no status predicate —
  that is the confirmed hole.
- The run_queue claim predicate excludes units whose job carries a live-or-
  within-deadline terminal command (bounded wait, never blocked-forever).
- S36 re-checks in its own transaction that no higher `report_seq` exists
  before destroying the workspace.
Acceptance test: the review's traced catastrophe — 202 accepted, finalizer
backlogged, user resumes, successor claims — must now die at the 409, and at
the claim exclusion if the verb is bypassed.

**M3 — bidirectional deference (synthesizer class 2).** The three confirmed
collisions, each fixed both ways (sweep stands down on live lease; the
finalizer's own effect CASes on world state and marks itself `superseded` on
miss):
- `unstick_reviewing_parents` vs the critic spawn — spawn gains the
  parent-status predicate; the verdict writers gain expected-status CAS.
- Project-loop heal vs loop advance — take the doc's preferred fix: move the
  barrier claim out of Class B into the same Class C transaction as the
  spawn.
- Lifecycle reconciler vs Class C teardown — **this replaces yesterday's
  conservative ownership veto in `lifecycle/workspace_manager.py`** (its own
  comment says so): route it — skip while the lease is live, and on expiry
  resume the same UID-keyed effect rows rather than running a parallel
  teardown. One shared UID-preconditioned delete implementation in
  `container_provisioner` for both actors (VM path already has
  preconditions; pods/PVCs are the gap). Migrate the veto's tests to the
  routed behavior rather than deleting them.

**M4 — the reorder + the nets.** Behind the new flag:
- Terminal status moves after **Class B + the delivery effects only**. The
  delivery set is a judgment call the doc bounds but does not enumerate
  ("delivery effects gate the terminal status; teardown, spawns, loop
  advance and notifications do not") — derive it from the inventory,
  **record the chosen set in §5.4.5 as a short step-4 note**, and encode it
  as effect-group metadata, not scattered conditionals.
- The day-one safety net: the dumb second sweep for command rows whose job
  is already terminal or that never advanced past their first effect —
  **reconcile means mark-`superseded`, never execute** (the rollback-stranded
  rows scenario; executing a stale S36 months later is the workspace kill
  through the back door).
- The operator force verb: abandons remaining effects, still writes terminal
  status, marks `force_resolved`, records skipped effects, logs as an
  incident.
- Alarms in the sweep/monitoring path (never inside the finalizer loop):
  zero finalizer leaders, and max **age** of the oldest unfinalized command.

**M5 — the CI invariant.** The generalized ownership property as a real-PG
test: every job carrying an unfinalized command row is owned by exactly one
actor — live agent lease, live finalizer lease, or exactly one routed sweep —
**whatever its status** (`reviewing`, terminal, `paused`, `waiting` included;
scoping to `processing` misses every real collision). Construct each M1–M3
collision scenario and assert single ownership.

**M6 (stretch, only if M1–M5 are green and soaked) — step 5.** Background
finalizer for **stateless units only**: their accepts return 202 and the
background drain finalizes (agent-side 202 handling already shipped). Add the
stateless queued-age alarm (scale-to-zero leaves queued units with no
claimant). Pinned stays inline. Same flag discipline.

## 3. The soak matrix (k3d, flag(s) ON — record evidence per row)

From §5.4.5's acceptance lists, the rows step 4 owns:
1. Park a command after the `reviewing` write >30 min with the verification
   sweeper live, then unpark → exactly one critic, status coherent with the
   human's decision.
2. Park between the loop-barrier claim and the advance >10 min with the loop
   sweeper live → exactly one stage-N+1 spawn.
3. Resume a job whose terminal command is still finalizing → 409, workspace
   survives.
4. Report `job_complete`, crash, error-report under the same token →
   first-wins, job stays completed (finalize in `report_seq` order).
5. Cancel between accept and finalize → command supersedes, no Class C
   effect runs.
6. Wedge the finalizer (SIGSTOP mid-effect) → job rescuable once the lease
   expires, terminal by `deadline_at`, and matched by exactly one actor the
   whole time.
7. Kill the leader ungracefully → takeover in seconds, not minutes.
8. Delete a workspace pod, recreate under the same name, resume the command
   → the replacement survives (UID precondition proof).
9. With the reorder ON: kill the orchestrator between the delivery-gated
   effects and the status write → restart converges to the same terminal
   state, no duplicated delivery, no leaked workspace.

## 4. Traps for tonight specifically

- The parked-exclusion clause is the difference between "stuck forever" and
  "bounded delay" — do not simplify the predicate to `pending|finalizing`.
- Resume-the-finalizer on expired lease is the row people get wrong; the
  catastrophic default is re-dispatching the agent. Test that row hardest.
- The loop-heal age gate's own comment documents the live dup-iter-14
  incident — that heal must never be "cleaned up" into firing eagerly.
- `determine_job_status`'s coincident-error backstop stays load-bearing.
- A transaction touching two keys in the 0132 index can 23505/40P01 far from
  the offending INSERT — every revival path you touch must treat both as
  "someone else won".
- Yesterday's veto tests encode real races; migrate them, don't drop them.

## 5. Final gates + report

Same as the steps-2+3 brief §5/§6: full suite at the 11-failure baseline,
ruff, squawk, snapshot idempotence, per-milestone log entries, and the
morning hand-check nomination. Add: the soak-matrix table with per-row
evidence, the delivery-set decision recorded in §5.4.5, and — if M6 ran —
which stateless units finalized in background and their queue timings.
