---
tags:
  - agent-architecture
  - tool-development
  - context-management
  - performance
  - research
---

# VM-Based Agent Isolation (KubeVirt)

Design document for running each agent job in a dedicated virtual machine instead of a container, providing full OS-level isolation with enterprise-style management and browser-based remote access.

## Motivation

Containers (even hardened ones with Kata/gVisor) impose inherent limitations on what an agent can do. Seccomp profiles, dropped capabilities, and read-only filesystems protect the host but cripple the agent. For a truly autonomous agent that needs to install packages, compile code, run services, and operate a full development environment, the isolation boundary should be a **hypervisor**, not a container runtime.

The second motivation is **remote collaboration**. The whole point of a cloud-hosted agent is that you shouldn't need to sit at your dev machine to work with it. A VM gives you something a container can't: a real machine you can jump into through a browser, watch the agent work, inspect its environment, or cooperate with it in real-time — from your phone, tablet, or any device.

### Why Not Containers?

| Concern | Container approach | VM approach |
|---------|-------------------|-------------|
| Agent needs `apt install` | Requires sudo/root, breaks `cap_drop: ALL` | Normal operation, gated by sudo plugin |
| Agent needs to run services | PID limits, no systemd, no init | Full init system, own kernel |
| Debugging agent's environment | `docker exec` into a stripped-down fs | Browser-based IDE, full OS tools |
| "Works on my machine" | Can't reproduce agent's exact state easily | Jump into the VM, see exactly what the agent sees |
| Isolation strength | Shared kernel (even with gVisor) | Own kernel, hypervisor boundary |
| Collaboration | Not practical | Browser-based terminal/IDE, real-time |

## Architecture Overview

### Infrastructure Stack

**MVP:** Separate single-node k3s cluster on a spare machine with KubeVirt. Physically isolated from the main cluster — agent VMs can only reach services explicitly exposed (NATS, orchestrator API). No Harvester, no distributed storage, just k3s + KubeVirt + ephemeral containerDisk images.

```
Main k3s cluster                    Agent k3s cluster (spare machine)
├── orchestrator                    └── KubeVirt
├── cockpit                             ├── agent-vm-job-abc123
├── databases                           ├── agent-vm-job-def456
├── NATS ◄──── (NodePort) ────────────► └── agent-vm-job-ghi789
└── ...                                     (management daemons connect to NATS)
```

**Production (future):** Harvester on 3 dedicated nodes for HA, live migration, distributed storage. See [Harvester Setup Guide](./vm_harvester_setup.md) for that path.

- **KubeVirt** provides the `VirtualMachine` / `VirtualMachineInstance` CRDs
- Agent VMs are ephemeral — created per job, destroyed on completion
- Boot images pulled from Docker Hub as `containerDisk` (no persistent storage needed for MVP)

### VM Internal Architecture

Each VM runs two fully separated layers:

```
┌─────────────────────────────────────────────────────────┐
│  MANAGEMENT PLANE (root)                                │
│                                                         │
│  ┌──────────────┐ ┌───────────┐ ┌────────────────────┐ │
│  │ sudo-plugin  │ │ monitor   │ │ remote-access      │ │
│  │              │ │           │ │ (code-server /      │ │
│  │ intercept    │ │ CPU, mem, │ │  ttyd / noVNC)      │ │
│  │ approve/deny │ │ procs,    │ │                     │ │
│  │ via orch.    │ │ network,  │ │ browser-based       │ │
│  │              │ │ anomalies │ │ workspace access    │ │
│  └──────┬───────┘ └─────┬────┘ └──────────┬──────────┘ │
│         │               │                 │             │
│  ┌──────┴───────────────┴─────────────────┴──────────┐  │
│  │              management daemon                     │  │
│  │   single process, communicates with orchestrator   │  │
│  └──────────────────────┬────────────────────────────┘  │
│                         │                               │
│                         │ NATS JetStream                │
├─────────────────────────┼───────────────────────────────┤
│  AGENT PLANE (non-root user: "agent")                   │
│                         │                               │
│  ~/workspace/           │   python agent.py             │
│  ~/tools/               │   (unprivileged user)         │
│  ~/.local/              │                               │
│                         │                               │
└─────────────────────────┴───────────────────────────────┘
```

