---
tags:
  - security
  - agent-architecture
  - orchestrator
  - infrastructure
  - coding-tools
aliases:
  - sudo gate
  - approval daemon
  - privilege escalation control
  - sudo-gated
related:
  - "[[sudo_approval_plugin]]"
  - "[[security_checklist]]"
  - "[[universal_shell_command]]"
  - "[[vm_backend]]"
  - "[[nats]]"
  - "[[coding_agent]]"
  - "[[cockpit_ds]]"
---

# Sudo Approval Gate — Implementation Roadmap

Human-in-the-loop privilege escalation for autonomous LLM agents running inside KubeVirt VMs.

## Problem

Agents running inside VMs currently have unrestricted `sudo` access via `NOPASSWD` in `/etc/sudoers`. This is a binary all-or-nothing model: either the agent can escalate to root, or it can't. Neither extreme works well for autonomous LLM agents:

- **With unrestricted sudo**: A prompt injection, hallucination, or reasoning error can execute destructive privileged commands (`rm -rf /`, `chmod 777 /etc/shadow`, install malware). The agent operates as de-facto root.
- **Without sudo**: The agent can't install packages, modify system configuration, or perform legitimate privileged operations that many coding and DevOps tasks require. This severely limits capability.

The `run_command` tool's blocklist (`src/tools/coding/shell_manager.py:50`) originally blocked `sudo` entirely, but this was already relaxed — `sudo` and `systemctl` are commented out of `DEFAULT_BLOCKED_COMMANDS` because agents need them for service management in VM deployments. The security checklist (`docs/security_checklist.md`) recommends `no-new-privileges` and dropping all capabilities, but these prevent package installation at runtime.

**We need a third option**: the agent *can* escalate, but only with explicit human consent for each operation.

## Solution

A custom sudo approval plugin that intercepts every `sudo` invocation, suspends execution, and forwards the request to the orchestrator for human approval via the cockpit UI. The architecture uses sudo's purpose-built approval plugin API (type=4, introduced in sudo 1.9.0) — exactly designed for this use case.

### Data flow

```
Agent (agent-host user)
  │
  │  sudo apt-get install nodejs
  ▼
sudo binary (SUID root)
  │  1. Policy plugin (sudoers) → ALLOW (NOPASSWD)
  │  2. Approval plugin → sudo_gate.so
  ▼
sudo_gate.so (C, runs as root)
  │  Unix domain socket: /run/sudo-gated/sudo-gated.sock
  │  Sends: {command, user, cwd, argv, runas_user}
  │  Blocks on poll() with 305s timeout
  ▼
sudo-gated (Go daemon, systemd service)
  │  NATS request/reply: sudo.request.{vm_id}.{job_id}
  │  nc.Request() with 300s timeout
  ▼
NATS (leaf node → hub)
  │
  ▼
Orchestrator (FastAPI)
  │  Stores in PostgreSQL (sudo_approval_requests table)
  │  Evaluates auto-approval rules
  │  Pushes to cockpit via SSE
  ▼
Cockpit UI (Angular)
  │  Human operator sees command, context, risk badge
  │  Clicks Approve or Deny
  ▼
Orchestrator → NATS reply (_INBOX) → sudo-gated → Unix socket → sudo_gate.so
  │
  │  check() returns 1 (approve) or 0 (deny)
  ▼
sudo: executes or rejects the command
```

### Wire protocol (plugin ↔ daemon)

The Unix socket uses a length-prefixed JSON framing protocol:

```
┌──────────────┬──────────────────────────────────┐
│ 4 bytes      │ N bytes                          │
│ uint32 BE    │ JSON payload (UTF-8)             │
│ (length = N) │                                  │
└──────────────┴──────────────────────────────────┘
```

**Request** (plugin → daemon):
```json
{
  "command": "/usr/bin/apt-get",
  "runas_user": "root",
  "user": "agent-host",
  "host": "agent-vm-abc123",
  "tty": "",
  "cwd": "/home/agent-host/workspace/job_abc123",
  "argv": ["apt-get", "install", "-y", "nodejs"]
}
```

**Response** (daemon → plugin):
```json
{"approved": true}
```
or
```json
{"approved": false, "reason": "Denied by operator: destructive command"}
```

The plugin sends the length prefix + JSON, then calls `poll()` with a 305s timeout waiting for the response. The daemon reads the length prefix, parses the JSON, forwards to NATS, waits for the reply, and writes the length-prefixed response back.

