---
tags:
  - security
  - agent-architecture
  - deployment
  - infrastructure
related:
  - "[[security_checklist]]"
  - "[[pod_runtime]]"
  - "[[vm_backend]]"
  - "[[sudo_permissions]]"
  - "[[sudo_approval_gate]]"
---

# Hardened Default Agent Container

> **Status: Design.** Implements the container hardening roadmap from `docs/security_checklist.md`, making the secure image the default for all container-based agent jobs and reserving VMs for workloads that genuinely need system-level access.

## Problem

Today the agent container (`docker/Dockerfile.agent`) ships with passwordless sudo, a full dev toolchain, no capability restrictions, and no K8s-level security enforcement. This was acceptable when the project was single-user/private, but it creates a fundamentally wrong default: **every agent gets root-equivalent access even when it only needs a Python interpreter and API keys**.

The existing runtime tiers:

| Runtime | Isolation | sudo | Best for |
|---------|-----------|------|----------|
| Container (current) | Namespace/cgroup, no hardening | Passwordless, unrestricted | Everything (by default) |
| VM (KubeVirt) | Hypervisor, sudo approval gate | Gated per-command by human | Untrusted code, system admin |

The problem is that the container tier offers **no middle ground**. An agent doing Python data analysis or writing a report gets the same privileges as one that needs to compile C++ and install system libraries. Most jobs (document processing, research, database ops, Python coding) never need `sudo`.

### Specific risks in the current setup

1. **Passwordless sudo** (`srw ALL=(ALL) NOPASSWD: ALL` in Dockerfile) gives any agent full root inside the container
2. **No `securityContext`** in `deployment/21-agent.yaml` -- K8s doesn't enforce non-root, doesn't drop capabilities
3. **No `no-new-privileges`** -- SUID binaries can escalate
4. **Writable root filesystem** -- an agent (or prompt injection) can modify system binaries
5. **Secrets in environment variables** -- readable via `/proc/self/environ` or `env`
6. **No PID limits** -- fork bomb vulnerability
7. **No network policy** -- unrestricted egress (beyond VPN sidecar routing)

## Design Principles

1. **Secure by default, permissive by exception.** The hardened container is the default. VMs are the opt-in escape hatch for jobs that need system-level access.
2. **No sudo in containers.** If a job needs to install packages at runtime, it needs a VM. The container ships everything the agent needs pre-installed.
3. **Least privilege.** Drop all capabilities, read-only root, no privilege escalation. The agent runs as a regular user with write access only to `/workspace` and `/tmp`.
4. **Defense in depth.** Container hardening, K8s securityContext, and the existing shell command blocklist all reinforce each other.
5. **Backward compatible.** The same image, same agent code. Changes are in the Dockerfile, K8s manifests, and compose files -- not in the Python codebase.

## What Changes

### 1. Dockerfile.agent -- Remove sudo, lock down filesystem

```dockerfile
# REMOVE these lines:
#   sudo \                                          (from apt-get install)
#   RUN echo "srw ALL=(ALL) NOPASSWD: ALL" > ...    (passwordless sudo grant)

# The agent runs as non-root user 'srw' (already the case).
# Without sudo installed, there is no privilege escalation path.
```

**Packages to keep:** The container retains its full dev toolchain (git, build-essential, cmake, nodejs, ripgrep, tmux, Playwright, etc.). These are needed for coding tasks. The difference is that the agent runs them as a regular user, not as root via sudo. Python packages install into the venv (user-writable). npm packages install into the workspace. The only things that require root are system package installation (`apt-get install`) and service management (`systemctl`) -- both are VM territory.

**User-writable pip/npm:** Add paths so the agent can install Python and Node packages without sudo:

```dockerfile
# User-writable package directories (no sudo needed)
ENV PIP_TARGET=/home/srw/.local/lib/python3.11/site-packages \
    PATH="/home/srw/.local/bin:${PATH}" \
    npm_config_prefix=/home/srw/.npm-global

RUN mkdir -p /home/srw/.local/bin /home/srw/.local/lib/python3.11/site-packages \
             /home/srw/.npm-global \
    && chown -R srw:srw /home/srw
```

This lets agents `pip install pandas` and `npm install express` as the `srw` user without needing root.

