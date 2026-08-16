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
- Unattended backlog release is still blocked by the supported enable-control gap and fixed
  pre-filter starvation, followed by the still-open provisioning, materialization, roster,
  evidence, and operational residues listed below.

“Implemented” in the feature docs therefore still does not mean unattended backlog release
is safe. The earlier tranche was deployed and O6 was released successfully with
`auto_pull=false`; the later lifecycle/configuration transaction checkpoint was also
deployed and passed a bounded disposable Officer gate on 2026-08-16. BP-05 is the current
local, uncommitted and not-deployed checkpoint. Nothing here authorizes `auto_pull=true` or
unattended backlog release. The umbrella stays open until the remaining P0 live gates in
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
in [[officer_backlog_pools_resavio_livefire]]. BP-05 below is local and not deployed.

| Order | Priority | Issue | Audit finding(s) | Why this boundary |
|---:|---|---|---|---|
| 1 | P0 | [[officer_message_actions_trust_shared_transport_identity]] | OC-02 | Establish actor identity before trusting any officer mutation.  **DONE 2026-08-15.**|
| 2 | P0 | [[backlog_machine_tags_trust_any_persistent_session]] | BP-09 | Put dispatch authorization on the same trusted caller substrate.  **DONE 2026-08-15.**|
| 3 | P0 | [[officer_admission_does_not_lock_the_durable_post]] | BP-02/03/04 | One post lock and transaction governs manual and automatic admission. **DONE 2026-08-15.** |
| 4 | P0 | [[officer_decommission_is_not_atomic]] | OC-03 | The stable post lock governs the complete handoff and adjacent continuity decisions. **DONE 2026-08-15.** |
| 5 | P0 | [[direct_blocking_message_freeze_can_outlive_route]] | OC-01 | Make the default blocking-send creation recoverable.  **DONE 2026-08-15.**|
| 6 | P0 | [[message_route_resume_lacks_generation_cas]] | OC-04 | Fence every reply/timeout to the exact freeze generation.  **DONE 2026-08-15.**|
| 7 | P0 | [[officer_evidence_and_messages_leak_secret_shaped_content]] | OC-05 | Sanitize routine officer/user presentation before live use.  **DONE 2026-08-15 (blocker closed; remaining surfaces rescoped P2).**|
| 8 | P0 | [[backlog_fixed_windows_starve_eligible_tickets]] | BP-06 | Remove permanent starvation before the tick is enabled. |
| 9 | P0 | [[officer_post_cannot_enable_auto_pull]] | BP-01 | Expose the owner control only after its downstream invariants exist. |
| 10 | P1 | [[message_route_delivery_failure_is_stamped_delivered]] | OC-06 | Separate attempted/failed from durably accepted delivery.  **DONE 2026-08-15 (false stamp fixed; attempt-count rescoped P3).**|
| 11 | P1 | [[officer_internal_messages_consume_human_rate_limits]] | OC-07 | Split internal flood control from human interruption quotas. |
| 12 | P1 | [[job_liveness_defaults_disagree_across_surfaces]] | OC-08 | Give every supervision surface one liveness policy. |
| 13 | P1 | [[officer_card_ignores_viewer_authority_and_i18n]] | OC-10 | Make the management surface truthful for roles and locales. |
| 14 | P1 | [[deleting_a_job_releases_its_backlog_ticket_claim]] | BP-05 | Persist claim retention independently of job retention. **DONE 2026-08-16 after rolling-upgrade/authority repair; local and not deployed.** |
| 15 | P1 | [[auto_pull_jobs_are_dispatchable_before_provisioning]] | BP-07 | Add non-dispatchable preflight and honest failure causes. |
| 16 | P1 | [[kb_materialization_failure_reports_ready_or_closed]] | BP-08 | Stop authorization/disposition writes from reporting false success. |
| 17 | P1 | [[backlog_floor_wake_failure_consumes_debounce]] | BP-10 | Debounce durable wake success, not an attempted call. |
| 18 | P1 | [[officer_roster_patch_cannot_remove_or_drain_a_slot]] | BP-11 | Give the roster whole-map edits and its documented zero drain. |
| 19 | P1 | [[headless_officer_cannot_read_screenshot_evidence]] | ES-01 | Either deliver bounded image content or make the tester fallback honest. |
| 20 | P2 | [[unknown_work_category_fails_open_for_parallelism]] | OC-09 | Close the latent fail-open before the helper gains a caller. |
| 21 | P2 | [[officer_ready_depth_poll_multiplies_backlog_queries]] | BP-12 | Optimize only after exact eligibility semantics are fixed. |

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

### BP-05 — durable claim ledger (**DONE 2026-08-16; not deployed**)

