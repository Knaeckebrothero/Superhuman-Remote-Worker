# Docker Compose Deployment Mode

**Status:** Implementing (Phase 1-4 complete)
**Last updated:** 2026-04-11
**Historical note:** An earlier plan (`docs/local_development.md`, now removed)
introduced a `--dev` flag that let the agent use its own filesystem as the
workspace. That escape hatch and the `LocalBackend` class it depended on have
both been deleted — production and dev now both go through SSH to a workspace
container. See "Production safety guard" below for the current design.

## Context

The system was heading toward Kubernetes-only deployment (`docs/deployment.md` Phase 3:
"Deprecate Docker Compose"). That decision is reversed. Docker Compose remains a
supported deployment tier alongside Kubernetes.

**Why:** Rebuilding and redeploying to k3s on every code change is prohibitively slow
for iterative development. Beyond developer ergonomics, Docker Compose is also a
legitimate deployment option for smaller setups that don't need dynamic scaling.

**The gap:** Three compose files already exist (`docker-compose.yaml`,
`docker-compose.dev.yaml`, `docker-compose.local.yaml`) with the full stack --
databases, orchestrator, agents, MinIO, NATS, Gitea, cockpit. But workspace and VM
provisioning requires Kubernetes. In Docker Compose mode, the orchestrator calls
`ContainerProvisioner.create_workspace()` which hits the k8s API and fails silently.
Agents then either get no workspace or fall back to `backend: local`, which uses the
agent container's own filesystem (see Security section below).

**Industry context:** Platforms like Coder, DevPod, and OpenHands all support multiple
backend providers (k8s, Docker, SSH). The common pattern is a provider abstraction
with explicit backend selection -- not runtime auto-detection. We diverge slightly by
auto-detecting, but the underlying architecture (provider interface with pluggable
implementations) is the same.


## Architecture: Two Deployment Modes

The orchestrator auto-detects its environment. No flags needed.

```
Orchestrator startup
  |
  +-- kubernetes python package importable?
  |     +-- yes --> try load_incluster_config() or load_kube_config()
  |     |            +-- success --> verify with API call (list namespaces)
  |     |            |                +-- success --> KUBERNETES MODE
  |     |            |                +-- failure --> DOCKER COMPOSE MODE (log warning)
  |     |            +-- failure --> DOCKER COMPOSE MODE
  |     +-- no  --> DOCKER COMPOSE MODE
  |
  +-- Log: "Running in {mode} mode — {reason}"
```

This detection already exists in `container_provisioner.py` (`K8S_AVAILABLE` flag +
`_init_k8s()`), `vm_provisioner.py`, and `persistent_provisioner.py`. The change is
making the orchestrator behave differently when k8s is unavailable, rather than
silently degrading.

**Important:** `load_kube_config()` can succeed even when pointing at a dead cluster
(stale kubeconfig). Always follow up with an actual API call (e.g., list namespaces)
to confirm connectivity. Also guard against partial k8s availability -- the API may be
reachable but the ServiceAccount may lack pod creation permissions. Fail loudly with
a clear error rather than silently falling back to Docker Compose mode mid-operation.

| Concern | Kubernetes | Docker Compose |
|---------|-----------|----------------|
| **Detection** | k8s API reachable + verified | k8s API unreachable |
| **Workspace containers** | Dynamic via `ContainerProvisioner` | Fixed services in compose, recycled between jobs |
| **VMs** | KubeVirt CRDs (NATS or direct API) | QEMU-in-Docker containers |
| **Worker agents** | Dynamic pods, auto-scaled | Fixed `deploy.replicas` in compose |
| **Persistent agents** | Pod-per-thread, on-demand | Fixed pool, reassigned between sessions |
| **Scheduling** | Dynamic provisioning + dispatch | Static 1:1 assignment (agent <-> workspace) |
| **Workspace lifecycle** | Create pod -> use -> snapshot to S3 -> delete pod | Use -> snapshot to S3 -> restart container -> reuse |
| **IDE sessions** | On-demand pod from S3 snapshot | Workspace already on the network; direct code-server access |
| **Service discovery** | k8s DNS + Services | Docker network DNS (`workspace-1:22`) |


## What Already Exists

### S3 Snapshot / Restore (complete)

The full workspace persistence pipeline is built and tested:

