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

# Session 3 — S1 completion pickup (2026-08-08)

Branch: `feature/stateless-sessions-s1-completion`, forked from
`feature/stateless-agents`; not pushed. The S3 worker branch was not used as a
base and no `worker_batch` unit was enqueued.

## Baseline and order

Read the completion brief in full, then the feature-doc sections, both prior
implementation sessions (including every trap), and `turn_executor.py` before
editing. Baseline focused server suite:
`tests/test_sessions_router_prepare.py` + resume neighbors = **93 passed in
0.53 s**. Tilt and the `srw` resource both reported `updateStatus: ok`, but the
orchestrator rollout was already unhealthy; see the operational failure below.

## Provisioning gate — DONE, focused tests + k3d verified

The brief named four entries (`resume_thread`, `_do_prepare`,
`_provision_agent_for_thread`, `get_connection`). Exact pinned whitelists landed
at all four, before provisioning mutations, with owner/auth checks retained
before lane-specific responses.

The call-site audit showed the brief's list was incomplete and an entry-only
check would still permit the same two-executor corruption:

- create-thread's fire-and-forget `provision_or_assign` refetch could attach a
  warm agent or spawn a pod after a lane change;
- resume's background `_reprovision` refetched under the advisory lock but did
  not recheck the lane;
- `_send_session_attach_locked` was the actual warm-pool delivery/bind boundary;
- a dedicated pod can start while pinned, remain `agent_id IS NULL`, then
  register after the row is flipped. `register_agent` was the authoritative
  late bind and had no lane check;
- permission-link wake and officer watchdog could recreate persistent pods.

All now whitelist exactly `'pinned'`. A final adversarial review caught that the
first warm-attach fix was itself unsafe: it delivered HTTP first, then on a
lost lane CAS called ordinary `/session/detach`. That endpoint marks the thread
ended, and the dual app returns from attach before its background setup exists
to detach. The first registration fix likewise upserted by hostname before its
lane check, then deleted the returned ID; that ID can identify a pre-existing
legitimate row. Neither version was committed.

The landed shape reserves warm ownership **before** HTTP with two CASes in one
transaction: agent must be ready/unbound/not working, and thread must be
pinned/unbound. After reservation, only HTTP 200 is confirmed acceptance.
Every non-200 response—including 409—and every transport exception retains the
reservation and returns a non-fallback outcome; stranding is recoverable,
provisioning a second executor is corrupting. This was a second adversarial
correction: `mark_stuck_session_agents_ready` can make a still-live pod look
idle during resume, and that pod legitimately returns 409 “already attached to
this same thread.” Releasing then would launch a rival. All three fresh-pod
fallback callers re-read lane and binding after a pre-reservation refusal.

Persistent registration now acquires the thread advisory lock and checks the
authoritative lane before any agent mutation. Hostname is not UNIQUE and is not
an ownership credential: a same-host restart targets only the exact
snapshotted owner (`expected_agent_id`); a genuinely new or different-host
replacement uses `insert_only` and cannot reuse an arbitrary hostname row; a
different live owner and a missing referenced owner fail before mutation. It
finishes with a checked lane-qualified CAS against the snapshotted
`thread.agent_id` (including NULL). That owner predicate is load-bearing: a
warm reservation does not take the registration advisory lock, so it can
otherwise land between registration's read and write and be overwritten. A
final bind miss clears only the exact agent/thread association and returns 409;
lane refusal never deletes an agent row.

The create, resume-refetch, permission-wake, and officer paths return before
pool lookup or pod creation when their authoritative read is non-pinned. A raw
lane edit can still land after one of those reads and waste a pod, but the warm
reservation or final registration fence prevents that pod from becoming a
second executor.

Decision/deviation: there is still no sanctioned application lane-transition
verb; current flips are operator SQL. A future transition must take the same
thread advisory lock and require both no binding and no in-flight pod marker.
`agent_id IS NULL` alone is not detachment while a pod is booting. Raw SQL can
always violate application invariants; the final registration check makes a
correctly quiesced operator flip converge safely instead of binding late.

Deferred transition-UX caveat: `_do_prepare` emits `provisioning -> failed` if
a pinned request is scheduled and an operator flips it to stateless before the
background lock/refetch. `provision_or_assign` can similarly leave one stale
`provisioning` event if raw SQL lands during its warm-attach window. These are
ephemeral SSE events, not durable state, and both paths stop before a second
executor; `/connection` remains authoritative. A sanctioned lane-transition
API must add lane-aware lifecycle cancellation while taking the same advisory
lock and rejecting the in-flight pod marker.

## Honest connection contract — DONE server-side, k3d verified

`GET /api/sessions/{thread_id}/connection` is now a discriminated contract:

- pinned: `execution_lane='pinned'`, `control_socket='websocket'`, non-null
  `ws_url`, token, and expiry (existing readiness ladder unchanged);
- detached stateless: immediate `200 ready`, `control_socket='none'`, null
  socket fields;
- stateless with an incompatible agent binding, missing/corrupt lane, or future
  lane: fail closed 409.

Every marker and null socket field is required. The response model is an
OpenAPI `oneOf` discriminated by `execution_lane`; neither `{}` nor an
incoherent lane/socket combination validates as ready.

`POST /prepare` returns a clear 409 for every non-pinned lane, matching the
brief's definition-of-done wording. Internal/background callers independently
recheck rather than trusting the public request snapshot.

`control_socket` is intentionally not called `control_transport`: the server
has no replacement REST control path. Here `ready` means a turn can be admitted
to the queue, not that every pinned-session capability is available.

Live existing stateless fixture `9a756800`: the initial probe returned the
earlier `control_transport='rest'` draft; `/prepare` and `/resume` both returned
409; its row stayed `agent_id IS NULL`, queue `done`. That marker was corrected
after review because it advertised an unbuilt transport. A synthetic persistent
registration against that row returned 409, left **0** probe agent rows, and
left the thread unbound. Final-image re-verification of `control_socket='none'`
is recorded below.

Pinned regression: created a fresh `session_base` virtual thread through the
orchestrator, reached `ready/websocket` in **12 s**, submitted
`PINNED-S1-GATE-OK`, and observed the exact AI row by **17 s** from create. The
test thread was permanently deleted afterward.

## Control REST step — STOP, NOT DONE (design premises disproved)

The next dependency in the brief says the orchestrator can accept a verb and
write a journaled ack while no pod owns the thread. That is not safe with the
built journal allocator. `append_system_frame` explicitly excludes a live
in-process writer; the stateless executor retains exactly such a writer in its
300 s warm cache after completing a claim. An orchestrator system write can
allocate the same `(epoch, seq)` as the warm writer's local `_next_seq`, killing
the writer. `run_queue.state='done'` is therefore not a no-writer signal.

Further call-site findings invalidate verb-level assumptions:

- detached conversation rewind admits stateless rows because `agent_id` is
  null, but it bumps only `events_epoch`, not `run_queue.lease_token/state`; an
  active claimant remains authorized after the transcript sweep;
- there is no detached compact endpoint. Boundary compact matches in-memory
  message IDs, while restore mints new IDs and does not select the persisted
  ID;
- undo's file originals exist only in `PersistentSession.file_checkpoints`
  RAM;
- workspace upgrade must keep both backends live during seed/swap and would
  move S1 into the explicitly unbuilt S2 workspace lane;
- archive has a multi-side-effect, non-idempotent finalization path;
- config persistence can succeed before live apply later rejects model fit.

The smallest credible replacement design starts with migration **0119** and a
durable `thread_control_requests` inbox. Human and control admission need one
commit-ordered per-thread sequence (the current human path also lacks an
admission lock); the lease-owning executor consumes the oldest item, emits the
ack through its own writer, awaits durable flush, fenced-CASes the request, and
completes at that sequence. `mode.set`/`narration.set` are the first plausible
subset. Rewind needs an atomic lease steal; undo and upgrade remain explicit
501. This needs adversarial design review before implementation, so no REST
control route, migration, or cockpit routing was scaffolded.

Cockpit audit corrections recorded for that review: accepted-send waiting UX
already exists (`pendingTurnCount`/spinner), so the missing queued UX is durable
queue position/reload/multi-tab state rather than literally no feedback; the
initial `session.state` snapshot is WS-only even though incremental journal SSE
is shared; ended stateless threads currently take `/resume` and would strand;
and interrupt/canvas/upgrade controls require explicit lane behavior.

## Verification and failures

- First focused post-change suite: **187 passed in 1.02 s**. After the
  adversarial bind-boundary corrections: **434 passed in 1.50 s** / **4.44 s
  wall** (four pre-existing AsyncMock resource warnings in
  `test_internal_auth.py`). Contract-hardening slice: **28 passed in 0.12 s**;
  final bind/connection-focused slice: **185 passed in 0.87 s** / **3.91 s
  wall**. After the final 409/hostname-identity audit, the stable changed-file
  suite was **460 passed in 1.61 s** / **4.66 s wall**.
- Full `ruff check src/ orchestrator/ tests/`, `ruff format --check` (**1,038
  files**), and `git diff --check`: clean.
- Running orchestrator source before the first live probes had the draft
  registration fence and REST marker. Agent containers do not carry
  orchestrator source, so those paths are not applicable there. The corrected
  source/image grep and no-socket response are recorded in the final probe
  entry below.
- Final k3d image proof, after all adversarial corrections: the one running
  orchestrator pod had count **1** for each of the discriminated no-socket
  response, insert-only thread registration, exact expected-owner bind,
  hostname-upsert bypass, and ambiguous non-200 attach handling. The first
  grep attempt used repository paths (`/app/orchestrator/...`) and failed;
  this image flattens that directory, so the successful proof used
  `/app/routers/sessions.py`, `/app/main.py`, and `/app/database/postgres.py`.
  Its live OpenAPI response exposed two `oneOf` branches with
  `discriminator.propertyName=execution_lane`. On that exact image the
  stateless fixture returned
  `{state: ready, execution_lane: stateless, control_socket: none, ws_url:
  null, token: null, expires_at: null}`; `/prepare` and `/resume` were both
  **409**. A persistent-registration probe was **409 before upsert**: agent
  rows for its hostname stayed **0 -> 0** and the thread stayed unbound.
- Final pinned regression on the same image: a fresh `session_base` virtual
  thread reached `ready` in **14.43 s** with
  `execution_lane='pinned'`, `control_socket='websocket'`, and all three socket
  fields non-null. One input produced exactly one
  `PINNED-S1-FINAL-REGISTRATION-20260808` AI row by **17.48 s** from create. The
  first DELETE was the documented soft end (200); the follow-up
  `?permanent=true` DELETE returned 200 and left **0** thread rows.
- Real-Postgres helper probe on that image: forced the agent half of a warm
  reservation to succeed and the thread half to fail, then verified transaction
  rollback restored the candidate to ready/unbound. In the final-bind race, a
  real warm reservation won after registration's NULL-owner snapshot; the
  exact-owner CAS preserved that winner and cleared only the loser association.
  Result: `reserve_rollback=1 owner_cas=1 residual=0`.
- Real-Postgres registration probe: a new thread registered with a hostname
  already owned by another thread and received a fresh row while the old
  thread/agent/IP stayed unchanged (`insert_only_no_hijack=1`); an offline
  same-host restart returned the exact original ID (`exact_restart=1`). Cleanup
  left **0** probe rows.
- Final stateless timing harness: accepted at queue depth 1, answered in **10 s
  wall**; agent line `mode=fresh bundle=0.12s attach=2.63s turn=2.82s
  push=0.09s complete=0.01s total=5.67s`; orchestrator claim bundle
  `lease=0.002s creds=0.015s assemble=0.083s total=0.100s`. Exactly one
  `STATELESS-S1-FINAL-20260808` AI row persisted.
- Worker isolation check after all live probes: **0** `worker_batch` queue
  rows. No worker unit was enqueued or claimed.
- Operational failure: the shared k3d DB has the parallel S3 migration
  `0118_jobs_execution_lane.sql` applied, while this branch correctly starts at
  0119 and lacks that file. The new orchestrator refused startup with
  `applied but missing on disk`. For live verification only, the exact 0118
  file from `feature/stateless-workers-s3` was materialized as an untracked
  shim, every live check above was run on the healthy new pod after source
  greps, then the shim was deleted and never staged. No S3 code or traffic was
  exercised. Terminal cluster state after removal: Tilt `updateStatus=ok`,
  `runtimeStatus=pending`; the source-verified pod remains Ready, while its
  replacement fails startup with the same explicit 0118 error. This is shared
  cluster migration residue, not a deployable state claimed for this branch.
- First full `pytest tests/ -q --tb=short`: **15,031 passed, 14 failed, 107
  skipped in 964.37 s**. Eleven failures were the documented environment
  baseline (Postgres, MCP/Python 3.14, and missing arxiv); three were real stale
  `/prepare` fixtures that lacked `execution_lane` and therefore hit the new
  fail-closed 409. Those fixtures now explicitly describe pinned rows.
- An intermediate full run was intentionally interrupted after a schema audit
  found defaulted response markers could turn `{}` into a ready stateless
  response: **7,174 passed, 1 known-baseline failure, 18 skipped in 411.78 s**.
  A later run was intentionally interrupted at **947 passed, 6 skipped in
  83.34 s** when the transaction audit found the offline/same-host identity
  gap. The final model requires every response field and emits an explicit
  discriminator; the registration path now has exact-ID and insert-only modes.
- Complete stable-tree gate: **15,060 passed, 11 failed, 107 skipped in 957.30
  s** / **962.70 s wall**. All eleven failures are the known local environment
  baseline: one absent localhost Postgres, seven Python-3.14/MCP transport
  incompatibilities, and three missing-arxiv contract failures. No changed-path
  failure remained.
- Probe failures retained for reproducibility: the first final `/prepare` call
  omitted its required `{}` request body and therefore measured validation 422,
  not the lane gate; the corrected call returned 409. The first in-pod Python
  HTTP probe omitted `kubectl exec -i`, so stdin never reached Python and no
  request ran; it was rerun with `-i`. Neither changed data.

## Phase boundary

Provisioning safety + the honest server connection contract are DONE and
verified. Cockpit consumption, control verbs, queued durability, permission
retire, and Path-A compaction persistence remain unbuilt. Stop here rather than
build control transport on the disproved live-journal-writer premise.

---

# Session 4 — durable controls and S1 completion (2026-08-08)

Branch: `feature/stateless-sessions-s1-completion`; not pushed. Continued from
the reviewed server boundary (`504b153f`, `fe436783`) after re-reading the
updated completion brief, its required feature-doc sections, the full prior
implementation log, and `turn_executor.py`. The worker lane remains out of
scope; no `worker_batch` unit was enqueued.

## Baseline before the control-inbox phase

The cluster started healthy (`Tilt srw: ok/ok`; two stateless agents and one
orchestrator Ready). The first image-proof attempt used the wrong agent path
(`/app/api/turn_executor.py`) and failed. Agent source is not flattened the way
orchestrator source is: the correct path is `/app/src/api/turn_executor.py`.
After proving `turn timing: unit=` exactly once in **both** serving agent pods
and `_thread_input_stateless` twice in the sole orchestrator pod, a stateless
turn completed in **5 s wall** with:

`mode=reuse bundle=0.05s attach=0.00s pending=0.00s turn=3.12s push=0.08s complete=0.01s total=3.26s`

The orchestrator claim bundle was **0.046 s** (`lease=0.001s`, `creds=0.009s`,
`assemble=0.037s`). The immediately preceding cold observation, retained only
as a diagnostic because its source proof came afterward, was **4.99 s** agent
total / **6 s** wall (`attach=2.43s`).

## Cockpit verification gate — STOP, NOT DONE (corrected premise still false)

The requested read-before-build check disproved §3.4's remaining “zero Cockpit
changes for send and receive” premise. The active-tab happy path is close, but
reload/multi-tab correctness is not:

- `ConnectionPayload` declares `ws_url: string`, `_openControlWs` marks the
  session ready and then unconditionally calls `_installControlWs(threadId,
  connection.ws_url)`. A stateless response supplies null, so this reaches
  `new WebSocket(null)` and its failure/close enters the eight-attempt reconnect
  ladder. `_ensureControlWs` can reset that ladder on focus, SSE recovery and
  user actions. The null socket therefore is not currently a quiet no-op.
- `session.state` is sent directly by the control WebSocket and is not in the
  journal. It carries load-bearing reload state: permission/narration modes,
  `turn_in_flight` + turn-count reconciliation (which joins a REST-restored
  partial turn to its cursor-replayed suffix), the running tool, and pending
  permission rows. A stateless reload gets none of it. A null-WS guard alone
  can split a live turn or make a permission card disappear permanently.

This is the fifth wrong premise caught by reading the call site. Per the brief's
explicit instruction, implementation stopped instead of building the durable
control inbox on top of an incomplete client-state transport. A draft 0119
migration and shared helper had just been materialized when the parallel audit
reported the issue; both were deleted immediately and were never staged or
committed. Tilt had already built the partially edited snapshot and applied the
empty draft table, however. Once the file disappeared, a replacement
orchestrator correctly refused startup because its migration ledger contained
an applied migration missing from the image; the preceding pod remained Ready.
The draft table contained zero rows. In one explicit transaction, the exact
`0119_thread_control_requests.sql` ledger row, `thread_control_requests` table,
and `notify_thread_control_request()` function were removed, then the failed pod
was recycled. Final live checks showed all pods Ready, ledger count zero, both
objects absent, and `worker_batch` count zero. Migration 0119 remains unused in
both the branch and the dev database. This is the partial-snapshot Tilt trap the
brief warned about, and is retained here as a failure rather than elided.

The revised dependency is: first design a lane-agnostic, transport-independent
`session.state` snapshot (REST or journal/SSE-safe equivalent) for **both**
lanes, including an ownership/freshness contract for in-flight and permission
state. Only then land 0119 control admission/consumption and route Cockpit
controls to REST. No worker traffic was created.

### Read-only audits retained at the stop boundary

The control transaction audit found that “lease owner” is incomplete for the
pinned lane: its equivalent fence is the exact current `threads.agent_id`
binding. A request table alone is also insufficient. `complete_unit` currently
requeues only on `input_seq > consumed_seq`, so a control committed during a
lease can be stranded when completion writes `done`; 0119 needs control
watermarks (or a rigorously unified admission cursor) updated under the same
queue-row serialization as completion. The executor's pre-attach
skip-if-answered and no-pending-input branches would otherwise discard a
control-only claim. Terminal request CAS must fence on lease token (stateless)
or exact binding (pinned), and `_broadcast()` is not proof of durable journal
acceptance — a writer receipt/flush seam or journaled-request recovery is
required before terminalizing the row.

The safe first verb subset is smaller than §3.3 implies. `mode.set` and
`narration.set` are idempotent assignments, though mode must move off its
current memory-only WS behavior and through grant-checked persistence. Plain
compact can follow only with a durable receipt; boundary compact cannot resolve
restored message IDs today. Stateless rewind has two additional defects: the
detached route does not fence `run_queue`, and attached `_handle_rewind`
replaces the writer without carrying the stateless lease. Undo is RAM-only;
upgrade enters unbuilt S2; archive needs a terminal finalizer; config mutation
is not transactionally fenced. Those verbs cannot safely share one blanket
`_sendControl -> POST` implementation.

The lifecycle audit found two more non-mechanical items:

- Permission rows carry no lease identity. A thread-wide post-steal sweep can
  expire a successor's fresh gate, while the current steal commits before its
  contained journal side effect and has no durable retry marker. Safe retirement
  needs a request stamped under the current fence and durable reaped-token
  evidence. The current executor also **does not** release its lease at a gate,
  despite the feature doc's future-state wording, and stateless SSE viewers are
  absent from the agent-local `_subscribers` oracle.
- Durable session wake writes `role='event'`, but the stateless executor fetches
  only `role='human'`. Merely flipping an ended status would therefore settle
  the wake without ever running a turn. Safe wake admission needs a stable event
  id plus message/status/watermark in one transaction, role-preserving restore
  and injection, and an explicit parked-unit rule.

Path-A resume compaction remains the smallest independent next phase, but its
fix is not a copied `_record_compaction` call. Restore does not project the DB
message id/seq and mints new UUIDs; the boundary lookup would stay null.
Persistence must be split from banner emission, restore must preserve the
persisted boundary identity, and Path A must detect a real compaction and set
`turn_count` before writing a persist-only checkpoint. This was not changed in
this stopped session.

# Session 5 — transport-independent session state (2026-08-08)

Branch: `feature/stateless-sessions-s1-completion`; not pushed. This session
re-read the updated completion brief and absorbed its corrected scope. The
reasonable route around the WebSocket-only `session.state` dependency was to
build the same current-state contract over REST for both lanes, not to proceed
directly to controls and not to expose a lane to the browser. This phase lands
that prerequisite only. Migration 0119 remains unused; no control-inbox object
exists and no `worker_batch` unit was enqueued.

## Snapshot contract — DONE, focused tests + k3d verified

`GET /api/persistent/threads/{thread_id}/state` is authenticated with the
existing exact thread-owner gate and returns `Cache-Control: private,
no-store`. One repeatable-read transaction captures the whole thread row,
current journal high-water mark, durable message count, latest turn lifecycle,
unmatched running tool, current `run_queue` state and pending permission rows.
Only after that read does config resolution run, against the exact captured
thread row; the response projects safe display scalars rather than returning
metadata, a resolved config blob, credentials, lane or agent identity.

The 13-key response is deliberately `session.state`-compatible rather than a
claim to be identical to agent RAM:

`thread_id`, `permission_mode`, `narration_mode`, `turn_count`,
`turn_in_flight`, `message_count`, `model`, `temperature`, `running_tool`,
`pending_permissions`, `event_cursor`, `replay_cursor`, `snapshot_source`.

`turn_in_flight` and `running_tool` mean “durably observed as of
`event_cursor`”; the ordered writer can still have an edge in memory. A known
idle stateless queue state corrects a dropped terminal edge, but a stale pinned
queue row cannot do so and unknown queue/lifecycle states fail closed. Turn
count comes from the latest lifecycle event, with a fallback that excludes
admitted human/event rows so queued future inputs cannot masquerade as the turn
currently executing. `message_count` is the count of live durable message rows,
not `len(agent_session.messages)`. A pinned socket can still deliver its later,
exact in-process welcome frame.

The crucial addition to the original snapshot idea is `replay_cursor`: it is an
exclusive floor immediately before the latest surviving `turn.started`, while
`event_cursor` marks the scalars' high-water. The client applies the snapshot
first and replays the full latest logical turn. That closes all three races the
simple high-water design missed: REST history racing turn completion, a cached
cursor landing in the middle of a token stream, and a terminal frame covered
by the snapshot that must still close a reconstructed transcript turn.

## Cockpit transport and replay — DONE for the snapshot slice

The public `/connection` union is now discriminated by transport:
`control_socket='websocket' | 'none'`. `execution_lane` is absent from both
responses and from the Cockpit type. During rolling version skew, a non-empty
legacy `ws_url` remains usable, but an explicit no-socket marker, null URL or
missing URL normalizes to a stable no-socket connection. It never calls
`new WebSocket(null)` and focus/online/SSE recovery cannot put it into the
control-socket reconnect ladder.

The Cockpit loads the state snapshot after REST history/metadata and before
journal SSE. A cursor retained by this tab may resume incrementally only when
it is in the same epoch and between the latest-turn replay floor and snapshot
high-water. Otherwise the tab repaints history and replays the whole latest
turn. The shared IndexedDB cursor is now cold fallback only: rereading a cursor
advanced by another tab could skip frames this tab never applied, so each live
tab owns and advances its cursor synchronously. Epoch changes, a cursor behind
the latest-turn floor, retention resets and IndexedDB failures all take the
safe repaint path.

A state-read failure keeps a known socketless session not-ready rather than
presenting an enabled composer with missing approval state. Reconnect,
focus/online and stream-horizon recovery retry the snapshot; there is no
independent polling timer. Pinned sessions can heal via their exact WebSocket
state. Pending permission rows are authoritative even when the list is empty,
but snapshot hydration is kept separate from transcript replay so it does not
invent a tool card before `turn.started`. Duplicate durable rows are folded
latest-wins by tool-call id because the newer waiter owns the answerable
approval id.

The approval action was also made durable-ack-driven. For a row carrying an
`approval_id`, the client calls the existing orchestrator REST endpoint for
both connection shapes and leaves the card visible until the lease owner emits
`permission.resolved`; it never falls back to WebSocket after an ambiguous REST
failure. A resolution racing a 5xx cannot resurrect the card, a later
resolution clears its keyed retry banner, and 404/409 retires an already
terminal/stale card. The no-`approval_id` legacy shape keeps the old WebSocket
compatibility path. This is snapshot usability, not the migration-0119 control
inbox: all other control verbs remain untouched.

## Measurements — before and after the code change

The before turn was measured on the synced pre-snapshot image, then the after
turn was measured only after grepping the new source in the running pods.

| | Before | After |
|---|---:|---:|
| End-to-end wall | 9 s | 6 s |
| Agent mode | fresh | fresh |
| Bundle | 0.12 s | 0.05 s |
| Attach | 2.66 s | 2.38 s |
| Pending-input drain | 0.00 s | 0.00 s |
| Turn | 2.67 s | 3.18 s |
| Push | 0.14 s | 0.04 s |
| Complete | 0.06 s | 0.01 s |
| Agent total | **5.64 s** | **5.66 s** |
| Orchestrator claim bundle | 0.103 s | 0.037 s |

The meaningful comparison is the agent total: **5.64 → 5.66 s**, no measurable
turn-path regression. The 9 → 6 s wall improvement and provider portion moved
in opposite directions and are cluster/provider noise, not a claimed speedup.
The after probe persisted exactly one human and one AI response containing the
unique marker, with one completed lifecycle. Its input API reported turn 94
while the durable message/lifecycle rows reported 95; this appears to be a
pre-existing accepted-response counter mismatch and was recorded rather than
silently rationalized. Snapshot `turn_count=95` matched the durable lifecycle.

