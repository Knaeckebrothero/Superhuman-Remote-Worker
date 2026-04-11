---
tags:
  - agent-architecture
  - vm-lifecycle
  - object-storage
  - ide
  - persistence
---

# VM Snapshots & On-Demand IDE Sessions

Design document for persisting agent environments to S3-compatible object storage and restoring them on demand — enabling Web IDE access for any job regardless of current VM state.

## Motivation

Today, agent VMs are **ephemeral**: created per job, destroyed on completion. This means:

1. **No IDE for completed jobs.** The VM is gone — its installed packages, build artifacts, custom configs, and terminal history are lost. Only the workspace files survive (in Gitea via git versioning).
2. **No IDE for non-VM jobs.** Agents that ran as cluster pods (without a VM) never had a code-server to begin with.
3. **Environment > Code.** A Gitea clone gives you the workspace files, but the agent's *working environment* — pip packages, compiled binaries, node_modules, system configs — is often more valuable for understanding and reproducing what the agent did.

The goal: **click "IDE" on any job, any time, and get back the full environment the agent had.**

### Use Cases

| Scenario | Today | With this feature |
|----------|-------|-------------------|
| Completed VM job (1 week old) | Gitea browse only | Restore VM from S3 snapshot, code-server in ~30s |
| Running VM job | Code-server on live VM | Same (no change) |
| Cluster pod job (no VM) | Gitea browse only | Snapshot pod FS on completion, restore into VM for IDE |
| Failed job debugging | Read logs, workspace.md | Restore exact environment, reproduce the failure |
| Job resume after cluster maintenance | Fresh VM, checkpoint replay | Restore snapshotted VM, resume with full env state |

## Architecture Overview

```
                                  S3-Compatible Object Store
                                  (MinIO on SSD, cluster-local)
                                  ┌───────────────────────────┐
                                  │  jobs/                    │
                                  │  └── <job_uuid>/          │
   Job completes / VM teardown    │      ├── manifest.json    │
   ────────────────────────────►  │      └── env.tar.zst      │
   (snapshot & upload)            │                           │
                                  └─────────────┬─────────────┘
                                                │
                                                │ User clicks "IDE"
                                                │ (restore & boot)
                                                ▼
                                  ┌───────────────────────────┐
                                  │  Restored VM              │
                                  │  ├── code-server :8080    │
                                  │  ├── full agent env       │
                                  │  └── workspace files      │
                                  └───────────────────────────┘
```

### Components

| Component | Role |
|-----------|------|
| **Snapshot Service** (`orchestrator/services/snapshot_service.py`) | Orchestrates capture, upload, restore, and TTL cleanup |
| **S3 Client** | Boto3 for S3-compatible object store (MinIO, AWS S3, etc.) |
| **VM Provisioner** (existing) | Extended: `create_vm_from_snapshot()` alongside existing `create_vm()` |
| **VM Controller** (existing) | Extended: snapshot capture handler (tar + upload from agent node) |
| **Management Daemon** (existing) | Reports code-server activity via existing heartbeat mechanism |
| **Orchestrator API** | New `/api/jobs/{job_id}/ide` and `/api/jobs/{job_id}/snapshot` endpoints |
| **Cockpit** | IDE button upgraded from static link to session-aware |

## S3 Object Layout

```
s3://srw-snapshots/
├── jobs/
│   └── <job_uuid>/
│       ├── manifest.json       # Latest snapshot metadata + restore instructions
│       ├── env.tar.zst         # Latest environment tarball (always points to newest phase)
│       └── phases/
│           ├── phase_1/
│           │   ├── manifest.json
│           │   └── env.tar.zst
│           ├── phase_2/
│           │   ├── manifest.json
│           │   └── env.tar.zst
│           └── ...
├── bases/
│   └── agent-vm-base-<hash>/   # Base image layers (future: dedup)
└── gc/
    └── pending_delete/         # Soft-delete staging area (7-day grace period)
```