## Management Daemon

The management daemon is a single root-level service pre-installed in the base image. It is the orchestrator's control surface inside the VM.

### Responsibilities

| Function | Description |
|----------|-------------|
| **Sudo plugin** | Intercepts `sudo` calls from the agent user. Routes the request (command, context, risk level) to the orchestrator. Orchestrator pushes to cockpit/phone notification. Human or policy engine approves/denies. Daemon allows or blocks the `sudo` call. |
| **Monitoring** | Reports resource usage (CPU, memory, disk, network), process list, listening ports, and agent state back to orchestrator at regular intervals. Detects anomalies: crypto mining signatures, reverse shells, unexpected outbound connections. |
| **Freeze / Resume** | Orchestrator can instruct the daemon to pause the agent process (`SIGSTOP`). User inspects the frozen environment (via remote access), then resumes (`SIGCONT`). Enables human review of in-progress work. |
| **Job lifecycle** | Receives job configuration on boot (via cloud-init or orchestrator API). Can swap jobs without destroying the VM. Extracts outputs on completion and signals orchestrator. |
| **Remote access** | Manages the browser-based access layer (code-server, ttyd, or noVNC). Controls session authentication and permissions. |
| **Permission control** | Enforces filesystem permissions, network policies, and tool access based on job configuration. Can lock/unlock specific directories or capabilities mid-job. |

### Communication Channel

**Decision: NATS JetStream** — the cloud workspace architecture already specifies NATS for agent↔orchestrator communication. Using NATS inside the VM keeps the messaging layer consistent. The NATS client is lightweight (~5 MB static binary or Python library). The management daemon connects to the NATS cluster running on the main k3s cluster over the internal network.

Subject namespace (draft):
```
agent.vm.{job_id}.register     — daemon announces VM is ready
agent.vm.{job_id}.heartbeat    — periodic health/resource report
agent.vm.{job_id}.status       — agent state changes (running, frozen, completed)
agent.vm.{job_id}.sudo.request — privilege escalation request
agent.vm.{job_id}.sudo.response— approve/deny from orchestrator
agent.vm.{job_id}.control      — freeze, resume, terminate commands from orchestrator
```

## Remote Access (Browser-Based)

A core feature: users can jump into any agent's VM through their browser.

### Options

| Technology | What it provides | Weight | Best for |
|------------|-----------------|--------|----------|
| **Harvester VNC console** | Basic terminal via Rancher UI | Free (built-in) | Quick inspection |
| **ttyd** | Browser terminal, lightweight | ~5 MB | Terminal-only workflows |
| **code-server** (Coder) | Full IDE in browser, file tree, terminal, extensions | ~200 MB | Development agents |
| **Apache Guacamole** | RDP/VNC/SSH gateway, multi-user, session recording | Medium | Multi-protocol, audit trail |

**Recommendation: code-server** is the primary choice. MIT licensed, 76k+ GitHub stars, actively maintained (v4.109.x as of early 2026). It's a patched VS Code served over HTTP with built-in password/token auth and PWA support. Extensions install from Open VSX (not Microsoft's marketplace due to licensing). The LinuxServer.io image (`lscr.io/linuxserver/code-server`) is well-maintained and supports `DEFAULT_WORKSPACE` for pre-configured directory opening.

Note: Gitpod rebranded to Ona (Sep 2025) and pivoted to an enterprise AI agent platform. Their self-hosted CDE is effectively dead — community images are no longer published. OpenVSCode Server (their lightweight fork) is still maintained but lacks built-in auth, making code-server the better fit for our managed VM environment.