### 2. Kubernetes securityContext -- Enforce at the pod level

Add to `deployment/21-agent.yaml`:

```yaml
containers:
  - name: agent
    securityContext:
      runAsNonRoot: true
      runAsUser: 1000
      runAsGroup: 1000
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      seccompProfile:
        type: RuntimeDefault
      capabilities:
        drop:
          - ALL
```

And volume mounts for writable directories:

```yaml
volumeMounts:
  - name: workspace
    mountPath: /workspace
  - name: tmp
    mountPath: /tmp
  - name: run
    mountPath: /run
  - name: home-srw
    mountPath: /home/srw
  # ... existing mounts
volumes:
  - name: tmp
    emptyDir:
      medium: Memory
      sizeLimit: 256Mi
  - name: run
    emptyDir:
      medium: Memory
      sizeLimit: 16Mi
  - name: home-srw
    emptyDir:
      sizeLimit: 512Mi
```

**Why `emptyDir` instead of `tmpfs`:** In K8s, `emptyDir` with `medium: Memory` creates a tmpfs-backed volume. `sizeLimit` enforces the quota. This is the K8s-native equivalent of Docker's `tmpfs` mounts.

**Why `/home/srw` needs to be writable:** tmux creates its socket in `$HOME` or `/tmp`, pip writes to `~/.cache`, Playwright stores browser profile data. With `readOnlyRootFilesystem: true`, the home directory embedded in the image is read-only, so we overlay it with an emptyDir.

### 3. Resource limits -- Add PID limit

```yaml
resources:
  requests:
    memory: "512Mi"
    cpu: "250m"
  limits:
    memory: "2Gi"
    cpu: "1000m"
# Note: PID limits are set via LimitRange or container runtime config
# in K8s (not directly in pod spec for most runtimes)
```

For PID limits, create a `LimitRange` in the namespace:

```yaml
# deployment/00b-limit-range.yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: agent-limits
  namespace: superhuman-remote-worker
spec:
  limits:
    - type: Container
      default:
        memory: "2Gi"
        cpu: "1000m"
      defaultRequest:
        memory: "512Mi"
        cpu: "250m"
```

PID limits are enforced at the container runtime level (containerd config) or via a PodSecurity admission policy, not in the pod spec directly. Document this as a cluster-level configuration step.

### 4. Docker Compose -- Mirror the K8s hardening

```yaml
# docker-compose.yaml and docker-compose.local.yaml
agent:
  # ... existing config ...
  cap_drop:
    - ALL
  security_opt:
    - no-new-privileges:true
  read_only: true
  tmpfs:
    - /tmp:rw,noexec,nosuid,size=256m
    - /run:rw,noexec,nosuid,size=16m
  volumes:
    - workspace_data:/workspace:z
    - agent_home:/home/srw       # Named volume for writable home
  deploy:
    resources:
      limits:
        cpus: '1.0'
        memory: 2G
        pids: 256
  # ... rest unchanged ...

volumes:
  agent_home:     # Add to volumes section
```

### 5. Sudo intercept -- Freeze and offer VM upgrade

Instead of hard-blocking `sudo` with a generic error, the shell manager intercepts sudo attempts, freezes the job, and gives the operator the option to upgrade the job to a VM. The agent doesn't notice a difference -- from its perspective, the command is just "pending" until the operator decides.

**Config change** (`config/defaults.yaml`):

```yaml
shell:
  mode: stateless
  blocked_commands: [reboot, shutdown, poweroff, halt, init]  # hard blocks (unchanged)
  sudo_action: freeze    # "freeze" (default, intercept + offer VM upgrade) or "block" (hard reject)
```

`sudo` moves from the blocklist to its own `sudo_action` key. This keeps the hard-block behavior for destructive system commands while giving sudo a smarter path.

**ShellManager change** (`src/tools/coding/shell_manager.py`):

The `_check_blocked()` method gains a sudo-specific branch. When the first word is `sudo` and `sudo_action` is `freeze`, it returns a special marker string instead of a generic block message:

```python
# New attribute on ShellManager.__init__():
self.sudo_action = sudo_action or "freeze"  # from config shell.sudo_action

def _check_blocked(self, command: str) -> str | None:
    """Return error message if command is blocked, else None."""
    if not command.strip():
        return None
    first_word = command.strip().split()[0]

    # Sudo intercept: freeze for VM upgrade instead of hard block
    if first_word == "sudo":
        if self.sudo_action == "freeze":
            return "SUDO_FREEZE_REQUESTED"  # sentinel consumed by tool wrapper
        else:
            return (
                f"Command blocked: 'sudo' is not available in this container. "
                f"System package installation requires a VM runtime."
            )

    # Hard blocks for destructive commands
    if first_word in self.blocked_commands:
        return (
            f"Command blocked: '{first_word}' is not allowed. "
            f"Blocked commands: {', '.join(sorted(self.blocked_commands))}"
        )
    return None
```

**Tool wrapper** (`src/tools/coding/shell_tools.py`):

The `run_command` / `shell_execute` tool function checks for the sentinel and triggers a freeze via the tool context:

```python
blocked = shell_manager._check_blocked(command)
if blocked == "SUDO_FREEZE_REQUESTED":
    # Request job freeze -- agent pauses, operator sees upgrade prompt
    context.request_freeze({
        "freeze_type": "vm_upgrade_required",
        "reason": "Agent attempted a sudo command that requires VM-level access.",
        "command": command,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return (
        "This command requires elevated privileges (sudo). "
        "The job has been paused while the operator decides whether to "
        "upgrade this job to a VM environment. You do not need to take "
        "any action -- the job will resume automatically if approved."
    )
elif blocked:
    return blocked
```

The `context.request_freeze()` mechanism already exists (`src/tools/context.py:410`) and is consumed by the graph's `audited_tools` node (`src/graph.py:2942`). It writes `output/job_frozen.json` with the freeze data and sets `should_stop=True`, gracefully pausing the agent at a clean checkpoint boundary. This is the same mechanism used by blocking `send_message`.

**What the agent sees:** A tool result saying "paused, will resume if approved." The agent doesn't crash, doesn't need error handling -- it just stops at the next graph checkpoint. When resumed (with or without a VM), it picks up from exactly where it left off.

### 6. NetworkPolicy -- Restrict egress

```yaml
# deployment/21c-agent-network-policy.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: srw-agent-egress
  namespace: superhuman-remote-worker
spec:
  podSelector:
    matchLabels:
      app: srw-agent
  policyTypes:
    - Egress
  egress:
    # Allow DNS
    - to: []
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    # Allow traffic to VPN sidecars (same namespace)
    - to:
        - podSelector:
            matchLabels:
              app.kubernetes.io/component: vpn
    # Allow orchestrator communication
    - to:
        - podSelector:
            matchLabels:
              app: srw-orchestrator
      ports:
        - protocol: TCP
          port: 8085
    # Allow database access (same namespace)
    - to:
        - podSelector:
            matchLabels:
              app: srw-postgres
      ports:
        - protocol: TCP
          port: 5432
    - to:
        - podSelector:
            matchLabels:
              app: srw-postgres-vector
      ports:
        - protocol: TCP
          port: 5432
    - to:
        - podSelector:
            matchLabels:
              app: srw-mongodb
      ports:
        - protocol: TCP
          port: 27017
    - to:
        - podSelector:
            matchLabels:
              app: srw-neo4j
      ports:
        - protocol: TCP
          port: 7687
    # Allow Gitea
    - to:
        - podSelector:
            matchLabels:
              app: srw-gitea
      ports:
        - protocol: TCP
          port: 3000
    # Block cloud metadata service (IMDS)
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 169.254.169.254/32
```

**Note:** The agent reaches external APIs (OpenAI, Anthropic, etc.) through the VPN sidecar, not directly. The NetworkPolicy allows VPN sidecar traffic, which handles external egress. Direct internet access from the agent pod is not needed and should not be allowed.

### 6. Orchestrator -- Handle `vm_upgrade_required` freeze type

The approve endpoint (`orchestrator/main.py:3685`, `POST /api/jobs/{job_id}/approve`) already dispatches on `freeze_type`. Add a new branch for `vm_upgrade_required`:

