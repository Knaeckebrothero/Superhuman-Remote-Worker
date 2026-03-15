---
tags:
  - agent-architecture
  - tool-development
  - deployment
  - infrastructure
---

# Workspace Backend Abstraction

Design document for decoupling the agent's tool layer from the local filesystem, enabling workspaces to live on remote VMs while the agent pod runs on the main cluster.

**Status (March 2026):** Phases 1-4 code-complete. `WorkspaceBackend` ABC, `LocalBackend` (pathlib), `RemoteBackend` (SSH/SFTP + remote tmux), config integration, `ShellManager` delegation, NATS Helm values, VM Controller service + K8s manifest, NATS bridge + VM provisioner in orchestrator, REST endpoints, lifecycle hooks, auto-dispatch wiring (VM IP injection into config_override), remote-aware phase snapshots (extract from VM / push to VM), VM failure recovery (detect → re-provision → seed → resume). Needs deployment and integration testing against real KubeVirt infrastructure.

## Motivation

The [VM isolation design](./vm.md) establishes that each agent job gets a dedicated VM on the agent cluster. The original design (option 2) placed the agent process inside the VM alongside the workspace. This document specifies option 1: **the agent pod runs on the main cluster, the VM is a remote workspace only**.

### Why separate the agent from its workspace?

The agent executes arbitrary shell commands as part of its workflow. If it runs something destructive (`rm -rf /`, a forkbomb, a broken install script), the blast radius determines recovery:

| | Agent inside VM (option 2) | Agent on main cluster (option 1) |
|--|---------------------------|----------------------------------|
| VM crashes | Agent dies. No error report, no recovery. Dead job. | Agent detects VM failure. Logs the error, reports to orchestrator. Can be assigned a fresh VM. |
| Agent corrupts workspace | Agent may corrupt its own state files alongside workspace | Agent state (checkpoint, database connections, LLM context) is safe on the main cluster |
| Resource exhaustion | Agent's LLM orchestration competes with workspace tasks for VM resources | LLM orchestration runs on main cluster hardware, VM resources dedicated to workspace tasks |
| Monitoring | Must self-report from inside a potentially broken VM | Orchestrator monitors agent pod directly, VM health checked independently |

The core insight is **separation by function**: the main cluster is the brain (LLM orchestration, state, databases), the VM cluster is the hands (shell execution, file manipulation). The hands are expendable — the brain must survive.

### Why not just mount the VM filesystem?

NFS/SSHFS mounts are fragile across clusters. A network hiccup causes stale file handles, hung I/O calls, and kernel-level pain. An explicit backend abstraction with proper error handling is more resilient than a transparent mount that fails silently.

## Architecture

### Implemented Architecture

The workspace backend sits underneath the managers, not underneath the tools. Tools are completely unchanged.

```
Tools (unchanged — read_file, write_file, run_command, git_log, etc.)
  │
  ├── WorkspaceManager ──► WorkspaceBackend.read_file()
  │                        WorkspaceBackend.write_file()
  │                        WorkspaceBackend.list_dir()
  │                        WorkspaceBackend.exists()
  │                        ...
  │
  ├── ShellManager ──────► WorkspaceBackend.shell_run()    (when backend.supports_shell)
  │     │                  WorkspaceBackend.shell_read()
  │     │                  WorkspaceBackend.shell_send()
  │     │                  ...
  │     └── libtmux ◄──── (when local, via else branch — unchanged)
  │
  └── GitManager           subprocess.run(["git", ...], cwd=workspace_path)
                           (unchanged — uses local subprocess, not wired to backend yet)
```

Two implementations:

```
WorkspaceBackend (ABC)  ← src/core/workspace_backend.py
  │
  ├── LocalBackend        pathlib for files. No shell ops — ShellManager
  │   src/core/           uses libtmux directly for local execution.
  │   backends/local.py   Zero behavioral change from pre-refactor.
  │
  └── RemoteBackend       paramiko SFTP for files. Remote tmux over SSH
      src/core/           for shell. Same sentinel-based completion
      backends/remote.py  detection as ShellManager.run_sync().
```

### What stays local regardless

Not everything goes through the backend. The agent's own infrastructure stays on the pod:

| Component | Location | Why |
|-----------|----------|-----|
| Checkpoint DB | Agent pod (SQLite) | Resume/recovery is agent-local |
| Database connections | Agent pod → main cluster DBs | PostgreSQL, Neo4j, MongoDB are on the main cluster |
| LLM calls | Agent pod | API keys, context management, token counting |
| Orchestrator communication | Agent pod → orchestrator | Heartbeat, status, job lifecycle |
| Web search / citation tools | Agent pod | No workspace dependency |
| Todo/Plan/Memory managers | Agent pod (managers run locally, files read/written through backend) | Manager logic is local, but `todos.yaml`, `plan.md`, `workspace.md` live on the VM |

Everything that touches workspace files or executes commands goes through the backend:

| Component | Goes through backend | Why |
|-----------|---------------------|-----|
| `read_file` / `write_file` | `WorkspaceBackend.read_file()` / `.write_file()` | Files live on VM |
| `list_files` / `search_files` | `WorkspaceBackend.list_dir()` / `.glob()` | Directory listing on VM |
| `run_command` / `shell_execute` | `WorkspaceBackend.shell_run()` | Commands execute on VM |
| `shell_read` | `WorkspaceBackend.shell_read()` | Terminal output lives on VM |
| `git_log` / `git_diff` / `git_show` | `WorkspaceBackend.run_command()` | Git repo is on VM |
| Document processing (PDF, PPTX) | `WorkspaceBackend.read_file()` (binary) | Documents stored on VM |
| `workspace.md` injection | `WorkspaceBackend.read_file()` | workspace.md lives on VM |

## WorkspaceBackend Interface

Defined in `src/core/workspace_backend.py`. Key design decisions:

- **File operations are abstract** — every backend must implement them.
- **Shell operations are non-abstract** — default to `NotImplementedError`. Only `RemoteBackend` overrides them. `LocalBackend` doesn't need to because `ShellManager` handles local shell directly via libtmux.
- **`supports_shell` property** — lets `ShellManager` decide whether to delegate or use libtmux.
- **No `ShellResult`/`ShellReadResult` dataclasses** — shell methods return plain strings and tuples to match `ShellManager`'s existing return types, avoiding a conversion layer.

```python
class WorkspaceUnavailableError(Exception):
    """Raised when the workspace backend cannot be reached."""
    pass


class WorkspaceBackend(ABC):

    # --- File operations (abstract) ---

    @abstractmethod
    def read_file(self, path: str, binary: bool = False) -> str | bytes: ...
    @abstractmethod
    def write_file(self, path: str, content: str | bytes) -> None: ...
    @abstractmethod
    def append_file(self, path: str, content: str) -> None: ...
    @abstractmethod
    def exists(self, path: str) -> bool: ...
    @abstractmethod
    def is_file(self, path: str) -> bool: ...
    @abstractmethod
    def is_dir(self, path: str) -> bool: ...
    @abstractmethod
    def list_dir(self, path: str = "", pattern: str = "*") -> list[str]: ...
    @abstractmethod
    def search_files(self, query: str, path: str = "", case_sensitive: bool = False) -> list[dict]: ...
    @abstractmethod
    def mkdir(self, path: str) -> None: ...
    @abstractmethod
    def delete_file(self, path: str) -> bool: ...
    @abstractmethod
    def delete_directory(self, path: str) -> bool: ...
    @abstractmethod
    def move(self, src: str, dst: str) -> None: ...
    @abstractmethod
    def copy(self, src: str, dst: str) -> None: ...
    @abstractmethod
    def stat(self, path: str) -> int: ...
    @abstractmethod
    def resolve_path(self, relative_path: str) -> str: ...

    # --- Shell operations (non-abstract, default NotImplementedError) ---
    #
    # Override in backends that support remote shell execution.
    # For local execution, ShellManager uses libtmux directly.

    def shell_run(self, command, timeout=120, tab_name="default", working_dir=None) -> str:
        raise NotImplementedError("Shell operations not supported by this backend")
    def shell_send(self, tab_name, text, enter=True) -> str: ...
    def shell_read(self, tab_name, lines=50, since_cursor=False) -> Tuple[str, Dict]: ...
    def shell_read_with_offset(self, tab_name, lines=30, offset=None) -> Tuple[str, Dict]: ...
    def shell_ensure_tab(self, name) -> None: ...
    def shell_open_tab(self, name, command=None, tab_type=None) -> Dict: ...
    def shell_close_tab(self, name) -> str: ...
    def shell_list_tabs(self) -> List[Dict]: ...
    def shell_format_tab_header(self) -> str: ...
    def shell_cleanup(self) -> None: ...
    def shell_is_alive(self) -> bool: ...

    @property
    def supports_shell(self) -> bool:
        """Used by ShellManager to decide delegation vs local libtmux."""
        return False

    # --- Lifecycle (abstract) ---

    @abstractmethod
    def connect(self) -> None: ...
    @abstractmethod
    def disconnect(self) -> None: ...
    @abstractmethod
    def is_connected(self) -> bool: ...

    @property
    @abstractmethod
    def root(self) -> str:
        """Return the workspace root path as a string."""
        ...
```

## LocalBackend

**File:** `src/core/backends/local.py`

Implements all file operations via `pathlib`. Does **not** implement shell operations — for local execution, `ShellManager` continues to use `libtmux` directly (its `supports_shell` returns `False`).

This was a mechanical extraction of I/O code from `WorkspaceManager` into the backend, with zero behavioral change.

```python
class LocalBackend(WorkspaceBackend):
    """Workspace backed by the local filesystem (current behavior)."""

    def __init__(self, workspace_path: Path):
        self._root_path = Path(workspace_path)

    @property
    def root(self) -> str:
        return str(self._root_path)

    @property
    def root_path(self) -> Path:
        """Convenience for WorkspaceManager (not part of ABC)."""
        return self._root_path

    def _resolve(self, relative_path: str) -> Path:
        """Path validation extracted from WorkspaceManager.get_path()."""
        if not relative_path:
            return self._root_path.resolve()
        full_path = (self._root_path / relative_path).resolve()
        workspace_resolved = self._root_path.resolve()
        try:
            full_path.relative_to(workspace_resolved)
        except ValueError:
            raise ValueError(f"Path '{relative_path}' escapes workspace boundary")
        return full_path

    # File ops: read_file, write_file, append_file, exists, is_file, is_dir,
    # list_dir, search_files, mkdir, delete_file, delete_directory, move, copy,
    # stat, resolve_path — all via pathlib + shutil

    # Lifecycle: connect/disconnect are no-ops, is_connected always True

    # Shell ops: NOT implemented — ShellManager handles local shell via libtmux
```

`WorkspaceManager` became a thin wrapper that delegates file I/O to the backend and adds higher-level logic (read-before-write tracking, document rendering, vision integration).

## RemoteBackend

**File:** `src/core/backends/remote.py` (~1027 lines)

Full SSH/SFTP backend with remote tmux shell management. `supports_shell` returns `True`, so `ShellManager` delegates all shell operations to this backend instead of using local libtmux.

```python
class RemoteBackend(WorkspaceBackend):
    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = "agent-host",
        key_path: Optional[str] = None,
        workspace_path: str = "/home/agent-host/workspace",
        job_id: str = "",
        scrollback_limit: int = 5000,
        default_timeout: int = 120,
        max_tabs: int = 15,
        blocked_commands: Optional[List[str]] = None,
        sandbox_cwd: Optional[str] = None,
        connect_timeout: int = 30,
        max_retries: int = 3,
    ):
        # paramiko is imported with try/except at module level (deferred dependency)
        # ...
```

Key implementation details:

| Component | Approach |
|-----------|----------|
| **File I/O** | SFTP via `self._sftp.open()` — read, write, append in binary mode |
| **Directory ops** | SFTP `listdir_attr`, `mkdir`, `rmdir` + `rm -rf` for recursive delete |
| **Path validation** | `posixpath.normpath` + boundary check against `_remote_root` |
| **File search** | Server-side `grep -rni` via `_exec()` — avoids transferring every file |
| **Directory size** | Server-side `du -sb` via `_exec()` |
| **SSH reconnect** | `_ensure_connected()` with exponential backoff (1s → 2s → 4s, capped at 10s) |
| **All SSH commands** | `_exec()` helper wraps `exec_command` + raises `WorkspaceUnavailableError` on failure |

### Remote Shell: SSH + tmux (Implemented)

The `RemoteBackend` manages a remote tmux session (`agent_{job_id[:12]}`) over SSH, preserving the same interface as local `ShellManager`:

```
Agent Pod                          VM
  │                                │
  │  paramiko exec_command:        │
  │  "tmux send-keys ... Enter"    │
  │ ──────────────────────────►    │
  │                                ├── tmux session: agent_{job_id[:12]}
  │  "tmux capture-pane -p"        │     ├── tab: default
  │ ──────────────────────────►    │     ├── tab: build
  │  ◄──────────────────────────   │     └── tab: test
  │      (scrollback content)      │
```

The remote shell is **lazily initialized** on the first shell operation (`_init_shell()`). It creates a detached tmux session with a `default` window, sets the history limit, and `cd`s to the workspace.

**`shell_run()` implementation** (sentinel-based completion detection):
1. Pre-flight: capture current scrollback, check for blocked tab (existing interactive prompt)
2. Send `{command}; echo "__DONE_{uuid}__ $?"` via `tmux send-keys`
3. Poll `tmux capture-pane` every 300ms (slightly longer than local for SSH latency)
4. Scan scrollback backwards for the sentinel line → extract exit code and output
5. Same stall detection (5s of unchanged output) and interactive prompt detection as local `ShellManager`
6. Returns formatted string: `Exit code: N\n--- stdout ---\n...`

**Tab management:**
- `_RemoteTab` dataclass tracks per-tab state (name, type, read_cursor, timestamps) — replaces `ShellTab` which requires libtmux objects
- `shell_open_tab()` → `tmux new-window -t session -n name -d`
- `shell_close_tab()` → `tmux kill-window -t session:name`
- `shell_cleanup()` → `tmux kill-session -t session`
- Agent-side `_tabs: OrderedDict[str, _RemoteTab]` registry mirrors local ShellManager's tab tracking

**Blocked command detection, interactive prompt detection, stall detection** — all ported from `ShellManager`, using the same regex patterns and thresholds.

### File Search on Remote (Implemented)

`search_files()` uses server-side `grep` to avoid transferring files over SFTP:

```python
def search_files(self, query, path="", case_sensitive=False):
    flags = "-rn" + ("" if case_sensitive else "i")
    excludes = " ".join(f"--exclude='*.{ext}'" for ext in ["pdf", "docx", "png", ...])
    cmd = f"grep {flags} {excludes} -- '{safe_query}' {remote_path} 2>/dev/null || true"
    output = self._exec(cmd, timeout=60)
    # Parses grep output into [{"path": rel, "line_number": N, "line": content}, ...]
```

### Workspace Initialization

**Decision (updated):** The VM image provisions `~/workspace/` in `agent-host`'s home directory. The agent's `WorkspaceManager` initialization creates subdirectories via `backend.mkdir()` as needed. This mirrors the current local behavior. Single-user model — `agent-host` owns everything.

## VM User Model

Each VM has a single user for simplicity — one VM per job, no shared state:

```
┌──────────────────────────────────────────────────────┐
│  agent-host (sole user, has SSH access + sudo)        │
│  ├── Receives SSH connections from agent pod          │
│  ├── Owns the tmux session and all shell commands     │
│  ├── Owns ~/workspace/ directory                      │
│  └── Full control of the VM environment               │
└────────────────────────────────────────────────────────┘
```

### tmux Session Ownership

The tmux session is owned by `agent-host` (the SSH user). No cross-user complexity — the same user owns the session, the workspace, and the shell.

```bash
# Agent pod operates via SSH:
ssh agent-host@vm "tmux send-keys -t agent_session 'make build' Enter"
ssh agent-host@vm "tmux capture-pane -t agent_session -p"
```

Because `agent-host` owns everything:
- SSH connects as `agent-host`, tmux commands work directly
- SFTP file operations work without ACLs or group hacks — user owns `~/workspace/`
- The SSH connection survives regardless of what happens inside the pane
- No permission issues between SSH user and workspace owner (same user)

## Communication Model

### Within the main cluster: HTTP

Pods on the same Kubernetes cluster communicate via HTTP and ClusterIP Services. This is the industry standard — Kubernetes itself, service meshes, and job orchestrators (Argo, Airflow) all use HTTP/gRPC for intra-cluster communication. A message broker within a single cluster is justified for fan-out (one event, many consumers) or massive-scale work queuing (thousands of workers). We have ~3 agents, one orchestrator, one cockpit. HTTP is exactly right.

The existing agent↔orchestrator communication stays unchanged:
- Agent registration: `POST /api/agents/register` (HTTP)
- Heartbeats: `POST /api/agents/{id}/heartbeat` every 60s (HTTP)
- Job dispatch: `POST http://{agent_pod_ip}:8001/job/start` (HTTP)
- Job control: pause, resume, cancel (HTTP)

No NATS needed for any of this.

### Between clusters: NATS

The only thing HTTP can't do cleanly is cross-cluster communication. The orchestrator on the main cluster can't `POST http://vm-controller.namespace.svc:8080` on the agent cluster — Kubernetes DNS doesn't resolve across clusters. Workarounds (VPN tunnels, NodePort per service, federated DNS) are fragile.

