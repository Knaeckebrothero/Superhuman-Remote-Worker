---
tags:
  - issue
  - jobs
  - dispatcher
  - preemption
  - checkpoint-resume
  - agent-resilience
  - project-loop
  - lifecycle-reaper
---

# Preemption during a job's first phase replays the opening (job "starts over" 2–3×)

**Filed:** 2026-06-24, found on the first real dev-cluster run of the
project self-improvement loop (`docs/features/project_self_improvement_loop.md`).

## Symptom

A job's **opening is executed multiple times** — it re-reads its task brief,
re-runs its first strategic LLM calls, and re-writes its first knowledge-base
notes, before finally running through to completion. In the cockpit this looks
like "prompt duplication" / the job visibly starting over. It is **not** a UI
artifact — the agent genuinely re-initialized the graph from scratch.

Concrete incident — loop `27cabc53` scholar job
`1b099a61-b4e0-4368-982c-5aaa094c5363` (model `gpt-5.5`):

| # | `init_workspace` at | Ran on agent | Outcome |
|---|---|---|---|
| 1 | 07:58:20 | `55f440ed` | 1 LLM call, then **preempted** 07:58:45 |
| 2 | 07:59:16 | `d13800ec` | 3 LLM calls (wrote 4 KB notes), then **preempted** 08:01:01 |
| 3 | 08:01:39 | `4830ffb1` | ran uninterrupted → completed 08:30 |

Three `init_workspace` runs, the graph's internal iteration counter reset to 0
each time, the same `task_brief.md` re-read, and the same 4 KB notes re-written.
Job elapsed wall-clock: **39m 44s** for what is a single scholar step. (The
duplicate KB writes happened to collapse to 4 notes because `kb_write` upserts
by slug — luck, not a guarantee.)

## Root cause

Three independent facts combine. Remove any one and the duplication disappears.

### 1. Preemption detaches the job from its pod (orchestrator)