There is no pre-change endpoint to compare with `/state`. The first manual live
read was **95 ms server total** (`auth=.012`, `config=.072`, snapshot=.011).
Warm curl observations were **35–102 ms server total**, with config resolution
dominating. In the final browser proof the three server timing lines were:

- initial: `auth=.001s config=.027s snapshot=.004s total=.032s`
- cache-cleared reload: `auth=.001s config=.038s snapshot=.003s total=.042s`
- second tab: `auth=.001s config=.029s snapshot=.003s total=.033s`

These are observations, not percentiles. Snapshot construction itself was
3–4 ms in that proof, but endpoint cost includes config resolution.

## Verification

- Focused backend/router/journal/security gate: **88 passed in 1.29 s**.
  Relevant Ruff checks passed.
- Full Python suite: **15,076 passed / 11 failed / 107 skipped in 960.51 s**.
  The same 11 environment failures are the established baseline (local
  PostgreSQL absent, Python-3.14 MCP subprocess behavior, and the optional
  `arxiv` package); this slice adds 16 passing tests and no new failure.
- Focused Cockpit state/replay/control tests: **266/266 passed**. Full Cockpit:
  **1,790/1,790 passed in 8.47 s**. Production build passed in **13.04 s** with
  only the existing bundle/CommonJS budget warnings.
- Actual running-image proof, after the last code edit: the orchestrator pod had
  one `session-state timing:` marker, two `replay_cursor` markers in the new
  service, one `control_socket` discriminator and no lane discriminator. The
  Cockpit pod had the durable-snapshot/replay markers and zero
  `execution_lane`. An earlier grep failed because it assumed the orchestrator
  package lived at `/app/orchestrator/...`; Tilt flattens it under `/app/...`.
  The failed path is retained here because “source exists locally” was not used
  as image proof.
- The real state response returned exactly the 13 safe keys, `private,
  no-store`, epoch/cursor values, and no lane, agent, metadata, config or token.
  The final `/connection` response returned exactly `state`, `control_socket`,
  `ws_url`, `token`, `expires_at`, with `control_socket=none`, null socket
  fields and no lane.
- Browser proof with injected owner bearer auth: initial load, a cache-cleared
  hard reload and a second tab enabled the composer in **822/496/575 ms**.
  Each made exactly one state read (38/48/37 ms client-observed) and one
  connection read (7/6/4 ms). After six seconds of focus switching there were
  still exactly three of each, no application WebSocket (only Angular HMR), no
  mutation, page error, console error or unexpected request failure.
- A real supervised gate was also reached with a concretely bound, read-only
  `list_files(path="", pattern="*", depth=0)` call. The durable row and approval
  card were visible, denial through the canonical REST route returned 200, and
  the lease owner journaled the matching `permission.resolved`; the tool never
  executed. The probe's combined text locator failed before the planned hard
  reload, so its fail-safe denied immediately. A subsequent exact prompt
  produced no tool call and therefore no row. No row was synthesized and the
  pending-card **reload/multi-tab browser case remains unproven live**; its
  snapshot/replay/ack ordering is covered by the focused tests.
- The timed post-change turn answered once. Final read-only closeout found all
  16 `srw` pods Running/Ready with zero restarts; the fixture queue was `done`,
  `input_seq=consumed_seq=2370`, with no lease owner and zero pending
  permissions. `worker_batch` remained at zero. Migration 0119 still has no
  file, ledger row, table or function.

## Failures and deviations that changed the implementation

1. The brief's zero-client-change premise was a scope error, not a dead end.
   The alternative is the durable snapshot plus replay contract above.
2. The first server iteration still returned `execution_lane` as the OpenAPI
   discriminator. A live response audit caught the topology leak; the contract
   now discriminates `control_socket` and current pod source/response were
   re-proved.
3. A shared IndexedDB replay cursor is not a multi-tab cursor. The client now
   treats it as cold fallback and uses a per-tab live cursor, with tests for a
   second tab advancing storage behind the first.
4. A snapshot high-water alone loses reconstruction races. The endpoint now
   supplies both the state high-water and a full-latest-turn replay floor;
   covered terminal frames close transcript state without rolling back newer
   snapshot scalars.
5. Optimistically removing a durable approval on REST 200 can hide a committed
   but not-yet-journaled gate, while falling back to WebSocket after a 5xx can
   execute the decision twice. Cards are now owner-journal-ack-driven and the
   ambiguous cases have explicit tests.
6. One full-suite run during active edits reported 15,065 pass / 15 fail. Four
   failures were stale `/connection`/endpoint-inventory expectations observed
   while files were changing, so it was discarded rather than called a
   baseline. The settled run above has exactly the known 11 failures.
7. The first live permission prompt asked for `run_command`, but the model
   emitted the unbound alias `shell_execute`; the runtime correctly rejected it
   before the gate. The next probe first established `list_files` from both
   claim logs and `/tool-groups`, then produced a genuine row. Its browser text
   assertion—not the product selector—was brittle, so safety cleanup took
   precedence over preserving the row. The later no-tool response shows why a
   model-driven permission fixture is not deterministic enough to hide this
   gap or justify a synthetic database row.
8. A redundant manual closeout query lost its SQL string quotes through nested
   local/remote shell quoting and failed read-only with a syntax error. It
   exposed no credential and changed nothing; the independent closeout above
   supplied the exact DB state instead.

## Honest boundary and remaining unverified work

This is a clean, committed snapshot phase boundary, not S1 completion. Live
idle reload/concurrent-tab transport is proven. Mid-turn reconstruction,
retention/epoch races, snapshot failure recovery, running-tool restoration and
permission ordering are extensively simulated in Vitest; the pending card was
seen live and safely denied, but its reload/multi-tab state was not captured.
These are not all real-cluster fault injections. The browser proof injected a bearer token into
safe requests, so normal BFF-cookie login remains unverified. Durable approval
cards depend on the owner eventually journaling `permission.resolved`; an owner
crash in that interval can leave an already-open tab stale until its next
snapshot/reconnect.

Next is migration 0119 and the REST control inbox, with the already-recorded
non-optional queue control watermarks, exact pinned-agent fence and durable
journal-write receipt. After that remain durable queued-turn UX, permission-row
retirement, stateless ended-session wake, Path-A compaction persistence and the
other S1 surrounds. None was scaffolded in this slice.

# Session 6 — REST control inbox (2026-08-09)

Branch: `feature/stateless-sessions-s1-completion`; not pushed. Continued from
the reviewed snapshot boundary at `49b5b2a0`. The completion brief, its required
feature-doc sections, the full implementation log and `turn_executor.py` were
re-read before code. The worker lane remains out of scope and no
`worker_batch` unit has been enqueued.

## Browser proof debt closed before migration work — DONE

The normal browser authentication path is now exercised rather than simulated:
Chromium navigated from `https://localhost/sessions/<thread>` through
`/auth/login`, the Keycloak login form, `/auth/callback`, and back to the
Cockpit. `/auth/me` returned 200 and the resulting `srw_session` cookie was
HttpOnly. No bearer header or request interception was installed.

One honest supervised-gate attempt then used the existing stateless fixture and
the concretely bound read-only call
`list_files(path="", pattern="*", depth=0)`. The real card rendered with the
same tool and args in all three places: the originating tab, that tab after a
hard reload, and a concurrently opened second tab. Clicking the visible Stop
action denied approval `22fd7e07-a581-45a5-87af-07cc8eade116`; the canonical
REST decision returned 200, both tabs removed the card after the owner-journaled
ack, and the next state snapshot contained zero pending permissions. Postgres
confirmed the row was `denied`, `permission.request_batch` at epoch/seq
`7/5039`, matching `permission.resolved` at `7/5040`, and no `tool.started` or
`tool.completed` exists for its tool-call id. The queue settled `done` at
`input_seq=consumed_seq=2374`, lease token 81.

Harness failure retained: the first attempt matched the Cockpit URL before its
SPA auth redirect completed and waited for a composer while Keycloak was still
loading. It sent no input, created no permission row, and cleanup observed zero
pending rows. The corrected harness raced the actual login form against the
composer before proceeding; only then was the single real tool prompt sent.

## Durable lane-free scalar controls — DONE, focused tests + k3d verified

The safe initial subset is `mode.set` and `narration.set`; `interrupt` keeps its
existing 501. Both pinned and stateless clients use the same owner-gated
`POST /api/persistent/threads/{thread_id}/controls`. The public request and
response contain no lane or pod identity. The orchestrator performs grant
checks and admission only: it never allocates or writes a journal sequence and
never publishes the desired scalar before the serving owner applies it.

Migration 0119 adds `thread_control_requests`, a threads-row-serialized
`control_seq_hwm`, an exact nullable pinned capability UUID, first-class
permission/narration scalars, and independent `run_queue` control input/consumed
watermarks. 0120 is a non-transactional concurrent unique receipt index; 0121
backfills explicit legacy narration overrides and validates the narration and
composite receipt constraints after 0119 releases its catalog locks. All three
existing tables are locked together inside a retryable subtransaction before
0119 DDL, so a failed later lock does not sleep while holding an earlier lock.
The final from-zero PG15 replay measured **52 ms / 8 ms / 25 ms** for
0119/0121/0120 and the checked-in snapshot matched exactly. Squawk 2.59.0
reported **0 issues in 3 files**.

Narration deliberately differs from the original “default auto” sketch. A
legacy thread can inherit narration from an expert/account layer that SQL
cannot resolve, so `NULL` is an explicit “not materialized yet” sentinel.
Creation stores the resolved value; attach and snapshot fall back to resolved
config only while the scalar is NULL; the first owner-applied control
materializes it. This avoids silently changing existing users during migration.

Admission is one transaction: lock the thread, allocate a commit-ordered
request sequence, insert the idempotency-keyed request, and, for stateless
threads, advance the control input watermark without disturbing a live lease.
Pinned admission requires the exact reciprocal agent binding and a capability
equal to that agent UUID. Every bind/resume path clears the credential; only an
inbox-capable owner opens it after its writer and first drain are ready, and it
closes before its final drain. A boolean gate was rejected during review because
it could transfer truth from owner generation A to B. Internal ownerless admin
threads remain supported with `IS NOT DISTINCT FROM NULL`; ordinary users still
pass the normal owner gate.

The lease owner drains at claim time and watches during a turn. It validates
without mutating RAM, writes one correlated result frame through its own ordered
writer, waits for the real database commit receipt, then transactionally
publishes the scalar, terminal request and stateless consumed watermark under
the current lease token or exact pinned-agent fence. The request/thread
composite foreign key prevents cross-thread receipt links; one concurrent
partial unique index prevents duplicate receipts. Recovery of “journal commit,
finalization crash” reads the indexed receipt and finalizes without emitting a
second frame. Mixed writer batches split control frames into singleton commits,
so a lost control fence cannot drop an ordinary frame already queued behind it.
Warm affinity reconciles RAM from the first-class DB scalars before every drain
and after an ambiguous already-terminal finalizer.

Completion now treats control watermarks independently from human input: a
control committed while a stateless lease is live makes completion requeue
until the owner-written receipt is terminal and the control consumed cursor
catches up. Event pruning preserves a receipt linked to a pending request.
Pinned lifecycle teardown uses an exact-owner status update, closes admission,
drains, then changes status; archive and idle paths route through that common
teardown rather than writing an unfenced terminal state. Conference teardown's
exact-owner projection includes metadata, project and title so its hold-release
wake is not silently lost. If a binding moves between close and final drain,
the stale process skips lifecycle mutation but continues local cleanup; the
successor adopts the request. User end and resume also explicitly NULL the
capability. A stop-responsive LISTEN wait lets back-to-back control-only claims
release asyncpg cooperatively, with a one-second cancellation backstop.

A final lifecycle audit widened that invariant beyond the successful pinned
smoke: generic agent end, drain-suspend, attention-sleep, both orphan sweeps,
magic-link reactivation, officer respawn, both direct-DB terminal setters and
agent deletion/GC now close the capability too. Agent deletion first locks
every thread bound to (or carrying the capability of) the captured agent IDs,
then deletes those exact still-eligible rows in the same transaction. This
preserves admission's thread-before-agent lock order even when the capability
was already closed, rather than relying on PostgreSQL to abort an inverted FK
cleanup as a deadlock.

The Cockpit uses one bounded, single-flight FIFO for both lanes. It registers
the acknowledgement marker before starting HTTP (the SSE ack may beat 202),
coalesces only unsent same-scalar assignments, keeps an ambiguous retry head,
reuses its UUID across timeouts/0/408/425/429/5xx, and applies visible state only
from the correlated owner-written journal frame. A later same-scalar ack can
clear an earlier admission error; unrelated or older acks cannot. Snapshot
overlay plus receipt-aware pruning closes reload/multi-tab recovery without
inventing an orchestrator journal writer.

## Live control proof and measurements

Before trusting the proof, every running stateless pod returned one match for
the final cooperative-watcher marker, the orchestrator returned one match for
the 425 path, and the Cockpit source returned two matches for its durable REST
timeout marker. The live database matched the exact SHA-256 checksums of
0119–0121; narration was nullable/no-default, the capability was nullable UUID,
both constraints were validated, and the receipt index was valid and unique.

On the stateless fixture, a real BFF-cookie browser changed narration
`auto → verbose`, hard-reloaded with `verbose` rendered, restored `auto`, then
sent one same-UUID duplicate. The first cold UI acknowledgement took
**5.713 s** (REST admission **35 ms**; owner result **29 ms**, including
**15 ms** journal, and the claim drain **46 ms**). Warm restore took
**1.163 s**; the same-value idempotency probe reached terminal `applied` in
**574 ms**. Requests 5–7 each had exactly one matching receipt and all payload
identity fields matched; the queue finished `done`, human input stayed
`2374/2374`, control watermarks converged **7/7**, no lease owner remained, and
the fixture scalar was restored to `auto`. Both final agent logs and the
post-login browser console were error-free.

On the pinned fixture, the browser intentionally clicked during cold resume.
Registration kept admission closed, so four requests returned lane-free 425
(**15/1/1/1 ms**) and all four reused client UUID
`508c94b6-c4b3-4d31-b6fb-b1c02d13c660`. When the exact owner opened, that UUID
admitted once in **20 ms**; UI acknowledgement arrived **7.808 s** after the
original click. Reload rendered `verbose`; restore took **636 ms**; the
same-UUID terminal retry took **191 ms**. Requests 4–6 each had one receipt,
`applied_agent_id = accepted_agent_id`, no lease token, and matching
request/client/sequence payload identity. Soft end returned in **157 ms**, left
the thread ended with narration `auto`, NULL capability and zero pending
requests, and removed the pinned pod. The nine console resource errors during
provisioning were the expected connection/control 425s; after the first owner
ack there were zero page or console errors.

There is no pre-change control endpoint to compare with these timings. The
important decomposition is that owner journal/finalization is **17–29 ms** and
claim drains are **27–46 ms**; cold browser latency is provisioning/polling, not
control processing. No `worker_batch` unit existed before or after either proof.

## Final verification

- Full Python: **15,150 passed / 11 failed / 121 skipped in 969.11 s**. The 11
  failures are exactly the established environment baseline: no PostgreSQL on
  localhost:5432, Python-3.14 MCP subprocess/transport behavior, and the
  optional `arxiv` package absent. No feature test failed.
- Full Cockpit: **1,803/1,803 passed in 9.44 s**. Both i18n checks passed
  (**2,454 keys**); the production build completed in **9.681 s** with only the
  existing initial/persistent-SCSS budget and CommonJS warnings.
- Final lifecycle-focused set: **219/219 passed**. Queue/control regression set:
  **53 passed / 95 skipped**. The broader independent runtime audit ran
  **599 passing Python tests** and the control service audit ran
  **227/227 Cockpit tests**; three read-only schema/runtime/client audits
  reported no blocker.
- Real PostgreSQL after the final lock-order patch: **95/95 queue/control tests
  in 124.79 s**, plus **2/2 full-schema sweep prepare/bind tests in 17.69 s**.
  The final scratch PG15 replay matched `schema_current.sql`; that observation
  applied 0119/0121/0120 in **69/9/39 ms** (the earlier measured replay was
  52/8/25 ms). The migration SQL was unchanged between them; the difference is
  scratch-container/catalog timing, not a claimed regression. Squawk remained
  **0 issues in 3 files**.
- Ruff check and format-check passed on every touched Python path; the
  `git diff --check` gate passed. Cockpit lint is represented by its configured
  i18n and build gates above.
- Final running-image proof used content digests, not Tilt tags. Both stateless
  pods (`sha256:5b628c...`) contained the final watcher and direct-DB terminal
  fence markers; the orchestrator (`sha256:0a1f98...`) contained the final 425
  class and thread-before-agent delete predicate; the Cockpit
  (`sha256:c5c997...`) contained the durable REST markers. All 16 namespace
  pods were Running/Ready with zero restarts, and agent/orchestrator logs since
  those starts had no matching traceback, unique violation, deadlock or LISTEN
  cancellation error.
- The live ledger checksums exactly matched local 0119–0121. Both deferred
  constraints were validated and the one-receipt index was valid+unique. The
  stateless fixture was `done`, unleased, human watermarks **2374/2374** and
  control watermarks **7/7**; all seven stateless and six pinned requests had
  exactly one receipt. Both fixture capabilities were NULL, the pinned fixture
  was ended, pending controls were **0/13**, and `worker_batch` rows remained
  **0**.

## Failures and deviations retained

1. The first stateless restore attempt changed the select before the settings
   pane's thread + tool-group reads had anchored its diff baseline. The pane
   correctly swallowed the edit and no request row existed. The harness now
   waits for both responses; no database row was fabricated.
2. The first post-migration warm claim logged one unobserved asyncpg
   `unexpected connection_lost()` future when an immediately-created LISTEN
   task was cancelled. Requests and watermarks were correct, but the lifecycle
   warning was real; cooperative stop fixed it and the repeated live path was
   clean.
3. The first pinned cold-resume click received 409 while the exact capability
   was closed, so the client treated a transient refusal as semantic. A narrow
   `ControlAdmissionNotReady` now maps to lane-free 425 and uses the existing
   same-UUID retry policy; all other conflicts stay 409. The final cold proof
   exercised this route rather than waiting around it.
4. The first successful pinned soft end left its old capability UUID populated.
   Ended status still rejected admission and rebinding cleared the value, so it
   could not cross owners, but the documented NULL-is-closed invariant was
   false. End and resume now clear it explicitly; the repeated proof observed
   NULL after teardown.
5. The exact-owner status query initially projected metadata but omitted
   project/title, making conference conclusion a silent no-op. The final query
   carries the full helper input and focused officer tests pass.
6. The shared k3d database contained an earlier draft 0119 checksum/schema.
   Read-only audit found 10 terminal narration probe requests and their 10
   linked probe events, zero pending/cross-thread rows, and no worker units. A
   local-only transaction removed those probes and the one draft ledger row,
   dropped only the draft 0119 objects, then the normal migration runner applied
   final 0119–0121. The new orchestrator correctly refused startup both before
   reconciliation and while the checksum differed. Its reload parent did not
   retry migrations after startup failure, so the exact failed pod was force
   deleted per the brief; the replacement applied all three checksums.
7. A browser console count initially reported three errors, all expected 401
   bootstrap requests before the Keycloak redirect. The final harness resets
   diagnostics only after `/auth/me` proves the HttpOnly BFF session; stateless
   post-login output is zero, and pinned readiness errors are separated from
   post-ack output.
8. The first final full-suite attempt exposed two non-environment failures: the
   old `thread_input_enqueue` fake omitted the new control watermark columns,
   so the read model raised `KeyError` before its assertions. The fake now
   models both independent cursors; the focused set passed and the repeated
   full suite returned to the exact 11-failure baseline. No production change
   was made to hide the mismatch.
9. One full-suite run was intentionally interrupted after **917 passed / 6
   skipped** when the final lock audit found the bound-thread delete predicate.
   Continuing it while Tilt/source changed would have produced an incoherent
   number. The predicate was landed first, then the complete 969.11-second run
   above was started from one stable source snapshot.
10. The first closeout SQL used a guessed `lease_expires_at` name and a guessed
    request-side receipt column; both read-only queries failed before returning
    those projections. The schema uses `leased_until` and the receipt lives on
    `thread_events.control_request_id`. Corrected queries produced the exact
    unleased/watermark and one-receipt counts recorded above.

## Phase boundary and remaining work

This is a verified control-inbox phase boundary. Permission-row retirement is
not hidden inside mode acknowledgement: rows do not carry lease identity, so a
thread-wide sweep can delete a successor's gate. It still needs the lease-aware
schema/reaper design already called out in §9.1. Stateless ended-session wake
and the Path-A resume-compaction persistence bug also remain unverified and
unbuilt, as do queued-turn durability and the other S1 surrounds. They were not
scaffolded in this phase.

---

# Parallel track — S3 worker gates (branch `feature/stateless-workers-s3`)

Merged into this log at consolidation. This work ran in parallel with the
session track above; both are now on `feature/stateless-agents`.

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
has been enqueued. Gate 2B (default-unarmed graph boundary and agent registry
consumption) is next.

## Phase 2 / Gate 2B — checkpointed batch boundary (DONE, default-unarmed)

The shared registry is now consumed by the agent as well as the orchestrator;
the former private `_AUTO_CONTINUE_FREEZE_TYPES` duplicate is gone.
`UniversalAgentState` carries four checkpointed, claim-local budget fields:

- `worker_batch_started_at`;
- `worker_batch_start_iteration`;
- `worker_batch_target_wall_seconds`;
- `worker_batch_iteration_cap`.

All four default to `None`, so existing pinned jobs, session turns, old
checkpoints, and every currently deployed driver are incapable of emitting a
boundary. A future worker claim must arm them explicitly.

`worker_batch_boundary_updates()` implements wall-clock-first rotation. It
requires finite arming values, clamps any target to a hard **300 s minimum**,
and allows the secondary iteration cap only after the same 300 s floor. When
both are due, wall clock wins. The existing `iteration` field is an
execute/LLM-iteration counter, not a literal LangGraph superstep counter; this
is an explicit deviation from the design doc's terminology and is named and
reported honestly rather than pretending to have a counter the graph lacks.

Safe boundary placement and priority are now enforced:

1. existing completion/human/error outcomes;
2. version drain intent;
3. a consumed replan request and its completed phase transition;
4. batch rotation.

Mid-phase rotation exists only on `check_todos`' pending-todos path, after the
tool node has drained its transient carriers and after `_replan_request` has
been consumed. It cannot intercept empty-manager recovery, a completed phase,
or `audited_tools`, and a pending version drain suppresses it. The preferred
phase-boundary path runs after transition, replan-message injection, and reply
drain. Both paths checkpoint todos, emit `batch_boundary` with
`should_stop=True`/`error=None`, and clear all four arming fields. The existing
`version_upgrade` handoff now also disarms them, preventing an expired claim
start from immediately re-freezing its successor. Batch rotation does **not**
write `output/job_frozen.json`.

Added the stable tuning line
`worker_batch_boundary: boundary=... trigger=... elapsed=... target=...` with
iteration delta/cap. Tests pin default-unarmed behavior, the 300 s floor,
wall-clock tie-break, iteration gating, all priority rules, todo persistence,
empty tactical recovery, replan semantics through the next boundary, stale
error handling, field disarming, absence from `audited_tools`, and the
unconditional `tools -> check_todos` edge.

Verification:

- graph/drain/loop/Todo/replan suites: **326 passed** in 5.02 s;
- targeted Ruff check + format check and `git diff --check`: clean;
- after the final Tilt rollout and termination of every intermediate pod, both
  running agent containers were inspected directly. Each contains one
  `worker_batch_boundary:` timing marker, one 300 s floor definition, the
  phase-transition goal guard, and the shared auto-continue import;
- a pure assertion inside **each** running agent proved initial state is
  unarmed, an undersized target cannot fire at 299.9 s, it fires as
  `batch_boundary` at 300 s, and the arming state is cleared;
- the running orchestrator still contains its unknown-freeze fail-soft guard;
- live DB query after verification: **0 `worker_batch` rows**.

Final post-change session probe (turn 81): answer observed in **11 s**, agent
**7.16 s total** (bundle 0.20, attach 2.62, turn 4.24, push 0.09, complete
0.01). Orchestrator claim bundle was **0.170 s**. The immediately preceding
Gate-2B probe was 9 s / 4.86 s (bundle 0.19, attach 2.48, turn 2.00, push 0.17,
complete 0.01; orchestrator 0.167 s). The variance is provider/turn time (2.24
s) plus normal attach variation (0.14 s); no worker graph executes on a session
turn and no hot-path regression is attributed to the default-unarmed fields.

Audit-driven corrections made before declaring DONE:

- moved the mid-phase check below empty-todo recovery and completed-phase
  handling, avoiding a freeze that would preserve a stale `phase_complete` or
  mask the known empty-manager recovery path;
- added explicit `phase_complete=False` to a mid-phase freeze checkpoint;
- made drain intent suppress mid-phase rotation and disarm claim budget state;
- allowed only a true phase boundary to clear a stale prior error;
- ensured transition-produced `goal_achieved`, freeze, stop, or error state
  cannot be replaced by a batch boundary;
- split the replan test into the reachable tactical→strategic shape rather than
  accepting an impossible strategic request-replan state.

Operational note: Tilt built several intermediate snapshots while this ordered
edit was in progress. No measurement trusted them. Verification waited until
all terminating pods were gone and then checked every container in the final
ReplicaSet.

