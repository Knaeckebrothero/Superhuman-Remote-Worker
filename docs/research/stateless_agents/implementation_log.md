# S1 implementation log — night of 2026-08-07

Working session: autonomous overnight build of the S1 spine from
`docs/features/stateless_agents.md` v3 (§5.1–§5.3, §9/S1). User approved the doc
and asked for a subagent-driven implementation, verified on the local k3d
cluster (Tilt up). This file is the running log: plan, decisions, deviations,
verification results. **Statuses reflect only work actually completed and
verified — updated as milestones land.**

**Ground rules for the night**
- No `git commit` / no push — the user reviews and commits in the morning.
  (Executed 2026-08-08: everything committed to `feature/stateless-agents` at
  the user's request; the per-milestone patch snapshots that guarded the
  uncommitted overnight phase were removed as superseded by the branch
  history.)
- Everything flag-gated: the pinned session lane stays the default; the
  stateless lane activates per-thread via `threads.execution_lane='stateless'`.
- Local verification on k3d is the gate for each milestone (CLAUDE.md workflow).

## Scope ruling for the night

S1 in full (doc §9) is weeks. Tonight builds the **S1 spine** end-to-end for
lite/virtual sessions, in dependency order, quality-first:

| Milestone | What | Status |
|---|---|---|
| M0 | Baseline: cluster health, git state, plan; pinned-lane e2e baseline | DONE |
| M1 | `run_queue` substrate: migration 0115, shared claim/heartbeat/fence/complete module in `src/shared/run_queue/`, real-Postgres tests | DONE (73 real-PG tests green; 0115 applied on k3d) |
| M2 | Epoch/seq redesign (§5.3.2): DB-seeded seq, epoch bump only on steal/rewind, fenced journal writer, system-writer class | DONE (live-verified: epoch-stable reattach at seq 547) |
| M3 | Stateless turn executor for lite sessions: claim loop reusing pool attach/detach + one-turn drive; scrub-on-claim | DONE (26 tests + 508-test adjacent sweep) |
| M4 | Orchestrator control plane: enqueue-on-input, claim-bundle endpoint, leader reaper (per-row, `turn.interrupted` system frames), lease read-model | DONE (claim-bundle probed live; reaper leader-gated live) |
| M5 | Helm: `agent-stateless` Deployment (distinct class labels), Tilt wiring | DONE (2/2 Running on k3d; selector non-overlap proven) |
| M6 | k3d e2e + fault injection (steal mid-turn, zombie fence reject, watermark no-double-answer, FIFO drain) + docs refresh | DONE (7-point matrix PASS; final gate 539+73 tests green) |

The night delivered the full spine; every stage is flag-gated
(`agent.stateless.enabled` chart default OFF; lane per-thread via
`threads.execution_lane`, default `'pinned'`). The pinned lane's only behavior
changes are deliberate improvements: epoch-stable reattach (no more
cache-wipe cascade) and the pop-first embedding-env scrub (closes a live
cross-tenant residue).

Explicitly deferred from S1 scope tonight (recorded, not forgotten — each has a
doc anchor): control-verb REST subset (§6.7), media fidelity (§5.3.6), Path-A
summary persistence (§5.3.3 — small, slot in if time allows), permission-row
retire on lease expiry, queued-turn UX frame, journal coalescing tick (§5.5),
metering shadow ingestion (§7), per-user `fair_key` round-robin beyond the
claim ORDER BY (the column ships now; the CTE can follow), loaded-bench
acceptance runs (need the bench rig).

## Decisions taken tonight (with reasons)

1. **`threads.execution_lane`, not `runner_kind`.** The doc's §5.4.4 says
   "partition on `jobs.runner_kind`, which exists" — verified: it exists but
   carries *grant* semantics (`user|lifecycle|service`, owner-capability
   classes; see column comment in schema_current.sql:5365). Overloading it
   would tangle dispatch grants with runtime class. New column
   `threads.execution_lane TEXT NOT NULL DEFAULT 'pinned'` (`pinned|stateless`),
   app-validated (no CHECK on the existing table → avoids the squawk 3-file
   split). The jobs-side partition column is an S3 decision — flagged for the
   doc.
