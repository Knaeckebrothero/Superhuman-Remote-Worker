# Workspace Simplification

## Problem

The current workspace system evolved from a time when agents shared a single filesystem. It uses `job_{uuid}` subdirectories under a base path to isolate jobs from each other. Now that every job/session gets a dedicated workspace container (K8s pod) or VM, this subdirectory system is redundant — the container IS the isolation boundary.

This unnecessary layering causes real bugs:

- **File leaks between sessions**: Cleanup code tried to `find -name 'job_*'` on a flat remote filesystem where no `job_*` dirs exist. Files from previous sessions leaked into new ones.
- **Nextcloud sync broken**: `WorkspaceSyncService` used `os.walk()` on a local path, but the actual files live on a remote workspace container via SSH/SFTP. The local directory was always empty, so nothing ever synced.
- **Gitea clone failures**: Clone URLs used `localhost` which doesn't resolve inside workspace containers.
- **Path confusion**: The orchestrator's `WorkspaceService` constructs `base/job_{id}/` paths to read workspace files locally, but in K8s the orchestrator has no filesystem access to workspace containers at all — these paths are dead code in production.
- **Stale PVC data**: PVCs mounted at `/home/agent-host/workspace` persist across pod restarts, requiring explicit cleanup that frequently fails or is incomplete.

---

## Current Implementation

### Workspace Path Construction

**WorkspaceManager** (`src/core/workspace.py:175-214`)
```
base_path (WORKSPACE_PATH env or ./workspace)
  └── job_{uuid}/           <-- THIS IS THE PROBLEM
       ├── archive/
       ├── documents/
       ├── chunks/
       ├── candidates/
       ├── requirements/
       ├── output/
       ├── repos/
       ├── workspace.md
       ├── plan.md
       └── todos.yaml
```

The `job_{uuid}` subdirectory is always appended (`workspace.py:203`):
```python
self._workspace_path = self._base_path / f"job_{job_id}"
```

### Agent Pod (harness) vs Workspace (execution environment)

The agent process runs on a separate pod from the workspace. All file/shell operations are proxied:

```
Agent Pod (harness)                    Workspace Container/VM
┌──────────────────────┐              ┌──────────────────────┐
│ LangGraph process    │   SSH/SFTP   │ /home/agent-host/    │
│ Checkpoints (.db)    │ ──────────>  │   workspace/         │
│ Logs (.log)          │              │     (agent files)    │
│ Phase snapshots      │              │ .ssh/                │
│                      │   tmux/SSH   │ .tmux.conf           │
│ ShellManager ────────│──────────>   │ code-server          │
│ WorkspaceManager ────│──────────>   │ tmux sessions        │
└──────────────────────┘              └──────────────────────┘
```

The proxy is implemented by `RemoteBackend` (`src/core/backends/remote.py`):
- File operations: SFTP (read_file, write_file, list_dir, etc.)
- Shell operations: tmux sessions over SSH (run_command, send_keys, read_output)
- Path validation: all paths resolved relative to `workspace_path`, escape prevented

### Workspace Container Setup

**Dockerfile** (`docker/Dockerfile.workspace`):
- User: `agent-host` (UID 1000), no sudo
- Home: `/home/agent-host/`
- Workspace: `/home/agent-host/workspace/` (separate subdirectory)
- SSH keys: `/home/agent-host/.ssh/authorized_keys`
- Services: sshd (port 22), code-server (port 8080)

**K8s Pod** (`orchestrator/services/container_provisioner.py:564-567`):
- PVC mounted at `/home/agent-host/workspace`
- SSH pubkey mounted from K8s secret

**Entrypoint** (`docker/workspace-entrypoint.sh`):
- Copies SSH pubkey to `~/.ssh/authorized_keys`
- Starts sshd + code-server
- code-server launched with explicit path: `code-server --bind-addr 0.0.0.0:8080 /home/agent-host/workspace`

**VM base** (`docker/agent-vm-base/scripts/provision.sh:144-157`):
- Same `agent-host` user, same `/home/agent-host/workspace` path

### Orchestrator's Workspace View

**Container provisioning** (`orchestrator/main.py:698,726,971`):
```python
remote.setdefault("workspace_path", "/home/agent-host/workspace")
```

**IDE URL** (`orchestrator/main.py:9089`):
```python
f"{proxy_base}/api/ide/{thread_id}/proxy/?folder=/home/agent-host/workspace"
```

