# Local Development Mode

> **Note:** This document has been largely superseded by
> [`docs/docker_compose_mode.md`](docker_compose_mode.md), which implements a full
> Docker Compose deployment tier with workspace containers, persistent agent pools,
> and QEMU-in-Docker VMs.
>
> **What has been implemented from this design:**
> - `config/defaults.yaml` now defaults to `workspace.backend: remote` (not `local`)
> - The agent refuses `backend: local` in production — a hard guard raises
>   `RuntimeError` unless `--dev` is set
> - `agent.py --dev` sets `DEV_MODE=1` to allow local workspace in bare-metal development
> - The registration payload includes `dev_mode: true` when `--dev` is active
>
> The `--dev` flag is for running `python agent.py` directly on a developer's
> machine without any containers. For containerized local development, use
> Docker Compose (`podman-compose up -d`) — the orchestrator auto-detects the
> environment and assigns workspace containers from a static pool.

## Problem

End-to-end testing of the agent system currently requires a Kubernetes cluster.
The orchestrator provisions workspace containers (or KubeVirt VMs) via the k8s
API, and the agent connects to them over SSH using `RemoteBackend`. Rebuilding
and redeploying to k3s on every code change is prohibitively slow for iterative
development.

Running the agent locally with `python agent.py --port 8001` partially works —
the agent starts, registers with the orchestrator, and accepts jobs — but the
orchestrator still provisions a workspace container on the cluster and injects
`config_override.workspace.backend = "remote"` with the pod's cluster-internal
IP (e.g. `10.42.0.24`). That IP is unreachable from the developer's machine, so
the job fails immediately on SSH connect.

### Security issue: silent local-filesystem fallback (RESOLVED)

While investigating this, we discovered a safety gap in the architecture.

The agent config (`config/defaults.yaml`) previously shipped with
`workspace.backend: local` as the default. In production this was always
overridden to `remote` by the orchestrator after provisioning a container.
However, if the override failed to apply, the agent silently fell back to
`LocalBackend` — using the **pod's own filesystem** as the workspace.

**This has been fixed:**
- `config/defaults.yaml` now defaults to `workspace.backend: remote`
- The agent refuses `backend: local` unless `DEV_MODE=1` (set by `--dev` flag)
- A `RuntimeError` is raised with clear instructions if the guard triggers
- Both Docker Compose and Kubernetes modes always inject `backend: remote`


## Solution

### 1. Agent: remove the local-filesystem fallback, add `--dev` flag

**Remove the silent fallback.** In production (the default), the agent must
always receive a workspace backend from the orchestrator (`remote` pointing to
a provisioned container or VM). If `workspace.backend` resolves to `local`
without `--dev`, the agent refuses the job with a clear error.

**Add `--dev` flag** to `agent.py` (applies to both `worker` and `persistent`
modes). When set:

- `workspace.backend: local` is allowed — the agent uses the local filesystem
  via `LocalBackend` (workspace at `./workspace/job_<uuid>/`).
- Shell commands run in local tmux (no container isolation).
- The orchestrator's `config_override.workspace` is ignored — the agent forces
  `backend: local` regardless of what the orchestrator sends.
- The agent registers with `dev_mode: true` so the orchestrator knows not to
  provision infrastructure for it.

```bash
# Dev: local workspace, no container needed
python agent.py --dev --port 8001

# Dev with specific config
python agent.py --dev --config experts/developer --port 8001

# Production (default): refuses to run without a remote workspace
python agent.py --port 8001
```

#### Changes required

| File | Change |
|------|--------|
| `agent.py` | Add `--dev` argument to `parse_args()` |
| `agent.py` | Pass `dev_mode` flag through to `UniversalAgent` |
| `src/agent.py` | Store `dev_mode` flag on the agent instance |
| `src/agent.py` `process_job()` | If `backend == "local"` and not `dev_mode` → raise error, refuse job |
| `src/agent.py` `process_job()` | If `dev_mode` → force `backend = "local"`, skip remote config |
| `src/api/orchestrator_client.py` | Include `dev_mode: true` in registration payload |
| `config/defaults.yaml` | Change `workspace.backend` default to `remote` (or remove it — the orchestrator must always provide it) |

