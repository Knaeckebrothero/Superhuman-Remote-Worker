---
tags:
  - issue
  - audit
  - officers
  - orchestration
  - security
  - liveness
  - backlog
status: open-release-blocker
created: 2026-08-15
aliases:
  - officer control-plane audit
  - officer backlog release blockers
  - post-implementation officer audit
related:
  - "[[officer_post]]"
  - "[[officer_knowledge_plane]]"
  - "[[officer_supervision_surface]]"
  - "[[officer_message_routing]]"
  - "[[officer_backlog_pools]]"
  - "[[unified_orchestrator_tool_surface]]"
---

# Officer control-plane post-implementation audit — authority, atomicity, and liveness gaps

## Status and verdict

**OPEN — release blocker for unattended `auto_pull` (2026-08-15). Keep it off.**

This is the durable follow-up to the post-implementation review of the complete officer
stack: Officer Post O1–O5, Knowledge Plane K1–K3, Supervision E1–E5/S5, Message Routing
M1–M4, and Backlog Pools B1–B6. The implementation was reviewed across its concurrent
rebases; the current equivalent stack is B1–B6 `6237e629` through `0002535d`, followed by
B7 `fa9fe159` and the independently committed schema repair `ae736584`. The earlier review
findings were rechecked against the resulting code rather than copied forward from design
notes.

The result remains mixed, but the authority/atomicity baseline has materially advanced:

- The six pre-deployment findings (runtime-derived actor identity, machine-tag authority,
  direct blocking route creation, route-generation CAS, initial sanitization, and truthful
  retryable notification outcome) are closed.
- BP-02/BP-03/BP-04 and OC-03 are closed: manual and tick admission share one
  durable-post transaction, tick enumeration starts from commissioned posts, and the
  adjacent no-force, orphan-End, continuity, completion-routing and commission-CAS
  decisions now use the same post-locked authority boundary.
- Real-PostgreSQL tests now exercise final-slot and same-ticket contention, lifecycle/config
  races, rollback after every decommission substep, idempotency, route fallback, and
  decommission/recommission serialization. These are no longer inferred from AsyncMocks.
- BP-05 is closed locally after independent-review repair. Migration 0162 now governs old
  replicas with an atomic jobs-table cut, strict/collision-loud backfill and jobs triggers;
  raw claim context is server-owned, null/missing provenance fails closed, historical
  context merges remain compatible, and deletion truth comes from the delete transaction.
- Unattended backlog release is still blocked by the supported enable-control gap followed
  by the still-open roster, evidence, authorization/liveness-policy, and operational
  residues listed below. BP-06's pre-filter starvation is closed. BP-07 and BP-10 now
  close the provisioning and floor-wake seams on main dev. BP-08's fail-closed write
  boundary passed live. BP-13's REST retry/projection repair is green locally and awaits
  its bounded main-dev rerun.