**WorkspaceService** (`orchestrator/services/workspace.py:74`):
```python
job_path = self._base / f"job_{job_id}"
```
This reads workspace files (todos, workspace.md, plan.md) via LOCAL filesystem. Only works when orchestrator shares a volume with agents (dev/docker-compose). Dead code in K8s.

### Agent-Side Harness Storage

Stored on the agent pod (NOT the workspace), under `WORKSPACE_PATH`:
- `workspace/checkpoints/job_{id}.db` — LangGraph SQLite checkpoint
- `workspace/logs/job_{id}.log` — per-job log file
- `workspace/phase_snapshots/job_{id}/` — phase recovery snapshots

---

## Detailed Impact Analysis

### 1. Phase Snapshots & Recovery

**PhaseSnapshotManager** (`src/core/phase_snapshot.py`)

Storage structure on agent pod:
```
workspace/phase_snapshots/job_<id>/
├── phase_<n>/checkpoint.db       (LangGraph checkpoint copy)
├── phase_<n>/metadata.json       (Snapshot metadata)
├── phase_<n>/workspace.md        (Workspace file copy)
├── phase_<n>/plan.md             (Plan file copy)
├── phase_<n>/todos.yaml          (Todos file copy)
└── phase_<n>/archive/            (Archived todos)
```

Critical path references:
- Line 186: `self._snapshots_dir = get_phase_snapshots_path() / f"job_{job_id}"`
- Line 187: `self._workspace_path = base_path / f"job_{job_id}"`
- Line 188: `self._checkpoint_path = base_path / "checkpoints" / f"job_{job_id}.db"`
- Lines 230-331: `create_snapshot()` copies workspace files to snapshot dir
- Lines 273-304: Copies `workspace.md`, `plan.md`, `todos.yaml`, `archive/` from workspace
- Lines 383-512: `recover_to_phase()` restores files from snapshot to workspace

**Key**: Snapshots live on the agent pod, not the workspace container. The `_workspace_path` reference is used to know WHERE to copy files from (via the backend). After simplification, the workspace path just changes — the snapshot storage pattern on the agent pod can stay as-is.

### 2. Checkpoints

**Agent pod storage** (`src/core/workspace.py:113-125`, `src/agent.py:2526-2535`)
- Path: `workspace/checkpoints/job_{id}.db`
- Used by: `AsyncSqliteSaver` for LangGraph state persistence
- Referenced in resume paths: `src/agent.py:494-499, 2340-2405, 2407-2524`

**No change needed**: Checkpoints are on the agent pod, not the workspace container. The `job_{id}` in the filename is fine — it's just a naming convention for the SQLite file, not a directory structure issue.

### 3. Logging

**Per-job log files** on agent pod:
- `src/api/app.py:353-388`: `_setup_job_file_logging()` → `logs_dir / f"job_{job_id}.log"`
- `src/api/dual_app.py:339-343,758-761`: Same pattern
- `orchestrator/main.py:9396`: Reads logs via `workspace_service.base_path / "logs" / f"job_{job_id}.log"`

**No change needed**: Logs are on the agent pod, named by job ID. Not affected by workspace container layout.

### 4. Frozen Job State & Output

Files written INSIDE the workspace (via backend):
- `output/job_frozen.json` — written by `src/core/phase.py:527-530,925-928` and `src/graph.py:2667,2949`
- `output/job_completion.json` — written during completion

Files read by orchestrator (LOCAL filesystem, dead code in K8s):
- `orchestrator/main.py:4625-4640` — Read frozen state from `workspace_service.base_path / f"job_{job_id}" / "output" / "job_frozen.json"`
- `orchestrator/main.py:4650-4657` — Delete frozen file
- `orchestrator/main.py:4806-4813` — VM upgrade frozen read
- `orchestrator/main.py:4850-4854` — Delete frozen file
- `orchestrator/main.py:6165-6174` — Freeze data retrieval

**Within workspace**: `output/job_frozen.json` uses relative paths via WorkspaceManager — no change needed.
**Orchestrator local reads**: All use `job_{id}` paths. These are dev-only / dead code in K8s. Should be deprecated.

### 5. Gitea & Git Versioning

**Repo naming** (`orchestrator/main.py:2646`):
- `repo_name = f"job-{short_id}"` — This is a Gitea repo name, NOT a filesystem path. No change needed.