NATS solves this with the hub+leaf topology from [nats.md](./nats.md). The leaf node on the agent cluster dials **out** to the hub on the main cluster — no inbound exposure needed on the agent cluster.

```
Main cluster (HTTP internally)              Agent cluster (k3s + KubeVirt)
┌──────────────────────────────┐           ┌──────────────────────────────┐
│                              │           │                              │
│  orchestrator ◄──HTTP──► agents          │  VM controller               │
│  cockpit ──HTTP──► orchestrator          │    │ KubeVirt API (local)    │
│                              │           │    │                         │
│  NATS hub ◄─────── leaf ────┼───────────┤  NATS leaf                   │
│    │  (cross-cluster only)   │           │    ▲                         │
│    ▼                         │           │    │                         │
│  orchestrator subscribes     │           │  management daemons (in VMs) │
│  to VM events                │           │                              │
└──────────────────────────────┘           └──────────────────────────────┘
```

NATS carries **only** cross-cluster traffic:

| Subject | Direction | Purpose |
|---------|-----------|---------|
| `vm.lifecycle.create` | Orchestrator → VM controller | Request VM creation for a job |
| `vm.lifecycle.delete` | Orchestrator → VM controller | Request VM teardown |
| `vm.lifecycle.status` | VM controller → Orchestrator | VMI creation result (success/fail) |
| `agent.vm.{job_id}.register` | Management daemon → Orchestrator | VM ready, reports IP |
| `agent.vm.{job_id}.heartbeat` | Management daemon → Orchestrator | VM resource monitoring |
| `agent.vm.{job_id}.sudo.request` | Management daemon → Orchestrator | Privilege escalation |
| `agent.vm.{job_id}.sudo.response` | Orchestrator → Management daemon | Approve/deny |

### VM Controller

A small service running on the **agent cluster** that handles VM lifecycle. The orchestrator doesn't need the agent cluster's kubeconfig — it publishes requests on NATS, the VM controller handles KubeVirt locally.

```python
# Conceptual — ~200 lines of actual code
class VMController:
    """Runs on agent cluster. Creates/deletes VMs via KubeVirt API."""

    async def handle_create(self, msg):
        """NATS handler for vm.lifecycle.create"""
        job_config = json.loads(msg.data)
        # Template the VMI manifest (job_id, image, resources, cloud-init)
        manifest = self.template_vmi(job_config)
        # Apply via Kubernetes API (local cluster access)
        await self.k8s_client.create_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace="agent-vms",
            plural="virtualmachineinstances", body=manifest
        )

    async def handle_delete(self, msg):
        """NATS handler for vm.lifecycle.delete"""
        job_id = json.loads(msg.data)["job_id"]
        await self.k8s_client.delete_namespaced_custom_object(
            group="kubevirt.io", version="v1", namespace="agent-vms",
            plural="virtualmachineinstances", name=f"agent-vm-{job_id}"
        )
```

The VM controller:
- Subscribes to `vm.lifecycle.create` and `vm.lifecycle.delete`
- Has native Kubernetes API access (ServiceAccount with RBAC for KubeVirt CRDs in the `agent-vms` namespace)
- Templates the VMI manifest from `deployment/harvester/vm-template.yaml`
- Reports creation status back on `vm.lifecycle.status`
- That's its entire scope — no job logic, no agent management

## Impact on Management Daemon (vm.md)

The [vm.md](./vm.md) design assumed the agent runs inside the VM (option 2). With option 1 (agent on main cluster), the management daemon's responsibilities change:

| Responsibility | vm.md (option 2) | This design (option 1) |
|----------------|-------------------|------------------------|
| Start/stop agent process | Daemon launches `python agent.py` as agent-host | **Not needed** — agent is a K8s pod on the main cluster |
| Receive job config | Daemon receives config over NATS, passes to agent | **Not needed** — orchestrator configures agent pod directly |
| Freeze/resume agent | Daemon sends SIGSTOP/SIGCONT to agent process | **Simplified** — orchestrator pauses agent pod; daemon can still freeze the tmux session for workspace inspection |
| Sudo plugin | Daemon intercepts sudo, routes to orchestrator | **Unchanged** — still needed, still runs on VM |
| Monitoring | Daemon reports CPU, memory, processes | **Unchanged** — still needed for VM health |
| Remote access (code-server) | Daemon manages code-server sessions | **Unchanged** — user can still jump into the VM workspace |
| VM health reporting | Daemon heartbeats to orchestrator | **Unchanged** |

The daemon becomes simpler — it's a monitoring and access control service, not an agent lifecycle manager. It still communicates with the orchestrator (via NATS or HTTP callback), but it doesn't need to know about job configs, checkpoints, or agent state.

The daemon's NATS subjects from vm.md shrink:

```
# Still needed:
agent.vm.{job_id}.register         VM is booted and ready (SSH reachable)
agent.vm.{job_id}.heartbeat        Resource monitoring
agent.vm.{job_id}.sudo.request     Privilege escalation
agent.vm.{job_id}.sudo.response    Approve/deny

# Simplified or removed:
agent.vm.{job_id}.status           Only "ready" and "terminated" — no agent state tracking
agent.vm.{job_id}.control          Only "terminate" — no freeze/resume of agent process
agent.vm.{job_id}.job.config       Not needed — agent gets config from orchestrator, not daemon
```

## Configuration (Implemented)

Backend selection is config-driven, wired into the existing YAML config system.

**Config YAML** (`config/defaults.yaml`):
```yaml
workspace:
  backend: local  # "local" or "remote"
  # Remote backend settings (only used when backend: remote)
  # remote:
  #   host: ""                                        # VM IP or hostname
  #   port: 22
  #   username: agent-host
  #   key_path: /run/secrets/vm-ssh-key               # Path to SSH private key
  #   workspace_path: /home/agent-host/workspace    # Workspace root on the VM
```