### NATS reply mechanism (daemon ↔ orchestrator)

The daemon uses NATS core request/reply — **not** pub/sub or JetStream:

1. Daemon calls `nc.Request("sudo.request.{vm_id}.{job_id}", payload, 300s)` — this **blocks the goroutine**
2. NATS internally creates a unique ephemeral `_INBOX.xxx` subject and sets it as the reply-to header
3. The orchestrator, subscribed to `sudo.request.>`, receives the message with `msg.Reply` containing the `_INBOX` subject
4. The orchestrator stores `msg.Reply` in the `nats_reply_subject` column of `sudo_approval_requests`
5. When the human approves/denies (or auto-approval fires, or expiry triggers), the orchestrator publishes the decision to the stored `_INBOX` subject via `nc.Publish(reply_subject, response)`
6. The daemon's `nc.Request()` call receives the reply, unblocks, and writes the response to the Unix socket

If the daemon's 300s timeout fires before the orchestrator replies, `nc.Request()` returns `nats.ErrTimeout`, the daemon writes a denial to the socket, and any later reply from the orchestrator to the `_INBOX` is silently dropped (the subscription is already gone). This is the correct behavior — a stale approval must never execute.

### Agent-side behavior

From the LLM agent's perspective, a gated `sudo` command looks like a slow shell command:

1. Agent calls `run_command(command="sudo apt-get install nodejs", timeout=600)`
2. The `run_command` tool dispatches to `ShellManager.run_sync()`, which runs the command in a tmux tab
3. sudo loads the plugin, which blocks waiting for approval (up to 305s)
4. The agent's `run_command` call blocks for the duration

**Timeout consideration**: The default `run_command` timeout is 120s, but sudo approval can take up to 305s. Agents using the sudo gate should set `timeout=600` (the max) on sudo-prefixed commands. The agent's system prompt or instructions should note that `sudo` commands may take several minutes while awaiting human approval, and that a denial will appear as a non-zero exit code with "sudo request denied by operator" in stderr.

**On denial**: The agent sees exit code 1 and stderr output like:
```
sudo: sudo request denied by operator
```
The agent should note this in `workspace.md` and either try an alternative approach or flag the need to the operator via its regular todo/phase workflow.

### Why this architecture holds

1. **The plugin runs as root** — loaded by sudo's SUID binary before any uid/gid transition. The agent process (`agent-host` user) cannot modify, replace, or bypass it.
2. **The daemon runs as a separate system user** — with aggressive systemd hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `CapabilityBoundingSet=`). The agent cannot kill, inspect, or manipulate it.
3. **The decision comes from outside** — the orchestrator and cockpit live on a different cluster entirely. The agent has no path to influence the approval decision.
4. **Fail-closed by default** — any failure at any layer (plugin crash, daemon down, NATS unreachable, orchestrator offline, human timeout) results in command denial.

## Related Documents

| Document | Location | Relevance |
|----------|----------|-----------|
| Concept design | `docs/sudo_approval_plugin.md` | Original architecture proposal, component descriptions, security analysis |
| Research: Technical blueprint | `docs/researches/sudo_plugin/Human-in-the-Loop Sudo Approval Gate for Autonomous LLM Agents.pdf` | Sudo plugin API deep dive, C skeleton code, Go daemon architecture, NATS request/reply patterns, build order, Ubuntu 24.04 specifics |
| Research: Academic analysis | `docs/researches/sudo_plugin/LLM Agent Sudo Approval Research.pdf` | Formal analysis of plugin lifecycle, return codes, cross-component timeout coordination, attack surface modeling, Angular signals integration |
| Container security checklist | `docs/security_checklist.md` | Broader container hardening context; sudo gate addresses the privilege escalation gap |
| Shell command access | `docs/universal_shell_command.md` | Current `run_command` security model (blocklist, sandbox); sudo gate replaces the binary blocklist approach |
| VM backend design | `docs/features/vm_backend.md` | Workspace backend abstraction; sudo gate lives inside the VM alongside the workspace |
| NATS messaging layer | `docs/features/nats.md` | NATS topology (hub + leaf), subject naming, existing subjects; sudo gate adds new subjects |
| Container security research | `docs/researches/Secure LLM Agent Containers.pdf` | Full research paper behind the security checklist |
| Coding agent design | `docs/coding_agent.md` | Agent toolset and `run_command` design; sudo gate enables unrestricted shell with gated escalation |