```python
if freeze_type == "vm_upgrade_required":
    # VM Upgrade flow:
    # 1. Provision a VM for this job
    # 2. Seed the VM workspace from the container workspace (rsync/SFTP)
    # 3. Update job context with VM details
    # 4. Resume the job -- agent resumes from checkpoint on the VM
    #
    # The agent's checkpoint is intact (graceful freeze).
    # On resume, the RemoteBackend detects the VM context and
    # delegates shell operations to the VM via SSH.

    job_id_str = str(job["id"])

    # Provision VM (reuses existing vm_provisioner)
    vm_ok = await vm_provisioner.create_vm(
        job_id=job_id_str,
        agent_config=job.get("config_name", "defaults"),
        description=f"VM upgrade for job {job_id_str[:8]}",
    )
    if not vm_ok:
        raise HTTPException(500, "Failed to provision VM for upgrade")

    # Wait for VM to become ready (IP assigned)
    # The orchestrator already handles this via NATS vm.lifecycle.status
    # or direct K8s API polling -- same as normal VM dispatch

    # Remove frozen file, update status
    # ... (same pattern as phase_boundary approval)

    # Resume the job on the now-VM-backed workspace
    # The resume endpoint handles agent selection and dispatch
    return {
        "status": "approved_vm_upgrade",
        "job_id": job_id,
        "freeze_type": freeze_type,
        "vm_provisioned": True,
    }
```

**New REST endpoint** for the cockpit upgrade button:

```
POST /api/jobs/{job_id}/upgrade-to-vm
```

This is a convenience wrapper that combines approve + VM provision + resume into a single action. It:
1. Validates the job is frozen with `freeze_type: vm_upgrade_required`
2. Provisions a VM via `vm_provisioner.create_vm()`
3. Waits for the VM to be ready (IP assigned, SSH reachable)
4. Seeds the VM workspace from the container workspace (the existing phase snapshot or workspace files)
5. Updates job context with `vm` details (SSH host, port, etc.)
6. Removes `job_frozen.json`
7. Resumes the job -- the agent picks up from checkpoint with `RemoteBackend` now active

The operator can also choose **not** to upgrade. In that case, they can:
- **Resume without VM**: The frozen file is removed, the job resumes in the container, and the agent gets the tool result saying sudo isn't available (it adapts or tries a different approach).
- **Cancel the job**: Standard cancel flow.

### 7. Cockpit -- "Upgrade to VM" button

When the cockpit detects a frozen job with `freeze_type: vm_upgrade_required`, the job detail view shows:

```
┌─ Job Frozen: Sudo Required ─────────────────────────────────┐
│                                                               │
│  The agent attempted a privileged command:                     │
│  ┌──────────────────────────────────────────────────────┐     │
│  │ sudo apt-get install -y libxml2-dev                  │     │
│  └──────────────────────────────────────────────────────┘     │
│                                                               │
│  This job is running in a hardened container without sudo      │
│  access. You can upgrade it to a VM to allow privileged       │
│  commands (with the sudo approval gate), or resume without    │
│  a VM (the agent will adapt).                                 │
│                                                               │
│  [Upgrade to VM]  [Resume without VM]  [Cancel Job]           │
│                                                               │
│  ℹ Upgrading is one-way. The job will continue on the VM      │
│    with full sudo access (gated by the approval system).      │
└───────────────────────────────────────────────────────────────┘
```

The cockpit already renders frozen jobs with a review UI (the approve/reject panel for `job_complete` and `phase_boundary` freeze types). This adds a third variant for `vm_upgrade_required` with the upgrade-specific buttons.

**SSE notification:** The existing SSE event stream (`/api/jobs/events`) already broadcasts job status changes. When the job freezes, the cockpit receives the event and can show a toast notification: "Job {id} needs sudo -- upgrade to VM?"

## What Does NOT Change

| Component | Why |
|-----------|-----|
| **Agent graph/core** | No changes. The freeze mechanism (`context.request_freeze()` + `audited_tools` node) already exists. The agent resumes from checkpoint identically whether on container or VM. |
| **Agent configs** (`config/*.yaml`) | Only `defaults.yaml` gains `sudo_action: freeze`. Expert configs inherit it. |
| **VM runtime** | VMs are unaffected. A job that starts on a VM never hits the sudo intercept (sudo is available via the approval gate). |
| **CI/CD pipeline** | The `build-agent` job in `.github/workflows/production.yml` builds from `Dockerfile.agent` with no build args -- it continues to work unchanged. |
| **Checkpoint/resume** | The upgrade is transparent to the resume system. It's just a freeze → approve → resume cycle, same as `blocking_message` or `phase_boundary`. |

