---
tags:
  - feature
  - orchestration
  - scheduling
aliases:
  - auto-assignment
  - job dispatcher
  - automatic scheduling
  - job queue
  - preemptive scheduling
related:
  - "[[verification_phase]]"
  - "[[autonomy_levels]]"
---

# Job Auto-Assignment — Priority Queue with Preemptive Scheduling

> Automatic dispatch of queued jobs to available agents, with priority-based preemption: high-priority jobs pause low-priority ones, and paused jobs resume when agents become free. Agents are stateless — any agent can run any job configuration.

## Problem

### What happens today

```
User creates job via cockpit → status: "created" → sits idle
    ↓
User manually selects a ready agent → clicks "Assign"
    ↓
Orchestrator sends job to agent pod → status: "processing"
```

Job assignment is fully manual. A human must open the cockpit, notice a job is waiting, check which agents are available, and click assign. This doesn't scale — especially with verification jobs that spawn automatically and need immediate pickup.

Cancellation is also blunt: the only way to stop a running job is to cancel it, which is effectively a kill. There's no way to pause a job, free the agent for higher-priority work, and resume later. The cancel flow also has gaps — it doesn't clear `assigned_agent_id` on the job or update the agent record in the database, relying entirely on the next heartbeat cycle (up to 60s) to correct the stale state.

### What we want

```
User creates job → status: "created"
    ↓
Dispatcher runs → finds ready agent → assigns automatically
    ↓
No agent available? → check running jobs for preemption candidates
    ↓
Low-priority job running? → pause it (checkpoint saved, agent freed)
    ↓
Agent picks up high-priority job → works to completion
    ↓
Agent free again → dispatcher resumes paused job from checkpoint
```

Low-priority jobs serve as **backfill work** — agents always have something to do, but they yield immediately when urgent work arrives. This maximizes agent utilization while ensuring high-priority jobs get prompt attention.

## Design

### Agent Model

Agents are **stateless and generic**. Any agent pod can run any job configuration. The job carries its own `config_name` (creator, critic, validator, etc.) — the agent receives this at assignment time and configures itself accordingly. Agent `config_name` at registration is informational only and not used for matching.

### Priority Levels

Jobs are dispatched in priority order, FIFO within the same priority level:

```sql
ORDER BY priority DESC, created_at ASC
```

New column on the `jobs` table:

```sql
priority INTEGER DEFAULT 5
```

| Label  | Value | Use case |
|--------|-------|----------|
| Low    | 0     | Background/backfill work (preemptible) |
| Normal | 5     | Standard user-created jobs (default) |
| High   | 10    | Urgent work, verification/critic jobs |

Verification jobs created by `_maybe_trigger_verification()` default to priority 10. The cockpit job creation form exposes a priority dropdown.

### Job States

New `paused` status added to the job lifecycle:

```
created ──────→ processing ──→ pending_review ──→ completed
                  │  ↑  ↑
                  │  │  └── (human/critic resumes)
                  │  │
                  │  └───── (dispatcher auto-resumes)
                  ↓
               paused ────→ cancelled
                  ↑
                  │
                  └── (preempted by higher priority, or manual pause)

processing ──→ failed ──→ processing (manual resume)
                            ↓
                         completed
```

Key transitions:
- `processing → paused` — Graceful pause (preemption or manual). Checkpoint saved, agent freed.
- `paused → processing` — Auto-resumed by dispatcher (or manual unpause). Loads checkpoint, continues on any available agent.
- `paused → cancelled` — User decides to abandon the paused job.

**`paused`** is distinct from `cancelled` and `failed`:
- `paused` — Gracefully stopped by the scheduler or user. Checkpoint saved. Will be auto-resumed when an agent is available and no higher-priority work is waiting.
- `cancelled` — Killed by user. Terminal state (unless manually restarted).
- `failed` — Crashed. Requires manual intervention to resume.

Paused jobs re-enter the dispatch queue and compete for agents alongside `created` jobs, but they use the **resume** path (load checkpoint, continue from last completed node) instead of starting fresh.

### Pause Mechanism

