# VM-Based Agent Isolation (KubeVirt + Harvester)

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

```
Rancher (management UI)
├── Main k3s cluster (orchestrator, cockpit, databases, NATS)
└── Harvester cluster (agent VMs)
        │
        ├── agent-vm-job-abc123
        ├── agent-vm-job-def456
        └── agent-vm-job-ghi789
```

- **Rancher** manages both the main k3s cluster and the Harvester cluster
- **Harvester** (built on KubeVirt) handles VM lifecycle, storage, networking
- **KubeVirt** provides the `VirtualMachine` / `VirtualMachineInstance` CRDs
- Agent VMs are ephemeral — created per job, destroyed on completion

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
│                         │ NATS / HTTP / virtio-vsock    │
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

The daemon needs a reliable channel to the orchestrator. Options (to be decided):

| Option | Pros | Cons |
|--------|------|------|
| **virtio-vsock** | No network needed, most secure, direct host↔VM | KubeVirt support varies, more complex setup |
| **HTTP over internal network** | Simple, well-understood, works with existing orchestrator API | Requires network config, slightly weaker isolation |
| **NATS** | Already planned for agent↔orchestrator comms (cloud_workspace.md), pub/sub fits event model | Extra dependency inside VM |

The NATS approach is the most likely fit since the cloud workspace architecture already specifies NATS JetStream for agent communication.

## Remote Access (Browser-Based)

A core feature: users can jump into any agent's VM through their browser.

### Options

| Technology | What it provides | Weight | Best for |
|------------|-----------------|--------|----------|
| **Harvester VNC console** | Basic terminal via Rancher UI | Free (built-in) | Quick inspection |
| **ttyd** | Browser terminal, lightweight | ~5 MB | Terminal-only workflows |
| **code-server** (VS Code) | Full IDE in browser, file tree, terminal, extensions | ~200 MB | Development agents |
| **Apache Guacamole** | RDP/VNC/SSH gateway, multi-user, session recording | Medium | Multi-protocol, audit trail |

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

## Open Questions

1. **Communication channel**: virtio-vsock vs NATS vs HTTP for daemon↔orchestrator. NATS is the current frontrunner (already in cloud workspace architecture).

2. **Sudo plugin implementation**: Custom PAM module? Sudo plugin API (`/etc/sudo.d/`)? Wrapper binary that replaces `sudo`? Needs prototyping.

3. **VM startup time**: 5-30s is acceptable for long-running jobs but may need optimization for short tasks. Pre-warmed VM pools could help.

4. **Image size vs build time**: Full TeXLive is ~4 GB. Balance between pre-installing everything and keeping images manageable.

5. **Multi-agent collaboration**: Can two agents share a VM? Or should collaboration happen via shared repos/databases with separate VMs?

6. **Cost/density**: How many agent VMs can run concurrently on the home lab hardware? Depends on RAM and storage. Each VM needs ~512 MB-2 GB RAM minimum.

7. **Harvester vs bare KubeVirt**: Harvester is a full HCI platform (separate cluster on bare metal). Could also install KubeVirt directly on the existing k3s cluster. Trade-off: dedicated VM infrastructure vs simpler setup.

## Related Documents

- [Cloud Workspace Architecture](../cloud_workspace.md) — k3s deployment, NATS communication, storage
- [Security Checklist](../security_checklist.md) — container hardening checklist (partially superseded by VM approach)
- [Deployment](../deployment.md) — current containerized deployment
- [Datasources](../datasources.md) — external database/repo connections