## Key Technical Decisions

Informed by both research reports:

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Plugin type | Approval plugin (type=4) | Purpose-built for gating. Runs after policy plugin, blocks synchronously, multiple can coexist. Only correct choice. |
| Plugin language | C | Required by sudo's plugin ABI. Header-only API (`sudo_plugin.h`), function pointers provided at runtime. Vendor cJSON for JSON serialization. |
| Plugin timeout mechanism | `poll()` with 305s timeout | Cleaner than `SO_RCVTIMEO` — explicit distinction between timeout (0), error (-1), and data ready (>0). |
| Daemon language | Go | NATS ecosystem is Go-native (`nats.go` is the reference client). Goroutine-per-connection maps to the blocking approval model. Static binary via `CGO_ENABLED=0`. 3-5x faster development than Rust for this scope. |
| Daemon communication | NATS core request/reply | `nc.Request(subject, data, timeout)` provides synchronous RPC over async messaging. `ErrNoResponders` fires immediately if orchestrator is offline. No JetStream — stale approvals must not execute. |
| Real-time UI push | SSE (Server-Sent Events) | Unidirectional push (server → client). Approve/deny is a standard HTTP POST. Extends existing SSE infrastructure (builder chat). Native `EventSource` auto-reconnection + `Last-Event-ID` resume. |
| Timeout hierarchy | Orchestrator 295s < NATS 300s < Plugin 305s | Each lower layer has a slightly longer timeout to prevent race conditions where an approval arrives after the caller has already timed out. |
| Socket activation | systemd `sudo-gated.socket` unit | Socket created by systemd with correct permissions (`SocketMode=0660`, `SocketUser=root`, `SocketGroup=sudo-gated`). Zero-downtime daemon restarts — connections queue in kernel backlog. |
| Auto-approval engine | `fnmatch` pattern matching | No regex — no ReDoS risk. Rules stored in PostgreSQL, evaluated highest-priority-first. Commands with pipes/redirects/chaining are never auto-approved. |

## Development and Build Environment

### Local development

The C plugin can only be tested on a Linux system with sudo 1.9.0+ (Ubuntu 24.04 VM recommended). The Go daemon can be developed on any platform but tested on Linux.

**Minimum dev setup**:
- A KubeVirt VM (existing agent VM base image) or a local Ubuntu 24.04 VM for plugin testing
- NATS running locally (`podman-compose -f docker-compose.dev.yaml up -d nats`) for daemon testing
- The mock scripts (`mock_plugin.py`, `mock_orchestrator.py`) allow testing each component in isolation

**Cross-compilation**:
- Go daemon: `GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -o sudo-gated ./cmd/sudo-gated/`
- C plugin: must be compiled on the target architecture (or cross-compiled with `gcc-aarch64-linux-gnu` etc.). Simplest approach: compile inside the Packer build or on the target VM.

### CI/build pipeline

Compiled binaries (the Go daemon and C plugin `.so`) should **not** be checked into the repository. Instead:

1. `sudo-gated/Makefile` and `sudo-gate-plugin/Makefile` produce the binaries
2. The Packer build for the VM base image compiles both from source during provisioning (or downloads pre-built artifacts from a CI release)
3. For iterative development, `scp` the binaries to a running VM for testing

The existing CI pipeline (GitHub Actions) can be extended with build steps for Go and C, producing artifacts that the Packer build consumes.

## Implementation Phases

### Phase 1: Go Daemon with Mock Plugin (days 1-3)

**Goal**: Build and test the daemon in isolation before touching the C plugin or orchestrator.

**Deliverables**:
- `sudo-gated/` — Go module with Unix socket server, NATS client, rate limiter
- `sudo-gated/cmd/sudo-gated/main.go` — Entry point with systemd integration (`sd_notify`)
- `sudo-gated/internal/gate/` — Socket server, request handling, NATS forwarding
- `sudo-gated/test/mock_plugin.py` — Python script that connects to the Unix socket and sends JSON payloads mimicking the C plugin
- `sudo-gated/test/mock_orchestrator.py` — Python NATS subscriber that auto-approves/denies for testing

**Test criteria**:
- Mock plugin sends request → daemon forwards via NATS → mock orchestrator replies → daemon writes response to socket → mock plugin reads approval
- Kill NATS → daemon returns denial to socket
- Flood socket connections → rate limiter kicks in (5 req/min, burst of 3)
- Daemon restart while request pending → plugin receives error (socket closed)
- `go build` produces static binary (`CGO_ENABLED=0`)

