# Gate 3 adversarial review — synthesis of record

Reviewed 2026-08-10/11 at commit `b5794fda`, by four independent adversarial
critics (lenses: **sweep interleavings**, **protocol/fence/identity**,
**storage/effects schema**, **rollout/coexistence**), each grounding every claim
in code and instructed to refute seeded candidates. Load-bearing claims were
then spot-verified first-hand (marked ✓ where I re-read the code myself this
session). **Every seeded candidate was CONFIRMED.** ~35 raw findings, deduped
and triaged here. This file is the input to the §5.4.5 fold; until that fold
lands, where this file and §5.4.5 disagree, **this file wins**.

**Verdict in one paragraph.** The protocol core — durable command row, accept
fence, background finalizer, effects-as-rows — survived all four attacks; no
critic found a reason to abandon the shape. What did not survive is the
*periphery*: the design integrated with 3 of the orchestrator's **11**
job-status-keyed autonomous actors and mis-specified several of its own
mechanisms (one PK, the authority rule, the parked state, the pinned fence's
provenance). Ten blockers below change the design before step 2 ships anything;
none of them changes its direction. Separately, the review surfaced one **live
pinned-lane bug family existing today** (ready-before-report) that explains a
"job ran twice" symptom class and deserves a fix independent of Gate 3.

---

## The reframe: four actor classes, not one predicate

Decision (6) routed three sweeps. The census (below) finds **eleven**
status-keyed autonomous actors, in four classes needing four different
treatments:

1. **Re-dispatch rescuers** (recover_orphaned, recover_expired_lease,
   stale-agent; plus the llm_outage / infra_transient / vm_upgrade-expiry
   redispatch sweeps for *pause* commands) → the exclusion **view + routing
   table** works as designed. Extend membership; nothing structural.
2. **Status-consuming effect synthesizers** (unstick_reviewing + wallclock arm,
   project-loop sweeper heal, lifecycle workspace/VM reaper, session-wake
   status arm) → exclusion alone is insufficient or wrong. Each needs
   **bidirectional deference**: stand down while the command's lease is live;
   once the sweep has legitimately fired (expired/parked — it *is* the operator
   fallback), the **finalizer's own effects must CAS on the world state and
   mark themselves `superseded` on miss**. The lifecycle reaper additionally
   must *resume the same UID-keyed teardown effect rows* rather than run a
   parallel implementation — it is decision (8)'s leak backstop and cannot be
   excluded forever.
3. **The queue-side rescuer** (run_queue reaper) → unfixable by any jobs-side
   predicate; fixed **at accept** (B4).
4. **Non-sweep concurrent verbs** (cancel, preempt, drain, human approve) →
   need a status predicate on the **finalizer's own Class A write** (B10); no
   view can provide this.

**CI invariant, generalized** (the design's is scoped to `processing` and every
collision found occurs in `reviewing`/terminal/`paused`/`waiting`): *every job
with an unfinalized command row is owned by exactly one actor — live agent
lease, live finalizer lease, or exactly one routed sweep — whatever its
status.*

---

## Blockers (design changes required before step 2 ships)

