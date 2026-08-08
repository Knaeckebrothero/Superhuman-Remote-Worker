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

### M2 — epoch/seq/system-writer (done)
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

---

# Session 2 — performance (2026-08-08, morning)

**Mandate:** "continue testing on k3d; the goal is to get everything faster and
more streamlined." The user's specific hypothesis: don't re-resolve the config
on every request — persist the resolved config and reload it, re-resolving only
when settings change.

**Result: 99.6 s → 5.4 s cold, 3.0 s warm** on the same thread and cluster.
The user's hypothesis turned out to be right about the *symptom* and wrong
about the *cost*, which is exactly why the first thing built was instrumentation.

## Method

Same loop every time, no exceptions: instrument → deploy via Tilt → drive a real
turn on k3d → read the numbers → change one thing → re-measure. Every claim in
this section is a log line from a running pod, not a reading of the code.

Two harnesses in the scratchpad (reusable):
* `drive_turn.sh "<message>"` — mints a fresh admin-cli `id_token` (they expire
  in ~15 min, so per-run), posts one message, waits for the `ai` row, then
  prints the timing lines from both planes.
* `burst.sh N` — fires N messages back-to-back and reports the drain: order,
  wall time, and which pod served each.

Instrumentation added (kept — it is how the next regression gets found):
* `turn timing:` in the executor — bundle / detach / attach / pending / turn /
  push / complete / total, tagged `mode=fresh|reuse`.
* `claim-bundle timing:` in the orchestrator — lease check / credential
  injection / assembly.
* `attach step:` and `setup steps:` — the phases inside attach and inside
  `PersistentSession.setup`.
* `pull detail:` / `push detail:` in cloud sync — list vs reconcile vs
  downloads, with counts.
* `affinity miss: ... changed_paths=[...]` — **key paths only, never values**
  (claim bundles carry credentials). This one line diagnosed the whole affinity
  problem in a single turn.
* rclone op tally (`N ops Xs [verbx36=Ys ...]`), drained per setup.

## What the numbers said

Baseline, turn 1 (cold): `bundle=0.08s attach=49.09s turn=42.41s push=8.01s
total=99.64s`.

**The config re-resolution the user suspected costs 70 ms** — 1 ms lease check,
10 ms credential injection, 60 ms assembly. A persisted-resolved-config cache
would have optimized 0.07 % of the turn. It was not built, and the doc now
records that with the measurement, so nobody re-proposes it from code reading.

But the hypothesis pointed at the right *place*. Re-resolution produced a fresh
`resolved_config.resolved_at` timestamp on every claim — and the attach
fingerprint hashed it. That single volatile field is why the warm-session cache
never hit. Excluding resolution metadata from the fingerprint is the cheap,
correct version of "load the same config instead of resolving it again."

Where the time actually was:

| Cost | Baseline | After | Cause |
|---|---|---|---|
| Attach-time cloud `pull_all` | 41 s | 0 s | Full remote-tree walk on the claim critical path, duplicating the turn-start pull seconds later |
| Teardown cloud `pull_all` | 41 s | 0 s | Same walk again on claim-switch, refreshing a workspace whose next consumer pulls first |
| Remote tree listing | 39.9 s | 0.55 s | `webdav3.list()` runs a probe PROPFIND before the real one, ~2.5 s/dir; one `Depth: infinity` PROPFIND returns the tree in ~1 s |
| Backend walk + stats (push) | 7.8 s | 0.20 s | Per-file `stat` over an object store; the virtual backend can return the whole tree with sizes in one op |
| Session setup | 7.2 s / 51 rclone spawns | 1.5 s / 13 | 36 of the 51 were listings — setup asking "does this exist?" about one small tree |

## Changes, in the order they were made and measured

1. **Skip the attach-time and teardown cloud pulls in stateless mode.** Both
   duplicate a pull that runs anyway. Broken-mount surfacing moves to the
   turn's `_resilient_cloud_sync`, which already broadcasts
   `workspace_sync.error` and flags degradation — same operator visibility,
   one walk instead of three. The pinned lane keeps both (its workspace can be
   browsed after detach without another attach ever running).
   → 99.6 s → 59.3 s.