A higher-priority *pending* job preempts the lowest-priority *running* job in the
dispatcher (`orchestrator/main.py:4033-4063`; strictly-higher-priority rule at
`:4043`, fire-and-forget `_initiate_pause` at `:4055`). Pause sets the job to
`status='paused'` and **clears `assigned_agent_id`**, returning it to the
dispatchable pool. The next resume (`main.py:2336` "Dispatch: resumed job … on
agent") therefore lands on **whatever agent is free — a different pod**
(`55f440ed → d13800ec → 4830ffb1` above).

### 2. The LangGraph checkpoint is pod-local (agent)

The checkpoint DB lives at `/workspace/checkpoints/job_<id>.db` on the agent
**pod's own volume** — `get_workspace_base_path()` / `get_checkpoints_path()` in
`src/core/workspace.py:89-138` is explicitly *"the agent process's own state
directory … NOT the job workspace."* It is created per-job in
`src/agent.py:717-723`. A fresh pod has no copy of the previous pod's checkpoint.

### 3. The only cross-pod recovery point is a phase snapshot (agent)

Because checkpoints are pod-local, the agent copies the checkpoint into the
**shared** SSH workspace as a *phase snapshot* — but only **at phase
boundaries** (`src/core/phase_snapshot.py`, written by the `archive_phase`
node). The resume path (`src/agent.py:761-826`) for a graceful `paused` resume
first tries the local `checkpoint.db` (`_resume_from_checkpoint`), then falls
back to snapshot recovery (`_resume_from_snapshot`).

### The intersection

The job was preempted **31 s in, during the *initial* strategic phase (phase 0),
before it ever crossed a phase boundary.** At resume on a new pod, *both*
recovery sources were empty: the local `checkpoint.db` was on the now-detached
pod, and `get_latest_snapshot()` returned nothing (no phase had completed). So
`_resume_from_snapshot` hit its no-snapshot branch and cold-started
(`src/agent.py:2909-2917`):

```python
latest_snapshot = snapshot_manager.get_latest_snapshot()
if not latest_snapshot:
    logger.warning(f"No phase snapshots found for job {job_id}, starting fresh")
    graph_input = create_initial_state(...)   # ← re-enters init_workspace, iteration 0
    return graph_input, thread_id, thread_config
```

Both preempts landed before the first phase boundary, so the opening cold-started
**3 times**. **The crux:** the "graceful resume uses the local checkpoint" fast
path (`src/agent.py:774`) silently assumes the job resumes *on the same pod* —
preemption breaks that assumption, and there is no phase snapshot to bridge the
gap until the job has finished at least one phase.

### Contributing defects (each independently fixable)

- **D1 — Parasitic preemption.** The preemptor in this incident,
  `e92fcfcc` (priority 10), is a **stale `critic` verification job from
  2026-06-17, `status='paused'`, `assigned_agent_id IS NULL`**. The dispatcher
  treats any `status IN ('created','paused') AND assigned_agent_id IS NULL` job
  as "pending," so a job that **can never actually be placed** still preempts
  running jobs every cycle, forever, without making progress. The preemption
  loop never checks that the pending job is placeable.
- **D2 — Zombie accumulation (already tracked).** There are **11** such
  priority-10 paused `critic` jobs piled up 06-17 → 06-23 — orphaned
  verification subjobs that never got reaped. This is the upstream source of the
  preemptors. See
  [`critic_failure_leaves_parent_job_stuck_reviewing.md`](critic_failure_leaves_parent_job_stuck_reviewing.md).
- **D3 — Cold-restart on early-phase cross-pod resume.** The intersection
  described above (the agent/orchestrator side of the bug). A later code read
  found this is broader than "early-phase": the checkpoint is *never* replicated
  to shared storage, so **any** cross-pod resume cold-starts, even after a phase
  boundary. Tracked separately in
  [`cross_pod_resume_cold_starts_checkpoint_not_replicated.md`](cross_pod_resume_cold_starts_checkpoint_not_replicated.md).
- **A1 — Loop jobs are low priority.** Loop jobs are created at **priority 5**
  (`orchestrator/services/project_loops.py` `create_loop_job` →
  `create_job(..., priority=5)`; `postgres.py:822` default is also 5), making
  them the preferred preemption victims for *any* priority-10 job, legit or
  zombie.

## Effects

- **Wasted tokens and wall-clock** — the entire opening (system prompt + brief +
  first strategic turns + early tool calls) is paid for N+1 times. A 1-step
  scholar job took ~40 min and several redundant `gpt-5.5` calls.
- **Duplicated side effects** — early KB writes ran twice. Idempotent here only
  because `kb_write` upserts by slug; a non-idempotent early action (file
  creation, external call, git commit) would duplicate for real.
- **Breaks unattended loop runs.** With 11 zombie preemptors lurking, *every*
  loop job gets thrashed at its vulnerable start whenever there is any capacity
  contention — the exact scenario an overnight self-improvement loop runs in.
- **Misleading observability** — the run looks confused/duplicative to the user
  even though the loop's own logic is correct (it advanced scholar→critic fine).

## Proposed fix

Ordered by leverage. Immediate ops step unblocks current runs; the rest are
code changes that need a redeploy.

1. **Reap the zombie preemptors now (ops, no deploy).** Cancel the 11
   priority-10 paused `critic` jobs (06-17 → 06-23) so they stop preempting.
   This is a symptom clear, not a fix.
2. **D2 — Reap stale verification subjobs at the source.** A lifecycle sweep
   that fails/cancels `critic` (verification) jobs that are `paused` with no
   agent and whose parent is terminal, or that have been paused+agentless for
   more than N hours. Folds into
   [`critic_failure_leaves_parent_job_stuck_reviewing.md`](critic_failure_leaves_parent_job_stuck_reviewing.md).
   Removing the zombies removes the preemptors.
3. **D1 — Only let *placeable* jobs preempt.** In the dispatcher
   (`main.py:4033-4063`), a pending job must not preempt unless it would
   actually be dispatched/resumed given a freed slot. Cheapest guard: exclude
   any job from the preemptor set that already failed to place in Phase 1/1.5
   this cycle, or that has been `paused`+agentless beyond a staleness threshold.
   A job that can never run is not a live priority.
4. **D3 — Make resume survive a pod move.** This is bigger than first scoped and
   is now tracked in its own doc,
   [`cross_pod_resume_cold_starts_checkpoint_not_replicated.md`](cross_pod_resume_cold_starts_checkpoint_not_replicated.md):
   the checkpoint is never pushed to shared storage (not even at phase
   boundaries, contrary to the assumption above), so the real fix is to
   replicate `checkpoint.db` to the shared workspace at snapshot time + pull it
   back on resume, then add **snapshot-on-pause** (push the checkpoint at the
   graceful-pause point) so a job preempted mid-phase-0 has a recovery point. A
   **grace period** (don't preempt before a recovery point exists) is only a
   useful complement once that replication works.
5. **A1 — Loop-job priority/idempotency.** Make loop-job priority configurable
   (and default it above 5 for dedicated runs) so a loop isn't trivially
   preempted; keep early-phase actions idempotent (KB writes already are).

## Verification (when fixed)

- Start a low-priority job; while it is in its **first** strategic phase, submit
  a higher-priority job to force preemption. Assert the low-priority job resumes
  **without** a second `init_workspace` / iteration reset and **without**
  re-reading its brief or re-writing opening notes (check the audit trail for a
  single `initialize` and a monotonic iteration counter).
- Assert a stale `paused` + agentless job no longer appears in
  `Preempt: pausing …` log lines.

## Evidence (dev, 2026-06-24)

Orchestrator log (`srw-orchestrator`):
```
07:58:14  Dispatch: assigned job 1b099a61 → agent 55f440ed
07:58:45  Preempt: pausing job 1b099a61 (priority=5) for pending job e92fcfcc (priority=10)
07:59:15  Dispatch: resumed job 1b099a61 → agent d13800ec
08:01:01  Preempt: pausing job 1b099a61 (priority=5) for pending job e92fcfcc (priority=10)
08:01:37  Dispatch: resumed job 1b099a61 → agent 4830ffb1   (→ completed 08:30)
```

Tool-set / instruction-hierarchy diff across the cold restarts (from the audited
LLM requests) — a tell that each run re-resolved the graph from scratch:

| Run | Tools bound at iter 0 | KB tools | Instruction hierarchy |
|---|---|---|---|
| 1 (`doc 3820`) | 35 | absent | `… > memory > …` |
| 3 (`doc 3825`) | 45 | present (`kb_*`) | `… > knowledge base > …` |

`e92fcfcc` = `critic`, priority 10, `status=paused`, created 2026-06-17 —
"Verify deliverables of job a22625fa (bughunter)." One of **11** such stale
priority-10 paused critic jobs in the dispatchable set (06-17 → 06-23).

**Update (2026-06-24, full-cycle review):** the cold-restart was **broader than
this incident** — it also hit **job 2 (the critic, `7a777bb0`)**: 4 cold starts,
preempted **3× within 2 minutes** during its phase-0 restart (on top of job 1's 3
starts) — and it **failed an unrelated bystander scholar job** (`a3edd26e`,
preempted 3× → `status=failed`). Of the ~10–11 armed zombies, only the oldest
(`e92fcfcc`) fired in this window; the rest are dormant-but-armed and will hit
future low-priority jobs whenever the dispatcher selects them. Both loop jobs
still completed legitimately (confidence 0.88 / 0.95), confirming the impact is
wasted compute + latency, not correctness.

## Related

- [`cross_pod_resume_cold_starts_checkpoint_not_replicated.md`](cross_pod_resume_cold_starts_checkpoint_not_replicated.md) — the deeper defect under D3: the checkpoint is never replicated to shared storage, so any cross-pod resume cold-starts. The robust fix for this incident's restarts lives there.
- [`critic_failure_leaves_parent_job_stuck_reviewing.md`](critic_failure_leaves_parent_job_stuck_reviewing.md) — the upstream source of the zombie preemptors.
- [`lifecycle_session_agents_without_thread_never_drain.md`](lifecycle_session_agents_without_thread_never_drain.md) — sibling lifecycle-reaper gap.
- `docs/features/project_self_improvement_loop.md` — the feature whose first real run surfaced this.
