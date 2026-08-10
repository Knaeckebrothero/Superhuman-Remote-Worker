# Codex brief — S3 driver: worker jobs on the stateless lane

**Goal: an opted-in worker job runs on the stateless lane end to end — enqueue →
claim → batch → release → reclaim on another pod → terminal completion, exactly
once — with the pinned lane untouched.**

Branch `feature/stateless-agents` (work directly on it, as before). Do not push.
Do not touch `develop`. Migrations: you own **0133–0139** (0130–0132 are Gate 3
step 1; Gate 3 keeps 0140–0149).

The governing ruling is the **scope correction in
`docs/features/stateless_agents.md` §5.4.5 (2026-08-10)**: a batch rotation
releases through the run_queue and **never calls `/api/jobs/{id}/complete`**.
Terminal stops call it once, exactly as the pinned lane does today. Everything
you build follows from that.

---

## 1. Read first

1. `docs/features/stateless_agents.md` — §5.4.5 "Scope correction" (the four
   conditions are your requirements), §5.4.1–§5.4.3 (batch edge, cheap
   teardown, steering), §5.4.4 (coexistence; **point 2 — the claim CAS on the
   jobs row — is yours to build**), §5.7 (fenced saver, dependency pins), §9.1.
2. `docs/research/stateless_agents/implementation_log.md` — the traps sections.
3. `src/api/turn_executor.py` — the session claim loop you are extending;
   `src/shared/run_queue/` — the queue you must not modify beyond adding reads
   you need (its module contract: touches ONLY run_queue).

## 2. Facts verified against code (2026-08-10) — do not re-derive

* `turn_executor.py` polls `UNIT_KIND_SESSION_TURN` only (`:473`). Nothing
  enqueues or claims `worker_batch` anywhere.
* The lane partition is live in eight predicates (`jobs.execution_lane =
  'pinned'` in all four sweeps + dispatcher). A stateless job is invisible to
  the dispatcher — which also means **`queue_job_for_resume`
  (`postgres.py:6754`) strands a stateless job forever**: it parks as
  "'paused' (dispatchable)" and the dispatcher never looks.
* S3 Gates 1+2 are merged: the freeze registry (`src/shared/job_freeze_types.py`)
  has `batch_boundary` with its arming envelope built but **unarmed**;
  `determine_job_status` maps it to `paused` — that mapping is now
  defense-in-depth for version skew, not the mechanism.
* The S2 tmux ownership substrate (`src/core/backends/remote.py`, lease-token
  fenced, flock-serialized) is what preserves worker shell state across claims.
  Do not reintroduce kill-on-disconnect; drive through it.
* The pinned dispatch payload is built in `_dispatch_job_to_agent`
  (`orchestrator/main.py:3584`, `JobStartRequest` at `:3987`). **Reuse that
  builder** for the worker claim bundle — credential injection at dispatch
  parity, delivered against `(unit_id, lease_token)` proof like the session
  bundle. Do not re-derive config resolution.
* Agent-side job end says nothing to the orchestrator except
  `report_completion` (plus a registered-agent heartbeat stateless pods don't
  have). Steering acks are a separate endpoint (`ack_job_guidance`).

## 3. The work, ranked — core loop first

### 3.1 Core loop (the night's meat)

1. **Admission**: a job opts in at creation with `execution_lane='stateless'`
   behind a helm/config gate, **default off**. First slice: Kubernetes-pod
   workspaces only — **VM jobs stay pinned** (the stateless Deployment has no
   mesh sidecar). Where the pinned path would dispatch, enqueue a
   `worker_batch` unit (`unit_id = job_id`).
2. **Claim**: extend the executor to also poll `worker_batch` (sessions first —
   a worker claim must never starve an interactive one; separate poll cadence
   is fine). The claim transaction **CASes the jobs row**
   (`created|paused → processing`) atomically with the queue claim (§5.4.4
   pt 2); `assigned_agent_id` stays NULL — `leased_by` is diagnostics.
3. **Run**: the worker graph on a **fenced saver** (§5.7): process-wide psycopg
   pool, fence-and-retry subclass on the run_queue lease, never
   `pipeline=True`. Pin `langgraph-checkpoint-postgres` 3.x exactly +
   `LANGGRAPH_STRICT_MSGPACK=true` (CVE-2025-64439; the repo pin is `>=1.0.0`
   — fix it).
4. **Batch boundary**: arm the existing `batch_boundary` envelope wall-clock
   first (300 s floor in production config, but make the budget overridable per
   job via context so the k3d exercise rotates in ~60 s). In-graph freeze →
   END.