| Component | File | Status |
|-----------|------|--------|
| S3 client (boto3, MinIO) | `orchestrator/services/snapshot_service.py` | Production-ready |
| SSH tar capture (zstd) | `snapshot_service.py` `capture_vm_snapshot()` | Complete |
| Workspace suspension | `orchestrator/services/workspace_suspension.py` | Complete, 842 lines of tests |
| IDE session restore | `orchestrator/services/ide_session.py` | Complete, 3 restore paths |
| Phase snapshots | `src/core/phase_snapshot.py` | Complete, 608 lines of tests |
| GC with retention policy | `snapshot_service.py` `run_gc()` | Complete (soft-delete, 7-day grace) |
| Job resume integration | `orchestrator/main.py` lines ~3965, ~5352 | Wired into job lifecycle |

Key insight: the snapshot service works over SSH. It doesn't care whether the target
is a k8s pod or a Docker container -- it just needs a hostname and SSH access. This
means **workspace recycling in Docker Compose mode works out of the box** once the
orchestrator knows the container's hostname.

### Docker Compose Files (complete)

All three compose files include the full application stack. The `docker-compose.yaml`
already defines `agent-persistent` as a service (currently `replicas: 0`, started
on demand). Workspace containers just need to be added as compose services.

### Workspace Container Image (complete)

`docker/Dockerfile.workspace` builds an image with SSH, tmux, code-server, git,
Python, Node.js, Chromium, and dev tools. No sudo. Entrypoint runs sshd + code-server.
This image is used by `ContainerProvisioner` on k8s and would be used identically in
Docker Compose -- it doesn't know or care whether it's running in a k8s pod or a
Docker container.


## What Needs to Be Built

### 1. SSH Key Provisioning

Agents need SSH access to workspace containers. Use an init service in compose that
generates a keypair on first startup and shares it via a named volume:

```yaml
ssh-keygen:
  image: alpine:3
  container_name: srw-ssh-keygen
  command: >
    sh -c '
      if [ ! -f /keys/id_ed25519 ]; then
        apk add --no-cache openssh-keygen &&
        ssh-keygen -t ed25519 -f /keys/id_ed25519 -N "" -C "agent@srw" &&
        cat /keys/id_ed25519.pub > /keys/authorized_keys &&
        chmod 600 /keys/id_ed25519 &&
        chmod 644 /keys/authorized_keys;
      fi
    '
  volumes:
    - ssh_keys:/keys
  restart: "no"

volumes:
  ssh_keys:
    name: srw_ssh_keys
```

Workspace containers mount `authorized_keys` read-only:
```yaml
workspace-1:
  volumes:
    - ssh_keys:/home/agent-host/.ssh:ro
  depends_on:
    ssh-keygen:
      condition: service_completed_successfully
```

Agent containers mount the private key read-only:
```yaml
agent:
  volumes:
    - ssh_keys:/run/secrets/ssh:ro
  depends_on:
    ssh-keygen:
      condition: service_completed_successfully
```

**Gotcha -- file permissions and UIDs:** SSH is strict about key file permissions
(`600` for private key, `644` for `authorized_keys`). When sharing volumes between
containers with different UIDs, ensure the agent user and workspace user have matching
UIDs, or use an init step that copies and `chown`s the keys. On SELinux systems
(Fedora/RHEL), use the `:Z` suffix on bind mounts.

### 2. Workspace Container Services in Compose

Add fixed workspace containers to `docker-compose.yaml`. Use explicitly named services
(not `docker compose up --scale`) because `--scale` creates anonymous replicas that
cannot have per-instance volumes or configuration.

```yaml
workspace-1:
  image: ghcr.io/knaeckebrothero/superhuman-remote-worker-workspace:${IMAGE_TAG:-latest}
  container_name: srw-workspace-1
  environment:
    WORKSPACE_ID: "1"
  volumes:
    - ssh_keys:/home/agent-host/.ssh:ro
  healthcheck:
    test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/localhost/22"]
    interval: 10s
    timeout: 5s
    retries: 5
    start_period: 15s
  depends_on:
    ssh-keygen:
      condition: service_completed_successfully
  restart: unless-stopped

workspace-2:
  # ... same pattern, different container_name
```

The number of workspace containers matches the number of agents. For the default
setup (2 workers + 1 persistent), 3 workspace containers.

**Service discovery:** Set `WORKSPACE_HOSTS=workspace-1,workspace-2,workspace-3` in
the orchestrator environment. This is simpler and more reliable than DNS probing
(which fails if a workspace container is temporarily down during startup). Docker
Compose's embedded DNS resolves these service names to container IPs automatically.