## Runtime Selection Matrix

With this change, the runtime tiers become:

| Runtime | Isolation | sudo | Install packages | Best for |
|---------|-----------|------|-------------------|----------|
| **Container (hardened)** | Namespace/cgroup + seccomp + drop ALL + read-only root | Intercepted → freeze + upgrade prompt | `pip install` / `npm install` as user only | Research, writing, Python coding, DB ops, document processing |
| **VM (KubeVirt)** | Hypervisor + sudo approval gate | Gated per-command | Full `apt-get install`, `systemctl`, etc. | System admin, C/C++ builds needing system libs, untrusted code |
| **Container → VM (upgrade)** | Starts as container, upgrades on first sudo attempt | Gated (after upgrade) | Full (after upgrade) | Jobs where sudo need is uncertain upfront |

The key insight: **runtime selection doesn't have to be a decision made before the job starts.** Most jobs never need sudo. For the ones that do, the upgrade happens seamlessly mid-job. The operator doesn't need to guess upfront.

The decision of which runtime to use is already handled by the dispatcher (`_job_needs_vm()` in orchestrator/main.py). Jobs explicitly requesting VMs get them immediately. All others start on the hardened container and can upgrade on demand.

## Implementation Roadmap

### Phase 1: Dockerfile hardening (low risk, high impact)

**Changes:**
- Remove `sudo` package and the `sudoers.d` rule from `docker/Dockerfile.agent`
- Add user-writable pip/npm directories
- Ensure all runtime directories (`/home/srw/.cache`, `/home/srw/.local`) are pre-created with correct ownership

**Validation:**
- Build image locally: `podman build -t srw-agent:hardened -f docker/Dockerfile.agent .`
- Run agent with a test job, verify shell commands work as `srw` user
- Verify `pip install <package>` works without sudo
- Verify `sudo` is not found (`command not found`)

**Rollback:** Revert the Dockerfile. No other components are affected.

### Phase 2: Sudo intercept + freeze mechanism

**Changes:**
- Add `sudo_action: freeze` to `config/defaults.yaml` under `shell:`
- Modify `ShellManager._check_blocked()` to return `SUDO_FREEZE_REQUESTED` sentinel for sudo commands
- Add `sudo_action` parameter to `ShellManager.__init__()` (from config `shell.sudo_action`)
- Update `run_command` / `shell_execute` tool wrapper to detect sentinel and call `context.request_freeze()` with `freeze_type: vm_upgrade_required`

**Files:**
- `config/defaults.yaml` -- add `sudo_action: freeze`
- `src/tools/coding/shell_manager.py` -- sudo intercept in `_check_blocked()`, new `sudo_action` param
- `src/tools/coding/shell_tools.py` -- detect sentinel, call `context.request_freeze()`
- `src/agent.py` -- pass `sudo_action` from config to ShellManager

**Validation:**
- Run agent, have it attempt `sudo apt-get install something`
- Verify job freezes with `freeze_type: vm_upgrade_required` in `output/job_frozen.json`
- Verify agent receives the "paused, will resume if approved" message
- Verify job can be resumed (without VM) and agent adapts
- Existing tests pass (`pytest tests/`)

### Phase 3: K8s securityContext + emptyDir volumes

**Changes:**
- Add `securityContext` block to `deployment/21-agent.yaml` (container-level)
- Add `emptyDir` volumes for `/tmp`, `/run`, `/home/srw`
- Add corresponding `volumeMounts`

**Validation:**
- Deploy to cluster, verify pod starts and passes health checks
- Verify agent can process a job end-to-end
- Verify `readOnlyRootFilesystem` is enforced: `touch /etc/test` should fail
- Verify capabilities are dropped: `cat /proc/1/status | grep Cap` shows minimal set

**Rollback:** Revert `21-agent.yaml`. Fleet auto-syncs the previous version.

### Phase 4: Docker Compose hardening

