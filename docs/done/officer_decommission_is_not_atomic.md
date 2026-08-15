---
tags:
  - issue
  - officers
  - lifecycle
  - database
  - liveness
status: done
priority: P0
created: 2026-08-15
completed: 2026-08-15
aliases:
  - OC-03
  - split officer decommission
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_post]]"
  - "[[officer_message_routing]]"
---

# Officer decommission is a chain of independent transactions

**Status:** DONE 2026-08-15 — lifecycle/data-integrity finding **OC-03** was
reopened after its first transaction checkpoint and is now closed by the additional
post-locked race coverage below.

The first transaction checkpoint proved rollback-or-complete-handoff for the original
database substeps, but follow-up review found five uncovered authority seams. The final
checkpoint closes all five with real-PostgreSQL tests:

- the no-force in-flight decision must run under the same post lock as admission and count
  every non-terminal job across the full incarnation lineage;
- direct End of a legacy/orphan officer must make one post-locked decision and must never
  harvest or alter the commissioned incarnation;
- commission continuity restore/drain/wake creation must remain fenced to the exact
  incarnation through the post-locked boundary;
- completion routing must decide under the post lock between the exact current wake queue
  and the vacant continuity ledger, exactly once; and
- the commission-only post configuration write must use a vacancy/generation CAS so a
  losing commission cannot patch the winner.

The O6 Resavio live-fire was already released successfully with `auto_pull=false` and was
in progress before this local checkpoint. That committed history is not superseded: the
earlier tranche is deployed, while the transaction changes described here are uncommitted
and not deployed.

**Read-surface half fixed 2026-08-15** (pre-live-fire): `get_project_officer_summary`
now derives `commissioned` from the live post join (`get_officer_thread_for_project`,
which already filters ended threads) instead of link non-nullness, so a stale
ended-thread link renders as ordinary vacancy — never `commissioned: true` over an
empty officer block. Regression pinned in
`tests/test_officer_conference.py::test_stale_ended_thread_link_reads_as_vacant`.
The commission endpoint already used the live join for its already-commissioned
guard, so a stale link does not block recommissioning. The atomicity half is now closed by
the resolution below.

## Problem

`orchestrator/main.py::_decommission_officer_post` performs state harvest, wake-queue fold,
blocking-route drain, post unlink, and incarnation append through separate database calls.
`_stand_down` catches and swallows failure of that hygiene chain. A direct thread end and
the explicit decommission endpoint therefore share a funnel, but not one atomic state
transition.

The read surface has a related truth bug: `get_project_officer_summary` derives
`commissioned` from a non-null post link before proving that the linked thread is live. A
stale ended-thread link can return `commissioned: true` and an empty officer block.

## Impact

A crash can vacate the post while losing harvested state or incarnation history, leave
pending routes only partly drained, or delete wake intent without folding it into the
vacant ledger. The next commission then starts from a history that claims a cleaner handoff
than actually occurred.

## Required direction

- Add one Postgres lifecycle method that locks the `project_officers` row and validates the
  expected current thread/incarnation.
- In the same transaction: harvest state, fold/clear wake rows, transition pending routes
  to their durable fallback/outbox state, append exactly one incarnation, and clear the
  post link.
- Keep external notification delivery outside the transaction, driven by durable outbox
  intent written inside it.
- Make thread end/decommission report an incomplete authoritative transition; do not swallow
  it as non-fatal after ending the thread.
- Compute `commissioned` from a valid live post join, not link non-nullness.

## Acceptance

- Inject failure after every decommission substep. The transaction either rolls back with
  the same commissioned officer or commits one complete vacant-post handoff.
- Repeating decommission is idempotent: one incarnation entry, one route fallback, no lost
  or duplicated wake entries.
- Concurrent decommission/recommission cannot install the new incarnation before the old
  handoff commits.
- Direct DELETE/end and the explicit endpoint have identical authoritative behavior.
- A stale ended-thread link is repaired/read as vacant and never returns
  `commissioned: true`.
- The no-force in-flight decision and admission serialize on the same post row, count the
  full incarnation lineage, and cannot both succeed.
- Direct End of an enabled orphan retires only that orphan under the post lock, whether
  the post is occupied or a concurrent registration is waiting on a vacancy.
- Commission state restore, vacant-ledger drain, continuity wake creation and final result
  validation are fenced to the exact candidate incarnation.
- A completion racing commission appears exactly once: in either the drained commission
  brief or the exact current incarnation's wake queue.
- A losing commission cannot change the winning officer's post or thread configuration.

## Dependencies

Use the same stable post/project lock chosen by
[[officer_admission_does_not_lock_the_durable_post]].

## Resolution

`PostgresDB.decommission_project_officer()` is now the single authoritative
commissioned-to-vacant transition. In one PostgreSQL transaction it locks the durable
post and expected current thread, counts all statuses other than
`completed`/`failed`/`cancelled` across the complete incarnation lineage for the no-force
gate, harvests authoritative thread state, locks/folds/clears applicable wake rows, stages
all pending blocking officer routes as durable
`escalated_to_user` fallback intent, appends at most one incarnation, unlinks the post,
and performs the server-owned disable/end write. The stable lock prefix is shared with
admission, registration, hold/release and post config changes. Because admission holds the
same row through job INSERT, either admission commits first and no-force decommission
returns the in-flight warning with the post intact, or decommission commits first and the
prepared admission is rejected.

