---
tags:
  - issue
  - jobs
  - checkpoint-resume
  - agent-resilience
  - phase-snapshot
  - workspace
  - kubernetes
  - preemption
  - project-loop
---

# Cross-pod resume cold-starts: the LangGraph checkpoint is never replicated to shared storage

**Filed:** 2026-06-25, discovered while scoping the fix for
[`preemption_before_first_checkpoint_replays_job_opening.md`](preemption_before_first_checkpoint_replays_job_opening.md).
That issue documents the *trigger* (parasitic preemption restarting a job's
opening); this one documents the deeper, independent defect underneath it: **a
job that resumes on a different pod has no way to recover its graph state, so it
cold-starts — and this is true even after it has crossed a phase boundary.** The
preemption doc's "D3" fix lands here (recommended approach: a shared Postgres
checkpointer — see Recommended fix).

## TL;DR

The agent's resume machinery has two recovery sources — the **pod-local
LangGraph checkpoint** and a **phase snapshot** — and *neither survives a move to
a different pod*:

- The checkpoint DB is pod-local (known).
- The phase snapshot was supposed to be the cross-pod bridge, **but it copies
  `checkpoint.db` pod-locally too and never pushes it to the shared workspace.**
  Only the workspace `.md`/`.yaml` files are pulled from shared storage.

So on any cross-pod resume, `get_latest_snapshot()` returns nothing on the new
pod, `_resume_from_snapshot` hits its no-snapshot branch, and the graph
cold-starts from `create_initial_state` (re-reads brief, resets `phase_number`,
empties `messages`, re-runs phase 0). The checkpoint/snapshot system effectively
only works for **same-pod** pause→resume.

**Recommended fix:** make the checkpoint shared *by construction* — swap the
pod-local SQLite saver for `AsyncPostgresSaver` (Strategy B below). Probe-verified
viable (2026-06-25); the snapshot-bridge approach is demoted to a fallback.

## Symptom

A job that changes pods mid-run **loses its in-graph state and restarts the
graph from scratch** — re-reads its task brief, resets its phase counter and
iteration to the initial values, drops its message history, and re-runs its
opening strategic phase. Workspace files (plan.md, todos.yaml, archive/, notes/,
KB) persist, so it is not *total* data loss, but every cross-pod resume is a
graph cold-start, not a resume.

Observed (pre-boundary): in loop `27cabc53`, scholar job
`1b099a61-b4e0-4368-982c-5aaa094c5363` was preempted twice during phase 0 and
resumed on three different pods (`55f440ed → d13800ec → 4830ffb1`),
cold-starting all three times — it never once recovered state cross-pod. (Full
incident in the preemption doc.)

Confirmed (post-boundary, 2026-06-25): the snapshot directory is pod-local too
(Root cause §2–3), so a job preempted *after* completing a phase also cold-starts
on a new pod — losing every completed phase's graph state, not just the opening.
Confirmed by code read (the snapshot's `checkpoint.db` is never pushed to the
shared workspace) plus the empirical three-pod cold-start, so no flaky live
relocation was needed (see Verification).

## Root cause

Four facts, all on the agent side. The first is known; facts 2–3 are the new
finding.

### 1. The live checkpoint is pod-local

`AsyncSqliteSaver` persists graph state to `/workspace/checkpoints/job_<id>.db`
on the agent **pod's own volume**. `get_checkpoints_path()` resolves under
`get_workspace_base_path()` (`src/core/workspace.py:89-138`), explicitly *"the
agent process's own state directory … NOT the job workspace."* A fresh pod has
no copy. (Checkpoint path also `src/agent.py:3110-3119`.)

### 2. The phase snapshot — the intended cross-pod bridge — is ALSO pod-local for the checkpoint

`PhaseSnapshotManager.create_snapshot` (`src/core/phase_snapshot.py:198-333`)
writes to `self._snapshots_dir / f"phase_{n}"`, and `get_phase_snapshots_path()`
(`src/core/phase_snapshot.py:110-121`) resolves under the **same pod-local**
`get_workspace_base_path()`. The `checkpoint.db` is WAL-flushed and then copied
with a plain local `shutil.copy2` (`src/core/phase_snapshot.py:236-247`).

### 3. The snapshot pulls workspace files from shared storage, but never *pushes* the checkpoint to it

`create_snapshot` *does* reach the shared SSH workspace — but only to **pull**
`workspace.md`/`plan.md`/`todos.yaml` + `archive/` into the pod-local snapshot
dir (`src/core/phase_snapshot.py:260-272`). The `checkpoint.db` — the only thing
that holds the LangGraph state (message history, node position, `phase_number`,
`iteration`, `is_strategic_phase`, in-graph todos/`staged_todos`, `freeze_data`)
— is **never written back to the shared workspace**. Neither is the snapshot
directory itself. So the durable, portable artifact the resume path depends on
exists only on the pod that created it.

