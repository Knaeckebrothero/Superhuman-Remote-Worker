# Ephemeral Workspace Lifecycle

## Problem

Workspace environments (containers and VMs) accumulate state across jobs and sessions. Two bugs compound the issue:

### 1. No cleanup on session init (without git remote)

`src/core/workspace.py:276-307` has two initialization paths:

- **Path 1** (`git_remote_url` set): Runs `rm -rf workspace/*` before cloning — clean slate.
- **Path 2** (no `git_remote_url`): Only runs `mkdir -p` — stale files from previous sessions survive.

Persistent threads typically take Path 2, so repos, output files, and other artifacts from prior sessions bleed into new ones.

### 2. Datasource resolution returns unlinked non-global datasources

`orchestrator/database/postgres.py:2624-2645` resolves datasources for threads with:

```sql
WHERE d.id = ANY($1::uuid[])          -- explicitly attached
   OR pd.project_id IS NOT NULL        -- linked to thread's projects
   OR NOT EXISTS (                     -- !! catches ALL unlinked datasources
       SELECT 1 FROM project_datasources pd2
       WHERE pd2.datasource_id = d.id
   )
```

The `NOT EXISTS` clause returns every datasource not linked to any project, not just `is_global = true` ones. This causes repository datasources to be cloned into every session regardless of user selection.

### Root cause

Both bugs stem from the assumption that workspace environments start clean. The agent process itself follows an ephemeral lifecycle (terminate after job, fresh instance for next assignment), but workspace environments don't — containers are long-lived with persistent volumes, and cleanup between sessions is incomplete.


## Existing Infrastructure

Before proposing changes, here's what already exists. The codebase is closer to full ephemeral lifecycle than it first appears.

### SnapshotService (`orchestrator/services/snapshot_service.py`)

Fully implemented S3-backed snapshot system, already wired into the orchestrator:

- **Initialized** at `main.py:2122` via `snapshot_service.connect(db=postgres_db)`
- **Passed to** `docker_provisioner`, `ide_session_service`, `workspace_suspension_service`
- **Capture**: `capture_vm_snapshot()` — SSHs into environment, runs `tar | zstd`, uploads to S3 with manifest
- **Download**: `download_snapshot()` — downloads tarball to local file
- **GC**: `run_gc()` — retention-based cleanup with pin exemptions
- **S3 layout**: `s3://srw-snapshots/{entity_type}/{uuid}/env.tar.zst` + `manifest.json`, optional `phases/phase_N/`
- **Dev config**: MinIO on port 9000, env vars `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`

### WorkspaceSuspensionService (`orchestrator/services/workspace_suspension.py`)

**Already implements the full ephemeral pattern for K8s workspaces:**
- **Suspend** (lines 74-143): Snapshot workspace via SSH → delete pod
- **Restore** (lines 148-204): Create new pod → extract snapshot via SSH

This is the reference implementation for what we want to generalize.

### IDE Session Restore (`orchestrator/services/ide_session.py`)

- `_extract_snapshot_to_vm()` (lines 782-836): Downloads tarball from S3, SSHs into target, pipes `zstd -d | tar -xf - -C /`
- `restore_snapshot_for_resume()` (lines 902-935): Wrapper used by job resume flow

### Existing Snapshot Triggers in `orchestrator/main.py`

| Trigger | Location | Condition | Method |
|---------|----------|-----------|--------|
| Job completion | `main.py:6038-6073` | Status in (completed, failed), VM exists | `snapshot_service.capture_vm_snapshot()` |
| Thread deletion | `main.py:8940-8949` | Snapshot available, workspace ready | `snapshot_service.capture_vm_snapshot()` |
| Docker workspace release | `docker_provisioner.py:238-259` | Snapshot service available | `snapshot_service.capture_vm_snapshot()` |

### Existing Cleanup Calls in `orchestrator/main.py`

**Jobs:**

| Location | Trigger | Actions |
|----------|---------|---------|
| `main.py:6075-6101` | Job completion (completed/failed) | VM: `delete_vm()`. Docker: `release_workspace()`. K8s: `delete_workspace()` + `delete_workspace_pvc()` |
| `main.py:3046-3060` | Job cancellation | Same as completion cleanup |
| `main.py:2898-2913` | Child job cascade cleanup | Same as completion cleanup |

