# Step-5 hand-check finding — non-terminal report wedges a stateless job

**Found live 2026-08-13 by the nominated hand-check** (job `a61d9940-e72a`,
command `2b028d0c`, k3d). **Status: CLOSED 2026-08-14.** The accept-side guard
landed same day (`CompletionNonTerminalReport`, 422 via the
`CompletionPayloadMismatch` mapping — no `main.py` change needed); both
follow-ups below were then closed by the wedge-follow-ups run and are retained
here as the record of what was wrong and why. Current behaviour lives in
`stateless_agents.md` §9.1 "Worker wedge follow-ups"; the build detail is in
`implementation_log.md` Session 22.

- **Follow-up 1 (driver source) — CLOSED.** Root cause was a second LangGraph
  state update consuming the pending successor task, so the graph ran no
  successor and returned the armed `should_stop=false` state. Routing and
  arming are now one durable update. The on-422 driver contract was defined
  with it: never-accepted, no hold, shell preserved, fence-release with linear
  backoff, successor claims under token N+1.
- **Follow-up 2 (rescue route) — CLOSED.** The ownerless shape now parks to
  `pending_review` with the `stateless_terminal_queue_unowned` marker plus an
  operator alert, never re-enqueues, and is covered by the ownership-invariant
  census.
- **The preserved specimen has been consumed** — it was the fixture that
  verified the rescue route: parked once, one snapshot, one UID-fenced
  workspace release, public delete 200. Its terminal effect rows and
  append-only audit records remain as history. Nothing below is live state.

## What happened

A stateless worker job (review autonomy, MiniMax) ran ~35 minutes, then its
executor POSTed `/complete` with `should_stop=false, goal_achieved=false,
freeze_type=∅` — a **continue-shaped report**. The accept honored the lease
fence and **B4-terminalized the run_queue unit** (`done`) as it does for
every stateless report; the finalizer then correctly concluded "not a stop"
(`outcome.new_status='processing'`, no disposition effects). Net state:
queue row done (nothing can claim), no freeze, `jobs.status='processing'`
forever, shell/workspace held alive by the M1 hold (nonterminal outcome ⇒
preserve). The job is permanently wedged.

## Why every net missed it

- The M5 ownership invariant inspects jobs with an UNFINISHED command —
  this command is `done`.
- The decision-(6) exclusion view covers `pending|finalizing|parked` —
  not `done`.
- `recover_orphaned_jobs` keys on an assigned agent; stateless jobs have
  none.
- `recover_expired_lease_jobs` keys on the jobs-table lease; the lane is
  partitioned out of it.
- The day-one safety net looks for stranded commands, not for a *done*
  command under a non-terminal job.

§5.4.5 decision (6) said "a **terminal-report** accept runs
`complete_unit`" — the implementation dropped the load-bearing word and
keyed only on the lane.

## Fixed now (accept side)

The stateless accept arm rejects any payload without a truthy
`should_stop` **before any write** (fail-closed on absent too). Pinned is
untouched — the loop-continue report is a real pinned path. Real-PG test:
`test_stateless_accept_rejects_non_terminal_report_without_any_mutation`
(lease untouched, zero command rows, hwm unmoved).

## Follow-up 1 (CLOSED 2026-08-14) — driver side (why was it sent at all?)

Per the scope correction and driver brief §6b, rotations release through
the queue and recoverable-error stops never call `/complete`. Something in
the step-5 M1/M2 worker terminal path produced a continue-shaped report.
Find the emitting path in `src/agent.py`/`src/api/turn_executor.py` (the
batch wall-clock floor is 60 s on k3d — the rotation boundary is a prime
suspect), and guard it at the source: a driver that has nothing terminal
to say must not report. With the accept guard the failure is now loud
(422) instead of a wedge — but the 422 leaves the driver holding a leased
unit it thinks it finished; define what the driver does on this 422
(resume the batch? release with backoff?) rather than letting it park in
the hold.

## Follow-up 2 (CLOSED 2026-08-14) — rescue route for the wedged shape

Add the missing net: a stateless-lane job in a non-terminal status whose
queue row is terminal (`done`/absent) with **no unfinished command** is
owned by nobody — route it (re-enqueue or park+alert), and extend the M5
invariant census to include this shape so CI would have caught it.

## Preserved specimen — CONSUMED 2026-08-14 (historical)

Job `a61d9940-e72a-454d-a8d4-1b9f9e38a826`, command `2b028d0c-…`, its
workspace pod (held alive by design) and queue row on k3d are the live
reproduction for both follow-ups. Retire them only after follow-up 1's fix
is verified against them (the rescue route from follow-up 2 is allowed to
be what finally moves the job).
