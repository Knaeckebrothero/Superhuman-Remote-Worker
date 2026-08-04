# Ephemeral Workspace Lifecycle

**Status:** Historical implementation record; static Docker reuse design superseded
**Last updated:** 2026-07-13

> **Static Docker safety correction:** Production/default Compose workspaces are
> durable-inventory endpoints, not disposable pool slots. A fresh endpoint enters
> the released pool only through the one-time
> `DOCKER_WORKSPACE_BOOTSTRAP_ATTESTED_ENDPOINTS` import with an exact
> `WORKSPACE_HOST_KEY_FINGERPRINTS` match. Operators must first recreate the
> container **and** replace or independently sanitize persistent
> `/home/agent-host` data, then remove the bootstrap variable immediately after a
> successful import. Release snapshots and quarantines the endpoint; only explicit
> recreation/data reset and fresh attestation may return it to production use.
> Container restart and SSH deletion are not tenant resets.
>
> `DOCKER_WORKSPACE_TRUSTED_DEV_REUSE=true` retains pinned-SSH cleanup only for
> disposable single-user/same-trust development. It is never production
> attestation. The no-fingerprint dev seed can receive one initial lease, but
> release cannot make an unpinned SSH connection and therefore quarantines it.
> Configure exact fingerprints for repeated dev reuse. Workspace containers listen
> on `30022` internally; the dev Compose file publishes those endpoints as
> `localhost:2201`, `localhost:2202`, and `localhost:2203`.

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
Environment retired (container deleted / VM destroyed / static Docker host quarantined)
    │
    ▼