**Threads:**

| Location | Trigger | Actions |
|----------|---------|---------|
| `main.py:8951-8969` | Thread deletion | Docker: `release_thread_workspace()`. K8s: `delete_thread_workspace()` + PVC. Agent pod: `delete_agent_pod_by_thread()`. VM: `delete_thread_vm()` |

### Job Resume with Snapshot Restore (`main.py:4485-4505`)

Already exists: when resuming a job, the orchestrator calls `ide_session_service.restore_snapshot_for_resume()` to extract the S3 snapshot into the new workspace via SSH.

### Agent-Side Lifecycle

- **No workspace archival at completion**: Agent calls `POST /api/jobs/{id}/complete` then terminates. Workspace archival is orchestrator's responsibility.
- **Persistent session detach** (`persistent_app.py:565-624`): Git commit+push, `session.cleanup()` (disconnects SSH). No S3 archival.
- **Resume expects workspace to exist**: Checkpoint DB at `workspace/checkpoints/job_{id}.db` must be accessible. Phase snapshots stored pod-locally at `workspace/phase_snapshots/`.


## Proposed Solution: Ephemeral Workspaces (Containers + VMs)

Mirror the agent lifecycle: workspace environments are disposable. After a job or session completes, the environment archives its state to S3, then terminates. A new fresh environment is started for the next assignment. This applies uniformly to containers and VMs.

### Lifecycle (shared by containers and VMs)

```
[New Job/Session]
    │
    ▼
Orchestrator provisions fresh workspace (container or VM)
    │
    ▼
Agent connects via SSH, initializes workspace
    │
    ▼
Job/session runs (workspace accumulates state)
    │
    ▼
Job/session completes or is paused
    │
    ├──► Snapshot workspace to S3 (SnapshotService.capture_vm_snapshot)
    │       - tar+zstd via SSH, upload to s3://{entity_type}/{uuid}/
    │       - already implemented, just needs consistent invocation
    │
    ▼
Environment terminated (container deleted / VM destroyed / pool slot reset)
    │
    ▼
[Ready for next assignment: fresh environment]
```

### Resume from checkpoint

Already partially implemented at `main.py:4485-4505`:

1. Orchestrator provisions a fresh workspace (container or VM)
2. Calls `ide_session_service.restore_snapshot_for_resume()` — downloads tarball from S3 and extracts via SSH
3. Agent connects, resumes from checkpoint

### Current state of each provisioner

| Provisioner | Creates | Deletes | Snapshots before delete | Resets between jobs |
|---|---|---|---|---|
| **ContainerProvisioner** (K8s) | Per-job pods, `emptyDir` | `delete_workspace()` | Only for VMs at `main.py:6061` | N/A (ephemeral volume) |
| **DockerProvisioner** (compose) | Static pool assignment | `release_workspace()` | Yes (calls SnapshotService) | No (documented gap) |
| **VMProvisioner** (KubeVirt/NATS/Docker) | Per-job VMs | `delete_vm()` | Only at job completion, **not** threads | N/A (VM destroyed) |
| **WorkspaceSuspensionService** (K8s) | Restore from snapshot | Suspend to S3 | Yes (full pattern) | N/A (pod recreated) |


## Behavior by Deployment Method

The orchestrator auto-selects provisioners based on available infrastructure (`main.py:1289-1596`): K8s in-cluster first, then Docker Compose fallback (detected via `.dev/ssh-keys` on disk).

### Kubernetes

| Aspect | Containers (ContainerProvisioner) | VMs (VMProvisioner, direct K8s) |
|--------|-----------------------------------|----------------------------------|
| **Provisioning** | Per-job pod, `emptyDir` volume (10Gi limit) | Per-job KubeVirt VM from template |
| **Isolation** | Pod boundary. Storage dies with pod. | Full VM boundary. Disk via CDI DataVolume. |
| **Current cleanup** | `delete_workspace()` deletes pod. No snapshot. | `delete_vm()` destroys VM. Snapshot only at job completion, not thread VMs. |
| **Resume** | New pod + `restore_snapshot_for_resume()` from S3 | New VM + same restore path |
| **What's needed** | Snapshot before pod deletion (Phase 2b) | `release_vm()` with snapshot (Phase 2a) |
| **WorkspaceSuspensionService** | Already implements full suspend/restore cycle | Not used for VMs |