External notification delivery runs only after commit from the returned durable route
rows. Failure leaves `user_delivery_at` null for the existing reconciler and cannot roll
back or falsify the vacant post. Blocking route creation also validates the current
post/incarnation under this same lock prefix, preventing a stale route/freeze from landing
after the handoff drained routes and committed.

Both direct thread End and the explicit decommission endpoint call the same helper. The
End funnel no longer disables first or swallows an authoritative handoff failure; a failed
post transition is visible and retryable, while external workspace/resource cleanup keeps
its existing best-effort boundary. A vacant retry is an idempotent success, and stale
ended or missing joined threads read as vacant rather than `commissioned: true`. Direct
End passes an explicit orphan-retirement mode: under the post lock, a target that is not
the current holder is disabled/ended atomically without harvesting state, appending
history, draining wakes/routes, unlinking, or touching a commissioned successor.

Commission continuity now joins registration's authoritative transaction. After the
post links the exact candidate, that same transaction restores harvested state into the
candidate, drains the while-vacant ledger and creates its deduplicated commission wake.
An immediate decommission refolds an undelivered commission brief before clearing wakes,
so continuity is not consumed by a lifecycle race. The endpoint performs a final
post-locked exact-incarnation confirmation before reporting success. The preceding
commission-only configuration update is itself vacancy- and `updated_at`-generation
fenced, so a loser cannot patch the winner.

Job-completion routing no longer performs an unlocked "find officer, else append"
sequence. `route_project_officer_job_transition()` locks the post and decides once, in one
database transaction, whether to enqueue for the exact current live incarnation or append
to the vacant ledger. Its connection-aware shape leaves the decision composable with
future durable claim work without adding another transaction funnel.

## Acceptance evidence

`tests/test_officer_post_transactions_real_postgres.py` injects a failure after every
named database substep (`post_locked`, `thread_locked`, `in_flight_checked`, `state_harvested`,
`wake_rows_locked`, `wake_entries_folded`, `wakes_cleared`,
`routes_fallback_staged`, `incarnation_appended`, `post_unlinked`, and
`thread_disabled`). Each case rolls back to the same commissioned thread, original state,
pending routes, untouched wake rows and empty incarnation history.

The same real-PostgreSQL suite proves repeated decommission produces one incarnation, one
route transition and one folded ledger entry; recommission waits for predecessor handoff
commit; durable fallback remains unstamped until external acceptance; and a stale
blocking-route snapshot cannot freeze a job after decommission. It also proves both sides
of the admission/no-force race, every non-terminal status across old/new lineage, occupied
and vacant/concurrent-registration orphan End, commission/decommission continuity,
completion/commission exactly-once routing, and the losing-commission configuration CAS.
Unit tests prove direct End/explicit endpoint equivalence, visible failure semantics,
stale ended/missing summary truth, and retryable notifier failure after a committed
handoff.

Verification at the completed checkpoint:

```bash
python -m pytest \
  tests/test_officer_post_transactions_real_postgres.py \
  tests/test_officer_message_routing_real_postgres.py -q --tb=short
# 48 passed in 115.55s

python -m pytest \
  tests/test_officer_lifecycle.py tests/test_officer_post.py \
  tests/test_officer_backlog_tick.py tests/test_officer_slots.py \
  tests/test_officer_message_routing.py \
  tests/test_officer_message_routing_real_postgres.py \
  tests/test_backlog_ticket_plumbing.py \
  tests/test_runtime_actor_authorization.py \
  tests/test_stateless_worker_control.py \
  tests/test_officer_post_transactions_real_postgres.py \
  tests/test_officer_conference.py tests/test_session_wake_linkage.py \
  -q --tb=short
# 461 passed in 199.47s

ruff check src/ orchestrator/ tests/
# All checks passed!
ruff format --check src/ orchestrator/ tests/
# 1201 files already formatted
git diff --check
# clean
```

The repository fast suite was also attempted with its default system interpreter:
`./scripts/pytest-fast.sh` reached **14,772 passed and 123 skipped** before its fail-fast
boundary stopped on
`tests/tools/research/test_arxiv_client.py::test_installed_arxiv_package_exposes_client_results`.
`/usr/bin/python -c "import arxiv"` reproduces `ModuleNotFoundError`, while the same file
under the project virtualenv passes (**22 passed in 0.12s**). This is a proved local
dependency/interpreter distinction, not an Officer checkpoint failure.

No schema migration was required. O6 had already released the Resavio officer successfully
with `auto_pull=false`, and that live-fire remained in progress on the earlier deployed
tranche. This transaction checkpoint is local, uncommitted and not deployed; it neither
turns on `auto_pull` nor authorizes unattended backlog work, and it does not close the
remaining OC-05/OC-06 residues.
