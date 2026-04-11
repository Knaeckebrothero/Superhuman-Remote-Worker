# Local End-to-End Testing

## Problem

The full system cannot be tested end-to-end on a local machine. The core job
workflow (create, assign, execute, return results) works locally via
`docker-compose.dev.yaml` or `docker-compose.local.yaml`, but the **VM
workspace path** — where an agent receives an isolated virtual machine instead
of a shared PVC — is only testable on the k3s cluster.

This means changes to the VM lifecycle, management daemon, sudo gate, or
Headscale networking can only be validated after deploying to the cluster,
which slows iteration and makes debugging difficult.

## Scope

### What already works locally

| Component              | How                                                  |
|------------------------|------------------------------------------------------|
| PostgreSQL + pgvector  | Container in compose                                 |
| MongoDB                | Container in compose                                 |
| Neo4j                  | Container in compose                                 |
| NATS (JetStream)       | Single-node container in compose                     |
| Gitea                  | Container in compose (SQLite mode)                   |
| Keycloak SSO           | Container in compose (dev mode, realm import)        |
| Nextcloud (WebDAV)     | Container in compose                                 |
| MinIO (S3)             | Container in compose                                 |
| VPN sidecars           | Containers in compose (cluster, research, workstation)|
| Orchestrator           | Native (`uvicorn --reload`) or container             |
| Agent (worker mode)    | Native or container, shared `/workspace` PVC         |
| Agent (persistent mode)| Native or container                                  |
| Cockpit (Angular)      | Native (`npm start`) or container                    |
| MCP server             | Container in compose                                 |

### What is missing

> **Update (2026-04-04):** Docker Compose mode (`docs/docker_compose_mode.md`)
> now addresses the first four gaps below. The orchestrator's `DockerProvisioner`
> assigns workspace containers from a static pool, persistent agents run as a
> fixed pool with session attach/detach, and QEMU-in-Docker provides VM
> workspaces without KubeVirt. The remaining gaps (Headscale, Tailscale,
> Management Daemon, Sudo Gate) are cross-cluster features that don't apply
> to single-host Docker Compose deployments.

| Component               | Cluster implementation           | Local equivalent |
|--------------------------|----------------------------------|------------------|
| ContainerProvisioner     | Creates workspace pods via K8s API | `DockerProvisioner` assigns from static pool |
| PersistentProvisioner    | Creates persistent agent pods via K8s API | Fixed pool with `POST /session/attach` |
| VM Controller            | Listens NATS, calls KubeVirt API | Not needed (QEMU containers managed directly) |
| KubeVirt VMs             | Real VMs on agent cluster        | QEMU-in-Docker (`qemux/qemu` with existing qcow2) |
| Headscale                | StatefulSet, SQLite, ACLs        | Not needed (same Docker network) |
| Tailscale sidecar        | Kernel-mode WireGuard in pods    | Not needed (same Docker network) |
| Management Daemon        | Inside VM, reports via NATS      | Unchanged (runs inside QEMU VM) |
| Sudo Gate (full flow)    | C plugin + Go daemon + NATS      | Unchanged (runs inside QEMU VM) |

## Impact

### Untestable code paths

- **Workspace pod provisioning** — `orchestrator/services/container_provisioner.py`
  creates per-job K8s pods (`workspace-{job_id[:12]}`) via
  `CoreV1Api.create_namespaced_pod()`. The full lifecycle — create, poll
  readiness (TCP socket on port 22, 120s timeout), inject SSH config into
  `config_override.workspace.remote`, cleanup on job end — only runs on
  a real cluster. The dispatcher's `_job_needs_container()` check, auto-
  provisioning, and the `created → creating → ready → deleted` state
  machine are never exercised locally.
- **Persistent agent pod provisioning** —
  `orchestrator/services/persistent_provisioner.py` creates on-demand
  agent pods (`persistent-{thread_id[:12]}`) when users create threads
  or connect via WebSocket. Lifecycle: background pod creation, init
  container waits for orchestrator, agent registers with
  `agent_mode="persistent"`, WebSocket proxy to pod IP:8001, idle
  timeout (30min default) with S3 workspace snapshot, and pod cleanup.
  None of this works without K8s.