Pausing a job requires a **graceful stop** — the agent must finish its current tool call and save state before releasing the job. An abrupt `CancelledError` (as the current cancel does) could lose in-progress work within a node.

**New agent endpoint: `POST /job/pause`**

```python
@app.post("/job/pause")
async def pause_current_job(request: JobPauseRequest):
    """Gracefully pause the current job.

    Sets a cooperative flag that the graph checks between node executions.
    The agent finishes its current node, LangGraph saves the checkpoint,
    then processing stops and the agent becomes available.
    """
    if _current_job_id is None:
        raise HTTPException(404, "No job currently running")

    _pause_requested.set()  # asyncio.Event — checked between iterations
    # Wait for the job to actually stop (with timeout)
    await asyncio.wait_for(_job_paused.wait(), timeout=120)
    return {"status": "paused", "job_id": _current_job_id}
```

**Graph-level cooperative check:**

LangGraph saves checkpoints after every node execution. Between nodes, the streaming loop checks the pause flag. If set, it exits the loop cleanly — the checkpoint already contains the last completed node's state.

The check lives in `_process_orchestrator_job()`:

```python
async for state in streaming_gen:
    final_state = state
    if _pause_requested.is_set():
        # Checkpoint already saved by LangGraph after the completed node
        # Exit the loop — state is safely persisted
        break

# After the loop:
if _pause_requested.is_set():
    _pause_requested.clear()
    await _update_job_status("paused")   # Not failed, not cancelled
    _current_job_id = None
    _job_paused.set()                    # Signal the /pause endpoint
```

**Why not reuse cancel?** Cancel raises `CancelledError` which can interrupt mid-tool-call. Pause is cooperative — it waits for the current node to finish, ensuring the checkpoint captures a complete state. This makes resume reliable. (As a side improvement, we should also fix cancel to clear `assigned_agent_id` and update the agent record in the DB, rather than relying on the next heartbeat.)

### Preemptive Scheduling

When a high-priority job is created but no agents are free, the dispatcher checks if any running jobs can be preempted.

**Preemption rules:**
1. Only preempt if the pending job's priority is **strictly higher** than the running job's priority (prevents thrashing between equal-priority jobs)
2. Preempt the **lowest-priority running job** first
3. If multiple running jobs share the lowest priority, preempt the one that started most recently (least work lost)

**Two-phase preemption:**

Preemption is **non-blocking** to avoid holding the dispatcher lock during the up-to-120s graceful pause. It uses a two-phase approach:

```
Phase 1 — Initiate pause (non-blocking):
    Dispatcher identifies the preemption candidate
    Sends POST /job/pause to the agent (fire-and-forget via asyncio.create_task)
    Marks job as "pause_pending" in memory (not a DB status — just a dispatcher-internal flag
    to avoid re-preempting the same job on the next cycle)
    Releases the lock immediately

Phase 2 — Dispatch on next cycle:
    Agent completes the pause → heartbeat reports "ready"
    Job status in DB: "paused", assigned_agent_id cleared
    Next dispatcher cycle (trigger #2 or #4) sees:
      - A free agent (the one that just finished pausing)
      - The high-priority pending job still in the queue
    Normal dispatch matches them
```

This means preemption adds a delay of one dispatcher cycle (up to 30s from the background sweep, or immediate from the heartbeat trigger). This is acceptable — the alternative (holding the lock for 120s) would block all other dispatch operations.

**What about cascading preemption?** A priority-10 job preempts a priority-0 job. Then a priority-5 job arrives — does it preempt the priority-10? No, because rule #1 requires **strictly higher** priority. The priority-5 job queues until an agent is free.

### Cooldown

When an agent finishes a job (completes normally, not paused), it gets a **30-second cooldown** before receiving the next one. This gives the agent time to clean up resources (temp files, connections, memory).

New column on the `agents` table:

```sql
last_completed_at TIMESTAMP WITH TIME ZONE
```

Set when the heartbeat detects a `working → ready` transition after a job completes or fails. **Not** set after a pause — paused jobs don't trigger cooldown since the agent was interrupted, not finished. The dispatcher filters:

```sql
status = 'ready'
AND (last_completed_at IS NULL OR NOW() - last_completed_at >= INTERVAL '30 seconds')
```

**Exception:** Cooldown is bypassed when the dispatcher is matching a preemption-freed agent to the high-priority job that triggered it. The entire point of preemption is urgent dispatch — adding a cooldown would defeat it. In practice this happens naturally: the agent doesn't go through a `completed` state after pause, so `last_completed_at` isn't set, and the cooldown filter passes.

### Dispatcher Function

Core function: `_try_dispatch_pending_jobs()`

```
acquire asyncio.Lock
  if AUTO_ASSIGN_ENABLED is false → return

  # Dispatchable jobs: created (new) + paused (preempted), ordered by priority
  pending_jobs = query jobs WHERE status IN ('created', 'paused')
                   AND assigned_agent_id IS NULL
                   ORDER BY priority DESC, created_at ASC

  available_agents = query agents WHERE status='ready'
                       AND cooldown expired

  # Phase 1: Direct assignment (free agents → highest priority pending jobs)
  for (job, agent) in zip(pending_jobs, available_agents):
      if job.status == 'paused':
          success = resume_job_on_agent(job, agent)
      else:
          success = dispatch_new_job_to_agent(job, agent)
      if success:
          mark both as matched
  remove matched jobs/agents from lists

  # Phase 2: Preemption (remaining high-priority jobs → lowest-priority running jobs)
  for pending_job in remaining_pending:
      running = get_running_jobs_by_priority_asc()
      candidate = first running job where candidate.priority < pending_job.priority
      if candidate and candidate not already pause_pending:
          initiate_pause(candidate)   # fire-and-forget, non-blocking
          # Actual dispatch happens on next cycle when agent reports ready

release lock
```

Protected by an `asyncio.Lock` to prevent double-assignment when multiple triggers fire simultaneously.

### Trigger Points

The dispatcher is called from four places:

| # | Trigger | Location | Why |
|---|---------|----------|-----|
| 1 | Job created | End of `POST /api/jobs` | New job might have a waiting agent (or trigger preemption) |
| 2 | Agent becomes ready | `POST /api/agents/{id}/heartbeat` when status transitions to `ready` | Agent just freed up, check for queued/paused jobs |
| 3 | Job status change | `POST /api/jobs/{id}/approve` and `POST /api/jobs/{id}/cancel` | Agent about to be free, check queue early |
| 4 | Background sweep | `auto_assign_dispatcher()` task, 30s loop | Catch-all for edge cases, stale recovery |

Triggers 1-3 are **fire-and-forget** (`asyncio.create_task`) — they don't block the API response. Trigger 4 is a background loop identical in structure to `stale_agent_detector()`.

### Resuming Paused Jobs

When the dispatcher picks a paused job, it uses the **resume path** instead of the start path:

1. Dispatcher calls `POST /job/resume` on the agent pod (not `/job/start`)
2. Agent loads checkpoint from `workspace/checkpoints/job_<id>.db`
3. LangGraph continues from the last completed node
4. Job status updated: `paused → processing`, `assigned_agent_id` set to the new agent

The existing `POST /api/jobs/{id}/resume` orchestrator endpoint currently only accepts `failed` jobs. This needs to be extended to also accept `paused` status.

Paused jobs compete in the same priority queue as new jobs. A paused priority-5 job resumes before a new priority-0 job starts (higher priority wins; within the same priority, `created_at` breaks ties — and paused jobs were created earlier).

**Manual unpause:** Users can also manually trigger resume of a paused job via the cockpit, which calls the same resume endpoint. This bypasses the dispatcher's priority ordering — if the user wants to force-resume a paused job, they can.

### Refactoring the Assignment Logic

The existing `assign_job_to_agent()` endpoint (lines 1811-1972 in `main.py`) contains the full assignment logic: datasource resolution, config overrides, HTTP POST to agent pod, status updates. This gets extracted into reusable internal functions:

```python
async def _dispatch_job_to_agent(job: dict, agent: dict) -> bool:
    """Start a new job on an agent. Returns True on success, False on failure.

    Extracted from assign_job_to_agent() endpoint:
    - Resolve datasources (job > project > global)
    - Build config_override with datasource-driven tool injection
    - Resolve project repositories and git remote URL
    - Build JobStartRequest
    - POST to agent /job/start
    - Update job status → processing, set assigned_agent_id
    - Update agent status → working via heartbeat simulation
    """

async def _resume_job_on_agent(job: dict, agent: dict) -> bool:
    """Resume a paused/failed job on an agent. Returns True on success.

    - Re-resolve datasources (in case they changed)
    - POST to agent /job/resume (with job_id, config_name, feedback if any)
    - Update job status: paused → processing, set assigned_agent_id
    - Update agent status → working
    """

async def _initiate_pause(job: dict) -> None:
    """Request graceful pause of a running job. Non-blocking (fire-and-forget).

    - Look up assigned agent
    - POST to agent /job/pause (don't await completion — agent reports ready via heartbeat)
    - Mark job as pause-pending in dispatcher state (prevent re-preemption)
    """
```

The HTTP endpoint `POST /api/jobs/{id}/assign/{agent_id}` becomes a thin wrapper around `_dispatch_job_to_agent()`.

### Toggle

Environment variable on the orchestrator:

```
AUTO_ASSIGN_ENABLED=true   # default: true
```

When `false`, the system behaves exactly as today — manual assignment only. The background dispatcher loop still runs but short-circuits immediately. Preemption is also disabled.

Optional: expose `GET/PUT /api/settings/auto-assign` for runtime toggling from the cockpit without restarting the orchestrator.

## Implementation

### Phase 1: Pause Infrastructure

Add the pause mechanism before building the dispatcher, since the dispatcher depends on it.

**Schema** (`orchestrator/database/schema.sql`):
- Add `paused` to job status CHECK constraint
- Add `priority INTEGER DEFAULT 5` to `jobs` table
- Add `last_completed_at TIMESTAMP WITH TIME ZONE` to `agents` table
- Update `job_summary` view to include `priority`

**Agent endpoint** (`src/api/app.py`):
- Add `_pause_requested` and `_job_paused` as `asyncio.Event` module globals
- Add `POST /job/pause` endpoint (sets flag, waits for confirmation with 120s timeout)
- Modify `_process_orchestrator_job()` streaming loop to check `_pause_requested` between iterations
- On pause: update job status to `paused`, clear `_current_job_id`, signal `_job_paused`
- Reset both events at the start of each new job

**Database** (`orchestrator/database/postgres.py`):
- Add `pause_job(job_id)` method — sets status to `paused`, clears `assigned_agent_id`
- Update `create_job()` to accept `priority` parameter
- Fix `cancel_job()` to also clear `assigned_agent_id` (existing bug)

**Orchestrator** (`orchestrator/main.py`):
- Add `POST /api/jobs/{job_id}/pause` endpoint (proxies to agent pod, like cancel does)
- Extend `POST /api/jobs/{job_id}/resume` to accept `paused` status (currently only `failed`)

### Phase 2: Dispatcher Core

**Database methods** (`orchestrator/database/postgres.py`):
- `get_dispatchable_jobs(limit)` — jobs with status IN ('created', 'paused'), `assigned_agent_id IS NULL`, ordered by priority DESC, created_at ASC
- `get_available_agents(limit)` — agents with status='ready' and cooldown filter
- `get_preemption_candidates()` — processing jobs ordered by priority ASC, created_at DESC (lowest priority, most recent first)
- Update `heartbeat()` — detect `working → ready` transition, set `last_completed_at` only when previous heartbeat was `working` (not after pause)

**Orchestrator** (`orchestrator/main.py`):
- Add `AUTO_ASSIGN_ENABLED` env var (default `true`)
- Extract `_dispatch_job_to_agent(job, agent) -> bool` from existing `assign_job_to_agent()` endpoint
- Implement `_resume_job_on_agent(job, agent) -> bool`
- Implement `_initiate_pause(job) -> None` (non-blocking)
- Implement `_try_dispatch_pending_jobs()` with `asyncio.Lock` (direct assignment + preemption)
- Implement `auto_assign_dispatcher()` background task (30s loop)
- Register background task in `lifespan()`
- Hook dispatcher into `create_job()`, `heartbeat()`, `approve_job()`, and `cancel_job()` endpoints
- Add `priority` field to `JobCreate` Pydantic model
- Refactor `assign_job_to_agent()` endpoint to use extracted helper