2. **`Depth: infinity` PROPFIND** as an optional transport primitive
   (`_list_remote_tree_fast`, default `None` = "walk as before"). Nextcloud
   gates the feature behind `dav.propfind.depth_infinity`; any non-207 flips a
   per-instance flag and the per-directory walk takes over permanently, so an
   unsupported server pays one probe, once. Plus parallel stat batches and a
   bulk `list_files_with_sizes` on the virtual backend (one rclone spawn
   instead of one per file).
   → 59.3 s → 11.0 s.

3. **Fingerprint excludes `resolved_config.resolved_at`.**
   → warm reuse becomes *possible*.

4. **Affinity grace (migration 0117).** Possible wasn't enough: both pods poll
   at ~0.5 s, so the cold one won about half the races and paid a full attach.
   `run_queue.last_leased_by` records the last holder; the general claim hides
   a freshly queued unit from everyone else for `AFFINITY_GRACE_SECONDS` (2 s).
   The executor also refuses poll backoff while warm, so it is always in the
   fast cadence to spend its head start, and drops a warm session after 300 s
   idle so a pod doesn't hold clients for a thread nobody is using.
   → warm turns 3.0 s, `attach=0.00s`, three consecutive turns on one pod.

   Two failover rules fell out of writing the tests, and both are now in the
   SQL: **release** and **reaper steal** clear `last_leased_by`. A pod that
   said it cannot serve, or that missed its heartbeats, must not make every
   other pod wait out a grace window before taking over.

5. **Scoped metadata index on the virtual backend.** Every metadata probe there
   is an rclone process spawn. `begin_read_cache()` / `end_read_cache()` build
   a key→size map in one op and serve `is_file`/`is_dir`/`list_dir`/`walk`/
   `stat` from it. Deliberately **not ambient**: setup opens it and a `finally`
   closes it, so tool work never reads through a cache and no caller has to
   reason about staleness. Local mutations update the map in place (the backend
   knows exactly which keys moved), so it stays exact inside the scope. Also
   made `mkdir` skip re-writing markers that already exist — scaffolding
   recreates the same directories every attach, 9 spawns for nothing.
   → cold attach 7.4 s → 2.2 s; cold turn 5.4 s.

## Drain-in-lease: designed, measured, not built

The user's second idea — let the pod answer several queued messages under one
lease instead of releasing and re-claiming. With affinity working, `burst.sh 3`
drains 3 messages on one pod in 11 s, every turn `mode=reuse` with
`attach=0.00s`, FIFO preserved. The executor re-claims *immediately* after
completing (the loop only sleeps when the queue is empty), so the sole cost
drain-in-lease removes is the claim-bundle fetch: **0.05–0.09 s of a ~2.9 s
turn**. A second completion path through the exactly-once core is not worth
2–3 %. Recorded rather than discarded — S3's worker batches face the same
question with a very different ratio.

## Fault matrix re-verified (the claim SQL changed, so the proofs had to be redone)

* Pod force-deleted mid-turn → reaper steal at **t+97 s** (TTL 60 + grace 30 +
  tick, inside the ≤105 s bound) → answer regenerated **exactly once** →
  `events_epoch` +1 → `last_leased_by` NULL after the steal → attempts reset to
  0 on completion.
* An accidental fault gave a free extra test: a partially-built image (see
  below) made attach raise on every claim. The unit released, backed off
  linearly, retried 13 times without ever double-answering, and completed
  cleanly the moment the fix landed — the release/backoff/retry path working on
  a real bug rather than an injected one.
* `kill -9 1` inside the container **does nothing** — a container PID 1 with no
  handler is protected from in-namespace fatal signals. Use
  `kubectl delete pod --force --grace-period=0` (or the cgroup freezer, as in
  session 1). Both sessions independently rediscovered this; it is now a comment
  in `kill_test.sh`.

## Traps hit (both already in memory, both hit anyway)