The top-level `manifest.json` and `env.tar.zst` are always copies of the latest phase snapshot. This allows the restore flow to use a single path regardless of how many phases were captured. Per-phase snapshots enable restoring to a specific phase for debugging.

### Manifest Schema

```json
{
  "version": 1,
  "job_id": "abc123-...",
  "source_type": "vm",
  "created_at": "2026-03-17T14:30:00Z",
  "agent_config": "developer",
  "base_image": "agent-vm-base-v2.1",
  "compression": "zstd",
  "size_bytes": 2147483648,
  "size_compressed_bytes": 856432100,
  "checksum_sha256": "a1b2c3...",
  "capture_method": "ssh_tar",
  "captured_paths": [
    "/home/agent-host/",
    "/usr/local/",
    "/etc/custom/"
  ],
  "environment": {
    "os": "ubuntu-24.04",
    "python_version": "3.12.3",
    "node_version": "22.x",
    "dpkg_selections": "base64-encoded gzip of dpkg --get-selections",
    "pip_freeze": ["numpy==1.26.4", "pandas==2.2.1"],
    "npm_global": ["typescript@5.4.2"],
    "custom_services": ["postgresql-16"]
  },
  "workspace": {
    "git_remote": "http://gitea:3000/srw/job-abc123",
    "branch": "main",
    "last_commit": "a1b2c3d"
  },
  "restore": {
    "min_cpu": 2,
    "min_memory": "4Gi",
    "disk_size": "20G",
    "estimated_boot_seconds": 25
  }
}
```

**`source_type`**: `"vm"` (full VM environment captured) or `"pod"` (workspace + package manifests only).

**`capture_method`**: `"ssh_tar"` (MVP — tar over SSH) or `"kubevirt_snapshot"` (future — native KubeVirt CRD).

## Snapshot Lifecycle

### 1. Capture (Job Completion / VM Teardown)

**Today's teardown flow:**

```
Job completes → orchestrator calls delete_vm() → VM destroyed
```

**New flow:**

```
Job completes (or fails/is cancelled with snapshot.on_failure=true)
│
├── VM job:
│   1. Freeze VM (NATS: agent.vm.{id}.control:freeze)
│   2. SSH into frozen VM, tar key directories (see "What gets captured")
│   3. Stream compress with zstd, pipe to S3 multipart upload
│   4. Write manifest.json to S3
│   5. Update jobs.context.snapshot = { status: "available", ... }
│   6. Delete VM (existing flow)
│
└── Pod job (no VM):
    1. Before pod termination: capture pip freeze, npm list, dpkg selections
    2. Tar workspace/job_<uuid>/ + package manifests
    3. Compress with zstd, upload to S3
    4. Write manifest.json to S3
    5. Update jobs.context.snapshot = { status: "available", ... }
```

**Failure handling:** If snapshot capture fails (network error, S3 unreachable, timeout), log the error and proceed with VM deletion. The snapshot is a convenience feature — it must not block teardown. Set `jobs.context.snapshot.status = "capture_failed"` with the error message so the cockpit can show why IDE is unavailable.

**What triggers a snapshot:**

| Event | Snapshot captured? |
|-------|-------------------|
| Phase boundary (strategic → tactical or vice versa) | Yes — incremental snapshot, stored per-phase in S3 |
| Completed successfully | Yes (always, final snapshot) |
| Failed | Yes, if `snapshot.on_failure: true` (default: true) |
| Cancelled by user | Configurable: `snapshot.on_cancel: false` (default: false) |
| VM crashed (workspace_unavailable) | No — VM is already gone |

#### VM Disk Export Options

| Method | Pros | Cons |
|--------|------|------|
| SSH tar of key directories | Simple, no hypervisor access, works with containerDisk | Misses disk-level state (running services, open files) |
| `qemu-img convert` | Full disk, bit-perfect | Requires VM stopped, hypervisor access on node |
| KubeVirt `VirtualMachineSnapshot` CRD | Native K8s, supports live snapshots | Requires PVCs (not containerDisk) |
| `virsh snapshot-create-as` | Fast, external snapshots | Requires libvirt access on node |

