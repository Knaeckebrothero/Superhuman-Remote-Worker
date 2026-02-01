# Cloud Workspace Architecture

This document outlines the design considerations for deploying agent workspaces on a k3s cluster.

> **Research Reference:** For detailed analysis, benchmarks, and citations, see [cloud_workspace_research.pdf](./cloud_workspace_research.pdf).

## Vision

Each agent gets its own isolated, containerized Linux workspace. This provides:
- Strong isolation between agents/jobs
- Resource limits per agent
- Clean ephemeral environments
- Horizontal scaling

## Research Summary

Based on comprehensive research into Kubernetes agent deployment patterns, Anthropic's sandboxing approach, and storage performance characteristics, we have concrete recommendations for each architectural decision.

### Key Decisions

| Component | Decision | Rationale |
|-----------|----------|-----------|
| **Container Lifecycle** | Kubernetes Jobs | Matches finite task nature; handles retries via `backoffLimit`, cleanup via `ttlSecondsAfterFinished` |
| **Storage** | Local Path Provisioner + MinIO | SQLite WAL breaks on NFS; Longhorn 5-10x slower; local for performance, MinIO for durability |
| **Isolation** | One PVC per Job | Strict isolation; `ownerReferences` cascades cleanup automatically |
| **Communication** | NATS JetStream + SSE | Event-driven push model; decouples services; real-time log streaming |
| **Tooling** | Implement `execute()` | Essential for flexibility; secured via read-only root + emptyDir overlays |
| **Network Security** | NetworkPolicy + Squid Proxy | Solves FQDN filtering (K8s NetworkPolicy is IP-only); blocks internal network |

### Why Kubernetes Jobs (Not Deployments)

Deployments restart pods that exit successfully (exit code 0), treating completion as failure. This is wrong for agents that complete tasks and should terminate. Jobs:
- Mark successful termination as `Complete` (not failure)
- Support `backoffLimit` for transient failures (LLM API rate limits)
- Auto-cleanup via `ttlSecondsAfterFinished`
- No "split-brain" risk between orchestrator and k8s controller

### Why Local Path (Not NFS/Longhorn)

**SQLite WAL mode is critical** for agent performance (high-throughput reads/writes to checkpoints). WAL relies on shared memory (.shm) via mmap.

- **NFS breaks WAL**: mmap inconsistencies cause "database is locked" errors or corruption
- **Longhorn is too slow**: Synchronous replication adds 5-10x latency on random writes
- **Local Path**: Native I/O speeds, zero config, bundled with k3s

Trade-off: Data is node-bound. Mitigated by archiving checkpoints to MinIO.

### Tiered Storage Pattern

```
┌─────────────────────────────────────────────────────────────────┐
│  Tier 1: Active Workspace (Local Path PVC)                      │
│  - Fast NVMe/SSD-backed storage                                 │
│  - SQLite checkpoints, workspace files, git repos               │
│  - Ephemeral - deleted when Job cleaned up                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ periodic sync (sidecar)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Tier 2: Durable Archive (MinIO/S3)                             │
│  - Checkpoint snapshots every 5 minutes                         │
│  - Final workspace archive on completion                        │
│  - Enables resume after node failure                            │
└─────────────────────────────────────────────────────────────────┘
```

**Lifecycle:**
1. InitContainer checks MinIO for existing checkpoint (resume case)
2. Agent runs on fast local PVC
3. Sidecar periodically syncs checkpoints to MinIO
4. On completion: archive to MinIO, Job TTL expires, PVC garbage collected

### Communication: NATS + SSE

Instead of REST polling (brittle with ephemeral pod IPs), use event-driven push:

```
Agent Pod                    NATS JetStream              Orchestrator → Cockpit
    │                              │                              │
    ├─── publish ─────────────────►│                              │
    │    logs.agent.<job_id>       │                              │
    │    events.agent.<job_id>     │◄──── subscribe ──────────────┤
    │                              │                              │
    │                              │────── SSE stream ───────────►│
    │                              │      /api/stream/{job_id}    │
```