Safety state: Gates 1 and 2 are DONE and verified. Boundary code exists in the
agent image but no production path can arm it. No real worker work has been
queued. Gate 3 remains closed.

## Phase 3 / Gate 3 — completion ownership audit (STOP boundary; NOT DONE)

The implementation was stopped before the worker driver after three independent
read-only audits reached the same no-go conclusion: the brief's statement that
stage-4 completion currently CASes on `assigned_agent_id` is not true in this
checkout. `JobCompleteRequest` contains no agent identity or lease token, and
`PostgresDatabase.update_job_status()` ends in `UPDATE jobs ... WHERE id = ?`.
There is therefore no small existing CAS to switch from agent id to queue token.

More importantly, `/api/jobs/{job_id}/complete` is a long, multi-autocommit
workflow rather than one terminal write. Before and around the ordinary status
update it can persist freeze/context state, increment infra/memory/LLM and
deliverable retry counters, pause or recover workspaces, and deliver loop output
to cloud storage. Afterward it can graft subjob output, handle critic/scholar/
delegation results, spawn verification/curation work, advance loops, write
terminal records, enqueue session wakes, notify users, and archive/delete the
workspace. Several operations are external and several are not idempotent.
The terminal job status is written before some of those hooks; a crash followed
by a retry can hit the route's terminal early-return and permanently skip the
remaining work.

Consequences for the three obvious fencing shapes:

- an entry-time `fence_lease()` can succeed and commit, then the reaper can
  steal while the stale handler continues mutating state;
- a final `UPDATE ... WHERE lease_token = ?` rejects too late, after stale
  mutations and external effects have already happened;
- holding the `run_queue` row lock across the whole route would hold a database
  transaction across seconds-class network/VM/archive work and block the
  queue's reaper contract.

The required Gate 3 design is durable accepted-command ownership:

1. Pinned completion remains tokenless on its existing path. A stateless report
   requires a strictly positive `lease_token`; a token on a pinned job, no token
   on a stateless job, an unknown lane, or an unaccepted stale token fails closed.
2. One short transaction uses global lock order `run_queue -> jobs -> completion
   command`, verifies the leased `worker_batch` token, records an immutable
   command keyed by `(job_id, lease_token)` with its canonical payload, and
   consumes the queue lease. Exact retries return the recorded state/result;
   the same key with a different payload is a conflict.
3. `batch_boundary` is the bounded DB-only case: in that transaction, set the
   job to rotation-paused, clear legacy assignment/lease metadata, stash and
   clear the freeze, reset/requeue the durable queue row, and finish the command.
   It returns before generic graft/delivery/verification/cleanup hooks.
4. Terminal reports leave a durable accepted obligation and a non-runnable
   queue row. A separately claimed, visibility-timeout finalizer owns recovery.
   Its core mutation and every retry/finish write are command-token CASes;
   irreversible/external effects need deterministic idempotency keys or durable
   per-step markers. A `core_applied` retry resumes outstanding hooks rather
   than re-running status determination or hitting the legacy terminal guard.

An intake table plus the atomic boundary disposition would be a safe Gate 3A
slice, but it would not support terminal reports and therefore would not finish
Gate 3 or authorize a driver. No migration, request field, intake scaffold, or
worker-only partial path was landed: doing so would create a misleading
"lease-token fenced" surface while the corrupting race remained. The next
implementation should include real-Postgres acceptance/reaper races,
same-token exact/divergent replay tests, rollback injection, finalizer visibility
timeout fencing, crash-after-intake and crash-after-core convergence, and
once-only assertions for counters, grafts, verification/loop hooks, delivery,
and cleanup. Existing pinned completion behavior must remain unchanged.

## Shared-pool decision at the stop boundary

The requested one-Deployment experiment is **NO-SHIP at this boundary**. This is
not a measured starvation failure: safety correctly prevented any worker claim,
so no concurrent session/worker load result exists. The architecture audit did
find two prerequisites absent from the proposed pod-local reservation:

- a pod refusing a worker when its own slot is reserved does not guarantee a
  pool-wide interactive executor; a static two-replica Deployment can lose the
  reserve during rollout, pod failure, or executor unavailability without a
  coordinator-visible presence/capacity contract;
- the built stateless Deployment intentionally has no Tailscale/VM-mesh sidecar,
  so it cannot serve the full existing worker capability set as one generic
  pool.

The design doc now retains two Deployments as the production default. A future
one-pool experiment must first provide a failure-aware interactive reservation,
capability-aware claims, and then pass the p95 session claim-wait benchmark
under worker load. Gate 3 must be complete before that traffic is legal.

Safety state at the boundary: the running DB still has zero `worker_batch` rows;
the only deployed driver remains the session driver. Gates 1 and 2 are DONE.
Gate 3, TodoManager resume hydration, generator-close cleanup, real tmux
reattach, the worker driver, shared-pool guard, two-pod batch handoff, fault
injection, and Job Bench parity/performance remain unverified.

## Final verification of the stop boundary

- Full repository run: **15,026 passed, 113 skipped, 23 failed** in 975.45 s.
  Twenty-two failures are the current environment's missing-`asyncssh` canvas
  transport group (15) and MCP 1.26 contract group (7); the exact same 22
  failures reproduced from untouched base commit `3802d0e2` in a temporary
  worktree outside Tilt's watch root. The remaining failure is the brief-listed
  `test_connect_disconnect` localhost-Postgres assumption; it independently
  fails with connection refused on port 5432, and neither its test nor
  `src/database/postgres_db.py` differs from the base commit. No S3-focused or
  queue test failed.
- Repository Ruff check: clean. Ruff format check: all **1,039 files** already
  formatted. `git diff --check`: clean.
- Schema replay/drift check: clean through migration 0118. The first two final
  invocations failed before replay because the script changes into
  `orchestrator/` and a relative/absent `PYTHONPATH` could not import the new
  root-level shared module. This was a real Gate-2 tooling integration defect,
  not left as an invocation workaround: `schema-snapshot.sh` now explicitly
  carries `REPO_ROOT` in the migration runner's `PYTHONPATH` while retaining the
  orchestrator working directory. `bash -n` is clean and the ordinary command
  `scripts/schema-snapshot.sh --check app` now passes without caller-provided
  environment; 0118 replayed in **4 ms** on that final run.
- Tilt reports `(Tiltfile)=ok`, `srw=ok`; `agent-stateless` is **2/2** and the
  orchestrator is **1/1**. Every running agent container contains exactly one
  boundary timing marker, exactly one 300 s floor definition, and exactly one
  default-unarmed target initializer. The running orchestrator contains exactly
  one unknown-freeze corruption guard, six legacy-lane recovery predicates, and
  exactly one shared `batch_boundary` registry definition.
- Final live SQL invariant: **0 `worker_batch` rows**. No worker workload was
  used for a test or measurement.


---

# Consolidation — 2026-08-09

All three branches merged back onto `feature/stateless-agents`:

* `feature/stateless-sessions-s1-completion` fast-forwarded in (it already
  contained every commit on `feature/stateless-agents`).
* `feature/stateless-workers-s3` merged with four conflicts, all resolved by
  keeping both sides' intent:
  - **`postgres.py`** — the sharpest one. S1 restructured `register_agent` so
    hostname is no longer an ownership credential, which moved the job-pause
    block out of the `if existing:` branch; S3 had added the
    `execution_lane = 'pinned'` predicate to that same query. Taking either
    side alone would have silently dropped the other's fix. Kept the
    restructure, re-applied the predicate, verified all eight lane predicates
    survive.
  - **`test_infrastructure_metering_migrations.py`** — both branches advanced
    `APP_CURRENT_MIGRATION_HEAD` to their own tip. Head is now the true max
    (0121) with named constants retained for 0118 and 0119.
  - **Both docs** — stale-status collisions, rewritten fresh rather than
    resolved mechanically. `§9.1`'s S1 section had accumulated layered dated
    appendices; it is now one current picture. This log keeps both tracks'
    history under their own headings, because it is append-only history.

`schema_current.sql` auto-merged. Rather than trust that, it was regenerated
from the merged migration set — **no diff**, which confirms the merge instead of
assuming it. The snapshot carries both branches' schema (`execution_lane` on
`jobs` and `threads`, the `thread_control` inbox).

On the cluster: migration 0118 re-applied automatically after the merge (it had
been rolled back earlier so the S1 branch could run against the shared dev DB),
all seven migrations 0115–0121 are applied, all pods Ready, a turn answers end to
end, and the worker lane remains off — zero `worker_batch` rows, zero non-pinned
jobs. The turn timing line now carries `controls=0.02s`, the control-inbox drain.

**Migration numbering note for the future:** the collision that wedged the dev
database came from two topic branches numbering migrations independently against
one shared cluster. Now that both are on one branch the problem is gone, but if
the tracks fork again, either range-allocate numbers up front or land shared
safety migrations on the integration branch first.

---

# Session 7 — S2 workspace sessions (2026-08-09)

Branch: `feature/stateless-agents`; not pushed. Read the S2 brief in full, then
its required feature-doc sections, the complete implementation history, and the
complete stateless executor and remote backend before editing. S3 remains out of
scope and no `worker_batch` unit was enqueued.

## Phase 0 / S2 fail-closed workspace-tier gate — DONE

Before S2, a sandbox/VM thread could be operator-flipped to the stateless lane,
accepted by the public input/control routes and attached by a claimant. Both
ends of that claim killed its deterministic remote tmux session. The lane now
admits only the exact lite whitelist (`virtual`/`none`) until the rest of S2 is
verified:

* human input refuses before inserting a message or advancing a queue watermark;
* a new durable control refuses before inserting a control request or waking a
  control-only claim (an already-committed idempotent retry stays observable);
* the internal claim-bundle refuses before credential resolution/attach, which
  also protects already-queued legacy rows and direct operator/DB mistakes.

The effective physical backend is read from the create-time-materialized
`metadata.config_override.workspace.backend`. Missing, malformed and unknown
future values fail closed. The claim-bundle check is the authoritative safety
boundary; the two public checks make the refusal immediate and avoid leaving a
poison unit to retry/park.

Verification:

* focused input/claim/control tests: **49 passed**; targeted Ruff check, Ruff
  format check and `git diff --check`: clean;
* running-image proof: the serving orchestrator contained exactly one
  `S2 lite-only gate` marker in `/app/main.py` before any live assertion;
* live sandbox proof used an ended, detached sandbox thread belonging to the
  same test owner, changed only its lane for the bounded probe, and restored it
  via an exit trap. Input and control both returned clear **HTTP 409**. Durable
  counts stayed exactly **messages 0→0, controls 0→0, queue rows 0→0**;
* live lite regression: the existing virtual fixture answered exactly once.
  Before: **9 s wall / 5.73 s agent total** (`bundle=.10`, `attach=1.65`,
  `controls=.02`, `turn=3.75`, `push=.19`, `complete=.02`). After: **7 s wall /
  5.88 s agent total** (`bundle=.16`, `attach=1.96`, `controls=.01`,
  `turn=3.55`, `push=.19`, `complete=.01`). The 0.15 s agent delta is attach/
  provider variance; the gate is orchestrator-side and adds no served-lite hot
  path beyond one metadata lookup already in memory.

Failures retained: the first timing-harness invocation omitted its required
message and exited before mutation; the first read-only sandbox-candidate query
used nonexistent `threads.updated_at`, then was corrected to `created_at`.

The gate remains in place until the final S2 acceptance run. Next: remote tmux
reattach/preservation, followed by the §6.1 agent-local `/workspace` inventory.

## §6.1 agent-local session-state inventory — audit recorded; fixes NOT DONE

The inventory found a premise error in the original PVC framing. A persistent
session's logical workspace is backend-owned, not agent-local. `virtual` uses a
durable object-store prefix, `sandbox`/`vm` uses the remote workspace at
`/home/agent-host/workspace`, and `none` uses an intentionally disposable
`/tmp/srw-scratch-*` backend with file tools disabled. `WorkspaceManager`
delegates session file operations to that backend; its local compatibility path
does not identify the authoritative copy.

Both live `agent-stateless` pods were inspected. In each container,
`/workspace` was an empty root with no child entries. The one ordinary
session-path producer found below it is a recomputable vision-description cache.
Therefore an agent PVC is not the fix: it would neither preserve backend-owned
workspace state nor repair RAM-only state or tools which incorrectly treat a
backend path as a local path. No production fix described in this inventory has
been implemented or marked DONE.

### Filesystem and externally owned state

| Item / path | Writer and lifecycle | Authoritative or external copy | Survival requirement | Decision (not yet implemented) |
| --- | --- | --- | --- | --- |
| Agent-local `/workspace` root | `WorkspaceManager.initialize()` creates the local base directory, but creates configured workspace directories through `backend.mkdir` (`src/core/workspace.py`). | None required; it is not the logical workspace. | None. | Keep it disposable. Both live stateless containers had an empty `/workspace`; do not add a PVC merely for this root. |
| `/workspace/.vision_cache/<sha256>.txt` | `DescriptionCache` stores content-addressed image/PDF descriptions after visual reads (`src/services/description_cache.py`; callers in `src/tools/workspace/files.py`). | The source image/document remains on its durable workspace backend; the description can be recomputed. | No correctness requirement, but a cold cache repeats vision cost. | Declare disposable and move to an explicitly ephemeral cache location such as `/tmp` or `/home/srw/.cache`. Do not promote it to DB/object storage. |
| `/workspace/checkpoints`, `/workspace/logs`, `/workspace/phase_snapshots` | No persistent-session writer was found. Their call sites belong to worker/legacy runtimes (`src/agent.py`, `src/api/app.py`, `src/api/dual_app.py`, `src/graph.py`, `src/core/phase_snapshot.py`). | Session conversation, journal, checkpoint and compaction state is in Postgres; application logs use stdout. | Not applicable to persistent sessions. | Do not provision agent storage for these paths. |
| `/tmp/srw-scratch-<thread>-*` | `ScratchBackend` creates it for backend `none` and removes it on disconnect (`src/core/backends/scratch.py`). | None, by the explicit no-files tier contract; file tools are disabled. | Must not be relied on across claims. | Keep disposable. |
| Operation-scoped temp files | `WorkspaceManager.local_copy`, cloud sync, research-paper download, document rendering and audio helpers create `NamedTemporaryFile`, `mkstemp`, `/tmp/paper_dl_*`, `/tmp/docrender_*` or `/tmp/audio_chunk_*` staging and clean them in `finally`. | Input and completed output live on the workspace backend, cloud provider or other external service. | Only for the duration of the operation. | Keep disposable; retain deterministic cleanup. |
| Logical directories `archive/`, `documents/`, `chunks/`, `candidates/`, `requirements/`, `output/` | Workspace initialization and file tools create/write them through `WorkspaceManager` and its backend (`src/core/workspace.py`, `src/tools/workspace/files.py`). | Virtual: object-store objects and directory markers. Sandbox/VM: remote workspace storage. | User workspace state must survive claims and agent-pod loss. | Already externalized. Prove remote workspace preservation and handoff; do not duplicate it onto an agent PVC. |
| Arbitrary user/tool output | File tools, remote shell, web archival under `documents/external/`, email exports, bibliography generation, KB exports, catalog/workflow exports and messaging attachments all write via the backend. The persistent shell deliberately has no local-agent fallback. | The selected workspace backend is authoritative. | Must survive. | Already externalized; preserve object-store or remote-workspace durability. |
| `uploads/**` | The orchestrator, not the agent, stages thread uploads: SFTP into a sandbox/VM workspace or object-store objects under the virtual thread prefix. Virtual ZIP extraction occurs in orchestrator memory before members are stored. Backend `none` refuses uploads. | Remote workspace or object store. | Must survive. | Already externalized. The generic job upload staging path is not persistent-session residue. |
| `skills/<name>/**` and configured instruction files | Persistent-session attach deploys resolved skills/instructions through the workspace backend (`src/api/persistent_session.py`). Ordinary files use first-save-wins semantics so user edits are retained. | Frozen resolved configuration/catalog plus the durable workspace copy. | Workspace copies and user edits must survive. | Already externalized; do not rematerialize over user-edited files. |
| `skills/present-with-canvas/**` and `skills/present-with-canvas/.srw-managed.json` | The canvas-skill reconciler writes managed assets and a digest ownership manifest through the backend; it deletes only still-owned, digest-matching files. | Durable workspace backend. | Both assets and manifest must survive; losing the manifest weakens the no-clobber ownership rule. | Already externalized; treat the manifest as authoritative session workspace state. |
| `datasources.md` | Datasource setup and live updates write it through `WorkspaceManager`; regeneration preserves the existing user-controlled prefix. | Mostly derivable from DB datasource payloads, with user-authored content only in the durable workspace copy. | Must survive if user content has been added. | Keep on the durable backend and rematerialize only the managed section. |
| Virtual `tools/**`, `contacts/**`, `instructions.md`, `task_brief.md` | `VirtualOverlayBackend` providers synthesize these paths; most are read-only and are not physical agent files. | DB/config/provider state. | No agent-local survival requirement. | Leave virtual; do not copy onto `/workspace`. |
| `.git/`, `.gitignore`, repository files, `repos/<name>/**`, `.worktrees/**`, `.subagents*` | `GitManager`, repository setup and delegation tools operate through the backend/remote shell. Reader-subagent worktrees are cleaned after use; the main tree and delegation manifests are live workspace state. | Live backend workspace is authoritative between successful pushes. Gitea plus the Postgres turn-to-SHA ledger is only an external copy after a successful turn push. Virtual-tier Git is disabled, so its object-store workspace is the only live copy. | Main repositories and manifests must survive; temporary reader worktrees need only survive their call. | Keep authoritative repositories on remote/object storage; clean only explicitly temporary worktrees. Do not assume Gitea has every in-flight change. |
| Cloud folder-mirror files | `CloudSyncCoordinator` deliberately unwraps to the physical workspace backend; local files are only operation-scoped staging. Its listing/dedup maps are in RAM and reseed from the remote listing on a cold attach. | Cloud provider and workspace backend are external truths. | Mirrored workspace files survive; the cache maps need not. Cross-pod push/pull ordering still needs the separate durable generation fence. | Keep files external and caches disposable. Do not use an agent PVC as an ordering mechanism. |
| Rclone mount cache/config/log; overlay upper/work directories | Rclone commands run on the workspace pod and store state under that pod's home (including `.cache/srw/rclone/<thread>/<mount>/`); cloud symlinks/backups and protected overlay upper/work dirs are likewise workspace-side, not agent-side. | The cloud is authoritative for an ordinary mount. A protected overlay upperdir contains an unsynced staged diff with no equivalent cloud copy yet. | Ordinary cache is disposable only after flush/clean unmount. Protected overlay diffs must survive or be staged before workspace teardown. | Preserve or explicitly stage protected overlay state as part of the workspace lifecycle; do not copy it to agent `/workspace`. |
| Memory and knowledge-base artifacts | `RecallStore` uses Postgres/pgvector; optional graph data uses Neo4j; KB writes materialize server-side into the project KB repository rather than agent storage. Explicit KB exports are ordinary backend workspace files. | Postgres/pgvector, optional Neo4j, project KB repository, or durable workspace export. | Must survive. | Already externalized. The deprecated workspace-memory manager is not wired into the persistent-session path. |
| Canvas source, metadata and published bytes | The canvas tool sends a workspace source pointer to the orchestrator. The source remains on the workspace backend, canvas metadata is in Postgres, and the last published eligible bytes are stored in object storage. Canvas processes/browser state run on the workspace pod. | Workspace source + Postgres canvas row + object-store published snapshot. | Source and metadata must survive; the snapshot is only the last-published fallback. | Already externalized. Preserve the workspace source and managed-skill manifest; no agent PVC is needed. |
| Session logs | Persistent-session application logs go to stdout/journal; rclone logs are on the workspace pod. | Logging infrastructure, Postgres journal and remote workspace where applicable. | No session correctness dependency on an agent-local log file. | Keep agent-local logs disposable. |

### RAM-only state and backend-path bypasses

These are the actual persistence or correctness gaps found by the inventory.
They are proposals, not completed fixes.

| Item | Producer / consumer and failure mode | Existing external copy | Survival requirement | Decision (not yet implemented) |
| --- | --- | --- | --- | --- |
| `SessionTaskManager._tasks` and `_next_id` | A new in-memory manager is created on every attach; task tools mutate it and emit `tasks.updated`. A handoff loses the user's checklist and ID sequence (`src/managers/session_tasks.py`, `src/api/persistent_session.py`). | None. | Must survive turns, reloads and agent handoffs. | Externalize to Postgres keyed by thread. A workspace JSON file is weaker and cannot support backend `none`. |
| File-undo checkpoints | Write/edit callbacks place full pre-write contents in `PersistentSession.file_checkpoints`; the undo route consumes this process-local map. | No exact copy. Sandbox/VM may have later Git commits and a Postgres turn-to-SHA ledger; virtual Git is disabled. | Must survive if REST undo remains a product promise. | For sandbox/VM, restore through a proven Git/turn-ledger operation. For virtual, use object-store versioning/snapshots or a durable DB/blob undo store. Otherwise fail closed and explicitly declare undo unsupported for that tier. |
| Memory-extraction interval cursor | `_last_extraction_turn` starts at zero on each attach and advances only in process. Extracted memory rows survive, but a new claimant can repeat extraction and its auxiliary LLM work once the global turn count exceeds the interval. | Memory rows, but no cursor. | Must survive or be derivable to prevent repeated extraction/writes. | Persist a thread cursor in Postgres or derive the last covered turn from durable memory metadata such as `source_turn_end`. |
| Read-before-write and instruction stamps | `ToolContext` keeps recent-read hashes, pinned reads and instruction stamps in RAM. A new claimant forgets them and requires a reread. | None. | Correctness does not require survival; forgetting is conservative, but adds tokens and latency. | Explicitly declare disposable per claim and measure cold reread cost. Persist only immutable version stamps if the UX later requires it. |
| Cloud citation anchors | WebDAV reads register `_cloud_anchors` in `ToolContext`; a later citation consumes them. A handoff between read and cite silently loses provider drift/version metadata. | A citation source/snapshot becomes durable only after citation registration. | Must survive across turns if cloud provenance is promised. | Persist per-thread/path anchor metadata in Postgres or durable workspace metadata. |
| Cloud-sync listing/dedup caches | `_local_state`, `_remote_state`, `_remote_dirs`, `_pushed_sizes` and `_remote_seeded` are process-local. A cold claimant reseeds them from remote listings. | Cloud provider and workspace backend. | No semantic survival requirement once durable generation fencing is correct. | Declare disposable and measure cold-listing cost. |
| Other process caches | Citation source registry and delivered-reply dedup state cache durable DB/journal truth or intentionally implement process-local at-least-once behavior. | DB/journal where applicable. | No filesystem persistence requirement. | Keep disposable. Background outbox, presence and interrupt ownership remain separate S2 work rather than `/workspace` residue. |
| WebDAV local-path bypass | `webdav_read` takes `workspace.get_path("documents")`, calls local `os.makedirs`, and downloads directly there; `webdav_write` tests/uploads a local `workspace.get_path(source)` (`src/tools/webdav/tools.py`). A sandbox path names a directory on the remote workspace pod, while a virtual path is an object key; neither is a valid agent-local path. | Intended destination/source exists only through the workspace backend. | The operation must work correctly; persistence alone cannot repair it. | Stage downloads in `/tmp`, then call `backend.write_file`; upload through `workspace.local_copy`. Preserve the cloud anchor in durable metadata. |
| Research-paper local-path bypass | Research download code infers remoteness from `backend.host is not None`. A virtual backend has no host, so its object key is treated as an agent-local path, commonly under read-only `/app` (`src/tools/research/workflow.py`, `src/tools/research/papers.py`). | Intended workspace backend. | The completed paper must reach the backend; temporary download need not survive. | Always download to an operation-scoped temp path and write through the backend, or introduce an explicit local-filesystem capability. Do not infer path semantics from `host`. |
| Citation cloud-snapshot path ambiguity | Citation resolution uses `get_path` during cloud-anchor lookup. It later registers the relative path through backend-aware copying, but the cloud snapshot attempt can still receive a remote-resolved path as though it were local (`src/tools/citation/sources.py`). | Durable citation/source row exists after successful registration. | Provider snapshot/provenance should not silently disappear at a handoff or backend boundary. | Fold this into durable cloud-anchor metadata and make all byte access backend-aware. |

Inventory boundary: the agent-local PVC proposal should be removed from the S2
solution. The implementable route is to retain backend-owned workspace data,
move the one true agent cache to an explicitly disposable location, correct the
three backend-path bypasses, externalize task/undo/extraction/anchor state, and
document safe cold-start caches as disposable. Remote workspace preservation,
protected-overlay staging and the broader S2 daemon/outbox/presence work remain
unverified and are not changed by this audit.

## Phase 0 audit hardening — DONE

The first Phase-0 implementation classified only the materialized declaration
at `metadata.config_override.workspace.backend`. An adversarial schema audit
found that insufficient: live upgrades provision a workspace/VM first and only
then best-effort the declaration update, so old rows can say `virtual` while
still carrying a real remote binding. The gate was hardened before starting
tmux work:

* one shared classifier requires an exact `virtual`/`none` declaration, allows
  only the Gitea repository keys in otherwise-lite workspace context, and
  refuses any VM context, physical workspace context, remote/malformed binding,
  missing tier, or future shape;
* stateless human input re-reads and classifies the authoritative thread row
  `FOR UPDATE` inside the same message/watermark transaction;
* control admission includes metadata in its existing locked thread read and
  classifies before its high-water mark, request, or queue write;
* claim-bundle retains the final pre-credential defense for legacy/manual rows;
* both sandbox and VM live-upgrade endpoints require the exact pinned lane
  before grants or provisioning, and generic config mutation cannot switch a
  stateless row to a physical backend. `abort-vm-upgrade` deliberately remains
  callable so cleanup cannot be fenced out;
