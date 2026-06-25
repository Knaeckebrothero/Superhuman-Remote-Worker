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
preemption doc's "D3 / snapshot-on-pause" fix lands here.

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

Inferred (post-boundary): because the snapshot directory is pod-local (see Root
cause), a job preempted *after* completing a phase would also cold-start on a new
pod — losing every completed phase's graph state, not just the opening. This is
the broader blast radius and the one thing this doc flags for a confirming repro
(see Verification).

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

## Proposed fix

**Core: replicate the checkpoint to shared storage at snapshot time, pull it
back at resume time.** This finishes the bridge that facts 2–3 left half-built,
without changing the verbatim-restore logic.

1. **Write side.** Extend `create_snapshot` (and add a `create_pause_snapshot`)
   to **push** the snapshot's `checkpoint.db` + `metadata.json` to the **shared
   SSH workspace** via the workspace backend (`backend.write_file` / SFTP), at a
   deterministic job+phase path (e.g.
   `<shared-workspace>/.srw/phase_snapshots/job_<id>/phase_<n>/`). WAL-flush
   first (already done at `phase_snapshot.py:235-247`). **Pass `thread_id=job_id`**
   — the current `archive_phase` caller (`src/graph.py:2412-2419`) doesn't, so
   snapshots store `thread_id=None` and lean on `discover_thread_id_from_checkpoint`.
2. **Read side.** Extend `get_latest_snapshot` / `recover_to_phase` so that when
   no pod-local snapshot exists, it **lists + pulls** the snapshot from the
   shared workspace before restoring. `_resume_from_snapshot` then needs no logic
   change — it hydrates the local snapshot dir from shared storage, then restores
   as today.
3. **Snapshot-on-pause** (builds on #1 — this is the preemption doc's "D3").
   The preemption pause is a **graceful, cooperative node-boundary stop** (HTTP
   `/job/pause` at `src/api/dual_app.py:821-837` → cooperative-stop Event → the
   `astream` loop breaks at `src/api/dual_app.py:502-523`), so there is a real
   code window. At `src/api/dual_app.py:519` (`reason == "pause"`, before
   `_complete_stop`), call `create_pause_snapshot` so even a job preempted *during
   phase 0* — before any boundary — has a cross-pod recovery point. At that point
   `checkpoint.db` is fully persisted (the last completed node wrote it) and the
   live SSH `WorkspaceManager` + `snapshot_manager` are still in scope.

### Alternatives considered

- **(A) Shared RWX checkpoint volume** — mount a ReadWriteMany PVC for
  `/workspace/checkpoints` so all pods see the same dir. Sidesteps push/pull but
  needs RWX storage (NFS/CephFS) and risks sqlite corruption from concurrent
  access (old pod finishing a node while the new pod resumes). Possible future
  infra option, not the default.
- **(B) Put the live checkpoint directly on the shared SSH workspace** — change
  `get_checkpoints_path()` to the shared volume. But the checkpoint is rewritten
  every node; sqlite-over-SFTP would be slow and fragile. This is almost
  certainly *why* it's pod-local today: pod-local for write performance, with
  snapshots meant as the durability bridge — except the snapshot half was never
  wired to push the checkpoint. Rejected; the core fix keeps the performance
  design and just completes the bridge.
- **(C) Grace period — don't preempt before a recovery point exists.** The
  preemption doc's stopgap. Only meaningful *after* #1+#2, because today even a
  post-boundary snapshot doesn't bridge pods. Useful as a complementary guard
  once the core fix lands, not a substitute.

## Verification

**Confirm the blast radius first** (the post-boundary case is inferred from code,
not yet observed):

1. Start a job; let it cross **≥1 phase boundary** (confirm a `phase_N/` snapshot
   dir exists on its pod). Force a cross-pod resume (preempt it, or delete its
   pod). Assert whether it cold-starts (look for `No phase snapshots found …
   starting fresh` on the **new** pod and a `phase_number`/iteration reset) or
   resumes. If it cold-starts, the post-boundary blast radius is confirmed.
2. Inspect storage: on an agent pod, confirm `/workspace/checkpoints/` and the
   snapshot dir are pod-local (not a shared RWX mount) via `kubectl describe pod`
   volumes; and confirm the **shared SSH workspace** contains no `checkpoint.db`
   / `phase_snapshots/` today.

**After the fix:**

3. Repeat (1): the new pod must find the pushed snapshot, pull it, and resume
   **without** a `create_initial_state` reset — single `initialize`, monotonic
   `iteration`, preserved `phase_number`, brief not re-read.
4. Preempt a job **during phase 0** (snapshot-on-pause): it must resume from the
   pause snapshot on a different pod rather than cold-starting — directly retiring
   the preemption doc's symptom.

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

## Related

- [`preemption_before_first_checkpoint_replays_job_opening.md`](preemption_before_first_checkpoint_replays_job_opening.md)
  — the trigger; its D3 / snapshot-on-pause fix is implemented here. D1+D2 in
  that doc stop the *parasitic* preemption; this doc is what makes *any*
  preemption (or pod move) non-destructive.
- `docs/features/project_self_improvement_loop.md` — the feature whose first real
  run surfaced both bugs.
