# Outage/cooldown resilience for subjobs (scholar, critic, delegates)

Status: **IMPLEMENTED on develop + k3d E2E VERIFIED 2026-07-16** (§Implementation
notes). Refined 2026-07-16 by a multi-agent code audit + industry survey;
decisions locked same day (§Decisions locked).
Date: 2026-07-16
Scope: **subjobs** (`parent_job_id` set) of worker jobs — the auto-spawned pre-job
**scholar**, post-job **critic**, and `delegate_work` **delegates**. Extends
`[[llm_cooldown_pause_and_resume]]` + `[[llm_outage_pause_and_backoff_redispatch]]`;
top-level jobs already pause+resume (k3d-verified 2026-07-16).
Provenance: found during the cooldown-pause k3d E2E — a scholar subjob emitted a
cooldown freeze but surfaced as `pending_review` instead of pausing. Refined by a
4-agent research pass: delegation-timeout internals, subjob routing/reapers,
per-kind lifecycles, and how Temporal/Step Functions/Celery/Airflow/Oban/K8s solve
the same problem. Line numbers below are develop @ 2026-07-16 and will drift.

## Problem

A retriable LLM outage / quota cooldown **pauses + resumes a top-level job**, but a
**subjob is diverted to `pending_review`** by the subjob short-circuit in
`determine_job_status` (`completion.py:719-748`): the freeze lands on the generic
fallback (`:747-748`) before the type-specific `llm_unavailable` (`:815`) and
memory/kb (`:794`) branches are ever reached. Only `version_upgrade` is special-cased
for subjobs (`:743-746`).

**What `pending_review` actually does per kind today is worse than "surfaces to a
human"** (audit findings, all with live code refs):

| Kind | Outage → `pending_review` means… |
|---|---|
| **pre-job scholar** | **Silent false success.** `_handle_scholar_completion` (`main.py:10874-10951`) treats anything not `failed`/`cancelled` as success (`is_failure`, `:10905`) → parent unblocks with `scholar_completed=True` and *no research output*. Nobody notices. |
| **post-job critic** | **Live wedge.** Parent stuck in `reviewing` forever: a `pending_review` critic is not cancellable by the stale-verification sweeper (only `created`/`paused`, `postgres.py:4208`) *and* blocks `unstick_reviewing_parents` (`NOT IN ('failed','cancelled')`, `postgres.py:4268-4273`). No notification. Pre-existing bug — exists today independent of this feature. |
| **`delegate_work` delegate** | **Counts as terminal.** `all_delegation_children_terminal` includes `pending_review` (`postgres.py:1548-1556`) → parent unblocks and proceeds with an empty result for that child. |

`spawn_subagent` **light mode needs nothing**: light delegates are in-process readers
(no job rows, `light_runner.py`); an outage there surfaces in the *parent's* turn and
the parent's own (already shipped) pause handles it. Heavy mode = `delegate_work`.

## Goal

A subjob that hits a retriable `llm_unavailable` (cooldown within budget, or transient
outage) **pauses and resumes from its checkpoint** like a top-level job — **without
being reaped by any parent-await mechanism while legitimately waiting** — and still
fails loudly (with the parent unblocked) at the 12h ceiling or when its parent is
terminal.

## How each kind actually waits (audit result — they do NOT share one mechanism)

| Kind | Parent-wait mechanism | Delegation 2h timeout applies? | Reaper that would kill a paused child |
|---|---|---|---|
| `delegate_work` child | parent `status='waiting'` + row `freeze_data.freeze_type='delegation'` (`completion.py:776-777`) | **Yes — the only kind scanned** by `_check_delegation_timeouts` (`main.py:11065-11212`) | Delegation timeout: default 7200s from `freeze_data.timeout`, elapsed measured from `freeze_data.timestamp` (agent-stamped, wall-clock, never rebased); on expiry cancels every child `NOT IN (completed,failed,cancelled)` — **`paused` is cancellable** (`postgres.py:1047-1057`) |
| pre-job scholar | parent `status='waiting'` with **no freeze_data at all** (`main.py:10800-10801`) — invisible to the delegation scan, **no timeout exists** | No | None directly; but see the sweep-fail strand (below) |
| post-job critic | parent `status='reviewing'` (`completion.py:766-768`), orchestrator-held; verdict via `_handle_critic_verdict_on_complete` (`main.py:11318`) | No | `cancel_stale_verification_subjobs` (`postgres.py:4177-4221`): cancels a `('created','paused')` agentless critic when parent terminal **or `updated_at` > 6h stale** — a cooldown pause >6h (< our 12h budget) leaves the row untouched → **cancelled mid-outage** |