* **Tilt builds partial edits.** Twice a build was triggered mid-edit and
  produced an image with a call site but not its method definition
  (`'PersistentSession' object has no attribute '_end_backend_read_cache'`),
  and once with only 2 of 4 log markers. `updateStatus: ok` is not evidence.
  The reliable check is `kubectl exec <pod> -- grep -c <marker> <file>` on
  **every** running pod before trusting a measurement.
* **`git checkout` while Tilt is up is a DEPLOY of that branch.** Checking out
  `develop` for a few seconds — to confirm the eleven suite failures were
  pre-existing — was enough for Tilt to catch the swapped tree and build
  `srw-agent:tilt-978bb3da`, which then crash-looped with
  `agent.py: error: argument --mode: invalid choice: 'stateless'`: develop's
  `agent.py` under the feature branch's Deployment spec. Confusing because the
  code you are reading is correct; the tell is mismatched `tilt-` image tags
  across ReplicaSets. Caught in the closing health sweep and rebuilt. To test
  "was this failing before?", stash only the specific new file
  (`git stash push -- <path>`) or use a worktree outside Tilt's watch root.
* **admin-cli `id_token` expiry** (~15 min) shows up as a silent 401 and a turn
  that never appears. `drive_turn.sh` mints one per run.
* **A container's PID 1 ignores in-namespace SIGKILL.** `kubectl exec … kill -9 1`
  silently does nothing, which reads as "the fault injection ran and nothing
  broke". Both sessions rediscovered this independently; now a comment in
  `kill_test.sh`. Use `kubectl delete pod --force --grace-period=0`, or the
  cgroup-v2 freezer for a zombie (not a death).
* **Optimization outran the fault harness.** The first two kill runs proved
  nothing because turns now finish in <5 s — the pod died after the answer was
  already persisted. The steal test needs a deliberately long generation and a
  kill fired the instant the claim appears.

## Test posture

* `tests/test_run_queue.py` now applies **0115 + 0117** (the harness applied one
  migration file; a column the SQL contract depends on could have passed here
  and failed on a real cluster). 8 new affinity tests → **81 real-PG tests**.
* `tests/test_virtual_workspace_backend.py` gained 11 scoped-index tests: the
  index must be invisible (identical answers to the uncached backend), exact
  across local mutations, gone when the scope closes, and prefix-isolated —
  plus an op-counting store proving 30 probes cost one listing and zero heads.
* Two pre-existing run_queue tests changed behavior legitimately (a *different*
  pod claiming immediately after a completion is now graced). Both were updated
  to opt out of the grace with a comment pointing at `TestAffinityGrace`, rather
  than weakening the new contract to keep them green.

**Full-suite gate: 15 013 passed, 107 skipped, 12 failed** (`pytest tests/`),
`ruff check` + `ruff format --check` clean across `src/ orchestrator/ tests/`.
Eleven of the twelve failures reproduce on `develop` (Python 3.14 environment
noise — `mcp_manager`, arxiv/semantic-scholar package contracts,
`test_connect_disconnect`), consistent with the standing note that the local
baseline is not zero.

The twelfth was real and **pre-existing on this branch**:
`test_migration_heads_are_unique_and_snapshots_are_not_the_contract` pins
`APP_CURRENT_MIGRATION_HEAD`, which session 1 left at `0114` when it added
0115 and 0116 — so that tripwire had been red since the spine landed and the
night ended without noticing. It is now `0117` with a comment saying what the
assertion is for (a migration landed → check the snapshot was regenerated and
nothing was renumbered). Verified pre-existing by stashing *only* the new
migration file and re-running, rather than by switching branches — see the
trap below for why that distinction cost an hour.

## Commits (branch `feature/stateless-agents`, not pushed)