Benefits:
- Orchestrator doesn't need to track pod IPs
- Agent crash → new pod publishes to same subject → stream continues
- Natural backpressure and persistence with JetStream

### Network Security: The Proxy Pattern

K8s NetworkPolicy only supports IP-based rules, not FQDNs. Since github.com/openai.com IPs change via CDN, we use a proxy:

```
Agent Pod ──► NetworkPolicy allows only ──► Squid Proxy ──► Internet
                  proxy:3128 + DNS              │
                                                │ allowlist:
                                                │ - github.com
                                                │ - api.openai.com
                                                │ - pypi.org
              NetworkPolicy blocks:
              - 10.0.0.0/8 (cluster)
              - 169.254.169.254 (metadata)
              - K8s API server
```

### Read-Only Root + emptyDir Overlays

Security best practice: `readOnlyRootFilesystem: true`. But agents need to `pip install`. Solution:

```yaml
env:
  - name: PYTHONUSERBASE
    value: /home/agent/.local
  - name: PATH
    value: /home/agent/.local/bin:$PATH
volumeMounts:
  - name: workspace
    mountPath: /workspace           # Local Path PVC
  - name: user-packages
    mountPath: /home/agent/.local   # emptyDir
  - name: tmp
    mountPath: /tmp                 # emptyDir
  - name: shm
    mountPath: /dev/shm             # emptyDir (Memory) - for SQLite WAL
```

### Secrets Strategy

**Problem:** `execute("env")` reveals environment variable secrets.

**Solution:** Mount secrets as files outside workspace:
```yaml
volumeMounts:
  - name: secrets
    mountPath: /var/secrets
    readOnly: true
```

Agent code reads `/var/secrets/OPENAI_API_KEY`, initializes client, clears from memory. Orchestrator's log streaming should regex-redact `sk-...` patterns before sending to frontend.

## The Tool Design Question

### Current Approach
We have many specialized tools: `read_file()`, `write_file()`, `delete_file()`, `edit_file()`, etc.

### Alternative: Single `execute(command: str)`
Give the agent a single tool that runs shell commands in its container:
- `delete_file()` → `execute("rm file.txt")`
- `read_file()` → `execute("cat file.txt")`
- `edit_file()` → `execute("sed -i ...")`

**Pros:**
- Simpler API surface (one tool)
- LLMs know Linux extremely well
- Maximum flexibility
- No tool implementation maintenance

**Cons:**
- Interactive tools don't work (`nano`, `vim` require TTY)
- Unstructured output (parsing shell text is fragile)
- Safety guardrails harder to implement (parsing commands vs structured args)
- Atomicity issues (multi-step shell operations can fail midway)
- Token efficiency (tool calls often more compact)

### Recommended Hybrid Approach
Keep specialized tools for operations that need:
- **Atomicity**: file edits, writes
- **Safety**: path validation, permission checks
- **Structured I/O**: predictable return formats
- **Special handling**: binary files, large files, encoding

Add a general `execute()` for everything else:
- Package management (`pip install`, `npm install`)
- Git operations
- Build commands
- Custom scripts
- System exploration

## k3s Deployment Architecture