5. **Release**: on a `batch_boundary` freeze the driver **skips
   `report_completion` entirely**, does the §5.4.2 bounded teardown (memory
   drain, checkpointer close; skip `job_frozen.json`/git-push/todo-archive;
   tmux stays alive via the S2 substrate), and returns the unit to the queue
   runnable-now. Pick the queue verb deliberately (complete-and-requeue vs
   release) and write down why.
6. **Reclaim + resume**: successor claims, gets the bundle, resumes from the
   checkpoint, and **hydrates TodoManager from checkpointed state** (the
   companion fix — on ANY resume, not just the END lane).
7. **Terminal**: a genuine stop calls `report_completion` exactly as today, and
   the orchestrator gains the **thin entry fence** (§5.4.5 condition 2): for a
   stateless-lane job, reject any report whose lease token is not the unit's
   current token. ~20 lines plus tests. Late zombie → rejected loudly.

### 3.2 Verbs and steering (needed for a usable lane)

8. **Lane-aware verbs** (§5.4.5 condition 3): resume/approve/feedback
   re-enqueue the unit instead of dispatcher-parking; cancel removes or parks
   the queued unit, and a **leased** holder learns of cancel/preempt via lease-
   renewal `RETURNING` (condition 4).
9. **Steering**: on claim, pull queued replies DB-direct and rebuild the dedup
   set from checkpointed state; ack only after the absorbing checkpoint is
   durable (§5.4.3). Minimal correct version; no new tables.

### 3.3 Stretch (only if the night is long)

10. Job-log per-claim capture (today's archive trigger dies with pod deletion).
11. Worker Deployment split + KEDA. For k3d proving, the existing stateless
    pods may claim both kinds behind the gate; the production shape remains two
    Deployments (§5.8 ruling stands — do not architect around one pool).

## 4. Scope guards

- **Rotation must not call `/complete` — assert it in a test** (e.g. spy on the
  client; a rotation that reports is a failure).
- **Do not build Gate 3 step 2** (no command/effect tables, no finalizer, no
  `completion_seq_hwm`). If something seems to need them, stop and say so.
- Do not modify `src/shared/run_queue/` semantics; additive reads only.
- Everything default-off; a fresh install behaves exactly as today.

## 5. Acceptance

- **Happy path on k3d**: an opted-in worker job (k8s-pod workspace) enqueues,
  is claimed by stateless pod A, runs ≥1 batch, rotates (release, no
  `/complete` call — shown in logs), is reclaimed by pod B, todos intact via
  checkpoint hydration, shell state intact via the S2 substrate, completes
  terminally with **exactly one** `/complete`, correct final status, workspace
  archived. Numbers for each phase.
- **Kill test**: pod force-deleted mid-batch → reaper steal → successor resumes
  from checkpoint → exactly-once terminal completion; the zombie's late report
  is rejected by the entry fence (visible log/4xx).
- **Verb test**: cancel a queued stateless job (unit leaves the queue; status
  correct) and resume a review-paused one (unit re-enqueued, NOT
  dispatcher-parked).
- **Pinned untouched**: README smoke path; a pinned job completes exactly as
  before.
- Standing gates: full suite at the 11-failure baseline, `ruff check` +
  `ruff format --check` clean, helm lint, schema snapshot + head-pin in the
  same commit as any migration, squawk on lint-covered migrations.

## 6. Traps (all have drawn blood)

- Tilt ships partially-edited images — `kubectl exec <pod> -- grep` a string
  you just wrote, on EVERY pod, before trusting a run.
- `git checkout` while Tilt is up deploys that branch. `kill -9 1` in a
  container does nothing — `kubectl delete pod --force --grace-period=0`.
- A blocking advisory lock deadlocks against `CREATE INDEX CONCURRENTLY` —
  use a session try-lock in `.notx` work (learned in 0131/0132).
- `IF NOT EXISTS` false-greens against an INVALID index; the hardened
  migration runner handles retry recovery — don't bypass it.
- admin-cli's `access_token` has no `sub`; use the `id_token`; it dies in
  ~15 min as a silent 401.
- Never `git add -A` (shared worktree); never `helm upgrade` by hand; never
  `tilt trigger srw` — it uninstalls the release.
- A fresh git worktree fails 19 helm tests spuriously — don't baseline there.

## 7. Stop rule

Stop only when a premise is load-bearing AND there is no reasonable alternative
route. A discovered dependency is scope to absorb — record the deviation in
`docs/research/stateless_agents/implementation_log.md` and continue. Keep §9.1
of the feature doc current as you land things; leaving it stale is a defect.

## 8. Report back with

What you built; what you verified with numbers; which queue verb you chose for
rotation and why; every deviation from §5.4.5's four conditions; what remains
unverified. And explicitly: the log line or test proving a rotation made zero
`/complete` calls.