**Changes:**
- Add `cap_drop`, `security_opt`, `read_only`, `tmpfs` to agent service in `docker-compose.yaml` and `docker-compose.local.yaml`
- Add `agent_home` named volume

**Validation:**
- `podman-compose -f docker-compose.local.yaml up agent` starts cleanly
- Agent processes a test job
- Verify read-only root: `podman exec srw-agent touch /etc/test` fails

### Phase 5: NetworkPolicy

**Changes:**
- Create `deployment/21c-agent-network-policy.yaml`

**Validation:**
- Deploy, verify agent can reach orchestrator, databases, VPN sidecars
- Verify agent cannot reach IMDS (`curl 169.254.169.254` times out)
- Verify agent cannot reach arbitrary external IPs directly (only through VPN sidecar)

**Rollback:** Delete the NetworkPolicy manifest. NetworkPolicies are additive -- removing them restores default allow-all.

### Phase 6: Orchestrator VM upgrade endpoint

**Changes:**
- Add `vm_upgrade_required` branch to the approve endpoint (`orchestrator/main.py`)
- Add `POST /api/jobs/{job_id}/upgrade-to-vm` convenience endpoint
- Workspace seeding: copy workspace files from container PVC to VM via SSH/SFTP (reuses existing `RemoteBackend` seeding logic from VM dispatch flow)

**Files:**
- `orchestrator/main.py` -- new freeze_type handler + upgrade endpoint
- Potentially `orchestrator/services/vm_provisioner.py` -- helper for upgrade-specific provisioning

**Validation:**
- Freeze a job via sudo intercept
- Call `POST /api/jobs/{id}/upgrade-to-vm`
- Verify VM is provisioned, workspace seeded, job resumes on VM
- Verify sudo commands now work (gated by approval system)
- Verify "resume without VM" also works (job continues in container, agent adapts)

### Phase 7: Cockpit upgrade UI

**Changes:**
- Add `vm_upgrade_required` freeze type rendering to the job detail component
- Show the attempted sudo command, upgrade/resume/cancel buttons
- Wire buttons to the orchestrator endpoints

**Files:**
- `cockpit/src/app/job-detail/` (or equivalent component)
- Reuses existing frozen-job UI patterns

**Validation:**
- Create a job, trigger sudo intercept
- Verify cockpit shows the upgrade prompt with the attempted command
- Click "Upgrade to VM" -- verify full flow
- Click "Resume without VM" -- verify agent continues

### Phase 8: Pod Runtime integration (follows `pod_runtime.md`)

When the Pod Runtime feature (`docs/features/pod_runtime.md`) is implemented, the dynamically created agent pods should inherit the same hardened securityContext. The `PodProvisioner._build_job_manifest()` method should include the securityContext, emptyDir volumes, and resource limits from Phase 3.

## Phase Dependencies

```
Phase 1 (Dockerfile) ──────────── independent, do first
Phase 2 (sudo intercept) ─────── independent of Phase 1 (works even with sudo installed)
Phase 3 (K8s securityContext) ──── depends on Phase 1 (image must work without sudo)
Phase 4 (Compose hardening) ────── depends on Phase 1
Phase 5 (NetworkPolicy) ────────── independent
Phase 6 (orchestrator upgrade) ─── depends on Phase 2 (needs freeze_type to exist)
Phase 7 (cockpit UI) ──────────── depends on Phase 6 (needs endpoint)
Phase 8 (Pod Runtime) ──────────── depends on Phase 3 + pod_runtime.md
```

**Recommended shipping order:**
- **PR 1:** Phases 1 + 2 (Dockerfile + sudo intercept) -- the core security improvement
- **PR 2:** Phases 3 + 4 (K8s + Compose hardening) -- enforcement layer
- **PR 3:** Phase 5 (NetworkPolicy) -- independent, low risk
- **PR 4:** Phases 6 + 7 (VM upgrade flow) -- the upgrade feature

## Trade-offs