### Target Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              k3s Cluster                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────┐    NATS     ┌─────────────┐     SSE      ┌──────────────┐ │
│  │ Orchestrator│◄───────────►│    NATS     │◄────────────►│   Cockpit    │ │
│  │  (FastAPI)  │             │  JetStream  │              │  (Angular)   │ │
│  └──────┬──────┘             └─────────────┘              └──────────────┘ │
│         │                                                                    │
│         │ creates Job + PVC (ownerRef)                                      │
│         ▼                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                     Agent Job Pod (per job)                          │   │
│  │                                                                      │   │
│  │  ┌─────────────────┐  ┌──────────────────┐  ┌───────────────────┐  │   │
│  │  │  InitContainer  │  │   Agent (main)   │  │  Log Sidecar      │  │   │
│  │  │                 │  │                  │  │                   │  │   │
│  │  │ - Check MinIO   │  │ python agent.py  │  │ Fluent Bit        │  │   │
│  │  │ - Download      │  │ --config X       │  │ → NATS publish    │  │   │
│  │  │   checkpoint    │  │ --job-id Y       │  │   logs.<job_id>   │  │   │
│  │  └─────────────────┘  └──────────────────┘  └───────────────────┘  │   │
│  │                                                                      │   │
│  │  Volumes:                                                            │   │
│  │  ┌─────────────────────────────────────────────────────────────┐   │   │
│  │  │ /workspace        → Local Path PVC (fast, per-job)          │   │   │
│  │  │ /home/agent/.local → emptyDir (pip install --user)          │   │   │
│  │  │ /tmp              → emptyDir                                 │   │   │
│  │  │ /dev/shm          → emptyDir (Memory) - SQLite WAL          │   │   │
│  │  │ /var/secrets      → Secret mount (read-only, outside ws)    │   │   │
│  │  └─────────────────────────────────────────────────────────────┘   │   │
│  │                                                                      │   │
│  │  Security: readOnlyRootFilesystem, runAsNonRoot, drop ALL caps     │   │
│  │                                                                      │   │
│  └───────────────────────────────┬─────────────────────────────────────┘   │
│                                  │                                          │
│                                  │ HTTP_PROXY / HTTPS_PROXY                │
│                                  ▼                                          │
│  ┌─────────────────┐         ┌─────────────────────────────────────────┐   │
│  │   Squid Proxy   │────────►│              Internet                    │   │
│  │  (FQDN allowlist)│        │  (github.com, api.openai.com, pypi.org) │   │
│  └─────────────────┘         └─────────────────────────────────────────┘   │
│         ▲                                                                    │
│         │ NetworkPolicy: agents can only reach proxy + DNS                  │
│         │                                                                    │
│  ┌──────┴──────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐       │
│  │ PostgreSQL  │  │   MongoDB   │  │    MinIO    │  │  (Neo4j)    │       │
│  │  (jobs)     │  │  (audit)    │  │ (archives)  │  │ (optional)  │       │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘       │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Resolved Questions

| Question | Decision | Details |
|----------|----------|---------|
| Container lifecycle | **Kubernetes Jobs** | One Job per task; `ttlSecondsAfterFinished` for cleanup |
| Long-running jobs | **Liveness probes** | Detect deadlocks; Job restarts pod up to `backoffLimit` |
| Workspace persistence | **Local Path + MinIO** | Fast local for active work; MinIO for checkpoint durability |
| Communication | **NATS JetStream + SSE** | Event-driven; agents push logs/events to NATS subjects |
| Orchestration | **Orchestrator as Job factory** | Templates Job+PVC manifests; kubectl apply |
| Network security | **Squid proxy + NetworkPolicy** | FQDN allowlisting without Cilium |

### Remaining Open Questions

1. **Container image**
   - Base image requirements (Python, Node, common tools)?
   - Pre-baked vs dynamically installed dependencies?
   - GPU access for local LLM inference?

2. **Multi-node scaling**
   - How to handle node affinity for local storage?
   - When to upgrade from Local Path to distributed storage?

### Execute Tool Implementation

If we go with a containerized workspace, the `execute()` tool becomes straightforward:

```python
async def execute(command: str, timeout: int = 300) -> ExecuteResult:
    """
    Execute a shell command in the agent's workspace container.

    Returns:
        ExecuteResult:
            stdout: str
            stderr: str
            exit_code: int
            timed_out: bool
    """
    # If running locally: subprocess
    # If running in k3s: kubectl exec / container runtime API
    pass
```

The same tool works locally (subprocess) and in k3s (exec into container).

## Agent Sandbox Design

### How Anthropic Does It (Claude Code)

Research on Anthropic's approach to sandboxing Claude Code:

**Two complementary isolation boundaries:**

1. **Filesystem Isolation** - OS-level primitives (Linux bubblewrap, macOS Seatbelt) restrict access to specific directories. Prevents prompt-injected agents from modifying system files. Covers not just direct interactions but spawned subprocesses too.