* operator unpark remains kind-agnostic and may cause one refused claim/park
  cycle; the claim-bundle is its safety boundary. Raw SQL lane changes remain
  an operator responsibility.

Verification:

* focused gate plus config-plumbing suite: **161 passed**; Ruff, format and
  `git diff --check` clean;
* the actual running orchestrator contained the shared classifier, locked-row
  recheck and one `S2 lite-only gate` marker, and no merge marker remained in
  its database module; both running agent pods contained the turn-timing marker;
* bounded live proof used the ended sandbox fixture with an existing `remote`
  binding, changed only its declaration/lane to `virtual`/`stateless`, and
  restored it via an exit trap. Input and control each returned **409**;
  message/control/queue counts stayed **0|0|0 → 0|0|0**; final state was
  **pinned|sandbox|remote**.

Timing evidence remains honest. Before the original gate, the virtual probe was
**9 s wall / 5.73 s agent** (bundle .10, attach 1.65, controls .02, turn 3.75,
push .19, complete .02). After that first implementation it was **7 s wall /
5.88 s agent** (bundle .16, attach 1.96, controls .01, turn 3.55, push .19,
complete .01). The audit-hardening probe was accepted in **34 ms**, claimed
with a **.059 s** bundle, and completed exactly once, but cluster egress made it
an intentionally ugly measurement: **256 s wall / 188.01 s agent** (bundle
.07, attach 1.83, controls .01, turn 186.03, push .05, complete .01). Repeated
provider `APIConnectionError` retries account for the turn time; the gate's
orchestrator-side bundle stayed below both earlier samples. This is useful
failure evidence, not a claim about normal post-change latency.

Operational failures retained: Tilt restarted from an old partially-edited
image containing a merge marker even though the working tree was clean; direct
container inspection caught it. The normal all-image rebuild then failed twice
because Docker bridge networking could not reach npm (`ENETUNREACH`, masked as
"Exit handler never called"). A temporary uncommitted Cockpit build
`network='host'` override let Tilt converge; it is not an S2 change and will be
removed. Two bounded SQL-probe attempts restored safely but exited before the
HTTP assertions: one concatenation needed parentheses around JSONB `#>>`, and
one command substitution retained a newline. Neither left a row changed.

Phase boundary: the foot-gun is closed even for stale upgraded rows and
sanctioned upgrade/config paths. The lite positive path was accepted, claimed,
answered and completed despite degraded provider egress; a normal-latency
post-change sample remains unavailable. The temporary gate remains installed
until full S2 acceptance. Live queue invariant after the probe: **0
`worker_batch` rows**.

## Phase 1 remote-shell handoff substrate — DONE; sandbox admission remains closed

This phase implemented and verified the remote-shell half of S2 without
removing the lite-only admission gate. The brief described this as
"reattach-if-exists", but a plain `tmux has-session` is not a safe ownership
protocol: an expired claimant can still type into the successor's pane, an
agent can die between reserving a pane and sending a command, tmux target names
are prefix-matched by default, and a completed command's captured output can be
mistaken for an idle prompt. The implementation therefore grew into a durable
handoff protocol rather than adopting the smaller design literally.

### What landed in this phase

* `RemoteBackend.disconnect()` is transport-only and genuine teardown is an
  explicit disposition. Queue claim switches preserve the workspace-side tmux
  session; genuine session end destroys it. A terminally retired backend
  object cannot reconnect from a cancelled Python worker thread.
* Stateless claim ownership is bound before workspace, Git or cloud attach can
  issue a shell command, then eagerly promoted under a workspace-side lock.
  Physical sessions are never kept in the agent's warm-affinity cache: the old
  backend is retired and detached while its lease is still held, before the
  queue unit becomes claimable. Lite affinity remains unchanged.
* A durable `active | creating | retired` shell-generation record and monotonic
  lease-token fence serialize create, crash recovery, promotion and teardown.
  Tmux mutations use the exact full thread id, exact session/pane targets, one
  managed pane, and compare-and-set pending-command guards. State-changing
  metadata and `send-keys` happen inside the same workspace-side lock.
* Commands, including asynchronous commands, carry unique completion records.
  The parser accepts only an exact sentinel, integer exit status and absolute
  resulting working directory. Working-directory enter, user command,
  restoration and the completion record are one guarded command, so a crash
  cannot expose an unguarded changed cwd. Captured output retains the tail when
  it exceeds the 5 MiB transport cap.
* Reattach reconstructs and validates tab type, pane id, setup state, pending
  command, protocol version and full owner identity. Split panes, malformed or
  future metadata, incomplete setup and unknown topology fail closed. First
  pane id `%0` is accepted. Prompt text is not an ownership signal: a real
  tmux experiment proved that a running Bash command can print `$PS1` while
  `pane_current_command` remains `bash`.
* The stateless Deployment now receives the existing platform workspace SSH key
  through the same read-only Secret mount used by persistent agents. Workspace
  NetworkPolicy admits `srw-agent-stateless` only to SSH/CDP ports and only when
  the stateless Deployment is enabled. No Secret payload is copied into chart
  values or test output.

Pinned sessions deliberately retain their established destructive fresh-init
behavior. Only a lease-token-bearing stateless backend can adopt an existing
remote shell. That avoids silently changing pinned resume semantics while the
separate pinned end/resume teardown race remains unresolved.

### Measurements and live verification

The local baseline was loaded dynamically from `HEAD` without switching the
Tilt-watched branch. Its first command took **0.560 s**, destructive detach
took **0.005 s**, and the next init/command took **0.572 s**; exported state did
not survive. The new protocol took **0.028 s** to create/claim and **1.424 s**
to initialize/run, then **0.069 s** to promote a successor and **0.686 s** to
reattach/run. Exported environment and cwd survived, and the old token was
rejected. A command producing more than 5 MiB of scrollback still found its
completion record in **1.443 s**. The `$PS1` forgery stayed guarded, a
colliding command was refused, and explicit cancel recovered the pane.

The k3d proof used a disposable sandbox workspace and the production
`RemoteBackend` directly from two different stateless Deployment pods; it did
not change the thread's execution lane or enqueue queue work. Pod A, token
1001, claimed in **0.253 s** and initialized/ran in **2.479 s**, exporting
`SRW_HANDOFF=cluster-ok` and changing cwd to `/tmp`. Pod B, token 1005,
claimed in **0.249 s** and reattached/ran in **1.143 s** with both values intact.
Pod A's stale token was then rejected. Token 1006 retired only the synthetic
session and the test marker/lock were removed after every harness process had
exited. Final cluster invariants were two updated/Ready stateless replicas,
zero leased queue rows, and zero `worker_batch` rows.

One live failure changed the protocol. Tmux 3.4 rendered the proposed ASCII
unit separator in `list-windows` output as the literal text `\\037`, so the
successor saw one malformed field and refused reattach. The delimiter is now
`|`, which the strict field vocabularies exclude; the images were rebuilt and
the two-pod proof above then passed. This is why the cluster result differs
from the initial mocked transport design.

Tilt's first two final rollouts again failed in the unrelated Cockpit
`npm ci` step because bridge-network egress was unavailable. The previously
proven temporary Cockpit `network='host'` build override allowed build 150 to
converge and was removed before staging. Both current stateless pods and the
orchestrator then matched working-tree hashes and contained the timeout-reset,
joined-capture, delimiter, durable-fence and destructive-drain markers. This
image inspection, not Tilt's aggregate status, is the deployment evidence.

The pinned README-path smoke reached a current-image pinned pod and initialized
one shell/tab in **1.261 s**. Its requested turn then recorded the expected
durable error after provider `APIConnectionError` retries, so no successful AI
reply is claimed. The disposable thread was soft-ended and its workspace pod
removed afterward.

The pre-commit adversarial pass found four regressions outside the successful
handoff path, so this phase was not marked DONE until they were fixed. Tool
timeout recovery had still assumed `disconnect()` killed tmux; it now performs
an exact-owner reset before reconnect and refuses to reconnect if that reset is
not proven. Pinned drain now explicitly destroys the shell before asking the
orchestrator to snapshot. Tmux capture uses joined logical lines so a cwd wider
than the pane cannot strand a strict completion record. Finally, stale
prompt-looking scrollback is consulted only while a durable pending guard
exists; it cannot block a new command on an idle pane. A real tmux 3.7b probe at
width 20 produced **0** strict records without `-J` and **1** with it. A second
probe killed the exact session during `sleep 2; touch ...`; the late file was
absent after 2.1 s.

Focused verification covered the ownership state machine, crash boundaries,
stale-token and prefix-neighbor rejection, exact pane topology, sentinel CAS,
setup retry, async collision rules, timeout reset, pinned drain ordering,
terminal retirement and queue-release ordering: **842 passed** with four known
mock-cleanup warnings. The repository-wide run completed with **15,277 passed,
11 failed and 121 skipped**. The 11 failures exactly match the established
environment baseline: no Postgres on localhost, MCP subprocess/loopback
transport failures, and unavailable external research clients. Restoring the
two existing public tiktoken assets to `/tmp/data-gym-cache` first prevented a
network-only inflation of that count; no cache file is part of the change.
Ruff and format checks pass, Helm lint passes for both supplied values files
with stateless mode both off and on, and `git diff --check` is clean.

### Boundary and remaining S2 work

This phase is DONE only as a gate-closed shell-handoff substrate. It is not
authorization to remove the sandbox/VM admission gate, and S2 is not DONE.
Before a physical thread may enter the lane, all of the following still need a
durable design and fault-injected proof:

* bind shell ownership to the authoritative workspace backing and runtime
  incarnation, including planned suspension, container restart, Pod/PVC
  replacement and split-brain recovery;
* move the lock/marker authority out of the workload user's writable home (or
  explicitly accept a cooperative-only threat model); surface terminal
  retirement failures instead of swallowing them, and make lifecycle
  completion wait for or reconcile cleanup;
* implement the cloud push-generation fence. The successor must not pull until
  the predecessor's exact generation is acknowledged; the current 60-second
  background-push cutoff and detach-time push cannot provide that guarantee;
* re-home or explicitly retire the rclone refresh and overlay-heal daemons,
  post-turn outbox work, hard-interrupt routing and canvas presence;
* implement the §6.1 RAM/path-bypass decisions: durable task/undo/extraction/
  cloud-anchor state, backend-aware WebDAV/research/citation paths, and explicit
  disposal of the remaining cold caches;
* define pinned-to-stateless cutover and rollback. Marker-absent existing tmux
  and tokenless access to a stateless marker intentionally fail closed today.

No migration was needed for this slice. The agent-local PVC proposal remains
rejected: the durable shell and files belong to the remote workspace, while the
actual agent-local/process residue needs targeted externalization rather than a
per-thread volume on the shared Deployment.

## Phase 2 cloud sync-generation fence — DONE; tier gate remains closed

§3.3 now has a durable handoff protocol and a forced two-pod proof. This is a
phase boundary, not authorization to remove the lite-only gate: the default
`rclone_mount` path and the §3.4/§3.5 resident/background work remain open.

### Design that landed

The first design used a durable Postgres requirement plus a resource marker and
proposed a forced full upload for normal completion and recovery. Adversarial
testing rejected that design. It preserved same-size local edits, but overwrote
unrelated cloud-side edits to untouched files and would have turned the measured
pathological fresh push (**121 s for 28 files**) into the ordinary turn cost.
The shipped protocol instead records a content baseline and commits only the
delta.

Migrations **0122** and **0123** define one row per thread and stable logical
cloud destination. Migration **0124** is comment-only: 0123 had already been
applied and checksummed, so its stale “v2 marker” comment was corrected without
rewriting an applied migration. The queue lease token is the required
generation. Each row is bound to the orchestrator-owned workspace generation,
a SHA-256 of the non-secret destination scope, and a canonical per-path
`{sha256, remote_etag}` turn-start baseline. `thread_mounts.id` was rejected as
the key because `replace_thread_mounts` deletes and reinserts those UUIDs. The
stable identity is the legacy-session name or a digest-derived mount key.

Claim start is now **reconcile predecessor -> strict pull -> capture content
baseline -> ARM current generation**, all before tool or LLM work. The whole
configured scope set is validated before any remote write, so a removed or
duplicated mount cannot hide a pending generation. Missing or older resource
markers replay only the baseline delta. An exact marker with a lagging DB ACK
is ACK-only and performs zero recovery uploads. A marker ahead of the DB row,
a pending scope mismatch, malformed baseline, stale Ready endpoint, or changed
workspace binding fails closed.

The v3 resource marker carries the bounded, validated post-commit manifest. It
is namespaced by thread and scope so two threads sharing a WebDAV root do not
overwrite one another. Membership lets the next strict pull distinguish a
real remote deletion from a fresh local file without granting an edited marker
authority over ignored paths. Same-size local and cloud edits are detected.
Unrelated cloud edits are preserved. Local deletions use idempotent, fenced
WebDAV deletes. A process that writes or deletes a durable workspace file after
the predecessor's marker leaves a real next-generation delta rather than
causing a false ACK.

Pulls no longer trust equal file size on a fresh pod. Bytes are staged beneath
`.srw`, written with a write-all loop, exact-replaced, then read back and hashed
before sync state advances. Directory/file collisions and remote command
failures fail closed. Strict sandbox walks propagate root and nested listing
errors. OpenCloud rechecks ownership before a second write after a 401 refresh.
The public workspace `move()` contract and pinned sync algorithm remain
unchanged; exact replacement is a cloud-staging-only primitive.

The executor captures immutable claim requirements for the push task and waits
for it to become terminal before every queue completion or release. The old
60-second “complete while the push may still run” edge is gone. Stateless
teardown does not issue the old raw, unacknowledged second push; pinned teardown
keeps its established push/pull behavior. A claim-start sync failure kills the
loop and releases the unit without advancing the consumed-input watermark.

Two compatibility findings were absorbed. `backend=none` has neither file
tools nor a durable source identity, so cloud payloads are suppressed **only on
the stateless lane**; pinned `none` retains its old behavior. A stateless virtual
attach now exposes its workspace generation only when the binding's backing id
exactly matches the current deterministic object-store backing. The final
generation from a late workspace fetch is retained even when no coordinator is
built, so a degraded payload cannot conceal a pending predecessor row.

### Authority boundary

The database half and resource half deliberately have different strength.
ARM, LOAD, CURRENT and ACK are **control-plane enforced among honest
executors**: each SQL operation proves the exact leased queue token and the
exact current workspace binding/Ready endpoint generation. The WebDAV bytes,
resource marker and per-write rechecks are **cooperative**, not an external
transaction. A PUT that passed its check and then stalled can still finish
after a lease steal. Closing that window needs conditional server-side writes
or immutable generation objects plus a CAS pointer. Same-path concurrent edits
retain the existing last-writer semantics; the phase does not claim
transactional WebDAV ordering.

### Verification and measurements

* Final focused review: **133** generation/transport/integration tests, **229**
  workspace-backend tests and **29** focused generation-fence tests passed.
  Adversarial cases cover same-size edits on both sides, unrelated concurrent
  cloud edits, empty ETags, remote and local deletions, post-marker workspace
  writes, marker-before-DB-ACK recovery, partial multi-mount recovery, malformed
  or ignored manifest entries, strict walk failures, duplicate logical keys,
  idempotent deletes, OpenCloud 401/lease loss, short writes, staged replacement
  failure, and start failure without input consumption.
* A rollback-only real k3d Postgres transaction executed the actual schema and
  application ARM/LOAD/ACK SQL. A token bump rejected stale CURRENT and ACK.
  The transaction left **0 generation rows** and **0 probe leases**.
* The schema snapshot was regenerated at migration head **0124**. Applied
  migration checksums were preserved rather than edited in place.
* The repository-wide comparable run completed with **15,351 passed, 11 failed
  and 122 skipped in 783.74 s**. The 11 failures exactly match the established
  environment baseline: localhost Postgres, MCP subprocess/loopback transport,
  and unavailable arXiv. An earlier `.venv` run reported 23 failures because
  that Python 3.13 environment lacked the declared `asyncssh` dependency; it is
  not the baseline and is recorded to avoid repeating the count error.
* Ruff check, repository-wide Ruff format check and `git diff --check` are
  clean.

The live proof used fallback `sync` deliberately: Helm defaults to
`rclone_mount`, which bypasses the push/pull coordinator and would make a green
handoff vacuous. Every non-terminating stateless pod first matched the working
tree hashes and contained the v3 marker, committed-manifest, write-all,
exact-replace and generation-path code.

On a disposable real sandbox PVC and real Nextcloud folder, pod A captured a
two-file baseline, changed one local file `1111 -> ABCD`, and an independent
cloud writer changed the other `2222 -> WXYZ`; both edits preserved byte length.
Generation N committed **1 upload / 4 bytes**, made **12** ownership checks,
wrote one v3 marker and one DB ACK. Its comparable full generation cost was
**12.140 s**; the delta portion was **2.35 s**.

After confirming no queue lease, pod A was force-deleted. Pod B read the marker
in **6.657 s**, reconciled ACK-only with **0 recovery uploads** in **7.101 s**,
then pulled N+1 in **14.516 s**. Its workspace contained exactly `ABCD` and
`WXYZ`; it downloaded **2 files / 8 bytes**, made **4** ownership checks and one
ACK. SSH setup took **0.154 s**. Re-downloading the already-committed local file
is safe but redundant and is the main measured optimization left in §3.3.

Harness failures were kept rather than hidden: the first isolation attempt used
the session-folder root and stopped before a marker; a fresh Nextcloud child was
briefly inconsistent; and the first successor assertion expected one download,
revealing the safe two-download behavior. Cleanup verified zero `srw-e2e-*`
cloud entries, removed the workspace child, restored the fixture to
`ended|pinned`, and left **0 generation rows**, **0 leased queue rows**, and
**0 worker_batch rows**.

### Remaining S2 boundary

The rclone/overlay path is §3.4, not evidence supplied by this phase. The tier
gate remains closed until mid-idle token expiry heals on a new claimant, the
resident controllers and post-marker writers have safe ownership, §3.5's
outbox/interrupt/presence decisions are implemented or explicitly accepted,
and the remaining workspace-incarnation/terminal-retirement and §6.1 items are
resolved. No worker batch was enqueued.

## Phase 3 resident cloud-mount controllers — DONE; tier gate remains closed

The two processes named as “resident daemons” in the design are not agent-side
daemons after this phase. The rclone/FUSE process and protected overlay remain
resident on the workspace, while the bearer refresh and ENOTCONN monitor are
claim-scoped controllers. On stateless handoff, the current owner cancels and
retires only those local controllers and preserves the workspace processes. The
next exact lease owner publishes a newly minted bearer, forces a bounded root
`vfs/refresh recursive=false`, performs a real directory read, and either
adopts the healthy resident or exactly heals it. A sidecar or orchestrator cron
was rejected because either route would move the long-lived credential client
or SSH scheduling authority into a broader resident component without buying a
stronger external fence.

The lifecycle disposition is explicit and separate from shell preservation:
`preserve_workspace_daemons` is used only for a stateless physical handoff.
Pinned cleanup and genuine terminal cleanup keep their historical destructive
unmount behavior. A stateless setup failure detaches inherited controllers
locally rather than destroying a predecessor's resident resource. Partial
startup rollback tracks ownership and never unmounts a healthy adopted mount.
The fresh bearer used by token refresh also becomes the seed for any later
ENOTCONN restart, fixing the prior path that could reinstall the expired
attach-time token.

Rclone resident identity is a strict, non-secret digest plus a
`creating -> active` generation, exact PID/cmdline and RC identity. Token writes,
probe/heal scripts and terminal mutations run through a separate claim-resource
primitive that shares the exact remote lease token and durable flock but does
not depend on a tmux pane or the local shell-admission bit. Script and token
staging paths are controller-unique. Unknown source keys or provider flags are
not hashed as a secret-shaped oracle; they conservatively force remount.

Protected overlay has the same claim-aware split. A healthy exact-config
overlay is adopted, a dead lower is healed once, the upperdir is preserved, and
the workdir is freshly recreated for every new fuse-overlayfs mount. The
`creating -> active` marker makes crash recovery explicit. A cancelled or stale
claim cannot resume an unmount/remount after the successor promotes.

### Authority boundary

The queue lease and exact remote token fence are enforced between honest
executors. The resource identity and flock themselves live below the workload
user's `$HOME`, and tmux/rclone options are writable by that same user. This is
therefore a **cooperative correctness protocol**, not a security boundary
against a model or user command that deletes, forges or replaces the marker or
lock. An enforced version would need a root-owned workspace supervisor or an
external conditional control surface; neither exists in S2.

### Live verification and failures retained

The first complete live attempt was stopped before acceptance evidence because
the fake bearer proxy was not actually listening; a later generated proxy file
also had a quoted-newline syntax error. Both failures were harness defects, and
both runs were fully cleaned without changing the fixture lane or queue.

The corrected checkpointed proof then passed the required expiry/handoff path:

* Pod/controller A cold-mounted a real rclone/FUSE resource and read
  `alpha-from-webdav`. Controller-only detach took **0.001 s**.
* The old bearer returned **401** and the fresh bearer **200**. Controller B
  claimed in **0.502 s**, forced the root VFS refresh in **0.266 s**, and adopted
  the same PID and resident generation. The retired A object failed locally and
  a separately constructed old-token backend was rejected by the remote fence
  without changing the token.
* The protected overlay was adopted in **0.287 s** and its upper file remained
  `upper-survives-handoff-and-heal`.

The same run's optional dead-lower fault found a product bug. After the exact
rclone PID died, `mountpoint -q` could report false while the kernel still held
an ENOTCONN FUSE entry. Best-effort cleanup then let remount reach `mkdir -p`,
which failed on the stale target. The stateless exact teardown now checks the
mount table with `findmnt -C -M`, performs bounded normal and lazy FUSE detach
with a bounded `sudo -n umount -l` fallback, and returns success only after the
mount entry is absent and the target directory is accessible. Heal/restart
requires that success before remount; a live or reused mismatched PID still
fails closed before mutation. Pinned teardown was not changed.

The final Gate-C-only rerun passed on the exact rebuilt image. Cold rclone mount
took **1.043 s** and cold overlay mount **0.515 s**. Killing PID `294` left the
expected stale mount-table entry. Exact heal took **1.212 s**, produced PID
`623` and a new resident generation, restored the lower read, preserved
`upper-survives-gate-c-heal`, and used a fresh overlay workdir. No privileged
fallback was needed in that run.

Every current stateless pod and the orchestrator matched the working-tree hashes
for all seven touched production files before the measurements. Final cleanup
removed proof mounts, processes and paths, restored the disposable thread to
`ended|pinned`, and left **0** fixture queue rows, **0** global leased/queued
rows and **0** active `worker_batch` rows.

### Verification

* Final focused cloud-mount, RemoteBackend and overlay suite: **353 passed in
  1.32 s**; generated shell scripts also passed `bash -n`.
* Final repository-wide comparable run: **15,392 passed, 11 failed, 122 skipped
  in 805.61 s**. The 11 failures are exactly the established environment
  baseline: localhost Postgres, MCP subprocess/loopback transport, and optional
  arXiv/Semantic Scholar clients.
* Ruff check, repository-wide Ruff format check and `git diff --check` are
  clean.

Pinned behavior is covered by explicit branch tests and the full suite; this
phase did not run a separate live pinned cloud-mount smoke. The tier gate stays
closed. §3.5 outbox, interrupt and presence re-homing, workspace-incarnation and
terminal-retirement authority, and the deferred §6.1 RAM/path-bypass work remain
unverified. No worker batch was enqueued.

## Phase 4 durable attached-client presence — DONE; Canvas remains separate

Migration **0125** and the generic session-presence slice are implemented on
`feature/stateless-agents`; nothing was pushed. The existing owner-gated SSE
stream is now the lane-free client attachment signal. The Cockpit sends no lane
and gained no new endpoint or heartbeat: a stateless SSE establishment writes
one database-clock TTL row per thread, renews it only after re-running
`require_thread_owner` with the current BFF cookie, and leaves the row to expire
on disconnect. Pinned streams do none of those writes and retain their exact
process-local `_subscribers` behavior.

One row deliberately means “at least one attached client”, not a refcount.
Concurrent tabs renew the same row; closing one cannot decrement another;
reload within the 30-second TTL has no presence/status flicker. A genuine
expired/absent → live establishment clears `awaiting_user`, but periodic renew
and a second within-TTL establishment do not. This is necessary for polite
mode: its `awaiting_user` state is an explicit review pause and must remain so
while an already-attached viewer watches it.

The two stateless natural-pause sites and permission timeout now read this
durable oracle. Eager and sudo pause only when the TTL is absent; polite pauses
regardless. The permission expiry is an exact lease-fenced CAS and returns the
live TTL remainder, so a tab closing just before the five-minute permission
slice is reconsidered at presence expiry rather than after another five
minutes. Unknown presence retains the card. A leader-gated convergence pass
promotes only stateless, non-officer, post-first-turn threads whose queue is
durably `done` after the final TTL expires. Its failure has an independent
exception boundary and cannot suppress the pre-existing awaiting-user
suspension sweep.

The final client audit found a separate live-rendering mismatch: the Cockpit
mapped every non-`approved` `permission.resolved` frame to `denied`, even though
the agent deliberately distinguishes `expired`/`interrupted` as NO_ANSWER and
REST history treats only literal `denied` as a refusal. The lane-free SSE
reducer now always retires the no-longer-actionable card but dispatches a turn
decision only for literal `approved` or `denied`. No execution-lane field or
new UI copy was introduced.

### Authority and concurrency decisions

