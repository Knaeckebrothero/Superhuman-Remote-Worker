---
tags:
  - security
  - agent-architecture
  - orchestrator
aliases:
  - sudo gate
  - approval daemon
  - privilege escalation control
related:
  - "[[security_checklist]]"
  - "[[tool_issues]]"
  - "[[cockpit_ds]]"
  - "[[deployment]]"
---

# Sudo Approval Plugin: Human-in-the-Loop Privilege Escalation for LLM Agents

> **Status: Implemented.** This is the original concept design document. For the detailed implementation roadmap, deployment notes, and current file layout, see `docs/done/sudo_approval_gate.md`.

## Concept

A custom `sudo` plugin that intercepts privilege escalation requests from an autonomous agent, freezes them, and forwards them to an external approval authority (the orchestrator). The agent cannot proceed with the privileged operation until a human operator approves or denies the request via the cockpit UI.

This solves the fundamental tension between giving agents enough power to be useful (installing packages, modifying system state) and maintaining control over what they actually do with that power.

## Why This Works

Traditional container security is binary — either the agent has sudo or it doesn't. An approval gate introduces a third option: the agent *can* escalate, but only with explicit human consent for each operation.

The security model holds because:

1. **The plugin runs as root** — the agent process (running as `agent` user) cannot modify, replace, or bypass it
2. **The approval daemon runs as a service user with wheel** — separate from the agent's user context, inaccessible to the agent
3. **The decision comes from outside** — the orchestrator/cockpit lives on a different machine entirely
4. **sudo's plugin architecture is a kernel trust boundary** — it's invoked by the SUID sudo binary before the agent gains any privileges

The agent cannot:
- Write to `/etc/sudoers` or `/etc/sudoers.d/` (owned by root, mode 0440)
- Replace the plugin shared object (owned by root in `/usr/libexec/sudo/`)
- Write to the approval daemon's Unix socket (owned by root/wheel)
- Fake an approval response (the daemon validates responses cryptographically or via the external orchestrator)
- Kill or manipulate the approval daemon (different UID, agent has no CAP_SYS_PTRACE)

## Architecture

```
┌─────────────────── VM / Kata Container ───────────────────┐
│                                                           │
│  ┌─────────────┐    sudo     ┌──────────────────────┐     │
│  │ Agent        │───────────>│ sudo binary (SUID)   │     │
│  │ user: agent  │            │ loads plugin chain    │     │
│  │ no wheel     │            └──────────┬───────────┘     │
│  └─────────────┘                        │                 │
│                                         │ invoke          │
│                                         ▼                 │
│                              ┌──────────────────────┐     │
│                              │ approval_plugin.so   │     │
│                              │ (custom sudo plugin) │     │
│                              └──────────┬───────────┘     │
│                                         │                 │
│                                    Unix socket            │
│                                    /run/sudo-gate/        │
│                                    owner: root            │
│                                         │                 │
│                                         ▼                 │
│                              ┌──────────────────────┐     │
│                              │ sudo-gated            │     │
│                              │ (approval daemon)    │     │
│                              │ user: gate (wheel)   │     │
│                              └──────────┬───────────┘     │
│                                         │                 │
└─────────────────────────────────────────┼─────────────────┘
                                          │ HTTP/gRPC
                                          ▼
                               ┌─────────────────────┐
                               │ Orchestrator API     │
                               │ POST /api/agents/    │
                               │   {id}/sudo-request  │
                               └──────────┬──────────┘
                                          │
                                          ▼
                               ┌─────────────────────┐
                               │ Cockpit UI           │
                               │ Approve / Deny       │
                               └─────────────────────┘
```

## Components

### 1. Sudo Plugin (`approval_plugin.so`)

A shared library implementing the `sudo` approval plugin API (`sudo_plugin(5)`).