**B1 — effects PK collides for session producers.** ✓ `producer_id` = "the
turn's unit id", but a session unit's `unit_id` IS the thread_id (0115: one
durable row per unit) — turn N+1's effects silently swallow or 23505. Neither
`lease_token` (not 1:1 with a turn; steals double effects; BIGINT≠UUID) nor
`consumed_seq` (control-induced turns collide; two watermark domains since
0119) works. **Fix: mint a `turn_execution_id` inside the fenced transaction
that makes the turn durable** — a fenced-out attempt never commits its effect
rows (they neither orphan nor double; they never exist). Add a nullable indexed
`scope_id` for the thread join; define `complete_by` against the finalizer's
per-effect claim (the turn's queue lease is gone by then). Name the run_queue
module-contract relaxation this enqueue-in-persist-txn requires.

**B2 — `parked` commands are invisible to the sweeps.** ✓ (my predicate; the
hole is mine.) A command can park *before* the status write (delivery group
exhausts attempts — the design's own dialect), leaving the job `processing`,
agent gone, predicate TRUE → pause + re-dispatch against a half-applied parked
command. **Fix: exclusion covers all non-terminal command states
(`pending|finalizing|parked`); routing for `parked` = alert-only** (it already
is the operator worklist); CI invariant names parked explicitly.

**B3 — nothing blocks a new claim/resume while a terminal command is pending.**
✓ (`resume_job` allows `failed`; `queue_job_for_resume` is `WHERE id=$1`, no
status predicate — I read it.) Verified timeline: terminal report 202 →
finalizer backlog → user resumes → new pod claims the same PVC → finalizer
drains the OLD command's Class C and **destroys the workspace under the running
successor**; the UID precondition does not help (same object, legitimately
captured UID). **Fix, three layers:** the lane-aware resume verb refuses (409
"completion finalizing") while the newest command is `pending|finalizing` with
a live lease or within `deadline_at`, and on expired lease kicks
resume-the-finalizer first; the claim predicate excludes units whose job has
such a command (decision-6's liveness answer transfers verbatim: live lease OR
within deadline, so blocked-forever degrades to bounded-wait); S36's executor
re-checks for a higher `report_seq` in its own transaction before deleting —
the authority rule applied at the effect site.

**B4 — accept must terminal-ize the run_queue unit in the same transaction.**
Otherwise: agent reports terminal (202), dies before `complete_unit`, lease
expires, reaper requeues, successor **re-runs the final batch** inside a
workspace being archived, then files a second report under a valid fence that
the old authority rule would crown. **Fix: a terminal-report accept runs
`complete_unit` (or terminal-park) under the report's token in the accept
transaction; fence failure rejects the report.** Acceptance: kill the agent
between 202 and queue release; assert no successor claims.

**B5 — unstick vs. the resumed finalizer: two authorities on one round.**
Sweeps critic's confirmed seed, worse than seeded: finalizer writes
`reviewing` (Class B), parks before the critic spawn; unstick fires at 30 min
(vacuously eligible with zero critic children ✓ predicate read), human is
invited and decides; finalizer later resumes and spawns anyway (spawn path has
**no parent-status predicate**); the eventual verdict is applied by writers
with no status predicate (`queue_job_for_resume` ✓; `_set_target_to_
autonomy_status` unconditional per critic) — a `returned` verdict re-queues a
human-approved, merged, torn-down job; an `approved` verdict overrides a human
rejection. **0132 does NOT help here — this interleaving contains exactly one
critic INSERT** (attempt died before `create_job`); the index closes only the
crash-after-INSERT-before-marker case. **Fix: bidirectional deference (class 2
above)** — unstick joins the view for live-lease commands; the `critic_spawn`
effect CASes on `status='reviewing'` and supersedes on miss; the verdict
writers gain expected-status predicates.

**B6 — loop heal re-arms a barrier the finalizer already claimed.** ✓ (heal
guard is emptiness+age only; the code comment documents the live duplicate-
iter-14-critics incident this age gate was built against.) The finalizer makes
the claim→write-back window routine-unbounded, invalidating the age heuristic:
heal restores membership, re-runs the advance; the resumed finalizer, whose
effect log says the claim is done, spawns stage N+1 **again**. **Fix (simpler
of the two): move the barrier claim out of Class B into the same Class C
transaction as the spawn** — nothing in the triage rationale requires the claim
to precede the status write (loop jobs resolve `completed` regardless). Else
the heal must consult the effect table.

**B7 — lifecycle reconciler races Class C teardown, in a different leadership
domain.** Terminal statuses are reapable with **no grace**; `reviewing` is
idle-reapable with only a live-child guard — which is false between the Class B
`reviewing` write and the Class C spawn, so the parent pod is reaped under the
not-yet-spawned critic (re-creating a documented incident). Double
snapshot-then-delete to the same S3 key; reconciler can delete the pod
mid-finalizer-tar. **Fix: route, don't exclude** — terminal job + live-lease
command ⇒ skip; expired ⇒ the reaper resumes the *same UID-keyed teardown
effect rows*. Ship UID preconditions once in `container_provisioner`, shared by
both actors. **Correction to the critic: the VM path already passes
`preconditions: {uid}` (vm_provisioner.py:1138, 1959 ✓) — copy that precedent;
pods/PVCs genuinely lack it.**

**B8 — the pinned fence has no provable provenance, and its substrate is a live
bug today.** ✓ (I read the ready-before-report ordering myself at app.py
630–675.) The agent heartbeats `ready` BEFORE reporting; `recover_orphaned_jobs`
pauses on "agent says ready" with no `current_job_id` check and no grace;
variant A discards a legitimate completion with a 400 (rescue path is
failed-only); variant B spans the whole handler (minutes for loop delivery) —
sweep pauses + re-dispatches mid-handler, S17 later blindly overwrites
`paused→completed` while a second agent re-runs. `accepted_agent_id` can only
be copied from `jobs.assigned_agent_id`, which the sweep NULLs asynchronously —
the fence as specced is vacuous or fail-closed on the race. **Fixes, layered:
(1) swap the two awaits — report first, ready-heartbeat second (two lines,
step-1-class, fixes a live bug); (2) the accept INSERT becomes the FIRST DB
write of `/complete`, before any guard — the routed sweep then sees the command
and stands off, making the race structurally impossible; (3) `JobCompleteRequest`
gains optional `agent_id`, compared at accept.**

**B9 — "highest report_seq wins" manufactures a regression.** Driver reports
`job_complete` (seq N), crashes during teardown; the outer error handler
reports the failure (seq N+1, same still-valid token) → the error report wins →
completed work marked failed. Today's status guard gives first-wins. **Fix:
keep `report_seq` for ordering and audit; drop "highest wins" — commands
finalize in seq order, each applying against the state its predecessors
produced**; the existing already-successful backstop in `determine_job_status`
absorbs the trailing error as a no-op.

**B10 — the Class A terminal write must CAS on expected entry statuses, not
only the finalizer term.** The term CAS defends against a resurrected
finalizer, not against a legitimate concurrent writer: report accepted (202) →
user cancels / dispatcher preempts → background finalizer commits `completed`
over it and runs merge/spawn for a cancelled job. Inventory destructive-
interleaving #4 survives the redesign verbatim otherwise. **Fix:
`AND status = ANY($expected_entry_statuses)`; a miss routes the command to
`superseded`/`parked`.** Condition 4's renewal-read covers mid-run cancels
only; this closes the post-report window.

---

## Majors (settle before implementation; none blocks step 2's *schema* once B1
and the DDL fixes land)

**Identity/retry.** Mint `client_report_id` randomly ONCE, persist id+payload
verbatim together (freeze_data/checkpoint), resend verbatim; re-derivation is a
new stop with a new id — the freeze embeds `datetime.now()` and `head_commit`,
so a deterministic id false-422s an honest crash-retry ✓ (freeze shape read).
Digest computed **server-side** over canonical JSON excluding transport/fence
fields. The server-synthesized fallback key **dedups nothing** (every arrival
mints a fresh seq → fresh key) — it is a NOT-NULL filler; the real skew rule is
agent-first deterministic ids, with an alarm on the synthesized-key fraction.
Retry-response matrix needs three missing rows: `parked` (409's "retry, it will
succeed" is FALSE for days — return 202-still-pending without the retry
promise, or expose parked), `superseded` (terminal "superseded by seq" — has no
outcome to return), `force_resolved`.

**State machine hygiene.** Six states used, five declared (`force_resolved`
missing from the list and from the sweep predicate); nothing ever *sets*
`superseded` — name the finalizer as that actor at winner selection. The
terminal-shape CHECK admits half-written states (pending rows with outcomes,
parked with finalized_at) — adopt 0119's full bidirectional style; the "no
CHECK, run_queue convention" rationale borrowed the wrong precedent (0115's was
about an existing hot table; these tables are new and empty). `fence_exactly_
one` forbids operator-origin commands that already exist as code paths —
`apply_terminal_job_side_effects` runs from **four** call sites ✓ (approve,
/complete, Mode-A accept + reject) with no agent identity; add
`origin`/`requested_by` and scope the XOR to agent-origin rows.