**Key implementation details**:
- `net.Listen("unix", "/run/sudo-gated/sudo-gated.sock")` for socket server
- `SO_PEERCRED` on accepted connections to verify connecting PID
- `/proc/{pid}/exe` readlink to confirm caller is `/usr/bin/sudo`
- `nats.Connect(url)` with `MaxReconnects(-1)` and `ReconnectWait(2s)`
- `nc.Request(fmt.Sprintf("sudo.request.%s.%s", vmID, jobID), payload, 300*time.Second)`
- `golang.org/x/time/rate.NewLimiter(rate.Every(12*time.Second), 3)` — 5/min with burst 3
- `signal.NotifyContext` for SIGTERM → context cancellation
- `sync.WaitGroup` tracks in-flight goroutines, 30s grace period on shutdown

### Phase 2: C Plugin — Stub and Lifecycle Validation (days 3-4)

**Goal**: Validate that the plugin loads correctly, sudo still works, and the struct/lifecycle is correct. Build incrementally from a no-op stub to a full socket-connected plugin.

**Prerequisites**:
- Obtain `sudo_plugin.h`: `apt-get source sudo` on Ubuntu 24.04 provides `sudo-1.9.15p5/include/sudo_plugin.h`
- Vendor cJSON: download `cJSON.c` and `cJSON.h` from the DaveGamble/cJSON repository (single-file library, no runtime dependencies)

**Deliverables**:
- `sudo-gate-plugin/sudo_gate.c` — Approval plugin implementation (built incrementally, see below)
- `sudo-gate-plugin/include/sudo_plugin.h` — Vendored from sudo source (avoids build-time dependency on `apt-get source`)
- `sudo-gate-plugin/cJSON.c`, `sudo-gate-plugin/cJSON.h` — Vendored single-file JSON library
- `sudo-gate-plugin/Makefile` — Build with GCC 14, `-fPIC -shared -Wall -Wextra -O2 -I./include`
- `sudo-gate-plugin/sudo.conf.d/sudo_gate.conf` — Plugin registration for `/etc/sudo.conf`

**Incremental build sequence**:
1. **Stub** — `check()` logs to `syslog(LOG_AUTH)` and returns 1 (always approve). Validates struct layout, symbol export, and plugin loading.
2. **Register** — add `Plugin sudo_gate_approval sudo_gate.so` to `/etc/sudo.conf`. Run `sudo ls` and confirm syslog shows the plugin's log line.
3. **Socket client** — add Unix socket connection code to `check()`. Connect to daemon from Phase 1, send JSON request (length-prefixed), receive response.
4. **poll() timeout** — replace blocking `recv()` with `poll()` + 305s timeout. Test with short timeouts (5s) to verify timeout→deny behavior.
5. **Integration** — wire to the running daemon from Phase 1. Full approval flow end-to-end.

**Test criteria (on a VM — always keep a root shell open)**:
- Step 1: `sudo ls` → plugin logs to syslog, command executes (stub mode)
- Step 3: `sudo ls` → plugin connects to daemon, daemon approves, command executes
- Step 4: Kill daemon → plugin returns 0 (deny) or -1 (error), `sudo` rejects command
- Step 4: Set timeout to 5s, don't respond → plugin times out, command denied
- Plugin binary owned by root:root mode 0644, installed in `/usr/lib/sudo/`
- `/etc/sudo.conf` owned by root:root mode 0644

**CRITICAL**: A broken plugin `.so` prevents ALL sudo usage on the VM — there is no fallback. Always:
- Test with an existing root shell open in another terminal
- Keep `virtctl console` access as a recovery path
- Start with `fail_mode=open` during development (plugin returns 1 on errors), switch to `fail_mode=deny` for production

### Phase 3: End-to-End Local Flow (days 5-6)

**Goal**: Plugin and daemon working together on a VM with real sudo invocations, NATS forwarding to a mock orchestrator.

**Deliverables**:
- systemd unit files: `sudo-gated.service`, `sudo-gated.socket`
- Integration test script that exercises the full local flow
- Packer provisioning additions for the VM base image

**systemd units**:
```ini
# sudo-gated.socket
[Socket]
ListenStream=/run/sudo-gated/sudo-gated.sock
SocketMode=0660
SocketUser=root
SocketGroup=sudo-gated
DirectoryMode=0755

[Install]
WantedBy=sockets.target
```