**MVP recommendation:** SSH tar. It avoids hypervisor-level access, works with ephemeral containerDisk VMs, and captures what matters most (user-space changes). Full disk snapshots can come later when migrating to persistent volumes (Phase 5).

#### What Gets Captured (VM Jobs)

**Included:**
- `/home/agent-host/` — workspace, tools, user configs, dotfiles
- `/usr/local/` — pip packages, custom-compiled binaries
- `/etc/` modifications — diffed against base image to capture only changes
- `/var/lib/` selective — databases the agent installed (e.g. PostgreSQL data dir)
- Package manifests: `dpkg --get-selections`, `pip freeze`, `npm list -g`

**Excluded:**
- `/proc/`, `/sys/`, `/dev/` — virtual filesystems
- `/var/cache/` — apt/pip cache (reproducible from package manifests)
- `/tmp/` — transient data
- `*.pyc`, `__pycache__/`, `node_modules/.cache/` — regeneratable

**Estimated sizes:** 500 MB – 2 GB compressed (depends on what the agent installed).

#### What Gets Captured (Pod Jobs)

**Included:**
- `workspace/job_<uuid>/` — full workspace tree
- `pip freeze` output, `npm list` output
- Agent config snapshot (already in `resolved_config` JSONB, referenced by manifest)

**On restore:** Fresh VM from base image → extract workspace overlay → `pip install -r` → `npm install`. Slower restore (~60s vs ~25s) but smaller snapshot (~100–500 MB).

### 2. Restore (IDE Button Click)

```
User clicks "IDE" on job_abc123
│
├── Check: Is there a live VM for this job? (jobs.context.vm.status == "ready")
│   └── Yes → Return existing code-server URL (done)
│
├── Check: Is there an active IDE session? (jobs.context.ide_session.status == "active")
│   └── Yes → Return existing code-server URL (done)
│
├── Check: Is there a snapshot in S3? (jobs.context.snapshot.status == "available")
│   ├── Yes (VM snapshot):
│   │   1. Provision fresh VM from base image
│   │   2. Download env.tar.zst from S3 (streaming decompress)
│   │   3. Extract overlay into VM via SSH
│   │   4. Boot code-server, wait for management daemon heartbeat
│   │   5. Return code-server URL
│   │
│   └── Yes (Pod snapshot):
│       1. Provision fresh VM from base image
│       2. Download and extract overlay via SSH
│       3. Reinstall packages from manifest (pip install, npm install)
│       4. Return code-server URL
│
└── No snapshot exists:
    └── Gitea fallback:
        1. Provision lightweight container (code-server + git)
        2. Clone job repo from Gitea
        3. Return code-server URL (no environment, just code)
```

#### Network Routing

Restored VMs are on the agent cluster, same as job VMs. Code-server is accessed through the same path:

- **Cross-cluster (NATS):** NodePort service → user's browser hits `<agent-node-ip>:<nodeport>`
- **Same-cluster:** Pod IP directly reachable, or via orchestrator reverse proxy

The orchestrator returns the full `code_server_url` in the IDE session response. The cockpit opens it in a new tab — no proxying through the cockpit itself.

### 3. Idle Teardown (TTL)

Restored IDE sessions are on-demand and ephemeral:

- **Idle timeout**: 30 minutes of no code-server activity (configurable)
- **Max lifetime**: 4 hours (configurable, prevents resource waste)
- **On teardown**: No re-snapshot. The S3 snapshot is immutable. If the user made changes they want to keep, they commit to Gitea from inside code-server.
- **Idle detection**: The management daemon on the restored VM monitors code-server's WebSocket connections (same heartbeat mechanism used for agent VMs). When no active connections remain, it reports idle status. The orchestrator's TTL check runs on its existing periodic task loop.

**Session state in `jobs.context.ide_session`:**