**Config schema** (`config/schema.json`): Added `backend` enum property (`"local"|"remote"`) and `remote` object schema with `host`, `port`, `username`, `key_path`, `workspace_path` properties.

**Config dataclass** (`src/core/loader.py`): `WorkspaceConfig` gained two fields:
```python
@dataclass
class WorkspaceConfig:
    # ... existing fields ...
    backend: str = "local"                    # "local" or "remote"
    remote: Optional[Dict[str, Any]] = None   # {host, port, username, key_path, workspace_path}
```

Both config parsing functions (`load_agent_config_from_dict` and the resolved config variant) read these fields from the YAML.

**Agent startup** (`src/agent.py`): When `workspace.backend == "remote"` and `workspace.remote` is set, creates a `RemoteBackend`, calls `connect()`, and passes it to `WorkspaceManager`. Also passes the backend to `ShellManager` when `backend.supports_shell` is True.

**Dependencies:** `paramiko>=3.4.0` added to `requirements.txt`. The import is deferred (try/except) in `remote.py` — only required when `backend: remote` is configured.

For local development, `backend: local` preserves current behavior — zero changes to the dev workflow.

## Integration with Existing Components

### WorkspaceManager (Implemented)

**File:** `src/core/workspace.py`

`__init__` accepts an optional `backend` parameter. Defaults to `LocalBackend(self._workspace_path)` when none provided:

```python
def __init__(self, job_id, config=None, base_path=None, backend=None):
    # ... base path setup ...
    if backend is not None:
        self._backend = backend
    else:
        from .backends.local import LocalBackend
        self._backend = LocalBackend(self._workspace_path)
```

All file I/O methods delegate to `self._backend` (read_file, write_file, append_file, exists, list_files, search_files, create_directory, delete_file, delete_directory, move_file, copy_file, get_size, get_summary). `get_path()` delegates to `Path(self._backend.resolve_path(relative_path))`. Exposes `backend` property for ShellManager to check `supports_shell`.

Higher-level logic stays in the manager: read-before-write tracking, document rendering (VisionHelper, DocumentRenderer), workspace initialization (creating directory structure via backend.mkdir).

### ShellManager (Implemented)

**File:** `src/tools/coding/shell_manager.py`

Added `backend: Optional[Any] = None` parameter to `__init__`. Sets `_use_backend = True` when `backend.supports_shell` is True.

When `_use_backend` is True, **all methods delegate to the backend** via early returns at the top of each method:

```python
def run_sync(self, command, timeout, tail, working_dir):
    if self._use_backend:
        raw = self._backend.shell_run(command, timeout, "default", working_dir)
        # ... tail truncation ...
        return raw
    # ... existing libtmux code unchanged ...
```

Methods that delegate: `run_sync()`, `send()`, `read()`, `read_with_offset()`, `close_tab()`, `format_tab_header()`, `list_tabs()`, `cleanup()`, `is_alive()`, `open_tab()`, `ensure_tab()`.

For backend-delegated tabs, stub `ShellTab(window=None, pane=None)` objects are created for local tracking compatibility (tab registry).

**Sentinel-based completion detection lives in the backend** (`RemoteBackend.shell_run`), not in `ShellManager`. This avoids `ShellManager` needing to know about SSH channels. The local libtmux code in `ShellManager` is untouched in the else branches — zero behavioral change for `backend: local`.

### GitManager (Not Yet Wired)

Still uses `subprocess.run(["git", ...], cwd=workspace_path)`. Git commands work via `shell_run` for remote workspaces, but `GitManager` itself has not been modified to delegate through the backend. This is deferred — low priority since git tools are used only for workspace versioning, not core job execution.

### Workspace Injection

`workspace_injection.py` reads `workspace.md` through `WorkspaceManager`, which delegates to the backend. Works transparently for both local and remote.

### Document Processing

PDFs and other documents on the VM transfer over SFTP via `backend.read_file("documents/report.pdf", binary=True)`. `DocumentRenderer` processes bytes locally on the agent pod (Poppler renders pages to PNG). For typical document sizes (academic papers, code files), SFTP transfer is negligible.

## Error Handling

The `RemoteBackend` introduces failure modes that don't exist locally:

| Failure | Detection | Recovery |
|---------|-----------|----------|
| SSH connection lost | `paramiko.SSHException` on any operation | Auto-reconnect with backoff (3 attempts). If all fail, raise `WorkspaceUnavailableError`. |
| VM crashed / unreachable | Connection timeout | Agent reports to orchestrator. Orchestrator can provision a fresh VM and reassign. |
| Command hangs on VM | Same sentinel timeout as local (configurable) | `shell_run` returns timeout result. Agent handles as it does today. |
| SFTP transfer failure | `IOError` from paramiko | Retry once. On second failure, raise. |
| Disk full on VM | Write fails with `OSError` | Propagate to tool. Agent sees error, can clean up or report. |

A new exception type, `WorkspaceUnavailableError`, signals that the workspace backend is unreachable. The graph can catch this and either retry, request a new VM, or fail the job gracefully.

### Phase Snapshot Survival

Phase snapshots (`workspace/phase_snapshots/job_<id>/phase_<n>/`) currently live on the workspace filesystem. With a remote backend, they live on the VM — meaning a VM crash destroys the snapshots needed for recovery.

**Solution:** The agent extracts snapshots to the main cluster at phase boundaries. Phase transitions already trigger snapshot creation (`PhaseSnapshotManager`). Adding a "pull snapshot to pod-local storage" step after each phase is low-overhead (snapshots are small: checkpoint.db + a few text files). The local copies live alongside the checkpoint DB in `workspace/phase_snapshots/` on the agent pod.

This means recovery works even if the VM is gone:
1. Orchestrator provisions a fresh VM
2. Agent pod still has the checkpoint DB and phase snapshots locally
3. Agent pushes the snapshot to the new VM via backend
4. Agent resumes from checkpoint