Migration 0162 creates `officer_ticket_claims`, unique per project/ticket/ready generation
and per job identity, without a jobs FK. It backfills every verifiable extant ticket job,
rejects questionable provenance and same-generation collisions, and preserves multiple
legitimate re-ready generations. Manual and tick admission now insert
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

### BP-06 — fixed pre-filter windows can starve valid work forever (**P0 liveness**)

The tick asks `fetch_backlog(... limit=10)` and only afterwards filters one-shot claims,
ambiguous tags, and expert validity. Completed-but-still-ready tickets intentionally remain
claimed until officer disposition. Once ten higher-ranked claimed/invalid tickets occupy
the head, eligible ticket eleven is never inspected on any tick. The same pattern appears
elsewhere:

- Cockpit ready depth is capped before eligibility filtering (25), so it can under-report
  or hide an eligible tail.
- Breaker history fetches ten jobs before filtering terminal outcomes and distinct ticket
  IDs, so recent non-terminals or repeated outcomes can hide the relevant two outcomes.
- Stale-claim detection fetches only the newest 50 open claims while it needs the oldest
  overdue claim.

**Acceptance:** move semantic eligibility into a paginated/database query, or page until
enough eligible rows are found/exhaustion is proven. Tests need more than every current
limit, with claimed and malformed rows ahead of valid work.

### BP-07 — provisioning races dispatch and pollutes the failure breaker (**P1 major**)

The tick commits a `created` job, then provisions repository/cloud state outside the
transaction, and only afterwards nudges dispatch. The global dispatcher independently
polls `created` jobs, so it can lease the job before strict provisioning completes. When
provisioning raises, the tick changes the job to ordinary `failed`; a later breaker pass
counts that as a job failure even though the design says infrastructure failures never
feed breakers.

**Acceptance:** jobs are born in a non-dispatchable preflight state (or provisioning is
otherwise completed before visibility), then atomically activated. Provisioning failures
carry a machine-readable cause excluded from breaker outcomes. Test the next tick, not only
the tick in which provisioning failed.

### BP-08 — failed KB materialization is reported as a successful ready/close (**P1 major**)

`knowledge_tools._materialize_note()` intentionally returns a failed result rather than
raising. `kb_write`/`kb_update` paths ignore that result, continue updating projections,
and return success text. An officer can therefore believe a ticket was readied or closed
while the canonical OKF file was not changed. A later reindex can restore stale state or
resurrect the ticket.

This is the still-open K4 availability requirement, now on the authorization and
disposition path rather than merely a diagnostic nicety.

**Acceptance:** mutation responses expose materialization status; readiness and closure
fail closed or enter an explicit degraded/retry state. Tests inject git/materialization
failure and prove no false “Updated/READY/closed” result is returned.

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

When `notify` is absent, raises, or returns a false result, `tick_officer()` still stores
`backlog_floor_wakes[pool]=now`; a false return also increments its wake count. A transient
notification outage therefore suppresses the needed wake for six hours while metrics say
it happened.

**Acceptance:** write the debounce timestamp only after a durable outbox insert/positive
delivery contract. Failed attempts remain retryable and have separate metrics.

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
3. **Durable eligibility and preflight:** BP-05 is completed locally; BP-06, BP-07, OC-08
   and OC-09 remain. Claims now survive retention; scans must not starve, and a job must not
   be dispatchable until its prerequisites are ready.
4. **Truthful content and delivery:** OC-05–OC-07, BP-08, BP-10, ES-01. Redact before either
   audience, distinguish attempted from delivered, and surface degraded canonical writes.
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

Remaining minimum regression/live gates before `auto_pull` leaves its safe default:

- Repeat the completed transaction gates through supervised process/pod interruption in a
  disposable environment; no real project or held officer is in scope for this checkpoint.
- Put more than 10 claimed/invalid tickets ahead of an eligible ticket, more than 10 mixed
  breaker outcomes in a pool, and more than 50 open claims. The correct tail item/outcome/
  oldest stale claim remains visible.
- Make repository/cloud provisioning fail and delay it beyond a dispatcher poll. No job
  executes early and the infrastructure outcome does not trip the job-failure breaker.
- Attempt charter/`ready`/`parallel-safe` writes as worker, viewer session, editor session,
  owner session, conference, and commissioned officer.
- Force KB materialization and notification dispatch failures; the response, outbox,
  debounce, retry state, and UI must all tell the same truth.
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
close BP-01/BP-06/BP-07/BP-08/BP-11, ES-01, the remaining OC-05/OC-06 residues, or
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
paired inspection turn. LF-5 did not reproduce, but the tool results landed before process
death, so the exact orphan window and repeated-400 escalation remain unverified. All named
disposable rows and pods were removed; `auto_pull` stayed false.

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
remains uncommitted and not deployed. This evidence permits continued supervised manual/O6
testing only with `auto_pull=false`; it does not authorize unattended backlog release.