2. **Seq becomes DB-seeded, not DB-allocated-per-event.** At attach/claim the
   writer seeds `_next_seq` from `SELECT COALESCE(MAX(seq),-1)+1 FROM
   thread_events WHERE thread_id=$1 AND epoch=$2` (index-backed by the UNIQUE
   (thread_id, epoch, seq) index, migrations/app/0004:60). Gap-free
   (client-friendly), zero per-event cost, sync stamping in `_broadcast`
   preserved. Correctness backstop: the UNIQUE index + the fenced writer make
   any writer race loud instead of silent. The doc's alternative (block
   allocation on the threads row) is not needed at S1 concurrency. To verify
   in M2 before relying on it: cockpit seq-gap/contiguity assumptions in
   `persistent-chat.service.ts`.
3. **Epoch bump becomes conditional.** Clean attach re-uses the current epoch
   (seq continues); bump remains only for: reaper steal (M4), rewind (shipped
   path, gains seq-allocator init per §5.3.2.5), and a safety fallback if the
   MAX(seq) seed read fails. No cockpit change expected — the client cascade
   only fires on a bump, so the pinned lane gets quieter too (pod
   recycle/reattach no longer wipes IndexedDB).
4. **Executor reuses the pool-mode attach/detach machinery** (persistent_app
   already supports attach/detach in one process — `POST /session/attach`
   internals). The claim loop feeds claimed input through the existing
   `_loop_user_queue` and completes the unit at the existing turn-complete
   hook. Soft affinity v0: keep the last session attached; claim prefers its
   thread; detach only on a different-thread claim.
5. **Tests run against real Postgres** (k3d instance, scratch database),
   gated by a `RUN_QUEUE_TEST_DSN` env var (skip when absent, so CI stays
   green). Mock-based tests are worthless for SKIP LOCKED / FOR SHARE
   semantics.
6. **Fence helpers live in `src/shared/run_queue/`** (importable by both
   orchestrator and agent — the `orch_surface` precedent). Persist-time fence
   activates only when a lease context is set; the pinned lane passes no lease
   and keeps today's behavior. Exception applied to both lanes: the journal
   writer's epoch-current guard, a strict improvement (kills the
   stale-writer-inserts-into-dead-epochs-forever hole).

## Milestone log

### M0 — baseline (done)
- Cluster healthy (all pods Running), Tilt serving on :10350.
- Working tree at `30e154bc` + uncommitted: the two feature docs + this
  research folder + untracked `HomeLab/` (not touched tonight).
- Latest app migration: `0114_compute_interval_epoch_shape_repair.sql` → tonight
  uses 0115+. Vector migrations untouched tonight.
- Baseline diff snapshot: taken (working artifact; superseded by branch history).

### M0b — pinned-lane baseline e2e on k3d (done, PASS)
Thread `9a756800` (lite/virtual, pinned lane): create→accepted sub-second;
create→active ~15 s (provisioner pod `srw-agent-s-ba866ef1`); input accepted
(`turn_id` 1, agent-side `queue_depth` 1); answer `BASELINE-OK` landed ~2–3 min
after input, dominated by attach-time workspace seeding (skills scaffold +
16-file cloud sync) — the §5.3.3 attach cost, live. Facts for later
milestones: thread_messages roles are LangChain vocabulary (`human`/`ai`) —
the M4 enqueue persist and M3 unanswered-message query filter on
`role='human'`; messages REST is
`GET /api/persistent/threads/{id}/messages?limit=N`.

### M1 — run_queue substrate (done)
Spec handed to the implementation agent: doc §5.1 + §5.2 verbatim (schema,
claim CTE, heartbeat, per-row reaper, completion protocol, attempt semantics,
dedup queued-only) + `web_pg-queue-lease.md` as reference + house patterns
(`datasource_reconciliation.py`, `lock_ids.py`). Deliverables: migration
0115 (+ execution_lane column), `src/shared/run_queue/`, real-PG test matrix,
squawk probe, `scripts/schema-snapshot.sh` regen (mandatory memory rule),
local test run against k3d Postgres (scratch DB).
Delivered: **73/73 real-PG tests green** (scratch `run_queue_test` on the k3d
instance; skip-clean without the DSN); squawk v2.59.0 zero issues
(`enqueue_ord` as IDENTITY per `prefer-identity`); snapshot regenerated via
from-zero replay, `--check app` OK; 0115 applied on k3d by the orchestrator's
own `run_migrations()` (Tilt rebuild). Notable agent deviations, all adopted:
queued-merge `run_after` uses LEAST (new input must cut an error backoff, not
extend it); `queued_at` reset on release/steal too (round-robin per §5.1);
attempts counted at claim only; reaper candidate scan is FOR SHARE SKIP
LOCKED (FOR UPDATE candidates would *skip* fence-held rows — steals must
block behind persists, wedged rows must not stall the sweep; both proven by
tests). Post-M6 substrate fix folded in: creation initializes
`consumed_seq = input_seq - 1` (see M6 finding 1).
Results: _pending_.