**Language:** C (required by sudo's plugin ABI)

**Lifecycle:**
1. sudo loads the plugin via `/etc/sudo.conf`
2. Plugin's `open()` callback is called with the command, user, and environment
3. Plugin connects to the approval daemon via Unix socket at `/run/sudo-gate/gate.sock`
4. Plugin sends a JSON request: `{"user": "agent", "command": "apt-get install nodejs", "cwd": "/app", "timestamp": "..."}`
5. Plugin blocks (with configurable timeout, e.g., 300s)
6. Daemon responds: `{"approved": true, "approver": "operator@cockpit", "reason": "..."}` or `{"approved": false, ...}`
7. Plugin returns `SUDO_RC_ACCEPT` or `SUDO_RC_REJECT` to sudo

**Configuration** (`/etc/sudo.conf`):
```
Plugin approval_plugin approval_plugin.so
  socket_path=/run/sudo-gate/gate.sock
  timeout=300
  default_action=deny
```

**Key implementation details:**
- The plugin must be statically linked or depend only on libc (no Python/Node runtime)
- Timeout defaults to deny (if the daemon is unreachable or the operator doesn't respond, the command fails)
- The plugin logs all requests and decisions to syslog regardless of outcome

### 2. Approval Daemon (`sudo-gated`)

A lightweight daemon that brokers between the in-VM sudo plugin and the external orchestrator.

**Language:** Go or Rust (single static binary, no runtime dependencies, runs as a system service)

**Runs as:** `gate` user with `wheel` group membership (or root). Definitely not the `agent` user.

**Responsibilities:**
- Listens on Unix socket `/run/sudo-gate/gate.sock` (owner: root, mode: 0660, group: root)
- Receives requests from the sudo plugin
- Forwards them to the orchestrator API via HTTP
- Maintains a request queue with unique IDs
- Polls or uses WebSocket to receive approval/denial from orchestrator
- Responds to the waiting plugin with the decision
- Logs all requests, decisions, and timeouts

**Systemd unit** (`/etc/systemd/system/sudo-gated.service`):
```ini
[Unit]
Description=Sudo Approval Gate Daemon
After=network.target

[Service]
Type=simple
User=gate
Group=wheel
ExecStart=/usr/local/bin/sudo-gated --config /etc/sudo-gate/config.yaml
Restart=always
RestartSec=5

# Hardening
ProtectSystem=strict
ProtectHome=true
NoNewPrivileges=true
CapabilityBoundingSet=
ReadWritePaths=/run/sudo-gate

[Install]
WantedBy=multi-user.target
```

### 3. Orchestrator Integration

**New endpoints:**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/agents/{id}/sudo-request` | Daemon submits a new approval request |
| `GET` | `/api/agents/{id}/sudo-requests` | List pending requests for an agent |
| `POST` | `/api/agents/{id}/sudo-requests/{req_id}/approve` | Operator approves |
| `POST` | `/api/agents/{id}/sudo-requests/{req_id}/deny` | Operator denies |

**Request schema:**
```json
{
  "request_id": "uuid",
  "agent_id": "unrestricted-01",
  "job_id": "job-uuid",
  "user": "agent",
  "command": "apt-get install -y libxml2-dev",
  "cwd": "/app/workspace/job_abc123",
  "timestamp": "2026-02-13T14:30:00Z",
  "timeout_at": "2026-02-13T14:35:00Z"
}
```

**Response schema:**
```json
{
  "request_id": "uuid",
  "approved": true,
  "approver": "operator@cockpit",
  "reason": "Agent needs libxml2-dev for XML parsing task",
  "decided_at": "2026-02-13T14:31:15Z"
}
```

**Notification flow:** The orchestrator pushes the pending request to the cockpit via WebSocket. The cockpit shows a toast/modal with the command, the agent's current job context, and approve/deny buttons. If the operator doesn't respond before `timeout_at`, the request is auto-denied.

### 4. Cockpit UI

A new panel or modal in the cockpit for sudo approval:

- Shows pending requests with agent ID, job context, and the exact command
- Displays the agent's recent activity (last N tool calls from audit trail) for context
- Approve / Deny buttons with optional reason field
- Auto-deny countdown timer showing time remaining
- History of past approvals/denials for audit

### 5. MCP Tool

```
approve_sudo_request(agent_id, request_id, approved, reason)
list_sudo_requests(agent_id, status="pending")
```

This allows approval directly from Claude Code without opening the cockpit.

## VM Setup

The approval gate requires a full init system (systemd) for the daemon. This means the agent environment should be a VM rather than a minimal container:

**Option A: Kata Containers**
- Use `kata-runtime` as the OCI runtime
- The container specification runs inside a lightweight QEMU/Firecracker VM
- systemd runs as PID 1 inside the VM
- The sudo plugin and daemon are baked into the image

**Option B: Firecracker microVM**
- Build a custom rootfs with the agent application + approval infrastructure
- Launch via Firecracker API
- Minimal memory footprint (~128MB base + agent workload)
- Fastest boot time (~125ms)

**Option C: Full VM (libvirt/QEMU)**
- Standard VM with a lightweight distro (Alpine, Fedora CoreOS)
- Most flexibility, highest overhead
- Easiest to develop and debug

For development, Option C is simplest. For production, Option A (Kata) integrates with the existing Docker/Podman workflow.

## Security Analysis

**What the agent CAN do:**
- Run any command as `agent` user (unprivileged)
- Request sudo for any command (request is logged and gated)
- See that a request was approved or denied

**What the agent CANNOT do:**
- Execute privileged commands without approval
- Modify the plugin, daemon, or sudoers configuration
- Fake approval responses (communication is root-owned Unix socket → external HTTP)
- Kill or inspect the approval daemon (different UID, no ptrace capability)
- Bypass sudo entirely (no SUID binaries accessible, `no-new-privileges` set)

**Remaining attack vectors:**
- **Social engineering the operator**: The agent could craft a convincing reason in its tool call description for why it needs `sudo rm -rf /`. Mitigation: The cockpit UI shows the raw command, not the agent's description.
- **Timing attacks**: The agent could flood sudo requests to overwhelm the operator. Mitigation: Rate limiting on the daemon (e.g., max 3 pending requests per agent).
- **Approved command abuse**: Agent requests `sudo apt-get install foo` but the approved sudo session has a broader scope. Mitigation: The plugin passes the exact command to sudo, not a shell session. Each command is a separate approval.
- **Post-approval persistence**: Once a package is installed, the agent has it forever (within the VM lifetime). This is acceptable — the VM is ephemeral.

## Implementation Effort

| Component | Language | Estimated Effort | Complexity |
|-----------|----------|-----------------|------------|
| `approval_plugin.so` | C | 2-3 days | Medium (sudo plugin ABI is well-documented but C) |
| `sudo-gated` daemon | Go/Rust | 3-5 days | Medium (Unix socket server + HTTP client) |
| Orchestrator endpoints | Python | 1 day | Low (standard FastAPI CRUD) |
| Cockpit UI panel | Angular | 2-3 days | Medium (WebSocket integration, approval flow) |
| MCP tools | Python | 0.5 day | Low (follows existing pattern) |
| VM image build | Dockerfile/Packer | 1-2 days | Medium (systemd, user setup, plugin installation) |
| Integration testing | — | 2-3 days | High (end-to-end approval flow across 4 components) |
| **Total** | | **~12-17 days** | |

## Future Extensions

- **Auto-approval rules**: Operator defines patterns that are always approved (e.g., `apt-get install *-dev`), reducing approval fatigue while keeping dangerous commands gated
- **Command rewriting**: The approval UI could let the operator modify the command before approving (e.g., strip a `-rf` flag)
- **Approval delegation**: Allow the LLM orchestrator itself to auto-approve low-risk commands based on a policy engine, escalating only high-risk ones to the human
- **Audit integration**: All sudo requests and decisions flow into the MongoDB audit trail alongside the agent's regular tool calls
- **Multi-agent coordination**: In a multi-agent setup, one agent's approval could be contingent on another agent's state (e.g., "only allow network changes if the monitoring agent confirms health")

## Related

- [[security_checklist]] - Container security hardening checklist for agents
- [[deployment]] - Deployment infrastructure and VM configuration
- [[cockpit_ds]] - Cockpit UI for monitoring and managing agents
- [[tool_issues]] - Tool phase filtering and execution control
