---
tags:
  - agent-architecture
  - deployment
  - infrastructure
---

# Pod Runtime — Dynamic Agent Pods via Kubernetes API

Design document for dynamically creating agent pods on the same cluster when jobs are dispatched, eliminating the need for pre-registered long-running agent processes.

> **Status (2026-04-11):** Retained as design context only. The entire premise of this document — single-job CLI mode on `agent.py` (`--job-id`, `--pod-mode`, `run_single_job()`) — no longer exists. The agent is always run as a server (`python agent.py --port 8001 --loop`) and jobs are dispatched over HTTP by the orchestrator. If a pod-per-task dispatch mode is still desired, it would need to be designed around the current server-mode entry point (e.g. spawn a pod, wait for `/ready`, POST the job, let it exit). Everything below describes a CLI-mode integration that has been deleted.

## Motivation

Today, agents are long-running Deployments (2 replicas, see `deployment/21-agent.yaml`) that register with the orchestrator via heartbeat and wait for jobs. The dispatcher (`_try_dispatch_pending_jobs` in `orchestrator/main.py:883`) matches pending jobs to available agents, then POSTs the full `JobStartRequest` to the agent's `/job/start` endpoint. This model has drawbacks:

| Problem | Impact |
|---------|--------|
| **Idle resources** | Agent pods consume 512Mi-2Gi memory even when no jobs are queued |
| **Fixed capacity** | Scaling requires changing the Deployment replica count and redeploying |
| **Registration dance** | Agents must heartbeat every 60s, dispatcher must match — extra state |
| **Cold start mismatch** | All agents run the same image with the same `AGENT_CONFIG`, even if jobs need different expert configs |

The VM provisioner (`orchestrator/services/vm_provisioner.py`) already solves dynamic provisioning for cross-cluster VMs via NATS. But for same-cluster pods, we don't need NATS, KubeVirt, or VM templates. Kubernetes has a native API for creating Jobs, and when the orchestrator runs in-cluster, `load_incluster_config()` provides auth for free.

### Why Pods, Not VMs?

VMs (KubeVirt) remain the right choice for untrusted workloads that need full OS isolation, `sudo`, package installation, and browser-based remote access. Pods are for **trusted, lightweight jobs** that just need a Python environment and API keys — the vast majority of jobs.