Alternatively, the orchestrator can pull snapshots via SFTP periodically, independent of the agent. Either way, snapshots must not live exclusively on the VM.

## Decisions (March 2026)

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | Agent location | **Main cluster** | Fault isolation — agent survives workspace destruction |
| 2 | Transport (agent↔VM workspace) | **SSH (paramiko + SFTP)** | Standard, encrypted, battle-tested. No additional infrastructure needed. |
| 3 | All files on VM | **Yes** | Both clusters on same LAN, latency is negligible. Simpler than splitting files between pod and VM. |
| 4 | Shell approach | **Remote tmux over SSH** | Preserves existing sentinel-based completion detection. Named tabs, scrollback, async — all work identically. |
| 5 | Backend selection | **Config-driven** | `workspace.backend: local` for dev, `remote` for production. Zero tool changes. |
| 6 | Intra-cluster communication | **HTTP (unchanged)** | Agent↔orchestrator, cockpit↔orchestrator stay HTTP via ClusterIP Services. Standard practice — a message broker is overhead for ~5 pods. |
| 7 | Cross-cluster communication | **NATS (hub+leaf)** | Only for traffic between main cluster and agent cluster. Orchestrator↔VM controller, management daemon↔orchestrator. |
| 8 | VM lifecycle management | **VM controller on agent cluster** | Small service subscribes to NATS, calls KubeVirt API locally. Orchestrator doesn't need remote kubeconfig. |
| 9 | Mount-based approach | **Rejected** | NFS/SSHFS mounts fail silently on network issues. Explicit backend with error handling is more resilient. |
| 10 | tmux ownership | **agent-host owns session and all shells** | Single-user model — no cross-user complexity. SSH user owns everything. |
| 11 | Workspace file access (SFTP) | **Direct ownership** | `agent-host` owns `~/workspace/`. No ACLs, no group hacks needed. |
| 12 | Phase snapshot survival | **Extract to agent pod at phase boundaries** | Snapshots must survive VM destruction for recovery to work. |
| 13 | SSH keys (MVP) | **Shared keypair** | One keypair for all agent↔VM connections. Per-job keys as a hardening step later. |
| 14 | ShellManager vs backend | **ShellManager delegates, backend owns shell logic** | Sentinel detection, stall detection, prompt detection all live in `RemoteBackend.shell_run()`. ShellManager is a thin delegator when `_use_backend` is True. For local, ShellManager keeps its libtmux code unchanged. |
| 15 | Agent pod reuse | **Long-lived pods, reconnect per job** | Agent stays alive between jobs, `RemoteBackend` does `disconnect()` + `connect()` to a new VM. No pod scheduling overhead. Matches current behavior (agents process jobs sequentially, go back to `ready`). |
| 16 | Document delivery to VM | **Agent pushes after connecting** | Agent copies input documents to VM via `backend.write_file()` during workspace setup. Same flow as local — `WorkspaceManager` copies documents during init. No special cloud-init or orchestrator-side push needed. |

## Implementation Roadmap

### Phase 1: Extract LocalBackend — DONE

**Goal:** Refactor current code to use the backend interface without changing any behavior.

**What was implemented:**

1. **`WorkspaceBackend` ABC** in `src/core/workspace_backend.py`
   - File operations (abstract): `read_file`, `write_file`, `append_file`, `exists`, `is_file`, `is_dir`, `list_dir`, `search_files`, `mkdir`, `delete_file`, `delete_directory`, `move`, `copy`, `stat`, `resolve_path`
   - Shell operations (non-abstract, default `NotImplementedError`): `shell_run`, `shell_send`, `shell_read`, `shell_read_with_offset`, `shell_ensure_tab`, `shell_open_tab`, `shell_close_tab`, `shell_list_tabs`, `shell_format_tab_header`, `shell_cleanup`, `shell_is_alive`
   - `supports_shell` property for backend capability detection
   - Lifecycle: `connect`, `disconnect`, `is_connected`, `root`
   - `WorkspaceUnavailableError` for connection failures

2. **`LocalBackend`** in `src/core/backends/local.py`
   - Extracts all file I/O from `WorkspaceManager` into backend methods
   - Path validation (`_resolve()`) extracted from `get_path()`
   - Shell operations not implemented (local libtmux stays in ShellManager)

3. **`WorkspaceManager`** wired to backend
   - `__init__` accepts optional `backend` parameter, defaults to `LocalBackend`
   - All file I/O methods delegate to `self._backend`
   - `get_path()` delegates to `self._backend.resolve_path()`
   - Zero behavioral change — all 1654 tests pass unchanged

### Phase 2: Implement RemoteBackend — DONE

**Goal:** Agent can operate on a remote workspace over SSH.

**What was implemented:**

1. **`RemoteBackend`** in `src/core/backends/remote.py`
   - paramiko SSH client with auto-reconnect + exponential backoff
   - SFTP for all file operations (read, write, append, list, delete, move, copy, stat)
   - Server-side `grep` for `search_files()` (avoids transferring every file)
   - Server-side `du -sb` for directory size, `rm -rf` for recursive delete
   - SSH `exec_command` for all remote operations via `_exec()` helper

2. **Remote tmux integration**
   - Creates remote tmux session lazily on first shell operation (`_init_shell()`)
   - `shell_run()` implements full sentinel-based completion detection over SSH
   - Same stall detection and interactive prompt detection as local ShellManager
   - Named tab management via `tmux new-window`, `tmux kill-window`, `tmux send-keys`
   - `_tmux_capture()` reads scrollback via `tmux capture-pane -p`
   - Tab state tracked in local `_tabs` OrderedDict (agent-side registry)

3. **ShellManager backend delegation**
   - New `backend` parameter on `ShellManager.__init__`
   - When `backend.supports_shell` is True, all methods delegate to backend
   - Local libtmux code untouched in the else branch — zero behavioral change
   - Agent startup passes backend to ShellManager when `workspace.backend: remote`

