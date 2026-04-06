# Feature: Dual-Mode Agent Pods

## Summary

Refactor agent pods so a single pod can serve as either a **worker** (job-based) or **persistent** (interactive session) agent at runtime, rather than being locked into one mode at startup. An idle dual-mode pod accepts whichever work arrives first — a dispatched job or a session attachment — and returns to the available pool when done.

## Motivation

**Current state:** Worker and persistent agents are separate deployments (or at least separate processes started with different `--mode` flags). This creates:

- **Resource waste**: Persistent pods sit idle between sessions. Worker pods can't absorb interactive demand spikes, and vice versa.
- **Scaling inflexibility**: Autoscaling must be configured per mode. A cluster might have 4 idle worker pods and 0 available persistent pods during peak interactive usage.
- **Operational overhead**: Two deployment manifests, two scaling policies, two monitoring dashboards for what is fundamentally the same binary.

**Desired state:** A single pool of agent pods that dynamically serve either workload. The orchestrator assigns work based on demand, not pre-assigned mode. Cluster utilization improves because any idle pod can handle any work type.

## Current Architecture

### What's Shared (Already Unified)

| Component | Details |
|-----------|---------|
| Container image | Same Dockerfile, same `agent.py` entry point |
| `UniversalAgent` class | Identical instantiation via `from_config()` for both modes |
| LLM instances | Same creation, same model catalog |
| Tool implementations | Same code — worker excludes session tools, persistent excludes phase tools |
| Database connections | Same PostgreSQL, pgvector, MongoDB, Neo4j clients |
| Registration protocol | Same `/api/agents/register` endpoint (discriminated by `agent_mode` field) |
| Heartbeat protocol | Same `/api/agents/{id}/heartbeat` endpoint, same 60s interval |
| Config system | Same YAML loading, same `$extends` inheritance, same deep-merge |

### What's Separate (Needs Unification)

| Component | Worker | Persistent |
|-----------|--------|------------|
| FastAPI app | `src/api/app.py` — HTTP `/job/start`, `/job/pause`, `/job/resume` | `src/api/persistent_app.py` — WebSocket `/ws/chat`, `/session/attach`, `/session/detach` |
| Execution loop | `process_job()` via LangGraph (phase alternation, todos, archive) | `run_persistent_loop()` via simple while-loop (no phases, no todos) |
| Config base | `config/defaults.yaml` (phases, verification, delegation, autonomy) | `config/persistent_defaults.yaml` (minimal, interactive-only) |
| Entry point | `run_server()` — blocks on uvicorn with worker app | `run_persistent_server()` — blocks on uvicorn with persistent app |
| Dispatcher integration | Joins worker pool; orchestrator POSTs `JobStartRequest` | Binds to thread; orchestrator POSTs `/session/attach` |
| Agent DB row | `agent_mode='worker'`, `thread_id=NULL` | `agent_mode='persistent'`, `thread_id=<uuid>` |
| Concurrency | One job at a time, sequential | One session at a time (pool mode), sequential |

### Key Insight

Both modes already operate on a **one-task-at-a-time, sequential** model:
- Worker: accepts job, processes, completes, becomes ready.
- Persistent (pool mode): accepts session attach, serves session, detaches, becomes ready.

This means dual-mode doesn't require concurrent job+session handling — just the ability to accept *either* type of work when idle.

## Design

### Core Principle: Mode as Runtime State, Not Startup Config

Instead of `--mode worker` vs `--mode persistent` at container start, the pod starts in **dual mode** and dynamically enters worker or persistent state based on what the orchestrator assigns.