**Subjob branches** (`orchestrator/main.py:2587`):
- `branch_name = f"subjob/{short_id}/{config_name_slug}"` — Semantic, not filesystem. No change.

**Git clone targets** (`src/core/workspace.py:310-315`):
- `GitManager.clone(url, self._workspace_path, backend=self._backend)` — Clones into workspace root. After simplification, this clones into home dir, which is correct.

**Worktree paths** (`orchestrator/main.py:5095-5096,5772-5773`):
- `worktree_path = f"/home/agent-host/worktrees/{short_id}-{config_name}"` — Already uses a separate `/home/agent-host/worktrees/` path. No change needed.

**IDE git clone** (`orchestrator/services/ide_session.py:564,852`):
- `git clone --branch {branch} {clone_url} /home/agent-host/workspace` — Hardcoded path needs update.

**Squash merge** (`orchestrator/main.py:216-310`):
- `_squash_merge_subjob()` operates on Gitea repos via API. No filesystem paths. No change.

**Subjob cleanup files** (`orchestrator/main.py:185-193`):
- `SUBJOB_CLEANUP_FILES = ["workspace.md", "plan.md", "todos.yaml", ...]` — Relative paths deleted from git branch before merge. No change.

### 6. Nextcloud & Cloud Sharing

**Session folders** (`orchestrator/main.py:8702`):
- `nc_folder = f"sessions/{thread_id[:8]}"` — Thread-scoped, NOT job-scoped. No change.

**Folder creation** (`orchestrator/services/nextcloud_admin.py:343-370`):
- Creates WebDAV folders named `sessions/{thread_id[:8]}`. No `job_*` references.

**Workspace sync** (`src/services/workspace_sync.py`):
- Syncs files from workspace root via backend. Path-agnostic — only needs the `workspace_path` parameter to change.
- Ignore patterns (line 27-38): No `job_*` references.
- WebDAV remote paths are relative (e.g., `documents/report.pdf`). No change.

**Project cloud folders**: Named by project name, not job ID. No change.

**S3 snapshots** (`orchestrator/services/snapshot_service.py`):
- Uses `s3://{bucket}/{entity_type}/{uuid}/` paths. Orthogonal system. No change.

**Cockpit UI**: Uses `thread.nc_session_folder` from database. No hardcoded paths. No change.

### 7. Container & VM Provisioning

**Container provisioner** (`orchestrator/services/container_provisioner.py`):
- Line 567: `"mountPath": "/home/agent-host/workspace"` — **Must change** to `/home/agent-host`
- Line 164: PVC naming `pvc-workspace-{job_id[:12]}` — naming convention, not path. Can stay.

**VM provisioner** (`orchestrator/services/vm_provisioner.py`):
- No direct hardcoded workspace paths. VM manifest templates used.

**Agent provisioner** (`orchestrator/services/agent_provisioner.py`):
- Line 591: Mounts emptyDir at `/workspace` on agent pod (local scratch). Not workspace container.

**Persistent provisioner** (`orchestrator/services/persistent_provisioner.py`):
- Line 475: Mounts at `/workspace` on agent pod. Not workspace container.
- Line 153: PVC naming `pvc-persistent-{thread_id[:12]}`.

### 8. IDE Sessions

**Hardcoded paths** (`orchestrator/services/ide_session.py`):
- Line 25: `folder="/home/agent-host/workspace"` — default code-server folder
- Line 564: `git clone ... /home/agent-host/workspace` — K8s IDE clone target
- Line 852: `git clone ... /home/agent-host/workspace` — VM IDE clone target
- Line 603: code-server URL with `/home/agent-host/workspace`

All need update to `/home/agent-host`.

### 9. Orchestrator Local Workspace Access

**WorkspaceService** (`orchestrator/services/workspace.py`):
- Line 74: `job_path = self._base / f"job_{job_id}"` — **THE** central `job_*` reference
- Used by 8+ endpoints for todos, workspace files, output reading
- Dev-only/docker-compose functionality; dead code in K8s

**Orchestrator endpoints** (`orchestrator/main.py`):
- Lines 4625-4854: Multiple frozen job file reads using `workspace_service.base_path / f"job_{job_id}"` paths
- Line 6165-6174: Freeze data retrieval
- Lines 6824-6942: Workspace/todo endpoints

### 10. Deployment Manifests