4. **Config integration**
   - `WorkspaceConfig` gains `backend: str` and `remote: Optional[Dict]` fields
   - Config parsing in `loader.py` reads `workspace.backend` and `workspace.remote`
   - `config/defaults.yaml` has `backend: local` with commented-out remote settings
   - Agent startup (`src/agent.py`) creates `RemoteBackend` when config says `remote`
   - `paramiko>=3.4.0` added to `requirements.txt`

**Remaining for Phase 2 (deferred):**

- Integration test with a real VM (needs KubeVirt or VirtualBox test environment)
- GitManager wiring to use backend for remote git commands (git works via shell_run for now)
- Per-job SSH key support (shared keypair is fine for MVP)

**Risk:** Medium. Transport layer is complete but untested against a real VM.

### Phase 3: NATS + VM Controller — PARTIALLY DONE

**Goal:** Cross-cluster communication works. VM controller can create/delete VMs on demand.

**What was implemented (code + deployment artifacts):**

1. **NATS Helm values** in `deployment/nats/`
   - `nats-hub-values.yaml` — 3-node cluster, JetStream, leafnode port on NodePort 30742
   - `nats-leaf-values.yaml` — single node, no JetStream, dials out to hub
   - `setup-streams.sh` — creates `VM_EVENTS`, `AGENT_HEARTBEATS`, `JOB_ASSIGNMENTS` streams

2. **VM Controller service** in `vm-controller/`
   - `controller.py` (~270 lines) — async Python service using `nats-py` + `kubernetes` client
   - Subscribes to `vm.lifecycle.create`, `vm.lifecycle.delete`, `vm.lifecycle.get`
   - On create: renders `vm-template.yaml` via string substitution → applies VirtualMachine via K8s API
   - On delete: deletes VirtualMachine by name (`agent-vm-{job_id}`)
   - On get: queries VirtualMachine status (request/reply pattern)
   - Publishes results on `vm.lifecycle.status`
   - Auto-reconnect to NATS (indefinite retries)
   - `Dockerfile` + `requirements.txt` (nats-py, kubernetes, pyyaml)

3. **K8s manifest** `deployment/25-vm-controller.yaml`
   - Namespace `agent-vms`, ServiceAccount, Role (KubeVirt CRDs only), RoleBinding
   - VM template mounted as ConfigMap volume
   - Env: `NATS_URL`, `VM_TEMPLATE_PATH`, `VM_NAMESPACE`, `DEFAULT_VM_IMAGE`, defaults

4. **NATS bridge in orchestrator** — `orchestrator/services/nats_bridge.py` (**DONE**)
   - Module-level `try/except` import for `nats-py` (same graceful degradation as MongoDB)
   - `NatsBridge` class: `connect()`, `disconnect()`, 4 publishers, 4 subscription handlers
   - Subscriptions: `vm.lifecycle.status`, `agent.vm.*.register`, `agent.vm.*.heartbeat`, `agent.vm.*.status`
   - Publishers: `request_vm_create`, `request_vm_delete`, `query_vm_status`, `send_control`
   - Job context updated under `"vm"` key via read-modify-write on JSONB
   - Module-level singleton: `nats_bridge = NatsBridge()`

5. **Local dev NATS in docker-compose** — `docker-compose.dev.yaml` (**DONE**)
   - `nats:2.10-alpine` with JetStream, ports 4222 (client) + 8222 (monitoring)
   - Named volume `srw_nats_dev_data`

**Remaining (requires deployment):**

6. **Actually deploy NATS** — hub on main cluster, leaf on agent cluster, verify cross-cluster pub/sub
7. **Build and push VM controller image** — `podman build` + `podman push`
8. **Deploy VM controller** to agent cluster

**Risk:** Medium. Code is written but untested against real NATS + KubeVirt infrastructure.

### Phase 4: Orchestrator VM Integration — DONE

**Goal:** Full VM lifecycle: orchestrator requests VM, waits for readiness, configures agent, tears down on completion.

**Job lifecycle with VM:**

```
1. Job created (cockpit / API)
       │
       ▼
2. Orchestrator provisions VM:
   - Same-cluster: direct KubeVirt API call
   - Cross-cluster: publishes vm.lifecycle.create on NATS
   (job_id, image, resources, cloud-init config)
       │
       ▼
3. VM controller (or orchestrator directly) creates KubeVirt VMI
       │
       ▼
4. VM boots → management daemon starts → publishes agent.vm.{job_id}.register
   (includes VM IP, confirms SSH is reachable)
       │
       ▼
5. Orchestrator receives register → records VM IP in job metadata
       │
       ▼
6. Auto-dispatcher assigns job to a ready agent pod (existing HTTP flow)
   Agent config includes: workspace.backend=remote, remote.host=<vm_ip>
       │
       ▼
7. Agent connects to VM via SSH (RemoteBackend) → works
   Phase snapshots extracted to pod-local storage at each phase boundary
       │
       ▼
8. Job completes → agent disconnects → orchestrator deletes VM
   (direct K8s API or vm.lifecycle.delete via NATS)
       │
       ▼
9. VM destroyed
```

**What was implemented (orchestrator-side VM provisioning):**

1. **Unified VM provisioner** in `orchestrator/services/vm_provisioner.py`
   - `VMProvisioner` class with auto-selected backend: NATS (cross-cluster) or direct K8s API (same-cluster)
   - NATS priority: when `NATS_URL` is configured, NATS mode is used; otherwise falls back to direct K8s if `kubernetes` client and VM template are available
   - Direct mode renders the same `vm-template.yaml` as the VM Controller, calls KubeVirt API via `kubernetes` Python client (`asyncio.to_thread` to avoid blocking)
   - `create_vm()`, `delete_vm()`, `query_status()`, `send_control()` — unified interface regardless of backend
   - Module-level singleton: `vm_provisioner = VMProvisioner()`