2. **Network Isolation** - All outbound connections route through a Unix domain socket to an external proxy server. The network namespace is removed entirely from the sandboxed process, forcing all traffic through host-side proxies.

**Key architectural decisions:**

| Aspect | Implementation |
|--------|----------------|
| Filesystem | Allow-only for writes, deny-only for reads |
| Network | All traffic via proxy (HTTP proxy + SOCKS5 for TCP) |
| Credentials | Git credentials/SSH keys stay **outside** sandbox |
| Proxy role | Validates requests, enforces domain allowlist, handles user confirmation |
| Primitives | `bubblewrap` + seccomp on Linux, `sandbox-exec` + Seatbelt on macOS |

**Claude Cowork (Desktop)** uses a full Linux VM via Apple's Virtualization Framework - not a container. Network restricted to strict allowlist, MCP servers passed through dynamically.

**Claude Cowork v2 (Server)** uses Docker containers with the `anthropic/claude-code` image. Only explicitly mounted directories visible to agent. Launches via:
```bash
/usr/local/bin/claude-code --workspace /sessions/<id>/mnt/workspace --vsock-endpoint /run/vsock.sock
```

**Results:** Sandboxing reduces permission prompts by 84% internally - agents operate autonomously within boundaries, humans only intervene for boundary-crossing requests.

**Open source:** Anthropic released [sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime) - OS-level isolation without containers. Config lives in `~/.srt-settings.json`.