| Decision | Trade-off | Mitigation |
|----------|-----------|------------|
| **No sudo in containers** | Agents can't install system packages at runtime | Pre-install everything in the image. User-writable pip/npm for Python/Node packages. Seamless VM upgrade on first sudo attempt. |
| **Sudo freezes the job** | Brief pause when agent hits sudo for the first time | Agent receives a clear message and stops cleanly. Operator can approve upgrade in seconds. Agent adapts if resumed without VM. |
| **One-way upgrade** | Can't downgrade back to container after VM upgrade | Acceptable -- once an agent needs sudo, it's likely to need it again. The VM's sudo approval gate provides ongoing governance. |
| **Read-only root filesystem** | Can't write to `/etc`, `/usr`, etc. | emptyDir overlays for `/tmp`, `/run`, `/home/srw`. Workspace volume for `/workspace`. Covers all legitimate write paths. |
| **Drop ALL capabilities** | Some debugging tools won't work (`ping` needs CAP_NET_RAW, `strace` needs CAP_SYS_PTRACE) | These are debugging tools, not agent workflow tools. If needed, use a VM or temporarily add capabilities for debugging. |
| **NetworkPolicy restricts egress** | Agent can't reach arbitrary endpoints | All external traffic goes through VPN sidecars (already the design). Direct egress was never intentional. |
| **PID limits** | Could theoretically hit the limit during heavy parallel processing | Set to 256 (generous for an agent). Monitor and adjust if needed. |

## Security Posture After Implementation

| Control | Before | After |
|---------|--------|-------|
| Capabilities | Docker default (14 caps) | DROP ALL |
| Seccomp | Docker default | RuntimeDefault (K8s managed) |
| Privilege escalation | Passwordless sudo | `allowPrivilegeEscalation: false`, no sudo binary |
| Root filesystem | Writable | Read-only |
| Writable paths | Everything | `/workspace`, `/tmp`, `/run`, `/home/srw` only |
| PID limits | None | 256 (compose) / cluster LimitRange (K8s) |
| no-new-privileges | Not set | Enabled |
| Network egress | Unrestricted | Namespace-scoped + VPN sidecar only |
| IMDS access | Open | Blocked via NetworkPolicy |
| Shell sudo | Allowed (commented out in blocklist) | Intercepted → freeze + VM upgrade prompt (binary also not installed) |
| `/proc/self/environ` | All secrets readable | Unchanged (future: file-based secrets) |

## Container → VM Upgrade Flow (Detail)

The upgrade flow reuses existing infrastructure. No new systems are needed -- it's a composition of the freeze mechanism, VM provisioner, workspace seeding, and resume flow.

```
Agent attempts sudo command
        │
        ▼
ShellManager._check_blocked() returns SUDO_FREEZE_REQUESTED
        │
        ▼
Tool wrapper calls context.request_freeze({
    freeze_type: "vm_upgrade_required",
    command: "sudo apt-get install ...",
})
        │
        ▼
Graph audited_tools node writes output/job_frozen.json
  sets should_stop=True → agent checkpoints and stops
        │
        ▼
Orchestrator detects frozen job (status: pending_review)
  SSE broadcasts to cockpit
        │
        ▼
Cockpit shows freeze card with:
  [Upgrade to VM]  [Resume without VM]  [Cancel]
        │
        ├─── "Upgrade to VM" ───────────────────────────┐
        │                                                │
        │    POST /api/jobs/{id}/upgrade-to-vm           │
        │        │                                       │
        │        ▼                                       │
        │    1. vm_provisioner.create_vm(job_id)         │
        │    2. Wait for VM ready (IP assigned)          │
        │    3. Seed workspace: rsync container PVC      │
        │       → VM via SSH (same as VM dispatch)       │
        │    4. Update job context with vm details       │
        │    5. Remove job_frozen.json                   │
        │    6. Resume job → agent picks up from         │
        │       checkpoint with RemoteBackend active     │
        │                                                │
        │    Agent resumes. sudo now goes through        │
        │    the VM's sudo approval gate (C plugin +     │
        │    Go daemon + NATS). Full VM lifecycle.       │
        │                                                │
        ├─── "Resume without VM" ───────────────────────┐
        │                                                │
        │    POST /api/jobs/{id}/approve                 │
        │    (standard phase_boundary-style approval)    │
        │        │                                       │
        │        ▼                                       │
        │    Remove job_frozen.json, status→processing   │
        │    Agent resumes in container. The tool result  │
        │    it already received says sudo isn't          │
        │    available -- the agent will try a different  │
        │    approach (pip install, compile from source   │
        │    in userspace, etc.)                          │
        │                                                │
        └─── "Cancel" ──────────────────────────────────┐
             Standard cancel flow                        │
```

