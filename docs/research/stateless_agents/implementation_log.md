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