```
                        ┌─────────────┐
                        │    IDLE     │ ← Pod starts here
                        │ (dual mode) │
                        └──────┬──────┘
                               │
                    ┌──────────┴──────────┐
                    │                     │
            /job/start              /session/attach
                    │                     │
              ┌─────▼─────┐       ┌───────▼───────┐
              │  WORKING  │       │   SESSION    │
              │ (job mode)│       │ (persistent) │
              └─────┬─────┘       └───────┬───────┘
                    │                     │
             job completes         session detaches
                    │                     │
                    └──────────┬──────────┘
                               │
                        ┌──────▼──────┐
                        │  POD EXIT   │ ← Process exits, K8s/Docker restarts fresh
                        └─────────────┘
```

**Pod-per-task model:** Each pod handles exactly one task (job or session), then exits. K8s `restartPolicy: Always` or Docker Compose `restart: unless-stopped` brings up a fresh container. This eliminates memory leaks, workspace artifacts, and state contamination between tasks. Pod restart time (~10-15s) replaces the old 30s cooldown.

### Agent-Side Changes

#### 1. Unified FastAPI App (`src/api/dual_app.py`)

Merge worker and persistent routes into a single FastAPI application. No endpoint conflicts exist — the two apps use different path prefixes:

```
Worker routes:          Persistent routes:
  POST /job/start         WS   /ws/chat
  POST /job/pause         POST /session/attach
  POST /job/resume        POST /session/detach
  POST /job/cancel
```

The unified app mounts all routes and uses a state machine to gate access:

```python
class PodState(str, Enum):
    IDLE = "idle"           # Ready for any work
    WORKING = "working"     # Processing a job
    SESSION = "session"     # Serving a persistent session

_pod_state: PodState = PodState.IDLE
```

**Guards:**
- `/job/start` returns `409 Conflict` if state is not `IDLE`
- `/session/attach` returns `409 Conflict` if state is not `IDLE`
- `/ws/chat` returns `403` if state is not `SESSION`
- `/job/pause`, `/job/resume` return `404` if state is not `WORKING`
- `/session/detach` returns `404` if state is not `SESSION`

#### 2. Config Hot-Loading

When entering a mode, load the appropriate config base:

```python
async def _enter_worker_mode(job_request: JobStartRequest):
    """Transition IDLE → WORKING."""
    # Config comes from job_request.config_name + config_override
    # (already works this way — process_job() loads config per-job)
    _pod_state = PodState.WORKING
    asyncio.create_task(_process_job(job_request))

async def _enter_session_mode(thread_id: str, config_override: dict):
    """Transition IDLE → SESSION."""
    # Load persistent_defaults.yaml, merge config_override
    # Create PersistentSession, call setup()
    _pod_state = PodState.SESSION
    await _attach_session(thread_id, config_override)
```

No structural changes to `UniversalAgent` — it's already mode-agnostic. The worker path calls `agent.process_job()`. The session path wraps the agent in `PersistentSession`.

#### 3. Registration

Register with `agent_mode='dual'` instead of `'worker'` or `'persistent'`:

```python
await orchestrator_client.register(
    agent_mode="dual",
    thread_id=None,  # Not bound to any thread at startup
)
```

Heartbeat reports the current state:

```python
def get_status() -> str:
    match _pod_state:
        case PodState.IDLE:
            return "ready"
        case PodState.WORKING:
            return "working"
        case PodState.SESSION:
            return "session"  # New status value
```

#### 4. Entry Point Change

Replace the `--mode` flag with a default dual mode:

```python
# Before:
parser.add_argument("--mode", choices=["worker", "persistent"], default="worker")

# After:
parser.add_argument("--mode", choices=["worker", "persistent", "dual"], default="dual")
```

`--mode worker` and `--mode persistent` remain as options for backward compatibility and special-purpose deployments (e.g., a dedicated persistent pod for a long-running session).

### Orchestrator-Side Changes

#### 1. Schema Migration

Add `'dual'` to the agent_mode vocabulary and `'session'` to agent status:

```sql
-- Extend agent_mode to include 'dual'
-- (No CHECK constraint on agent_mode currently, so this is just a convention change)

-- Add 'session' to valid agent statuses
ALTER TABLE agents DROP CONSTRAINT IF EXISTS valid_agent_status;
ALTER TABLE agents ADD CONSTRAINT valid_agent_status CHECK (
    status IN ('booting', 'ready', 'working', 'session', 'draining', 'completed', 'failed', 'offline')
);
```

#### 2. Dispatcher Changes

**Job dispatcher** (`_try_dispatch_pending_jobs`): Include dual-mode agents in the available pool.

```sql
-- Before:
WHERE status = 'ready' AND COALESCE(agent_mode, 'worker') = 'worker'

-- After:
WHERE status = 'ready' AND COALESCE(agent_mode, 'worker') IN ('worker', 'dual')
  AND current_job_id IS NULL
  AND thread_id IS NULL  -- Not currently bound to a session
```

**Session dispatcher** (thread creation / resume): Include dual-mode agents when looking for available persistent agents.

```sql
-- Before:
WHERE status = 'ready' AND agent_mode = 'persistent' AND thread_id IS NULL

-- After:
WHERE status = 'ready' AND agent_mode IN ('persistent', 'dual') AND thread_id IS NULL
```

#### 3. Priority and Fairness

When both a job and a session are waiting, the orchestrator needs a decision rule.

**Approach:** FIFO — first request to arrive gets the next idle pod. The dispatcher runs on a 30s cycle; session attachment is immediate (via REST call to the pod). No reservation logic, no priority tiers. If all pods are busy, new work waits for a pod to restart after completing its task. The HPA scales up if idle pods drop below the target ratio.

#### 4. Thread Provisioning Changes

Currently, thread creation provisions a *new* persistent agent pod (K8s) or finds an idle one from the persistent pool (Docker Compose). With dual mode:

**K8s mode:** No change to provisioning — dual pods already exist in the deployment. The orchestrator just needs to find an idle dual pod and call `/session/attach` instead of spinning up a dedicated persistent pod. If no idle dual pod is available, the HPA (Horizontal Pod Autoscaler) scales up the single dual-mode deployment.

**Docker Compose mode:** The `_find_idle_persistent_agent()` function becomes `_find_idle_agent()` and queries for any idle dual/persistent pod.

### Deployment Changes

#### Single Deployment Manifest

Replace the two separate deployments with one:

```yaml
# Before: deployment/21-agent.yaml (worker) + deployment/22-persistent-agent.yaml
# After:  deployment/21-agent.yaml (dual)

apiVersion: apps/v1
kind: Deployment
metadata:
  name: srw-agent
spec:
  replicas: 4  # Single pool serves both workloads
  template:
    spec:
      containers:
        - name: agent
          command: ["python", "agent.py", "--mode", "dual", "--port", "8001"]
          env:
            - name: AGENT_CONFIG
              value: "default"
          ports:
            - containerPort: 8001
          livenessProbe:
            httpGet:
              path: /health
              port: 8001
          readinessProbe:
            httpGet:
              path: /ready
              port: 8001
```

**HPA (Horizontal Pod Autoscaler):**

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: srw-agent
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: srw-agent
  minReplicas: 2
  maxReplicas: 16
  metrics:
    - type: Pods
      pods:
        metric:
          name: agent_idle_ratio  # Custom metric: idle_pods / total_pods
        target:
          type: AverageValue
          averageValue: "0.25"  # Keep ~25% of pods idle for responsiveness