### 2. Orchestrator: add `--dev` flag for local scheduling

The orchestrator needs its own dev mode to handle the other side of the problem:
in production, it provisions workspace containers and VMs via k8s, schedules
agent pods, and manages their lifecycle. None of this works without a cluster.

**Add `--dev` flag** to the orchestrator startup:

```bash
# Dev: skip container/VM provisioning, work with locally-registered agents
uvicorn orchestrator.main:app --reload --port 8085  # + DEV_MODE=1 env var

# Production (default): full k8s provisioning
uvicorn orchestrator.main:app --port 8085
```

When `DEV_MODE` is set, the orchestrator must handle:

| Concern | Production | Dev mode |
|---------|-----------|----------|
| **Workspace containers** | Provisioned via k8s API | Skipped — agents use local filesystem |
| **VMs (KubeVirt)** | Provisioned via NATS/KubeVirt | Skipped |
| **Agent instances** | k8s deployments, auto-scaled | Manually started by developer |
| **Job dispatch** | Injects `backend: remote` + pod IP | Does **not** inject workspace override — agent uses its own `--dev` default |
| **Workspace file access** | Reads from container via SSH | **TBD** — needs a solution (see below) |
| **IDE sessions (code-server)** | Port-forward to workspace container | **TBD** |

#### Open questions (need design)

The orchestrator currently does more than just dispatch jobs. Several features
rely on being able to reach the workspace container:

1. **Workspace file browser** — The cockpit reads workspace files via the
   orchestrator's `WorkspaceService`, which SSH's into the container. In dev
   mode, the workspace lives on the developer's machine. The orchestrator would
   need an alternative path (e.g. the agent exposes a file-read endpoint, or
   the orchestrator reads from the local filesystem directly).

2. **IDE sessions** — `code-server` runs inside the workspace container and is
   accessed via port-forward. In dev mode, the workspace is a local directory —
   the developer can just open it in their editor. This may not need a
   replacement at all.

3. **Container lifecycle hooks** — Suspension, recovery, and cleanup are tied
   to k8s pod lifecycle (delete pod, retain PVC, recreate pod). In dev mode,
   there's nothing to suspend or recover. Jobs can simply pause/resume with
   the workspace directory intact on disk.

4. **Git delivery** — In production, workspace containers have Gitea repos
   provisioned for them, and the agent pushes deliverables there. In dev mode,
   the workspace is already a local git repo. The orchestrator could read
   results directly, or we skip git delivery entirely.

5. **Multi-agent scheduling** — In production, the orchestrator assigns jobs
   to available agent pods. In dev mode, there's typically one manually-started
   agent. The orchestrator should still dispatch to it normally (the agent
   registers via heartbeat), but features like agent affinity, capacity
   planning, or spinning up new agent instances don't apply.

These need to be solved before `--dev` on the orchestrator is fully functional.
The agent-side `--dev` flag can be implemented independently — it only requires
the orchestrator to not inject a `workspace.backend: remote` override, which
a dev-mode agent already ignores anyway.


## Implementation status

1. **Agent `--dev` flag** — **DONE.** `agent.py --dev` sets `DEV_MODE=1`,
   allowing `backend: local`. Registration payload includes `dev_mode: true`.

2. **Agent production guard** — **DONE.** `config/defaults.yaml` defaults to
   `backend: remote`. Hard refusal in `src/agent.py` `_setup_job_workspace()`
   raises `RuntimeError` if `backend == "local"` without `DEV_MODE`.

3. **Orchestrator Docker Compose mode** — **DONE.** The orchestrator auto-detects
   whether k8s is available. When it's not, `DockerProvisioner` assigns workspace
   containers from a static pool defined by `WORKSPACE_HOSTS`. The orchestrator
   `--dev` flag is no longer needed — Docker Compose mode handles it.
   See [`docs/docker_compose_mode.md`](docker_compose_mode.md) for full details.