```json
{
  "ide_session": {
    "status": "restoring",
    "vm_name": "ide-job-abc123",
    "code_server_url": null,
    "started_at": "2026-03-17T15:00:00Z",
    "last_activity": null,
    "idle_timeout_minutes": 30,
    "max_lifetime_hours": 4
  }
}
```

**Status transitions:**

```
(none) ──POST /ide──► restoring ──VM ready──► active ◄──► idle
                                                │           │
                                         TTL expired   idle timeout
                                                │           │
                                                └─────┬─────┘
                                                      ▼
                                                   expired ──cleanup──► (none)
```

### 4. Garbage Collection

Snapshots accumulate. Cleanup policy:

| Trigger | Action |
|---------|--------|
| Job deleted by user | Move snapshot to `gc/pending_delete/`, purge after 7 days |
| Snapshot age > retention period | Soft-delete (default: 90 days, configurable per project) |
| Storage quota exceeded | Delete oldest non-pinned snapshots (LRU) |
| User pins a snapshot | Exempt from automatic GC |

The orchestrator runs a daily GC task (background asyncio task) that:
1. Lists all snapshots in S3
2. Cross-references with job records in PostgreSQL
3. Applies retention rules, respects pins
4. Moves expired snapshots to `gc/pending_delete/`
5. Purges items in `pending_delete/` older than 7 days

## API Design

### New Endpoints

```
POST   /api/jobs/{job_id}/ide          Start or get an IDE session
GET    /api/jobs/{job_id}/ide          Get IDE session status + URL
DELETE /api/jobs/{job_id}/ide          Tear down an active IDE session

GET    /api/jobs/{job_id}/snapshot     Get snapshot metadata (manifest)
DELETE /api/jobs/{job_id}/snapshot     Delete snapshot from S3
PUT    /api/jobs/{job_id}/snapshot/pin Toggle pin (GC exemption)
```

### `POST /api/jobs/{job_id}/ide` — Start IDE Session

Idempotent: if a session is already active, returns it. If restoring, returns current status.

**Request** (empty body or optional overrides):
```json
{
  "cpu_cores": 2,
  "memory": "4Gi",
  "idle_timeout_minutes": 60
}
```

**Response** (restoring):
```json
{
  "status": "restoring",
  "snapshot_type": "vm",
  "estimated_seconds": 25
}
```

**Response** (already active):
```json
{
  "status": "active",
  "code_server_url": "http://10.0.50.12:8080/?folder=/home/agent-host",
  "expires_at": "2026-03-17T19:00:00Z"
}
```

### `GET /api/jobs/{job_id}/ide` — Poll Status

| `status` | Meaning | `code_server_url` |
|----------|---------|-------------------|
| `unavailable` | No snapshot, no Gitea repo — IDE not possible | `null` |
| `available` | Snapshot or Gitea repo exists, no active session | `null` |
| `restoring` | VM booting from snapshot | `null` |
| `active` | Code-server reachable | URL |
| `idle` | Session alive but no active connections | URL |
| `expired` | Session torn down (TTL), can be re-started | `null` |

The cockpit uses this endpoint to determine IDE button visibility and state.

### `GET /api/jobs/{job_id}/snapshot` — Snapshot Metadata

**Response:**
```json
{
  "status": "available",
  "source_type": "vm",
  "created_at": "2026-03-17T14:30:00Z",
  "size_compressed_bytes": 856432100,
  "pinned": false,
  "environment_summary": {
    "python_packages": 42,
    "node_packages": 12,
    "custom_services": ["postgresql-16"]
  }
}
```

## Cockpit Integration

### IDE Button Behavior

The IDE button replaces the current static link approach. It's visible on all jobs (not gated by `codeServerUrl` env var) and calls the session API:

```
Click "IDE"
│
├── GET /api/jobs/{job_id}/ide
│
├── status: "active" or "idle"
│   └── Open code_server_url in new tab (instant)
│
├── status: "available"
│   ├── POST /api/jobs/{job_id}/ide
│   ├── Show spinner overlay: "Starting IDE session..."
│   ├── Poll GET /api/jobs/{job_id}/ide every 3s
│   └── On "active" → open code_server_url in new tab
│
├── status: "restoring" (already in progress, e.g. another tab started it)
│   └── Show spinner + ETA, poll until active
│
├── status: "unavailable"
│   └── Button hidden (no snapshot, no repo)
│
└── status: "expired"
    └── Same as "available" — re-start the session
```

### Button States

| IDE session status | Button | Style |
|--------------------|--------|-------|
| Live VM (processing job) | `IDE` | Blue, opens code-server directly |
| Snapshot available | `IDE` | Blue, triggers restore on click |
| Gitea repo only (no snapshot) | `IDE` | Dimmed blue, Gitea clone fallback |
| Restoring (in progress) | `Starting...` | Disabled + spinner |
| Unavailable (no snapshot, no repo) | Hidden | — |

### Implementation Note

The current cockpit IDE button is a static `<a>` tag. It needs to become a `<button>` that calls `ApiService.startIdeSession(jobId)` and manages loading state. The `getIdeUrl()` method in both `job-list.component.ts` and `job-review.component.ts` should be replaced with an `openIde(jobId)` method that hits the API and polls.

## Infrastructure: S3-Compatible Object Store

### MinIO Deployment

MinIO on SSD-backed storage. Single-node for MVP, multi-node for production HA.

```yaml
# docker-compose.dev.yaml addition
minio:
  image: quay.io/minio/minio:latest
  container_name: srw-minio
  command: server /data --console-address ":9001"
  environment:
    MINIO_ROOT_USER: ${MINIO_ROOT_USER:-srw}
    MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:-srw_password}
  volumes:
    - minio_dev_data:/data
  ports:
    - "${MINIO_API_PORT:-9000}:9000"
    - "${MINIO_CONSOLE_PORT:-9001}:9001"
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
    interval: 10s
    timeout: 5s
    retries: 5
```

**Production (Kubernetes):** MinIO Operator with SSD-backed PVCs, or hostPath on dedicated NVMe nodes.

### Environment Variables

```bash
# S3 / MinIO (orchestrator + VM controller)
S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=srw
S3_SECRET_KEY=srw_password
S3_BUCKET=srw-snapshots
S3_REGION=us-east-1                     # MinIO ignores, but boto3 requires

# IDE session limits (orchestrator)
IDE_SESSION_IDLE_TIMEOUT=30             # minutes
IDE_SESSION_MAX_LIFETIME=240            # minutes (4 hours)
IDE_MAX_CONCURRENT_PER_USER=2

# Snapshot retention (orchestrator)
SNAPSHOT_RETENTION_DAYS=90
SNAPSHOT_MAX_SIZE_GB=10                 # Skip capture if estimated size exceeds
```

### Service Ports

| Service | Port |
|---------|------|
| MinIO API | 9000 |
| MinIO Console | 9001 |

## NATS Integration

New subjects for snapshot operations (cross-cluster mode only):

| Subject | Direction | Purpose |
|---------|-----------|---------|
| `vm.snapshot.capture` | Orchestrator → VM Controller | Request environment tar + S3 upload |
| `vm.snapshot.capture.status` | VM Controller → Orchestrator | Progress (exporting, compressing, uploading, done, failed) |
| `vm.snapshot.restore` | Orchestrator → VM Controller | Request VM creation + overlay extraction |
| `vm.snapshot.restore.status` | VM Controller → Orchestrator | Restore progress (provisioning, extracting, ready, failed) |

For direct K8s mode (same-cluster without NATS), the orchestrator handles capture/restore via SSH to the VM or pod exec.

Idle detection reuses the existing `agent.vm.{job_id}.heartbeat` subject — the management daemon already reports resource usage; it gains an additional `code_server_connections: int` field.

## Security Considerations