| | |
|---|---|
| `c290f525` | `feat(db, run_queue)` — 0117 affinity grace, `last_leased_by`, cleared on release + steal; harness applies 0115+0117; 8 affinity tests; migration-head pin fixed |
| `3a1a475c` | `perf(cloud-sync)` — `Depth: infinity` PROPFIND primitive with capability fallback; bulk/parallel stat batches; pull/push detail logging |
| `66806b7f` | `perf(virtual)` — scoped metadata index, idempotent `mkdir` skip, rclone op tally, 11 contract tests |
| `a2e7fa8d` | `perf(agent, orchestrator)` — fingerprint drops `resolved_at`, duplicate cloud pulls removed on the lane, warm-poll cadence + warm TTL, all instrumentation |
| `6a360848` | `docs` — feature doc v3.2, this log, BACKLOG session entry |

Session 1's five commits (`e506fdfa`→`af5c38a0`) precede these; ten total on
the branch, `develop` untouched.

## Final state on the cluster

Two `srw-agent-stateless` pods Running on the branch image, chart flag still
default-off (only the gitignored `values-local.yaml` enables it). Post-commit
verification turn: `mode=fresh bundle=0.07s attach=2.40s turn=2.26s push=0.04s
total=4.78s`. The `stateless-night-baseline` thread
(`9a756800-1ad2-4ef1-9e39-14ac8b1c312c`) remains the test fixture; it is the
only thread on `execution_lane='stateless'`.

## Where the whole feature stands

Rolled up into `docs/features/stateless_agents.md` **§9.1 Implementation
status**, written against the code rather than intent. Headline for anyone
picking this up: the shared substrate (`run_queue` + lease/fence/completion +
reaper) is built and genuinely kind-agnostic, so workers need no schema change
and no new lease semantics — but only the SESSION driver exists. Nothing
enqueues or claims a `worker_batch`; `jobs` has no `execution_lane`; job
dispatch is still the legacy `JobStartRequest` POST. S2 is untouched. Within
S1 the spine is proven and the surround is not: cockpit `/connection`+`/prepare`
compat, the control-verb REST subset, the provisioning gate, permission-row
retire, queued-turn UX, `fair_key` rotation, the lite agent-local-state
inventory, the coalescing tick, metering ingestion and object-store PUT fencing
are all open, and four acceptance criteria (TTFT, claim-wait under concurrency,
prompt-cache reuse, live tenant-residue probe) have never been measured.

**Correction to an earlier claim in this file:** a first pass at that status
said Path-A resume-compaction persistence was already implemented. It is NOT.
The grep behind that claim matched `ensure_within_limits(trigger="resume")`,
not `_record_compaction(trigger="resume")` — Path B persists
(`persistent_app.py:6552`), Path A calls `ensure_within_limits` at `:6441` and
returns at `:6473` without persisting, and the comment at `:6543` states that
deliberately (it avoids a banner double-render). So the §6.5 prerequisite
stands, and on the stateless lane it is the one live functional bug in the
shipped path: any thread that has ever compacted takes Path A, and if its
post-boundary tail is over budget it pays a blocking aux-LLM summarization per
CLAIM and discards it. Invisible in this session's numbers because the test
thread's tail is under budget. Caught by a second audit pass whose whole job
was to distrust the first — worth repeating on anything of this shape.

## Follow-ups this session leaves

1. The remaining 13 setup store ops: 3 reads + 3 writes + 3 deletes + 4 listings.
   Worth one more look at what is being deleted and rewritten every attach.
2. `AFFINITY_GRACE_SECONDS` is a constant; if pods ever poll on a different
   cadence it should derive from the poll interval.
3. The warm-session TTL (300 s) is unmeasured — no data yet on how often a
   session is re-claimed after >5 min idle.
4. Session 1's follow-ups still stand: provisioning gate, cockpit `/prepare`
   compat, live cross-tenant scrub probe, interrupt-on-lane (currently 501).

---

# S3 implementation log — worker jobs on the stateless lane (2026-08-08)

Working branch: `feature/stateless-workers-s3`, created directly from
`feature/stateless-agents` at `3802d0e2`. No push. The only pre-existing
working-tree entry was the untracked nested `HomeLab/`, which remains untouched.

## Phase 0 — orientation and pre-change baseline (DONE)