```ini
# sudo-gated.service
[Unit]
Description=Sudo Approval Gate Daemon
Requires=sudo-gated.socket
After=network-online.target

[Service]
Type=notify
ExecStart=/usr/local/bin/sudo-gated --config /etc/sudo-gate/config.yaml
Restart=always
RestartSec=5
WatchdogSec=30
EnvironmentFile=/etc/default/sudo-gated

# Hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
MemoryDenyWriteExecute=yes
SystemCallFilter=@system-service
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/run/sudo-gated

[Install]
WantedBy=multi-user.target
```

**Test criteria**:
- SSH into VM as `agent-host`, run `sudo apt-get install -y curl` → blocks
- Mock orchestrator approves → command executes
- Mock orchestrator denies → command rejected
- Multiple concurrent `sudo` invocations → each handled independently
- `systemctl restart sudo-gated` while request pending → plugin receives error, command denied
- Daemon restart → socket still accepting (systemd socket activation, kernel backlog)

### Phase 4: Orchestrator Integration (days 7-10)

**Goal**: FastAPI endpoints, PostgreSQL schema, NATS subscription, and approval/denial flow.

**Deliverables**:
- `orchestrator/database/queries/postgres/sudo_schema.sql` — Table + indexes + auto-approval rules table
- `orchestrator/services/sudo_gate.py` — NATS subscription handler, auto-approval engine, expiration sweeper
- REST endpoints added to `orchestrator/main.py` (the orchestrator uses a single-file endpoint pattern — no `routers/` directory; see existing `/api/vms` endpoints for the convention)
- SSE endpoint for real-time push to cockpit
- Migration applied to existing PostgreSQL instance

**Important orchestrator pattern**: The NATS subscription handler receives the message, stores the request, evaluates auto-approval rules, and — if no auto-match — **does not block**. The `nats_reply_subject` (from `msg.reply`) is persisted in PostgreSQL. The actual reply happens later, asynchronously, when:
- A human clicks approve/deny → the endpoint reads `nats_reply_subject` from the row and publishes the decision
- The expiration sweeper fires → it publishes denials to all expired rows' `nats_reply_subject` values
- Auto-approval matches → the handler replies immediately before returning

This means the daemon's `nc.Request()` blocks for up to 300s, while the orchestrator's NATS handler returns immediately. The two are decoupled by the persisted reply subject.

**Database schema**:
```sql
CREATE TYPE sudo_request_status AS ENUM (
    'pending', 'approved', 'denied', 'expired', 'auto_approved', 'auto_denied'
);

CREATE TABLE sudo_approval_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    vm_name         VARCHAR(255) NOT NULL,
    command         TEXT NOT NULL,
    arguments       TEXT[] DEFAULT '{}',
    working_directory TEXT,
    requesting_user VARCHAR(255) NOT NULL,
    target_user     VARCHAR(255) NOT NULL DEFAULT 'root',
    status          sudo_request_status NOT NULL DEFAULT 'pending',
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at      TIMESTAMPTZ,
    decided_by      VARCHAR(255),
    decision_reason TEXT,
    ttl_seconds     INTEGER NOT NULL DEFAULT 300,
    expires_at      TIMESTAMPTZ GENERATED ALWAYS AS
        (requested_at + (ttl_seconds || ' seconds')::INTERVAL) STORED,
    -- Stored so the expiration sweeper can send denials after the originating
    -- NATS subscription handler has already returned.
    nats_reply_subject TEXT,
    metadata        JSONB DEFAULT '{}'
);

-- Hot path: UI polling and SSE push for pending requests
CREATE INDEX idx_sudo_pending ON sudo_approval_requests (status, requested_at DESC)
    WHERE status = 'pending';
-- Job-scoped views in cockpit job detail panel
CREATE INDEX idx_sudo_job ON sudo_approval_requests (job_id, requested_at DESC);
-- Expiration sweeper: find pending requests past their TTL
CREATE INDEX idx_sudo_expiry ON sudo_approval_requests (expires_at)
    WHERE status = 'pending';
```

Note: The `status` enum distinguishes human decisions (`approved`/`denied`) from automated ones (`auto_approved`/`auto_denied`/`expired`), making the separate `auto_approved` boolean from the research reports unnecessary.

**NATS subjects** (additions to existing hierarchy in `docs/features/nats.md`):