**Startup ordering:** Use `depends_on` with `condition: service_healthy` so the
orchestrator doesn't attempt workspace assignment before containers are ready:
```yaml
orchestrator:
  environment:
    WORKSPACE_HOSTS: ${WORKSPACE_HOSTS:-workspace-1,workspace-2,workspace-3}
    VM_HOSTS: ${VM_HOSTS:-}
  depends_on:
    workspace-1:
      condition: service_healthy
    workspace-2:
      condition: service_healthy

agent:
  deploy:
    replicas: ${WORKER_REPLICAS:-2}
  volumes:
    - ssh_keys:/run/secrets/ssh:ro   # SSH private key for workspace access
    - workspace_data:/workspace:z
    - agent_home:/home/srw
  depends_on:
    ssh-keygen:
      condition: service_completed_successfully

agent-persistent:
  deploy:
    replicas: ${PERSISTENT_REPLICAS:-1}  # Fixed pool (was replicas: 0)
  volumes:
    - ssh_keys:/run/secrets/ssh:ro
    - workspace_data:/workspace:z
    - agent_persistent_home:/home/srw
  depends_on:
    ssh-keygen:
      condition: service_completed_successfully
```

### 3. Orchestrator: Docker Compose Provisioner

When k8s is unavailable, the orchestrator needs a different code path for workspace
assignment. The static pool model is recommended -- no Docker API needed.

```python
# orchestrator/services/docker_provisioner.py

class DockerProvisioner:
    """Workspace assignment for Docker Compose mode.

    Unlike ContainerProvisioner (which creates/deletes k8s pods on demand),
    this provisioner works with pre-existing containers defined in the
    compose file. It assigns available workspaces to jobs and recycles
    them after job completion.

    Follows the same interface as ContainerProvisioner where possible
    so the orchestrator dispatch logic can use either interchangeably.
    """

    def __init__(self, postgres_db, snapshot_service):
        self.db = postgres_db
        self.snapshot_service = snapshot_service
        # Workspace hostnames from WORKSPACE_HOSTS env var
        self.workspace_hosts: list[str] = self._load_hosts()
        self.is_available = len(self.workspace_hosts) > 0

    def _load_hosts(self) -> list[str]:
        """Load workspace hostnames from environment."""
        hosts_str = os.environ.get("WORKSPACE_HOSTS", "")
        return [h.strip() for h in hosts_str.split(",") if h.strip()]

    async def assign_workspace(self, job_id: str) -> dict | None:
        """Find a free workspace and assign it to this job.

        Checks jobs.context.workspace_container in DB to find which
        workspaces are currently in use. Returns the first free one.

        Returns: {"host": "workspace-1", "port": 22, "status": "ready"}
        Returns None if all workspaces are in use.
        """
        ...

    async def release_workspace(self, job_id: str):
        """Release workspace back to the pool after job completion.

        1. Snapshot workspace to S3 (via existing SnapshotService)
        2. Reset workspace (see Workspace Lifecycle section)
        3. Clear workspace_container from job context in DB
        """
        ...
```

**Why not Docker API?** Using the Docker SDK (`docker.from_env()`) to create/delete
containers on demand would mimic k8s behavior more closely. But it adds complexity
(socket mounting, permission management) and isn't necessary -- the fixed-count model
is simpler and sufficient for Docker Compose deployments. The orchestrator tracks
assignment state in PostgreSQL (same `jobs.context.workspace_container` JSONB field
used by `ContainerProvisioner`), not in Docker.

### 4. Orchestrator: Mode-Aware Dispatch

The job dispatch path in `orchestrator/main.py` (lines ~655-694) currently:
1. Checks `_job_needs_container()`
2. If yes, provisions via `ContainerProvisioner`
3. Injects `config_override.workspace.backend = "remote"` with pod IP

In Docker Compose mode, this becomes:
1. Call `DockerProvisioner.assign_workspace(job_id)`
2. Get back the workspace hostname (e.g., `workspace-1`)
3. Inject `config_override.workspace.backend = "remote"` with that hostname

The agent code doesn't change at all -- it receives `backend: remote` + host/port
and SSHes into the workspace container, same as in k8s mode. Docker Compose DNS
resolves `workspace-1` to the container's IP.

**Gotcha -- DNS caching:** Docker's embedded DNS has a short TTL, but if the agent
caches a resolved IP (e.g., in a paramiko SSH connection), it may hold a stale IP
after a workspace container restart. Always resolve the hostname fresh for each new
SSH connection. Avoid caching workspace IPs in the database -- store the hostname.