**Shared workspace PVC** (`deployment/13-workspace-pvc.yaml`):
- Single `srw-workspace` PVC, ReadWriteMany, 20Gi
- Mounted at `/workspace` on both orchestrator and agent pods
- This is the LOCAL scratch space on agent pod, not the workspace container

**ConfigMap** (`deployment/02-configmap.yaml`):
- `WORKSPACE_PATH: "/workspace"` — Agent pod local scratch path

### 11. Tests

Hardcoded `job_*` paths in tests:
- `tests/tools/research/conftest.py:65`: `backend.root = "/home/agent-host/workspace/job_abc123"`
- `tests/tools/research/test_browser_tools.py:467,536-537`: Paths with `/workspace/job_abc123/`
- `tests/test_workspace_backends.py:629,686,899,914,918,939,993,1034`: All reference `/home/agent-host/workspace`
- `tests/test_delegation.py:205,319-380`: Worktree path assertions

---

## Full Reference: All `job_{id}` Path Occurrences

| File | Line(s) | Pattern | Context |
|------|---------|---------|---------|
| `src/core/workspace.py` | 203 | `f"job_{job_id}"` | WorkspaceManager path construction |
| `src/core/workspace.py` | 113-125 | `checkpoints/` | Checkpoint base dir (keep) |
| `src/core/workspace.py` | 128-140 | `logs/` | Logs base dir (keep) |
| `src/core/phase_snapshot.py` | 186 | `f"job_{job_id}"` | Snapshot dir per job |
| `src/core/phase_snapshot.py` | 187 | `f"job_{job_id}"` | Workspace path ref |
| `src/core/phase_snapshot.py` | 188 | `f"job_{job_id}.db"` | Checkpoint path ref |
| `src/agent.py` | 2535 | `f"job_{job_id}.db"` | Checkpoint file naming |
| `src/api/app.py` | 366 | `f"job_{job_id}.log"` | Log file naming |
| `src/api/dual_app.py` | 343, 761 | `f"job_{job_id}.log"` | Log file naming |
| `src/init.py` | 197, 305 | `glob("job_*")` | Job directory discovery |
| `orchestrator/services/workspace.py` | 74 | `f"job_{job_id}"` | Local workspace access |
| `orchestrator/main.py` | 4629, 4652 | `f"job_{job_id}"` | Frozen file paths |
| `orchestrator/main.py` | 4713, 4808 | `f"job_{job_id}"` | Output dir paths |
| `orchestrator/main.py` | 4851, 6169 | `f"job_{job_id}"` | Frozen file read/delete |
| `orchestrator/main.py` | 9396 | `f"job_{job_id}.log"` | Log file retrieval |
| `orchestrator/main.py` | 2364-2386 | `startswith("job_")` | Workspace status endpoint |

## Full Reference: All `/home/agent-host/workspace` Occurrences

| File | Line(s) | Context |
|------|---------|---------|
| `docker/Dockerfile.workspace` | 133-134 | Directory creation |
| `docker/workspace-entrypoint.sh` | 18 | code-server launch path |
| `docker/agent-vm-base/scripts/provision.sh` | 151-152 | VM setup |
| `src/core/backends/remote.py` | 110 | Default workspace_path |
| `src/api/persistent_session.py` | 203, 219 | Remote backend config |
| `src/api/persistent_app.py` | 1503, 1521 | Workspace polling |
| `orchestrator/main.py` | 698, 726, 971 | Dispatch config injection |
| `orchestrator/main.py` | 9089, 9101 | IDE URL construction |
| `orchestrator/services/container_provisioner.py` | 567 | PVC mount path |
| `orchestrator/services/ide_session.py` | 25, 564, 852 | IDE folder/clone paths |

---

## Desired State

### Principle: The workspace IS the home directory

The agent user's home directory is the workspace. No subdirectories for job isolation — the container/VM provides that. The entry point is always `~` (home dir), never `~/workspace/job_{id}/...`.

This matches how every major CI/CD system works: GitHub Actions (ARC), GitLab CI (K8s executor), Jenkins (K8s plugin), and Tekton all use one fresh ephemeral container per job, destroyed on completion. None of them use persistent subdirectories for isolation.

Notably, Gitpod, Codespaces, and Coder all keep workspace and home directory separate (`/workspace/` vs `/home/`). We diverge from this pattern intentionally — our agent user has no interactive shell config needs, and keeping the home dir clean avoids the path indirection that caused our bugs.