| Subject | Direction | Purpose |
|---------|-----------|---------|
| `sudo.request.{vm_id}.{job_id}` | Daemon → Orchestrator | Request/reply for sudo approval |
| (reply via `_INBOX`) | Orchestrator → Daemon | Approval/denial decision |

**REST endpoints**:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/sudo/events` | SSE stream of pending requests |
| `GET` | `/api/sudo/requests` | List requests (filterable by job, status) |
| `GET` | `/api/sudo/requests/{id}` | Get single request |
| `POST` | `/api/sudo/requests/{id}/approve` | Approve with optional reason |
| `POST` | `/api/sudo/requests/{id}/deny` | Deny with required reason |
| `GET` | `/api/sudo/rules` | List auto-approval rules |
| `POST` | `/api/sudo/rules` | Create auto-approval rule |
| `DELETE` | `/api/sudo/rules/{id}` | Delete auto-approval rule |

**Auto-approval rules table**:
```sql
CREATE TABLE sudo_auto_rules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pattern     TEXT NOT NULL,          -- fnmatch pattern (e.g., "apt-get install *")
    action      VARCHAR(20) NOT NULL,   -- 'approve', 'deny', 'review' (force human)
    priority    INTEGER NOT NULL DEFAULT 100,  -- lower = higher priority
    description TEXT,
    created_by  VARCHAR(255),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enabled     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX idx_sudo_rules_active ON sudo_auto_rules (priority ASC) WHERE enabled = TRUE;
```

**Auto-approval flow**:
1. NATS message arrives on `sudo.request.>` subscription
2. Orchestrator stores request in PostgreSQL with status `pending`, saves `msg.reply` as `nats_reply_subject`
3. **Shell metacharacter check**: if the command contains `|`, `>`, `>>`, `;`, `&&`, `||`, or backticks — skip auto-approval entirely, always require human review
4. Evaluate against auto-approval rules (fnmatch, priority-ordered, enabled only):
   - Match "approve" → set status `auto_approved`, publish approval to `nats_reply_subject` immediately
   - Match "deny" → set status `auto_denied`, publish denial immediately
   - Match "review" or no match → push to SSE, wait for human
5. Background task (asyncio, runs every 15s in FastAPI lifespan) sweeps expired requests: sets status `expired`, publishes denial to `nats_reply_subject` for each

**Test criteria**:
- NATS message from mock daemon → request appears in PostgreSQL
- `curl POST /approve` → NATS reply sent, daemon receives approval
- Request expires → denial sent automatically
- Auto-approval rule matches `apt-get install *` → instant approval, no UI involvement
- SSE stream pushes new request within 1s of NATS arrival

### Phase 5: Cockpit UI (days 10-13)

**Goal**: Angular approval interface with real-time SSE updates.

**Deliverables**:
- `cockpit/src/app/services/sudo-approval.service.ts` — SSE client, `WritableSignal<ApprovalRequest[]>`
- `cockpit/src/app/components/sudo-approval/` — Approval card component, list view, rule management
- Integration with existing cockpit navigation and job detail views

**Approval card displays**:
- Full command string (syntax-highlighted, monospace)
- Risk badge: green (read-only), yellow (package installs), red (permission changes), critical (shell spawns, destructive ops)
- Context: requesting user → target user, VM name, job link, working directory
- Countdown timer showing time remaining before auto-deny
- One-click approve/deny buttons (reason required for deny)

**Angular patterns**:
- `EventSource` pointing to `/api/sudo/events`
- `WritableSignal<ApprovalRequest[]>` for reactive state
- `ApplicationRef.tick()` after signal updates (EventSource fires outside zone)
- `computed()` signals for derived state (pending count, high-risk flag)
- Approve/deny via standard `HttpClient.post()`

**Test criteria**:
- New sudo request → toast notification + card appears in <1s
- Countdown timer decrements in real time
- Click approve → command executes on VM, card moves to history
- Click deny → command rejected on VM, card shows denial reason
- Page reload → pending requests restored from REST API (not only SSE)

### Phase 6: VM Image Integration and Hardening (days 13-16)

**Goal**: Bake everything into the Packer VM base image, apply security hardening.

**Deliverables**:
- Updated `docker/agent-vm-base/scripts/provision.sh` — installs plugin + daemon
- `docker/agent-vm-base/files/sudo_gate.so` — Pre-compiled plugin binary
- `docker/agent-vm-base/files/sudo-gated` — Pre-compiled Go daemon binary
- `docker/agent-vm-base/files/sudo-gated.service` + `.socket` — systemd units
- `docker/agent-vm-base/files/sudo-gate.conf` — `/etc/sudo.conf` plugin registration
- Updated cloud-init in VM template to pass NATS URL and job ID to daemon

**Provisioning additions** (to `provision.sh`):
```bash
# Sudo approval gate
sudo install -o root -g root -m 0644 /tmp/sudo_gate.so /usr/lib/sudo/
sudo install -o root -g root -m 0755 /tmp/sudo-gated /usr/local/bin/
sudo install -o root -g root -m 0644 /tmp/sudo-gated.service /etc/systemd/system/
sudo install -o root -g root -m 0644 /tmp/sudo-gated.socket /etc/systemd/system/