```

## Implementation Plan

### Phase 1: Unified App (Agent-Side)

**Scope:** Create `dual_app.py`, merge routes, add state machine. No orchestrator changes yet — dual pods register as `worker` for backward compatibility.

| Task | File | Effort |
|------|------|--------|
| Create `PodState` enum and guards | `src/api/dual_app.py` | S |
| Mount worker routes from `app.py` | `src/api/dual_app.py` | M |
| Mount persistent routes from `persistent_app.py` | `src/api/dual_app.py` | M |
| Unified lifespan (create agent once, support both paths) | `src/api/dual_app.py` | M |
| Add `--mode dual` to entry point | `agent.py` | S |
| State transition logic (IDLE -> WORKING/SESSION -> exit) | `src/api/dual_app.py` | M |
| Tests: state guards, concurrent request rejection | `tests/test_dual_app.py` | M |

**Validation:** Start pod in dual mode, manually POST `/job/start` — verify it processes. POST `/session/attach` — verify it serves. Confirm mutual exclusion.

### Phase 2: Orchestrator Dispatch (Orchestrator-Side)

**Scope:** Teach the dispatcher about dual-mode agents. Add `session` status. Update agent queries.

| Task | File | Effort |
|------|------|--------|
| Add `'session'` to valid agent statuses | `orchestrator/database/schema.sql` | S |
| Update `get_available_agents()` to include dual-mode | `orchestrator/database/postgres.py` | S |
| Update `_find_idle_persistent_agent()` to include dual-mode | `orchestrator/main.py` | S |
| Reduce default session idle timeout to 15 minutes | `config/persistent_defaults.yaml` | S |
| Update registration to accept `agent_mode='dual'` | `orchestrator/main.py` | S |
| Update heartbeat to handle `status='session'` | `orchestrator/database/postgres.py` | S |
| Tests: dispatch prefers idle dual pod, mutual exclusion | `tests/test_dispatch_dual.py` | M |

**Validation:** Deploy 2 dual pods. Create a job — verify one pod picks it up. Create a session — verify the other pod serves it. Complete both — verify both return to idle.

### Phase 3: Deployment Unification

**Scope:** Merge K8s manifests. Configure HPA. Remove dedicated persistent deployment.

| Task | File | Effort |
|------|------|--------|
| Update agent deployment to use `--mode dual` | `deployment/21-agent.yaml` | S |
| Remove dedicated persistent deployment (if exists) | `deployment/22-persistent-agent.yaml` | S |
| Configure HPA with idle-ratio metric | `deployment/21-agent-hpa.yaml` | M |
| Update Docker Compose for dual mode | `docker-compose*.yaml` | S |
| Update Fleet/Kustomize overlays | `deployment-local/` | S |

### Phase 4: Cleanup and Documentation

**Scope:** `--mode dual` is already the default. Clean up references to old modes and update docs.

| Task | File | Effort |
|------|------|--------|
| Update CLAUDE.md command examples to use dual mode | `CLAUDE.md` | S |
| Add pod-exit-after-task logic (process exits after job/session completes) | `src/api/dual_app.py` | S |
| Update Docker Compose files for dual mode and restart policy | `docker-compose*.yaml` | S |
| Update CI/CD pipeline if it references `--mode` | `.github/workflows/` | S |

## Edge Cases and Risks

### Race Condition: Job and Session Arrive Simultaneously

**Scenario:** Orchestrator dispatches a job to a dual pod at the same moment a session attach arrives.

**Mitigation:** The pod state machine uses an `asyncio.Lock` for transitions. First request to acquire the lock wins; second gets `409 Conflict`. The orchestrator retries the rejected request on the next available pod.

```python
_state_lock = asyncio.Lock()

async def _enter_worker_mode(request):
    async with _state_lock:
        if _pod_state != PodState.IDLE:
            raise HTTPException(409, "Pod is not idle")
        _pod_state = PodState.WORKING