- Read `codex_s3_brief.md` end-to-end and followed its prescribed reading order:
  design §9.1 first, then §5.1/§5.2/§5.4/§5.4.4/§5.8/§9, this implementation
  log, the `run_queue` contract docstring, and the complete session executor.
- Cluster baseline: Tilt resources `(Tiltfile)=ok`, `srw=ok`; stateless agent
  Deployment `2/2`; both agent pods and the orchestrator pod Running.
- Verified the running image contents before measuring: both agent pods contain
  exactly one `turn timing:` marker in `/app/src/api/turn_executor.py`; the
  orchestrator image uses `/app/main.py` (not `/app/orchestrator/main.py`) and
  contains one `claim-bundle timing:` marker.
- Real session-turn baseline through `scripts/stateless-lane-probe.sh turn`:
  accepted as turn 73, answer observed in **7 s** wall clock. Agent timing was
  **4.62 s total**: bundle 0.10 s, attach 2.10 s, pending 0.00 s, turn 2.19 s,
  push 0.18 s, complete 0.05 s. Session setup was 1.99 s (13 remote-store ops,
  1.76 s). Orchestrator claim-bundle timing was **0.063 s**: lease 0.002 s,
  credentials 0.013 s, assembly 0.048 s.
- Branch safety: stopped the confirmed `tilt up` process with SIGINT, switched
  branches only after it exited, restarted Tilt, then re-verified `srw=ok` and
  the Deployment at `2/2`. No filesystem branch switch occurred while Tilt was
  running.
- Safety state: no `worker_batch` unit has been enqueued. Gate 1 and Gate 2 are
  still open and therefore continue to forbid a real worker claim.

## Phase 1 / Gate 1 — one claim authority per job (DONE)

Implemented migration `0118_jobs_execution_lane.sql`: `jobs.execution_lane`
is a NOT NULL, constant-default (`pinned`) runtime-plane class, deliberately
separate from `runner_kind` (`user|lifecycle|service` grant semantics). The
legacy plane now fails closed by admitting only `execution_lane='pinned'` in:

- `get_dispatchable_jobs`;
- `claim_job_for_agent`;
- all four mutation arms of `recover_orphaned_jobs`;
- `recover_expired_lease_jobs`;
- the same-host `register_agent` replacement recovery UPDATE.

The audit found two bypasses beyond the four named in the brief and they were
closed too: direct manual assignment now returns 409 for a stateless job, and
both direct `/job/start` and `/job/resume` helpers refuse a non-pinned row before
network I/O. `get_job()` was then found not to project the new lane; without
that fix the production guards would default every real stateless row to pinned
even though the mocks passed. It now returns `execution_lane`, with a real-DB
assertion for both values.

Migration discipline: app snapshot regenerated from zero; 0118 applied in
**7 ms** during snapshot replay and **8 ms** on k3d; `schema-snapshot.sh --check
app` is clean; the migration-head tripwire is 0118. Live DB reports default
`'pinned'::text`, NOT NULL. The running orchestrator image was checked directly:
six qualified sweep/re-registration predicates, one claim predicate, one
dispatcher predicate, two `get_job`/dispatch projections, and both direct helper
guards are present.

Verification:

- focused real-Postgres dispatcher/claim/manual suites: **33 passed** (including
  explicit stateless and unknown-future-lane fail-closed cases);
- full-schema k3d-Postgres sweep suite: **2 passed**, covering expired lease,
  all four orphan arms, and same-host registration replacement; pinned controls
  recover while stateless rows remain byte-for-byte owned by run_queue;
- schema replay/check: clean; targeted Ruff check/format: clean;
- post-change real session probe: answer observed in **7 s**, agent **4.87 s**
  total (bundle 0.08, attach 2.05, turn 2.48, push 0.25, complete 0.01), versus
  pre-change 7 s / 4.62 s. Orchestrator claim bundle was 0.070 s versus 0.063 s.
  This gate is off the session hot path; the 0.25 s agent delta is normal setup/
  provider variance, not attributed to the partition.

Failures recorded:

- first test invocation used nonexistent `venv/bin/pytest`; this checkout uses
  `.venv/bin/pytest` (Python 3.13) for the focused container suites;
