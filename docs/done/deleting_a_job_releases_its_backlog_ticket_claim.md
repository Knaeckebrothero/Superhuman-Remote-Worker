---
tags:
  - issue
  - officers
  - backlog
  - jobs
  - data-integrity
status: resolved
priority: P1
created: 2026-08-15
aliases:
  - BP-05
  - claim deletion re-arms ticket
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_backlog_pools]]"
---

# Deleting a job silently releases its one-shot backlog claim

**Status:** DONE LOCALLY 2026-08-16 — repaired after the first main-dev gate proved that
migration 0162's strict historical preflight rejects the genuine pre-ledger job shapes.
Audit finding **BP-05**. The corrected migration is not deployed, the failed live gate
must be rerun, and this does not enable `auto_pull`.

## Problem and semantic

Before this fix, `PostgresDB.newest_ticket_claims` reconstructed claims solely from extant
`jobs` rows, so authorized or retention deletion silently made a still-ready ticket
eligible again.

The implemented semantic is:

- A project-scoped durable claim outlives the operational job row. Terminal,
  non-terminal, API and retention deletion never release or erase it.
- Only a **newer server-observed Officer `ready_at` generation** re-arms the ticket. Equal
  or older generations remain consumed. A new generation is still refused while preceding
  work is non-terminal; cancel or finish it first.
- Pre-ledger jobs whose ready generation cannot be proven become
  `legacy_unversioned` claims at the migration cutover. Their `ready_generation_at` stays
  NULL; the ledger's server timestamp is a re-arm barrier, not a guessed historical
  generation. They remain consumed until the Officer explicitly re-readies the ticket
  after cutover, and a non-terminal predecessor still blocks that newer generation.
- `ticket=` means “claim this ready backlog ticket.” Ad-hoc work omits it. Manual and tick
  dispatch use the same post-locked claim/slot/job transaction.
- The caller/model supplies only the ticket slug and optional slot. It never supplies
  ready generation, claim source, project, Officer identity/incarnation or admission
  provenance.

## Schema and authoritative transaction

App migration `0162_officer_ticket_claims.sql` creates `officer_ticket_claims`. A normal
row records project/ticket/generation, claim timestamp/source, Officer
thread/incarnation/slot/category/config-fingerprint/lineage provenance, and durable job
identity without a jobs FK. A `legacy_unversioned` row deliberately records only observed
project/ticket/job/thread/slot/category data: generation and cryptographic admission
provenance are NULL because inventing either would turn migration code into authority.
Deletion audit retains timestamp, observed status, and the actor/reason when the deleting
application has that authority.

Unique backstops on `(project_id, ticket_note_id, ready_generation_at)` and `job_id`
prevent two claims for one generation or two claims for one job. The existing partial
unique non-terminal jobs index remains defence in depth. A dedicated
`(officer_thread_id, officer_slot, claimed_at DESC)` partial index supports permanent
lineage/optional-slot history consumers; a real-PostgreSQL `EXPLAIN` proof selects it.

Final admission keeps the established runtime order:
`project_officers` post → current thread → claim/new job. After external preparation, one
connection and transaction:

1. locks and revalidates the exact live post incarnation/configuration;
2. counts full-lineage non-terminal capacity and selects the authoritative slot;
3. validates the trusted ready generation and preceding work;
4. preallocates the job UUID and inserts the durable claim;
5. stamps authoritative slot/category/config/incarnation provenance;
6. inserts that exact job UUID on the same connection; and
7. commits both, or rolls both back on any fault.

Manual `create_job(ticket=...)` resolves the exact project-scoped knowledge row and its
database-owned `ready_at`. Missing, inactive/not-ready, non-ticket, ambiguous,
project-mismatched or unstamped state fails closed. Tick passes the same authoritative
generation. No generation field is model-selectable.

## Rolling-upgrade boundary and backfill

The independent review correctly found that application-only writes could not govern an
older replica after the migration committed. Migration 0162 now takes
`SHARE ROW EXCLUSIVE` on `jobs` before creating/backfilling the ledger and holds it through
trigger installation:

- a job writer committed before the lock is visible to strict backfill;
- a writer after migration commit sees the integrity/audit triggers; and
- a writer actually crossing the boundary is either backfilled or rejected—never accepted
  without a claim.

The migration does not lock posts or threads, so it cannot invert runtime admission's
order. If the two-second lock budget is unavailable, startup retries instead of exposing a
partial boundary.

The deferred-capable, initially-immediate jobs integrity trigger requires every
ticket-bearing INSERT/update to match a durable claim already visible in its transaction.
New admission inserts the claim first. An old replica may continue ordinary creation, but
its ticket dispatch is explicitly rejected until it rolls. A `BEFORE DELETE` trigger
records status/time for every claimed-job deletion, preserving richer actor/reason fields
already set by the current application. Old terminal deletion therefore does not become
`deleted_unknown`; old non-terminal deletion remains a truthful blocker.

The follow-up review found two subtler trigger defects. SQL `<>` comparisons allowed a
missing JSON value to pass through three-valued logic, and removing the top-level ticket
key bypassed the old early return. The trigger now looks up the ledger first, rejects
removal from any still-live claimed job, and uses explicit presence/type/range checks plus
`IS DISTINCT FROM` for all authority fields. Backfilled claims deliberately keep
`source=backfill` in the ledger while accepting the genuine pre-0162 admission shape,
which had no model-visible `ticket_claim_source`; unrelated later context merges therefore
continue to work without weakening provenance immutability.

Backfill has two intentionally different outcomes:

- A complete, internally consistent pre-cutover Officer stamp becomes a versioned
  `backfill` claim after exact project/thread, current-or-historical incarnation, lineage,
  slot/category, fingerprint and finite `ticket_ready_at` validation.
- Every project-scoped, nonblank ticket job that cannot meet that proof becomes a
  `legacy_unversioned` claim. It receives no generation and no inferred
  incarnation/fingerprint. Its `claimed_at` is the database cutover timestamp and is used
  only as a fail-closed re-arm barrier. A trusted `ready_at` must be strictly later.

The migration never trusts a model timestamp or guesses from `job.created_at`. It still
aborts rows that cannot be scoped at all (NULL project or blank ticket), because no safe
project/ticket barrier can represent them.

Idempotency is specifically `ON CONFLICT (job_id) DO NOTHING`. A collision between two
fully verified rows on `(project_id, ticket_note_id, ready_generation_at)` stays loud; the
migration reports the conflicting historical job IDs and statuses rather than choosing
one. Multiple unversioned rows may coexist because NULL is not a fabricated shared
generation; all of them participate in non-terminal blocking and share the cutover
barrier.

## Server-owned context and deletion truth

Public REST, internal/session creation, the unified job tool and the final
`PostgresDB.create_job()` funnel strip `ticket_note_id`, `officer_admission` and related
generation/source/identity keys from raw context. Only final post-locked admission opts
into preserving its replacement authoritative stamp, and only on its caller-owned
transaction. Explicit slot selection remains supported and is revalidated.

`PostgresDB.delete_job()` records its claim audit and deletes the job in one transaction;
the database trigger covers old/direct deletion. A missing job with missing audit now
means pre-trigger/bypassed corruption and remains fail-closed. REST reports
`ticket_claim_retained=true` only when the deleted job actually has a durable claim;
ordinary deletion reports false. That truth is returned by the deletion transaction
itself—REST performs no fallible second lookup after commit—so the endpoint cannot report
500 after having irreversibly succeeded. `ticket_rearmed` remains false in both cases.

## Acceptance evidence

The repaired real PostgreSQL 15 coverage in
`tests/test_officer_post_transactions_real_postgres.py` proves:

- manual/manual and manual/tick contention create one job and one claim;
- a fault after claim INSERT but before job INSERT rolls both back;
- terminal/non-terminal application and old-writer DELETEs retain correct audit semantics;
- equal/older generations stay consumed, one newer generation wins after terminal work,
  and live/deleted-non-terminal predecessors block newer work;