### 4. Resume therefore finds nothing on a new pod and cold-starts

The resume branch (`src/agent.py:756-865`) for a graceful `paused` status
(`paused` ∈ `GRACEFUL_STOP_STATUSES`, `:804-809`) tries
`_resume_from_checkpoint` first (`:819-829`). On a different pod the local
`checkpoint.db` is absent, so that returns a non-`None` fresh state (success
returns `None`; failure returns a fresh-state dict — `src/agent.py:2940-2943`),
and the guard at `:830` falls through to `_resume_from_snapshot`
(`src/agent.py:2991-3108`). That calls `snapshot_manager.get_latest_snapshot()`
(`:3010`), which scans the **pod-local** `_snapshots_dir`
(`src/core/phase_snapshot.py:558-565` → `list_snapshots` `:335-361`) and returns
`None` on the new pod, hitting the cold-start branch:

```python
# src/agent.py:3010-3018
latest_snapshot = snapshot_manager.get_latest_snapshot()
if not latest_snapshot:
    logger.warning(f"No phase snapshots found for job {job_id}, starting fresh")
    graph_input = create_initial_state(...)   # ← re-enters init_workspace, iteration 0
    return graph_input, thread_id, thread_config
```

`create_initial_state` (`src/core/state.py:136-195`) resets `initialized=False`,
`phase_number=1`, `is_strategic_phase=True`, `messages=[]`, `iteration=0`.

**The crux:** the restore logic itself is fine — `recover_to_phase`
(`src/core/phase_snapshot.py:385-514`) is a *verbatim* restore with **no
phase-boundary arithmetic**, so a snapshot would restore the exact node position
cleanly if one were present. The bug is purely that the checkpoint never reaches
shared storage, so cross-pod recovery has nothing to restore *from*.

## Why this is broad — every cross-pod resume hits it

Cross-pod resume is not a rare edge case in a multi-pod K8s deployment. It
happens on:

- **Preemption** — graceful pause clears `assigned_agent_id`; the next resume
  lands on whatever agent is free (the loop incident).
- **Orphan recovery** — an agent that misses heartbeats for 3 min is marked
  offline; `recover_orphaned_jobs` (`orchestrator/database/postgres.py:2405-2463`)
  pauses its `processing` jobs for re-dispatch, almost always onto a different
  pod.
- **Pod eviction** — node drain, autoscaler scale-down, OOMKill.
- **Rolling deploys / agent SHA bumps** — the dispatcher filters out stale-SHA
  agents, so an in-flight job during a deploy is *forced* to move to a new-SHA
  pod.
- **Any manual pause→resume** that happens to land elsewhere.

In all of these, the job cold-starts its graph.

## What survives vs. what's lost

- **Survives** (on the shared SSH workspace): `plan.md`, `todos.yaml`,
  `archive/phase_N_*.yaml`, `notes/`, the project KB and RecallStore (DB-backed),
  and any files the job wrote. The fresh `init_workspace` re-reads these.
- **Lost** (pod-local checkpoint only): the LangGraph state — full message
  history, `phase_number`/`iteration`/`is_strategic_phase`, in-graph
  `todos`/`staged_todos`, `freeze_data`, and the exact node position. The graph
  re-plans from the surviving files instead of resuming.

So it is a **graph cold-start**, not catastrophic data loss — but for
long-horizon, unattended jobs it is a major progress + token regression, it
re-runs the opening (re-reading the brief, re-issuing early tool calls), and it
can **duplicate non-idempotent early side effects** (file creation, external
calls, git commits). KB writes happen to be idempotent (upsert by slug); nothing
guarantees that for other actions.

## Recommended fix: shared Postgres checkpointer (Strategy B)

Make the checkpoint shared **by construction** — swap the pod-local
`AsyncSqliteSaver` for `AsyncPostgresSaver` so graph state lives in shared
Postgres. The **existing** `_resume_from_checkpoint` path then reads it on any
pod, so cross-pod resume "just works" for *every* move (preemption, eviction,
rolling deploy, orphan-recovery, mid-phase, pre-boundary) from the actual
interruption point — no snapshot bridge, no snapshot-on-pause, no SFTP push/pull.

**Why this is the proper fix, not a patch:**