# Create daemon user and group
sudo groupadd -r sudo-gated
sudo useradd -r -g sudo-gated -s /usr/sbin/nologin sudo-gated

# Register plugin in sudo.conf
sudo cp /tmp/sudo-gate.conf /etc/sudo.conf.d/ || \
    echo 'Plugin sudo_gate_approval sudo_gate.so socket_path=/run/sudo-gated/sudo-gated.sock timeout=305 fail_mode=deny' \
    | sudo tee -a /etc/sudo.conf

# Immutable attributes (defense-in-depth)
sudo chattr +i /usr/lib/sudo/sudo_gate.so
sudo chattr +i /etc/sudo.conf
sudo chattr +i /etc/sudoers

# Enable socket activation (daemon starts on first sudo)
sudo systemctl enable sudo-gated.socket
```

**Cloud-init additions** (to VM template):
```yaml
- path: /etc/default/sudo-gated
  permissions: '0644'
  content: |
    NATS_URL=${NATS_URL}
    JOB_ID=${JOB_ID}
    VM_ID=agent-vm-${JOB_ID}
```

**Security hardening checklist**:
- [ ] Plugin `.so` owned by root:root, mode 0644, `chattr +i`
- [ ] `/etc/sudo.conf` owned by root:root, mode 0644, `chattr +i`
- [ ] `/etc/sudoers` and `/etc/sudoers.d/` owned by root:root, `chattr +i`
- [ ] Daemon binary owned by root:root, mode 0755
- [ ] Socket at `/run/sudo-gated/sudo-gated.sock` mode 0660, owner root:sudo-gated
- [ ] Daemon runs as `sudo-gated` user with empty `CapabilityBoundingSet`
- [ ] `SO_PEERCRED` verification: daemon checks connecting PID is `/usr/bin/sudo`
- [ ] NATS auth: NKey or JWT credentials scoped to `sudo.request.{vm_id}.>` subjects only
- [ ] Commands with `|`, `>`, `;`, `&&`, `||` never auto-approved
- [ ] `virtctl console` tested as recovery path for lockout scenarios

**Test criteria (end-to-end on real cluster)**:
- Build VM image with Packer → image includes plugin + daemon
- Provision VM via orchestrator → cloud-init configures daemon env
- Agent runs job, executes `sudo apt-get install nodejs` → request appears in cockpit
- Operator approves → command executes, agent continues
- Operator denies → agent sees "permission denied", adapts
- Kill NATS → all sudo commands auto-denied
- Attempt to modify plugin from `agent-host` user → permission denied (`chattr +i`)

### Phase 7: MCP Tools, Agent Integration, and Polish (days 16-17)

**Goal**: Enable approval directly from Claude Code, integrate with the agent's instruction system, and add observability.

**Deliverables**:
- MCP tools in `orchestrator/mcp/`: `approve_sudo_request`, `deny_sudo_request`, `list_sudo_requests`
- MongoDB audit integration (sudo requests logged alongside LLM tool calls in the audit trail)
- Agent instruction update: add a note to `config/templates/instructions.md` (or the relevant expert's instructions) explaining that `sudo` commands require human approval, may take several minutes, and that denials should be handled gracefully
- `run_command` docstring update: note that sudo-prefixed commands may block for up to 5 minutes awaiting approval, recommend `timeout=600`
- Documentation: update `docs/features/nats.md` with the new `sudo.request.*` subjects, update `CLAUDE.md` with the new orchestrator endpoints and service ports (if any)

**Test criteria**:
- From Claude Code MCP: `list_sudo_requests` shows pending requests, `approve_sudo_request` triggers approval on VM
- MongoDB audit trail includes sudo request/decision events
- Agent handles denial gracefully (logs to workspace.md, continues with alternative approach)

## File Layout

```
sudo-gated/                          # Go daemon (new directory at repo root)
├── cmd/sudo-gated/main.go
├── internal/
│   ├── gate/server.go               # Unix socket server
│   ├── gate/handler.go              # Request processing, NATS forwarding
│   ├── gate/ratelimit.go            # Token bucket
│   ├── config/config.go             # YAML config loader
│   └── peer/verify.go               # SO_PEERCRED + /proc verification
├── go.mod
├── go.sum
├── Makefile
└── test/
    ├── mock_plugin.py               # Simulates the C plugin over Unix socket
    └── mock_orchestrator.py          # Simulates orchestrator via NATS subscription