- **VM lifecycle state machine** — `orchestrator/services/vm_provisioner.py`
  auto-selects NATS or direct K8s mode; the NATS path
  (`orchestrator/services/nats_bridge.py`) and the VM controller
  (`vm/controller/controller.py`) have zero local coverage.
- **NATS message flow** — 11 subjects across create, status, register,
  heartbeat, control, and sudo request/reply. Regressions in message
  schemas or routing are only caught on the cluster.
- **RemoteBackend workspace** — `src/core/backends/remote.py` uses paramiko
  SSH/SFTP with exponential-backoff reconnection. The `ShellManager`
  delegation path (`supports_shell = True`) is never exercised locally.
- **Management daemon** — `docker/agent-vm-base/files/management-daemon.py`
  IP detection (prefers Tailscale `100.64.x.y`, falls back to LAN),
  heartbeat loop, agent monitor, control signal handling
  (freeze/resume/terminate via `SIGSTOP`/`SIGCONT`/`SIGTERM`).
- **Sudo approval** — The full chain from C plugin (`vm/sudo-plugin/`) to
  Go daemon (`vm/sudo-daemon/`) to NATS request/reply to orchestrator
  auto-rules + SSE to cockpit UI.
- **Dispatcher dynamic provisioning** — The dispatcher's
  `_job_needs_container()` and `_job_needs_vm()` checks, auto-
  provisioning of workspace pods or VMs, waiting for ready status, and
  injecting SSH config into the agent's `config_override.workspace.remote`.

### NATS subject map (all untestable locally)

```
PUBLISHED BY ORCHESTRATOR:
  vm.lifecycle.create         → VM creation request
  vm.lifecycle.delete         → VM deletion request
  vm.lifecycle.get            → Status query (request/reply, 5s timeout)
  agent.vm.{job_id}.control   → freeze/resume/terminate signals

PUBLISHED BY VM CONTROLLER:
  vm.lifecycle.status         → created/failed/deleted/query_failed

PUBLISHED BY MANAGEMENT DAEMON (inside VM):
  agent.vm.{job_id}.register  → SSH host/port, hostname, PID
  agent.vm.{job_id}.heartbeat → CPU/memory/disk, agent_running, code_server_connections
  agent.vm.{job_id}.status    → Agent process exit (completed/failed + exit_code)

PUBLISHED BY SUDO DAEMON (inside VM):
  sudo.request.{vm_id}.{job_id} → Command, user, argv, cwd (request/reply)
```

## Existing architecture insights

### ContainerProvisioner (already exists, needs local backend)

`orchestrator/services/container_provisioner.py` creates per-job workspace
pods with SSH (port 22) and code-server (port 8080). Key details:

- **Pod spec**: workspace image, SSH pubkey from K8s Secret, emptyDir
  workspace volume (10Gi), readiness probe on TCP:22, resource
  requests/limits (default 500m/2000m CPU, 1Gi/4Gi memory)
- **Security**: Drops all capabilities, adds only SSHD-required ones
  (same set as the VM simulator design below)
- **Lifecycle**: `create_workspace()` → `_wait_for_ready()` polls every
  2s for 120s → dispatcher injects `pod_ip` as SSH host into
  `config_override.workspace.remote` → agent connects via RemoteBackend
- **Cleanup**: `delete_workspace()` with 10s grace period, triggered by
  job cancellation/completion
- **Graceful degradation**: If K8s unavailable, `is_available` returns
  `False` and the dispatcher falls back to local workspace

Locally, this needs a substitute that creates Docker/Podman containers
instead of K8s pods, using the same workspace image.

### PersistentProvisioner (already exists, needs local backend)

`orchestrator/services/persistent_provisioner.py` creates on-demand agent
pods for interactive persistent sessions. Key details:

- **Pod spec**: Agent image, init container waits for orchestrator,
  command `python agent.py --mode persistent --thread-id {id}`,
  config injected via ConfigMap/Secret, health probes on `/health`
  and `/ready`, resource defaults 250m/1000m CPU, 512Mi/2Gi memory
- **Security**: Non-root (UID 999), read-only root filesystem,
  dropped all capabilities
- **Lifecycle**: Thread creation or WebSocket connect triggers
  `create_agent_pod()` in background → agent registers with
  `agent_mode="persistent"` → orchestrator binds agent to thread →
  WebSocket proxied to `pod_ip:8001/ws/chat`
- **Idle management**: `WorkspaceSuspensionService` sweeps every 60s,
  snapshots workspace to S3, deletes container after 30min idle,
  restores on demand when thread accessed again
- **Graceful degradation**: If K8s unavailable, logs message and
  returns `False`; users can start agents manually with
  `python agent.py --mode persistent --thread-id {uuid}`

Locally, this needs a substitute that creates Docker/Podman containers
running the agent in persistent mode.

### Workspace backend abstraction (already exists)

The agent has a `WorkspaceBackend` interface (`src/core/workspace_backend.py`)
with a single production implementation: **`RemoteBackend`** (paramiko
SSH/SFTP, `supports_shell = True`). `LocalBackend` was removed in the
2026-04-11 cleanup — the agent never operates on its own filesystem, and
the config schema rejects `backend: local`.

The orchestrator injects the workspace container's Tailscale/SSH host into
`config_override.workspace.remote.host`. No changes needed to this
abstraction — the simulator just needs to provide a reachable SSH host.

### VMProvisioner dual-mode (already exists)

`orchestrator/services/vm_provisioner.py` auto-selects:
1. **NATS mode** — publishes to `vm.lifecycle.*`, VM controller handles KubeVirt
2. **Direct K8s mode** — calls KubeVirt API directly (same-cluster fallback)
3. **Disabled** — neither available, returns 503

The simulator replaces the VM controller on the NATS side. No orchestrator
changes needed.

### Graceful degradation (already exists)

`NatsBridge` follows the MongoDB pattern: if `NATS_URL` is unset or `nats-py`
is not installed, all operations return `False`/`None`. The system works
identically without NATS — only VM features are disabled. This means the
simulator is purely additive.

### Dispatcher VM awareness (already exists)

The dispatcher (`_try_dispatch_pending_jobs()`) already:
1. Checks `_job_needs_vm(job)` for `context.vm.requested` or
   `config_override.workspace.backend == "remote"`
2. Calls `vm_provisioner.create_vm()` if VM not yet provisioned
3. Skips jobs where `vm.status != "ready"`
4. Injects `ssh_host` and `ssh_port` into agent config on dispatch

### Existing workspace container image (reusable)

`docker/Dockerfile.workspace` already builds an SSH-enabled container with
code-server, tmux, git, Python 3, Node.js 22, and dev tools. The entrypoint
(`docker/workspace-entrypoint.sh`) starts SSHD as PID 1 with code-server in
the background. This can serve as the VM substitute image.

## Proposed solution

A phased approach, starting lightweight and extending as needed.

### Phase 1: Local provisioner backends (containers as K8s substitutes)

Three provisioning paths use the K8s API in production. Locally, each needs
a Docker/Podman backend that creates containers instead of K8s pods/VMs.

#### 1a. ContainerProvisioner local mode

Add a Docker SDK backend to `ContainerProvisioner` so it can create
workspace containers locally instead of calling `create_namespaced_pod()`.

**Approach**: The provisioner already has graceful degradation (returns
`False` when K8s is unavailable). Add a `CONTAINER_PROVISIONER_MODE`
env var: `k8s` (default) or `docker`. In `docker` mode, use the Docker
SDK to create containers from the same workspace image, on the compose
network, with the same SSH pubkey, ports, and resource limits. The
readiness poll, config injection, and cleanup paths stay identical — only
the container creation/deletion backend changes.