### Workspace Container Layout

```
/home/agent-host/          <-- THIS IS THE WORKSPACE ROOT (= home dir)
├── (agent's files)        <-- workspace.md, plan.md, todos.yaml, repos/, etc.
├── .bashrc                <-- minimal, seeded from /etc/skel.agent-host on first boot
├── .gitconfig             <-- seeded from skeleton
├── .tmux.conf             <-- seeded from skeleton
├── .local/                <-- pip packages (hidden from IDE via files.exclude)
├── .npm-global/           <-- npm packages (hidden)
├── .config/               <-- code-server config (hidden)
└── .cache/                <-- build caches (hidden)
```

### SSH: Keys Outside the Home Directory

Move SSH authorized_keys out of the user's home using the `AuthorizedKeysFile` sshd directive:

```
# /etc/ssh/sshd_config.d/agent.conf
AuthorizedKeysFile /etc/ssh/authorized_keys/%u
```

The `%u` token expands to the username (`agent-host`). sshd's `StrictModes` check requires the file and all parent directories to be owned by root or the target user, with no group/world-write bits. Since the entrypoint runs as root and writes this file, root ownership passes the check trivially.

```bash
# In Dockerfile
RUN mkdir -p /etc/ssh/authorized_keys && chmod 755 /etc/ssh/authorized_keys

# In entrypoint.sh
cp /tmp/ssh-pubkey/ssh-publickey /etc/ssh/authorized_keys/agent-host
chmod 644 /etc/ssh/authorized_keys/agent-host
```

This eliminates `~/.ssh/` from the workspace entirely.

### Dotfile Seeding (PVC/emptyDir Shadows Baked-in Files)

When a volume is mounted at `/home/agent-host`, it shadows everything the Docker image baked into that path. Dotfiles (`.bashrc`, `.gitconfig`, `.tmux.conf`, `.config/`) are gone.

Solution: stage dotfiles to `/etc/skel.agent-host/` in the Dockerfile, seed on first boot via the entrypoint:

```bash
# In Dockerfile (after user setup)
RUN cp -a /home/agent-host /etc/skel.agent-host

# In entrypoint.sh (idempotent)
if [ ! -f /home/agent-host/.workspace-initialized ]; then
    cp -rn /etc/skel.agent-host/. /home/agent-host/
    chown -R agent-host:agent-host /home/agent-host
    touch /home/agent-host/.workspace-initialized
fi
```

### code-server: State Outside Home, Open Home as Workspace

Move code-server's user-data-dir and extensions-dir outside the home directory so they don't appear in the file explorer and aren't affected by volume mounts:

```bash
# In entrypoint.sh
su -c 'code-server \
    --bind-addr 0.0.0.0:8080 \
    --user-data-dir /var/lib/code-server \
    --extensions-dir /var/lib/code-server/extensions \
    /home/agent-host' agent-host &
```

Pre-bake `files.exclude` in the code-server settings to hide remaining dotfiles:

```bash
# In Dockerfile
RUN mkdir -p /var/lib/code-server/User \
    && cat > /var/lib/code-server/User/settings.json <<'EOF'
{
    "files.exclude": {
        "**/.bashrc": true, "**/.bash_logout": true, "**/.bash_history": true,
        "**/.profile": true, "**/.gitconfig": true, "**/.tmux.conf": true,
        "**/.config": true, "**/.local": true, "**/.cache": true,
        "**/.npm-global": true, "**/.workspace-initialized": true
    }
}
EOF
```

### VM Layout

Same principle — the agent account's home directory IS the workspace:
```
/home/agent/               <-- workspace root, same as a human developer
├── (project files)
└── ...
```

### Storage: emptyDir, Not PVC

For ephemeral per-job/session workspaces, use disk-backed `emptyDir` instead of PVC:

| Factor | emptyDir | PVC |
|--------|----------|-----|
| Lifecycle | Dies with pod | Persists, requires deletion |
| Provisioning | Instant | Seconds to minutes (PV binding) |
| Cleanup | Automatic | Requires explicit `delete_workspace_pvc()` |
| Scheduling | No node affinity | RWO PVCs pin pod to a node |

```yaml
volumes:
  - name: workspace-data
    emptyDir:
      sizeLimit: "10Gi"
```

The container provisioner already uses emptyDir as fallback when no PVC is provided (`container_provisioner.py:590-596`). Make it the default.