Presence is explicitly **cooperative UX state**, never authorization, queue
ownership, a fencing token, or worker/finalizer liveness. Natural-pause status
uses the exact current lease predicate but is an advisory lifecycle marker; the
turn and journal stores retain their stronger persist fences. Taking a queue
`FOR SHARE` and then the threads row here would invert stateless `/input`'s
existing threads→queue order and create a deadlock cycle, so that lock was not
added to the lifecycle update.

Permission expiry is irreversible and therefore does take `run_queue FOR
SHARE`. It also serializes against SSE refresh with the same per-thread advisory
lock. The first implementation put both operations in one SQL statement. A real
Postgres race proved that wrong: a statement which starts and then waits for the
advisory lock retains its pre-renewal snapshot and expired the card after the
renewal committed. The final form acquires the advisory lock as statement one,
then runs the fenced CAS as statement two in the same READ COMMITTED
transaction, which gets a fresh post-renewal snapshot. The forced race now
leaves the card pending.

### Verification and measurements

* Fresh PG15 migration replay applied 0125 in **16 ms** and regenerated the
  12,739-line app snapshot. The k3d ledger reports
  `0125_thread_client_presence.sql|true`; the table and expiry index exist.
* The real scratch-PG contract passed **101/101**, including five new presence
  cases: multi-tab/polite preservation, expired re-establishment, eager versus
  polite pause, done-only expiry promotion, and the forced renewal/permission
  advisory-lock race.
* The affected focused suites passed **121 tests**. Repository-wide pytest
  returned **15,403 passed / 127 skipped / 11 failed in 793.25 s**. The eleven
  are exactly the established environment baseline (localhost Postgres, MCP
  transport and optional arXiv/Semantic Scholar clients).
* After the final concurrency and client-review fixes, the affected Python
  suites passed **123/123** and the focused Cockpit service suite passed
  **229/229**, including both `expired` and `interrupted` live outcomes. The
  production Cockpit build completed successfully; its pre-existing bundle
  budget and CommonJS warnings remain warnings only.
* The running Cockpit pod contains both the exact live-reducer marker and its
  regression-test marker (**1** match each), rather than merely reporting a
  successful Tilt update.
* Repository-wide Ruff check and Ruff format check (**1,055 files**) plus
  `git diff --check` are clean.
* Both running stateless pods hash-match the working tree for
  `persistent_app.py` and `thread_presence.py`; the orchestrator hash-matches
  `main.py`, the helper and migration 0125. An unauthenticated live SSE request
  returned **401** and left **0** presence rows, proving the owner gate precedes
  the write. Exact in-cluster renewal measured **22.37 ms cold, 6.47/7.19 ms
  warm**; cleanup restored zero rows.
* Closeout: **0** presence rows, **0** queued/leased units and **0
  `worker_batch` rows**. The tier gate remains closed.

The first full-suite invocation was stopped after the execution wrapper
detached its output at 6%; the monitored restart above is the comparable
result. No test failure was hidden by that harness restart.

### Remaining boundary

This generic attached-client signal fixes the load-bearing `_subscribers`
oracle, but it is not Canvas editor awareness. Stateless Canvas awareness still
uses the absent control WebSocket and agent RAM fan-out, so its no-flicker
acceptance remains unverified and requires a separate design. No live
BFF-authenticated reload/multi-tab browser exercise or live pinned-session smoke
was run in this slice; server auth ordering, TTL/multi-tab behavior and the
pinned split are covered by route tests and real Postgres. Outbox re-homing,
hard interrupt, gate release, permission-row retirement on lease expiry,
workspace-incarnation authority and the deferred §6.1 RAM/path bypasses remain
open. Gate 3 and migrations 0130+ were not touched.

# Session 8 — lane-free Canvas editor awareness (2026-08-10)

## Phase 5 durable Canvas awareness — DONE; tier gate remains closed

Migration **0126** replaces the Canvas editor-awareness dependency on the
per-session control WebSocket with one owner-authenticated transport for both
lanes. The Cockpit never receives or branches on `execution_lane`. Each browser
editor writes to
`PUT /api/persistent/threads/{thread_id}/canvases/main/awareness/{editing_session_id}`
and every Canvas pane, including the authenticated popout, reads the dedicated
`GET .../awareness/stream` SSE endpoint with its normal BFF cookie.

The table has one row per `(thread_id, canvas_id, editing_session_id)`, a stable
server-minted sender id, a client-monotonic sequence, an `editing` lease or
`idle` tombstone, and database-clock timestamps. Editing heartbeats are
accepted only against the exact current workspace-file path, presentation
revision and source version. A later idle mutation may retire that editor's
exact stored identity after the Canvas itself advances. Lower sequences return
the current row without mutation; an identical same-sequence retry is
idempotent and, importantly, does **not** extend its expiry; same-sequence
payload reuse conflicts; a higher sequence applies. A thread-scoped advisory
transaction lock serializes cleanup, first insert and the exact **256-row**
cap across concurrent tabs. Expired leases and tombstones are pruned after a
bounded **300-second** retention, with a separate limited `SKIP LOCKED` cleanup
path.

The server TTL is **15 seconds** and the client renews every **5 seconds**. The
SSE endpoint sends named `canvas_awareness` events containing complete
snapshots, including an initial or changed empty set. It deliberately sends no
`id:` line, does not touch `thread_events`, and never allocates a session-journal
sequence; reconnect simply reads current Postgres state. It re-runs
`require_thread_owner` every **10 seconds** and closes on authorization loss.
Idle transport keepalives are comments, separate from snapshots. Cleanup
failure is isolated from the live snapshot path. Pinned and stateless clients
now use these same REST/SSE semantics; the old agent-local awareness path is no
longer the Cockpit transport.

The Cockpit controller owns one single-flight mutation queue, persists its
monotonic identity/sequence across reload, replaces remote editor state
atomically from each complete snapshot, filters its own exact identity and
expires a snapshot locally if the SSE is interrupted. A fresh Canvas popout
can inherit its opener's `sessionStorage`; it now rotates that copied identity
once before awareness sync, records a popout marker, and reuses the rotated
identity on reload. This closes the ordinary popout collision without making
the editor identity a credential.

### Failure retained: Tilt applied an intermediate migration image

Tilt applied the first complete 0126 bytes while the implementation was still
being verified. A later, optional attempt to add a JavaScript-safe upper bound
to the already-applied database constraint changed the migration checksum, so
the next orchestrator pod correctly refused startup with `checksum changed`.
The previous pod stayed Ready. The migration was restored byte-for-byte to the
applied checksum
`f555070c47f67ca00386ab96edd6e5fa5f9d22e4cfc0e25772e2ebb8f82b414d`;
the JavaScript-safe bound remains enforced in the API and service instead. The
schema snapshot was regenerated, Tilt rebuilt naturally, and the Deployment
recovered to **1/1 updated and Ready**. The local file, running image and
`schema_migrations` ledger all report the same checksum; no ledger or data
repair was performed.

### Verification

* The final exact 0126 service/migration contract passed **7 real-Postgres
  tests** with **101 deselected**: no-TTL-extension idempotency, idle/reordered
  renewal fencing, exact Canvas identity, concurrent cap serialization,
  pinned/stateless parity with zero journal rows, bounded cleanup and Canvas
  cascade.
* The full scratch-Postgres queue/migration substrate through 0126 passed
  **108/108**. The broader affected Python group passed **66/66**. The final
  route/SSE suite passed **8/8** and the focused migration assertions passed
  **2/2**. An independent focused server run reported **40 passed / 7
  environment skips**.
* The final focused Cockpit awareness/controller group passed **39/39**. Full
  Cockpit Vitest passed **1,815 tests in 119 files**, and the production build
  passed with only the pre-existing budget/CommonJS warnings.
* Repository-wide Ruff check and Ruff format check passed for **1,057 files**.
  `scripts/schema-snapshot.sh --check app` replayed **111 transactional
  migrations** and reported the generated schema current. `git diff --check`
  is clean.
* The comparable repository-wide run under the declared global environment
  finished with **15,412 passed / 134 skipped / 11 failed in 786.29 s**. The
  eleven failures are exactly the established localhost-Postgres, MCP
  transport and optional arXiv/Semantic Scholar environment baseline. A first
  run under the repository's Python 3.13 `.venv` reported 24 failures: the same
  environment set plus the already documented 15 missing-`asyncssh` Canvas
  transport failures and one real stale endpoint-inventory assertion. The
  inventory was regenerated with both awareness routes classified
  `require_thread_owner`; its focused assertion then passed.
* The k3d orchestrator exposed the exact PUT and GET paths in its running
  OpenAPI document, contained the new service code and matching 0126 bytes,
  and its database contained the successful ledger row and awareness table.
  The running Cockpit pod also contained the current source.
* Two independent Playwright browser contexts authenticated through the real
  Keycloak/BFF flow; both received HttpOnly `srw_session` cookies. On a
  stateless DB-only fixture, A's PUT became visible to B through the named SSE
  in **1040 ms**, B's PUT became visible to A in **649 ms**, and a hard reload
  renewed the same editor identity at sequence 2 while preserving the server
  sender id. The server emitted the complete empty snapshot after TTL in
  **14.897 s**. A pinned fixture used the same API with no lane field and
  delivered A's editor to B in **916 ms**. The harness removed **3 awareness
  rows, 2 Canvas rows, 2 threads and 2 BFF sessions**; final harness residue and
  queue units were zero. This proves the authenticated browser REST/SSE
  transport, not the complete Monaco/controller rendering path.

### Accepted courtesy-state limits and remaining boundary

An ordinary browser **Duplicate Tab** operation can clone `sessionStorage`
without the explicit popout marker. Those tabs may therefore collapse onto one
courtesy row while both remain active and fail to show each other; this affects
awareness UX only, never authorization or lease ownership, and the row
disappears after renewals stop plus the 15-second TTL. Browser unload cannot be
relied on to finish an asynchronous PUT, so unload is not a correctness edge:
blur/inactivation sends an idle tombstone when possible and a hard close is
retired by TTL. The authenticated two-context REST/SSE path, hard reload and
pinned/stateless parity were exercised live; the full rendered Monaco/editor
UX and native EventSource recovery through a real authentication expiry were
not. Popout behavior and the local TTL fallback remain covered by tests.

This slice re-homes **ephemeral editor awareness only**. Durable Canvas source
and presentation mutation invalidations still share the legacy control-socket
seam and require their own durable mutation/outbox path. Generic session
outboxes and hard interrupt also remain open, along with
workspace/runtime-incarnation authority, terminal-retirement acknowledgement
and the deferred §6.1 RAM/path-bypass work. No completion-path/Gate-3 behavior,
worker admission or worker queue unit was touched. The S2 sandbox tier gate
therefore remains **closed**.

# Session 9 — exact-lease hard interrupt and the §3.5 stop line (2026-08-10)

## Phase 6 hard interrupt — DONE; tier gate remains closed

Migrations **0127–0129** and the lane-free Cockpit/API/runtime path implement a
durable hard interrupt for stateless sessions without reusing the scalar
control inbox. The public owner-gated endpoint remains
`POST /api/persistent/threads/{thread_id}/interrupt`. A current client sends a
stable `client_request_id` UUID and the exact positive `target_turn_id` it is
displaying. The client never receives or branches on `execution_lane`.

For stateless execution, admission locks `threads` and then `run_queue`, checks
the current exact lease token, and requires that owner to have opened
`interrupt_admission_lease_token` plus `interrupt_admission_turn_id` for the
same turn. The inbox captures the immutable accepted token/turn and does not
wake an idle queue unit or update an input/control watermark. Exact UUID retries
remain observable after the gate closes; reusing the UUID for another turn is a
conflict. The executor starts and initially drains the interrupt consumer before
opening admission. Its terminal sequence is close admission, stop and
shield-join the watcher, then perform one final committed-tail drain before any
queue transition. The old one-second cancel path was removed after a forced
race showed it could orphan the durable receipt writer and double-apply a
request.

Pinned behavior stays on the existing direct-agent route. A legacy missing or
empty body is still forwarded as `{}`. A current correlated body is forwarded
unchanged and the pinned agent validates the exact active turn before setting
its in-process interrupt state. This preserves old clients while preventing an
ambiguous retry from stopping a newer pinned turn.

### Durable intent, receipts and crash recovery

The request row itself is durable stop intent. A live exact owner sets the RAM
interrupt synchronously, then writes one linked `interrupt.ack` through its
existing journal allocator and terminalizes the request. The receipt carries
the request id, client UUID, target turn, applied/rejected outcome and mode.
Applied sibling requests for the same target share one
`result.consumed_input_seq`, so two tabs cannot consume two human messages.
Settlement selects the exact live human message by `turn_number`, resets
`attempts_since_completion`, and never infers the target as merely the next row
above the watermark.

Owner-loss ordering was deliberately changed from the initial rejected-request
design. If the owner dies after admission but before its receipt is durable,
the system writer records an applied hard `owner_lost` acknowledgement and
settles that exact input without signalling successor RAM. Otherwise the turn
could have observed the stop or performed partial side effects and then be
silently re-executed. Existing linked receipts remain authoritative. Recovery
handles both pending rows and old `outcome='applied'` rows missing the consumed
marker, stamps already-consumed historical rows without advancing a later
message, and processes every sibling before changing a parked queue state.

The reaper captures the pre-steal admission turn in the same locking statement
as the lease-token advance. Post-steal journal repair takes the explicit
`threads -> run_queue -> request` lock order, validates the exact post-steal
token, rotates the dead event epoch once, and emits one singular
`turn.interrupted` or `turn.parked` frame per exact target with both
`target_turn_id` and the snapshot-compatible `turn_id` alias. A claimant which
wins first makes the old reaper writer stop. Periodic system-writer repair is
parked-only; queued rows are reconciled by the live claimant so an attached warm
allocator cannot collide with an out-of-process journal writer.

Fresh-attach pending humans exposed an off-by-one trap: restored `turn_count`
already included the unanswered human row, while the graph increments before
opening the interrupt window. After stripping those restored pending messages,
the executor now rewinds `turn_count` to `target.turn_number - 1` before
injection. Stale interrupt reconciliation also moved ahead of pending-row
selection and returns the refreshed consumed watermark, so an owner-loss stop
cannot settle a row and then run the already-selected copy anyway.

The Cockpit treats HTTP 202 as admission, not completion; it retries ambiguous
transport results only with the exact same UUID/turn pair. Receipt and lifecycle
frames are target-correlated, including replay after a snapshot, and an old
covered receipt cannot promote or close a newer recovered placeholder.
`turn.completed` preserves an already-interrupted terminal status. Parked state
uses one stable, replay-idempotent localized operator message rather than
creating duplicates on reconnect.

The three migration files were applied during development and are frozen at:

* 0127:
  `ae59b0f15eac8542b7a0af6a62886a92c710b1715aaa281a7e64b3dbd59ab857`;
* 0128:
  `f30e60be4bf5be4a38be9950c5ca9601180807a85e67b4bbfd132b8deede8eab`;
* 0129:
  `a3de400493cde031e37c695c377d0c3fc64289f5bebbb360930ec3e5ecc6cd1b`.

The non-behavioral 0127 comments say that a successor never applies a request.
The final protocol is narrower and safer: a successor never signals or
retargets the request, but may settle the already-admitted exact stop intent
after owner loss. The applied migration and generated snapshot retain the old
comment because changing those bytes would violate the migration ledger; this
log is the status-of-record correction.

### Verification and live fallback

* The frozen integration group passed **502 tests with 116 environment
  skips**; the runtime consumer/executor owner group passed **65/65**. An
  independent adversarial review passed **419 tests with the same 116 skips**
  and returned GO after the lock, recovery, pruning, reaper and client replay
  audit.
* Full Cockpit Vitest passed **1,829/1,829 across 119 files**. The production
  build passed with the established bundle-budget, SCSS and CommonJS warnings.
  The focused service group passed **295 tests**; English/German translation
  parity remained **2,455 keys**.
* The app schema check replayed **113 transactional plus 16
  non-transactional migrations** and produced the current 13,001-line
  snapshot. The running database ledger contained the three exact checksums,
  both new constraints were validated and the receipt index was valid.
* Repository-wide Ruff check and Ruff format check passed for **1,062 files**;
  `git diff --check` passed.
* Repository-wide pytest finished with **15,481 passed / 142 skipped / 11
  failed in 801.06 s**. The failures are exactly the established baseline: one
  localhost-Postgres connection test, seven MCP transport/wiring tests and
  three optional arXiv/Semantic Scholar client tests.
* Running deployment checks found the orchestrator **1/1 Ready**, stateless
  agents **2/2 Ready** and Cockpit **1/1 Ready**, with **0 leased queue rows, 0
  open interrupt gates, 0 interrupt rows and 0 worker-batch rows**. All checked
  source hashes matched except stateless `persistent_app.py`; a full file diff
  showed that its only difference was Ruff line wrapping, with the final
  recovery markers and behavior present. No manual Tilt trigger was used.

The live bounded proof used a disposable virtual/stateless fixture and a real
Keycloak/BFF-cookie login. A correlated public POST for turn 7 returned **202
pending in 12.81 ms**. In the writer-exclusion fixture, the deployed production
system-frame helper plus exact-lease finalizer wrote one linked
`interrupt.ack` at epoch/sequence **0/1**, applied hard mode and consumed the
exact input in **51.82 ms**. The exact UUID retry returned **202 applied,
duplicate=true in 7.55 ms**. A new UUID targeting old turn 7 and the original
UUID retargeted to turn 8 both returned **409**, while the turn-8 gate remained
open. Cleanup left zero fixture thread, queue, message, event, interrupt, BFF,
pre-auth and generated Keycloak-session rows. This proves the authenticated API,
database exact-target, receipt and replay fences. It does **not** prove a live
main-process RAM signal/unwind, LISTEN watcher latency or a forced-pod interrupt
handoff. No live pinned executor existed and one was not provisioned merely for
this proof; pinned interrupt behavior remains integration-tested rather than
live-smoked.

## Generic §3.5 outbox — STOPPED at the explicit Gate 3 boundary

The outbox design's required invariant is “turn committed if and only if its
follow-up effect was enqueued.” The authoritative final message transaction is
`PostgresDB.save_thread_messages`, reached from `_save_turn_ai_messages` after
`turn.completed` has already been queued. Today that reconciliation catches its
own failures, and its caller proceeds after a five-second timeout; the executor
may then advance `consumed_seq` through `complete_unit`. Adding an outbox INSERT
inside the database method alone would therefore still allow a completed queue
unit with neither the final message batch nor the effect row. Making failure
block/release/reconcile completion is a normal completion-protocol change.

No generic carrier is available in the migration range. `run_queue` has the
`bg_task` class and dedup key but no effect payload, and the claimed-unit shape
does not carry that dedup key. `session_wake_events` has payload but is an
officer-only wake outbox whose claimant consumes every supported source; its
enqueue also opens its own connection and is not atomic with final persistence.
Migrations 0122–0129 are consumed and migrations 0130+ belong to Gate 3. Per
the brief's stop rule, no completion path was modified, no wake table was
repurposed and no live-epoch independent journal writer was introduced.

Ranked remaining effects from the audit:

1. **Blocked on the generic outbox/completion boundary:** the exact main
   `llm_requests` archive (untracked task with an unreconstructable prepared
   request), turn-end memory capture (bare tasks not covered by the existing
   drain), and post-callback Git commit/push plus turn-to-SHA ordering.
2. **Independently re-homeable in later authorized slices:** durable Canvas
   revision snapshots over a dedicated state stream, a pending-citation
   sweeper with an exact thread-bound verdict callback, and orchestrator-derived
   notifications from already-durable journal/permission state. These were not
   started after the generic stop condition triggered.
3. **Deferred:** title generation is already awaited but blocks release, and
   protected-cloud staging still crosses two process-local task registries.
4. **Already closed for this problem class:** cloud generation push is awaited
   before queue transition; presence, Canvas courtesy awareness and hard
   interrupt are durable; ordinary same-owner journal trailing flushes are
   intentionally token-fenced rather than an outbox.

## §6.1 inventory and remaining S2 boundary

The §6.1 audit remains unchanged and deliberately unimplemented in this pass.
It contains **18 filesystem/external-state rows** and **10 RAM/path-bypass
rows**. The decisive result is still that the logical workspace is
backend-owned: the agent-local `/workspace` root is disposable, both inspected
stateless pods had it empty, and an agent PVC would not repair the actual gaps.
The 18 filesystem rows separate disposable cache/temp/worker-only paths from
already-externalized logical directories, user output, uploads, skills,
datasources, repositories, cloud mirrors/mount state, memory/KB artifacts,
Canvas bytes and session logs. The 10 real follow-ups are session-task state,
file undo, the memory-extraction cursor, conservative read/instruction stamps,
cloud citation anchors, disposable sync/dedup caches, other process caches and
the WebDAV, research-paper and citation-snapshot backend-path bypasses.

Those fixes remain a separate authorized pass as requested. Durable Canvas
source/presentation invalidations, the generic outbox, workspace/runtime-
incarnation authority and terminal-retirement acknowledgement also remain open.
The live interrupt proof did not exercise a real executor unwind or forced pod
handoff, and no live pinned interrupt smoke was available. No worker admission,
worker queue unit, job-completion path, migration 0130+ or sandbox lane was
touched. The S2 sandbox tier gate remains **closed**.

# Session 10 — Gate 3 step 1: atomic pinned disposition and critic uniqueness (2026-08-10)

## Scope and Class A merge

Only the two standalone pinned-lane fixes from Gate 3 step 1 were built. The
command table, effects table, completion high-water mark, finalizer and every
step-2 protocol path remain absent.

`PostgresDB.update_job_status` now stamps
`completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)` automatically for
every `status='completed'` transition. The generic completion route, diff
accept and diff reject therefore cannot commit a completed row with a NULL
completion time; the latter two routes' redundant timestamp writes were
removed. The other raw completed transitions already wrote status and timestamp
in one statement.

For a pause, the same helper can bind the exact parsed completion-report freeze
payload into `context.last_freeze_data`, set `freeze_data=NULL`, clear
`assigned_agent_id`, and write status in one UPDATE. The generic completion
route enables that disposition only for the existing auto-redispatch freeze
types. Explicit-action freezes such as `vm_upgrade_required` keep their column
payload while still releasing the agent. A regression which injects failure of
the earlier best-effort freeze persist proves that the final atomic disposition
still stashes the exact report payload rather than a stale row value.

## Immutable critic guard and real-data pre-flight

The chosen schema remains the settled partial expression index rather than a
generated stored column. No caller needs a named constraint, so changing every
jobs row merely to simplify conflict inference would broaden the row shape for
no benefit. The critic writer keeps its ordinary INSERT and handles only a
`23505` naming `jobs_verification_uniq`; unrelated unique violations propagate.
There is consequently no `ON CONFLICT` predicate which can drift from the
index predicate.

The mandatory production pre-flight examined **138 indexed critic candidates
and 138 parent/round keys**: it found **zero duplicate groups and zero loser
rows**, zero missing or explicit-NULL round keys, and zero NULL parents. The
current k3d data had **7 candidates / 7 keys and zero duplicates**. The race has
therefore not left durable duplicates in the production data examined. A
separate Class-A pre-flight did find **one of 382 completed production jobs
with `completed_at IS NULL`**, proving S21 had fired; it found no surviving
paused-agent or auto-freeze dispatcher wedge. This slice fixes future
transitions and deliberately does not invent a timestamp for that historical
row.

The migration chain is:

* **0130** deterministically keeps the earliest `created_at, id` critic for a
  key. Because status is deliberately absent from the final predicate,
  cancellation alone cannot remove a loser. It cancels and unassigns the loser,
  archives its original round, winner and reason under
  `context.verification_dedupe`, and removes only the loser's
  `verification_round` key.
* **0131** is the single-statement concurrent drop of any valid or INVALID
  same-name index shell.
* **0132** is the exact settled `CREATE UNIQUE INDEX CONCURRENTLY`, with no
  status term and no `IF NOT EXISTS`.

An interrupted concurrent build exposed a second-order runner requirement. A
successful 0131 followed by a failed 0132 would otherwise leave 0131 ledgered,
0132 dirty and an INVALID shell; a normal retry could never advance. The runner
now has an explicit, reviewed recovery registry instead of inferring safety
from arbitrary SQL. For 0132 it takes a separate session try-lock across the
physical operation, catalog verification and ledger write; a blocking advisory
lock was rejected after a real test produced a concurrent-index virtual-XID
deadlock. It checks `pg_index` for the exact table, access method, keys,
predicate and unique/valid/ready/live flags, drops an INVALID or wrong-shape
shell through 0131, replays the checksummed/replay-safe 0130, executes the
immutable 0132 bytes, and records success only after the exact invariant holds.
An exact healthy-but-unledgered build is safely adopted. No manual ledger-row
deletion is part of the normal retry.

The applied migration hashes are frozen at:

* 0130: `5bcd766940730961c3f93019d16e6b8ada4e2eb6beed7b6c764fcfd5e158fd2e`;
* 0131: `75d532f151dc2672862b9a688fb1bcdbf814eb7e514c8bd177210f19ede25aed`;
* 0132: `498b6c004c2e2f7ca9808dde7d9109384409c536236e810370ce7b17ef98002e`.

A lint annotation was briefly added after k3d had applied 0132. The next natural
rollout correctly rejected the checksum drift. The annotation was removed,
restoring the exact ledger bytes, and the intentional Squawk exception moved to
`.squawk.toml`. The checksummed 0131/0132 headers still describe the initial
manual repair procedure; the recovery registry and database migration runbook
are the status of record.

## Verification

* Class-A and affected completion coverage passed **295 tests**; the exact
  endpoint/writer group passed **49/49** after the final automatic-timestamp
  change.