### M2 — epoch/seq/system-writer (in progress)
Pre-spec code audit (done inline, main context) found two constraints the doc's
§5.3.2 sketch didn't know about:
1. **The cockpit's resume guard depends on epoch bumps.**
   `_isSupersededLifecycleFrame` (persistent-chat.service.ts:1722) swallows
   terminal lifecycle frames (`session.idle_timeout`/`session.ended`) with
   `epoch <= resumedFromEpoch` — the mechanism that stops a resume's tail
   replay from pinning "ended" UI over a live session. Under
   never-bump-on-attach, a *resumed* session's genuine future terminal frames
   would be swallowed forever. Rule shipped: **bump iff the current epoch
   already carries a terminal lifecycle frame, or the thread is in a terminal
   status at attach-read** (covers pruned-frames case); clean mid-life
   reattaches (pod recycle, drain handoff, pool detach/reattach) reuse the
   epoch. Zero cockpit changes needed.
2. **Retention pruning breaks a bare MAX(seq) seed.**
   `thread_events_prune_sweeper` (main.py:30937) deletes by age (24 h ended /
   7 d active) *within* the current epoch, so MAX(seq) can shrink below cached
   client cursors. Added `threads.events_seq_hwm` (last-used seq, migration
   0116): writer flush maintains it via GREATEST in the same statement; attach
   seeds `_next_seq = GREATEST(hwm, COALESCE(MAX(seq),0))`; epoch bumps reset
   it to 0 atomically.
Also settled: the system-frame helper (`src/shared/event_journal/`) allocates
(epoch, seq) from the hwm in one CTE and is restricted tonight to
writer-excluded contexts (post-steal, post-park, rewind) — a live-epoch
system write (e.g. outbox `title.updated`) would race the in-memory counter;
that generalization is an S2 refinement, noted as deferred.

**Code complete (agent run, ~23 min): 0116 migration (final-form, unapplied),
`src/shared/event_journal/` (`append_system_frame`, `bump_epoch`),
persistent_app rewrite (tuple-returning conditional resolver,
`_bump_event_journal_epoch`, fenced single-statement writer flush with
writer-owned epoch + hwm GREATEST, fence-out/mixed-epoch → terminal + writer
stops, rewind = drain→bump→new-writer swap). 23 new tests green;
`test_persistent_app.py` 280 passed; SQL semantics validated on a throwaway
pg15 container.** Deviations adopted (all sound):
1. **Third bump condition — beyond-retention**: `/resume` flips
   `ended→created` BEFORE attach (main.py:29877), so once the 24 h prune has
   removed terminal frames both settled signals are gone; rule adds "non-virgin
   epoch with zero surviving rows → bump" (protects the resumedFromEpoch guard
   AND pre-0116 hwm=0 backfills). Virgin first attach still reuses.
2. Rewind swaps the writer (old writer would fence itself out post-bump).
3. Probe-failure fallback covers the EXISTS read; fence-stop on partial
   insert (`inserted < len(batch)`), not just zero.