**Lock order.** Binding rule, written down: any transaction locking both a
`run_queue` row and its `jobs` row acquires **run_queue first** — the claim's
shape forces it, and this **inverts the 0119 parent-first precedent** (safe on
sessions only because `claim_unit` never locks `threads`). "Extend the proven
pattern" must not be applied to lock order. Accept therefore validates the
fence (FOR SHARE on run_queue) *before* the jobs-row hwm lock.

**deadline_at.** Origin: `accepted_at + per-lane constant` (name them). It
bounds *machine-driven* finalization only (`pending|finalizing` → auto-park);
`parked` is exempt (else unpark-after-deadline instant-re-parks and the only
exit force-abandons delivery — defeating effect groups); explicit `unpark`
re-arms attempts AND stamps a fresh deadline epoch. Reword the wedge-acceptance
bullet to "terminal **or parked-with-alert** by deadline_at".

**The 60-second fuse.** `report_completion` times out at 60 s and starlette
**cancels the handler on client disconnect** — today's completion pipeline has
an undocumented upper bound whose failure mode is already duplicate execution
(cancelled pre-status-write → orphan sweep re-runs). Step 3 must (a) split
accept from finalize so cancellation can only land between them, (b) ship the
finalizer-resume drain WITH step 3 (not step 4/5 — else cancellation mints
undrainable pending commands), (c) agent: 202-as-success + retry-once-on-
timeout, safe today (status guard) and correct after (idempotency key).