“Implemented” in the feature docs therefore still does not mean unattended backlog release
is safe. The earlier tranche was deployed and O6 was released successfully with
`auto_pull=false`; the later lifecycle/configuration transaction checkpoint was also
deployed and passed a bounded disposable Officer gate on 2026-08-16. BP-05, BP-06, and
LF-5 are prior closed/deployed checkpoints. **BP-07/BP-08/BP-10 were deployed on
2026-08-17; their bounded main-dev gate passed BP-07/BP-10 and failed BP-08 on the new
BP-13 residue.** Nothing here authorizes `auto_pull=true` or
unattended backlog release. The
umbrella stays open until the remaining P0 live gates in
[Release order and acceptance](#release-order-and-acceptance) pass.

## Individual issue ledger

The audit remains the cross-feature explanation. Implementation work should start from one
issue below and close only that issue's acceptance contract. The order preserves the
dependency chain; it is not a claim that every item in one priority must ship in the same
change.

**Pre-deployment tranche (orders 1, 2, 5, 6, 7, 10), BP-02/BP-03/BP-04 and OC-03
closed 2026-08-15; deployed disposable gate passed 2026-08-16.** The earlier tranche
reached dev and O6 was released successfully with `auto_pull=false`; the additional
transaction/configuration checkpoint later reached dev and passed the bounded gate recorded
in [[officer_backlog_pools_resavio_livefire]]. The BP-07/BP-08/BP-10 deployment gate is
recorded in [[officer_correctness_live_gate_2026-08-17]].

| Order | Priority | Issue | Audit finding(s) | Why this boundary |
|---:|---|---|---|---|
| 1 | P0 | [[officer_message_actions_trust_shared_transport_identity]] | OC-02 | Establish actor identity before trusting any officer mutation.  **DONE 2026-08-15.**|
| 2 | P0 | [[backlog_machine_tags_trust_any_persistent_session]] | BP-09 | Put dispatch authorization on the same trusted caller substrate.  **DONE 2026-08-15.**|
| 3 | P0 | [[officer_admission_does_not_lock_the_durable_post]] | BP-02/03/04 | One post lock and transaction governs manual and automatic admission. **DONE 2026-08-15.** |
| 4 | P0 | [[officer_decommission_is_not_atomic]] | OC-03 | The stable post lock governs the complete handoff and adjacent continuity decisions. **DONE 2026-08-15.** |
| 5 | P0 | [[direct_blocking_message_freeze_can_outlive_route]] | OC-01 | Make the default blocking-send creation recoverable.  **DONE 2026-08-15.**|
| 6 | P0 | [[message_route_resume_lacks_generation_cas]] | OC-04 | Fence every reply/timeout to the exact freeze generation.  **DONE 2026-08-15.**|
| 7 | P0 | [[officer_evidence_and_messages_leak_secret_shaped_content]] | OC-05 | Sanitize routine officer/user presentation before live use.  **DONE 2026-08-15 (blocker closed; remaining surfaces rescoped P2).**|
| 8 | P0 | [[backlog_fixed_windows_starve_eligible_tickets]] | BP-06 | Remove permanent starvation before the tick is enabled. **DONE locally 2026-08-16; not deployed/live-fired.** |
| 9 | P0 | [[officer_post_cannot_enable_auto_pull]] | BP-01 | Expose the owner control only after its downstream invariants exist. |
| 10 | P1 | [[message_route_delivery_failure_is_stamped_delivered]] | OC-06 | Separate attempted/failed from durably accepted delivery.  **DONE 2026-08-15 (false stamp fixed; attempt-count rescoped P3).**|
| 11 | P1 | [[officer_internal_messages_consume_human_rate_limits]] | OC-07 | Split internal flood control from human interruption quotas. |
| 12 | P1 | [[job_liveness_defaults_disagree_across_surfaces]] | OC-08 | Give every supervision surface one liveness policy. |
| 13 | P1 | [[officer_card_ignores_viewer_authority_and_i18n]] | OC-10 | Make the management surface truthful for roles and locales. |
| 14 | P1 | [[deleting_a_job_releases_its_backlog_ticket_claim]] | BP-05 | Persist claim retention independently of job retention. **DONE 2026-08-16 after rolling-upgrade/authority repair; local and not deployed.** |
| 15 | P1 | [[auto_pull_jobs_are_dispatchable_before_provisioning]] | BP-07 | Add non-dispatchable preflight and honest failure causes. **DEPLOYED; MAIN-DEV LIVE GATE PASSED 2026-08-17.** |
| 16 | P1 | [[kb_materialization_failure_reports_ready_or_closed]] | BP-08 | Stop authorization/disposition writes from reporting false success. **DEPLOYED; failure boundary passed, recovery residue BP-13 open.** |
| 17 | P1 | [[backlog_floor_wake_failure_consumes_debounce]] | BP-10 | Debounce durable wake success, not an attempted call. **DEPLOYED; MAIN-DEV LIVE GATE PASSED 2026-08-17.** |
| 18 | P0 | [[knowledge_metadata_retry_commits_then_projection_fails]] | BP-13 | Repair the canonical-retry timestamp codec and sweeper-won destructive defaults. **IMPLEMENTED LOCALLY; main-dev rerun pending.** |
| 19 | P1 | [[officer_roster_patch_cannot_remove_or_drain_a_slot]] | BP-11 | Give the roster whole-map edits and its documented zero drain. |
| 20 | P1 | [[headless_officer_cannot_read_screenshot_evidence]] | ES-01 | Either deliver bounded image content or make the tester fallback honest. |
| 21 | P2 | [[unknown_work_category_fails_open_for_parallelism]] | OC-09 | Close the latent fail-open before the helper gains a caller. |
| 22 | P2 | [[officer_ready_depth_poll_multiplies_backlog_queries]] | BP-12 | Optimize only after exact eligibility semantics are fixed. |

## What B1–B6 genuinely fixed

These are real gains and should be preserved while repairing the findings:

- Work categories and their kickoff contracts are centralized in
  `orchestrator/services/work_categories.py`; loop jobs receive the contract through
  `context.kickoff_message`.
- Machine tags are normalized, excluded from search documents, and queried with GIN-backed
  containment. `ready_at` survives ordinary edits and changes only on an explicit re-ready.
- Worker jobs have `ready` and `parallel-safe` stripped. Durable claims survive terminal
  status and physical job deletion; only a newer trusted `ready_at` generation can re-arm,
  and a preceding non-terminal job still blocks it.
- Migration 0160 adds a partial unique index against concurrent non-terminal claims for the
  same ticket.
- Manual and automatic officer creation both lock the durable post, revalidate the current
  incarnation/config/lineage, check claim and capacity, and INSERT on one connection.
- Capacity consistently includes every non-terminal job state, including paused and
  `waiting_for_reply` jobs.
- Executor work is serialized and gated on prior disposition unless the ticket carries the
  explicit parallel-safe authorization.
- Tick-created jobs carry full autonomy, the category, ticket provenance, slot, and the
  evidence contract expected at disposition.
- Sitrep, loop prompts, and the Cockpit card expose the new concepts. The gaps below concern
  truthfulness, authority, and operability, not absence of those surfaces.

## Previously reported findings — current status

| ID | Severity | Finding and current evidence | Effect once backlog pools run |
|---|---|---|---|
| OC-01 | **DONE 2026-08-15** | Direct blocking message creation persists message, route, wake intent, and freeze in one transaction. | Failure injection proves no frozen job can outlive its route. |
| OC-02 | **DONE 2026-08-15** | Officer message actions use the server-derived runtime actor credential boundary. | Shared transport identity alone cannot claim officer authority. |
| OC-03 | **DONE 2026-08-15** | One post-locked lifecycle transaction now includes the full-lineage no-force gate; direct orphan End, commission continuity, completion routing and commission config CAS use the same exact post/incarnation fence. | Real-PostgreSQL races prove admission and no-force decommission cannot both succeed, and continuity is delivered or retained exactly once across lifecycle changes. |
| OC-04 | **DONE 2026-08-15** | Reply/timeout resume is fenced to the exact route/freeze generation. | ABA and concurrent resolver tests prove an old actor cannot resume a newer wait. |
| OC-05 | **P0 blocker DONE 2026-08-15; P2 residues remain** | Initial evidence and worker/officer message presentation uses secret-shape sanitization. | The release-blocking routine surfaces are closed; separately enumerated lower-priority surfaces remain in the existing OC-05 residue scope. |
| OC-06 | **P1 blocker DONE 2026-08-15; P3 residue remains** | Delivery stamps now derive from accepted provider outcome; failure stays retryable with a null stamp. | Attempt-count observability remains in the existing lower-priority OC-06 residue scope. |
| OC-07 | **P1** | Human message quotas are checked before effective routing. Officer-only internal traffic is written into the same outbound message ledger counted by `check_message_rate_limit`. | Internal chain-of-command traffic consumes the human interruption quota, contrary to the routing contract. |
| OC-08 | **P1** | The liveness implementation still has multiple defaults: the shared helper/environment and MCP descriptor use 30 minutes while the REST/officer/session surface defaults to 60. | Stale-claim alarms, tools, and UI can disagree about whether the same job is stuck. |
| OC-09 | **P2 latent** | `allows_parallel(category)` treats an unknown or absent category as parallel-safe. No production caller currently uses this helper. | The first future caller can silently weaken executor serialization. Unknown categories must fail closed before the helper is activated. |
| OC-10 | **P1 UX/authority** | The officer card receives no `canManage`/role input, shows owner-only mutations to viewers, and contains inline English without Transloco keys in either locale. Server-side owner checks prevent the write but not the false affordance. | Viewers are invited into controls that always fail; the card violates the Cockpit authority and i18n contracts. |

## New findings in, or exposed by, B1–B6

### BP-01 — the supported management surface cannot enable auto-pull (**P0 functional**)

The Cockpit type `OfficerPostPatch` declares `auto_pull` and
`worker_spend_ceiling_daily`, and the summary reads both. The server’s
`_OFFICER_POST_EFFECTS` and numeric-field allowlists omit them, so commission/PATCH rejects
them as unknown. The card displays `auto_pull` but has no editor for it or for the century
spend ceiling; it also has no per-slot spend-ceiling editor.

The tick is consequently dormant through the supported Officer Post API and UI. It can be
armed only through an out-of-band thread/row mutation, which defeats the durable-post
authority and makes “end-to-end usable” inaccurate.

**Acceptance:** owner-only API and UI controls round-trip row state, take effect at the
documented boundary, survive recommission, and are covered by a default-off test plus a
deliberate enable/disable live test.

### BP-02 — authoritative manual/tick admission (**DONE 2026-08-15**)

Manual `POST /api/jobs` and automatic tick dispatch now call the same
`admit_and_create_job()` transaction. It locks the durable post and current thread,
revalidates configuration/lineage, counts all non-terminal capacity, validates the durable
ticket generation, stamps provenance, and inserts with `create_job(conn=...)` in the same
claim transaction. Real-PostgreSQL races
prove one winner for both different-ticket final-slot contention and same-ticket
manual/manual or manual/tick contention; the loser is a normal 409/skip, not a 500.

### BP-03 — commissioned-post tick authority (**DONE 2026-08-15**)

`officer_backlog_tick_once()` now uses the dedicated
`list_commissioned_officer_posts_for_backlog()` query over
`project_officers JOIN threads`. `list_officer_threads()` remains unchanged for the
watchdog/session-wake callers that intentionally enumerate runtime officer-shaped threads.
An enabled orphan is excluded by the authoritative query and rejected again at final
admission.

### BP-04 — stable post lock across lifecycle/config races (**DONE 2026-08-15**)

Admission, registration/recommission, hold/release, post roster/config writes, blocking
route creation and decommission share the stable `project_officers` row-lock prefix. Final
admission re-reads the exact live incarnation, enabled/hold/auto-pull state, roster,
category, owner and full lineage. Real-PostgreSQL tests interleave hold, disable, roster
change, decommission and recommission immediately before INSERT and prove the stale request
never creates a job.

### BP-05 — durable claim ledger (**DONE LOCALLY 2026-08-16; redeploy gate pending**)

Migration 0162 creates `officer_ticket_claims`, unique per project/ticket/ready generation
and per job identity, without a jobs FK. It preserves every project-scoped extant ticket
job: verifiable stamps retain their generations, while incomplete or questionable history
becomes an unversioned cutover barrier rather than guessed authority. Verified
same-generation collisions stay loud, and multiple legitimate re-ready generations remain
preserved. Manual and tick admission now insert
the durable claim and exact preallocated job UUID in the same post-locked transaction.
Eligibility, ready depth, stale claims, breaker history and executor disposition read this
ledger rather than reconstructing claims from current jobs.

Authorized job deletion records status/time/actor/reason on the claim in the same database
transaction and never releases it. Equal/older generations stay consumed; a newer trusted
Officer `ready_at` wins exactly once only after prior work is terminal. Real-PostgreSQL
tests cover manual/manual and manual/tick contention, the claim/job fault boundary, terminal
and non-terminal deletion, legacy retention DELETE, newer/equal/older generations, project
scope, recommission continuity and idempotent backfill. Manual `ticket=` resolves project,
readiness and generation server-side; no ready timestamp is model-selectable. See
[[deleting_a_job_releases_its_backlog_ticket_claim]].

Independent review found the application transaction correct but the rollout boundary
incomplete. The repair takes `SHARE ROW EXCLUSIVE` on `jobs` through backfill and trigger
installation. Pre-lock commits are captured; later old ticket INSERTs fail the named
integrity constraint, while old DELETEs trigger-audit status. Backfill idempotency is
`ON CONFLICT (job_id) DO NOTHING` only. Public/internal/tool/database funnels strip raw
claim authority. The trigger is ledger-first and null-safe, preserves the authentic
source-less pre-0162 stamp on backfilled rows, and forbids a live claimed job from removing
its authority stamp. Deletion truth is returned inside the delete transaction rather than
queried after commit. Real PostgreSQL covers the lock boundary, old writers, collision
diagnostics, nullable/missing provenance, historical context merging, endpoint bypasses,
atomic deletion response and the index plan. See
[[deleting_a_job_releases_its_backlog_ticket_claim]].

The first main-dev gate then exposed a field-history gap in that repair: all seven real
ticket jobs lack a stamped ready generation, while the test fixture called “historical”
already carried one. The migration rolled back cleanly but left the new replica unable to
start. The reopened contract records each genuine pre-ledger row as a
`legacy_unversioned` fail-closed barrier without inventing a generation or admission
provenance. Only an explicit trusted re-ready strictly after the database cutover can
supersede it, and non-terminal predecessors continue to block. Exact stamp-less and
partial-stamp PostgreSQL fixtures are the repair's release gate.

The local repair now satisfies that contract. Six stamp-less and one partial-stamp fixture
become `legacy_unversioned` rows with NULL generation/provenance and one database cutover
barrier; equality stays consumed, strict post-cutover re-ready wins once after terminal
work, and deleted non-terminal history still blocks. The complete real-PostgreSQL Officer
Post file passed 66 tests, the expanded Officer checkpoint passed 547, migration tests
passed 34, and the from-zero app chain replayed through 0162. Main dev still has the failed
old 0162 ledger row and old serving replicas, so BP-05 is not deployed until the documented
recovery sequence and live gate pass.

### BP-06 — fixed pre-filter windows can starve valid work forever (**P0 liveness**)

**Closed locally 2026-08-16; not deployed/live-fired.** KB ordering and app claim state
cannot be joined, so `BacklogCursor` + `_scan_eligible_tickets` now keyset-page the complete
`priority → created_at NULLS LAST → note_id` order until admission has enough eligible
rows or exhaustion is proven. Ready depth always scans to exhaustion and omits unavailable
pools instead of publishing a false exact zero. The 100-row page size is transport only;
there is no page/time correctness ceiling. Migration 0021 adds the matching partial vector
index.

Breaker history is a dedicated terminal-first, `DISTINCT ON (ticket_note_id)` query. Stale
claims are selected by open + threshold predicates and ordered oldest-first in SQL; sitrep
uses a dedicated oldest-open query. Executor category/terminal predicates now precede
`LIMIT 1`, and optional slot spend reads the complete scoped job-id set.

Acceptance evidence covers 11 claimed head rows plus a valid tail, equal-key page
boundaries, 30 exact ready candidates, mixed repeated breaker history, 60 stale claims,
KB/app failure states, and the existing manual/tick one-claim race. At 10k target rows,
exhaustive vector pagination measured 183.85 ms (`idx_knowledge_backlog_page`; first-page
plan 0.03 ms), and the expanded 10k-ledger app query set measured 22.71 ms
(`idx_officer_ticket_claims_lineage_slot_claimed`; measured plan 0.08 ms). See
[[backlog_fixed_windows_starve_eligible_tickets]] in `docs/done`.

### BP-07 — provisioning races dispatch and pollutes the failure breaker (**P1 major**)

**Closed; deployed and live-gated 2026-08-17.** Strict Officer admission now inserts one claim
and a born-paused job with an `officer_preflight` freeze in the existing post-locked
transaction. All dispatch lanes and their final claim CAS require `freeze_data IS NULL`.
The jobs row carries the normalized `not-attempted -> in-progress -> activated` state plus
retryable/permanent failure arms; a lease/token CAS changes `paused -> created` and clears
the freeze only after repository and cloud provisioning finish.

Repository/cloud failures remain visible, capacity- and claim-holding, and classified as
infrastructure. The distinct-ticket breaker query excludes them while genuine worker
failures retain existing behavior. Tick and manual strict creation share the boundary;
recovery and concurrent attempts provision/activate once. Deterministic before/after-
activation faults and real-PostgreSQL races prove one job/claim and no early lease. See
[[auto_pull_jobs_are_dispatchable_before_provisioning]] in `docs/done`.

The main-dev gate crossed 35 seconds with the job paused/unassigned, recorded an injected
repository failure outside breaker history, then provisioned a real isolated Gitea repo,
activated once, and cancelled before dispatch. Cleanup removed all disposable state.

### BP-08 — failed KB materialization is reported as a successful ready/close (**P1 major**)

**Core failure-truth boundary closed and deployed 2026-08-17; BP-13 recovery residue
open.** Migration 0165's durable materialization ledger
makes the project knowledge Git repository canonical and pgvector its required
search/eligibility projection. Every content/metadata mutation and backlog close persists
intent, then crosses Git, then projection. Missing binding/repository, Git exception,
conflict refusal, malformed frontmatter, materializer exception, or projection failure can
no longer return Created, Updated, READY, or closed.

Unresolved notes are excluded from backlog eligibility. Failed close leaves the executor
disposition and projection open. Retry leases run before reindex, then reindex settles only
the newest canonical intent; the exact canonical `ready_at` is reused rather than bumped.
Tool/API/SITREP/Cockpit surfaces name canonical, pending-sync, failed, projection-only,
retry, and projection outcomes. See [[kb_materialization_failure_reports_ready_or_closed]]
in `docs/done`.

The main-dev gate proved the failure half: 409 `pending_sync`, unchanged eligibility
projection, and no reindex resurrection. The due retry then committed Git but the REST
route passed its ISO `ready_at` string to asyncpg's datetime codec and returned 500. The
same code audit found an `already-canonical` retry can default missing canonical tags and
generation to `[]`/NULL. [[knowledge_metadata_retry_commits_then_projection_fails]] owns
that release blocker.

### BP-09 — “officer-only” readiness is actually persistent-session-only (**P0 security**)

`_has_officer_authority()` returns true for any `ToolContext` carrying `_thread_id`. Thread
project authorization accepts any membership role, including `viewer`, and
`build_knowledge_bindings()` marks the primary project KB writable without carrying that
role. The same broad boundary is used for `charter` writes.

A project viewer can create an ordinary persistent session, add `ready` or
`parallel-safe`, rewrite standing orders, and cause owner-funded auto-pull/executor
parallelism. Worker prompt injection is blocked, but member/session privilege escalation is
not.

**Acceptance:** the orchestrator supplies an unforgeable caller role/kind in trusted
runtime context. `ready`, `parallel-safe`, and charter mutation require the explicit
officer or the chosen human project role (normally owner/admin), never merely a thread ID.
Tests cover viewer, editor, owner, conference, commissioned officer, and worker callers.

### BP-10 — floor-wake debounce records attempts as deliveries (**P1**)

**Closed; deployed and live-gated 2026-08-17.** Migration 0165's floor-episode ledger defines
success as a verified insert into the existing durable session-wake outbox. Attempted,
durably queued, delivered, failure, retry, and next retry are separate fields. Only
`last_queued_at` starts the six-hour policy debounce; transient `next_retry_at` is an
independent clock.

Project/incarnation/pool/episode deduplication and the post -> thread -> wake lock order
make duplicate replicas converge on one intent. Missing/false/raising notifier and outbox
rollback remain retryable and do not increment the success metric. Hold preserves a
durably queued intent as pending; decommission deletes/supersedes it under the same post
lock. Real PostgreSQL tests cover queue/decommission and queue/hold races. See
[[backlog_floor_wake_failure_consumes_debounce]] in `docs/done`.

The main-dev gate proved rollback after the durable insert consumes no policy debounce,
retry queues, concurrent replicas converge on one event, delivery updates the same
episode, and queue/decommission leaves no live event or episode.

### BP-13 — canonical metadata retry commits, then REST projection fails (**P0 release**)

**Implemented locally and k3d-gated 2026-08-17; main-dev rerun pending.** The
deployed materializer parses `ready_at` from the canonical YAML as a string.
`update_knowledge_note()` passes that value directly to an
asyncpg `timestamptz` parameter, which rejects it after Git has committed. The route
returns 500 and records projection failure until reindex plus scheduled settlement.

If the scheduled sweep wins before a client retry, the durable intent's
`already-canonical` result omits exact tags/readiness while the route defaults those values
to empty/null. That arm can report synced projection while temporarily contradicting Git.
Real-PostgreSQL codec and sweeper-won retry tests now pass locally. A disposable k3d run
also crossed the real HTTP/Gitea/app-ledger/pgvector boundary and proved a 200 direct
READY, fail-closed 409, single retry commit, exact post-sweep client projection, and full
cleanup. That Tilt image came from the repaired working tree rather than a commit on
`origin/develop`, so the repeat main-dev gate remains required. See
[[knowledge_metadata_retry_commits_then_projection_fails]].

The local repair now parses canonical readiness into an aware `datetime`, rereads the
complete current vault note when another retry already canonicalized the intent, and
rejects incomplete snapshots without touching pgvector. Executor close uses the same
canonical status snapshot, and successful manual/post-write reindex settles the newest
canonical intent like the scheduled sweep. Direct and sweeper-won endpoint paths pass
against the migrated real pgvector schema. The historical deployed failure remains open
until the bounded BP-08 slice passes on the replacement image.

### BP-11 — roster edits cannot remove one slot or set a zero-count drain (**P1 functional**)

Both post and thread config writers recursively deep-merge `officer.slots`. The card sends
the remaining map after `removeSlot()`, so a removed key survives server-side unless the
entire roster is cleared with `slots:null`. The UI also clamps counts to at least one even
though the server accepts zero, making the documented drain/disable value unreachable.

**Acceptance:** `slots` has replace-map semantics (or explicit per-key tombstones), removal
of one among several slots round-trips, and the UI permits the server’s 0–20 range.

### BP-12 — readiness queries do unnecessary count work on every card poll (**P2**)

`fetch_backlog()` always runs both its row query and grouped count query. The tick and
`ready_depth_by_pool()` discard counts. The officer card polls every 15 seconds and computes
depth serially per pool, multiplying vector queries by the number of pools and viewers.

**Acceptance:** add a no-count/dedicated eligible query and batch pool depth. Verify query
count and latency with the maximum supported roster rather than only component tests.

### ES-01 — screenshot evidence is not visible to a headless officer (**P1 supervision**)

`read_evidence_entry()` integrity-checks screenshot bytes but returns a
`{type: job_repo_file, path, ref}` view pointer. `format_evidence_read()` converts that into
plain text telling the caller to open the existing job-file viewer. Descriptor-backed
officer tools return a string; the background officer has neither the viewer nor an
object-plane file tool. He can verify screenshot metadata, but cannot inspect the image.

That does not meet E4’s “screenshot and report live read” acceptance or the executor
disposition rubric. Either return a bounded multimodal tool attachment that the officer
runtime can actually consume, or state that screenshots always require a tester/recon
delegate and remove the stronger acceptance claim.

### B7 review — writer landed without a new blocker

The writer work changed while this audit was being recorded and has since landed separately
as `fa9fe159`: config/persona, `CATEGORY_EXPERTS[executor]`, app-guide catalog, grant
fixture, and category tests. The inheritance claim is sound (`worker_base` already denies
shell and delegation), its 64-tool grant matches `general-worker`, and an `expert:writer`
ticket passes executor membership while remaining invalid for researcher/tester pools.

The three schema declarations discovered during that work landed independently as
`ae736584`, so they are not silently attributed to B7. No new B7-specific blocker was found.
It remains intentionally outside the first acceptance kit; landing a wider roster does not
relax any release gate in this audit.

## Closed interactions and remaining compound risks

| Finding | Current backlog interaction |
|---|---|
| OC-01 transactional route creation — **closed** | A blocking job cannot hold a claim/slot without the matching durable route. |
| OC-02 runtime actor boundary — **closed** | A worker cannot claim officer authority merely by naming the commissioned thread. |
| BP-02/03/04 post authority + OC-03 handoff — **closed** | Admission, no-force handoff, orphan retirement, commission continuity and completion routing serialize on the durable post. |
| OC-04 route-generation CAS — **closed** | An old reply/timeout cannot change the liveness/capacity view of a newer freeze. |
| OC-05 routine sanitization — **release blocker closed** | Lower-priority enumerated presentation residues remain, but routine officer inputs no longer carry the original release-blocking leak. |
| OC-08 liveness drift | Stale-claim pages, officer tools, and Cockpit can disagree about the same slot. |
| OC-10 UI authority gap | BP-01/BP-11 leave the one visible management surface both over-permissive in appearance and incomplete for real operation. |

## Release order and acceptance

Fixing isolated symptoms in arbitrary order will keep reopening the same seams. The safe
sequence is:

1. **Identity and authority — completed 2026-08-15:** OC-02, BP-03, BP-04, BP-09.
   Runtime actors are server-derived and the durable post is dispatch authority.
2. **Atomic state transitions — completed 2026-08-15:** OC-01, OC-03, OC-04 and BP-02
   now have transactional/CAS boundaries, including the full-lineage no-force gate,
   orphan-End decision, commission continuity, completion routing and commission config
   generation fence.
3. **Durable eligibility and preflight:** BP-05, BP-06, and BP-07 are complete; BP-07's
   deployed main-dev gate passed. Claims survive retention, cross-store scans do not
   starve, and strict jobs remain non-dispatchable through mandatory provisioning. OC-08
   and OC-09 remain.
4. **Truthful content and delivery:** BP-10's deployed main-dev gate passed. BP-08's
   failure boundary passed; BP-13's successful-retry repair is local/k3d green and awaits
   its main-dev rerun. OC-05–OC-07 and ES-01 remain; continue to redact before either
   audience and preserve the attempted/queued/delivered distinctions.
5. **Supported operation:** BP-01, BP-11, BP-12, OC-10. Only after the invariants exist
   should the owner-facing enable switch and live card be treated as a release surface.
6. **Live fire:** enable one non-executor pool with disposable tickets, inject notification,
   KB, provisioning, and officer-restart faults, then graduate to the executor singleton.

Completed automated transaction gates (not a substitute for live fire):

- Failure after every direct blocking-send/decommission database substep yields rollback
  or one complete recoverable state; no untracked freeze or partial vacant-post handoff.
- Manual/manual and manual/tick admission races, plus hold, disable, roster,
  decommission/recommission interleavings, preserve lineage capacity and reject stale
  incarnations.
- Admission/no-force decommission cannot both succeed; direct orphan End cannot disturb a
  commissioned successor; commission continuity and job-completion routing remain
  exactly-once across commission/decommission races; a losing commission cannot patch the
  winner.
- Durable claim/job insertion rolls back together; manual/manual and manual/tick races
  produce one claim/job; deletion, retention, re-ready and recommission preserve the
  project-scoped ledger contract.
- Route A reply/timeout actors cannot resume a refrozen route B generation.
- Strict Officer jobs are born paused/frozen, cannot be claimed during provisioning, and
  recover across concurrent and before/after-activation faults with one job/claim/effect.
  Repository/cloud failures remain outside distinct-ticket breaker history.
- Canonical knowledge intent, required projection, retry/reindex settlement, and exact
  readiness generation converge under injected Git/materializer/projection failures.
- Duplicate floor ticks queue one durable wake; notifier/outbox failures do not debounce,
  and hold/decommission races leave no deliverable orphan.

Remaining minimum regression/live gates before `auto_pull` leaves its safe default:

- Repeat the completed transaction gates through supervised process/pod interruption in a
  disposable environment; no real project or held officer is in scope for this checkpoint.
- Put more than 10 claimed/invalid tickets ahead of an eligible ticket, more than 10 mixed
  breaker outcomes in a pool, and more than 50 open claims. The correct tail item/outcome/
  oldest stale claim remains visible.
- ~~Make repository/cloud provisioning fail and delay it beyond a dispatcher poll.~~
  **Passed on main dev 2026-08-17:** no early execution; infrastructure outcome absent
  from breaker history; real repository recovery activated once.
- Attempt charter/`ready`/`parallel-safe` writes as worker, viewer session, editor session,
  owner session, conference, and commissioned officer.
- Force KB materialization and notification dispatch failures; the notification/outbox/
  debounce half passed on main dev. KB failure truth passed and BP-13 is repaired locally;
  rerun its successful-retry half after deployment. UI truth remains to be exercised.
- Consume a real screenshot in a headless officer turn, not only through a Cockpit viewer.
- Exercise the owner and viewer cards in both locales, including auto-pull enable/disable,
  one-slot removal, zero-count drain, spend ceilings, and a failed write.

## Verification performed during this audit

The complete pre-rebase B1–B6 tree was tested in an isolated archive so the concurrent
rebase and working tree were not mutated. The focused officer/backlog/knowledge/routing/
evidence set produced **371 passes**. Its sole reported failure was the knowledge-grant test
resolving `/tmp/config/experts/centurion/config.yaml` outside the archived repository; the
same source/config contract was inspected directly and this was an archive-path artifact,
not evidence of a product failure.

After the final rebase and the separate B7/schema commits, the expanded suite was repeated
at current tip `ae736584`, including writer/category and grant-snapshot coverage:
**404 passed in 75.34 seconds**. Command:

```bash
.venv/bin/python -m pytest \
  tests/test_work_categories.py tests/test_config_tool_grants_snapshot.py \
  tests/test_backlog_ticket_plumbing.py tests/test_officer_backlog_tick.py \
  tests/test_officer_pool_surfacing.py tests/test_officer_post.py \
  tests/test_officer_lifecycle.py tests/test_officer_knowledge_plane.py \
  tests/test_officer_message_routing.py tests/test_job_evidence.py \
  -q --tb=short
```

That suite remains useful historical evidence for the mechanics listed in “What B1–B6
genuinely fixed.” The later post-safety checkpoint added the negative, race and crash
coverage that was absent there. At the completed checkpoint, the expanded focused command
was:

```bash
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
```

The real-PostgreSQL admission/routing/handoff subset produced **48 passes in 115.55
seconds**. It uses a disposable PostgreSQL 15 testcontainer and includes final-slot and
same-ticket races, every named decommission fault point, repeated handoff, concurrent
handoff/recommission, the stale-route race, both admission/no-force outcomes, all
non-terminal lineage states, occupied/vacant orphan End, commission continuity,
completion/commission exactly-once routing, and the losing-commission CAS.
`auto_pull=true` is synthesized only in the isolated manual/tick race fixture.

Repository static gates are clean: `ruff check src/ orchestrator/ tests/`,
`ruff format --check src/ orchestrator/ tests/` (**1201 files already formatted**), and
`git diff --check` all exited zero.

The current checkpoint also ran `./scripts/pytest-fast.sh` with its default system
interpreter. It reached **14,772 passes and 123 skips in 120.85 seconds** before the
script's fail-fast boundary stopped on one import-time failure:
`tests/tools/research/test_arxiv_client.py::test_installed_arxiv_package_exposes_client_results`.
`/usr/bin/python -c "import arxiv"` reproduces `ModuleNotFoundError`; the complete file
passes under the project virtualenv (**22 passed in 0.12 seconds**). This proves a local
interpreter/dependency distinction rather than an Officer assertion failure.

An earlier non-fail-fast diagnostic established the rest of the known environment shape.
The system interpreter is not the pinned CI Python and lacks two declared requirements:
`arxiv` and `langchain-mcp-adapters`; a local
`DATABASE_URL` also pointed at an absent `localhost:5432` service. With that database
variable blanked and the directly affected collection files excluded, a non-fail-fast run
reached **18,614 passes and 164 skips**; its only three failures were the two remaining
arXiv health assertions and one MCP wiring assertion importing those same missing packages.
The arXiv client module produced **22 passes** and both health assertions passed under the
project venv, where `arxiv` is installed. Direct imports reproduce the MCP adapter absence
under both available interpreters. One unrelated permission-wait file was excluded after
the broad xdist order reused an `asyncio.Event` bound to another loop; the complete file
passes alone (**24 passed**). These are explicit local-environment/test-isolation gaps, not
officer checkpoint failures.

The umbrella nevertheless remains open: this automated transaction evidence does not
close BP-01/BP-11, ES-01, OC-07/OC-08/OC-10, the remaining OC-05/OC-06 residues, or
the live background-officer image-consumption gate.

### 2026-08-16 deployed gate and local BP-05 checkpoint

The intended shared development environment was resolved explicitly as context `main`,
namespace `superhuman-remote-worker`, rather than local `k3d-srw`. One uniquely named
disposable project commissioned a fresh Officer through the supported endpoint. Database,
API and runtime evidence agreed on `centurion`, `autonomous`, one exact live post link,
the 49-tool control/inspection/evidence surface, absence of workspace/object tools,
persisted tool-result pairing, useful output and a normal next wake. A tiny ticketed
sandbox researcher job carried authoritative ticket/incarnation/slot/category provenance.
After an exact-pod restart the replacement restored 59 messages and completed another
paired inspection turn. That live run missed LF-5's exact orphan window. LF-5 has since
closed locally through a deterministic post-persist/pre-tool fault seam, same-process
next-turn repair, restore coverage, and a two-equivalent-400 circuit that persists one
operator-visible escalation and spends no third provider call. The exact pod-window run
was not repeated and is not claimed as a passed live gate. All named disposable rows and
pods were removed; `auto_pull` stayed false.

The subsequent local BP-05 implementation and independent-review repair verified:

```text
earlier focused Officer/admission/deletion set:        662 passed in 239.96s
earlier real Officer Post PostgreSQL file:              53 passed in 117.42s
follow-up malformed/backfill/delete cases:              13 passed in 40.03s
follow-up complete Officer PostgreSQL file:             64 passed in 137.24s
follow-up broader Officer/API/tool checkpoint:         636 passed in 262.65s
follow-up deletion collaborator checkpoint:            150 passed in 0.73s
migration/head tests:                                   34 passed in 28.82s
schema replay and generated drift check:                OK (all artifacts current)
Cockpit job-list (earlier checkpoint):                  19 passed
Cockpit EN/DE i18n (earlier checkpoint):                2530 keys, clean
ruff check / format check / git diff --check:           clean
```

The repository fast suite exposed two genuine stale tests (the app migration-head sentinel
and a ready-depth mock), both corrected. Its final system-Python run reached **14,783
passed / 123 skipped** before the already-known missing `arxiv` dependency stopped it; the
exact file passes under the project virtualenv (**22 passed**). The review repair ran the
proportionate focused set rather than repeating that environment-limited full suite. BP-05
is committed in HEAD but not claimed deployed; BP-06 remains uncommitted and not deployed.
This evidence permits continued supervised manual/O6 testing only with `auto_pull=false`;
it does not authorize unattended backlog release.

The post-deployment BP-05 smoke is specified separately in
[[officer_ticket_claim_ledger_live_gate_2026-08-16]]. Its PASS updates deployment evidence
only; it does not change this umbrella's open auto-pull verdict.

### 2026-08-17 local BP-07/BP-08/BP-10 correctness checkpoint

This tranche is local, uncommitted, undeployed, and not live-fired. Migration 0165 adds the
canonical-knowledge convergence and floor-wake episode ledgers; strict provisioning uses
the existing paused/frozen jobs authority rather than adding a public status. The post ->
thread -> jobs -> wake/routes lock order, migration 0162 claim barrier, project scoping,
one-shot claim semantics, and `auto_pull=false` default remain intact.

```text
focused materialization/project/tool suite:            360 passed in 2.90s
expanded knowledge/reindex/authorization suite:       1081 passed in 27.63s
focused Officer/provision/dispatch/wake suite:         431 passed in 61.28s
expanded Officer/admission/lifecycle/routing suite:    991 passed in 110.43s
real PostgreSQL Officer/routing/pagination suite:       97 passed in 232.40s
migration/schema contract suite:                       103 passed, 3 skipped in 62.05s
from-zero schema replay/drift check:                    139 app / 16 vector / 5 audit, clean
Cockpit Officer component:                              64 passed in 759ms
Cockpit EN/DE parity + hardcoded-copy check:             2553 keys, clean
Cockpit production build:                               passed in 7.55s (known budget/CommonJS warnings)
changed-file Ruff check / format check:                  clean (26 Python files)
git diff --check:                                        clean
```

The expanded and broad suites exposed and prompted repair of stale BP-08 success fixtures
and Officer-summary health doubles. The final system-Python run reached **14,985 passed /
123 skipped in 219.27 s**
before the known undeclared local interpreter gap stopped it at
`tests/tools/research/test_arxiv_client.py`: `/usr/bin/python -c "import arxiv"`
reproduces `ModuleNotFoundError`. The exact file passes under the repository `.venv`
(**22 passed in 0.12 s**).

The three issue contracts are recorded in `docs/done`. No shared forge, notification
channel, Officer, database, pod, or cluster was mutated, so this evidence does not claim a
live provisioning, Git, wake-delivery, interruption, or unattended-dispatch gate. The
umbrella remains open and `auto_pull` remains off and unexposed.

### 2026-08-17 deployed BP-07/BP-08/BP-10 main-dev gate

The tranche was subsequently committed, pushed, and deployed as image `sha-4d703be` on
two ready, zero-restart replicas. App migration 0165 applied successfully in 27 ms. The
bounded run in [[officer_correctness_live_gate_2026-08-17]] used one disposable project,
synthetic Officer, real Gitea vault, ticket, strict job, and durable wake episodes while
leaving the existing Officer untouched and every post at `auto_pull=false`.

```text
BP-07 strict provisioning:                         PASS
  born paused/frozen; 35-second dispatcher window: PASS
  infrastructure failure absent from breaker:     PASS
  real Gitea recovery, one activation:             PASS

BP-08 canonical failure truth:                     PASS
  409 pending-sync; projection unchanged:          PASS
  broken reindex invents no READY state:            PASS
BP-08 canonical retry REST projection:             FAIL (BP-13)
  Git commit:                                      PASS
  stable single ready_at through two reindexes:    PASS
  endpoint result:                                 HTTP 500, asyncpg str/datetime mismatch

BP-10 failed insert/debounce truth:                 PASS
  concurrent queue exactly once:                   PASS
  delivery settlement and decommission race:       PASS
cleanup / existing-post / auto-pull baseline:      PASS
```

The failed route supplied an ISO timestamp string to an asyncpg `timestamptz` parameter
after the canonical Git update. The exception path correctly recorded projection failure;
manual reindex restored the vector row without changing the canonical generation, and the
scoped production settlement helper allowed the independent BP-07/BP-10 probes to finish.
This continuation is evidence of recoverability, not a pass. BP-13 also records the
unexercised sweeper-won retry arm where absent canonical result fields currently default
to empty tags/null readiness.

All disposable project/thread/job/claim/intent/episode/vector rows, managed repositories,
cloud folder, and identity group were removed. Final counts were zero, the original
commissioned-post set was unchanged, both serving replicas remained ready with zero
restarts, and dirty migrations/`auto_pull=true` posts remained zero. The umbrella remains
open; BP-13 must be repaired and its slice rerun before the later release gates proceed.