### Phase 3: Cockpit UI

**Job creation form** (cockpit component):
- Add priority dropdown: Low (0), Normal (5), High (10)
- Default to Normal (5)

**Job list**:
- Show priority badge/indicator on jobs
- Add pause button for running jobs
- Add resume button for paused jobs
- Show `paused` status with distinct styling (e.g., amber/yellow)

### Phase 4: Verification Integration

Update `_maybe_trigger_verification()` in `src/api/app.py` to set `priority: 10` on critic jobs so they get dispatched promptly and preempt backfill work.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| No agents available, no preemption candidates | Job stays `created`/`paused`, picked up by next sweep |
| No agents available, all running jobs have equal or higher priority | No preemption. Job queues until an agent completes its current work |
| Agent goes offline mid-pause | Pause request times out or never reaches agent. Stale detector marks agent offline (3 min). Job stays `processing` with a stale agent — requires manual recovery (cancel + reassign or admin cleanup) |
| Pause during long tool execution (e.g., web search, shell command) | Cooperative flag checked between nodes. Current node runs to completion. If the node takes >120s, the pause endpoint times out — but the flag persists, so the job still pauses after the node finishes. The orchestrator may need to retry or let the background sweep handle it |
| Multiple high-priority jobs, one agent | Highest priority dispatched first. Others queue. After completion + cooldown, next highest is dispatched |
| Equal priority jobs | FIFO by `created_at`. No preemption between them |
| Paused job vs new job, same priority | Paused job has an earlier `created_at`, so it wins FIFO ordering and resumes first |
| Agent finishes high-priority job | Cooldown (30s), then dispatcher checks queue: paused + new jobs by priority |
| Preemption frees agent | No cooldown (agent didn't finish, was interrupted). Next cycle dispatches high-priority job immediately |
| Auto-assign disabled | All dispatching and preemption disabled. Manual assign/pause/resume still work |
| Manual pause via cockpit | Uses same `POST /api/jobs/{id}/pause`. Job enters `paused` status, agent freed. Dispatcher may auto-resume it, or user can manually resume |
| Manual assign while auto-assign on | Works normally — dispatcher skips already-assigned jobs |
| Job cancelled while paused | Allowed. `paused → cancelled` is a valid transition |
| All jobs are low priority (0) | Normal FIFO dispatch, no preemption ever triggers (no higher-priority jobs exist) |
| Cascading preemption | Not possible — rule #1 requires strictly higher priority. A preempted agent runs the high-priority job, which can't be preempted by equal or lower |
| Agent pod restarts while job is paused | Paused jobs have `assigned_agent_id = NULL` and status `paused`. They're in the dispatch queue. Any agent (including the restarted one) can resume them |
| Preemption initiated but agent finishes job naturally before pausing | Agent completes the job, reports ready via heartbeat. The pause request arrives at an idle agent and returns 404 (no job running). The high-priority job gets dispatched normally on the next cycle |

## Files Modified

| File | Changes |
|------|---------|
| `orchestrator/database/schema.sql` | `priority` column, `paused` status, `last_completed_at` column, view update |
| `orchestrator/database/postgres.py` | `get_dispatchable_jobs()`, `get_available_agents()`, `get_preemption_candidates()`, `pause_job()`, heartbeat tracking, fix `cancel_job()` |
| `orchestrator/main.py` | Dispatcher, background task, trigger hooks, env var, pause endpoint, resume extension, assignment refactor |
| `src/api/app.py` | `POST /job/pause` endpoint, cooperative pause flag, streaming loop check, verification priority (Phase 4) |
| `cockpit/.../job-create` component | Priority dropdown |
| `cockpit/.../job-list` component | Pause/resume buttons, priority badge, paused status styling |