2. **REST API endpoints** in `orchestrator/main.py`
   - `POST /api/vms` — create VM for a job (returns `mode: "nats"|"direct"`)
   - `GET /api/vms` — list jobs with active VMs (DB query, no NATS required)
   - `GET /api/vms/{job_id}` — get VM status (DB + optional `?live=true` for real-time query)
   - `DELETE /api/vms/{job_id}` — delete VM for a job
   - All return 503 when no VM provisioning backend is available (except `GET /api/vms`)

3. **Lifecycle hooks** in existing endpoints
   - `cancel_job`: sends terminate control (NATS only) + deletes VM (either backend)
   - `pause_job`: sends freeze control to management daemon (NATS only)
   - `complete_job`: auto-deletes VM when job status is `completed` or `failed`

4. **Graceful degradation** following MongoDB pattern
   - `nats-py` optional import with `NATS_AVAILABLE` flag
   - `kubernetes` optional import with `K8S_AVAILABLE` flag
   - No NATS + no K8s = VM features disabled, system works as before
   - All operations return `False`/`None` when unavailable (no exceptions)

5. **Infrastructure**
   - `NATS_URL` env var in orchestrator K8s deployment (`optional: true`)
   - `VM_TEMPLATE_PATH`, `VM_NAMESPACE` env vars in orchestrator K8s deployment (`optional: true`)
   - `nats-py>=2.9.0` and `kubernetes>=28.0.0` in `orchestrator/requirements.txt`
   - `VMCreateRequest` Pydantic model for the create endpoint

6. **Auto-dispatch wiring** (**DONE**)
   - Daemon register payload now includes VM IP address (`daemon.py`)
   - NATS bridge stores `ssh_host` in job context on registration (`nats_bridge.py`)
   - Dispatcher detects jobs needing VMs via `_job_needs_vm()` helper — checks `context.vm.requested` flag and `config_override.workspace.backend == "remote"`
   - Jobs with VMs in provisioning state are held (skipped by dispatcher) until VM registers
   - `_dispatch_job_to_agent()` injects `workspace.backend=remote` and `workspace.remote.host=<vm_ip>` into `config_override` when VM is ready

7. **Phase snapshot extraction** (**DONE**)
   - `PhaseSnapshotManager` accepts optional `workspace_backend` parameter
   - On `create_snapshot()`: when backend is remote, pulls workspace files (workspace.md, plan.md, todos.yaml, archive/) from VM to pod-local snapshot directory via SFTP
   - On `recover_to_phase()`: when backend is remote, pushes snapshot files back to VM via SFTP
   - Checkpoint DB is always pod-local (no change needed)

8. **VM recovery on failure** (**DONE**)
   - Graph's audited tool node detects `WorkspaceUnavailableError` in tool results and re-raises (bypassing ToolNode's error-to-message conversion)
   - Agent's `process_job()` catches `WorkspaceUnavailableError` and returns error with `type: "workspace_unavailable"`
   - Orchestrator's `complete_job` detects `workspace_unavailable` error type → deletes old VM → resets VM context with `requested: true` → sets job to `paused` (dispatchable) → triggers dispatcher
   - Dispatcher auto-provisions a new VM for the re-queued job → daemon registers → dispatcher dispatches with workspace config
   - Agent resume detects fresh VM workspace (no workspace.md) → seeds from latest pod-local snapshot

9. **Auto-provision on job creation** (**DONE**)
   - Dispatcher's `_try_dispatch_pending_jobs()` checks each pending job with `_job_needs_vm()`
   - If VM needed but not provisioned: calls `vm_provisioner.create_vm()` and skips dispatch (waits for daemon register)
   - If VM provisioning/creating: skips dispatch (waits)
   - If VM ready: proceeds with dispatch (workspace config auto-injected)

**Risk:** Low for code correctness. Needs integration testing against real KubeVirt infrastructure to validate VM boot timing, SSH connectivity, and snapshot transfer performance.

### Phase Summary

| Phase | Depends on | Scope | Status |
|-------|-----------|-------|--------|
| 1 — Extract LocalBackend | Nothing | Refactor only, no behavior change | **DONE** |
| 2 — RemoteBackend | Phase 1 | SSH/SFTP files + remote tmux shell | **DONE** (untested against real VM) |
| 3 — NATS + VM Controller | Nothing (independent of Phase 1-2) | Cross-cluster infra, small new service | **Code DONE** — needs deployment |
| 4 — Orchestrator VM Integration | Phases 2 + 3 | Full VM lifecycle, wiring everything together | **DONE** — provisioner, REST API, lifecycle hooks, auto-dispatch, snapshot extraction, VM recovery |

Phases 1-4 are code-complete. All VM lifecycle management code is implemented: backend abstraction (local + remote), NATS infrastructure, VM provisioning (two backends), auto-dispatch wiring, snapshot extraction from VMs, and VM failure recovery. Needs integration testing against real KubeVirt infrastructure.

## Open Questions

1. **VM boot timeout**: How long should the orchestrator wait for a management daemon to register before declaring VM creation failed? VMs take 5-30s to boot. With cloud-init, management daemon startup, and NATS connection — 60s timeout seems reasonable. But a stuck cloud-init or image pull could take longer. Needs testing with real VM images on the actual hardware.

2. **Per-job SSH keys (future hardening)**: Shared keypair is fine for the home lab. For production, per-job ephemeral keypairs are more secure but add orchestrator complexity (generate keypair → inject private key as K8s secret → inject public key via cloud-init → clean up secret after job). Evaluate when moving beyond the home lab.

3. **Connection pooling**: Single paramiko SSH connection handles SFTP + shell via multiple channels. Under heavy parallel tool calls, this could bottleneck. Not a concern for MVP — revisit if profiling shows SSH as a bottleneck during Phase 2 testing.

## Related

- [[vm]] — VM architecture, management daemon, image hierarchy
- [[nats]] — Messaging layer (orchestrator↔agent, not agent↔VM)
- [[deployment]] — Main cluster Kubernetes manifests
- [[cloud_workspace]] — Original cloud workspace spec