- claims remain project-scoped across decommission/recommission;
- a pre-migration job and a writer committed immediately before the migration lock are
  backfilled, while an old ticket INSERT after migration is atomically rejected;
- strict backfill replay is idempotent by job ID; a same-generation pair of fully verified
  rows fails with actionable diagnostics;
- field-realistic stamp-less and partial-stamp rows become unversioned barriers without a
  guessed generation, remain consumed at equal/older `ready_at`, and re-arm exactly once
  only after a post-cutover trusted generation;
- incomplete or malformed historical admission is quarantined rather than promoted, while
  new/versioned claims still reject missing identity; a claimed job cannot remove its
  ticket stamp;
- historical backfilled jobs still accept unrelated atomic context merges without being
  forced to acquire provenance that the old application never wrote;
- public and internal endpoints cannot persist raw claim context against real PostgreSQL;
  the database funnel and direct-writer trigger are additional backstops;
- legitimate manual and tick admission still pass the integrity trigger;
- claimed and ordinary API deletion return true and false respectively without a
  post-commit claim query; and
- the lineage/slot consumer has a supporting index plan.

Verification on 2026-08-16:

```text
earlier Officer/admission/routing/deletion set:  662 passed in 239.96s
earlier real Officer Post PostgreSQL file:        53 passed in 117.42s
follow-up malformed/backfill/delete cases:        13 passed in 40.03s
follow-up complete Officer PostgreSQL file:       64 passed in 137.24s
follow-up broader Officer/API/tool checkpoint:   636 passed in 262.65s
follow-up deletion collaborator checkpoint:      150 passed in 0.73s
migration/head tests:                             34 passed in 28.82s
schema replay/drift:                              OK; all three artifacts current
Cockpit job-list (earlier checkpoint):            19 passed
Cockpit i18n (earlier checkpoint):                2530-key parity; no hardcoded copy
ruff check / format check / git diff --check:     clean
```

Local field-history repair checkpoint on 2026-08-16:

```text
backlog eligibility/admission logic:               53 passed in 0.16s
legacy migration/race subset:                      13 passed in 40.25s
complete Officer Post PostgreSQL file:             66 passed in 141.84s
expanded lifecycle/routing/admission checkpoint:  547 passed in 265.84s
migration discovery/head/replay tests:             34 passed in 31.69s
app migration replay + schema regeneration:        OK (136 transactional migrations)
all app/vector/audit schema artifacts:              current
Ruff check / format check / git diff --check:       clean
```

The repair fixture reproduces the observed main-dev population—six jobs with no
`officer_admission` and one partial stamp without `ticket_ready_at`—instead of calling a
fully versioned automatic-tick stamp “historical.” All seven become unversioned barriers
with one server cutover timestamp and no inferred generation. Equal cutover time is
consumed; a one-microsecond-newer trusted generation wins exactly once after terminal
work. A directly deleted non-terminal legacy job remains blocked.

The earlier checkpoint's repository fast suite reached **14,783 passed / 123 skipped**
before its system-Python environment stopped on missing `arxiv`; the exact file passed
under the project virtualenv (**22 passed**). The review repair used the proportionate
662-test Officer set rather than repeating that environment-limited full run.

The dedicated main-dev post-deployment runbook is
[[officer_ticket_claim_ledger_live_gate_2026-08-16]]. It validates the deployed migration,
ledger completeness, rolled-back historical-context compatibility, one-shot deletion and
re-ready behavior, an ordinary-job control, and complete disposable cleanup while keeping
`auto_pull=false`.

This closes the local BP-05 repair only. Its main-dev deployment checkpoint remains open.
BP-06 subsequently closed locally in
[[backlog_fixed_windows_starve_eligible_tickets]]; BP-01, BP-07, BP-08, BP-11, ES-01 and
the remaining OC-05/OC-06 residues are unchanged. `auto_pull` remains false and unexposed.

## Dependencies

Coordinate schema/admission writes with
[[officer_admission_does_not_lock_the_durable_post]].