* The final critic/recovery/head group passed **57 tests with 3 optional-DSN
  skips**. Separately, **four real PostgreSQL 15 recovery tests** proved a
  runner-created INVALID+dirty index with a post-0130 duplicate and no ledger
  surgery, healthy exact unledgered adoption, valid wrong-shape rebuild without
  false-green, and two-runner serialization. The earlier direct migration
  matrix passed **6/6** on scratch PostgreSQL.
* The app schema replay passed through **114 transactional and 18
  non-transactional migrations**; `schema_current.sql` is current at **13,008
  lines** and contains the exact predicate.
* Repository-wide Ruff check passed and Ruff format check reported **1,066
  files** formatted. Squawk 2.59 reported zero issues in the two lint-covered
  migration files; 0132 is intentionally config-excluded and its full DDL is
  asserted byte-for-byte. `git diff --check` passed.
* Repository-wide pytest completed with **15,498 passed / 145 skipped / 11
  failed in 812.27 s**. The eleven failures are exactly the standing
  environment baseline: one unavailable localhost PostgreSQL test, seven MCP
  transport/wiring tests, and three optional arXiv/provider tests.

The final natural k3d rollout was **1/1 Ready** and health returned 200 in
**0.970 ms**. The running source and all three migration hashes matched the
tree. Its ledger showed 0130/0131/0132 successful in **2/0/29 ms**, no dirty
rows, and `jobs_verification_uniq` unique/valid/ready/live with the exact keys
and predicate. A deployed-package import mistake in the new runner was caught
by the first live attempt; the package-relative import fixed it, and that
orphan-reclaimed fixture was deliberately discarded rather than counted.

The clean README smoke created one disposable General Worker job through
Cockpit and proved `execution_lane='pinned'`, zero datasources and zero
Scholar/Critic children. It reached processing in **16.469 s**; its workspace
and dedicated agent pod became Ready with zero restarts. The completion
decision was journaled, `/complete` returned 200 in **4.186 s**, and the job
reached `completed|pinned` with non-NULL `completed_at` **718.062 s** after
creation. The workspace released and the agent pod was reaped. Its assignment
remained until deletion, matching the existing pinned lifecycle and S18's
pause-only clear. Authenticated deletion removed the exact job, two snapshot
objects, isolated Gitea repository and offline fixture agent; final job,
binding, agent and Kubernetes residue were all zero.

The S2 sandbox tier gate remains closed. No stateless admission, worker queue
unit, command/effect schema or finalization protocol was added in this step.

# Session 11 — S3 driver: worker jobs on the stateless lane (2026-08-11)

## Scope and admission

This slice implements the worker driver under §5.4.5's 2026-08-10 scope
correction. A batch rotation is a queue transition and makes **zero** calls to
`POST /api/jobs/{id}/complete`; only a genuine terminal or human-facing stop
uses that endpoint at pinned frequency. The completion command/effect redesign
is not a hidden dependency of rotation.

Admission is independently chart-gated by
`agent.stateless.worker.enabled=false`, and enabling it also requires the
existing stateless executor pool. An explicit stateless root is accepted only
for an in-cluster Kubernetes pod workspace. A VM request remains pinned, an
omitted root remains pinned, and a child whose lane is omitted inherits its
authoritative parent's lane. The pinned dispatcher and its recovery sweeps
remain exact-`pinned` predicates.

No schema change was necessary. App migration head remains **0132** and no file
in the allocated **0133–0139** range exists. Gate 3 step 2 was not built: there
is no command table, polymorphic effect table, completion high-water mark or
finalizer.

## Core driver and queue protocol

The existing stateless executor is now a session-first shared local pool. It
always polls `session_turn` first and polls `worker_batch` only when no session
claim is available and the worker gate is on. Worker claim is a queue-first
transaction: it takes the `run_queue` lease, locks the jobs row, and CASes
`created`, `paused`, or the expected reclaim state `processing` to
`processing`. `assigned_agent_id` remains NULL. A pinned, assigned, terminal or
otherwise ineligible jobs row is consumed to queue `done` instead of being
release-looped into a poison head.

The claim endpoint reuses the pinned dispatch builder for the canonical
`JobStartRequest`, including its existing config and credential resolution. It
validates `(unit_id, lease_token)` before assembly and again after the slow
assembly so a stolen claimant cannot receive the bundle. Worker state uses a
process-wide psycopg checkpoint pool and an exact-token fenced saver. Every
checkpoint put/write proves the current worker generation; the saver never
uses pipeline mode. The persisted wire stack is pinned exactly at
`langgraph==1.2.10`, `langgraph-checkpoint==4.1.1`,
`langgraph-checkpoint-postgres==3.1.1` and `psycopg-pool==3.3.1`, with
`LANGGRAPH_STRICT_MSGPACK=true` required at startup.

The driver arms the existing wall-clock-first `batch_boundary` envelope for
each claim. Production retains its 300-second floor; the explicit Tilt overlay
uses 60 seconds so a handoff is observable. At the boundary the graph reaches
END, bounded teardown drains memory and closes the checkpointer, and the S2
remote-shell substrate leaves ownership-fenced tmux state available to the
successor. A resume hydrates TodoManager from graph state on **every**
checkpoint path, not only one END branch. It also restores the safe durable
instruction-read receipts introduced after the first live handoff exposed that
those enforcement stamps had otherwise remained pod-local. A configured
instruction receipt carries its phase/turn and content hash across repeated
handoffs and FIFO churn, and fails closed if the instruction bytes change;
ordinary read-before-write authorization is intentionally not restored. The
resumed audited-tools node also restores the checkpointed phase before applying
phase-scoped enforcement.

Rotation uses the prescribed **complete-and-requeue** composition. While the
old generation still holds and locks the row, it advances the synthetic input
watermark and then completes the old consumed watermark. The completion CASE
atomically leaves the row queued and runnable now, resets
`attempts_since_completion`, and preserves `last_leased_by` affinity. Ordinary
release was rejected for successful rotation because it preserves the failure
counter, clears affinity and adds attempt-scaled backoff. The stable log
contract, asserted by the runtime test and observed in the live run, is:

```text
worker_batch rotate: unit=<id> token=<n> queue_verb=complete_and_requeue queue_state=queued input_seq=<old> next_input_seq=<new> complete_calls=0 http_complete_calls=0
```

Recoverable infrastructure stops use queue release/backoff and never report.
A terminal or human-facing stop reports while the heartbeat still owns the
lease; after a 2xx it completes the exact queue watermark to `done`, while a
failed/ambiguous report releases for a successor to re-report from the END
checkpoint. `JobCompleteRequest` now carries an optional lease token. Before
the handler's status guard or any side effect, the stateless arm requires exact
equality with the worker row's current token, regardless of queue state. A
missing or stale token returns 409; pinned reports remain tokenless and follow
their historical path.

## Verbs, steering and lifecycle repair

Resume, approve and feedback now select by lane. Their stateless arm acquires
queue before jobs, stamps a durable resume/delivery generation, resets a parked
attempt budget when appropriate, and re-enqueues the worker instead of leaving
it paused for a dispatcher which excludes it. Queued guidance/replies carry
stable delivery keys reconstructed from the checkpoint. The live owner writes
their acknowledgement only from a post-checkpoint hook, after the absorbing
fenced checkpoint is durable.

Cancel hard-closes the worker queue first; a leased holder observes the jobs
status through the renewal companion read and stops without relying on a
registered-agent heartbeat. Cancellation retains a durable cleanup marker
until strict checkpoint and workspace cleanup complete under one per-job
PostgreSQL advisory single-flight. Permanent DELETE first hard-closes and, if
needed, bumps the leased worker generation, marks the jobs row non-runnable,
strictly prunes and proves the checkpoint thread empty, then performs external
cleanup before atomically removing the queue and job anchors. The final lock
order is queue row, Docker advisory lock where applicable, then jobs row.
Stale stateless verification critics use the same queue-first cancellation and
strict checkpoint cleanup; a marker-bearing cancelled critic remains live to
parent-review convergence until that cleanup finishes.

This lifecycle work was prompted by the first full live core run: its normal
DELETE removed the job and external resources but initially left **49
checkpoints / 85 checkpoint blobs / 247 checkpoint writes** plus the queue
tombstone. Retention later compacted that residue, which did not make deletion
correct. At the final exact pre-cleanup check, **3 checkpoints / 25 blobs / 8
writes** remained. The new strict production helper removed those **36** rows,
the exact tombstone was removed, and jobs, queue, checkpoints, blobs and writes
all proved zero.

## Verification

* A disposable PostgreSQL 16 acceptance suite passed **10/10**. It includes
  **32 simultaneous claim races**, **25 consecutive rotations**, saver fencing,
  saver-before-DELETE and DELETE-before-saver interleavings, strict pruning and
  a stale live verification-critic hard fence.
* The focused affected matrix passed **484/484**. The broader worker/lifecycle
  matrix passed **702 tests with 2 skips**.
* Repository-wide pytest completed in **818.99 s** with **15,659 passed / 155
  skipped / 11 failed / 51 warnings**. The eleven failures are exactly the
  standing environment baseline: one unavailable localhost PostgreSQL test,
  seven MCP transport/wiring tests and three optional arXiv/provider tests.
* Repository-wide Ruff check and Ruff format check passed for **1,078 files**.
  Both Helm lint value sets passed. The app schema replay and head check passed
  through frozen migration 0132, with no 0133–0139 migration to lint or
  snapshot. Dependency checking proved the four exact checkpoint pins above,
  and the strict-msgpack checkpoint round trip passed.

## Live evidence

### Earlier full core run, before the final cleanup repair

Fixture `b7cce761-…` supplied the full feature-path fault proof. Pod A held token
**12** and was force-deleted mid-batch; after reaper recovery pod B reclaimed at
token **14**. The successor restored Todo state, instruction-read receipts,
tmux panes, shell cwd/environment and the workspace marker from the checkpoint
plus S2 substrate. A deliberately late token-12 completion POST returned
**409**, and a before/after read proved pod B's authoritative row unchanged.
Rotations produced the zero-call log contract above. The genuine final stop
made exactly **one** successful `/complete` request (**200**) and reached the
expected `pending_review` state. This run predates the strict DELETE/cancel
repair described above; it is the run that exposed the orphaned checkpoint
rows, not evidence that the old cleanup path was acceptable.

### Current final-byte rotation, resume and deletion

Fixture `264228ba-…` cleanly rotated from pod A token **2** to queued and was
claimed by pod B at token **3** in about **2.3 seconds**. Rotation reset the
attempt counter; the successor claim then recorded attempt **1**. It restored
**4 Todo entries**, **2 tmux tabs**, cwd/environment and the remote marker.
The pre-terminal `/complete` count was exactly **0**.

The retained, verbatim pod-A line is:

```text
2026-08-11 01:36:25 - src.api.turn_executor - INFO - worker_batch rotate: unit=264228ba-9705-4324-8c36-7d0bf5106b2d token=***REDACTED*** queue_verb=complete_and_requeue queue_state=queued input_seq=1 next_input_seq=2 complete_calls=0 http_complete_calls=0
```

The logger deliberately redacts the token. The correlated committed-row watcher
recorded token **2**, `state=queued`, `attempts=0`, `input=2`, `consumed=1` at
`01:35:43.740055Z`; the successor then claimed token 3. All **8** retained
rotations carried `complete_calls=0 http_complete_calls=0`, and the
orchestrator access log contained zero completion requests before the first
controlled terminal transition.

Because the model continued looping, terminal placement was intentionally
controlled through the normal exact-token endpoint; it was **not** a natural
model-selected terminal. The token-**8** request reached `pending_review`, and
the queue closed at token **9**. A public `POST /resume` returned **200**; pod B
claimed token **10** within about **3 seconds**, with prior status `paused`,
attempt **1**, input/consumed watermarks **9/8**, and the same canonical
checkpoint, four Todos and two tmux tabs.

A second controlled exact-token callback placed the resumed job back in
`pending_review`; the DELETE was deliberately issued while token 10 was still
leased so it exercised the saver/delete race rather than waiting for ordinary
driver teardown. At **01:45:56**, the checkpoint tables held **45 checkpoints
/ 78 blobs / 230 writes**. DELETE returned **200 in 0.183 seconds**. The
immediate proof found zero jobs, queue rows, checkpoints, blobs and writes; all
six vector-table fixture counts were zero; both snapshot objects and the
isolated Gitea repository were gone. The late saver logged lease loss and
recreated nothing. The worker pod was gone by the response, and its Service/PVC
counts reached zero about **39 seconds** later. This is the current-byte proof
of queue fencing, strict pruning, public resume and deletion during a live
successor lease; it is not presented as a natural terminal-callback count.

The final-byte queued-cancel fixture `0ddcc619-…` began
`created/worker_batch/queued`, token 0, with no checkpoint. Public cancel
returned **200 in 3.686 seconds** and left the job `cancelled`, the queue `done`,
no cleanup marker and no checkpoint or Kubernetes residue. Its subsequent
authenticated DELETE returned **200 in 0.133 seconds** and removed both job and
queue rows.

The omitted-lane README General Worker fixture `3cde7045-…` proved pinned
parity on the same final bytes: the database lane was `pinned`, the legacy
dispatcher assigned dedicated pod `srw-agent-j-8ab0fd07`, and the natural agent
callback made exactly one completion POST (**200 in 37 ms**) into the configured
`pending_review` state. Public approval returned **200 in 1.057 seconds**, moved
the job to `completed`, and stamped
`completed_at=2026-08-11 02:03:32.688762+00`. Public DELETE returned **200 in
194 ms** and left job, queue, Kubernetes and vector residue at zero.

That pinned DELETE exposed a pre-existing pinned-lifecycle debt: after the job
row was gone, its canonical checkpoint thread still contained **12 checkpoints
/ 33 blobs / 48 writes**. The S3 change deliberately did not alter the pinned
path. The deployed strict helper removed exactly those **93** disposable-fixture
rows and the final five-table proof was zero; no other row was touched. A
general pinned permanent-delete checkpoint cleanup remains outside this S3
slice.

A disposable pinned canary also archived its agent log after its job had
already been deleted, leaving one exact late object. Its provenance was proved,
the deployed blob helper removed only that key (**HEAD 200 -> 404**), and no S3
worker code was broadened to address the pre-existing pinned archive race.

Final cleanup across all seven disposable fixture ids proved job, queue and all
three checkpoint-table counts zero; all six vector-table counts zero; exact
snapshot/log objects and Gitea repositories absent; Kubernetes pod, Service and
PVC counts zero; and no agent current-job references. Both disposable dedicated
pinned-agent rows were offline and unbound, then removed through the normal
authenticated agent DELETE endpoint. Only their expected unowned metering
tombstones remain.

The production two-Deployment/KEDA split, per-claim job log, Job Bench A/B
performance gate and Gate 3 multi-effect finalizer remain unbuilt.

# Session 12 — S2 close-out: sandbox admission and durable lifecycle (2026-08-11/12)

Branch: `feature/stateless-agents`; not pushed. `develop` remained untouched.
This session completed the S2 workspace lane, removed the sandbox admission
gate, ran repository and live acceptance, and repaired every P0/P1 found by the
final adversarial/live passes. Section 5.4.5 of the feature design was not
changed.

## Scope, admission and migration

Stateless session admission now accepts exact `virtual`/`none` and an in-cluster
Kubernetes `sandbox`. It still refuses VM, Docker-backed physical,
protected-cloud, officer/conference, unknown and malformed classes. The same
combined class/workspace decision is repeated at input, control, interrupt,
upload, IDE/browser/Canvas and final claim-bundle credential boundaries under
the fresh thread/lifecycle lock. All four stop-marker keys fail closed by key
presence; load-bearing booleans require exact JSON booleans.

Migration **0133** added the durable session-task tables, memory-extraction
cursor and path-scoped citation anchors. A canonical disposable replay applied
all **133** app migrations (115 transactional and 18 non-transactional) and
matched the committed **13,125-line** schema snapshot byte for byte. Migration
0133's recorded checksum is
`7f088fc7eb7a17290f8947fff681839177bdec93880c669e0035e0e2573c9735`.
The changed-SQL group passed **141 tests** and migration ordering/head passed
**3**; Squawk reported zero issues. App-schema head is 0133 and 0134–0139 are
unused.

## Durable state and path dispositions

Tasks are PostgreSQL-owned and hydrate on every attach. Memory extraction uses
the migration-0133 durable interval cursor rather than a process-local last
turn. Citation delivery uses a per-path durable anchor. Sandbox undo is one
durable control request plus one linked receipt, backed by the remote Git/turn
ledger; virtual and `none` sessions reject undo as unsupported. Claim-local
read-before-write stamps and sync caches are deliberately disposable, forcing a
fresh authoritative read after handoff. WebDAV and research downloads stage in
operation-scoped temporary storage and write through the workspace backend.

RAM teardown now joins the loop/watchers, title/stage work, CitationEngine and
MemoryManager admission/drain before any queue transition or claim-loss ACK.
Routine stateless detach does not run a duplicate `session_end` extraction.
The remaining semantic gap is public End's exact final memory tail: a crash can
leave **0–4 turns** after the five-turn interval uncaptured. Closing it requires
a same-transaction durable terminal effect/outbox; a cleanup-side retry cannot
provide authority or crash idempotence.

## Workspace incarnation and retirement authority

One durable `{generation, mode, attempted, replaces_uid}` marker owns each
Kubernetes creation. Only the exact false-to-true `attempted` CAS winner may
issue Pod create. A continuation validates full owner labels, opposite-owner
absence, generation, immutable Pod UID, workspace storage binding, seed
ConfigMap, PVC and Service identities on every read through Ready publication.
Exact-terminal recreation persists replacement authority before a UID-
preconditioned delete and waits for the old name to reach 404 before rotating.
404, replacement and API ambiguity are never process-zero proof.

Remote shell, tmux, IDE-terminal, browser, rclone and overlay processes carry
the exact workspace/runtime tag. Terminal cleanup closes the SFTP subsystem,
retires every resident/controller, scans all same-UID processes fail closed,
and writes resident/shell acknowledgements bound to terminal token, workspace
and endpoint generations, runtime UID and host-key fingerprint. Soft End
requires a strict checksum-manifest snapshot for emptyDir and leaves a settled
resume tombstone; permanent End converges external and DB resources before row
deletion. Legacy suspension and generic workspace/VM lifecycle reapers refuse
stateless rows.

The remote markers/flocks remain workload-user writable. This is accepted for
sandbox v1 as a cooperative correctness protocol, not a hostile same-user
security boundary. PostgreSQL/Kubernetes identity remains the admission and
persistence fence.

## Failure-driven repairs and milestone commits

The implementation landed as explicit milestones:

- `f3885df7` — route session paths through workspace backends;
- `291c56a1` — bind shells to workspace incarnations;
- `0fc23b5a` — open sandbox session admission;
- `9d2ee44d` — preserve the already-applied S2 migration checksum;
- `eab430bd` and `8f025dc3` — preserve/frame remote undo and Git records;
- `df70ab6b` — consolidate the durable sandbox lifecycle;
- `d2dd1f68` — align the pinned inventory fixture with the new DB authority;
- `4594ea50` — run fenced resident/zero scripts under Bash;
- `f5c4a471` — strict rclone drain, SFTP close and retry-safe retirement;
- `d4ed907e` — retain the non-null parked retry deadline;
- `d34ada8f` — bind claim-loss JSON paths as canonical text;
- `5e60a644` — `exec` Python as PID 1 so SIGTERM reaches Uvicorn;
- `77d6909a` — materialize eviction provenance before Pod deletion;
- `e17418bd` — restore JSON-held deadlines through asyncpg text/timestamp casts.

The final four were live-fault discoveries. The first controlled steal proved
`run_after` was correctly retained, then exposed asyncpg rejecting an integer
JSON-path argument. A rollout exposed the Helm PID-1 shell swallowing SIGTERM.
The next controlled steal showed `jsonb_set(..., false)` returning a row without
creating `eviction_requested_at`. A rollback-only real-PostgreSQL run then
found ISO strings being bound directly as asyncpg `timestamptz` arguments.
Each repair was narrow and independently reviewed; malformed/null/wrong-token
authority still fails closed.

## Verification

Repository-wide pytest completed in **843.10 s** with **16,681 passed, 156
skipped, 11 failed and 53 warnings**. The eleven failures are the established
local environment baseline: one unavailable local PostgreSQL check, seven
MCP/Python-3.14 transport/wiring cases and three optional arXiv/provider cases.
Ruff check and format check covered **1,092 files**. Both Helm value sets
linted. Cockpit passed **119/119 files and 1,835/1,835 tests**, the production
build, and the **2,455-key** i18n check. The final claim-loss/End/TurnExecutor/
Helm delta passed **212** tests; the final timestamp/eviction group passed
**163**.

An independent high-risk integration union passed **599** tests; full
TurnExecutor/PersistentSession passed **176**, and PersistentApp passed **292**
with two nonblocking AsyncMock hygiene warnings. The exact frozen implementation
review found P0=0/P1=0 before live rollout; each later live repair received a
fresh bounded review. The final combined `d34ada8f`/`5e60a644`/`77d6909a`/
`e17418bd` verdict is GO with no remaining P0/P1.

## Live sandbox, virtual, rollout and pinned evidence

The headline sandbox fixture `d52bbbd7-…` crossed two executor pods while
preserving four tmux windows, cwd, environment, files and two durable tasks
(one completed, one pending). Turn B changed an exact file from A to B; one
idle undo request produced one receipt and restored the A bytes plus two
40-character Git SHAs. One hard interrupt produced one linked receipt and zero
forbidden AI answer.

A safe workspace replacement first terminated the exact observable UID, then
created a different UID while retaining the same PVC bytes and rejecting the
old tmux runtime. Soft End at token **13** and Resume created a third workspace
UID; the successor answered exactly once at token **14**. Permanent End at
token **15** returned two expected 503 responses while physical and auxiliary
retirement converged, then 200. Final proof: **28** thread-id relations zero,
threads/queue zero, K8s Pod/Service/PVC/ConfigMap zero, virtual and snapshot
objects zero, repository 404 and owner state 404.

Fresh virtual fixture `825b51bc-…` had no physical workspace, persisted one
15-byte upload, completed an exact token-1 turn, soft-ended/resumed, then
completed token 3 with the prior durable marker in context. Before deletion it
held four messages and 35 objects/56,688 bytes. Final permanent DELETE returned
200; all **31** checked application relations, the virtual prefix, repository
and K8s fixture resources were zero, and public state returned 404.

Natural termination fixture `77d388fa-…` claimed input sequence **2662** at
token 1 on pod `s8pph` (UID `7e5da300-…`). Python PID 1 logged shutdown
immediately, renewed its lease through bounded completion, and committed
exactly one AI marker **99 s** later. The queue ended `done` with
`input_seq=consumed_seq=2662`, attempts 0 and no loss/hold. Public permanent
delete left thread, queue, messages and snapshot prefix at zero. A synthetic
outer-rollback real-PostgreSQL proof then materialized the eviction leaf and
ran ACK end to end in **5.0 ms**: loss/hold cleared, parked became queued, token,
attempts, `run_after` and `queued_at` stayed exact, and rollback left 0 thread /
0 queue rows.

Pinned parity fixture `83202843-…` remained on the dedicated plane. One control
was applied in **53 ms**; replay of the same UUID returned the one stored row
with HWM 1. An ordinary response arrived in about 12 s and no `run_queue` row
was created. Public DELETE returned 200 in **12.819 s** and removed DB/K8s/
Gitea/Nextcloud authorities, but exposed pre-existing pinned cleanup debt: two
snapshot objects totaling **99,094,250 bytes** and one stale unbound agent row.
Those exact disposable artifacts (2 objects, 1 row) were removed under guarded
cleanup; the generic pinned path was not broadened.

## Accepted boundaries and remaining debt

- Public End exact final memory capture remains blocked on the generic durable
  effect/outbox (0–4-turn tail described above).
- A caller passing displayed tool-call `.id` rather than canonical UUID
  `.approval_id` receives 500/DataError; the canonical field returned 200. This
  is P2 validation debt, not an authority bypass.
- The pinned snapshot/agent-retention issue is pre-existing and remains a
  separate pinned-lifecycle cleanup item.
- Full two-editor Monaco/EventSource auth-expiry UX, server-side conditional
  object-store writes and generic Canvas invalidation remain open.
- S3 worker admission stays default off; the production two-Deployment/KEDA
  split, per-claim log, Job Bench and Gate 3 finalizer remain unbuilt.

## Guarded disposable-fixture cleanup

Five same-day acceptance/fault fixtures were removed under their exact
stateless-session lifecycle locks. The guarded pass deleted **24,119 database
rows** across 31 checked relations, including five queue rows, five thread rows
and 16 explicit non-FK turn commits. It also removed two Services, two PVCs and
two PVs (zero Pods), four snapshot objects/198,189,808 bytes, 35 virtual
objects/56,688 bytes, five Gitea repositories and five Nextcloud folders and
shares. Total object-store removal was **39 objects / 198,246,496 bytes**; the
operator operations took **41.303 s** in aggregate.

Three fixtures intentionally carried pre-fix parked claim-loss evidence. Each
was reasserted twice under the lifecycle lock with its exact queue, hold, loss
ledger and original `quiesced:false` metadata. Cleanup performed **zero
claimant-ACK writes** and did not update a marker, fabricate a UID or treat Pod
absence as quiescence. Final independent inventory found zero residue for all
five UUIDs in every checked DB relation, Kubernetes resource class,
object-store prefix, repository and cloud path. The unrelated 2026-08-07
baseline fixture `9a756800-…` was explicitly excluded and remained unchanged.

# Session 13 — origin/develop integration and final worker authority (2026-08-12)