**Gitea integration**: Gitea has no native "Open in code-server" or Codespaces-like feature ([gitea#33904](https://github.com/go-gitea/gitea/issues/33904) is open but the core team has pushed back on deep IDE integration). Our integration goes through the management daemon and orchestrator instead — the cockpit provides the "Open workspace" action, not Gitea's UI. The management daemon controls code-server sessions (auth, lifecycle) and the orchestrator routes the user to the correct VM's code-server instance. The `?folder=/path` URL parameter opens a specific directory directly.

### User Experience

1. User gets a notification on phone: *"Agent #3 wants sudo to install `texlive-full`"*
2. User approves from notification
3. User taps *"Open workspace"* → browser-based VS Code opens
4. User sees the agent's file tree, terminal, current work
5. User edits a file → agent sees the change on next `read_file`
6. User leaves → agent continues autonomously

This solves the "sit at your dev machine" problem. You manage a fleet of agents from any device.

## Image Hierarchy

A layered image system where specialized images extend a common base.

### Base Image (`agent-base`)

Every agent VM starts from this. Contains the full management stack:

```
agent-base
├── Minimal OS (Ubuntu cloud image / Fedora CoreOS)
├── Management daemon (root-level service)
│   ├── Sudo plugin
│   ├── Monitoring agent
│   ├── Freeze/resume capability
│   └── Remote access server (code-server or ttyd)
├── Python 3.12 + agent framework + dependencies
├── cloud-init hooks for per-job configuration
├── Basic tools (git, curl, jq, vim)
└── Hardened SSH (key-only, management plane only)
```

### Specialized Images

Each extends `agent-base` with domain-specific tooling:

**`agent-dev`** — Software development tasks:
```
agent-dev (extends agent-base)
├── git, gcc, make, cmake, pkg-config
├── Node.js (LTS), Rust (stable), Go
├── Docker-in-VM (for agents that build containers)
├── code-server pre-configured with dev extensions
└── Language servers (pyright, typescript, rust-analyzer)
```

**`agent-writer`** — Documentation, academic writing, creative work:
```
agent-writer (extends agent-base)
├── TeXLive (full or scheme-medium)
├── pandoc, typst
├── poppler-utils, imagemagick, ghostscript
├── Fonts: Noto, Liberation, TeX Gyre, etc.
├── CitationEngine with [full] extras
└── Zotero CLI (optional)
```

**`agent-research`** — Web research, data analysis:
```
agent-research (extends agent-base)
├── Chromium + Playwright
├── Jupyter, pandas, scipy, matplotlib
├── Data viz tools (plotly, seaborn)
└── yt-dlp, ffmpeg (media processing)
```

### Image Build Pipeline

Images are built via CI (GitHub Actions / Gitea Actions) and stored in a container/VM image registry. Harvester can pull VM images from HTTP endpoints or container registries (using `containerDisk` or `dataVolume` sources).

```
Dockerfile.agent-base  →  packer build  →  agent-base.qcow2
Dockerfile.agent-dev   →  packer build  →  agent-dev.qcow2   (FROM agent-base)
Dockerfile.agent-writer→  packer build  →  agent-writer.qcow2 (FROM agent-base)
```

Note: VM images are built with **Packer** (not Docker), using QEMU or cloud-image builders. The `Dockerfile.*` naming above is conceptual — actual build definitions would be Packer HCL templates.

## Job Lifecycle

```
1. Job created (cockpit / API / phone)
        │
        ▼
2. Orchestrator selects VM image based on expert config
        │
        ▼
3. KubeVirt creates VirtualMachineInstance from template
        │
        ▼
4. cloud-init injects:
   - Job ID, config, secrets (file-mounted)
   - Workspace seed (documents, context)
   - Network policy (egress whitelist)
        │
        ▼
5. Management daemon starts → registers with orchestrator
        │
        ▼
6. Agent process starts as unprivileged "agent" user
        │
        ▼
7. Agent works autonomously
   ├── sudo requests → routed to orchestrator → human approves/denies
   ├── monitoring data → streamed to orchestrator
   ├── user can jump in via browser at any time
   └── orchestrator can freeze/resume/swap job
        │
        ▼
8. Job completes → daemon extracts outputs → orchestrator saves
        │
        ▼
9. VM destroyed (or snapshot preserved for debugging)
```

## Security Model

The VM approach changes the security model fundamentally. Instead of restricting *what the agent can do* (container hardening), we restrict *what the agent can reach* (network/permission boundaries) and *gate privilege escalation* (sudo plugin).

### What the container security checklist becomes:

| Checklist Section | VM Equivalent |
|-------------------|---------------|
| 1. Runtime Hardening (caps, seccomp, namespaces) | **Not needed** — own kernel provides isolation |
| 2. Filesystem Security | Agent user has normal permissions. Root fs is the VM's own. Management plane files are root-owned. |
| 3. Network Isolation | **Still applies** — VM gets an internal network, egress filtered through proxy, IMDS blocked |
| 4. Resource Limits | **VM-level** — vCPU count, RAM allocation, disk size set at creation |
| 5. Privilege Models | **Sudo plugin** — agent runs as non-root, privilege escalation requires approval |
| 6. Secrets Management | Secrets injected via cloud-init into root-owned files. Agent code reads from `/run/secrets/`. Management daemon handles rotation. |
| 7. Supply Chain | Agent can install packages (with sudo approval). Base images are pre-vetted. |
| 8. Monitoring | **In-VM daemon** replaces host-level Falco. Same detection rules, different enforcement point. |

### Defense in Depth

```
Layer 0: Hypervisor (KubeVirt/QEMU) — VM cannot escape to host
Layer 1: Network policy — VM can only reach approved endpoints
Layer 2: Management daemon — monitors and controls agent behavior
Layer 3: Sudo plugin — gates privilege escalation
Layer 4: Orchestrator — human-in-the-loop for sensitive operations
```

## Comparison: Isolation Approaches

| | Container (runc) | gVisor | Kata Container | KubeVirt VM |
|--|-----------------|--------|----------------|-------------|
| **Kernel** | Shared | User-space | Own (in microVM) | Own (full VM) |
| **Isolation** | Namespaces + cgroups | Syscall interception | Hypervisor (lightweight) | Hypervisor (full) |
| **Startup** | <100ms | ~150ms | ~300ms | ~5-30s |
| **Agent freedom** | Very restricted | Restricted | Moderate | Full |
| **Can install packages** | No (read-only fs) | No | Limited | Yes (sudo plugin) |
| **Can run services** | No (PID limits) | No | Limited | Yes |
| **Browser access** | Not practical | Not practical | Not practical | Full (code-server, VNC) |
| **Human collaboration** | No | No | No | Yes |
| **Snapshot/debug** | Container logs only | Container logs only | Limited | Full VM snapshot |
| **Resource overhead** | ~10 MB | ~50 MB | ~100 MB | ~256-512 MB |
| **Best for** | Stateless functions | Untrusted code exec | Moderate isolation | Full autonomy |

## Decisions (March 2026)

The following decisions lock in the initial implementation path. Optimization and advanced features come later.

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | Communication channel | **NATS** | Already specified in cloud_workspace.md for agent↔orchestrator. One messaging system, not two. |
| 2 | Sudo plugin | **Deferred** | Get the agent running in a VM first. Sudo plugin is a management daemon feature, not a blocker for initial deployment. |
| 3 | VM startup time | **Accept 5-30s, optimize later** | Pre-warmed pools are an optimization. Long-running agent jobs amortize boot time. |
| 4 | Image registry | **Docker Hub (temporary)** | Push container disk images to Docker Hub for now. Migrate to local registry (Harbor or Harvester built-in) later. |
| 5 | Multi-agent collaboration | **Separate VMs, shared repos/databases** | Simpler isolation model. Revisit if needed. |
| 6 | Cost/density | **Spare PC: i5 4-core, 16 GB DDR4** | 2-3 agent VMs at 2 vCPUs / 2 GB each. Sufficient for MVP testing. |
| 7 | Infrastructure | **Separate k3s + KubeVirt (MVP), Harvester (future)** | MVP: single-node k3s on a spare PC, KubeVirt installed via kubectl. Physically separate from the main cluster — keeps agent blast radius contained. Future: Harvester on 3 dedicated nodes for HA, live migration, distributed storage. |

## Implementation Priority

Focus: get an agent running in a VM, accessible via browser, reporting to the orchestrator.

```
Phase 1: Infrastructure (MVP — spare PC)
  1. Install k3s on spare machine
  2. Install KubeVirt (operator + CR) via kubectl
  3. Expose NATS on main cluster via NodePort
  4. Verify basic VM creation with a test containerDisk image

Phase 2: Base Image
  5. Build agent-base image with Packer (Ubuntu cloud image)
     - Management daemon stub (registers with orchestrator, runs agent)
     - code-server pre-installed
     - Python 3.12 + agent framework dependencies
     - cloud-init hooks for job configuration
  6. Push to Docker Hub as container disk image
  7. Boot agent-base VM, verify SSH + code-server access

Phase 3: Orchestrator Integration — DONE (code, needs deployment testing)
  8. Orchestrator provisions VM via unified provisioner
     - Same-cluster: direct KubeVirt API call from orchestrator
     - Cross-cluster: publishes to NATS, VM Controller handles K8s API
     See orchestrator/services/vm_provisioner.py, nats_bridge.py
  9. cloud-init injects job config (job ID, secrets, workspace seed)
  10. Management daemon registers with orchestrator over NATS (includes VM IP)
  11. Auto-dispatch: orchestrator injects workspace.backend=remote + VM IP
      into config_override, agent connects via SSH (RemoteBackend)
  12. Phase snapshots extracted from VM to pod-local storage at each boundary
  13. VM failure recovery: detect → delete old VM → re-provision → seed → resume
  REST API: POST/GET /api/vms, GET/DELETE /api/vms/{job_id}
  Lifecycle hooks: cancel→terminate+delete, pause→freeze, complete→auto-delete
  Auto-provision: dispatcher auto-creates VMs for jobs needing workspace.backend=remote

Phase 4: Remote Access
  12. code-server accessible via cockpit "Open workspace" action
  13. Proxy/ingress routing from cockpit to per-VM code-server

Phase 5: Iterate
  14. Monitoring (resource usage, process list)
  15. Freeze/resume
  16. Sudo plugin
  17. Specialized images (agent-dev, agent-writer, agent-research)

Phase 6: Production Migration (when needed)
  18. Stand up Harvester on 3 dedicated nodes
  19. Migrate VM workloads to Harvester cluster
  20. Pre-warmed VM pools
  21. Local image registry
```

See also: [Agent Cluster Setup Guide](./vm_agent_cluster_setup.md), [Packer Templates](../../deployment/harvester/packer/)

## Open Questions (Remaining)

5. **Multi-agent collaboration**: Separate VMs with shared repos/databases for now. Revisit if latency between agents becomes a bottleneck.

6. **Orchestrator kubeconfig management**: For same-cluster deployments, the orchestrator uses in-cluster K8s config automatically (direct mode in `vm_provisioner.py`). For cross-cluster, the orchestrator doesn't need a kubeconfig at all — it publishes to NATS and the VM Controller on the agent cluster handles K8s API calls. This eliminates the need for cross-cluster kubeconfig management.

## Related Documents

- [Cloud Workspace Architecture](../cloud_workspace.md) — k3s deployment, NATS communication, storage
- [Security Checklist](../security_checklist.md) — container hardening checklist (partially superseded by VM approach)
- [Deployment](../deployment_checklist.md) — current containerized deployment
- [Datasources](../datasources.md) — external database/repo connections

## Related

- [[cloud_workspace]]
- [[security_checklist]]
- [[deployment]]
- [[datasources]]
- [[advanced_job_configuration]]
- [[tool_issues]]