K8s containers are already ephemeral (emptyDir). The only gap is snapshotting before deletion for resume support.

### Docker Compose

| Aspect | Containers (DockerProvisioner) | VMs (DockerProvisioner, QEMU-in-Docker) |
|--------|-------------------------------|----------------------------------------|
| **Provisioning** | Static pool from `WORKSPACE_HOSTS` (e.g. `localhost:2201,2202,2203`). Assignment tracked in DB. | Static pool from `VM_HOSTS`. Assignment tracked in DB. |
| **Isolation** | Container-local storage (no named volumes for workspace data). Persists across container restarts but not recreation. | QEMU disk image. Persists until container recreation. |
| **Current cleanup** | `release_workspace()` snapshots to S3, marks slot as released. **Does not reset workspace directory.** | `assign_vm()` / pool tracking only. No release method. |
| **Resume** | Re-assign same or different container + `restore_snapshot_for_resume()` | Re-assign same or different VM + restore |
| **What's needed** | SSH-based workspace reset after snapshot (Phase 3) | Add `release_vm()` with snapshot (Phase 2a) |
| **No Docker socket** | Orchestrator cannot restart containers. SSH cleanup only. | Same constraint. |

Docker Compose is the problematic deployment: containers are long-lived, the orchestrator can't restart them, and `release_workspace()` doesn't reset the filesystem. This is the source of the stale workspace bug.

### Auto-detection (`docker_provisioner.py:89-118`)

In dev mode, if `WORKSPACE_HOSTS` is not set but `.dev/ssh-keys/id_ed25519` exists on disk, the DockerProvisioner auto-applies dev defaults:
- `WORKSPACE_HOSTS=localhost:2201,localhost:2202,localhost:2203`
- `WORKSPACE_IDE_HOSTS=localhost:18081,localhost:18082,localhost:18083`
- `SSH_KEY_PATH=.dev/ssh-keys/id_ed25519`


## Implementation Status

All five phases have been implemented. Changes summarized below with references to modified files.

### Phase 1: Immediate bug fixes -- DONE

**1a. `src/core/workspace.py:298-306` — Remote backend cleanup added to Path 2:**

The no-git-remote initialization path now clears stale files on remote backends (SSH workspace containers) before creating directories, matching the behavior of Path 1.

**1b. `orchestrator/database/postgres.py:2637-2643` — Datasource SQL scoped to globals:**

The `NOT EXISTS` clause now requires `d.is_global = true`, preventing non-global unlinked datasources (like repository datasources) from being returned for every session.

### Phase 2: Consistent snapshot-before-delete -- DONE

**2a. VMProvisioner (`orchestrator/services/vm_provisioner.py`):**
- `connect()` accepts `snapshot_service`
- `release_vm(job_id)`: resolves SSH coordinates from DB, snapshots to S3, then calls `delete_vm()`
- `release_thread_vm(thread_id)`: same pattern for threads with `entity_type="threads"`
- Snapshot failures are non-fatal — VM is still deleted

**2b. ContainerProvisioner (`orchestrator/services/container_provisioner.py`):**
- `connect()` accepts `snapshot_service`
- `release_workspace(job_id)`: reads pod IP via `get_workspace_status()`, snapshots, then deletes pod + PVC
- `release_thread_workspace(thread_id)`: reads pod IP directly from K8s API, snapshots, then deletes pod + PVC

**2c. Centralized cleanup (`orchestrator/main.py`):**

Added `_archive_and_cleanup_workspace(entity_id, entity_type)` helper that:
1. Reads workspace metadata from DB to detect provisioner type
2. Dispatches to the correct provisioner's `release_*` method (which handles snapshot internally)
3. Returns a list of action descriptions for logging