| Concern | Mitigation |
|---------|------------|
| Snapshots contain secrets | S3 bucket uses server-side encryption (SSE-S3). IDE endpoints enforce project membership via Keycloak token — only the job owner or project members can start a session or download the manifest. |
| Restored VMs run arbitrary code | Same hypervisor isolation as job VMs. IDE sessions get a restricted network policy: code-server port inbound only, no outbound internet by default. |
| S3 credentials | Kubernetes secrets, injected via env. Never stored in job context or Gitea. |
| Snapshot tampering | SHA-256 checksum in manifest, verified before restore. Manifest itself is integrity-checked via S3 ETags. |
| Resource exhaustion | Per-user concurrent session limit (default: 2). Max lifetime enforced server-side. Snapshot size cap per job (default: 10 GB compressed). |
| Stale secrets in snapshots | Document that API keys / tokens baked into the VM environment will persist in the snapshot. Users should use env-var injection (not hardcoded secrets) for agent jobs. |

## Integration with Existing Systems

| System | Impact |
|--------|--------|
| **VM Provisioner** | New `create_vm_from_snapshot(job_id, manifest)` method. Provisions VM from base image, then applies overlay. |
| **VM Controller** | New handlers: `vm.snapshot.capture` (tar + upload) and `vm.snapshot.restore` (download + extract). |
| **NATS Bridge** | New subscriptions for snapshot status subjects. |
| **Management Daemon** | Adds `code_server_connections` to heartbeat payload for idle detection. |
| **Job Completion Flow** | Snapshot step inserted between "job complete" and "delete VM". Non-blocking: capture failure does not prevent teardown. |
| **Phase Snapshots** | Extended. Phase snapshots currently capture workspace-level files (for checkpoint resume). Now also trigger S3 environment snapshots at each phase boundary, stored per-phase. Both systems run at the same trigger points but capture different scopes. |
| **Checkpoint Resume** | Integrated. On job resume, the orchestrator first restores the latest S3 environment snapshot into a fresh VM, then replays the LangGraph checkpoint. This gives true "pick up where you left off" (full environment + agent state). The resume flow becomes: find latest snapshot → provision VM → extract overlay → inject checkpoint → start agent. |
| **Gitea** | Unchanged. Still the source of truth for workspace files. S3 snapshots capture the environment around those files. |
| **Workspace Backend** | Unchanged. Restored IDE VMs use the same SSH/SFTP path as job VMs (RemoteBackend). |

## Implementation Phases

### Phase 1: MinIO + Snapshot Capture (Foundation)

- [ ] Add MinIO to `docker-compose.dev.yaml` and production K8s manifests
- [ ] Implement `SnapshotService` with boto3 S3 client (upload, download, list, delete, per-phase keys)
- [ ] Add SSH tar capture to VM teardown flow in `vm_provisioner.py`
- [ ] Add phase-boundary capture hook (triggered by `archive_phase` in the graph, alongside existing workspace phase snapshots)
- [ ] Add pre-termination capture hook for pod jobs (workspace + package manifests)
- [ ] Store snapshot metadata in `jobs.context.snapshot` (latest + per-phase index)
- [ ] Add `GET /api/jobs/{job_id}/snapshot` endpoint
- [ ] Show snapshot availability indicator in cockpit job list

### Phase 2: IDE Session Restore + Resume Integration

- [ ] Implement `POST /api/jobs/{job_id}/ide` — provision VM, download + extract snapshot
- [ ] Implement `GET /api/jobs/{job_id}/ide` — session status polling
- [ ] Implement `DELETE /api/jobs/{job_id}/ide` — manual teardown
- [ ] Add idle detection via management daemon heartbeat (`code_server_connections`)
- [ ] Add TTL enforcement (idle timeout + max lifetime) to orchestrator periodic task
- [ ] Upgrade cockpit IDE button from static link to session-aware (spinner, polling, redirect)
- [ ] Integrate snapshot restore into job resume flow (restore S3 snapshot → inject checkpoint → start agent)
- [ ] Support phase-specific restore: orchestrator resume with a target phase number uses phase N's S3 snapshot + checkpoint