Reserve PVCs only for persistent threads that must survive pod restarts.

### SSH Host Keys in Ephemeral Containers

Fresh containers regenerate SSH host keys, causing client-side warnings. Since agent-to-workspace communication is intra-cluster over the CNI (trusted network), use:

```python
# In RemoteBackend SSH connection setup
ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
# Already the case — paramiko.AutoAddPolicy is used at remote.py:202
```

No change needed — the agent already accepts any host key.

### Cleanup: Destroy, Don't Clean

- **Workspace containers**: Launch a new container for each job/session. Old container gets deleted (pod + emptyDir dies automatically). No file-level cleanup code at all.
- **VMs**: Terminate and recreate. Or if shared: the orchestrator tells the VM provisioner to reset.
- **Dev mode (local)**: `rm -rf` the workspace dir and recreate. One job at a time.

### Data Retention Before Container Death

When ephemeral containers die, workspace files are gone. Ensure artifacts are extracted before destruction:

1. **Normal completion**: Agent uploads `freeze_data` and completion payload via API. Key workspace files (frozen JSON, output) already sent to orchestrator in the completion request.
2. **Gitea push**: Agent pushes workspace git repo at phase boundaries and completion. Work survives container death.
3. **Nextcloud sync**: Workspace files synced to cloud folder after each turn (already implemented).
4. **SIGTERM handling**: Register a signal handler that checkpoints graph state, pushes to Gitea, and reports `paused` status before exit. Set `terminationGracePeriodSeconds: 120` in pod spec.
5. **Catastrophic failure** (OOM kill, node death): Accept data loss for the current phase. Resume from last checkpoint/Gitea push. Same behavior as today.

### Agent Pod Harness Storage

Checkpoints, logs, and snapshots stay on the agent pod. They are NOT workspace files — they're harness internals:
```
/workspace/checkpoints/job_{id}.db    <-- on agent pod, not workspace container
/workspace/logs/job_{id}.log          <-- on agent pod, not workspace container
/workspace/phase_snapshots/job_{id}/  <-- on agent pod, not workspace container
```
The `job_{id}` in these filenames is fine — it's just naming, not directory structure.

### Orchestrator Access to Workspace Files

The orchestrator does NOT read workspace files via local filesystem in production. Instead:
- Agent pushes results back via API (job completion payload, file uploads)
- Orchestrator reads files via SSH/SFTP to workspace container (if needed, through agent pod proxy)
- `WorkspaceService` with local `job_{id}` paths is dev-only or deprecated

---

## Affected Components

### Must Change

| File | What | Lines |
|------|------|-------|
| `src/core/workspace.py` | Remove `job_{id}` from workspace path | 203 |
| `src/core/backends/remote.py` | Default workspace_path → `/home/agent-host` | 110, 130, 144 |
| `src/api/persistent_session.py` | Remove cleanup code; update defaults | 185-228 |
| `src/agent.py` | Remove `job_{id}` path construction for remote | 1054-1079, 1122-1132 |
| `src/core/phase_snapshot.py` | Update workspace_path ref (no `job_{id}`) | 186-188 |
| `orchestrator/main.py` | Update all `workspace_path` defaults to `/home/agent-host` | 698, 726, 971, 9089, 9101 |
| `orchestrator/services/container_provisioner.py` | Mount PVC at `/home/agent-host` | 567 |
| `orchestrator/services/ide_session.py` | Update folder path to `/home/agent-host` | 25, 564, 603, 852 |
| `docker/Dockerfile.workspace` | SSH config out of home dir; home = workspace | 133-137, 159 |
| `docker/workspace-entrypoint.sh` | code-server path → `/home/agent-host` | 18 |
| `docker/agent-vm-base/scripts/provision.sh` | Workspace path → home dir | 151-152 |

### Should Change (dev-only / cleanup)

| File | What | Lines |
|------|------|-------|
| `orchestrator/services/workspace.py` | Remove `job_{id}` from `_get_job_path()` or deprecate | 74 |
| `orchestrator/main.py` | Frozen file local reads (dead code in K8s) | 4625-4854, 6165-6174 |
| `orchestrator/main.py` | Workspace status endpoint `job_*` glob | 2364-2386 |
| `src/init.py` | `glob("job_*")` patterns | 197, 305 |
| `src/services/workspace_sync.py` | Simplify: workspace root is `./` | all |
| Tests | Update hardcoded `job_*` paths and `/home/agent-host/workspace` refs | various |