### Workspace seeding during upgrade

The container workspace lives on the shared PVC at `/workspace/job_{uuid}/`. When upgrading to a VM:

1. The VM is provisioned (same as normal VM dispatch)
2. Once the VM's SSH is reachable, the orchestrator (or the agent on resume) seeds the VM workspace from the PVC
3. The existing `RemoteBackend` seeding logic (`src/core/backends/remote.py`) handles this -- it rsyncs the workspace directory to the VM
4. The checkpoint file (`workspace/checkpoints/job_{id}.db`) stays on the PVC -- the agent reads it locally on resume, but delegates file/shell operations to the VM via `RemoteBackend`

This is the same split the VM backend already implements: the agent's brain (checkpoint, DB connections, LLM context) stays on the main cluster, the hands (shell, workspace files) move to the VM.

### What the agent experiences

The agent's perspective across the upgrade:

1. **Before freeze:** Agent calls `run_command("sudo apt-get install -y libxml2-dev")`
2. **Tool response:** "This command requires elevated privileges. The job has been paused while the operator decides whether to upgrade to a VM. You do not need to take any action."
3. **Freeze:** Graph sets `should_stop=True`, agent checkpoints cleanly
4. *(operator clicks "Upgrade to VM")*
5. **Resume:** Agent resumes from checkpoint. Its next action is whatever it would have done after receiving the tool response. It can retry the sudo command -- this time it goes through the VM's shell via `RemoteBackend` and hits the sudo approval gate instead of the container intercept.

The agent code has **zero awareness** of the upgrade. The `RemoteBackend` and `LocalBackend` implement the same `WorkspaceBackend` ABC. The shell manager delegates to whichever backend is active. The switch is invisible.

### Not addressed in this document (future work)

- **File-based secrets** instead of environment variables (requires entrypoint wrapper + code changes)
- **Custom seccomp profile** (requires syscall profiling of a real agent workload)
- **gVisor runtime** (requires cluster-level configuration, significant performance impact)
- **User namespace remapping** (requires Docker daemon config changes)
- **Egress proxy with domain allowlist** (requires Squid/Envoy sidecar infrastructure)

These are documented in `docs/security_checklist.md` and can be pursued incrementally after the baseline hardening ships.

## Files Modified

| File | Change | Phase |
|------|--------|-------|
| `docker/Dockerfile.agent` | Remove sudo, add user-writable package dirs | 1 |
| `config/defaults.yaml` | Add `sudo_action: freeze` under `shell:` | 2 |
| `src/tools/coding/shell_manager.py` | Sudo intercept in `_check_blocked()`, new `sudo_action` param | 2 |
| `src/tools/coding/shell_tools.py` | Detect sentinel, call `context.request_freeze()` | 2 |
| `src/agent.py` | Pass `sudo_action` from config to ShellManager | 2 |
| `deployment/21-agent.yaml` | Add securityContext, emptyDir volumes | 3 |
| `docker-compose.yaml` | Add cap_drop, security_opt, read_only, tmpfs | 4 |
| `docker-compose.local.yaml` | Same as above | 4 |
| `deployment/21c-agent-network-policy.yaml` | **New** -- egress restrictions | 5 |
| `orchestrator/main.py` | `vm_upgrade_required` freeze handler + upgrade endpoint | 6 |
| `cockpit/src/app/.../job-detail` | VM upgrade prompt UI for frozen jobs | 7 |

## Related

- [Security Checklist](../security_checklist.md) -- Full hardening reference (this document implements the high-priority items)
- [Pod Runtime](./pod_runtime.md) -- Dynamic agent pods (should inherit hardened securityContext)
- [VM Backend](./vm_backend.md) -- Remote workspace over SSH (the heavy-isolation option)
- [Sudo Permission Profiles](./sudo_permissions.md) -- Advanced sudo gate features (VM-only)
- [Sudo Approval Gate](../done/sudo_approval_gate.md) -- Core sudo interception (VM-only)