[Next assignment uses a fresh or explicitly recreated-and-attested environment]
```

### Resume from checkpoint

Already partially implemented at `main.py:4485-4505`:

1. Orchestrator provisions a fresh workspace (container or VM)
2. Calls `ide_session_service.restore_snapshot_for_resume()` — downloads tarball from S3 and extracts via SSH
3. Agent connects, resumes from checkpoint

### Current state of each provisioner

| Provisioner | Creates | Deletes | Snapshots before delete | Resets between jobs |
|---|---|---|---|---|
| **ContainerProvisioner** (K8s) | Per-owner pods (job **and** session), `emptyDir` by default; PVC under `workspace.pvcEnabled` | `delete_workspace()` | Only for VMs at `main.py:6061` | N/A on emptyDir; PVC-backed workspaces **reattach** instead of resetting |
| **DockerProvisioner** (compose) | Durable host-inventory lease | Snapshot + quarantine | Yes (calls SnapshotService) | Only after explicit recreation/data reset/attestation; same-trust dev has an opt-in exception |
| **VMProvisioner** (KubeVirt/NATS/Docker) | Per-job VMs | `delete_vm()` | Only at job completion, **not** threads | N/A (VM destroyed) |
| **WorkspaceSuspensionService** (K8s) | Restore from snapshot | Suspend to S3 | Yes (full pattern) | N/A (pod recreated) |


## Behavior by Deployment Method

The orchestrator auto-selects provisioners based on available infrastructure (`main.py:1289-1596`): K8s in-cluster first, then Docker Compose fallback (detected via `.dev/ssh-keys` on disk).

### Kubernetes

| Aspect | Containers (ContainerProvisioner) | VMs (VMProvisioner, direct K8s) |
|--------|-----------------------------------|----------------------------------|
| **Provisioning** | Per-owner pod (job or session), `emptyDir` volume (10Gi limit); PVC named by owner UUID under `workspace.pvcEnabled` | Per-job KubeVirt VM from template |
| **Isolation** | Pod boundary. Storage dies with pod — **unless PVC-backed**, in which case it survives and reattaches by name. | Full VM boundary. Disk via CDI DataVolume. |
| **Current cleanup** | `delete_workspace()` deletes pod. No snapshot. | `delete_vm()` destroys VM. Snapshot only at job completion, not thread VMs. |
| **Resume** | New pod + `restore_snapshot_for_resume()` from S3 | New VM + same restore path |
| **What's needed** | Snapshot before pod deletion (Phase 2b) | `release_vm()` with snapshot (Phase 2a) |
| **WorkspaceSuspensionService** | Already implements full suspend/restore cycle | Not used for VMs |

K8s containers are already ephemeral (emptyDir). The only gap is snapshotting before deletion for resume support.

> **2026-08-04 — no longer unconditional.** `workspace.pvcEnabled` PVC-backs
> workspace pods for **both** jobs and sessions (a session additionally gets a
> claim for its agent pod). Where that flag is on, the primary resume path is
> **PVC reattach by name**, and the S3 snapshot demotes to a cross-node/DR
> backstop rather than the mechanism. Job claims are reclaimed at terminal
> status; session claims only when the thread row is hard-deleted. emptyDir
> remains the chart default and the rollback posture, and a mixed fleet is safe —
> the reaper reads each pod's actual volume mode.
> See [`workspace_pvc_branch_a_implementation.md`](workspace_pvc_branch_a_implementation.md).

### Docker Compose

| Aspect | Containers (DockerProvisioner) | VMs (DockerProvisioner, QEMU-in-Docker) |
|--------|-------------------------------|----------------------------------------|
| **Provisioning** | Static endpoints from `WORKSPACE_HOSTS`; production availability is tracked in durable host-keyed inventory and owner rows carry concrete lease IDs. Full-stack endpoints use `workspace-N:30022`; dev-published endpoints use `localhost:2201`-`2203`. | Static pool from `VM_HOSTS`. Assignment tracked in DB. |
| **Isolation** | Container state survives restart; persistent `/home/agent-host` data may also survive recreation when backed by a volume. Both are part of the reset boundary. | QEMU disk image. Persists until container recreation. |
| **Current cleanup** | `release_workspace()` snapshots, transitions the lease with compare-and-swap, and quarantines it by default. | `assign_vm()` / pool tracking only. No release method. |
| **Resume** | Restore only into a fresh/attested production endpoint, or an explicitly same-trust development endpoint. | Re-assign same or different VM + restore |
| **Production reuse** | Explicitly recreate the container, replace or independently sanitize persistent workspace data, and attest the resulting endpoint. No automatic controller exists yet. | Add `release_vm()` with snapshot (Phase 2a) |
| **No Docker socket** | The orchestrator cannot recreate containers. SSH cleanup is available only in opt-in trusted-dev mode and is not a security reset. | Same constraint. |

Docker Compose endpoints are long-lived and cannot be proven clean by restart or file
deletion. The production solution is to keep endpoint state independent of owner rows
and quarantine after every use, rather than advertise a best-effort cleanup as a reset.

### Auto-detection (`docker_provisioner.py:89-118`)

In dev mode, if `WORKSPACE_HOSTS` is not set but `.dev/ssh-keys/id_ed25519` exists on disk, the DockerProvisioner auto-applies dev defaults:
- `WORKSPACE_HOSTS=localhost:2201,localhost:2202,localhost:2203`
- `WORKSPACE_IDE_HOSTS=localhost:18081,localhost:18082,localhost:18083`
- `SSH_KEY_PATH=.dev/ssh-keys/id_ed25519`

This detection removes the need to export connection coordinates for an initial dev
lease; it does **not** opt the endpoint into reusable cleanup. Repeated same-trust dev
reuse requires both `DOCKER_WORKSPACE_TRUSTED_DEV_REUSE=true` and exact
`WORKSPACE_HOST_KEY_FINGERPRINTS` entries for the published endpoints. Without the
pins, release fails closed and quarantines the initial lease.


## Implementation Status

The five historical phases were implemented, but the static Docker portions of
Phases 3 and 5 were later restricted or disabled by the safety correction above.
Changes are summarized below with references to modified files.

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

### Phase 3: Docker Compose SSH workspace reset -- SUPERSEDED

**`orchestrator/services/docker_provisioner.py`:**

The original Phase 3 added `_reset_workspace_via_ssh(host, port)` and treated the
result as a generally reusable workspace. That cross-tenant claim is superseded.
The helper is now reachable only when
`DOCKER_WORKSPACE_TRUSTED_DEV_REUSE=true`, for same-trust development, and it
requires pinned SSH. Its historical cleanup command is retained here for context:

```
rm -rf ~/workspace/* ~/workspace/.[!.]* 2>/dev/null; mkdir -p ~/workspace
```

Missing keys, missing fingerprints, timeouts, SSH errors, and cleanup failures all
quarantine the endpoint. Production/default release does not run this command: it
snapshots and quarantines because processes, open file descriptors, system changes,
and persistent data outside the glob may survive. An unpinned automatic dev seed can
be assigned once, but cannot be cleaned for reuse.

### Phase 4: Snapshot tar excludes -- DONE

**`orchestrator/services/snapshot_service.py:285-296`:**

Added exclude patterns to reduce snapshot size by skipping regenerated content:
- `*/repos/*` — repository datasources re-cloned from remotes on workspace init
- `*/node_modules/*` — reinstalled from package.json on restore

### Phase 5: Generalized WorkspaceSuspensionService -- PARTIALLY SUPERSEDED

**`orchestrator/services/workspace_suspension.py`:**

Extended (not replaced) the existing service to be provisioner-aware:

- `connect()` now accepts `docker_provisioner` and `vm_provisioner` (backward compatible)
- `is_enabled` returns true if S3 + any provisioner is available (not just K8s)
- **`suspend_workspace()`** historically dispatched based on workspace metadata:
  - K8s container: snapshot → delete pod
  - Docker Compose: snapshot → SSH reset workspace (superseded; production/default
    static Docker suspension is disabled and release quarantines instead)
  - VM: snapshot → delete VM
- **`restore_workspace()`** historically dispatched based on workspace metadata:
  - K8s container: create pod → extract snapshot
  - Docker Compose: re-assign slot if needed → extract snapshot (superseded for
    production static Docker until explicit recreation and attestation exists)
  - VM: create VM → extract snapshot
- Thread variants generalized identically
- Idle sweepers (`check_idle_all`, `check_idle_threads`) now also detect idle VMs via `context->'vm'->>'status' = 'ready'`
- `main.py` wires `docker_provisioner` and `vm_provisioner` into the connect call


## Architecture Summary

The diagram below records the original generalized-cleanup design. Its Docker
`SSH reset` branch is superseded: production/default Docker release is now
`snapshot -> durable lease transition -> quarantine`, and restore requires a
separately recreated/data-reset/attested endpoint. The K8s and VM branches remain
historical implementation context.

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
                     Delete pod   QUARANTINE  Delete VM
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
| **3** | Docker Compose SSH-based workspace reset | Superseded; trusted-dev-only behind explicit opt-in and fingerprint pinning | `orchestrator/services/docker_provisioner.py` |
| **4** | Snapshot tar exclude tuning | Done | `orchestrator/services/snapshot_service.py` |
| **5** | Generalized WorkspaceSuspensionService | Partially superseded; static Docker suspend/restore disabled pending recreation + attestation | `orchestrator/services/workspace_suspension.py`, `orchestrator/main.py`, `tests/test_workspace_suspension.py` |