**What this tests**:
- Dispatcher `_job_needs_container()` decision logic
- Full workspace lifecycle: create → poll ready → dispatch → cleanup
- SSH config injection into `config_override.workspace.remote`
- Agent RemoteBackend SSH/SFTP workspace operations
- ShellManager remote delegation (remote tmux over SSH)

#### 1b. PersistentProvisioner local mode

Same pattern: add a Docker SDK backend to `PersistentProvisioner`.

**Approach**: Add `PERSISTENT_PROVISIONER_MODE` env var: `k8s` or `docker`.
In `docker` mode, create a container running
`python agent.py --mode persistent --thread-id {id}` with the agent image,
injecting the same env vars the ConfigMap/Secret would provide. The
container joins the compose network so the orchestrator can proxy WebSocket
traffic to `container_ip:8001`.

**What this tests**:
- On-demand persistent agent creation (thread create + WebSocket trigger)
- Agent registration with `agent_mode="persistent"`
- WebSocket proxy to persistent agent
- Workspace suspension/restore cycle (S3 snapshot + container recreate)
- Idle timeout cleanup

#### 1c. VM simulator (NATS-based, containers as VM substitutes)

A Python service (~200-300 lines) that subscribes to NATS and manages
workspace containers via the local Podman/Docker socket, replacing the real
VM controller + KubeVirt.

**Architecture**:

```
docker-compose.dev.yaml
├── nats (JetStream, single-node)
├── vm-simulator
│   ├── Subscribes: vm.lifecycle.create, vm.lifecycle.delete, vm.lifecycle.get
│   ├── On create: docker.containers.run(workspace-image, ...)
│   ├── On delete: container.stop() + container.remove()
│   └── Publishes: vm.lifecycle.status, agent.vm.{id}.register
├── [workspace containers created dynamically]
│   ├── SSH on port 22 (reachable via compose network)
│   ├── Management daemon (NATS heartbeats, control signals)
│   └── Sudo daemon (optional, for full sudo flow testing)
├── orchestrator (native or container)
├── agent (native or container)
└── ... (databases, VPNs, etc.)
```

#### What Phase 1 tests (combined)

- **Workspace pods**: Dispatcher auto-provisioning, SSH config injection,
  agent RemoteBackend, ShellManager delegation, workspace cleanup
- **Persistent agents**: On-demand pod creation, WebSocket proxy, agent
  registration, idle timeout, workspace suspension/restore
- **VMs**: Full NATS lifecycle message flow (all 11 subjects), management
  daemon registration/heartbeats/IP detection, sudo request/approval,
  freeze/resume/terminate control signals
- **Shared**: Agent RemoteBackend SSH/SFTP, code-server access

#### What Phase 1 does NOT test

- Headscale/Tailscale mesh VPN routing (Phase 2)
- Real VM boot times, kernel isolation, cloud-init
- KubeVirt-specific behavior (containerDisk, VirtIO, masquerade networking)
- K8s resource quotas, namespace isolation, RBAC
- Init container orchestrator-wait pattern (direct network access locally)

#### Implementation details

**Container runtime access:**
Mount the Podman socket into the simulator (same pattern as `dozzle`):
```yaml
vm-simulator:
  volumes:
    - /run/user/${HOST_UID:-1000}/podman/podman.sock:/run/podman/podman.sock
```