- The pod-local SQLite saver is LangGraph's single-process/dev option; the PG
  saver is the one built for multi-instance, relocatable runs. This uses the
  mechanism the framework already ships — and this repo already vendors:
  `langgraph-checkpoint-postgres` is in `requirements.txt`.
- It's a clean, reversible seam: the graph compiles with any `BaseCheckpointSaver`
  (`src/graph.py:4175`); the only construction site is `src/agent.py:761`
  (`AsyncSqliteSaver(wrapped_conn)`), and the worker agent already holds a PG
  connection (`src/agent.py:592-599`).

**What the swap entails:**

1. Build an `AsyncPostgresSaver` against a shared checkpoint store and pass it to
   the graph instead of the SQLite saver (`src/agent.py:761`); run `.setup()` once.
2. Resume simplifies — `_resume_from_checkpoint` now reads shared state and
   succeeds cross-pod, so the `_resume_from_snapshot` fallback +
   `create_initial_state` cold-start become the genuine last resort
   (corrupt/missing checkpoint), not the common cross-pod path.
3. **Retention is mandatory** (see probe): prune checkpoints —
   keep-latest-per-thread + delete-on-job-terminal — so the shared store doesn't
   accumulate. SQLite never needed this because it died with the pod.
4. **Store placement:** a *dedicated* checkpoint store, not the app DB, per the
   split-by-workload-class principle in
   `docs/features/database_architecture.md`, to isolate the per-node write load.
5. Open detail: the workspace suspend→S3→restore path (for paused jobs) interacts
   with workspace-*file* consistency, not the checkpoint — trace it when wiring
   resume, but it does not block the checkpoint swap.

### Probe results (2026-06-25 — `AsyncPostgresSaver` vs `AsyncSqliteSaver`, growing-conversation sim)

Measured the two real costs of B (60 super-steps, ~4KB messages, one thread):

- **Latency — a non-issue.** PG checkpoint writes are **~11ms p50 / 28ms p95 and
  flat as state grows** (1.14× across the run). At ~3 writes/super-step that's
  ~30ms per node — noise against multi-second LLM calls. (SQLite baseline ~0.5ms;
  faster, but both are negligible next to inference.)
- **Storage — the one real cost, and it's bounded.** Both savers write the *full*
  channel state every super-step. With no compaction the probe showed worst-case
  **quadratic** growth (~31MB on PG / ~44MB on SQLite for a 120-message thread; an
  earlier all-identical-bytes payload compressed to 976KB on PG via TOAST — a
  measurement artifact, not the real figure). Two things tame it in production:
  (1) the agent **already compacts** at phase boundaries (`RemoveMessage`), so each
  checkpoint blob is *bounded* → real growth is **linear in super-steps**, not
  quadratic; (2) only the *count* of checkpoint versions accumulates (LangGraph
  keeps all by default) → the retention policy above bounds it.

**Verdict:** latency clears B outright; storage is a managed cost (dedicated store
+ retention), not a blocker. The probe confirmed the prediction rather than
surprising us — the call is locked to B.

### Live dev verification (2026-06-25, shipped behind the flag)