Branch: `feature/stateless-agents`; not pushed. Local `develop` remained at
`30e154bc`; `origin/develop` was merged, not rebased. Merge `8a730c63` has
parents `fb1bfe87` and `c730409a` and contains all 113 develop commits which
had landed since the feature branch point. Two build-machine OOM interruptions
did not change the resolution: the first left resolved files unstaged, so the
written tree was audited and staged explicitly; the second resumed from the
committed final worker repair. Tilt was down for the merge and every edit, and
was started only for the live proof.

## Migration collision and ledger

Develop's `0115_datasource_tombstones.sql` remains byte-identical to its parent
(SHA-256 `75760ae595ac33394b4328e2c899ef84b5cd3c20489f055cef2caf996053fcf3`).
The feature migration was renamed to `0115a_run_queue.sql` with a 100% Git
rename and zero content edits; it and the old feature-parent blob both hash to
`e83475b3b4e77fb200bc26030f805f2ae2ff3bf0f0f72122b1db6a15ec936a93`.
Its stale filename header, and stale dependency comments in already-applied
0116–0132 files, were deliberately preserved to keep their checksums exact.

Production discovery reports **134 files / 134 unique prefixes** in the exact
order `0114_compute_interval_epoch_shape_repair.sql` →
`0115_datasource_tombstones.sql` → `0115a_run_queue.sql` →
`0116_events_seq_hwm.sql`; 0116–0133 remain byte-identical to the feature
parent. The feature parent already contained
`0133_thread_session_durable_state.sql`, so the merged metering tripwire keeps
the actual lexicographic head at **0133** while adding develop's tombstone
entry. The integration brief's literal 0132 sentence was stale relative to
its own source branch; reverting the test to 0132 would discard S2 and fail the
real head assertion.

With Tilt and the orchestrator down, a temporary PostgreSQL-only pod mounted
the retained app PVC and ran the one prescribed statement. `psql` returned
exactly **`UPDATE 1`**; the old ledger filename then counted zero, the new name
counted one, and its stored checksum still matched the byte-identical file.
The helper pod was deleted before Tilt started. The first merged orchestrator
startup logged exactly:

```text
applying 1 transactional migration(s) in app
→ 0115_datasource_tombstones.sql
✓ 0115_datasource_tombstones.sql (37 ms)
```

No duplicate-prefix, applied-but-missing, or checksum-changed error appeared.
The live ledger contains successful `0115a_run_queue.sql` and
`0115_datasource_tombstones.sql` rows with the two checksums above.

## Semantic conflict union

- `orchestrator/main.py` and `orchestrator/database/postgres.py` retain the feature lane
  partition, token-aware verbs, Class A writes and exact lifecycle gates while
  incorporating develop's datasource tombstones and owner-scoped config-drift
  resume flow. `register_agent` was not resolved by selecting either side; its
  existing behavior remains. The merge held 89 `execution_lane` occurrences
  in Postgres and 130 in main before the worker-attestation follow-up.
- `persistent_session.py`, `persistent_app.py` and `persistent_graph.py` keep
  S2 setup/teardown, shell-owner propagation, awaited settlement and transcript
  ordering together with develop's configured-but-unbound warning and durable
  pending/expired permission outcomes. Follow-up `f91a7f36` made permission-row
  retirement atomic with the exact stateless lease or reciprocal pinned
  binding, preventing an old claimant from expiring a successor's gate.
- `workspace_suspension.py`, `snapshot_service.py` and `ssh_helpers.py` formed
  a semantic overlap but not a design conflict. Develop's reclaim-on-idle,
  scoped-HOME restore, stage/verify/history/canonical promotion and honest
  pipeline status coexist with S2's strict runtime UID/host-key authority,
  joined cancellation-safe blocking effects, capture validation and
  shell-ownership arguments. The HOME command keeps both Bash `pipefail` and
  xattr/ACL flags. Generic suspension and lifecycle reapers continue to refuse
  stateless rows by design; stateless handoff remains the queue/UID retirement
  protocol, while snapshot suspension remains the legacy pinned path.
- Helm is a field-wise union of the stateless lane and develop's ESO,
  infrastructure-metering and reclaim-on-idle settings. Both stateless defaults
  remain false in `helm/values.yaml`; only the Tilt overlay enables them. The
  strict-msgpack environment and checkpoint dependency pins remain intact.
- Cockpit keeps same-thread replay/composer continuity, develop's expired
  permission outcome and attachment-send stages, both i18n sets and the
  stateless stale-interrupt/upload protections. The endpoint inventory and
  backlog are mechanical unions. The entire
  `docs/features/stateless_agents.md`, not only §5.4.5, is byte-identical to
  the feature parent (SHA-256
  `8fcf7b4ab5458022b4aa7875dd9a563d7f6bf5a944f7ddcab5953cd76d57eb9d`).

The merged upload DELETE route initially exposed a non-textual authority gap:
develop's generic Paramiko delete could race a stateless workspace handoff.
`9f484a03` and `f91a7f36` added the lifecycle lock and fresh-row/stop-marker
checks, exact Pod UID plus pinned-host-key AsyncSSH deletion, joined virtual
effects, and correct AsyncSSH-v4 type/no-such-path handling. Pinned behavior is
unchanged.

## Live-discovered worker authority repair

The first merged worker claim revealed that S3 workers still supplied the old
token/generation-only shell protocol, while S2 correctly requires process-tagged
workspace incarnation authority and returned exit 81. Provisional commit
`15588fdb` relaxed that requirement; review found it unsafe and immediate
revert `ab2d7a0f` restored the mandatory fence before another trusted proof.

Final repair `58784cd3` instead makes every worker claim attest its actual
workspace owner twice around slow bundle construction, then recheck the exact
lease. The in-memory bundle carries the freshly attested endpoint, PVC/Pod
backing UID, current Pod UID and SSH host-key fingerprint. Root workers use
`v1:job:<job-id>:<pod-uid>` workspace process authority. An inherited child
attests the parent workspace while retaining its own job/tmux identity, and
terminal cleanup targets only that child's exact shell-generation tag rather
than killing parent or sibling residents. Protocol-v1/v2 active and retired
markers still fail with exit 81 without mutation. Terminal cleanup remains
best effort under the existing one-phase worker completion contract: after an
accepted terminal response there is no durable cleanup-only claim state, so
pretending a raised cleanup error were retryable would wedge the row rather
than add authority. Two independent frozen-diff reviews found no P0/P1 blocker;
the focused repair matrices passed **586** and **166** tests.

## Verification

The final Python tree at `58784cd3` completed in **835.45 s** with **17,060
passed, 163 skipped, 17 failed and 57 warnings**. All 17 failures are reproduced
environment baselines: one unavailable local PostgreSQL connection, seven
MCP/Python-3.14 transport and wiring cases, six VM-chart contract cases
reproduced on a clean `origin/develop` worktree, and three optional
arXiv/provider cases. The earlier merged-tree run had the same 17 failures and
**17,026 passed**; the increase is the final authority coverage.

Repository-wide Ruff check passed and format check covered **1,104 files**.
Both Helm value-set lints, Squawk, fresh app-schema replay from zero and a
second snapshot replay passed. Cockpit passed **127/127 files and 2,082/2,082
tests**, its production build, and the **2,508-key** i18n parity check. Git
whitespace checks passed. The final worker repair changed only Python and
Python tests, so the Helm/schema/Cockpit gates were not rerun after that commit;
the full Python and Ruff gates were.

## k3d proof

All trusted runs first proved the final source markers inside every executor
pod and the orchestrator; both stateless executors and the orchestrator had
zero restarts.

1. Pinned README smoke: session `98af3d16-…` remained pinned and produced an
   AI response after creating the exact workspace file. Pinned job
   `4d002a4a-…` reached `pending_review`, approval reached `completed` with a
   non-null `completed_at`, and public deletion left job, queue and workspace
   resources at zero.
2. Stateless sandbox session `776e585d-…` was force-handed from one executor
   pod to a different pod. Its file, tmux environment and cwd survived exactly;
   the queue ended `done` with `input_seq=consumed_seq=2783`. Public permanent
   deletion converged to zero thread, queue and workspace resources.
3. Suspension/resume reused the pinned session because generic suspension must
   refuse stateless rows. A strict **99,083,944-byte** snapshot preserved the
   proof file; resume changed workspace Pod UID from `7d1722…` to `44ceb…`,
   changed the bound agent, restored `active|ready`, and the next AI turn read
   the exact bytes. Cleanup converged.
4. Natural stateless worker fixture `04c9ee52-…` held token 1 through the final
   pending todo and a 560-second command. At 12:44:31Z it emitted exactly one:

   ```text
   worker_batch rotate: unit=04c9ee52-4b47-4b3b-8b4a-d4fb3455017a token=***REDACTED*** queue_verb=complete_and_requeue queue_state=queued input_seq=None next_input_seq=1 complete_calls=0 http_complete_calls=0
   ```

   Token 2 restored the result, completed the todo and naturally reported the
   dependent phase boundary at 12:44:50Z. Exactly one terminal completion POST
   returned 200; the stable terminal line recorded
   `queue_state=done complete_calls=1 http_complete_calls=1`, and the database
   read `pending_review|done|token 2|input 1|consumed 1`. Approval correctly
   resumed this phase-boundary freeze rather than treating it as a final-job
   freeze; the disposable continuation was cancelled through the public API,
   then public DELETE returned 200. Job, queue, all three checkpoint tables,
   Pod, Service, PVC and ConfigMap inventoried zero.

After the proof, Tilt was stopped and the Helm release removed again.

## Remaining operator debt

Disposable pre-fix thread `e4bb35f8-…` remains ended with queue token 4 done,
input/consumed 2762/2754, one token-2 claimant-loss record with
`quiesced:false`, a permanent-retirement record with
`claimant_quiesced:false`, and its exact live workspace. Its old claimant Pod
was force-deleted before it could acknowledge quiescence; the reaper correctly
refuses to turn API/CRI absence into process-zero proof, so repeated public
DELETE returns 503. No claimant acknowledgement or metadata was fabricated.
Removing this disposable fixture requires the documented exceptional operator
procedure under the exact lifecycle lock with two unchanged-state assertions
and complete DB/Kubernetes/object/repository inventory; no checked-in helper
currently provides it. This is cleanup debt, not a weakened runtime fence.

Final branch proof before handoff is
`git rev-list --count HEAD..origin/develop` = **0**. The only intentionally
unverified operational item
is that guarded cleanup of `e4bb35f8-…`; the production two-Deployment/KEDA
split, Job Bench and Gate 3 finalizer remain the previously documented future
scope rather than integration regressions.

# Session 14 — Gate 3 steps 2+3, M1 schema (2026-08-12)

Branch: `develop`, direct and not pushed. The session opened at the brief commit
`f831ca29` with only untracked `HomeLab/`. During the untouched baseline run an
external rebase moved `develop` to the byte-equivalent brief commit `8230ac81`
on newer `origin/develop` `4a8da032`; the reflog records the rebase at
21:57:28+02. The three intervening differences are deployment image-tag/value
updates. No local Gate-3 edits were lost, Tilt was down, and work continued on
the newer base. `HomeLab/` remained untouched and untracked.

## Required pre-edit baseline

The literal `./scripts/pytest-fast.sh` invocation unexpectedly inherited the
wrapper's default `-x` and stopped at the first failure. Before any edit, the
same wrapper was therefore rerun explicitly without fail-fast as
`./scripts/pytest-fast.sh tests/ -q --tb=short`: **17,066 passed, 163 skipped,
11 failed**. The exact baseline failures are:

1. `tests/tools/research/test_arxiv_client.py::test_installed_arxiv_package_exposes_client_results`
2. `tests/test_database_phase1.py::TestPostgresDB::test_connect_disconnect`
3. `tests/test_mcp_manager.py::test_connect_discover_call_close`
4. `tests/test_mcp_manager.py::test_unreachable_server_degrades_not_raises`
5. `tests/test_mcp_manager.py::test_remote_transport_discover_and_call[http-streamable-http-/mcp]`
6. `tests/test_mcp_manager.py::test_remote_transport_discover_and_call[sse-sse-/sse]`
7. `tests/test_mcp_manager.py::test_tool_error_returns_string_not_raise`
8. `tests/test_mcp_manager.py::test_reconnect_once_revives_tool`
9. `tests/tools/research/test_semantic_scholar_client.py::test_arxiv_health_checks_installed_client_contract`
10. `tests/tools/research/test_semantic_scholar_client.py::test_combined_probe_is_secret_free`
11. `tests/test_mcp_agent_wiring.py::test_full_job_path_slice`

These are the expected missing-arXiv-package, localhost-Postgres and
MCP/Python-3.14 environment failures. They are the comparison set for every
later full-suite gate and were not chased.

## M1 shipped

- Added transactional migration `0140_job_completion_commands.sql`, copied
  from §5.4.5 for `job_completion_commands` and `completion_effects`, including
  the load-bearing agent/operator fence XOR, the full bidirectional terminal
  shape, the six-state vocabulary, the same-file bounded-churn drain index and
  deliberately no effect-state index or effect foreign key.
- Added `jobs.completion_seq_hwm`, a logged named finalizer lease table whose
  exact `(leader_id, elected_at)` pair forms its renewal term, and the single
  decision-(6) `job_completion_sweep_exclusions` view. The view excludes only
  live completion leases and parked operator work; an expired or NULL-lease
  command remains available to finalizer-resume routing.
- Regenerated `orchestrator/database/schema_current.sql` from the complete
  migration chain and advanced the app-migration-head tripwire to 0140.
- Added real-Postgres coverage for every documented half-written fence and
  terminal state shape, valid terminal shapes, per-job client-report dedup,
  drain/effect index policy, view semantics and term-shaped lease renewal.

There is no deviation from §5.4.5. That section does not prescribe names or
exact DDL for the lease table/view; `completion_finalizer_leases` is named so
the election can remain reusable by named finalizer domains, while M3 uses one
`job_completion` row. Schema remains completely dead: no production code reads
or writes any new object.

Verification so far: the new real-Postgres module passes **22/22**; Squawk
v2.59.0 reports **0 findings**; a full from-zero snapshot replay applied 0140
on PostgreSQL 15 and changed only the app artifact; the idempotence replay
reported the artifacts current. The post-M1 non-fail-fast suite produced the
same exact 11-failure set with **17,088 passed and 163 skipped** (the +22 are
the new real-Postgres cases). M2 (durable accept and agent retry identity), M3
(inline finalizer/resume drain), M4 (flag/parity/k3d kill proof) and optional
M5 remain.

## M2 shipped

- Added the default-off durable accept wrapper around `/complete`. With
  `COMPLETION_COMMANDS_ENABLED=false`, the wrapper authenticates and calls the
  legacy body directly; focused tests prove it does not import or call the new
  command service and returns the exact legacy object.
- Added optional `agent_id` and `client_report_id` transport fields. Pinned
  reports are fenced against the exact assigned agent; stateless reports are
  fenced against the exact live worker lease. The canonical server-side digest
  covers the job identity and operation body while excluding all three
  transport/fence fields.
- Admission locks `run_queue` before `jobs`, allocates `report_seq` from the
  locked jobs-row HWM, inserts the immutable command as its first database
  write, advances the HWM, and terminalizes a stateless `worker_batch` under
  the accepted token in the same transaction. Exact retries authenticate
  against the immutable accepted fence, not current mutable assignment/queue
  state. The full pending/finalizing, done, parked, superseded,
  force-resolved, and divergent-payload response matrix is covered.
- Added the agent retry envelope: a random UUID and exact four-field completion
  payload are persisted in the graph checkpoint immediately before END,
  reused byte-for-byte by HTTP retries and successor END reports, and cleared
  on genuine resume. `report_completion` accepts both 200 and 202, including a
  bodyless 202. Pinned callers now send their registered agent identity, and
  `dual_app` reports before its idle heartbeat, matching the already-correct
  ordinary app ordering.
- B4 queue terminalization means a 20-second heartbeat or the driver's
  post-report renewal can observe `run_queue.state='done'` while finalization
  is still running. The worker driver now recognizes only an exact
  `(job_id, accepted_lease_token)` command joined to that done queue row as a
  benign accepted handoff; it stops heartbeating and skips the old second
  `complete_unit` instead of declaring a stolen lease. This lookup is itself
  behind the same default-off flag, so the closed-gate worker path never reads
  a command relation.

One intentional deviation from the literal §5.4.5 lock wording: accept takes
the queue row `FOR UPDATE`, not `FOR SHARE`. B4, folded later in the same
authority section, requires accept to update that row. Two concurrent reporters
holding `FOR SHARE` and then upgrading can deadlock; taking the required write
lock at the start preserves the binding queue-before-jobs order and removes the
upgrade. A second small rolling-compatibility detail is explicit: the stored
payload is the verbatim four-field operation payload received by the accept
service, not its transport envelope; the route removes fences before calling
the service, exactly matching the digest and checkpoint contract.

Verification at the M2 boundary: **46 new focused tests** (19 command-unit, 7
real-Postgres accept/race/rollback, 8 endpoint-wrapper, 7 newly added
client/envelope cases, 2 B4 driver cases, and 3 ordering/state cases) and the affected completion/agent matrix
passes **300/300**. The real-Postgres cases prove concurrent same-key first
wins/one 409, exact pinned and stateless fences, zero mutation on a stale token,
transaction-wide rollback on injected queue-close failure, HWM/fallback ID
semantics, and same-transaction queue closure. M3 (effect journal, command and
leader leases, inline/resume drain), M4 operational parity/kill proof, and M5
remain.

The post-M2 repository-wide non-fail-fast suite produced the exact baseline
failure set again: **17,134 passed, 163 skipped, 11 failed**. This is +46 from
the post-M1 count and contains no new failure. Repository-wide Ruff check and
format check (1,109 files) and the whitespace gate also pass.

## M3 shipped

- Added a River-style completion finalizer with two independent ownership
  fences: a logged, expiring, exact-term leader lease for the drain and a fresh
  per-claim execution UUID for each command. Pending and expired-finalizing
  commands are claimed in report order, live terms are never stolen, renew/
  settle/retry/park writes all CAS the exact owner, and retries use the existing
  `5s * attempts * (1 + U(0, 0.2))` dialect with a process-global retry bucket.
  Election, candidate reads, renewal loss and release tolerate transient
  database failures without leaving a silent zero-leader drain.
- Factored the existing `/complete` body behind a zero-cost optional effect
  runner. The closed-gate arm calls that body directly and does not import the
  finalizer or read/write any Gate-3 relation. The durable arm accepts and
  commits the command first, then runs the same S1--S37 order inline; lifespan
  starts the resume drain only when the flag is enabled. Successful early
  returns and deterministic legacy HTTP guards are stored and replayed exactly.
- Added stable effect intent/completion records, bounded replay detail,
  per-effect ambiguity deadlines strictly inside the command lease, group-local
  retry scheduling and independently retryable tail groups. Pure Postgres
  effects can share a task-bound transaction with their journal marker; child
  tasks cannot inherit its connection. The main status disposition is fenced
  by both its recorded entry status and the exact finalizer term, so a foreign
  concurrent transition is not adopted as this command's write.
- Closed the inventory's external action/marker windows with command-keyed
  reconciliation where a database transaction cannot span the action: loop
  WebDAV delivery records a fixed-cardinality context intent; subjob graft and
  terminal merge use exact paginated Gitea commit/PR markers and probes;
  verification critic creation reconciles the 0132 natural key; curation handoff
  carries the command key. Ambiguous external responses remain pending for a
  probe instead of being journaled as success or failure.
- Made Kubernetes terminal cleanup replay-safe. Its intent captures exact Pod,
  PVC and Service UIDs plus fresh SSH endpoint/host-key identity. A strict,
  deterministic `history/completion-<command UUID>` snapshot is deeply verified
  or repaired before UID-preconditioned deletion; a same-name replacement
  aborts the entire old-resource teardown. This effect receives an 890-second
  ambiguity window inside a 900-second command lease, then atomically shrinks
  the command back to the ordinary 120-second term after completion.
- Preserved the accept-time B4 handoff: once the immutable worker token has been
  accepted and its queue row closed, finalization trusts the command fence and
  never revalidates the mutable worker lease. Resume after an S17 crash uses the
  command's own recorded disposition and continues the remaining tail rather
  than tripping the legacy late-callback guard.

The deliberately deferred boundaries are important. Status-write reordering,
decision-(6) resume/claim/sweep routing, Class-B/Class-A atomicity, background
stateless response timing and winner/supersession policy remain steps 4, 5 and
the optional M5; none is activated here. VM-specific recovery and VM/Docker
terminal teardown remain the exact legacy best-effort path because the brief
puts anything VM out of scope; they create no durable teardown effect. The
Kubernetes durable S24 snapshot awaits capture rather than merely journaling an
`asyncio.create_task` schedule marker, while the flag-off path retains the
historical detached timing. These are the only intentional coverage/timing
differences from the generic §5.4.5 model, chosen to respect the brief's scope
and to avoid falsely marking an external effect complete.

Verification at the M3 boundary: **121 net new collected cases** beyond M2
(the remaining four of the full-suite delta are unstaged M4 Helm cases), plus
the independently selected affected matrix passed **930/930**. The focused
coverage includes leader loss/takeover, dual command claims, deadline/cap
parking, jitter and token-bucket behavior, exact-term stale-writer rejection,
effect intent-before-action/replay, group independence, transactional rollback,
long-lease shrink, accept-to-cancellation drain resume, S17/tail crash replay,
large freeze payloads, command-keyed external probes and UID-safe teardown.
Real-Postgres tests exercise command/effect concurrency and exact-term lease
takeover. The repository-wide non-fail-fast suite produced the exact baseline
set again: **17,259 passed, 163 skipped, 11 failed**, with no new failure.
Repository-wide Ruff check and format check (1,116 files) and the whitespace
gate pass. M4 operational parity/kill proof and optional M5 remain.

## M4 shipped — default-off flag and k3d parity/recovery proof

The whole command/finalizer path is wired through the single
`COMPLETION_COMMANDS_ENABLED` gate. The chart default is `false`; the shared
ConfigMap carries it to the stateless execution plane and the orchestrator has
an explicit `configMapKeyRef`. The ignored local overlay is the only place it
was opened for this soak. `COMPLETION_FINALIZER_INLINE_DELAY_SECONDS` is a
second, default-zero local fault-injection setting: a positive value pauses
inside the already-claimed inline workflow, after accept and before S1. It was
15 seconds only for the pod-kill captures and is back to **0** in the final
running deployment. The tracked chart remains `false`/`0`.

Eleven focused regression cases were added for the flag/rendering, claimed
fault window, strict archive runtime contract, lifecycle ownership veto, and
UID-bounded deletion. The final affected M4 matrix passed **429/429**. The
post-fix non-fail-fast repository suite passed **17,269**, skipped **163**, and
failed only the exact 11 baseline environment cases. Repository-wide Ruff,
touched-file format, both required Helm lints, Squawk v2.59.0 (0 findings),
whitespace, and the full three-schema snapshot/idempotence replay all pass.

### k3d parity table

These are individual observed samples, not a p95 claim.

| Proof | Flag OFF control | Flag ON normal | Flag ON pod-kill recovery |
|---|---|---|---|
| Pinned completion | HTTP 200 in 0.048 s, `pending_review`, then approve in 0.374 s -> `completed` | HTTP 200 in 0.285 s, same response keys/types/actions and status path; approve in 0.454 s -> `completed` | Accepted/claimed command was `finalizing`, attempt 1, live owner/lease, **0 effects**, while the job was still `processing`; serving orchestrator Pod was force-deleted |
| Durable rows | commands=0, effects=0, leader leases=0, job HWM=0 | command `9193b633-…` done on attempt 1; 16 unique effects, all done on attempt 1 | command `577831df-…` done on attempt 2 after natural lease expiry (~141 s from claim); all 16 effects done on attempt 1 |
| Domain parity | `completed_at` set only on approval, 0 critic children, notification/dispatch and cleanup observed | identical `completed_at`/critic count and response actions; expected wake/dispatch effects done; workspace cleanup converged | exact `(effect_name,effect_group)` set equals the normal ON command; stored response replayed with `Idempotent-Replayed`; approval completed normally |

The OFF fixture was `6eb4fe1d-…`; the ON comparison fixture was
`ad2a0f63-…`; the required accept-before-effect kill fixture was
`2f0f7bab-…`. Runtime bytes, ConfigMap and Pod environment were checked before
trusting each phase. The OFF phase happened on a virgin migrated cluster, so
the zero-row claim is a total count rather than a delta.

A stronger, deliberately later crash was also exercised against job
`7582ab02-…`, command `66b7383f-…`: the process died after 15 effects existed
and S36 had begun. The old 900-second effect term expired naturally; the new
leader reclaimed it, finished the same command on command attempt 2, and left
16 unique done effects. Fifteen remained at attempt 1 and only
`workspace_archive_teardown` reached attempt 2. Its command-keyed object-store
prefix contains exactly one archive/manifest pair; compressed SHA, zstd/tar,
strict manifest identity, canonical-manifest repair and the exact proof marker
all verified. Pod, PVC and Service are absent.

### Soak findings folded into M4

The first terminal soak exposed three real integration holes; none was hidden
or repaired by editing durable rows:

1. The orchestrator runtime image lacked the `zstd` binary that strict terminal
   validation invokes, and the first validator command discarded stderr. Both
   production/dev runtime images now install `zstd`, validation preserves its
   diagnostic, and image-contract tests cover exit 127. This is an honest
   runtime dependency repair (including a new image capability with the gate
   off), while the closed completion-command path itself remains relation- and
   behavior-dark.
2. The generic lifecycle reaper observed the early legacy-compatible
   `completed` status and ran an ordinary snapshot/delete while S36 was still
   pending. A conservative, flag-gated ownership veto now preserves job
   workspaces whenever a command is pending/finalizing/parked, rechecking at
   snapshot, delete, give-up and orphan-PVC action time and failing closed on a
   lookup error. Flag off returns before any new-relation read and preserves the
   exact legacy metadata shape. This is only an S36 ownership exclusion; it
   does **not** activate step-4 status reordering, routed sweeps, redispatch, or
   worker admission.