| | Pod runtime | VM runtime |
|--|-------------|------------|
| **Startup** | ~5-15s (image pull cached) | ~30-60s |
| **Overhead** | ~512 Mi (same as current agents) | ~512 MB-2 GB |
| **Isolation** | Container (namespace/cgroup) | Hypervisor |
| **Can install packages** | No (limited to what's in the image) | Yes (sudo gate) |
| **Remote access (IDE)** | Not practical | code-server |
| **Best for** | Document processing, research, DB ops, writing | Development, system admin, untrusted code |

## Design

### Core Idea

When the dispatcher picks up a pending job, instead of POSTing to a pre-registered agent, it resolves the full dispatch payload, stores it on the job record, and creates a Kubernetes Job that runs the agent for that specific job. When the job completes, the pod exits and Kubernetes cleans it up.

```
Job created (cockpit / API)
        │
        ▼
Dispatcher picks up pending job
        │
        ▼
Resolve dispatch payload
  (datasources, API keys, user prefs, tool overrides —
   same logic as _dispatch_job_to_agent, lines 486-639)
        │
        ▼
Store resolved payload in jobs.dispatch_payload JSONB
        │
        ▼
Create K8s Job: agent-job-{short_id}
  - image: existing agent image (or per-expert override)
  - env: from srw-config + srw-secrets (same as 21-agent.yaml)
  - extra env: JOB_ID, POD_MODE=1
  - volumeMounts: srw-workspace PVC at /workspace
  - command: python agent.py --job-id {job_id} --pod-mode
        │
        ▼
Pod starts, agent reads dispatch_payload from DB
  - Gets full context: datasources, config_override, repos, branch
  - Processes the job normally
  - Sends heartbeats to orchestrator (progress tracking)
  - Writes workspace to shared PVC (/workspace/job_{uuid}/)
        │
        ▼
Job completes → agent calls /api/jobs/{id}/complete → pod exits
        │
        ▼
K8s TTL controller cleans up (ttlSecondsAfterFinished)
```

### The Dispatch Payload Problem

This is the central design challenge. Today's dispatch flow is **push-based**: the orchestrator resolves a rich `JobStartRequest` in `_dispatch_job_to_agent()` (orchestrator/main.py:472-673) and POSTs it to the agent's HTTP API. This resolution includes:

1. **Datasource resolution** — job-scoped, project-scoped, and global datasources merged with precedence
2. **Tool override injection** — datasource types map to tool categories (`graph`, `sql`, `mongodb`)
3. **API key resolution** — user keys > project keys > env var fallback, injected into `config_override.llm.api_key`
4. **User preference injection** — default model, autonomy level, reasoning level
5. **Project repository resolution** — repo URLs, branches, clone paths
6. **VM workspace config** — SSH host/port/credentials (not relevant for pod runtime)

For pod runtime, there's no running agent to POST to. The agent pod hasn't started yet.

**Solution:** Refactor the dispatch resolution into a shared helper, store the result in a `dispatch_payload` JSONB column on the `jobs` table, and have the agent read it on startup.

```python
# New helper extracted from _dispatch_job_to_agent()
async def _resolve_dispatch_payload(job: dict) -> dict:
    """Resolve the full dispatch payload for a job.

    Extracted from _dispatch_job_to_agent() so both push (HTTP) and
    pull (pod reads from DB) dispatch modes share the same resolution.
    """
    # ... datasource resolution, API key injection, user prefs, etc.
    # Returns a dict matching JobStartRequest schema
    return payload

# Push dispatch (existing): resolve + POST to agent
async def _dispatch_job_to_agent(job, agent):
    payload = await _resolve_dispatch_payload(job)
    # POST to agent...

# Pod dispatch (new): resolve + store + create K8s Job
async def _dispatch_job_as_pod(job):
    payload = await _resolve_dispatch_payload(job)
    await postgres_db.set_dispatch_payload(job_id, payload)
    await pod_provisioner.create_agent_pod(job_id, ...)
```

### Agent Pod Mode (`--pod-mode`)

A new startup mode in `agent.py`, complementing the existing single-job mode and API server mode:

```
Current modes:
  --job-id {id}                     → Single-job mode (no orchestrator integration)
  (default, no --job-id)            → API server mode (register, heartbeat, wait for dispatch)

New mode:
  --job-id {id} --pod-mode          → Pod mode (single job + orchestrator integration)
```

Pod mode combines the best of both:
- **From single-job mode**: processes one job and exits (no API server)
- **From server mode**: reads dispatch payload, sends heartbeats, calls `/complete`

```python
# In agent.py, pod mode startup:
async def run_pod_mode(config_path: str, job_id: str):
    """Run a single job with full orchestrator integration, then exit."""
    # 1. Read dispatch_payload from DB (or GET /api/jobs/{id}/dispatch-payload)
    db = PostgresDB()
    await db.connect()
    job = await db.jobs.get(job_id)
    payload = job.get("dispatch_payload", {})

    # 2. Create orchestrator client for heartbeats + completion
    orch_client = create_orchestrator_client_from_env(payload.get("config_name", "default"))
    await orch_client.connect()

    # 3. Start heartbeat loop (reports "working" status with job_id)
    heartbeat_task = asyncio.create_task(
        orch_client.run_heartbeat_loop(
            get_status=lambda: "working",
            get_job_id=lambda: job_id,
            get_metrics=lambda: None,
        )
    )

    # 4. Initialize agent with resolved config
    agent = UniversalAgent.from_config(config_path)
    await agent.initialize()

    # 5. Process job (using dispatch payload for datasources, config_override, etc.)
    result = await agent.process_job(job_id, metadata_from_payload(payload), stream=True)

    # 6. Report completion
    await orch_client.report_completion(job_id, result)

    # 7. Cleanup and exit
    orch_client.stop_heartbeat()
    await heartbeat_task
```

### No NATS, No VM Controller

Since pods run on the same cluster as the orchestrator:

- **Auth**: `load_incluster_config()` — the orchestrator's ServiceAccount gets RBAC to create Jobs.
- **Communication**: Direct HTTP via cluster-internal DNS (`srw-orchestrator:8085`). The `ORCHESTRATOR_URL` env var is already set in `srw-config` ConfigMap.
- **Status**: K8s Job status API + heartbeats for progress. The orchestrator can query `BatchV1Api.read_namespaced_job_status()` or rely on agent heartbeats.
- **Cleanup**: Kubernetes `ttlSecondsAfterFinished` on the Job spec auto-deletes completed pods.
- **Logs**: Written to `/workspace/logs/job_{id}.log` on the shared PVC (same as current agents), plus stdout/stderr captured by the cluster log collector.

### Implementation

#### 1. Pod Provisioner (`orchestrator/services/pod_provisioner.py`)

A lightweight provisioner (~150 lines). No template files — the Job manifest is built in Python.

```python
"""Pod Provisioner — create agent pods via Kubernetes Jobs API."""

import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from kubernetes import client as k8s_client, config as k8s_config
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False

# Match existing deployment values
AGENT_NAMESPACE = os.environ.get("AGENT_NAMESPACE", "superhuman-remote-worker")
AGENT_IMAGE = os.environ.get(
    "AGENT_IMAGE",
    "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent:latest",
)


class PodProvisioner:
    def __init__(self):
        self._batch: Optional[Any] = None
        self._available = False

    @property
    def is_available(self) -> bool:
        return self._available

    def connect(self) -> None:
        if not K8S_AVAILABLE:
            logger.info("Pod provisioner: kubernetes package not installed")
            return
        try:
            k8s_config.load_incluster_config()
        except Exception:
            try:
                k8s_config.load_kube_config()
            except Exception:
                logger.info("Pod provisioner: no K8s config available")
                return
        self._batch = k8s_client.BatchV1Api()
        self._available = True
        logger.info("Pod provisioner ready (namespace=%s)", AGENT_NAMESPACE)

    async def create_agent_pod(
        self,
        job_id: str,
        config_name: str = "default",
        image: str | None = None,
        cpu_request: str = "250m",
        cpu_limit: str = "1000m",
        memory_request: str = "512Mi",
        memory_limit: str = "2Gi",
        timeout: int = 14400,
    ) -> bool:
        """Create a K8s Job that runs the agent for a specific job."""
        ...

    async def delete_agent_pod(self, job_id: str) -> bool:
        """Delete the K8s Job for a job (cancellation)."""
        ...

    async def get_pod_status(self, job_id: str) -> Optional[dict]:
        """Query K8s Job status."""
        ...
```

The Job manifest mirrors the existing `21-agent.yaml` env setup:

```python
def _build_job_manifest(self, job_id, config_name, image, resources, timeout):
    short_id = job_id[:8]
    job_name = f"agent-job-{short_id}"

    # Build env list — reference existing srw-config and srw-secrets
    # exactly as deployment/21-agent.yaml does
    config_keys = [
        "DATABASE_URL", "VECTOR_DB_URL", "MONGODB_URL", "ORCHESTRATOR_URL",
        "WORKSPACE_PATH", "LLM_BASE_URL", "KEY_COOLDOWN_SECONDS",
        "VISION_MODEL", "BROWSER_LLM_MODEL", "BROWSER_LLM_BASE_URL",
        "RESEARCH_PROXY_TYPE", "RESEARCH_PROXY_HOST", "RESEARCH_PROXY_PORT",
        "CITATION_LLM_URL", "CITATION_LLM_MODEL", "CITATION_REASONING_LEVEL",
        "EMBEDDING_MODEL", "EMBEDDING_BASE_URL",
        "UNPAYWALL_EMAIL", "NEO4J_URL", "NEO4J_USERNAME", "LOG_LEVEL",
    ]
    # NB: the citation engine is now a native SRW subsystem — it uses the shared
    # vector pool (VECTOR_POSTGRES_*) + SRW's EMBEDDING_* service, so the former
    # CITATION_DB_URL / CITATION_EMBEDDING_* keys were retired (only the verifier
    # CITATION_LLM_* model slot remains). See docs/done/citation_engine_integration.md.
    secret_keys = [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
        "OPENROUTER_API_KEY", "GOOGLE_API_KEY", "TAVILY_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY", "VISION_API_KEY", "WHISPER_API_KEY",
        "EMBEDDING_API_KEY", "NEO4J_PASSWORD",
    ]

    env = [
        # Pod-mode specific
        k8s_client.V1EnvVar(name="JOB_ID", value=job_id),
        k8s_client.V1EnvVar(name="POD_MODE", value="1"),
        k8s_client.V1EnvVar(name="AGENT_CONFIG", value=config_name),
    ]
    # ConfigMap references
    for key in config_keys:
        env.append(k8s_client.V1EnvVar(
            name=key,
            value_from=k8s_client.V1EnvVarSource(
                config_map_key_ref=k8s_client.V1ConfigMapKeySelector(
                    name="srw-config", key=key, optional=True,
                )
            ),
        ))
    # Secret references
    for key in secret_keys:
        env.append(k8s_client.V1EnvVar(
            name=key,
            value_from=k8s_client.V1EnvVarSource(
                secret_key_ref=k8s_client.V1SecretKeySelector(
                    name="srw-secrets", key=key, optional=True,
                )
            ),
        ))

    return k8s_client.V1Job(
        metadata=k8s_client.V1ObjectMeta(
            name=job_name,
            namespace=AGENT_NAMESPACE,
            labels={
                "app": "srw-agent",
                "app.kubernetes.io/component": "agent-pod",
                "srw/job-id": job_id,
                "srw/config": config_name,
                "srw/runtime": "pod",
            },
        ),
        spec=k8s_client.V1JobSpec(
            ttl_seconds_after_finished=300,   # Clean up 5 min after done
            backoff_limit=0,                  # No retry (agent handles its own)
            active_deadline_seconds=timeout,  # Hard timeout (default 4h)
            template=k8s_client.V1PodTemplateSpec(
                metadata=k8s_client.V1ObjectMeta(
                    labels={
                        "app": "srw-agent",
                        "srw/job-id": job_id,
                        "srw/runtime": "pod",
                    },
                ),
                spec=k8s_client.V1PodSpec(
                    restart_policy="Never",
                    # Same init container as 21-agent.yaml
                    init_containers=[k8s_client.V1Container(
                        name="wait-for-orchestrator",
                        image="busybox:1.36",
                        command=["sh", "-c",
                                 "until nc -z srw-orchestrator 8085; do sleep 2; done"],
                    )],
                    containers=[k8s_client.V1Container(
                        name="agent",
                        image=image or AGENT_IMAGE,
                        command=[
                            "python", "agent.py",
                            "--job-id", job_id,
                            "--config", config_name,
                            "--pod-mode",
                        ],
                        env=env,
                        volume_mounts=[k8s_client.V1VolumeMount(
                            name="workspace",
                            mount_path="/workspace",
                        )],
                        resources=k8s_client.V1ResourceRequirements(
                            requests={
                                "cpu": resources.get("cpu_request", "250m"),
                                "memory": resources.get("memory_request", "512Mi"),
                            },
                            limits={
                                "cpu": resources.get("cpu_limit", "1000m"),
                                "memory": resources.get("memory_limit", "2Gi"),
                            },
                        ),
                    )],
                    volumes=[k8s_client.V1Volume(
                        name="workspace",
                        persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(
                            claim_name="srw-workspace",
                        ),
                    )],
                ),
            ),
        ),
    )
```

#### 2. Dispatch Payload Refactor

Extract the config resolution logic from `_dispatch_job_to_agent()` (orchestrator/main.py:486-639) into a shared helper:

```python
async def _resolve_dispatch_payload(job: dict) -> dict:
    """Resolve the full dispatch payload for a job.

    Performs datasource resolution, API key injection, user preference
    injection, project repo resolution, and tool override generation.

    Used by both push dispatch (HTTP POST to agent) and pod dispatch
    (stored in DB for agent to read on startup).
    """
    job_id = str(job["id"])

    # ... extract from current _dispatch_job_to_agent lines 486-639 ...

    return job_start.model_dump(exclude_none=True)


async def _dispatch_job_to_agent(job: dict, agent: dict) -> bool:
    """Start a new job on an agent via HTTP POST (push dispatch)."""
    payload = await _resolve_dispatch_payload(job)
    agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}/job/start"
    # POST payload, update status...


async def _dispatch_job_as_pod(job: dict) -> bool:
    """Start a new job by creating a K8s pod (pull dispatch)."""
    job_id = str(job["id"])
    payload = await _resolve_dispatch_payload(job)

    # Store resolved payload so the pod can read it on startup
    await postgres_db.set_dispatch_payload(job_id, payload)

    # Extract pod resource config from config_override
    config_override = job.get("config_override") or {}
    if isinstance(config_override, str):
        config_override = json.loads(config_override)
    pod_cfg = config_override.get("pod", {})

    ok = await pod_provisioner.create_agent_pod(
        job_id=job_id,
        config_name=job.get("config_name", "default"),
        image=pod_cfg.get("image"),
        cpu_request=pod_cfg.get("cpu_request", "250m"),
        cpu_limit=pod_cfg.get("cpu_limit", "1000m"),
        memory_request=pod_cfg.get("memory_request", "512Mi"),
        memory_limit=pod_cfg.get("memory_limit", "2Gi"),
        timeout=pod_cfg.get("timeout", 14400),
    )

    if ok:
        await postgres_db.update_job_status(job_id, "dispatched")
        logger.info(f"Pod dispatch: created pod for job {job_id}")
    return ok
```

#### 3. Database Change

One new nullable JSONB column on the `jobs` table:

```sql
ALTER TABLE jobs ADD COLUMN dispatch_payload JSONB;
```

And a helper method:

```python
# orchestrator/database/postgres.py
async def set_dispatch_payload(self, job_id: str, payload: dict) -> None:
    async with self.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET dispatch_payload = $1::jsonb WHERE id = $2::uuid",
            json.dumps(payload), uuid.UUID(job_id),
        )
```

#### 4. Dispatcher Integration

In `_try_dispatch_pending_jobs()` (orchestrator/main.py:883), add a pod runtime check after the existing VM check and before the agent-matching phase:

```python
dispatchable_jobs = []
pod_dispatched_ids = set()

for job in pending_jobs:
    job_id = str(job["id"])

    # Existing: VM check
    if _job_needs_vm(job):
        # ... existing VM provisioning logic ...
        continue

    # New: Pod runtime check
    if _job_wants_pod_runtime(job) and pod_provisioner.is_available:
        ok = await _dispatch_job_as_pod(job)
        if ok:
            pod_dispatched_ids.add(job_id)
        continue

    dispatchable_jobs.append(job)


def _job_wants_pod_runtime(job: dict) -> bool:
    """Check if a job should use pod runtime."""
    co = job.get("config_override") or {}
    if isinstance(co, str):
        co = json.loads(co)
    return co.get("runtime") == "pod"
```

#### 5. Cancellation

In the `cancel_job` endpoint (orchestrator/main.py:1999), add K8s Job deletion:

```python
# After existing agent cancel attempt and VM termination:

# If this was a pod-runtime job, delete the K8s Job
if pod_provisioner.is_available:
    deleted = await pod_provisioner.delete_agent_pod(job_id)
    if deleted:
        logger.info(f"Deleted agent pod for job {job_id}")
```

Kubernetes sends SIGTERM to the container. The agent should handle SIGTERM gracefully — save checkpoint, update job status. The `terminationGracePeriodSeconds` (default 30s) gives it time.

#### 6. RBAC

The orchestrator needs permission to manage Jobs. Add a ServiceAccount (if not already present) and a Role:

```yaml
# deployment/24-pod-runtime-rbac.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: srw-orchestrator
  namespace: superhuman-remote-worker
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: srw-pod-manager
  namespace: superhuman-remote-worker
rules:
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "delete", "get", "list", "watch"]
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: srw-pod-manager
  namespace: superhuman-remote-worker
subjects:
  - kind: ServiceAccount
    name: srw-orchestrator
    namespace: superhuman-remote-worker
roleRef:
  kind: Role
  name: srw-pod-manager
  apiGroup: rbac.authorization.k8s.io
```

Then reference the ServiceAccount in `deployment/20-orchestrator.yaml`:

```yaml
spec:
  template:
    spec:
      serviceAccountName: srw-orchestrator
```

#### 7. Workspace Storage

**No new storage needed.** The existing `srw-workspace` PVC (20Gi RWX via Longhorn, `deployment/13-workspace-pvc.yaml`) is already shared between orchestrator and agent pods. Dynamic pods mount the same PVC and write to `workspace/job_{uuid}/` — checkpoints, logs, and workspace files are all on shared storage.

This means:
- **Logs**: Written to `/workspace/logs/job_{id}.log` (accessible by orchestrator and cockpit)
- **Checkpoints**: Written to `/workspace/checkpoints/job_{id}.db` (resume works if a new pod is created)
- **Phase snapshots**: Written to `/workspace/phase_snapshots/job_{id}/` (recovery works)
- **Workspace files**: Standard `workspace/job_{uuid}/` directory

### Runtime Selection

A new `runtime` field in `config_override` (or at the expert/config level):

```yaml
# In agent config or config_override
runtime: pod      # "pod" | "vm" | "local"
                  # local = existing behavior (pre-registered agent process, default)

# Pod-specific resource overrides (optional)
pod:
  image: ghcr.io/knaeckebrothero/superhuman-remote-worker-agent:latest
  cpu_request: "250m"
  cpu_limit: "1000m"
  memory_request: "512Mi"
  memory_limit: "2Gi"
  timeout: 14400        # seconds, hard deadline
```

Default: `runtime: local` (backward compatible, no behavior change).

### Limitations Compared to Local Runtime

| Feature | Local (registered agent) | Pod runtime |
|---------|--------------------------|-------------|
| **Dispatch latency** | Immediate (agent already running) | ~10-20s (pod startup) |
| **Preemption** | Graceful pause via `POST /job/pause` | Not supported — cancel + re-queue instead |
| **Resume from pause** | Agent pauses mid-graph, resumes | Pod exits; new pod resumes from checkpoint |
| **Interactive commands** | Agent has persistent API server | No API server; heartbeat-only monitoring |
| **Cockpit "Open workspace"** | N/A (local workspace) | N/A (shared PVC, orchestrator can read files) |

Preemption is the main trade-off. The current preemption system (`_initiate_pause`, orchestrator/main.py:770) sends `POST /job/pause` to the agent's API server. Pod-mode agents don't run an API server. For priority preemption of pod-based jobs, the orchestrator would delete the K8s Job and re-queue the job — the agent resumes from checkpoint when a new pod is created.

### Scaling

Pod runtime scales naturally:

- **Up**: More jobs → more pods. Limited only by cluster resources and namespace ResourceQuotas.
- **Down**: No jobs → no pods → zero idle resource usage.
- **Burst**: 10 jobs created at once → 10 pods created in parallel. No waiting for agent registration.
- **Heterogeneous**: Different jobs can use different images, resource limits, and configs.
- **Node scaling**: Kubernetes Cluster Autoscaler or Karpenter can add nodes when pod demand exceeds capacity.

## What Changes, What Doesn't

| Component | Changes? | Details |
|-----------|----------|---------|
| `orchestrator/services/pod_provisioner.py` | **New** | ~150 lines, K8s Job CRUD |
| `orchestrator/main.py` (dispatcher) | **Moderate** | Refactor `_resolve_dispatch_payload` out of `_dispatch_job_to_agent`, add `_dispatch_job_as_pod`, add pod check in dispatcher loop |
| `orchestrator/main.py` (cancel) | **Small** | Add K8s Job deletion alongside existing agent cancel |
| `orchestrator/database/postgres.py` | **Small** | Add `dispatch_payload` column + `set_dispatch_payload()` method |
| `agent.py` | **Moderate** | New `--pod-mode` flag and `run_pod_mode()` function |
| `src/api/orchestrator_client.py` | **No change** | Already has heartbeat + completion reporting |
| `src/agent.py` (UniversalAgent) | **No change** | Already works with `process_job(job_id, metadata)` |
| Heartbeat/completion flow | **No change** | Pod-mode agents use the same `OrchestratorClient` |
| Cockpit UI | **No change** (initially) | Jobs look the same; optional: show runtime badge |
| `deployment/20-orchestrator.yaml` | **Small** | Add `serviceAccountName: srw-orchestrator` |
| `deployment/24-pod-runtime-rbac.yaml` | **New** | ServiceAccount + Role + RoleBinding |
| `config/schema.json` | **Small** | Add `runtime` enum and `pod` object |
| Existing secrets/configmaps | **No change** | Pod references existing `srw-secrets` + `srw-config` |
| Existing workspace PVC | **No change** | Pod mounts existing `srw-workspace` |
| VM provisioner | **No change** | VM and pod runtimes coexist independently |

## Comparison with Existing Runtimes

```
                  ┌─────────────────────────────────────────────┐
                  │              Orchestrator                    │
                  │                                             │
                  │  Dispatcher decides runtime per job:        │
                  │                                             │
                  │  runtime: local ──► POST /job/start to      │
                  │  (default)         registered agent          │
                  │                    (push dispatch)           │
                  │                                             │
                  │  runtime: pod ───► Store dispatch_payload    │
                  │                    + K8s Job API             │
                  │                    (pull dispatch, new)      │
                  │                                             │
                  │  runtime: vm ────► VM Provisioner            │
                  │                    (KubeVirt / NATS,         │
                  │                     existing)               │
                  └─────────────────────────────────────────────┘
```

## Implementation Plan

```
Phase 1: Dispatch payload refactor (orchestrator-side, no K8s yet)
  1. Add dispatch_payload JSONB column to jobs table
  2. Extract _resolve_dispatch_payload() from _dispatch_job_to_agent()
  3. Existing push dispatch still works (refactor, not rewrite)

Phase 2: Pod provisioner + dispatcher integration
  4. Create pod_provisioner.py with create/delete/status
  5. Add _dispatch_job_as_pod() and _job_wants_pod_runtime()
  6. Wire into dispatcher loop and cancel_job endpoint
  7. RBAC manifest + ServiceAccount on orchestrator deployment

Phase 3: Agent pod mode
  8. Add --pod-mode flag to agent.py
  9. Implement run_pod_mode(): read dispatch_payload, heartbeat, process, complete
  10. Handle SIGTERM gracefully (checkpoint + status update)

Phase 4: Config integration
  11. Add runtime + pod keys to config/schema.json
  12. Expert configs can set default runtime
  13. Cockpit: show runtime type in job detail view (optional)

Phase 5: Auto-fallback (optional)
  14. If no agents registered and pod provisioner available,
      auto-create pods for pending jobs (runtime: local → pod fallback)
  15. Resource quota management (namespace-level limits)
```

## Environment Variables

New variables (orchestrator-side only):

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_NAMESPACE` | `superhuman-remote-worker` | K8s namespace for agent pods |
| `AGENT_IMAGE` | `ghcr.io/knaeckebrothero/superhuman-remote-worker-agent:latest` | Default agent container image |
| `POD_RUNTIME_ENABLED` | unset | Set to `1` to enable pod runtime |
| `POD_TTL_AFTER_FINISHED` | `300` | Seconds before completed pods are cleaned up |
| `POD_ACTIVE_DEADLINE` | `14400` | Hard timeout for agent pods (4h default) |

Agent pods inherit all existing env vars from `srw-config` + `srw-secrets` (same as `deployment/21-agent.yaml`), plus:

| Variable | Description |
|----------|-------------|
| `JOB_ID` | Job UUID to process |
| `POD_MODE` | Set to `1` to activate pod mode |

## Open Questions

1. **Backoff limit**: Should failed pods retry? Currently set to `backoff_limit=0` (no retry) because the agent has its own error handling and checkpoint system. A pod failure likely means an infrastructure issue, not a transient error. Resume from checkpoint should be done via the cockpit "resume" action, which creates a new pod.

2. **Image per expert**: Should each expert config (`config/experts/*.yaml`) specify its own container image? e.g., `scholar` uses a lighter image without coding tools, `developer` gets a heavier one with build tools. This is a natural extension of the `pod.image` config key but adds image management overhead. For MVP, use the single existing agent image.

3. **Concurrent pod limit**: Should the orchestrator enforce a maximum number of concurrent agent pods? This could be done via K8s ResourceQuota (namespace-level) or in the dispatcher logic. Without a limit, a burst of job creation could overwhelm cluster resources.

4. **Graceful shutdown on SIGTERM**: The agent needs to handle SIGTERM to checkpoint before Kubernetes kills it. The current `run_single_job()` doesn't register signal handlers. Pod mode should catch SIGTERM, trigger a checkpoint, update job status to `paused`, and exit cleanly.

## Related Documents

- [VM-Based Agent Isolation](./vm.md) — KubeVirt VM runtime (heavier, full OS isolation)
- [Workspace Backend Abstraction](./vm_backend.md) — Remote workspace over SSH
- [NATS Messaging](./nats.md) — Cross-cluster communication (not needed for pod runtime)
- [Deployment README](../../deployment/README.md) — Current K8s deployment architecture