```

### Long-Running Sessions Block Job Dispatch

**Scenario:** All dual pods are in `SESSION` state; jobs queue up indefinitely.

**Mitigation:**
- HPA scales up based on idle ratio metric (target 25% idle).
- Idle timeout on sessions (15 minutes) exits the pod and returns capacity to the pool.
- Active sessions (user sending messages) are not affected by the timeout — only idle ones.
- Orchestrator dashboard shows pool utilization breakdown (idle/working/session).

### Session Resume After Pod Replacement

**Scenario:** A dual pod was serving a session, crashes, and the replacement pod (from the same deployment) starts fresh in IDLE.

**Mitigation:** No change from current behavior. The orchestrator already handles this:
1. Heartbeat timeout (3min) marks agent as offline.
2. Thread status set to "idle" (resumable).
3. User reconnects, orchestrator calls `/session/attach` on a new idle pod.
4. New pod restores message history from DB and resumes.

### Config Contamination Between Modes

**Scenario:** A pod serves a persistent session with custom config (e.g., `temperature=0.3`), then picks up a worker job that expects default config.

**Mitigation:** Not an issue — pods exit after every task. Each new pod starts with a clean `UniversalAgent` instance. No state, config, or workspace artifacts carry over.

### Workspace Isolation

**Scenario:** A job writes files to the workspace, then a session starts and sees leftover files.

**Mitigation:** Not an issue — pods exit after every task, so no workspace artifacts persist. Additionally, workspaces are already task-scoped (job gets `workspace/job_<uuid>/`, session gets `workspace/thread_<uuid>/`).

## Metrics and Observability

New metrics to expose from dual-mode pods:

| Metric | Type | Description |
|--------|------|-------------|
| `agent_pod_state` | Gauge (enum) | Current state: idle, working, session |
| `agent_task_type` | Label | Whether this pod served a job or session (reported at exit) |
| `agent_task_seconds` | Histogram (by type) | Time from task start to pod exit |
| `agent_idle_seconds` | Histogram | Time spent in IDLE before receiving a task |

Orchestrator-level:

| Metric | Type | Description |
|--------|------|-------------|
| `pool_idle_pods` | Gauge | Number of pods in IDLE state |
| `pool_working_pods` | Gauge | Number of pods in WORKING state |
| `pool_session_pods` | Gauge | Number of pods in SESSION state |
| `pool_utilization_ratio` | Gauge | `(working + session) / total` |
| `dispatch_rejected_409` | Counter | Times a dispatch was rejected (pod busy) |

## Non-Goals

- **Concurrent job + session on the same pod**: Out of scope. Both modes are CPU/memory intensive and share the same LLM client. Running both simultaneously would degrade quality and complicate state management. The one-task-at-a-time model is retained.
- **Multi-task pod lifecycle**: Pods exit after every task. No reuse, no task counters, no cooldown. Fresh container on every restart eliminates an entire class of state management bugs.
- **Sticky sessions**: No mechanism to pin a session to a specific pod. The 15-minute idle timeout and checkpoint/resume handle all lifecycle needs. If a session is interrupted (pod crash, timeout), the user reconnects and a new pod restores from DB state.
- **Live migration**: Moving an active job or session from one pod to another mid-execution. Checkpoint/resume already handles pod failures; live migration adds complexity without clear benefit.
- **Demand-aware eviction**: The orchestrator won't force-detach idle sessions when jobs are queuing. The 15-minute idle timeout is sufficient. HPA handles capacity scaling.

## Decisions

1. **`--mode dual` is the default immediately.** We're still in development — no backward-compatibility ceremony needed. The old `--mode worker` and `--mode persistent` flags remain functional but are no longer the default.

2. **Session idle timeout reduced to 15 minutes (from 30).** No demand-aware eviction logic. The shorter timeout is a simple way to return pods to the pool faster without adding orchestrator complexity. If 15 minutes proves too aggressive, we adjust the number.

3. **Pod restarts after every job or session.** No task counter, no `PERSISTENT_MAX_SESSIONS`. After a job completes or a session detaches, the pod exits and K8s/Docker restarts a fresh one. This eliminates memory leak concerns entirely — no LLM client state, tool caches, or workspace artifacts can accumulate across tasks. The 30s cooldown before accepting new work is replaced by pod restart time (~10-15s for a fresh container).

4. **No sticky sessions.** The 15-minute idle timeout is sufficient. Active sessions (user is sending messages) never time out. If sticky sessions become a real issue later, we revisit — but don't build it now.