### 5. Persistent Agent Pool with Reconfiguration

**Current state:** Persistent agents are 1:1 with threads. The orchestrator creates
a k8s pod per thread (`persistent_provisioner.py`). When the thread ends, the pod is
deleted. No reuse.

**Docker Compose mode:** Fixed number of persistent agents defined in compose
(`deploy.replicas: N`). They need to be assignable to threads and reconfigurable
between sessions.

**Industry context:** No major LLM agent platform (OpenHands, Devin, SWE-agent)
reuses a process across sessions. They either destroy the environment (container per
session) or keep it alive indefinitely (persistent VM). Our approach -- reuse the
process, swap the session -- is a third model that requires explicit state management.

The key insight from OpenHands' V1 SDK: agent configuration should be immutable and
serializable. All mutable state should live in a single disposable `SessionContext`
object. This makes teardown a single reference swap rather than resetting 15 fields.

#### Architecture: Singleton vs Session-Scoped State

```
+---------------------------------------------------+
|  Agent Process (long-lived, Docker container)      |
|                                                    |
|  Singleton Layer (lives with process):             |
|  - DB connections (postgres_conn)                  |
|  - LLM client pools (KeyRing)                     |
|  - Base config snapshot (_base_config)             |
|  - Tool registry (metadata only)                   |
|  - WebSocket server                                |
|                                                    |
|  Session Layer (created/destroyed per session):    |
|  - SessionContext {                                 |
|      config: merged config for this thread         |
|      workspace: WorkspaceManager                   |
|      shell: ShellManager                           |
|      tools: bound tool instances                   |
|      context: ContextManager                       |
|      checkpoint: AsyncSqliteSaver                  |
|      messages: List[BaseMessage]                   |
|      thread_id: str                                |
|    }                                               |
|                                                    |
|  Lifecycle:                                        |
|  1. Process boots -> init singleton layer          |
|  2. Register with orchestrator as "available"      |
|  3. Orchestrator assigns thread -> create Session  |
|  4. Interactive loop runs against SessionContext    |
|  5. Session ends -> snapshot, destroy Session,     |
|     return to "available"                          |
|  6. Orchestrator assigns next thread -> goto 3     |
+---------------------------------------------------+
```

This pattern follows Celery's `task_prerun` / `task_postrun` signal model: per-task
setup and teardown around a long-lived worker process. The existing `_base_config`
reset pattern in `src/agent.py` already works this way for worker agents -- extend it
to persistent agents.

**Safety valve:** Add a `max_sessions_per_process` counter (like Celery's
`max_tasks_per_child`). After N sessions, the agent process restarts to guard against
state leakage. In compose, `restart: unless-stopped` handles the restart automatically.

#### Required changes:

**a) Agent: session detach/attach**

When a thread ends:
1. Snapshot workspace to S3 (via existing flow)
2. Tear down `SessionContext` (close workspace, kill shell, clear messages)
3. Deregister from thread (set `agent_id = NULL` on thread)
4. Report status `available` in heartbeat

When a new thread is assigned:
1. Receive thread config from orchestrator (via REST or WebSocket)
2. Create new `SessionContext` with merged config
3. Restore workspace from S3 if resuming, or use fresh workspace
4. Bind to thread (set `agent_id` on thread)
5. Begin interactive loop

**b) Config hot-reload via WebSocket**

New `config.update` message type:
```json
{"method": "config.update", "config_override": {"llm": {"model": "..."}}}
```

The WebSocket handler holds a reference to the `SessionContext`. Reconfiguration
replaces that reference with a new instance. The WebSocket connection itself stays
alive -- it's the transport layer, decoupled from session state.

**c) Orchestrator: agent pool management**

When a user creates a thread:
1. Query agents where `agent_mode = "persistent"` and status = `available`
2. Pick one (round-robin or least-recently-used)
3. Send assignment via agent's REST API: `POST /session/attach {thread_id, config}`
4. Update `threads.agent_id`

When a thread ends:
1. Send detach signal: `POST /session/detach`
2. Agent snapshots, tears down session, returns to available
3. Clear `threads.agent_id`


### 6. Workspace Lifecycle: Recycle Between Jobs

After a job completes in Docker Compose mode:

```
Job completes
  |
  +-- Snapshot workspace to S3 (existing: snapshot_service.capture_vm_snapshot)
  |     +-- SSH into workspace container, tar /home/agent-host/, upload to S3
  |
  +-- Reset workspace for next job
  |     +-- Container restart (recommended)
  |           docker compose restart workspace-1
  |           Entrypoint re-initializes sshd + code-server
  |           ~5-10 seconds downtime
  |
  +-- Mark workspace as available in DB
```

**Why container restart over SSH cleanup:** Docker's OverlayFS destroys the
container's writable layer on restart, returning the filesystem to the image's clean
state. Named volumes (like SSH keys) survive. This catches everything an SSH cleanup
script would miss: dotfiles, installed packages, modified system configs, shell
history, tmux sessions, /tmp contents. The 5-10 second restart cost is worth the
guarantee of a clean slate.

**How to restart from the orchestrator:** The orchestrator doesn't need Docker API
access. SSH into the workspace, run `kill 1` (sends SIGTERM to the entrypoint/PID 1),
and Docker's `restart: unless-stopped` policy automatically restarts the container.
Or use the Docker API via socket mount if cleaner lifecycle control is needed.

For persistent sessions (save/restore):
```
Session ends
  |
  +-- Snapshot to S3 (full workspace state)
  +-- Restart workspace container (clean slate)
  +-- Agent returns to idle pool

Session resumes (or new session needs existing state)
  |
  +-- Assign idle agent + workspace
  +-- Download snapshot from S3
  +-- Extract to workspace via SSH (existing: ide_session._extract_snapshot_to_vm)
  +-- Agent connects, resumes
```


## VMs in Docker Compose Mode

### Problem

In production, KubeVirt provisions full VMs (Ubuntu 24.04, systemd, management daemon,
sudo approval gate, Tailscale, code-server). The `vm_provisioner.py` speaks either
NATS (cross-cluster) or KubeVirt API (same-cluster). Neither works without k8s.

### Solution: QEMU-in-Docker

Run the existing qcow2 VM image inside a Docker container using QEMU/KVM. The
[qemus/qemu](https://github.com/qemus/qemu) project (1.7k+ stars, actively
maintained) provides a mature Docker wrapper around QEMU that supports direct qcow2
boot, SSH forwarding, and Docker Compose.

```yaml
agent-vm-1:
  image: qemux/qemu
  container_name: srw-agent-vm-1
  devices:
    - /dev/kvm
    - /dev/net/tun
  cap_add:
    - NET_ADMIN
  environment:
    BOOT: /disk/agent-vm-base.qcow2
    RAM_SIZE: ${VM_RAM:-4G}
    CPU_CORES: ${VM_CPUS:-4}
    DISK_FMT: qcow2
    DISK_IO: native
    DISK_CACHE: none
  volumes:
    - ./docker/agent-vm-base/output/agent-vm-base.qcow2:/disk/agent-vm-base.qcow2:ro
    - vm_1_disk:/storage
  healthcheck:
    test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/localhost/22"]
    interval: 5s
    timeout: 3s
    retries: 20
    start_period: 60s
  stop_grace_period: 2m
  restart: unless-stopped
```

**Why this works:**
- The existing Packer-built `agent-vm-base.qcow2` boots unchanged -- same image for
  KubeVirt and Docker Compose. Zero image divergence.
- Full VM with its own kernel: systemd, management daemon, sudo gate, Tailscale,
  code-server all work identically to production.
- SSH access from agent containers via Docker network DNS (`agent-vm-1:22`).
  Uses user-mode (SLIRP) networking by default, which is fine for SSH and light
  traffic (~1 Gbps throughput ceiling).
- Docker Compose integration is straightforward.

**Requirement:** Linux host with `/dev/kvm` (hardware virtualization). This is
standard for development machines and bare-metal servers. It does **not** work on
macOS Docker Desktop or most cloud VPS without nested virtualization. Cloud providers
with nested virt: GCP (N1/N2/C2, must enable), Azure (Dv3+), AWS (`.metal` instances
only), Hetzner (dedicated servers only).

### QEMU-in-Docker: Operational Notes

**Startup time:** A full Ubuntu VM takes 15-45 seconds to reach SSH-ready. The health
check above uses `start_period: 60s` with aggressive retries (every 5s, 20 attempts)
to handle this. Sibling containers should use `depends_on: service_healthy`.

**Graceful shutdown:** Set `stop_grace_period: 2m` so the guest OS can shut down
cleanly before Docker kills the container. The default 10s is too short for systemd.