sudo-gate-plugin/                    # C plugin (new directory at repo root)
├── sudo_gate.c                      # Approval plugin implementation
├── include/
│   └── sudo_plugin.h                # Vendored from sudo 1.9.15p5 source
├── cJSON.c                          # Vendored single-file JSON library
├── cJSON.h
├── Makefile                         # gcc -fPIC -shared -Wall -Wextra -O2 -I./include
└── sudo.conf.d/
    └── sudo_gate.conf               # Plugin registration line for /etc/sudo.conf

orchestrator/
├── database/queries/postgres/
│   └── sudo_schema.sql              # New: approval requests + auto-approval rules
├── main.py                          # Modified: new /api/sudo/* endpoints + SSE
├── services/
│   ├── nats_bridge.py               # Modified: new sudo.request.> subscription
│   └── sudo_gate.py                 # New: auto-approval engine, expiration sweeper
└── mcp/
    └── (sudo tools added)           # Modified: approve/deny/list MCP tools

cockpit/src/app/
├── services/
│   └── sudo-approval.service.ts     # New: SSE client + WritableSignal state
└── components/
    └── sudo-approval/               # New
        ├── sudo-approval-card.component.ts
        ├── sudo-approval-list.component.ts
        └── sudo-rule-manager.component.ts

docker/agent-vm-base/
├── scripts/provision.sh             # Modified: install plugin + daemon + systemd units
└── files/
    ├── sudo-gated.service           # New: systemd service unit
    ├── sudo-gated.socket            # New: systemd socket activation unit
    ├── sudo-gate.conf               # New: /etc/sudo.conf plugin registration
    └── sudo-gated-config.yaml       # New: daemon config (NATS URL, timeouts, rate limits)
    # NOTE: compiled binaries (sudo_gate.so, sudo-gated) are built during
    # Packer provisioning or downloaded from CI artifacts — not checked in.

docs/HomeLab/deployments/srw/harvester/
└── vm-template.yaml                 # Modified: cloud-init adds /etc/default/sudo-gated
```

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| Broken plugin locks out all sudo on VM | High — no recovery without console access | Always test with root shell open. `virtctl console` as recovery. `fail_mode=open` option for development. |
| NATS outage blocks all privileged operations | Medium — agents can still run unprivileged commands | Agents should be designed to handle sudo denial gracefully (report to workspace.md, try alternative approaches) |
| Operator fatigue from too many requests | Medium — approvals become rubber-stamped | Auto-approval rules for safe patterns (`apt-get install *-dev`). Risk badges to focus attention. Rate limiting prevents flooding. |
| Agent social-engineers the operator | Low — but possible | Cockpit shows the raw command from sudo, not the agent's description. Highlight high-risk flags. |
| Ubuntu 25.10+ switches to sudo-rs (Rust) | Low — future concern | Ubuntu 24.04 LTS ships C sudo (1.9.15p5) through 2029. Monitor sudo-rs plugin compatibility. |
| Cross-component timeout race | Medium — approval recorded after plugin timeout | Cascading timeout hierarchy (295s < 300s < 305s) prevents this. Test extensively with deliberate delays. |
| `run_command` default timeout too short | Medium — agent's 120s default times out before 305s approval window | Agent instructions must note `timeout=600` for sudo commands. Consider auto-adjusting timeout in `run_command` when command starts with `sudo`. |
| Daemon `nats_reply_subject` stale after restart | Low — pending approval lost if daemon restarts | Daemon restarts close all socket connections; plugin receives error and denies. The orchestrator-side row remains `pending` and will be cleaned up by the expiration sweeper. No data inconsistency. |
