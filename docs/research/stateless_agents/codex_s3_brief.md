> **STATUS 2026-08-09 — PARTIALLY EXECUTED; HISTORICAL.** Gates 1 and 2 from §3
> are built, verified and merged onto `feature/stateless-agents`; the branch this
> brief tells you to create was consolidated and deleted. **For current status
> read `docs/features/stateless_agents.md` §9.1.**
>
> **Do not follow §3's plan as written.** Gate 3 is not the predicate change this
> brief describes — there is no completion CAS to re-key. It is a design problem,
> specified in §5.4.5 with its evidence base in
> `completion_path_side_effect_inventory.md`, and everything downstream (the
> worker driver, Phase 4's single pool, all worker acceptance) is blocked behind
> it.
>
> §5's single-pool goal also has evidence against it now: a pod-local capacity
> reserve cannot guarantee interactive availability across a rollout or pod loss,
> which argues for the two-Deployment split the design originally specified.

# Codex brief — S3: worker jobs on the stateless lane, one pool for both

You are picking up a feature mid-flight. The session half is built, proven on
k3d, and committed; the worker half does not exist. Your job is to build it on
the substrate that is already there, and to end with **one agent Deployment
that serves both session turns and worker job batches from the same queue**.

Work on a branch off `feature/stateless-agents`. Do not push. Do not touch
`develop`.

---

## 1. Read these first, in this order

1. `docs/features/stateless_agents.md` — the design. Read **§9.1 Implementation
   status** first: it is written against the code and tells you exactly what
   exists. Then §5.1 (queue lifecycle), §5.2 (lease and fencing), §5.4
   (workers — this is your spec), §5.4.4 (**the coexistence ruling; this is
   your first safety gate**), §5.8 (deployment shape), §9 (phasing/S3 list).
2. `docs/research/stateless_agents/implementation_log.md` — two sessions of
   build notes: decisions with reasons, deviations, what broke and why. The
   "Traps hit" sections will save you hours.
3. `src/shared/run_queue/queries.py` — read the module docstring. It states the
   contract invariants you must not weaken.

Then read the session driver you are about to mirror:
`src/api/turn_executor.py`.

---

## 2. What already exists (do not rebuild it)

The queue and lease substrate is **kind-agnostic and finished**. Migrations
0115/0117 give you `run_queue` with `unit_kind`, monotonic `lease_token`,
`input_seq`/`consumed_seq` watermarks, `last_leased_by` affinity, and per-row
reaping. `src/shared/run_queue/` gives you `enqueue_unit`, `claim_unit`,
`heartbeat_unit`, `fence_lease`, `complete_unit`, `release_unit`,
`reap_expired`, `unpark_unit`, `list_active` — every one takes `unit_kind` as a
parameter and none of them knows what a session is. **Adding workers needs no
schema change to `run_queue` and no new lease semantics.** 81 real-Postgres
tests pin the contract; keep them green.

The reaper (`orchestrator/services/run_queue_reaper.py`) already steals expired
leases of any kind. It deliberately skips the journal write for non-session
kinds (`(S3)` comment) — deciding what a stolen *worker* unit needs instead is
yours.

What is **not** shared, despite living in `src/shared/`: `event_journal/`
hardcodes `threads`/`thread_events`. Workers have no journal. Whether they get
one is open question §10.2 — do not invent one without deciding it explicitly
in the doc.

---

## 3. Hard safety gates — in this order, no exceptions

These are not style preferences. Each one is a way to corrupt user data or
silently destroy work, identified by an adversarial review pass and verified in
code.

**Gate 1 — the coexistence partition, BEFORE the first `worker_batch` unit is
ever enqueued.** §5.4.4. Today `get_dispatchable_jobs`
(`orchestrator/database/postgres.py`) selects `runner_kind` and filters on
nothing, and neither lease sweep excludes a class. If a stateless pod holds a
healthy `run_queue` lease while `jobs.lease_expires_at` goes un-renewed,
`recover_expired_lease_jobs` flips the job to `paused` and unassigned, the
leader dispatcher hands it to a pinned agent, and **two executors run the same
job against the same workspace**. Add `jobs.execution_lane` (do NOT reuse
`jobs.runner_kind` — it exists with *grant* semantics, `user|lifecycle|service`,
and overloading it tangles dispatch grants with runtime class). Then exclude the
stateless class from `get_dispatchable_jobs`, `claim_job_for_agent`,
`recover_expired_lease_jobs`, and `recover_orphaned_jobs`. The run_queue reaper
becomes the sole rescue authority for that class. Write a test that fails
without the partition.

**Gate 2 — the freeze-type registry, BEFORE any agent can emit
`batch_boundary`.** §5.4.1. `determine_job_status` maps an *unknown* freeze type
on a **loop** job to `('completed', None)` — "so the loop advances". So an
agent that emits `batch_boundary` to an orchestrator that doesn't know it yet
turns every batch boundary on a loop job into a phantom-COMPLETED job whose
partial work the RSI loop then merges as if finished. Four keep-in-sync freeze
lists span two separately-rolling images. Consolidate them into one module
under `src/shared/` (`src/shared/orch_surface/` is the precedent) consumed by
both the agent and `orchestrator/services/completion.py`, and parameterize the
`recover_orphaned_jobs` SQL literals from it. Deploy order constraint to
respect and to write down: **orchestrator understands `batch_boundary` before
any agent emits it; batch mode goes off before any orchestrator rollback.**

**Gate 3 — completion CAS keys on `lease_token`.** The stage-4 job completion
CAS currently keys on `assigned_agent_id`, which leaves the
fenced-out-`/complete`-still-wins hole open for the stateless class.

Do not start Gate 2 before Gate 1 is done and tested. Do not enqueue a real
`worker_batch` unit on the cluster before both are.

---

## 4. The build, in dependency order

Stop cleanly at a phase boundary rather than half-finishing several. A landed,
tested Phase 2 is worth far more than four phases of scaffolding.

**Phase 0 — orient.** Get the cluster green, drive one session turn with
`scripts/stateless-lane-probe.sh turn "..."` and read the timing line. You need
to know what healthy looks like before you change anything.

**Phase 1 — Gate 1** (jobs lane column + all four sweep exclusions + test).

**Phase 2 — Gate 2** (freeze registry consolidation) **and** the
`batch_boundary` freeze itself. Clone the shape of the existing
`version_upgrade` drain check at `handle_transition` in the graph. Budget is
**wall-clock-first** (≥5 min per batch), superstep cap secondary — §5.4.3
explains why: per-claim setup plus the handoff is ~1–5 s, which is 3–15%
overhead at a 25-superstep batch but under 2% at wall-clock-sized batches.
Mid-phase caps must only fire when transient carriers are empty (`_replan_request`
is consumed one superstep *after* it is set).

**Phase 3 — the worker driver.** Mirror `src/api/turn_executor.py`: claim a
`worker_batch`, fetch a bundle, run the graph to the next boundary, checkpoint,
complete with the watermark (or requeue). Reuse `src/shared/run_queue/`
unchanged. Companion fixes §5.4.1/§5.4.2 require: hydrate TodoManager from the
checkpoint on **any** resume (the mid-loop lane currently resumes with an empty
TodoManager and `check_todos` force-ends tactical phases — that is today's pause
bug, don't inherit it); make the tmux kill **conditional** (`ShellManager.cleanup()`
kills workspace tmux on every job end including pause, so "shell survives" is
false for workers today — deterministic session names make skip-the-kill free
cross-batch continuity); skip `job_frozen.json`, git commit/push, todo archive
and evidence push at a batch boundary; keep the bounded memory drain and
checkpointer/datasource close. Any app-layer generator break must
`await gen.aclose()` — the checkpoint flush rides generator close.

**Phase 4 — one pool for both kinds.** See §5 below; this is where you must
deviate from the doc deliberately.

**Phase 5 — the acceptance gate.** See §7.

---

## 5. The one-Deployment decision — a deliberate deviation, and it needs a guard

Read this carefully, because the design doc says something different and you
should understand why before you override it.

§5.8 specifies **one image, two Deployments**: an interactive one with a warm
floor sized by the Erlang formula, and a worker one on a KEDA scaler with
`minReplicaCount: 0` and a 300 s cooldown. The reason is that the two classes
want opposite autoscaling: sessions need warm capacity for latency, batches
should scale to zero for cost.

The intent here is **one Deployment serving both kinds**. That is a legitimate
answer to open question §10.6 ("does the worker deployment lend capacity to
session bursts?") — a shared pool means a session burst can borrow idle worker
capacity instead of queueing, and it removes KEDA as a new cluster dependency
entirely. But it reintroduces exactly the risk the split was avoiding: **a
multi-minute worker batch occupying a pod starves interactive turns**, and an
interactive turn that waits behind strangers is the one regression §5.3.7 says
must have a bound.

So a shared pool is only acceptable with a starvation guard, and building that
guard is part of this work, not a follow-up:

- **Reserve capacity for `session_turn`.** A pod (or the pool) must not let
  batch work consume every slot. The simplest correct version: the claim loop
  refuses to claim a `worker_batch` when fewer than N slots remain free for
  interactive work. Whatever you choose, the reservation must be *enforced*, not
  advisory.
- **Interactive claims win.** `run_queue.priority` exists and the claim orders
  by `priority DESC` — decide whether session units carry a standing higher
  priority, and write down what that does to worker fairness.
- **Bound the wait and measure it.** The acceptance criterion is a p95 claim
  wait for `session_turn` under concurrent batch load. If you cannot hold a
  bound, say so with numbers and recommend the two-Deployment split instead —
  that is a perfectly good outcome, and a measured "we tried one pool and here
  is why it needs two" is more valuable than an unmeasured merge.

Update §5.8 and §10.6 in the doc with whichever way it lands and why.

---

## 6. How to work (this is how today's session got 99.6 s → 5.4 s)

**Measure before you optimize, and measure after every single change.** Today's
biggest win came from instrumenting first: the hypothesis was that per-request
config resolution was the cost, and the measurement showed it was 70 ms while
two duplicate cloud-tree walks were 82 s. Do not trust a reading of the code
about where time goes. `turn timing:` / `setup steps:` / `claim-bundle timing:`
log lines already exist — add the worker equivalents and use them.

**Verify what is actually in the image, not the tag.** Tilt is running and
deploys your working tree, but it will happily build a **partially-edited**
snapshot. `updateStatus: ok` is not evidence. Twice today an image shipped a
call site without its method definition. Before trusting any measurement:
`kubectl --context=k3d-srw -n srw exec <pod> -c agent -- grep -c "<a string you just wrote>" /app/<file>`
on **every** running pod.

**Never `git checkout` another branch while Tilt is up** — Tilt watches the
filesystem, so a branch switch is a *deploy of that branch*. A few seconds on
`develop` today left a pod crash-looping on `--mode: invalid choice: 'stateless'`.
To check whether a test failure predates you, stash the specific file
(`git stash push -- <path>`) or use a worktree outside Tilt's watch root.

**Keep a log as you go**, appended to
`docs/research/stateless_agents/implementation_log.md`: decisions with reasons,
deviations from the doc, measurements, what broke. Statuses in it must reflect
only work that is actually complete and verified — writing "DONE" against
unverified work poisons the next session's starting assumptions.

**When a test fails because you changed a contract**, update the test to the new
contract with a comment saying why, or decide the contract change was wrong.
Never weaken an invariant to make a test pass.

---

## 7. Definition of done

Unit and contract level:

- `pytest tests/ -q` — the local baseline has ~11 pre-existing failures from the
  Python 3.14 environment (`mcp_manager`, arxiv/semantic-scholar package
  contracts, `test_connect_disconnect`). Confirm any failure you see reproduces
  on `develop` before spending time on it; do not chase them, and do not let
  them hide a real one.
- `ruff check src/ orchestrator/ tests/` and `ruff format --check` clean.
- New real-Postgres tests for anything you add to the queue contract. Recipe:
  `kubectl --context=k3d-srw -n srw port-forward svc/srw-postgres 55440:5432 &`
  then `RUN_QUEUE_TEST_DSN=postgresql://srw:dev_pg_password@localhost:55440/run_queue_test pytest tests/test_run_queue.py -q`.
  The DSN database name **must** contain "test" — the fixture refuses otherwise,
  because these tests TRUNCATE.
- The harness in `tests/test_run_queue.py` applies each run_queue migration
  explicitly; add yours to `MIGRATION_FILES` or your column won't exist there.

On the cluster:

- A worker job completes across **at least two batches on two different pods**,
  with the same final result a pinned run produces.
- Fault injection, mirroring what S1 had to pass:
  `scripts/stateless-lane-probe.sh kill` for the session shape, and the
  equivalent for a job — kill the pod mid-batch, confirm the reaper steals,
  confirm the job resumes on another pod, confirm **no duplicate work and no
  phantom completion**. Do the loop-job skew case explicitly (Gate 2's hazard).
- A fenced-out `/complete` is rejected (Gate 3).
- Sessions and jobs served by the **same** Deployment concurrently, with the
  session claim-wait bound from §5 measured and stated.

Migration discipline (non-negotiable, CI enforces some of it):

- Migrations are new numbered files in `orchestrator/database/migrations/app/`.
  **Never edit `schema.sql` or `vector_schema.sql`** — they are frozen at
  cutover.
- After **any** migration: regenerate with `scripts/schema-snapshot.sh` and
  stage the snapshot **in the same commit** as the migration, or the CI drift
  gate fails.
- Bump `APP_CURRENT_MIGRATION_HEAD` in
  `tests/test_infrastructure_metering_migrations.py`. It is a deliberate
  tripwire and it was left stale once already.
- Never `git add -A`. Stage explicit paths (there is an untracked `HomeLab/`
  directory that must stay untracked).

Cluster etiquette:

- Never run `helm upgrade`/`helm install` by hand, and **never `tilt trigger srw`
  — it uninstalls the release.** Tilt owns the release; edit
  `deployment/values-local.yaml` and let it reconcile.
- The stateless session lane is currently enabled in the gitignored
  `deployment/values-local.yaml` and the chart default is off. Keep the chart
  default off. Whatever you add stays flag-gated.
- Only flip a thread to the stateless lane while it is **detached**
  (`agent_id IS NULL`), and don't drive `/resume` or `/prepare` on a stateless
  thread — nothing in code stops you yet, and it will boot a pinned pod.

---

## 8. Two cheap wins you may take if you have time left

Neither is S3, both are small and independent:

1. **Path-A resume-compaction persistence** — the one live functional bug in the
   shipped path. Path B persists its resume compaction
   (`src/api/persistent_app.py:6552`); Path A calls
   `ensure_within_limits(..., trigger="resume")` at `:6441` and returns at
   `:6473` without persisting. The comment at `:6543` says Path A skips
   deliberately to avoid a banner double-render — so the fix must advance the
   boundary row **without** reintroducing that double render. Impact: any thread
   that has ever compacted takes Path A, so an over-budget tail pays a blocking
   auxiliary-LLM summarization on *every claim* and discards it.
2. **The scoped metadata index on the worker path** — `begin_read_cache()` /
   `end_read_cache()` on `VirtualWorkspaceBackend` collapse dozens of rclone
   process spawns into one listing, and are wired only into
   `PersistentSession.setup`. `src/agent.py` builds the same backend for lite
   jobs and never opens it. It needs the same open / `finally`-close pair around
   the job's workspace setup. Read `no_workspace_agent_mode.md` §5.1 for why op
   *count* is the cost on that tier.

---

## 9. Report back with

What you built, what you measured (numbers, not adjectives), what you decided
differently from the design doc and why, what is still broken or unverified, and
the commits. If you concluded the one-pool design should not ship, say that
plainly with the evidence — that is a successful outcome of this task, not a
failure of it.