**Loop topology (open question RESOLVED):** loop role jobs ("iter N · ROLE") are
**top-level** — `create_loop_job` (`project_loops.py:674-838`) passes no
`parent_job_id` and explicitly disables both scholar and verification subjobs
(`:725-735`). There is no loop-level critic subjob. The jobs that originally failed
are already covered by the shipped top-level feature.

## What reuses cleanly (confirmed by audit — the engine really is generic)

- **Per-subjob counters already exist.** `increment_job_llm_outage_attempt` and
  `evaluate_llm_outage` are row-scoped (`context.llm_outage` on the completing job's
  own row). The code comment at `completion.py:738-741` ("counters are
  top-level-scoped") is **stale** — it describes the wiring, not the storage.
- **The outage sweeper has no subjob filter.** `list_due_llm_outage_jobs` /
  `claim_llm_outage_redispatch` / `fail_llm_outage_job` (`postgres.py:4356-4443`)
  key on `status='paused'` + `freeze_type='llm_unavailable'` + `next_retry_at` — no
  `parent_job_id` anywhere.
- **The dispatch cascade guard admits it.** `get_dispatchable_jobs`
  (`postgres.py:4484-4501`) blocks only `paused/cancelled/failed` **ancestors** — a
  delegation/scholar parent is `waiting`, a critic parent is `reviewing`; neither
  blocks. (If the whole family pauses, the child waits for the parent — self-resolving.)
- **`/complete`'s llm pause block needs no change.** `main.py:13238-13310` keys off
  `new_status == "paused"` + freeze_type and is parent-agnostic; subjobs never reach it
  today only because `determine_job_status` routed them to `pending_review`.
- **Workspace reattach mostly works.** The dispatcher re-resolves inheritance every
  tick (`_resolve_subjob_inherited_workspace`, `main.py:3492-3637`, gated on the
  persisted `inherits_parent_workspace` flag — survives pause/resume). The parent pod
  stays warm through the pause: `waiting` parents aren't reapable at all
  (`workspace_manager.py:53-64`) and `reviewing` parents are shielded by
  `_live_shared_child_exists` (`:803-845`), which counts a `paused` child as live.
  Delegation children own their pods → suspend/restore like top-level. (Cost note: a
  12h pause pins the shared parent pod for 12h.)
- **The critic's parent-side machinery is already pause-tolerant.**
  `unstick_reviewing_parents` explicitly spares a `paused` critic
  (`postgres.py:4238-4240`).

**The entire gate is `determine_job_status` — flip it and the plumbing flows.** What
does NOT flow is the reaper interactions below.

## Industry survey — how mature orchestrators solve "paused child vs. parent reaper"

Surveyed 2026-07-16: Temporal, AWS Step Functions, Celery, Airflow, Oban, Kubernetes
Jobs, LangGraph (primary docs, claims spot-verified). Four recurring patterns:

- **A — Typed timeouts.** Separate the *active-work* clock (per-attempt; keep tight)
  from the *total-elapsed ceiling* (generous; breach is terminal and non-retryable —
  Airflow sensors: "Retrying does not reset the timeout"). Our analog: delegation
  timeout = active-work clock; `LLM_OUTAGE_CEILING_SECONDS` (12h) = the ceiling.
- **B — Liveness ≠ deadline.** Reapers key on *state + liveness*, never elapsed time
  alone. Celery is the counterexample proving the rule: its ETA-blind visibility
  timeout duplicate-executes healthy 12h waits by construction (documented footgun).
  Temporal and AWS are both explicit that heartbeats never extend a deadline.
- **C — Waiting is a first-class persisted state with an absolute next-wake timestamp,
  structurally invisible to reapers.** Oban snooze → `scheduled` (Lifeline rescuer
  only touches `executing`); Airflow `deferred`/`up_for_retry` carry absolute wake
  timestamps; Temporal's server owns the retry timer. Staleness is anchored on
  **next-wake, not last-activity**. *(This independently validates the shipped
  top-level design: our reset window anchors on `next_retry_at`, and our
  `status='paused'` + `freeze_type='llm_unavailable'` + `next_retry_at` is exactly
  this state.)* A wait costs at most one attempt (Temporal `NextRetryDelay`) — ours
  matches.
- **D — Deadline changes are explicit, persisted, absolute-timestamp transitions —
  nobody silently freezes an in-memory timer.** K8s Job suspend **resets**
  `.status.startTime` on resume (fresh `activeDeadlineSeconds` budget); Temporal's
  pause API deliberately does *not* touch Schedule-To-Close (extension must be an
  explicit `UpdateActivityOptions` call).

**Never-resumes hazard** (child pauses, never wakes → parent waits forever): guarded
everywhere by (1) a non-suspendable absolute ceiling, and/or (2) overdue detection on
the next-wake timestamp — `paused` past `resume_at + grace` is reclassified as stuck
and becomes reapable. Nobody relies on a suspended timer alone.

## Design (refined)

### Dependencies (tracked separately — user decision 2026-07-16)

Two delegation-freeze gaps found by the audit are filed as their own issue,
`docs/issues/delegation_freeze_lifecycle_gaps.md`, and **gate the delegate part of
this feature** (verify/fix before the delegation rebase, else delegate pause/resume
can't even E2E):

- **P0.1** resumed delegation parents may be dispatcher-invisible (`freeze_data`
  never cleared on `waiting → paused` requeue vs. `get_dispatchable_jobs`'s
  `freeze_data IS NULL` contract) — **CONFIRMED live on real PG and FIXED on
  develop 2026-07-16** (claim clears freeze; unblock handler re-queues via the CAS).
- **P0.2** the re-suspend freeze drops the `timeout` key (silently resets to
  7200s) — **FIXED 2026-07-16** (explicit `timeout` on the resume tool, capped).

### Core routing (shared by all kinds)

1. **`determine_job_status` subjob branch** (`completion.py:743-748`): for
   `freeze_type ∈ {llm_unavailable, memory_unavailable, kb_unavailable}`, apply the
   `_PARENT_TERMINAL_BLOCKING` guard (parent `failed`/`cancelled` → resolve terminally,
   mirroring `version_upgrade`), else **fall through to the existing type-specific
   branches** (`:794`, `:815`) — their retry caps, 12h ceiling, and deterministic-4xx
   fingerprint fail-fast are row-scoped and work per-subjob unchanged. Do NOT
   duplicate the logic in the subjob branch. Update the stale `:738-741` comment.
2. **Widen the coincident-error carve-out** (`completion.py:694-698`): `redispatchable`
   currently requires `parent_job_id is None`, so any subjob outage freeze riding a
   coincident error hard-fails (`:702-703`) before the subjob branch is reached. Allow
   subjobs when `parent_status not in _PARENT_TERMINAL_BLOCKING`.
3. **`/complete` llm pause block: no change** (verified parent-agnostic).
4. **Sweep-fail must unblock the parent.** `fail_llm_outage_job` is a direct DB UPDATE
   — **no `/complete` ever fires**, so no unblock handler runs. Today's consequences:
   scholar parent stranded in `waiting` **forever**; delegation parent rescued only by
   its (now possibly suspended) timeout; critic rescued by the unstick watchdog. Fix:
   in `_llm_outage_sweep_once`'s fail branch (`main.py:11250-11275`), when the failed
   row has `parent_job_id`, invoke the same unblock handlers `/complete` uses
   (`_handle_scholar_completion`, `_handle_delegation_child_completion`; critics need
   nothing — unstick covers them).
5. **Re-anchor the inherit wait budget.** `_INHERIT_WORKSPACE_MAX_WAIT_S` (600s,
   `main.py:3487`) is measured from the subjob's `created_at` (`:3620-3636`) — a
   subjob resuming 5h after spawn has zero budget: any transiently non-ready parent
   workspace (`creating`/`restoring`/`suspended`) at that instant → instant
   `_fail_subjob_and_unblock_parent` instead of waiting one tick. Anchor on the
   re-dispatch (e.g. `updated_at` or a stamped resume time) instead.

### Per-kind reaper fixes

6. **Delegates — make the delegation timeout pause-aware via re-anchoring (Pattern D). [LOCKED 2026-07-16: rebase, not credit]**
   In `_check_delegation_timeouts`, when any child is *legitimately outage-paused*,
   **rebase the parent's `freeze_data.timestamp = now`** (single `jsonb_set`) instead
   of evaluating the timeout. This suspends the clock AND prevents the fire-on-resume
   trap (a merely *skipped* check still has `elapsed > timeout` the moment the child
   resumes — the sweeper would kill the freshly-resumed child on the next tick).
   Semantics = K8s suspend (reset-to-full on resume): deliberately generous in the
   safe direction, no per-child interval bookkeeping, siblings' overlapping pauses
   can't double-count.
   - **Marker predicate:** child `status='paused'` AND `context.llm_outage.next_retry_at`
     in the future (+ grace). Do NOT key on the child's `freeze_data` — the redispatch
     CAS nulls it while the child is still `paused` awaiting pickup
     (`postgres.py:4400`); `context.llm_outage` survives.
   - **Overdue guard (never-resumes hazard):** if the child is `paused` but
     `next_retry_at + grace` is past (sweeper broken/stuck), do NOT rebase — let the
     timeout fire and cancel as today. The child's own 12h ceiling / 60-attempt
     backstop is the non-suspendable ceiling (Pattern A): when it trips,
     `fail_llm_outage_job` makes the child non-paused, rebasing stops, and the
     timeout resolves within one 60s tick.
   - On genuine expiry, cancelling paused children stays **explicit** (already the
     behavior — `paused` is in the cancel set): a dead parent never orphans a
     12h-sleeper.
7. **Critics — exempt legitimate outage pauses from the 6h staleness arm.** In
   `cancel_stale_verification_subjobs` (`postgres.py:4210-4214`), keep the
   parent-terminal arm untouched (correct: a subjob under a terminal parent can never
   proceed) and add to the staleness arm:
   `AND NOT (freeze/context marks llm-outage-paused AND next_retry_at + grace > now())`
   — same marker + overdue guard as #6, so an overdue paused critic is still reapable
   (Pattern C: reap `paused`+overdue, never `paused`+before-wake).
8. **Scholars — nothing to exempt** (no parent-side timeout exists). The parent's wait
   is bounded by the child's own 12h ceiling **provided #4 lands** (else ceiling-fail
   = stranded parent). **[LOCKED 2026-07-16: child ceiling only — no parent-side
   knob/sweeper]**, matching the no-separate-counter philosophy from
   `[[llm_cooldown_pause_and_resume]]`.

## Implementation map

| # | File | Change |
|---|---|---|
| P0.1 | (tracked in `docs/issues/delegation_freeze_lifecycle_gaps.md`) | verify/fix parent `freeze_data` clear on delegation requeue — **gates #6** |
| P0.2 | (tracked in `docs/issues/delegation_freeze_lifecycle_gaps.md`) | carry `timeout` into the re-suspend freeze — gates #6 |
| 1 | `orchestrator/services/completion.py:743-748` | outage freeze types: parent-terminal guard, then fall through to `:794`/`:815`; fix stale comment |
| 2 | `orchestrator/services/completion.py:694-698` | allow subjob `redispatchable` when parent not terminal |
| 4 | `orchestrator/main.py:11250-11275` | sweep-fail branch: run parent-unblock handlers for subjobs |
| 5 | `orchestrator/main.py:3620-3636` | re-anchor `_INHERIT_WORKSPACE_MAX_WAIT_S` on resume, not `created_at` |
| 6 | `orchestrator/main.py:11065-11212` | delegation timeout: rebase `freeze_data.timestamp` while a child is legitimately outage-paused; overdue guard |
| 7 | `orchestrator/database/postgres.py:4177-4221` | staleness arm: exempt legitimately outage-paused critics (marker + overdue guard) |

## Acceptance criteria

1. A **scholar/critic/delegate** subjob hitting a within-budget `model_cooldown` 429 →
   `paused` + `freeze_type=llm_unavailable`, agent freed — **not** `pending_review`.
2. The outage sweeper re-dispatches it; it **resumes from checkpoint**; `attempt`
   climbs on its *own* `context.llm_outage`; workspace reattach succeeds even hours
   after spawn (#5).
3. **The delegation timeout does not cancel** a legitimately paused child (3–5h
   cooldown, 2h timeout); the parent keeps `waiting` and unblocks on real completion.
   A child paused **past** `next_retry_at + grace` IS still cancelled (overdue guard).
4. The **stale-verification sweeper** spares a legitimately paused critic past 6h, but
   still reaps: paused critics of terminal parents, and overdue paused critics.
5. A paused subjob whose **parent goes terminal** resolves terminally — no silent
   paused wedge.
6. Over-budget cooldown / 12h ceiling / 60 attempts on a subjob **fails loudly AND
   unblocks the parent** (scholar parent must not strand in `waiting`; delegate
   parent gets a `failed` child result; critic parent reaches `pending_review` via
   unstick).
7. Both `cooldown` and transient `llm_unavailable` are covered; `memory_unavailable`/
   `kb_unavailable` subjob freezes route the same way.
8. A subjob outage freeze **riding a coincident error** still pauses (today it
   hard-fails before the subjob branch).

## Adjacent pre-existing bugs (found during the audit — FILED SEPARATELY, out of scope here)

Per user decision 2026-07-16, these are tracked as issue docs, not in this feature:

- **Critic `pending_review` wedge** → 2026-07-16 update appended to
  `docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md` (falls between
  both watchdog arms; wedges the parent silently regardless of cause).
- **Scholar false-success semantics** →
  `docs/issues/scholar_pending_review_silent_success.md`.
- **Delegation freeze lifecycle** (P0.1 dispatcher-invisibility, P0.2 dropped
  `timeout`, manual-cancel-skips-unblock) →
  `docs/issues/delegation_freeze_lifecycle_gaps.md` (P0.1/P0.2 gate #6 above).

## Decisions locked (user, 2026-07-16)

- **Timer policy = rebase** (#6): `freeze_data.timestamp = now` while a child is
  legitimately outage-paused; K8s-suspend semantics (fresh full window on resume);
  no pause-credit bookkeeping. Rejected: exact credit (most code, interval
  accounting), raising the timeout (weakens stuck-detection for all delegations).
- **Scholar bound = child ceiling only** (#8): no parent-side ceiling knob; the
  child's 12h/60-attempt ceiling + the #4 sweep-fail unblock bound the wait.
- **Adjacent bugs out of scope**: filed as separate issue docs (see above); this
  feature stays focused. P0.1/P0.2 remain hard *dependencies* for #6.
- **Implementation = one shot**: all items (#1-#8) in a single TDD pass, one deploy,
  one k3d E2E — no slicing. (P0.1 verification happens first within that pass since
  it gates #6.)

## Remaining follow-ups (non-blocking)

- **Parent visibility** — surface "child paused for cooldown until ~T" on the waiting
  parent vs. generic `waiting` (Cockpit follow-up).
- **Telemetry label** — industry treats a declared wait as success-like (Oban
  `snoozed`), not failure. Our pause already costs one attempt per window (Temporal
  `NextRetryDelay` model) — fine; consider a distinct label so dashboards don't count
  cooldown pauses as failures.

## Implementation notes (2026-07-16)

Implemented one-shot per the locked decision, TDD throughout (7 commits on
develop, unpushed; SHAs churn pre-push — don't cite hashes). Unit coverage:
~40 new tests across `test_llm_outage_resilience` (subjob routing),
`test_llm_outage_sweeper` (sweep-fail unblock), `test_delegation_timeout_outage`
(timer anchor), `test_delegation_resume_claim` + `test_per_job_repo` (P0.1 CAS +
handler), `test_stale_verification_outage_exemption` (real-PG staleness SQL),
`test_subjob_inherited_workspace` (inherit re-anchor), `test_delegation` (P0.2
timeout carry); 435 tests green across all touched suites.

**Deviation from the sketch (#6):** the rebase is implemented as a **derived
anchor** — effective start = `max(freeze.timestamp, latest child
llm_outage.next_retry_at)`, computed at evaluation time — rather than a
persisted `jsonb_set` rebase. Same locked K8s reset-on-resume semantics
(paused child's future wake parks the timer; resumed child gets a full window
from its wake; never-resuming child terminates at wake + timeout), but with
zero writes, no dual-leader write races, and no fire-on-resume window (a
write-side rebase at fire time still cancels a child that resumes just before
the rebased deadline). Children are only fetched once the naive timer expires.

**k3d E2E (deployed images + real PG, synthetic `model_cooldown` 429 stub
pinned via `config_override.llm.base_url`):**

- **A. Scholar pause→resume:** scholar → `paused`/`llm_unavailable`/
  `cooldown`/attempt=1 (NOT `pending_review`), outage sweeper reclaimed +
  re-dispatched it, resumed on an agent, re-paused attempt=2 — and the
  **parent stayed `waiting` throughout** (acceptance #1, #2).
- **Live-caught integration bug, fixed during the gate:** the first A run
  showed the parent flip `waiting → created` the moment the scholar paused —
  `_handle_scholar_completion` runs on every `/complete` (the pause path sets
  `job["status"]="paused"` in-memory first) and treated non-failed as
  research-success. Fixed with a non-terminal guard (+ unit test); the critic
  verdict handler was audited and is safe (no-verdict + not-completed → no-op);
  delegation unblock was already safe (`all_delegation_children_terminal`).
- **B. Ceiling-fail unblocks the parent:** scholar's `first_failed_at` pushed
  13h back → sweeper backstop failed it loudly ("past the give-up ceiling
  (duration…) — failed by the outage sweeper") and the parent was unblocked
  with `scholar_failed=true` — no stranded `waiting` parent (acceptance #6).
- **C. Delegation timer:** synthetic parent (freeze ts −3h, timeout 2h) +
  paused child with wake +2h → deployed sweeper logged "suspended: child
  outage wake … re-anchors the deadline (−7172s of 7200s consumed)" each tick,
  child NOT cancelled; wake flipped to −3h (overdue) → next tick cancelled the
  child and re-queued the parent `paused` with **`freeze_data` cleared (P0.1
  round-trip)** + `delegation_timed_out` + results (acceptance #3 + overdue
  guard).
- **D. Critic staleness exemption:** 7h-stale paused critic with wake +2h
  **survived** the 6h reap; 9h-stale critic with wake −2h was **cancelled**
  (acceptance #4, both arms).

Not exercised live: resume-to-COMPLETION (no real model on k3d — same limit as
the top-level feature) and an agent-driven `delegate_work` round (no real model
to call the tool; C exercised the deployed sweeper on synthetic rows).
Acceptance #5/#7/#8 (parent-terminal resolve, memory/kb types, coincident
error) are unit-covered.

## Relationship to prior work

- **Closes the deferred subjob follow-up** in `[[llm_outage_pause_and_backoff_redispatch]]`
  and `[[llm_cooldown_pause_and_resume]]` (whose `next_retry_at` staleness anchor the
  industry survey independently validates — Pattern C).
- **Reuses** the top-level pause engine wholesale; the net-new surface is
  reaper-awareness (delegation timeout, verification staleness) + parent-unblock on
  sweep-fail + the inherit-budget re-anchor.

## Rough scope

**Medium.** Core routing (#1-#3) is small and mechanical. The real work: the
delegation-timeout rebase + overdue guard (#6), the sweep-fail unblock (#4), the
inherit-budget re-anchor (#5), and P0.1 verification — each individually small but
each needs its own failure-mode tests (fire-on-resume, never-resumes, parent-terminal
races). Sequence **after** the top-level feature is trusted in prod.
**Locked 2026-07-16: one-shot implementation** (all items in one TDD pass + one k3d
E2E), with P0.1 verified first inside the pass since it gates #6.