**Effects/commands schema (with B1).** Indexes: commands get
`(run_after) WHERE state IN ('pending','finalizing')` (bounded churn — rows
transit once, retries capped); the sweeps' NOT EXISTS is served by the
`(job_id, …)` prefix of `uq_job_completion_seq`; the effects table must NOT get
a mutable-state partial index — either single-transition rows (intent folded
into INSERT) or the tiny-hot-table/durable-log split. Retention: named owner
(extend the reaper), order (effects before command; command id is the
enumeration key), LIMIT-batched deletes, an orphan sweep for effects whose
producer vanished (commands CASCADE on job deletion — effects must not strand ✓
job deletion is a live endpoint), `created_at NOT NULL` on effects (age alarms
are impossible today — `intent_at` is nullable), failures outlive successes.
`effect_group` semantics are unrepresentable as drafted — move
`max_attempts`/`run_after` onto effect rows; group = derived; command `done`
iff every row terminal-or-abandoned, enforced app-side + by the day-one safety
net. `detail` holds fixed-cardinality capture only (UIDs, SHAs, counts; ~8 kB
cap, truncate-and-count) — S15's per-file inventories stay in their existing
stashes. `payload` stays (late-completion re-resolution needs the as-reported
body; parked finalizers must not read a mutating `jobs.freeze_data`) — declare
the command row the report of record and retire the S19 context stash at step
4.

**Rollout/revert (adopt into step list).** Step 4 inherits §5.4.1's discipline,
stated: **drain (or flag-off the reordering) before any orchestrator rollback
during the soak**. Step 5 off with commands queued: the max-age alarm must live
in the sweep/monitoring path, **not** the finalizer loop (else disabling the
finalizer disables the alarm). Step 6 re-closed: **admission-off ≠
executor-off** — stop enqueueing, keep the claim loop until in-flight units
drain terminal (lane-flip re-dispatch restarts from phase 0: rotation never
stashes freeze_data and pinned agents cannot read the fenced PG checkpoint);
plus a stateless queued-age alarm (scale-to-zero leaves queued units with no
claimant and no reaper coverage). Steps 2/3 rollback: the day-one safety net's
"reconcile" for stale commands **means mark-superseded, never execute** — an
old S36 executed months late is B3 through the back door. Step 1 rollback note:
an image rollback cannot remove the 0132 index — pre-step-1 code has no 23505
handler, so the closed race re-manifests as an unhandled 500 (fail-loud;
acceptable; name it).

**Pinned-parity acceptance, testable form.** Split into contract (same terminal
status across the stop matrix; same response shape; same *set* of effects,
order-free, assertable from the effects table; same guard responses; p95
handler latency envelope) vs. accepted-documented deltas (intermediate status
timeline, `completed_at` skew ≤ delivery duration, new rows, new 4xx classes,
delivery-failure parking). Anything observable and in neither list fails.