### Phase 3: Gitea Fallback

- [x] Implement lightweight fallback: code-server container + `git clone` from Gitea for jobs with no snapshot
- [x] Unify the IDE session API response format across snapshot-restore and Gitea-clone paths (`restore_type: "vm" | "container"`)

### Phase 4: Polish & Operations

- [x] Snapshot GC (daily task: retention policies, soft-delete to `gc/pending_delete/`, 7-day grace purge, pin exemption)
- [x] `PUT /api/jobs/{job_id}/snapshot/pin` endpoint (toggle GC exemption)
- [x] `GET /api/snapshots/stats` endpoint (total count, size, GC pending)
- [x] MinIO console link in cockpit sidebar (alongside pgAdmin, Mongo Express, etc.)
- [x] Snapshot storage stats in cockpit job list header
- [ ] Per-project retention policy configuration

### Phase 5: Full Disk Snapshots (Future)

- [ ] Migrate VMs from ephemeral containerDisk to persistent volumes (PVC)
- [ ] Use KubeVirt `VirtualMachineSnapshot` CRD for native disk snapshots
- [ ] Base image dedup: layered snapshots (store only diff from base)
- [ ] Incremental snapshots at phase boundaries (not just completion)
- [ ] Live snapshot support (no freeze required)

## Configuration

### Agent-Side (`config/defaults.yaml`)

```yaml
snapshot:
  enabled: true                     # Capture snapshot on job completion
  on_phase_boundary: true           # Capture at each phase transition (strategic ↔ tactical)
  on_failure: true                  # Also capture on job failure
  on_cancel: false                  # Capture on user cancellation
  capture_method: ssh_tar           # ssh_tar (MVP) | kubevirt_snapshot (Phase 5)
  exclude_patterns:
    - "/var/cache/*"
    - "/tmp/*"
    - "*.pyc"
    - "__pycache__"
    - "node_modules/.cache"
  max_size_gb: 10                   # Skip capture if estimated size exceeds this
```

### Orchestrator-Side (Environment Variables)

```bash
S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=srw
S3_SECRET_KEY=srw_password
S3_BUCKET=srw-snapshots
S3_REGION=us-east-1
IDE_SESSION_IDLE_TIMEOUT=30         # minutes
IDE_SESSION_MAX_LIFETIME=240        # minutes
IDE_MAX_CONCURRENT_PER_USER=2
SNAPSHOT_RETENTION_DAYS=90
SNAPSHOT_MAX_SIZE_GB=10
```

## Resolved Design Decisions

1. **Delta snapshots vs full?** Start with full snapshots. Add layered/delta snapshots in Phase 5 if storage becomes a concern.

2. **Should IDE sessions be writable to Gitea?** Sessions are disposable. Users *can* `git push` manually from the IDE (git is configured), but we don't surface a push button in the cockpit. Revisit if users request it.

3. **Snapshot during job execution?** Yes — capture environment snapshots at phase boundaries, not just on completion/failure. This extends the existing phase snapshot mechanism (which currently captures workspace files only) to include the full environment. Gives finer-grained restore points for debugging and resume. The agent triggers capture via NATS or direct SSH; the orchestrator stores each phase snapshot as a separate S3 key (`jobs/<uuid>/phase_<n>/env.tar.zst`), with the latest always copied to `jobs/<uuid>/env.tar.zst`.

4. **Multi-user concurrent IDE access?** Yes, allow by default. Code-server handles multiple connections natively. Sessions are ephemeral — no conflict risk. May add cursor awareness in a future iteration.

5. **Snapshot + checkpoint resume integration?** Yes — integrate both systems. When resuming a failed/paused job, the orchestrator automatically restores the S3 snapshot into a fresh VM *before* replaying the LangGraph checkpoint. This provides true "pick up where you left off" (environment + state). The resume flow becomes: find latest snapshot → provision VM → extract snapshot → inject checkpoint → start agent.