**Python SDK:** Use `docker` (docker-py) — works with both Docker and Podman
(Podman's REST API is Docker-compatible). Auto-detect socket:
```python
for sock in [
    f"/run/user/{os.getuid()}/podman/podman.sock",
    "/run/podman/podman.sock",
    "/var/run/docker.sock",
]:
    if os.path.exists(sock):
        return DockerClient(base_url=f"unix://{sock}")
```

**Networking:** Workspace containers join the compose network. The simulator
reports the container's network IP as `ssh_host` in
`agent.vm.{id}.register`. No Tailscale needed — agent connects directly via
compose network.

**Container labels:** All simulator-created containers get
`srw/component=vm-simulator` and `srw/job-id={job_id}` labels for cleanup
and identification.

**Security:** Drop all capabilities, add back only SSHD-required ones
(`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`,
`NET_BIND_SERVICE`, `SYS_CHROOT`, `KILL`, `AUDIT_WRITE`). Mirrors K8s
pod security context.

**Management daemon inside container:** Start
`management-daemon.py` inside the workspace container via the entrypoint.
Pass `NATS_URL` and `JOB_ID` as environment variables. The daemon connects
to the compose NATS instance and publishes the same subjects as in
production. IP detection will return the container's compose-network IP
(no Tailscale), which is correct for this mode.

**Sudo gate (optional in Phase 1):** The Go sudo daemon
(`vm/sudo-daemon/`) and C plugin (`vm/sudo-plugin/`) require compilation
and installation inside the container. For Phase 1, this can be skipped
(sudo approval tested at the NATS message level via orchestrator endpoints).
Full sudo testing requires building the workspace image with the
`sudo_gate.so` plugin installed.

#### NATS message schemas (reference for simulator)

**Create request** (received by simulator):
```json
{
  "job_id": "uuid",
  "agent_config": "defaults",
  "vm_image": "ghcr.io/.../agent-vm-base:latest",
  "cpu_cores": 2,
  "memory": "4Gi",
  "nats_url": "nats://nats:4222",
  "description": "Task description"
}
```

**Status response** (published by simulator):
```json
{
  "job_id": "uuid",
  "status": "created",
  "vm_name": "vm-sim-{job_id[:12]}",
  "namespace": "local"
}
```

**Registration** (published by management daemon inside container):
```json
{
  "job_id": "uuid",
  "hostname": "vm-sim-abc123",
  "ip": "172.18.0.x",
  "pid": 1
}
```

**Status query** (request/reply, received by simulator):
```json
{"job_id": "uuid"}
```
Response:
```json
{
  "job_id": "uuid",
  "vm_name": "vm-sim-abc123",
  "ready": true,
  "phase": "Running",
  "created": true
}
```

---

### Phase 2: Local Headscale mesh (extend Phase 1)

Add Headscale and Tailscale to the compose stack so agent-to-VM traffic
flows through the same WireGuard mesh as production.

#### Architecture addition

```yaml
headscale:
  image: headscale/headscale:0.28.0
  command: serve
  volumes:
    - ./headscale/config:/etc/headscale:ro
    - headscale_data:/var/lib/headscale
  ports:
    - "8080:8080"
    - "3478:3478/udp"

headscale-init:
  image: headscale/headscale:0.28.0
  depends_on:
    headscale: { condition: service_healthy }
  command: |
    sh -c '
      headscale users create agents 2>/dev/null || true
      headscale preauthkeys create --user agents --reusable --expiration 24h > /shared/authkey
    '
  volumes:
    - shared_keys:/shared

ts-agent:
  image: tailscale/tailscale:v1.82.5
  hostname: agent-dev
  cap_add: [NET_ADMIN, SYS_MODULE]
  devices: [/dev/net/tun:/dev/net/tun]
  volumes:
    - ts_agent_state:/var/lib/tailscale
    - shared_keys:/shared:ro
  environment:
    TS_STATE_DIR: /var/lib/tailscale
    TS_EXTRA_ARGS: --login-server=http://headscale:8080 --advertise-tags=tag:agent
  entrypoint: sh -c 'export TS_AUTHKEY=$$(cat /shared/authkey) && /usr/local/bin/containerboot'
```

#### Headscale configuration (local dev)

```yaml
server_url: http://headscale:8080
listen_addr: 0.0.0.0:8080
prefixes:
  v4: 100.64.0.0/10
  v6: fd7a:115c:a1e0::/48
  allocation: sequential
derp:
  server:
    enabled: true           # Embedded DERP relay
    region_id: 999
    stun_listen_addr: 0.0.0.0:3478
  urls: []                  # No external DERP
  auto_update_enabled: false
dns:
  magic_dns: false          # Use IPs directly (matches production)
database:
  type: sqlite
  sqlite:
    path: /var/lib/headscale/db.sqlite
    write_ahead_log: true
ephemeral_node_inactivity_timeout: 30m
policy:
  path: /etc/headscale/acl.json
```

#### ACL policy

```json
{
  "tagOwners": {
    "tag:agent": [],
    "tag:vm": []
  },
  "acls": [
    {"action": "accept", "src": ["tag:agent"], "dst": ["tag:vm:22"]}
  ]
}
```

#### VM simulator changes for Phase 2

The simulator generates pre-auth keys via the Headscale REST API (reusing
`vm/controller/headscale_client.py`) and passes them to workspace containers.
Each workspace container gets a Tailscale sidecar (or runs Tailscale
internally). The simulator reports the Tailscale `100.64.x.y` IP instead
of the compose-network IP.

#### What this additionally tests

- Headscale pre-auth key generation and ephemeral node lifecycle
- Tailscale sidecar authentication and mesh establishment
- Agent SSH over WireGuard tunnel (100.64.x.y addresses)
- ACL enforcement (agents can only reach VMs on port 22)
- DERP relay fallback path

#### Startup timing

Cold start from `docker compose up` to fully meshed network: ~10-15 seconds.
- Headscale healthy: ~3-5s
- Init container creates user + key: ~1s
- Tailscale registration: ~2-5s (sub-second with persisted state)
- Mesh route establishment: <1s (same Docker host, no real NAT)

#### Known pitfalls

| Pitfall | Fix |
|---------|-----|
| No `/var/lib/tailscale` volume | Re-registers every restart, orphan nodes |
| `server_url` not using Docker service name | Clients can't find coordination server |
| No embedded DERP | Registered but can't route traffic |
| Single-use auth key with multiple containers | Use `--reusable` flag |
| Missing `/dev/net/tun` | Add device mount or use userspace mode |
| `listen_addr: 127.0.0.1` | Other containers can't reach Headscale |

---

### Phase 3: Local QEMU/libvirt VMs (optional, heaviest)

Replace containers with real QEMU VMs managed by libvirt. Only pursue if
Phase 1+2 are insufficient for catching production bugs.

**When this matters:** Testing cloud-init boot sequences, kernel-level
isolation, KubeVirt-specific disk/network behavior, VM snapshot/restore.

**Tools:** Vagrant with `vagrant-libvirt` plugin, or direct `virsh`
management. Requires `/dev/kvm` and the `libvirtd` daemon.

**Resource cost:** Each VM needs ~2GB RAM + 2 vCPUs. Boot time: 30-60
seconds. Not suitable for rapid iteration.

**Alternative:** KubeVirt on minikube with KVM2 driver. The KubeVirt project
officially supports this. Requires 8GB+ RAM for the cluster alone.

---

## NATS testing considerations

### Single-node vs production topology

The local compose runs a single NATS node. Production uses a 3-node hub
cluster with JetStream replication (R3) and leaf nodes on the agent cluster.
Key differences to be aware of:

| Behavior | Single-node local | Production hub+leaf |
|----------|-------------------|---------------------|
| JetStream writes | Always succeed immediately | Require quorum (2/3 nodes) |
| Message ordering | Always per-subject | Per-publisher only across nodes |
| Consumer ack timeouts | Instant processing | May need longer `ack_wait` |
| Leaf node subject routing | N/A | Can remap subjects, miss permissions |
| Request/reply | Direct | Crosses hub-leaf boundary |

### Production JetStream streams (reference)

```
VM_EVENTS:          vm.lifecycle.>, vm.status.>  (24h retention, 1GB, R3)
AGENT_HEARTBEATS:   agent.*.heartbeat            (1h retention, 100MB, R3)
JOB_ASSIGNMENTS:    agent.*.job.>                (work queue, 512MB, R3)
```

The simulator should create these streams locally with `num_replicas: 1`.

### Hub/leaf testing (optional, for NATS-specific regressions)

Can be added to compose for pre-merge CI:
```yaml
nats-hub:
  image: nats:2.10-alpine
  command: ["-c", "/etc/nats/hub.conf"]
nats-leaf:
  image: nats:2.10-alpine
  command: ["-c", "/etc/nats/leaf.conf"]
  depends_on: [nats-hub]
```

### Schema validation

Use Pydantic models as the source of truth for all NATS messages. Validate
at both publish and subscribe boundaries. This catches schema drift between
orchestrator, VM controller, and management daemon without requiring a
running cluster.

---

## Files involved

### Phase 1 (new)

| File | Purpose |
|------|---------|
| `vm/simulator/simulator.py` | VM simulator service (~200-300 lines) |
| `vm/simulator/Dockerfile` | Container image for the simulator |
| `vm/simulator/requirements.txt` | `nats-py`, `docker` (docker-py) |

### Phase 1 (modify)

| File | Purpose |
|------|---------|
| `orchestrator/services/container_provisioner.py` | Add Docker SDK backend (`CONTAINER_PROVISIONER_MODE=docker`) |
| `orchestrator/services/persistent_provisioner.py` | Add Docker SDK backend (`PERSISTENT_PROVISIONER_MODE=docker`) |
| `docker-compose.dev.yaml` | Add `vm-simulator` service, mount Docker/Podman socket for orchestrator |
| `docker-compose.local.yaml` | Same as above |
| `docker/workspace-entrypoint.sh` | Optionally start management daemon if `ENABLE_MGMT_DAEMON=true` |

### Phase 2 (new)

| File | Purpose |
|------|---------|
| `docker/headscale/config.yaml` | Headscale config for local dev |
| `docker/headscale/acl.json` | ACL policy (agents → VMs port 22) |

### Phase 2 (modify)

| File | Purpose |
|------|---------|
| `docker-compose.dev.yaml` | Add `headscale`, `headscale-init`, Tailscale sidecars |
| `vm/simulator/simulator.py` | Generate Headscale pre-auth keys, report Tailscale IPs |

### Reference (read-only, no changes needed)

| File | Purpose |
|------|---------|
| `vm/controller/controller.py` | NATS subjects, message schemas, KubeVirt template rendering |
| `vm/controller/headscale_client.py` | Headscale REST API client (reuse in Phase 2) |
| `orchestrator/services/nats_bridge.py` | All NATS subjects, subscription handlers, graceful degradation |
| `orchestrator/services/vm_provisioner.py` | Dual-mode provisioner, dispatcher integration |
| `orchestrator/services/sudo_gate.py` | Sudo approval auto-rules, NATS request/reply, SSE |
| `orchestrator/services/workspace_suspension.py` | Idle timeout, S3 snapshot, workspace restore |
| `src/core/backends/remote.py` | RemoteBackend SSH/SFTP, ShellManager delegation |
| `src/core/workspace_backend.py` | WorkspaceBackend interface |
| `docker/agent-vm-base/files/management-daemon.py` | Daemon code to run inside workspace containers |
| `vm/sudo-daemon/` | Go sudo approval daemon |
| `vm/sudo-plugin/` | C sudo plugin |
| `deployment/nats/setup-streams.sh` | JetStream stream definitions |
| `deployment/21-agent.yaml` | Agent deployment (static replicas, Tailscale sidecar) |

## Industry context

Container-as-VM simulation is the standard approach for local testing of
VM-based infrastructure. Companies like Gitpod, Coder, and GitLab never
test real VMs locally — they use containers for dev and reserve VM testing
for CI/staging with bare-metal runners. The key architectural enabler is an
interface abstraction (`VMProvider`/`WorkspaceBackend`) that allows swapping
backends, which this project already has.

Tools and patterns used by similar projects:
- **Testcontainers** (Python) for programmatic container lifecycle in tests
- **Docker SDK** (docker-py) for runtime container management from controllers
- **Molecule** (Ansible) for container-as-VM role testing
- **Vagrant** with libvirt for when real VMs are necessary
- **k3d + Tilt** for full K8s E2E testing with hot-reload
- **Embedded NATS server** or subprocess fixture for integration tests