Consumer audit: SSE/pruner/cockpit compatible; flagged for later lanes —
`apply_thread_rewind` (orchestrator detached rewind) predates 0116 and should
adopt the shared helpers (folded into M4's brief); `session_wake._deliver_durable`
comment stale (S2); cockpit cosmetic: 'suspended' status label can linger
until next meta load under reuse (S2 candidate: reset on `turn.started`).
Fallout owed by integrator (me): `tests/test_thread_events_phase2.py` (6) +
`tests/test_rewind_handler.py` (1) assert the OLD contract — updating next.
**Live k3d verification PASSED** (thread `bf11169e`, fresh pods on the
rebuilt image): virgin attach → `Reusing events_epoch 0 (seed 0)`; turn 1
pushed hwm to 14 (writer in-statement maintenance live); pod deleted → epoch
stable through a 4.5-min graceful teardown (hwm 547 — the dying pod streamed
turn 2 through its grace period, itself a nice specimen of the pinned-lane
fragility class); resume-from-ended → fresh pod logged `Reusing events_epoch 0
(seq_seed=547 hwm=547 max_seq=547)` — a fresh process continuing another
pod's journal, zero client cascade. Epoch 0 carried no terminal lifecycle
frames (pod-delete teardown doesn't write session.ended), so reuse was the
correct branch there; the bump-on-terminal-frame branch rests on the 23 unit
tests. Incidental find while driving the flow: `/resume` 409s any status but
'ended' with the message "Thread is already <status>" — a dead-pod
`awaiting_user` thread is unreachable by both `/input` (agent unreachable)
and `/resume` (409) until something flips its status; queue-lane threads
never enter that trap (no pod to die). Worth a line in the doc's §2
bug-graveyard list.

### M1/M2 integration (done, integrator)
- 7 stale-contract tests updated to the new contract
  (`test_thread_events_phase2.py` 6: resolver tuple + writer epoch kwarg +
  fetchval fenced flush; `test_rewind_handler.py` 1: rewind's order-spy now
  patches `_bump_event_journal_epoch`). 72/72 green across the three journal
  test files; ruff clean.
- Migrations 0115+0116 live on k3d (Tilt rebuild applied both:
  "schema up to date (116 applied)"); `schema_current.sql` regenerated covers
  both — **stage 0115 + 0116 + snapshot together when committing** or the CI
  drift gate fails.
- Patch snapshot: superseded by branch history.
### M3 — stateless turn executor (done; e2e in M6)
- New `src/api/turn_executor.py` (`StatelessTurnExecutor`, strip helper,
  attach fingerprint) + `src/api/lease_context.py` (mutable `LeaseHandle` —
  a snapshot tuple would pin the first claim's token across affinity
  re-claims; fenced writers read the handle at write time) +
  `orchestrator_client.get_claim_bundle()` (pinned contract, X-Internal-Key).
- persistent_app: `_turn_complete_external_hook` seam (wrapper + finally, so
  teardown races can't strand the executor's wait); writer `lease=` param
  filling M2's fence slot — **token-equality without `state='leased'`** so
  trailing flushes between `complete_unit` and the next claim still land
  (stragglers cut by the next claim's token bump, §5.2); stateless lifespan
  branch (no registration/heartbeat/watchdogs/officer boot-wake); all
  direct-input surfaces 409 in this mode; `/ready` = claim loop alive.
- postgres_db: `save_thread_message(s)` open a fenced transaction only when a
  lease context is active (torn-turn invariant comment lives there); pinned
  lane byte-identical.
- **Scrub-on-claim shipped as pop-first `_apply_session_embedding_env()`** —
  also fixes the live pinned-pool leak (§5.6). Deviation 7: removes the
  boot-env fallback for env_keys-less sessions — safe on k8s (orchestrator
  injects per-thread), affects only compose-era dev.
- Notable deviations: complete-failure = retry 3× then LET THE LEASE EXPIRE
  (release after a persisted answer ⇒ double answer — documented §5.2
  residual); stateless teardown never marks thread status / never
  idle-archives (orchestrator owns status; a pod-side 'ended' would force an
  epoch bump on next claim and break "0 bumps per clean handoff");
  strip-restored-pending matches by CONTENT tail-to-tail (restore mints fresh
  uuids — ids don't survive; id-match kept as future-proofing).
- Tests: 26 new + adjacents, combined **508 passed**; ruff clean.
- Boot: `python agent.py --mode stateless` (sets STATELESS_EXECUTOR=1);
  probes `/health` (mode tag) + `/ready` (claim loop); greppable: `run_queue
  claim|complete|release`, `lease lost`, `fence rejected`, `session reuse
  (affinity)`.
### M4 — orchestrator control plane (done; live-verified)
- **Enqueue-on-input**: `thread_input` branches on `execution_lane` before agent
  resolution; `_thread_input_stateless` runs ONE transaction: minimal
  thread_messages INSERT (id via the agent's own `_coerce_row_id(msg_…)` import
  — byte-identical rows, no drift), threads activity/turns bump,
  `record_input_seq` (sole admission call — its UPSERT semantics cover
  create/revive/merge/leased-watermark/parked). Response parity with a
  `"queue"` object replacing `"agent"`. Per-turn in-process lock skipped for
  the lane (replica-unsafe anyway). `thread_interrupt` → 501 on the lane (S1
  deferral).
- **Claim-bundle** `GET /internal/units/{unit_id}/claim-bundle?lease_token=`:
  X-Internal-Key + live-lease SELECT (watermarks in the same read); 401/403
  generic/404/409 taxonomy; attach payload from the factored
  `_assemble_session_attach_payload(thread_id, *, config_override, config_name)`
  — shared with `_send_session_attach_locked`, ALL fail-closed rules verbatim
  (`test_session_config_plumbing.py` untouched green). **Probed live on k3d**
  with a throwaway leased row: all four auth/error paths + 200 with real
  `resolved_config`.
- **Reaper** `orchestrator/services/run_queue_reaper.py`: own advisory lock
  (`RUN_QUEUE_REAPER_ID`), 15 s tick / 30 s grace, per stolen session unit one
  transaction `bump_epoch` + `turn.interrupted|turn.parked` system frame;
  non-session kinds reap-only; error-contained. Wired in lifespan; observed
  `run_queue reaper started/stopped` on the live cluster.
- **Rewind port**: `apply_thread_rewind` → shared `bump_epoch` +
  `append_system_frame`; closes M2's hwm-not-reset audit item.
- **Admin**: `GET /api/admin/run-queue` + `POST …/unpark` (existing
  `_require_admin` pattern); `docs/security/endpoint_inventory.txt`
  regenerated (+2 rows, drift-gate requirement).
- **Provisioning gate: deliberately NOT implemented** — 3 gate points, one in
  `orchestrator/routers/sessions.py::_do_prepare` (~:210-288, decision-level,
  needs the lifecycle-`ready` design choice = cockpit workstream); half-gating
  would leave `/prepare` still booting pods. Locations recorded in M4's
  report; `session_wake` verified provision-free (no gate needed). Tonight's
  e2e flips detached threads manually.
- Tests: 20 new green + 223 neighbors green; ruff clean.
- **M6 operational notes**: greppable `"run_queue steal:"`, `"run_queue
  enqueue:"`, reaper leadership lines; flip lanes ONLY on detached threads
  (`agent_id IS NULL`); do NOT drive `/resume`//`/prepare` on stateless
  threads until the F gates land; steal fires at `leased_until`+30 s on a
  15 s tick.
### M5 — deployment (done; pods pending M3's mode)
`helm/templates/agent/stateless-deployment.yaml` (gated `agent.stateless.enabled`,
default OFF; values-local ON — gitignored, prod untouched), mirrors the
provisioner pod spec (envFrom srw-config, MCP_INTERNAL_KEY, POD_UID, emptyDir
workspace, same securityContext/probes); labels `srw/class: agent-stateless` +
`app: srw-agent-stateless` with live-verified NON-overlap against every
manager selector (reap_pods, warm pool, lifecycle reconciler, per-session
lookups, agent Service/PDB — endpoints stayed `<none>`). DB NetworkPolicies
extended (gated) for the class; egress NP extended. `app` deliberately NOT
`srw-agent`: metering's `classify_product_pod` would flag
`dynamic-agent-identity-conflict`; the class reads as "shared platform
capacity" until §7 lease-interval attribution. Reloader annotation added
(static Deployment + envFrom would otherwise hold stale config forever).
Tilt: zero changes — the release's `image_deps` already rolls the Deployment
on agent-image rebuilds (verified live with a tilt-tagged image).
Current state: `deploy/srw-agent-stateless 0/2` CrashLoop on
`invalid choice: 'stateless'` — expected until M3's mode lands; self-heals on
the next rebuild. **S2 residue flagged**: workspace-pod NetworkPolicy admits
only `srw-agent|srw-persistent-agent` — stateless class can't reach workspace
SSH/CDP until extended (fine for S1 lite); no vm-ssh-key mount, no tailscale
sidecar (§5.8 capability variant later).
### M6 — end-to-end + fault injection (done — all correctness criteria PASS)
Test thread `9a756800` (lite/virtual, flipped to `execution_lane='stateless'`
detached, per M4's safety rule). Full matrix, all live on k3d:

1. **Happy path** — PASS, after finding+fixing a real substrate bug my spec
   caused: the first claim on a NULL `consumed_seq` treated the thread's
   entire pre-queue history as pending and re-answered an already-answered
   message (observed live as a duplicate `BASELINE-OK`). Fix: row creation
   initializes `consumed_seq = input_seq - 1` (the lane-flip boundary) in all
   three INSERT paths of `src/shared/run_queue/queries.py`; 73/73 real-PG
   tests re-run green (2 assertions updated to the new invariant). The system
   self-converged even with the bug — completion-requeue marched consumed
   forward — which is itself evidence the lifecycle degrades safely.
2. **Cross-pod clean handoff** — PASS: pod deleted between turns, different
   pod claimed, epoch unchanged, seq continued (the §5.2 client-invisibility
   claim, live).
3. **Steal mid-turn (force-kill during generation)** — PASS: kill −9 during
   the story turn; reaper stole at **t+88 s** (≤ ~105 s bound), token 6→7,
   **epoch bump + `turn.interrupted` frame at (epoch, seq 1)**; replacement
   pod re-claimed at t+93 s and regenerated the story; **exactly one final
   answer** (partial from the killed pod never persisted; watermark held).
4. **Queued input FIFO across the steal** — PASS: two messages sent into the
   dead window (`input_recorded` on the leased row — state untouched);
   after recovery, story → DRAIN-A → DRAIN-B answered in seq order across
   three claim cycles (completion-requeue), final consumed == input.
5. **Zombie fence** — PASS in two acts. Long freeze (cgroup freezer, ~4 min):
   kubelet's liveness budget killed the frozen pod → restart clean — the
   realistic production arc (zombies mostly die before they can write).
   Short freeze (thaw right after the steal, inside the liveness budget):
   the thawed zombie's first heartbeat logged
   `lease lost: … (heartbeat renewal found no leased row)` and aborted
   politely — the designed fast path; the persist-time fence behind it is
   substrate-proven (M1 real-PG tests: FOR SHARE blocks steal until commit;
   stale token rejected). No zombie row landed in any scenario (answer
   uniqueness verified by content-marker counts: story/fable/raven = 1/1/1).
6. **Skip-if-answered** — PASS: done row force-requeued with
   consumed==input; claimed and completed `(skip-if-answered)` in <8 s, no
   LLM call, watermark untouched.
7. **Scrub-on-claim** — unit-test matrix only (A→B env/singleton assertions);
   a live cross-tenant probe needs a second user identity — not run tonight.
Epoch audit across the whole night: 0→1 (terminal frames from the idle-reaped
pinned life — the resume-guard branch, correct), then exactly +1 per steal
(→4 after three steals), **zero bumps across every clean handoff** — the S1
acceptance line holds.
Admin read model: guard verified live (`Admin access required` for the
non-admin test user); the 200 path rests on unit tests.

**Performance findings (the honest part — correctness shipped, latency owed):**
- Turn latency on this session_base thread: ~100–155 s claim→done, of which
  ~100 s is the FULL RE-ATTACH paid **every turn** — the affinity cache never
  hits because the attach fingerprint covers the whole bundle and the bundle
  carries volatile fields (per-attach-minted lite object-store credentials at
  minimum). Observed: same pod re-claimed 1 s after completing the prior turn
  and still re-attached from scratch. Decomposition of turn 5 (152 s): 58 s
  bundle+pre-epoch setup, 42 s attach/restore, 52 s actual turn.
- The §5.5 TTFT budget (p50 < 2 s) is nowhere near met for THIS config class;
  the "lite core ~50–200 ms" claim applies to a minimal config, not a
  session_base thread with KB/memory/skills/cloud scaffolding.
- Claim distribution across pods is racy (0.5 s poll loops), so even
  pod-side `prefer_unit_id` affinity often loses the race.

**S1 follow-ups (ordered by leverage):**
1. Affinity fingerprint over a STABLE config subset (exclude injected
   credentials — the live session holds working clients; rotation can ride
   the next cold attach) + short DB-side claim bias toward `last_pod`.
2. Attach-cost decomposition per §5.3.3's table on the lite path (the 58 s
   pre-epoch chunk: bundle fetch, datasource/KB init, skills seeding, cloud
   sync — measure and cache per-pod what §5.3.3 marks claim-scoped).
3. Provisioning gate (M4's three recorded locations) so created-as-stateless
   threads never boot a pod; then the cockpit `/connection`+`/prepare` compat.
4. Live cross-tenant scrub probe (second user); queued-turn UX frame;
   `turn.interrupted` cockpit rendering check (frame is in the journal —
   client rendering untested tonight).
- Final regression gate: **539 unit/contract tests + 73 real-PG substrate
  tests green; ruff check + format clean repo-wide.**
- Patch snapshot: superseded by branch history. (was 21.5 k
  lines, includes all new files).

## Doc deltas owed back to the feature doc (folded at end of night)
- §5.4.4: `jobs.runner_kind` is taken (grant semantics) — S3 partition needs
  its own column; §5.1/§9: `threads.execution_lane` is the S1 partition.
- §5.3.2: seq design — MAX-seed (gap-free) chosen over block allocation
  (update the alternative note once verified).