**Smaller.** Session-wake's status arm synthesizes wakes from status alone ✓
(by design) — under the new order the wake outruns graft/merge by ≤20 s and the
officer acts on a repo whose merge is pending: add the pending-command
exclusion to that arm, or declare wake-before-merge accepted (it is ordering
only; dedup holds). S24 freeze snapshot checks the dispatch epoch (else it
overwrites the deterministic snapshot key with post-resume bytes after a
redispatch). S28 scholar unblock gets the CAS its delegation sibling has
(two writers now). Graft-group → unblock-group is a declared dependency.
Curation resume: marker and resume in ONE transaction (the stated rule; flag it
for this effect explicitly).

---

## Live pinned-lane bugs found (independent of Gate 3 — fix sooner)

1. **Ready-before-report** (B8 layers 1): two-line swap at app.py:640 and
   :1193. Variant B explains a "job ran twice" symptom class.
2. **Numbering double-book in the doc** ✓: rollout step 2 says the command
   tables land at "0133+" (two places) while the same commit carved 0133–0139
   to the worker driver — step 2 begins at **0140**. Fix at the fold.
3. Already known, sharpened: S28 no-CAS now has two possible writers; the
   fail-path of the outage sweeps runs parent-unblock effects with no command
   row (a second effects executor to fold in at step 3+).

## Already shipped tonight (driver brief §6b, commits f882e856 + 66226a09)

Claim CAS includes `processing` (re-assert) + terminal-at-claim consumes the
unit; rotation verb prescribed (watermark-bump + `complete_unit`; never
`release`); cancel via driver-side companion read; token as optional
`JobCompleteRequest` field, fence = token equality only, no-op keyed on
`execution_lane='pinned'`; terminal ordering report→2xx→`complete_unit`, raised
timeout, kill-the-handler fault test; recoverable-error stops NEVER call
`/complete` (release with backoff — the queue IS the retry machinery);
failure-arm = release-with-backoff; lock order run_queue-first.

## Acceptance additions (consolidated)

- Park the command after the `reviewing` write >30 min with the verification
  sweeper live; unpark; assert exactly one critic and a coherent target status
  (B5).
- Park between barrier claim and loop advance >10 min with the loop sweeper
  live; assert exactly one stage-N+1 spawn (B6).
- Complete a job with the teardown group blocked; assert exactly one snapshot
  object and that the reconciler waited or resumed the group (B7).
- Kill the agent between 202 and queue release; assert the unit is not
  requeued (B4).
- Cancel between accept and finalize; assert the command supersedes and no
  Class C effect runs (B10).
- Same-token sequential double report (complete then crash-error); assert
  first-wins semantics (B9).
- Kill the orchestrator handler mid-terminal-completion on the stateless lane;
  assert consume-or-benign-re-report, never park (60 s fuse).
- Block the merge group; assert the session wake is deferred or documented
  early (wake ordering).

## Actor census (coverage proof — 11 affected / checked)

recover_orphaned_jobs, recover_expired_lease_jobs, stale-agent detector
(class 1, routed); llm_outage + infra_transient + vm_upgrade-expiry redispatch
sweeps (class 1, add for pause commands); unstick_reviewing + wallclock arm
(class 2, B5); project-loop sweeper (class 2, B6); lifecycle reconciler
workspace+VM (class 2, B7); session-wake sweeper status arm (class 2, minor);
run_queue reaper (class 3, B4); dispatcher preemption + cancel/drain/approve
verbs (class 4, B10); delegation-timeout sweeper (benign — CAS proved; ordering
residue only); sudo-expiration sweeper (class-4 family, CAS on paused).
Checked and NOT affected: agent-pool reconciler, thread/session/IDE sweeps,
retention/GC/prune loops, create-side and read-side loops.

## DDL verdict for step 2

**Do not ship the DDL as currently written in §5.4.5.** Confirmed defects: B1
(PK), missing indexes, missing `created_at`, missing `origin`, effect-group
semantics unrepresentable, state list vs. CHECK inconsistencies, retention
unowned. The fold rewrites the DDL; step 2 proceeds after.

## Aggregate unverified

Per-image starlette versions (cancellation analysis relies on ≥0.35; dev venv
0.50 ✓ plausible for all images); real effect volumes (no production metering —
estimates labeled); S33 merge call-site ordering (inventory-order-based); VM
delete-flow internals beyond the precondition sites; all interleavings are
code-path traces, not live reproductions — each blocker carries a one-evening
reproduction recipe in its acceptance line.