Implemented as Strategy B (factory at `src/agent.py` gated by
`CHECKPOINTER_BACKEND`; retention via `PostgresDB.delete_checkpoint_thread` on
terminal in `update_job_status` + `cancel_job`; schema via the agent's `setup()`).
Flipped to `postgres` on dev (`deployment/values-experimental.yaml`). Verified live:

- **Cross-pod resume ✅ (the headline).** A real worker job (`developer`, thread
  `1859e831…`) wrote 130+ checkpoints to PG; its agent pod was force-killed
  mid-phase-0. The orphan-recovered job re-dispatched to a **different pod**, which
  logged `Resuming job … with existing workspace` / `Checkpointer initialized
  (postgres, thread_id=…)` / `Reloaded 3 strategic todos after resume`, and
  **continued** (checkpoints kept growing; no `No phase snapshots found … starting
  fresh`). The exact mid-phase-0 case that used to cold-start now resumes from PG.
- **Schema auto-create ✅** — the agent's `setup()` created the 4 tables on first job.
- **Retention partial:** non-terminal (`paused`) correctly *preserved* the rows;
  the terminal `cancel` ran the hooked `cancel_job` but **no-op'd** — the long-lived
  **orchestrator pod's `CHECKPOINTER_BACKEND` env was unset** (it predated the
  ConfigMap key and wasn't restarted; agents got it fresh per job). Code is correct
  (unit-verified); this is a deploy gap, not a logic bug.

**Rollout caveat:** the flag only takes effect at pod *start*. Worker agents pick
it up (provisioned per job); the **long-lived orchestrator must be restarted** for
retention to engage, else it silently no-ops (agents write PG checkpoints, the
orchestrator never prunes). Add a Stakater Reloader annotation on the orchestrator
deployment for `srw-config` so ConfigMap flips auto-bounce it; until then,
`kubectl rollout restart deploy/srw-orchestrator` after the flip.

### Fallbacks (only if B proves unviable)

- **(A) Bridge the SQLite checkpoint** *(the original proposal)* — push
  `checkpoint.db` + `metadata.json` to the shared SSH workspace at snapshot time
  (extend `create_snapshot`, add a `create_pause_snapshot` at the graceful-pause
  window `src/api/dual_app.py:519`, passing `thread_id=job_id`), and pull it back
  on resume (`_resume_from_snapshot` hydrates the local snapshot dir first;
  `recover_to_phase` is already a verbatim restore). Self-contained and keeps
  SQLite as the hot path, but adds SFTP transport + atomicity (temp-write+rename)
  + a snapshot-on-pause special case, and only covers phase-boundary +
  graceful-pause moves — a mid-phase crash/eviction still restarts from the last
  boundary. Keep only if the PG saver's write load proves unacceptable.
- **(B) RWX checkpoint volume** — a ReadWriteMany PVC shared across pods. Needs
  RWX storage (NFS/CephFS) and risks sqlite corruption under concurrent access
  (old pod finishing a node while the new pod resumes). Not preferred.
- **(C) Grace period — don't preempt before a recovery point exists.** A
  complementary guard at most (and only meaningful once a fix makes recovery
  points cross-pod), never a standalone fix.

## Verification

**Blast radius — confirmed (2026-06-25).** Two independent lines, no live
relocation needed:

- *Empirical:* job `1b099a61` cold-started across three pods → the checkpoint
  storage is provably not shared between agent pods (a shared store would have let
  pod 2 resume pod 1's checkpoint).
- *Code:* `create_snapshot` copies `checkpoint.db` only into the pod-local
  snapshot dir and never pushes it to the shared workspace
  (`src/core/phase_snapshot.py:235-278`), so a post-boundary snapshot is as
  pod-local as the live checkpoint → post-boundary cross-pod resume cold-starts
  too.

**After the fix (Strategy B):**

1. Take a job across **≥1 phase boundary**, force a cross-pod resume (preempt, or
   delete its pod). Assert it resumes from the shared PG checkpoint — single
   `initialize`, monotonic `iteration`, preserved `phase_number`, brief not
   re-read, **no** `create_initial_state` reset / `No phase snapshots found` line.
2. Preempt a job **during phase 0** (pre-boundary): it must *also* resume from PG,
   not cold-start — directly retiring the preemption doc's symptom.
3. Retention holds: after a job reaches a terminal state, its checkpoints are
   pruned from the shared store (no unbounded accumulation across jobs).

## Evidence (dev, 2026-06-24)

- Job `1b099a61` cold-started on three pods in succession, never recovering state
  cross-pod (preemption doc, Evidence section). Tool-set diff across the restarts
  (35 tools / no KB vs. 45 tools / KB present) confirms each run re-resolved the
  graph from `create_initial_state`.
- Code (read 2026-06-24): `src/core/phase_snapshot.py:236-247` (local
  `shutil.copy2` of `checkpoint.db`), `:260-272` (only workspace files pulled from
  remote, checkpoint never pushed back), `:110-121` (snapshot path pod-local);
  `src/core/workspace.py:89-138` (checkpoint base path pod-local);
  `src/agent.py:3010-3018` (no-snapshot cold start); `src/core/state.py:136-195`
  (cold reset).
- Probe (2026-06-25, throwaway local `AsyncPostgresSaver` vs `AsyncSqliteSaver`,
  60-super-step growing thread): PG checkpoint writes ~11ms p50 / 28ms p95, flat
  as state grows (1.14×); both savers store full channel state per super-step
  (~31MB PG / ~44MB SQLite for a 120-message thread, no compaction) → retention
  required. Latency is an upper bound (in-cluster PG shares the agent's LAN);
  `langgraph-checkpoint-postgres` confirmed already in `requirements.txt`.

## Related

- [`preemption_before_first_checkpoint_replays_job_opening.md`](preemption_before_first_checkpoint_replays_job_opening.md)
  — the trigger; its D3 fix is specified here (shared PG checkpointer). D1+D2 in
  that doc stop the *parasitic* preemption; this doc is what makes *any*
  preemption (or pod move) non-destructive.
- `docs/features/project_self_improvement_loop.md` — the feature whose first real
  run surfaced both bugs.