- first Ruff invocation similarly assumed `.venv/bin/ruff`; repository-pinned
  Ruff 0.14.10 is installed at `/home/ghost/.local/bin/ruff`;
- first semantic sweep assertion expected zero recovered rows, but its own
  pinned NULL-lease control correctly matched orphan recovery. The test now
  asserts exactly one pinned recovery and separately proves all stateless rows
  remain untouched.

Rollout constraint: worker admission remains OFF until every orchestrator
replica runs these predicates. Before an orchestrator rollback, disable worker
admission, drain/fence active stateless work, and convert lanes only after no
worker queue row is runnable/leased. An old orchestrator cannot understand the
partition and would otherwise reintroduce dual execution.

Safety state: still no `worker_batch` unit has been enqueued. Gate 2 remains the
next mandatory prerequisite.

## Phase 2 / Gate 2A — freeze contract deployed orchestrator-first (DONE)

The freeze taxonomy now lives in the stdlib-only
`src/shared/job_freeze_types.py`. It exposes separate semantic subsets for
Continue-as-New, legacy auto-redispatch, coincident-error immunity, agent END
resume, and subjob redispatch rather than one over-broad allowlist.
`batch_boundary` is in every continuation subset, but remains absent from the
agent emitter in this rollout slice.

Orchestrator completion now resolves both `version_upgrade` and
`batch_boundary` to `paused`, including live-parent subjobs and reports carrying
a coincident interrupt error. The dangerous loop fallback is narrowed:

- a loop stop with no declared freeze still maps to `completed`, preserving the
  weak-model escape hatch;
- a loop stop with any unknown non-null freeze maps to visible
  `pending_review`, never `completed`.

The legacy paused-job sweep no longer duplicates freeze literals in SQL. It
binds the deterministic shared registry as `ANY($1::text[])`, still qualified
by `execution_lane='pinned'`. A real-Postgres semantic case proves a pinned
`batch_boundary` freeze is cleared/stashed while the same freeze on the
stateless lane remains untouched.

Verification:

- focused status/registry regression: **21 passed**;
- full drain + loop suites: **144 passed**;
- full-schema k3d-Postgres sweep suite: **2 passed** in 17.37 s;
- targeted Ruff format/check and `git diff --check`: clean;
- after Tilt rollout, the sole running orchestrator was inspected directly:
  `/app/services/completion.py` contains the unknown-freeze guard,
  `/app/database/postgres.py` contains the bound text-array predicate, and
  `/app/src/shared/job_freeze_types.py` contains the batch constant;
- an in-process assertion inside that running container proved loop +
  `batch_boundary` → `paused`, loop + unknown declared freeze →
  `pending_review`, and loop + no freeze → `completed`.

Post-slice session measurement (after verifying both running agent images still
contained the timing marker): turn 77 answered in **11 s**, agent **7.96 s**
total (bundle 0.08, attach 2.37, turn 5.30, push 0.19, complete 0.02), versus
Gate-1's 7 s / 4.87 s. Orchestrator claim bundle was **0.060 s** versus 0.070 s.
The 2.82 s increase is entirely in provider/turn execution; attach and queue
overhead remained close, so it is not attributed to the status-only change.

Failures recorded:

- the first real-Postgres invocation used the example `postgres/postgres`
  credentials, but the k3d cluster has a different role; the rerun consumed
  the existing Kubernetes Secret without printing it;
- the first semantic assertion treated asyncpg's unregistered JSONB result as a
  dict. This test connection returns it as text, so the assertion now decodes
  either representation before inspecting `last_freeze_data`;
- Ruff is not present inside `.venv`; as in Gate 1, the repository's installed
  Ruff binary is `/home/ghost/.local/bin/ruff`.

Rollout decision: Gate 2 is split deliberately. This orchestrator-first slice
is verified before any agent-side boundary code exists. Rollback must disable
batch emission first; worker admission remains OFF and no `worker_batch` unit
has been enqueued. Gate 2B (default-unarmed graph boundary and agent resume
contract) is next.