3. Kubernetes honored the workspace Pod's ordinary 120-second termination
   grace when the requested 10-second value appeared only as a query argument.
   Captured S36 now carries the same 10-second grace in the UID-preconditioned
   `DeleteOptions` body and bounds the exact-absence proof at 45 seconds;
   legacy/default-off callers keep their old request body. On the rebuilt
   image, fresh pinned terminal job `11036e16-…` returned the complete legacy
   HTTP 200 body in **12.476 seconds**, below the agent's 60-second timeout.
   Its command and all 16 effects settled on attempt 1, the strict snapshot and
   exact marker deep-verified, and Pod/PVC/Service reached zero.

The two pre-fix failures remain as operator evidence because there is no safe
unpark/force-resolve verb in tonight's scope and raw SQL state fabrication
would invalidate the proof. Command `c6735d9d-…` is parked behind the missing
`zstd` plus lifecycle race; command `b9ab9aa6-…` is parked behind the old
30-second/120-second deletion mismatch. Each retains one pending S36 effect.
The latter job also has successor command `a010c9f0-…` pending at report_seq 2,
correctly blocked by predecessor ordering. Final new-table residue is therefore
**2 parked, 1 pending, 0 finalizing commands; 2 pending effects; 1 live leader
lease**. All post-fix normal and killed fixtures have zero unfinished effects.

The requested stateless-session check ultimately passed on a fresh disposable
virtual thread (`db95072f-…`) with an explicit reachable `gpt-5-mini` model:
one human input produced exactly one `COMPLETION-PARITY-OK` AI row in 48 seconds,
then the queue settled `done` at token 1 with `input_seq=consumed_seq=2798` and
zero new or thread-attributable job-completion commands. The original
long-lived probe's configured
`gemma-4-moe` endpoint repeatedly timed out; its first try produced a contained
`Connection error`, the retry was interrupted through the correlated public
verb, and its queue also settled cleanly. That provider outage was not treated
as completion-path evidence.

There is no M5 change. Decision-(6) routing/status activation, worker
admission, and winner/supersession remain deliberately unstarted.

### Morning hand-check

Inspect the force-deleted mid-S36 fixture (`7582ab02-…` / command
`66b7383f-…`) end to end: command attempt 2 and S36 attempt 2, exactly one
command-keyed strict archive/manifest pair with the marker intact, and the
captured Pod/PVC/Service UIDs absent. This is the highest-risk claim that a
row-count-only review could accidentally overstate.

# Session 15 — Gate 3 step 4, M1 routed rescuers (2026-08-13)

Starting point was `develop` at `26327151`, with only the explicitly excluded
untracked `HomeLab/`. Tilt and k3d were already up. The required non-fail-fast
baseline produced **17,269 passed, 163 skipped, 11 failed**: one missing
`arxiv` package case, one unavailable local-Postgres case, six MCP transport
environment cases, two Semantic Scholar health cases, and one MCP agent-wiring
case. Before M1 the preserved Gate-3 residue was exactly **2 parked + 1 pending
commands and 2 pending effects**.

## M1 shipped — route, do not redispatch

- Extended the 0140 seed view into one report-order routing authority. It
  exposes the oldest unfinished command for each job and classifies parked as
  `alert_only`, a live finalizer term as `stand_down`, non-live deadline/cap
  exhaustion as `park_alert`, and every other pending/expired term as
  `resume_finalizer`. A live exact finalizer term wins even on its last allowed
  attempt; retry-cap routing begins only after that lease expires.
- Added a durable rescue-action ledger. A job-row-locked monotonic HWM allocates
  the required `(job_id, attempt)` key; `(command_id, command_attempt)` is also
  unique, so concurrent orphan, lease, registration, and pause rescuers share
  one action. Exact action owners have renewable visibility leases, stale
  owners cannot finish, and a router crash is reclaimable without allocating a
  second action.
- Added a flag-gated `CompletionSweepRouter`. `resume_finalizer` and
  `park_alert` call the existing exact-ID finalizer with `inline=False`; they
  never queue or dispatch an agent. Parked rows only raise a deduplicated
  officer incident. If the command crosses its deadline between classification
  and finalizer claim, that same action is promoted to `park_alert`, preserving
  both the unique key and the required alert. Fresh pending rows respect their
  existing `run_after` grace.
- Every class-1 legacy mutation now consumes the shared view when
  `COMPLETION_COMMANDS_ENABLED` is on: all four orphan-recovery arms, expired
  jobs-row leases, same-host agent replacement, LLM/infra list-and-claim paths,
  the LLM ceiling fail arm, and VM-upgrade expiry. An unfinished command blocks
  those mutations regardless of lease state; the router—not the legacy
  rescuer—handles expiry. With the flag off the injected clause is literally
  empty, so the SQL contains no reference to any Gate-3 relation.
- Lifespan starts and awaits the router only beside the already-enabled
  completion drain. The default-off process does not import or construct it.

Tilt applied 0141 while its first version was visible. When the final live-term
precedence correction changed its checksum, the migration runner correctly
refused startup. No migration row or fixture was edited: 0141 was restored
byte-for-byte to its applied SHA-256 (`e8c8c577043d…`) and frozen, and the
correction moved into forward-only migration 0142. The regenerated schema
replays both files. This is why M2 begins at 0143.

Live k3d proof after 0142: the router left the preserved command/effect states
unchanged, allocated exactly one completed `alert_only` action for each of the
two parked commands, and did not expose or execute the report-seq-2 pending
successor behind its parked predecessor. No manual row cleanup or state
fabrication was performed.

Verification at the M1 boundary: the focused affected matrix passed **186/186**,
including real-Postgres routing rows, oldest-report ordering, two-router
contention, action heartbeat/takeover, exact stale-owner refusal, all legacy
rescuer families, flag-off zero-relation SQL, and the hardest expired-command
case (the no-command twin pauses while the command-owned job is untouched).
The repository-wide suite produced **17,336 passed, 163 skipped, 11 failed**,
with the exact baseline failure list and no new failure. Squawk v2.59.0 found
zero issues in 0141+0142; schema snapshot freshness/idempotence, repository-wide
Ruff check and format check (1,122 files), and whitespace checks pass.

# Session 16 — Gate 3 step 4, M2 command/control linearization (2026-08-13)

## M2 shipped — controls lose cleanly, successors cannot claim

- Completion admission now persists the jobs-row status observed under its
  queue-to-jobs lock. A finalizer resolves that immutable accept-time authority
  (or the immediately preceding command's proven `done` outcome), and a foreign
  status wins by settling the whole command `superseded` before Class C. This
  closes both cancel-after-accept-before-S1 and the ordinary mid-handler Class-A
  race; an unproven pre-0143 command fails closed rather than adopting the
  current jobs row.
- Resume, approve, Mode-A accept/reject, blocking feedback, VM decisions,
  manual assignment, cancel, pause/preemption/drain, agent release and their
  cascade paths now serialize on the jobs row. Slow human-control operations
  publish a fixed-shape, bounded `_completion_control_claim` while fencing the
  old pinned assignment or stateless queue token. Completion admission,
  dispatcher/worker admission, watchdogs, lifecycle cleanup and other controls
  stand down on that marker. Exact owners must consume it before its DB-clock
  expiry; malformed markers fail closed. Flag-off callers retain their legacy
  SQL, collaborator order and response behavior and do not name the new
  relations or marker.
- Worker-batch claiming filters command-owned units before leasing, skips a
  blocked FIFO head, and rechecks after the jobs lock. Pinned dispatcher scans
  and atomic claims use the same command/control exclusions. Post-agent replies
  no longer resurrect a row after a concurrent control: only the exact still-
  assigned pinned owner, with no marker, may consume queued context or publish
  its heartbeat. A bypassed public resume therefore cannot produce a successor
  claim while an unfinished completion command owns the job.
- Pause is a two-phase control rather than a DB-first redispatch window. The
  paused status, assignment fence and marker commit together; the old-agent/VM
  calls follow; the marker clears only on positive quiescence and otherwise
  expires through the bounded recovery path. Durable finalizer pause/recovery
  effects perform their guarded pause as the first transactional mutation.
  Kubernetes pod recovery records its UID-keyed delete-pending state before
  deletion and reconciles it on replay; a lost CAS performs no context write,
  delete or dispatch.
- S36 now has a jobs-lock authorization marker shared with fresh command
  admission. If a higher report acquired the lock first, the lower command
  durably defers without archive/delete; if S36 authorized first, a fresh higher
  report is rejected until that exact pending effect settles. A late no-op in
  `completed`, `pending_review` or `reviewing` consumes only its immediate
  predecessor's deferred teardown, so ordered trailing reports neither repeat
  unrelated effects nor leak the workspace. Kubernetes, VM and Docker command-
  backed teardowns all use the same journal/authority protocol; Kubernetes
  retains the exact UID/snapshot implementation.
- Lifecycle workspace and VM managers conservatively preserve resources for a
  live/malformed control marker and recheck before snapshot, delete, give-up and
  orphan cleanup. This is the M2 control exclusion only. M3 will replace the
  existing completion-command veto with routed ownership and must also close
  the remaining read-before-external-I/O interval by giving lifecycle actions
  a durable shared owner rather than adding another observational check.

Migration 0143 adds nullable `accepted_job_status`. It backfills only from a
completed S1 journal row, never from mutable current job state, and tightens the
canonical `superseded` terminal shape. Tilt applied these bytes before the
pinned-linter pass, so the migration remains frozen at SHA-256
`c292602dddf5…`; the documented Squawk exclusion is limited to its two
empty/default-off-table CHECK findings. Schema replay through 0143 is byte-for-
byte current, and migrations 0141–0143 remain checksummed in k3d.

The required catastrophe is covered in real PostgreSQL both ways: accept-first
makes resume return exact 409 with zero job/context/queue mutation, while a
control claim-first fences completion admission. Directly queued blocked units
remain untouched and a later eligible unit is claimable. Expired/pending
commands allocate one M1 resume action rather than dispatching an agent;
parked commands remain operator holds. S36 authorization/admission interleavings
likewise prove exactly one external teardown owner.

Verification at the M2 boundary: the independently selected affected matrix
passed **869/869**, including PG15 command/control interleavings, accepted-status
supersession, worker head-skipping, pause/cancel/cascade ordering, durable pod
recovery, watchdog exclusions, lifecycle marker guards, S36 handoff chains and
flag-off vacuity. The repository-wide non-fail-fast suite produced **17,451
passed, 163 skipped, 11 failed**, with the exact baseline failure list and no
new failure. Repository-wide Ruff check/format, Python compilation, whitespace,
Squawk v2.59.0, and the full app/vector/audit schema snapshot replay all pass.
The preserved live residue remains **2 parked + 1 pending commands and 2 pending
effects**; M2 did not fabricate, unpark or clean any fixture row.

# Session 17 — Gate 3 step 4, M3 bidirectional deference (2026-08-13)

## M3 shipped — one synthesizer, one durable owner

- Completion effects now accept a transactional `supersede_if` predicate. A
  losing world-state CAS settles that effect as the replayable terminal state
  `superseded`; replay, dependency checks and command finish consistently treat
  `done|superseded` as terminal, while effect-derived authority remains
  deliberately `done`-only. With `COMPLETION_COMMANDS_ENABLED=false`, the
  legacy callback still runs directly and the predicate is not evaluated.
- Both reviewing-parent watchdog arms defer only to a live completion owner,
  covering the target and any verification child. S27 now applies its locked
  target transition and effect marker transactionally, with external
  notification/dispatch follow-up only for the winner. S30 locks the exact
  `reviewing` parent and natural verification round, inserts at most one critic
  in the same transaction as its effect marker, and performs Gitea/workspace
  handoff separately. A watchdog or human decision that wins makes the
  finalizer effect supersede instead of moving the target back.
- Project-loop advancement now locks and validates the exact member world,
  inserts stage-N+1 jobs, updates loop pointers/counters/campaign state, and
  persists a bounded handoff descriptor in one application transaction. The
  old Class-B empty-barrier window no longer exists. S32 uses the same exact
  world CAS and supersedes after a healer wins; the sweeper stands down only
  for a live finalizer, while expired/parked work is routed and allowed to
  contend. The separately replayable external handoff has an exact-output,
  DB-clock lease with claimant-fenced renew/finish and authority checks around
  every independent consequence.
- Vector migration `0018_project_loop_ttl_effects.sql` adds the
  `(loop_id, total_jobs_run)` response-loss ledger. Its insert and the knowledge
  TTL decrement share one vector transaction. Deterministic message identities
  similarly make one durable insert the only notification publisher. The
  migration was applied by Tilt and frozen; M3 required no app migration.
- Lifecycle cleanup replaces the M2 observational command veto with shared
  routed ownership. A live command yields `stand_down`; an expired or alert
  route enqueues the same command attempt and S36 journal instead of starting a
  parallel cleanup or redispatching an agent. Command-free lifecycle work owns
  one renewable `_completion_control_claim` across snapshot through deletion;
  a stale owner cannot clear it or start a follow-on.
- Kubernetes teardown captures exact Pod/PVC/Service UIDs. Dirty snapshots also
  bind Pod UID, IP and SSH host key; clean/unreachable and orphan-PVC paths do
  not invent an SSH dependency. VM teardown carries provision generation, VM
  UID and root-disk UID through direct, HTTP, NATS and controller paths and
  fails closed when a purge identity is unknown. Docker retains its legacy
  cleanup. A hybrid workspace captures both Kubernetes and VM identities before
  I/O and reconciles each independently: retry/unknown outranks identity
  supersession, which outranks completion, so a replacement survives while an
  exact old resource on the other backend can still converge. Replacement
  supersession is local to S36.
- All M3 behavior rides the existing commands flag. Closed-path tests prove no
  command-relation query, lifecycle capability field, loop ledger call,
  `supersede_if` evaluation, metadata addition or collaborator-order change.

## M3 k3d soak evidence

Rows 1 and 2 used literal DB-clock elapsed time, never backdated timestamps.
There is still no production completion-command unpark verb, and M3
intentionally removes the old loop claim-to-spawn state, so the disclosed
park/retry and legacy barrier calls below are controlled fault construction,
not claimed product verbs. Every mutation was UUID/run-tag guarded and excluded
the three preserved command IDs.

| Row | Evidence |
|---|---|
| 1 — reviewing/critic | S17 durably wrote `reviewing` at **11:14:16.857591Z**. At 1,899.494 seconds it was still reviewing with zero critics; the live 300-second watchdog naturally wrote `pending_review` at **11:48:36.472592Z**. The read at 2,145.614 seconds still had zero critics. A guarded human `run_critic` retry plus controlled unpark then exercised real S30. The handcrafted fixture first demonstrated the correct fail-closed connector response because it lacked immutable `datasource_selection`; after a disclosed, single-transaction fixture-ledger correction to an authoritative empty selection, S1/S17 replay and real S30 produced exactly one critic, `89227f57-...`, with S30 `done`, `world_cas_won=true`, and the parent coherently `reviewing`. No S30 external handoff effect ran. |
| 2 — loop heal/advance | A controlled legacy barrier construction at **11:14:16.814369Z** naturally parked the command. At 600.604 seconds the cleared loop still had zero successors. The live sweeper then atomically created sole stage-N+1 job `5d7f1ec6-...` and repointed the loop at **11:24:36.772967Z**; reads at 630.638 and 743 seconds showed exactly that one successor. The extra harness expectation that it be born paused was not part of the acceptance row; it was actually `created` with cloud baseline seeding, and no blind correction was made. |
| 8 — same-name Pod | Real S36 stored an active authorization and captured Pod UID A `78773b27-...`, PVC UID `9b1b0465-...`, and Service UID `012afef2-...` before an injected release failure naturally parked the command. UID-A deletion was exact-preconditioned and absent by **11:22:51.179673Z**. A real same-name replacement reached ready with UID B `f8b91924-...`; exact command/effect replay settled S36 `superseded` with `identity_superseded` at **11:22:57.671292Z**, while UID B and the original PVC/Service identities still existed. |

All disposable resources were enumerated before cleanup. Row 1's critic had
already reached `processing`, so it was quiesced through the production
dual-callable cancel endpoint (HTTP 200), not a blind DB write. Its exact
workspace resources reached absence, its four-object snapshot prefix was
deleted through `SnapshotService`, Gitea was 404, and all six vector job tables
were empty. The fixture cleanup committed at **12:22:06.750391Z**. Two real,
append-only metering facts for that short-lived workspace intentionally remain
(`gib-hour` and `vcpu-hour`); they were not bypass-deleted.

Final independent proof found zero disposable DB/Kubernetes/vector/S3/Gitea
footprint and restored the exact preserved baseline: **7 done, 2 parked and 1
pending commands; 112 done and 2 pending effects**. The protected command/effect
projection was unchanged.

Verification at the M3 boundary: the current affected matrix passed
**1,142/1,142**, including real-Postgres contention and response-loss tests.
The repository-wide non-fail-fast suite produced **17,561 passed, 163 skipped,
11 failed**, with the exact baseline environment-only failures and no new
failure. Ruff check/format over 1,134 files, Python compilation, whitespace,
Squawk v2.59.0 on vector migration 0018 (zero findings), and complete
app/vector/audit schema replay and snapshot freshness all pass.

### Morning hand-check

Repeat row 1 from a naturally admitted production job: let the watchdog win,
use the future operator unpark verb, and confirm exactly one critic without a
fixture-ledger correction. That is the one live row whose handcrafted setup
omitted an immutable admission field; the fail-closed response was correct,
but it makes the production-shaped retry the most valuable hand check.

# Session 18 — Gate 3 step 4, M4 delivery-before-status and recovery nets (2026-08-13)

## M4 shipped — terminal status follows the durable product deliveries

- Migration 0144 adds the immutable per-command
  `status_reorder_enabled` discriminator, defaulting every old row to false.
  Fresh reordered commands are `job-completion-v2`; legacy commands remain
  `job-completion-v1`. A new executor drains both pairs, while an old/v1-only
  executor refuses v2 and a malformed version/capability pair parks loudly.
  This makes rolling deploys and flag changes incapable of silently changing
  the execution order of an admitted command.
- The exact product-delivery set is encoded in one effect-policy module and
  documented in §5.4.5: S15 `loop_project_cloud_delivery`, S26
  `subjob_output_graft`, and S33 `terminal_merge_change_record`. Their groups
  `delivery`, `subjob_graft`, and `terminal_delivery` gate only the canonical
  terminal set `completed|failed|cancelled`. S16 retains its shipped
  `delivery` group for replay compatibility but is not classified as a product
  delivery. S27's transactional verdict core is Class B before status; its
  external follow-up remains after status. M3's atomic S32 remains Class C.
- A reordered terminal command owns a strict jobs-row
  `completion_delivery` marker before S15. S15/S26/S33 recheck the exact live
  command term and that marker immediately before their callback. Cancel,
  pause, resume, admission, dispatch and lifecycle ownership already serialize
  on this marker, closing the check-to-external-I/O gap. S17 validates the
  accepted entry status, exact finalizer term and marker, then consumes the
  marker in its status transaction. Delivery retry/park retains it for exact
  adoption; force resolution may clear only that command's full marker shape.
- A crash after all delivery gates but before S17 replays the stored delivery
  output, writes S17 once and resumes the historical tail. It does not
  reacquire/run pre-status delivery after a completed S17. Pending delivery
  groups are sticky within the runner, so the finalizer releases to the
  effect's `run_after` instead of spinning or consuming command attempts.
  Response action ordering remains at the old presentation sites even though
  the corresponding delivery executed earlier.
- The status-reorder gate is independently default-off and hard-requires
  completion commands at both Helm render and process startup, before any DB
  connection. Admission stores the current gate; execution never consults the
  current global value. The ops note records the safe revert sequence: disable
  new reordered admissions, drain persisted-true unfinished rows, then roll
  back the image; never disable the command executor first.

## Day-one safety, operator recovery and alarms

- The independent safety net runs before each finalizer claim and from router
  maintenance. It never invokes a workflow callback. Only old-mode rows on an
  already terminal job, or sufficiently stale rows with no effect beyond S1,
  qualify. Live command/effect/action/control owners and active S36
  authorization are absolute holds; persisted reordered post-S17 tails are
  deliberately ineligible. Existing pending effects become `superseded` with
  their intent retained and an explicit `executed=false`, `callbacks=false`
  result.
- Admin `unpark` rearms the command and all pending-effect attempt/deadline
  budgets. Admin `force-resolve` requires the expected state, an explicit
  canonical terminal status and a bounded incident reason; it records the
  complete stable unstarted-effect plan, abandons materialized pending effects,
  writes the requested Class-A job state and marks the command
  `force_resolved`. Both reject active S36 and live executors. Pinned authority
  is not guessed from a stale FK: it requires a live jobs lease plus an exact
  working/draining agent whose `current_job_id` matches. Stateless authority
  uses the exact live queue lease. Operator locking remains queue → jobs →
  command. The routes stay service-dark when completion commands are off, but
  remain available with only the reorder gate off so persisted rows can drain.
- A separate 30-second monitor task reports a missing live finalizer leader
  after startup grace and the DB-clock age of the oldest unfinished command,
  including parked rows. Fixed dedup keys prevent an increasing age value from
  filing repeated incidents. Monitoring is not coupled to the finalizer drain;
  router maintenance contains only the safety pass.

## Preserved rollback residue

Opening the local reorder flag consumed the three explicitly preserved rows
without manual repair:

| Command | Result |
|---|---|
| `c6735d9d-…` / job `1a97a6b5-…` | Safety-superseded at **13:24:57.059316Z**. Its pending S36 moved to `superseded` with intent retained, `executed=false`, `callbacks=false`; 15 historical effects remained done. |
| `b9ab9aa6-…` / job `d119a6fc-…` | Safety-superseded at **13:24:57.155629Z** with the same non-executing S36 treatment; its pre-existing M1 `alert_only` action remained done. |
| `a010c9f0-…` / the same job | The first safety pass could not skip its still-unfinished predecessor. Once the predecessor settled, the ordinary `resume_finalizer` route claimed it and entry authority superseded it at **13:24:57.200581Z** because the pre-0143 row had no accepted-status/S1 proof and a safety-superseded predecessor is deliberately not adoptable. It has zero effect rows, hence zero callbacks. |

Both jobs remain completed. The final residue is **7 done + 3 superseded
commands; 112 done + 2 superseded effects; 0 unfinished commands, pending
effects, action claims or control markers**, with one live finalizer leader.

## M4 k3d row-9 crash proof

The disposable v2 command was `2936200e-…` on job `ab368922-…`. A pod-local
one-off harness used the production finalizer/workflow but intercepted only the
S17 DB call. Before the cut, the job was still `processing`, the strict
`completion_delivery` marker named the command, S33 was `done` on attempt 1,
and the sole immutable change record had committed at
**13:37:35.722666Z**; `main_status_write` did not exist. The actual sole
orchestrator pod was then deleted, killing both the app and harness process.

The replacement pod was Ready with both flags true by **13:38:41Z**. It waited
for the killed command term rather than fabricating expiry, reclaimed the
command on attempt 2, and finalized at **13:39:46.620873Z**. S33 remained
attempt 1 with its original completion timestamp and the record count remained
exactly one. S17 ran once at **13:39:46.393365Z**; S36 ran once at
**13:39:46.599490Z** with `teardown_disposition=completed`. All 19 effects
were done with maximum effect attempt 1, the job was `completed`, the delivery
marker was absent, and no Pod/PVC/Service, workspace/resource interval,
run-queue row, action, message or notification existed for the fixture.
Guarded cleanup then deleted exactly its 19 effects, one change record, one job
and one offline fixture agent, restoring the residue counts above.

An earlier disposable harness attempt intentionally used over-short local
lease values and was rejected before S1 with `completion command lease must
outlive its effect timeout`; the ordinary background finalizer subsequently
completed it, and its exact fixture rows were guarded and removed. It is not
counted as row-9 evidence.

## Verification and stretch decision

The M4 affected matrices passed **306/306**, with independent audit runs of
155 core/admission/Helm, 74 schema/admission/finalizer real-Postgres, 26
resolution pure/real-Postgres, and 22 final-wiring/admin cases also green. The
repository-wide non-fail-fast suite first exposed two contract artifacts: the
migration-head assertion still named 0143 and the endpoint-auth inventory
lacked the two new admin routes. After updating both, the suite produced
**17,651 passed, 163 skipped, 11 failed**, exactly the baseline environment-only
list, with 60 warnings. That integrated run also collected the already-frozen,
still-unstaged two-test M5 invariant file; M4's own focused and real-Postgres
counts above are independent of it. Repository-wide Ruff check/format over 1,144 files,
Python compilation, whitespace checks, both Helm lints, Squawk v2.59.0 on 0144
(zero findings), and full app/vector/audit schema replay and idempotence pass.

M6 was deliberately not started. Read-only reconnaissance found that the
existing worker cleanup treats a fresh stateless 202 while the reordered job is
still `processing` as permission to destroy its tmux shell. A background
command may instead resolve to review, pause, waiting or retry, so the stretch
needs a worker-handoff fix plus a real human-stop/shell-preservation soak. M6
is therefore deferred rather than shipping a completed-only happy-path branch.

### Morning hand-check

Repeat row 9 with a naturally admitted, real-workspace job whose S33 performs
an actual command-keyed project delivery: hold S17 after the delivery record,
kill the pod, then verify the record/delivery hash and effect attempt stay
unchanged while S17 and the UID-fenced S36 tail each converge once. This is the
highest-risk ordering claim and exercises more external surface than the
disposable no-project record used overnight.