Replaced all 4 scattered cleanup blocks:
- `complete_job()` — job completion/failure
- `cancel_job()` — job cancellation
- `_cleanup_child()` — cascade cancel cleanup
- `end_thread()` — thread deletion

`snapshot_service.connect()` moved before provisioner inits so it can be passed to all three provisioners.

### Phase 3: Docker Compose workspace reset -- DONE

**`orchestrator/services/docker_provisioner.py`:**

Added `_reset_workspace_via_ssh(host, port)` that SSHs into the workspace container and runs:
```
rm -rf ~/workspace/* ~/workspace/.[!.]* 2>/dev/null; mkdir -p ~/workspace
```

Handles: missing `SSH_KEY_PATH`, connection timeouts (30s), SSH errors. Called from both `release_workspace()` and `release_thread_workspace()` after snapshot and before marking the slot as released. This closes the gap where Docker Compose containers were snapshotted but never cleaned.

### Phase 4: Snapshot tar excludes -- DONE

**`orchestrator/services/snapshot_service.py:285-296`:**

Added exclude patterns to reduce snapshot size by skipping regenerated content:
- `*/repos/*` — repository datasources re-cloned from remotes on workspace init
- `*/node_modules/*` — reinstalled from package.json on restore

### Phase 5: Generalized WorkspaceSuspensionService -- DONE

**`orchestrator/services/workspace_suspension.py`:**

Extended (not replaced) the existing service to be provisioner-aware:

- `connect()` now accepts `docker_provisioner` and `vm_provisioner` (backward compatible)
- `is_enabled` returns true if S3 + any provisioner is available (not just K8s)
- **`suspend_workspace()`** dispatches based on workspace metadata:
  - K8s container: snapshot → delete pod
  - Docker Compose: snapshot → SSH reset workspace (container stays alive)
  - VM: snapshot → delete VM
- **`restore_workspace()`** dispatches based on workspace metadata:
  - K8s container: create pod → extract snapshot
  - Docker Compose: re-assign slot if needed → extract snapshot (container already running)
  - VM: create VM → extract snapshot
- Thread variants generalized identically
- Idle sweepers (`check_idle_all`, `check_idle_threads`) now also detect idle VMs via `context->'vm'->>'status' = 'ready'`
- `main.py` wires `docker_provisioner` and `vm_provisioner` into the connect call


## Architecture Summary

```
                     main.py completion/cancel/thread-end handlers
                                      │
                                      ▼
                       _archive_and_cleanup_workspace()
                                      │
                          ┌───────────┼───────────┐
                          ▼           ▼           ▼
                   ContainerProv  DockerProv   VMProvisioner
                   (K8s pods)    (compose)    (KubeVirt/NATS)
                          │           │           │
                     Snapshot ───► SnapshotService ◄─── Snapshot
                          │      (capture + upload)      │
                          ▼           │           ▼
                     Delete pod   SSH reset   Delete VM
                                      │
                                      ▼
                              s3://srw-snapshots/
                              {entity}/{uuid}/
                              ├── manifest.json
                              └── env.tar.zst

                    ─── Resume Path ───

                       Orchestrator dispatch
                              │
                              ▼
                     Provision fresh workspace
                              │
                              ▼
                  ide_session_service.restore_snapshot_for_resume()
                              │
                              ▼
                     Download from S3 → SSH extract
                              │
                              ▼
                        Agent connects
```


## Roadmap

| Phase | Scope | Status | Files Modified |
|-------|-------|--------|----------------|
| **1** | Bug fixes (workspace cleanup + datasource SQL) | Done | `src/core/workspace.py`, `orchestrator/database/postgres.py` |
| **2** | Consistent snapshot-before-delete, centralized cleanup helper | Done | `orchestrator/services/vm_provisioner.py`, `orchestrator/services/container_provisioner.py`, `orchestrator/main.py` |
| **3** | Docker Compose SSH-based workspace reset | Done | `orchestrator/services/docker_provisioner.py` |
| **4** | Snapshot tar exclude tuning | Done | `orchestrator/services/snapshot_service.py` |
| **5** | Generalized WorkspaceSuspensionService | Done | `orchestrator/services/workspace_suspension.py`, `orchestrator/main.py`, `tests/test_workspace_suspension.py` |