### No Change Needed

| Component | Why |
|-----------|-----|
| `src/core/workspace_backend.py` | Abstract interface, path-agnostic |
| `src/tools/` | Use WorkspaceManager, paths are relative |
| `src/graph.py` | Uses workspace_memory/manager, not raw paths |
| `src/core/phase.py` | Writes `output/job_frozen.json` via relative path |
| `src/managers/todo.py` | Archive writes via `workspace.write_file("archive/...")` |
| Checkpoint/log file naming | `job_{id}.db` / `.log` is just naming, stays on agent pod |
| Gitea repo naming | `job-{short_id}` is semantic, not filesystem |
| Gitea branch naming | `subjob/{id}/{config}` is semantic, not filesystem |
| Nextcloud session folders | Already use `sessions/{thread_id[:8]}`, no `job_*` |
| Nextcloud project folders | Named by project name |
| S3 snapshot service | Orthogonal system, no workspace path dependency |
| Cockpit UI | Uses DB fields for cloud folder access |
| Deployment manifests | `/workspace` on agent pod is local scratch, not workspace container |
| Worktree paths | Already use `/home/agent-host/worktrees/`, not workspace dir |

---

## Implementation Status: COMPLETE

All 6 phases have been implemented. **38 files changed, 3878 tests passing, 0 new failures.**

### Phase 1: Container Image & Entrypoint — DONE

| File | Changes |
|------|---------|
| `docker/Dockerfile.workspace` | Removed `mkdir /home/agent-host/workspace` and `~/.ssh` setup. Added `/etc/ssh/authorized_keys/` (root-owned). `AuthorizedKeysFile` → `/etc/ssh/authorized_keys/%u`. Added `/var/lib/code-server/` with `files.exclude` settings. Added dotfile skeleton (`cp -a /home/agent-host /etc/skel.agent-host`). |
| `docker/workspace-entrypoint.sh` | Rewritten: (1) Dotfile seeding from skeleton on first boot. (2) SSH key to `/etc/ssh/authorized_keys/agent-host`. (3) code-server with `--user-data-dir /var/lib/code-server` opening `/home/agent-host`. (4) SSHD foreground. |
| `docker/agent-vm-base/scripts/provision.sh` | Removed `/home/agent-host/workspace` and `~/.ssh`. Added `/etc/ssh/authorized_keys/`. `AuthorizedKeysFile` → `/etc/ssh/authorized_keys/%u`. |

### Phase 2: Core Path Change — DONE

| File | Changes |
|------|---------|
| `src/core/workspace.py` | `self._workspace_path = self._base_path` (was `self._base_path / f"job_{job_id}"`). Updated module docstring. |
| `src/core/backends/remote.py` | Default `workspace_path="/home/agent-host"` (was `/home/agent-host/workspace`). |
| `src/core/phase_snapshot.py` | `self._workspace_path = base_path` (was `base_path / f"job_{job_id}"`). Snapshots dir and checkpoint path on agent pod unchanged. |
| Tests (7 files) | `test_phase_snapshot.py`, `test_workspace_backends.py`, `test_container_provisioner.py`, `test_managers_git.py`, `tests/tools/research/conftest.py`, `tests/tools/research/test_browser_tools.py` — updated path assertions. |

### Phase 3: Agent & Persistent Session — DONE

| File | Changes |
|------|---------|
| `src/agent.py` | Default `workspace_path` → `/home/agent-host`. Worktree parent path → `/home/agent-host`. |
| `src/api/persistent_session.py` | Default `workspace_path` → `/home/agent-host`. Removed `find -mindepth 1 -exec rm -rf` cleanup block (fresh containers). |
| `src/api/persistent_app.py` | All three `workspace_path` defaults → `/home/agent-host` (VM polling, container polling, VM upgrade hot-swap). |

### Phase 4: Orchestrator Path Updates — DONE