**Disk performance:** `DISK_IO: native` enables O_DIRECT, `DISK_CACHE: none` avoids
double-caching. On COW filesystems (Btrfs, ZFS), disable COW on the storage directory
with `chattr +C /path/to/vm_storage/` to avoid severe fragmentation.

**VM reset between jobs:** Use qcow2 overlay snapshots for instant reset:
```bash
# One-time: base image is read-only
# Per-job: create a thin overlay (~200KB initial, instant)
qemu-img create -f qcow2 -b agent-vm-base.qcow2 -F qcow2 job-overlay.qcow2
# After job: delete overlay, create fresh one
```
All writes go to the overlay; the base image stays clean. Multiple VMs can share one
base image with independent overlays. This is faster than S3 snapshot/restore for
simple resets.

**Networking gotcha:** Docker's iptables rules (FORWARD chain DROP) can break QEMU
bridge networking. Symptom: VM boots but has no network. Fix: set
`net.bridge.bridge-nf-call-iptables = 0` on the host, or stick with user-mode
networking (default, works out of the box for SSH).

**Do not use `--privileged`:** Use specific device passthrough (`/dev/kvm`,
`/dev/net/tun`) and `cap_add: NET_ADMIN` instead. Full privileged mode weakens
Docker's isolation.

### Alternatives considered