Sources:
- [Anthropic Engineering Blog - Claude Code Sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing)
- [sandbox-runtime GitHub](https://github.com/anthropic-experimental/sandbox-runtime)
- [Claude Code Docs - Sandboxing](https://code.claude.com/docs/en/sandboxing)

### Lessons for Our Architecture

What we can adopt from Anthropic's approach:

1. **Credentials outside sandbox** - Git credentials, API keys, DB connection strings should be injected via environment or mounted secrets, never inside the workspace the agent can freely access.

2. **Proxy-mediated network** - Rather than trying to filter commands, route all egress through a proxy that enforces allowlists. This is cleaner than parsing `curl` commands.

3. **Allow-only for writes** - Agent can read broadly but can only write to `/workspace`. This matches our PVC-per-job model.

4. **vsock for communication** - Instead of exposing HTTP ports, use Unix sockets / vsock for orchestrator ↔ agent communication. More secure, no network exposure.

5. **Ephemeral + mounted workspace** - Container is disposable, only the workspace PVC persists. Clean separation.

**Differences in our context:**

| Anthropic (Claude Code) | Our System |
|-------------------------|------------|
| Interactive CLI tool | Long-running job agents |
| User's local machine | k3s cluster |
| Single user | Multi-tenant (multiple jobs) |
| `sandbox-runtime` (OS-level) | Kubernetes (container-level) |
| Needs macOS support | Linux only |

We can skip the OS-level sandboxing complexity since Kubernetes already provides container isolation. Our focus should be on:
- **NetworkPolicy** for egress control (equivalent to their proxy allowlist)
- **PodSecurityStandards** for container hardening
- **Secrets management** for credentials outside workspace
- **Audit logging** for observability

### The Core Tension

We want to give the agent **maximum flexibility** to accomplish tasks while maintaining **strong isolation** to prevent:
- Escape from the sandbox (container breakout)
- Access to other agents' data or workspaces
- Attacks on cluster infrastructure
- Resource exhaustion (DoS)
- Data exfiltration beyond intended channels

The key insight: **containerization shifts security from tool-level restrictions to environment-level isolation**. Instead of building guardrails into every tool ("don't delete system files"), we give the agent a full Linux environment where deleting "system files" just means deleting its own container's files.

### Why Container Isolation Enables Flexibility

| Without Sandbox | With Sandbox |
|-----------------|--------------|
| `delete_file()` must validate paths | `rm` anything - it's your container |
| `execute()` needs command blocklists | Run any command - can't affect host |
| Network calls need allowlists | Network policies at container level |
| Must prevent `pip install malware` | Install anything - container is ephemeral |
| Complex permission system per tool | Simple: you own everything in /workspace |

The agent becomes a **first-class user** of its own isolated Linux system, not a restricted subprocess begging for permissions.

### Isolation Layers

```
┌─────────────────────────────────────────────────────────────┐
│                    Layer 5: Network Policy                   │
│         - Egress: only allowed endpoints (LLM API, etc.)    │
│         - No access to cluster network / node network       │
│         - No access to other agent pods                     │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│                 Layer 4: Resource Limits                     │
│         - CPU/Memory limits (prevent noisy neighbor)        │
│         - Ephemeral storage limits                          │
│         - PID limits (prevent fork bombs)                   │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│                Layer 3: Security Contexts                    │
│         - Non-root user (runAsNonRoot: true)                │
│         - Read-only root filesystem                         │
│         - No privilege escalation                           │
│         - Drop all capabilities                             │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│                 Layer 2: Seccomp/AppArmor                    │
│         - Restrict syscalls (no mount, no kernel modules)   │
│         - Prevent container escape vectors                  │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│                  Layer 1: Container Runtime                  │
│         - Namespace isolation (PID, network, mount, etc.)   │
│         - cgroups for resource accounting                   │
│         - Separate filesystem view                          │
└─────────────────────────────────────────────────────────────┘
```

### What the Agent CAN Do (Flexibility)

Inside its sandbox, the agent should be able to:
- Execute arbitrary shell commands
- Install packages (`pip`, `npm`, `apt` if we allow it)
- Create/modify/delete any files in its workspace
- Run long-running processes
- Use common dev tools (git, curl, compilers, interpreters)
- Spawn subprocesses
- Use reasonable CPU/memory within limits

### What the Agent CANNOT Do (Isolation)

The sandbox prevents:
- Accessing host filesystem or other containers
- Communicating with other pods (network isolation)
- Calling arbitrary internet endpoints (egress filtering)
- Exhausting cluster resources (limits)
- Escalating privileges or escaping container
- Persisting beyond job completion (ephemeral)
- Accessing Kubernetes API or cluster secrets

### Network Egress Policy

Critical decision: what can the agent reach over the network?

**Strict (recommended for untrusted workloads):**
```yaml
# Only allow specific endpoints
egress:
  - to:
      - ipBlock:
          cidr: <LLM_API_IP>/32    # OpenAI/Anthropic API
  - to:
      - namespaceSelector:
          matchLabels:
            name: databases        # Internal DBs
```

**Permissive (for trusted workloads needing web access):**
```yaml
# Allow internet, block internal
egress:
  - to:
      - ipBlock:
          cidr: 0.0.0.0/0
          except:
            - 10.0.0.0/8          # Block cluster network
            - 192.168.0.0/16      # Block private ranges
            - 172.16.0.0/12
```

### Workspace Filesystem Layout

```
/                           # Read-only root filesystem
├── bin/, usr/, etc/        # Base OS (immutable)
├── workspace/              # Writable - agent's home
│   ├── job_<uuid>/         # Job-specific files
│   │   ├── workspace.md
│   │   ├── todos.yaml
│   │   ├── documents/
│   │   └── ...
│   ├── .cache/             # pip/npm cache (ephemeral)
│   └── tools/              # Installed tools
└── tmp/                    # Writable tmpfs
```

Using a read-only root with writable overlays at specific paths prevents the agent from modifying system binaries while allowing full control over its workspace.

### Example Pod Security Spec

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: agent-job-xxx
  namespace: agents
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: agent
      image: agent-workspace:latest
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      resources:
        limits:
          cpu: "2"
          memory: "4Gi"
          ephemeral-storage: "10Gi"
        requests:
          cpu: "500m"
          memory: "1Gi"
      volumeMounts:
        - name: workspace
          mountPath: /workspace
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: workspace
      persistentVolumeClaim:
        claimName: agent-job-xxx-workspace
    - name: tmp
      emptyDir:
        medium: Memory
        sizeLimit: "1Gi"
```

### Trust Levels

Different deployment scenarios may need different isolation levels:

| Trust Level | Use Case | Isolation |
|-------------|----------|-----------|
| **High** | Internal agents, known configs | Basic container isolation |
| **Medium** | User-submitted prompts | + Network egress filtering |
| **Low** | Arbitrary code execution | + gVisor/Kata, strict seccomp |

For our initial deployment (internal use), medium trust is likely sufficient.

### Monitoring & Audit

Even with isolation, we want visibility:
- **Command logging**: All `execute()` calls logged to MongoDB
- **Resource metrics**: Prometheus scraping per-pod metrics
- **Network flows**: Optional network policy logging
- **Workspace snapshots**: Archive workspace on job completion

## Implementation Roadmap

### Phase 1: Foundation

1. [ ] **Create agent container image**
   - Dockerfile with Python, common tools
   - Non-root user (UID 1000)
   - Entrypoint: `python agent.py`

2. [ ] **Implement `execute()` tool**
   - Subprocess execution with timeout
   - Working directory locked to `/workspace`
   - Structured output (stdout, stderr, exit_code)

3. [ ] **Deploy MinIO to k3s**
   - Single-node setup for checkpoint archival
   - Bucket: `agent-checkpoints`

### Phase 2: Kubernetes Integration

4. [ ] **Orchestrator: Job templating**
   - Generate Job + PVC manifests from job request
   - Set `ownerReferences` for cascading cleanup
   - Inject secrets via volume mount

5. [ ] **Deploy NATS JetStream**
   - Subjects: `cmd.agent.<job_id>`, `events.agent.<job_id>`, `logs.agent.<job_id>`
   - Retention policy for audit

6. [ ] **Agent: NATS logging handler**
   - Custom `logging.Handler` that publishes to NATS
   - Replace file-based logging in container mode

7. [ ] **Orchestrator: SSE bridge**
   - `/api/stream/{job_id}` endpoint
   - Subscribe to NATS, yield via `EventSourceResponse`

### Phase 3: Security Hardening

8. [ ] **Deploy Squid proxy**
   - FQDN allowlist: github.com, api.openai.com, pypi.org, etc.
   - Access logging

9. [ ] **Create NetworkPolicy**
   - Deny all egress by default
   - Allow: proxy service, kube-dns
   - Block: K8s API, metadata service, cluster network

10. [ ] **Apply Pod Security Standards**
    - Namespace label: `pod-security.kubernetes.io/enforce: restricted`
    - Verify agents run correctly under restrictions

### Phase 4: Resilience

11. [ ] **InitContainer for checkpoint restore**
    - Check MinIO for existing checkpoint
    - Download to `/workspace/checkpoints/`

12. [ ] **Sidecar for checkpoint sync**
    - Periodic upload to MinIO (every 5 min)
    - Final sync on SIGTERM

13. [ ] **Liveness probe**
    - HTTP endpoint or file-based heartbeat
    - Detect agent deadlocks

### Phase 5: Observability

14. [ ] **Log redaction**
    - Regex filter for API keys in log stream
    - Apply in orchestrator before SSE

15. [ ] **Prometheus metrics**
    - Per-job resource usage
    - Agent phase transitions
    - LLM API latency/tokens

16. [ ] **Cockpit integration**
    - Real-time log streaming via SSE
    - Job status from NATS events

## Reference Implementations

- **OpenDevin (OpenHands)**: Docker-based sandbox with mounted workspace. Validates separation of agent state from workspace state.
- **Anthropic sandbox-runtime**: OS-level isolation via bubblewrap/Seatbelt. Our proxy pattern is the k8s equivalent.
- **Claude Cowork v2**: Docker containers with `anthropic/claude-code` image, vsock communication.

## Related

- [Cloud Workspace Research (PDF)](./cloud_workspace_research.pdf) - Detailed analysis with citations
- [Angular Migration Plan](./angular_migration_plan.md) - Cockpit frontend
- `orchestrator/` - Backend API that will manage agent Jobs
- `config/` - Agent configurations that will run in containers