| File | Changes |
|------|---------|
| `orchestrator/main.py` | All `workspace_path` defaults → `/home/agent-host`. IDE folder URLs → `/home/agent-host`. Removed `f"job_{job_id}"` from all local workspace reads (6 occurrences). Replaced `job_*` directory listing with flat entries listing in workspace status endpoint. |
| `orchestrator/services/container_provisioner.py` | `mountPath` → `/home/agent-host`. |
| `orchestrator/services/ide_session.py` | All paths → `/home/agent-host` (default folder, git clone targets). |
| `orchestrator/services/workspace.py` | `_get_job_path()` returns `base_path` directly (no `job_{id}` subdirectory). |
| Tests (5 files) | `test_container_provisioner.py`, `test_ide_proxy.py`, `test_thread_endpoints.py`, `test_worktree_sharing.py`, `test_vm_upgrade_endpoint.py` — updated assertions. |

### Phase 5: Switch to emptyDir by Default — DONE

| File | Changes |
|------|---------|
| `orchestrator/services/container_provisioner.py` | `create_workspace()`: removed PVC creation, uses emptyDir. `create_thread_workspace()`: same. Added `terminationGracePeriodSeconds: 120` to pod spec. `delete_workspace_pvc()` / `delete_thread_workspace_pvc()` kept for backward compat with existing PVCs. |
| `tests/test_container_provisioner.py` | Added `test_manifest_termination_grace_period`. |

### Phase 6: Cleanup & Dev Tooling — DONE

| File | Changes |
|------|---------|
| `src/init.py` | Removed `glob("job_*")` patterns. Workspace info reports `entry_count` instead of `job_count`. |
| `config/defaults.yaml` | Updated comment: `workspace_path: /home/agent-host`. |
| `config/schema.json` | Updated default: `/home/agent-host`. |
| `cockpit/src/assets/schema.json` | Synced schema copy. |
| `docker-compose.yaml` | ssh-keygen: added `ssh-publickey` file. Workspace mounts: `ssh_keys:/tmp/ssh-pubkey:ro` (was `/home/agent-host/.ssh:ro`). |
| `docker-compose.local.yaml` | Same compose changes. |
| `docker-compose.dev.yaml` | Same compose changes (bind mount variant). |
| `vm/sudo-daemon/test/mock_plugin.py` | Updated `cwd` in mock data. |
| `docs/browser_use.md` | Browser profile path updated. |
| `docs/deployment_checklist.md` | Workspace description updated. |
| `docs/local_development.md` | Dev workspace path updated. |
| `docs/working_memory.md` | Workspace layout example updated. |
| `docs/features/vm_backend.md` | RemoteBackend paths updated. |
| `docs/features/vm_snapshots_and_ide.md` | code_server_url path updated. |

### What Was NOT Changed (intentional)

| Component | Why |
|-----------|-----|
| Agent pod harness storage (`checkpoints/`, `logs/`, `phase_snapshots/`) | On agent pod, not workspace container. `job_{id}` in filenames is naming, not directory structure. |
| `WORKSPACE_PATH=/workspace` in configmap | Agent pod local scratch, not workspace container path. |
| `src/services/workspace_sync.py` | Already path-agnostic, zero `job_*` references. |
| `docs/done/`, `docs/issues/` | Historical records of past decisions. |
| Gitea repo/branch naming | Semantic names, not filesystem paths. |
| Nextcloud session folders | Already use `sessions/{thread_id[:8]}`. |
| `delete_workspace_pvc()` / `delete_thread_workspace_pvc()` methods | Kept for backward compat with existing PVCs from before the emptyDir switch. |

### Verification

- **Test suite**: 3878 passed, 40 skipped, 0 new failures (23 pre-existing in `test_loader_interactive.py` and `test_persistent_graph.py`).
- **Lint**: `ruff check src/ orchestrator/ tests/` — all checks passed (2 pre-existing warnings in `orchestrator/main.py`).
- **Codebase sweep**: Zero remaining `/home/agent-host/workspace` references in active code or docs. Zero remaining `job_{id}` workspace directory patterns (only filename patterns on agent pod).

### Remaining Manual Testing

Before deploying to production:

1. **Docker build**: `docker build -f docker/Dockerfile.workspace .` — verify image builds successfully.
2. **SSH test**: Run the container, SSH in as `agent-host`, verify home dir is clean workspace root.
3. **code-server test**: Open code-server, verify it shows `/home/agent-host` contents with dotfiles hidden.
4. **Docker Compose**: `podman-compose up -d` — verify workspace containers accept SSH with new key mount path.
5. **Dev mode**: `python agent.py --dev --description "test"` — verify workspace files created at `./workspace/` directly.
6. **K8s deploy**: Fleet sync, verify workspace pods start with emptyDir, code-server URL works, sessions function end-to-end.