| Solution | Verdict | Why |
|----------|---------|-----|
| Kata Containers | Poor fit | Docker Compose networking is [broken](https://github.com/kata-containers/kata-containers/issues/11767) with Kata runtime |
| Firecracker | Wrong surface | No Docker Compose support; own API, own rootfs format. Best-in-class isolation but requires custom orchestration |
| Weave Ignite | Dead | Weaveworks shut down Feb 2024, repo archived |
| Incus/LXD | Separate ecosystem | Own orchestrator, no Docker Compose integration |
| Privileged container + s6 | Viable fallback | Lighter, works on macOS. But: no sudo gate, no real kernel isolation, creates dev/prod divergence |

### VM provisioner fallback

The `VmProvisioner` needs a Docker Compose mode path:

```python
# vm_provisioner.py -- extended selection logic
#
# NATS_URL set + nats-py installed       -> NATS mode (cross-cluster KubeVirt)
# No NATS, K8s + VM template found       -> Direct K8s mode (same-cluster KubeVirt)
# No NATS, no K8s, QEMU containers found -> Docker Compose mode (QEMU-in-Docker)
# None of the above                      -> VMs disabled
```

In Docker Compose mode, "provisioning a VM" means assigning one of the pre-existing
QEMU containers (same static pool model as workspace containers). VM reset uses qcow2
overlays rather than pod deletion/recreation.


## Configuration & Secrets

### How it works

Docker Compose natively reads `.env` from the project root. All compose files already
use `${VAR:-default}` syntax (178 references in `docker-compose.yaml`). The pattern:

| Deployment tier | Config source | Secrets source |
|----------------|---------------|----------------|
| **Docker Compose** | `.env` file (auto-loaded by compose) | Same `.env` file |
| **K8s single-cluster** | `.env` -> `create-secrets.sh` -> K8s Secrets + ConfigMaps | Same flow |
| **K8s production** | Fleet GitOps (ConfigMaps in `deployment/`) | Vault -> External Secrets Operator -> K8s Secrets |

No new secrets infrastructure is needed for Docker Compose mode. New variables go into
`.env.example` (committed, template) and `.env` (gitignored, actual values).

### New environment variables

Added to `.env.example` under "Docker Compose Mode -- Workspace & VM Pool":

```bash
# Workspace container hostnames (comma-separated, must match compose services)
# WORKSPACE_HOSTS=workspace-1,workspace-2,workspace-3

# Agent replica counts
# WORKER_REPLICAS=2
# PERSISTENT_REPLICAS=1

# VM settings (QEMU-in-Docker, requires /dev/kvm)
# VM_REPLICAS=0
# VM_RAM=4G
# VM_CPUS=4
# VM_DISK_SIZE=64G
# VM_HOSTS=agent-vm-1
```

### How variables flow into compose services

```yaml
# Orchestrator reads workspace pool from env
orchestrator:
  environment:
    WORKSPACE_HOSTS: ${WORKSPACE_HOSTS:-workspace-1,workspace-2,workspace-3}
    VM_HOSTS: ${VM_HOSTS:-}

# Agent replicas from env
agent:
  deploy:
    replicas: ${WORKER_REPLICAS:-2}

agent-persistent:
  deploy:
    replicas: ${PERSISTENT_REPLICAS:-1}

# VM resources from env
agent-vm-1:
  environment:
    RAM_SIZE: ${VM_RAM:-4G}
    CPU_CORES: ${VM_CPUS:-4}
    DISK_SIZE: ${VM_DISK_SIZE:-64G}
```

### Comparison with Kubernetes secrets

In k8s, the agent's SSH private key is a Secret (`vm-ssh-key`) mounted as a volume.
In Docker Compose, the equivalent is the `ssh_keys` named volume populated by the
init service (see SSH Key Provisioning above). Same content, different delivery
mechanism.

| Secret | Kubernetes | Docker Compose |
|--------|-----------|----------------|
| SSH key (workspace access) | K8s Secret `vm-ssh-key` | Named volume `ssh_keys` via init container |
| DB passwords | Vault -> ESO -> K8s Secret | `.env` file |
| API keys (LLM, search) | Vault -> ESO -> K8s Secret | `.env` file |
| Keycloak OIDC secrets | Vault -> ESO -> K8s Secret | `.env` file |
| MinIO/S3 credentials | Vault -> ESO -> K8s Secret | `.env` file |
| Gitea admin credentials | Vault -> ESO -> K8s Secret | `.env` file |

The `.env` file must be treated as sensitive. It is gitignored. The `.env.example`
template is committed with placeholder values.


## Security: The Local Backend Problem

While designing this, we discovered a safety gap in the current architecture.

`config/defaults.yaml` historically shipped with `workspace.backend: local`.
In production, the orchestrator overrode this to `remote` after provisioning
a workspace, but if the override failed to apply (race condition, config bug,
orchestrator restart) the agent would silently fall back to `LocalBackend` --
using the **agent pod's own filesystem** as the workspace.

This meant:
- LLM shell commands executed inside the agent pod (no isolation)
- `sandbox_cwd` was only a `cd`, not a real security boundary
- No separate container constraining the agent process

### Fix

1. **`LocalBackend` removed entirely**, along with the `--dev` flag and all
   CLI job-submission flags (`--description`, `--job-id`, `--resume`, etc.).
   The agent is only ever run as a server; jobs come in via the orchestrator.
2. **Schema rejects `backend: local`**: `WorkspaceConfig.__post_init__`
   raises `ValueError` if the config loads with `backend="local"`. There is
   no escape hatch.
3. **Agent refuses non-remote backends**: `src/agent.py` `process_job()`
   raises `RuntimeError` unless `backend == "remote"` and SSH credentials
   are present. Production and dev both go through SSH to a workspace
   container.
4. **Docker Compose mode**: agents always get `backend: remote` pointing
   to a workspace container in the static pool, same as k8s mode.


## Implementation Status

All core phases are implemented. Remaining work is integration testing and polish.

### Phase 1: Workspace containers in compose + orchestrator auto-detect — DONE

| What | Where |
|------|-------|
| SSH keygen init service (`ssh-keygen`) | `docker-compose.yaml`, `docker-compose.local.yaml` |
| 3 workspace containers (`workspace-1/2/3`) | Both compose files |
| SSH key volume mounts (agents + workspaces) | `ssh_keys` named volume, `:ro` mounts |
| `WORKSPACE_HOSTS` / `VM_HOSTS` env vars | Orchestrator environment in both compose files |
| `DockerProvisioner` class | `orchestrator/services/docker_provisioner.py` |
| Mode detection + routing + logging | `orchestrator/main.py` (startup + dispatch loop) |
| K8s API call verification | `container_provisioner.py` `_init_k8s()` — `list_namespace()` after config load |
| Workspace release on job completion | `orchestrator/main.py` — 2 cleanup paths updated |
| Thread workspace cleanup | `orchestrator/main.py` — thread deletion path updated |
| Tests | `tests/test_docker_provisioner.py` — 15 tests |

### Phase 2: Production safety guard — DONE (and later hardened)

| What | Where |
|------|-------|
| Default changed to `backend: remote` | `config/defaults.yaml` |
| Schema rejects `backend: local` | `src/core/loader.py` `WorkspaceConfig.__post_init__` |
| Hard refusal guard | `src/agent.py` `process_job()` — raises unless `backend == "remote"` with SSH creds |
| `LocalBackend` class deleted | `src/core/backends/` (test-only `FilesystemTestBackend` in `tests/_fs_backend.py`) |

### Phase 3: Persistent agent pool — DONE

| What | Where |
|------|-------|
| `_attach_session()` / `_detach_session()` helpers | `src/api/persistent_app.py` |
| Pool mode lifespan (no thread_id at startup) | `src/api/persistent_app.py` lifespan |
| `POST /session/attach` endpoint | `src/api/persistent_app.py` |
| `POST /session/detach` endpoint | `src/api/persistent_app.py` |
| `MAX_SESSIONS_PER_PROCESS` safety valve | `src/api/persistent_app.py` — `sys.exit(0)` after N sessions |
| Heartbeat reports "available" when idle | `src/api/persistent_app.py` lifespan |
| `_find_idle_persistent_agent()` | `orchestrator/main.py` |
| `_send_session_attach()` | `orchestrator/main.py` |
| Thread creation assigns pool agents | `orchestrator/main.py` `create_thread()` |
| Compose replicas from env var | `docker-compose.yaml` — `PERSISTENT_REPLICAS` |

### Phase 4: VM support in compose — DONE

| What | Where |
|------|-------|
| QEMU VM service template (commented) | `docker-compose.yaml` — `agent-vm-1` with `qemux/qemu` |
| VM provisioner Docker Compose fallback | `orchestrator/services/vm_provisioner.py` — `mode: "docker"` |
| VM pool assignment via `DockerProvisioner` | `docker_provisioner.py` `assign_vm()` |

### Phase 5: Polish — REMAINING

| Task | Status |
|------|--------|
| Documentation updates | Done (this update) |
| Compose profiles for optional services | Not started |
| Health monitoring (pool status API) | Not started |
| First-run setup guide | Not started |
| macOS fallback docs | Not started |
| Integration testing (full job lifecycle) | Not started |


## Best Practices (from research)

Collected from Coder, DevPod, OpenHands, Docker Sandboxes, and Spacelift:

1. **Separate persistent storage from ephemeral compute.** SSH keys and config in
   named volumes (survive restart). Workspace files in the container's writable layer
   (destroyed on restart). This is the universal pattern across all workspace platforms.

2. **Track assignments by immutable job ID, not hostname.** Store `job_id -> hostname`
   in the database, not `hostname -> current state`. This prevents races if a workspace
   is released and reassigned simultaneously.

3. **Use hostnames, not IPs.** Docker Compose DNS resolves service names to container
   IPs. After a container restart, the IP may change. If paramiko or any SSH client
   caches the resolved IP, the next connection fails. Always resolve fresh.

4. **Log the deployment mode decision at startup.** When something goes wrong, the
   first question is always "which mode is the orchestrator running in?" Make it
   obvious in the logs.

5. **Fail fast on partial k8s availability.** If the k8s API is reachable but the
   ServiceAccount can't create pods, throw at startup -- don't discover this when the
   first job arrives.

6. **Signal handling matters.** When restarting workspace containers, the entrypoint
   receives SIGTERM. Ensure sshd and code-server handle it gracefully. The standard
   `sshd` binary does; custom wrapper scripts must propagate signals.


## Open Questions

1. **How many workspaces/VMs in compose?** Default to matching agent count (1:1). Make
   configurable via env vars (`WORKER_REPLICAS`, `PERSISTENT_REPLICAS`,
   `VM_REPLICAS`).

2. **Persistent agent idle signaling:** How does an idle persistent agent advertise
   availability? Options: heartbeat field (`idle: true`), separate agent status
   (`available`), or orchestrator polls agent health endpoint. The heartbeat field is
   simplest and consistent with the existing heartbeat mechanism.

3. **Mixed mode:** Can a Docker Compose deployment use a remote k8s cluster for
   overflow? Not in scope for v1, but the static pool model doesn't prevent it later.

4. **macOS development:** QEMU-in-Docker requires `/dev/kvm` (Linux). macOS
   developers can't use VM workspaces in compose. Fallback: use workspace containers
   only (no VMs), or run compose on a remote Linux machine via Docker context.

5. **Workspace container restart vs Docker API:** The current design avoids needing
   the Docker socket mounted into the orchestrator. If cleaner lifecycle control is
   needed later (e.g., real `docker restart` instead of `kill 1`), mounting
   `/var/run/docker.sock` is an option but adds a security surface.


## Related Documents

- `docs/deployment.md` -- Deployment strategy (updated to reference this doc)
- `docs/features/vm_snapshots_and_ide.md` -- S3 snapshot architecture
- `docs/features/vm_backend.md` -- VM workspace design
- `docs/features/sessions.md` -- Persistent agent session architecture
