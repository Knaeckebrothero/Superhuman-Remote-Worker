---
tags:
  - feature
  - architecture
  - cloud-infrastructure
  - workspace
  - rclone
  - guardrails
aliases:
  - rclone cloud mount
  - lazy cloud workspace
  - virtual cloud filesystem
related:
  - "[[cloud_collaboration_model]]"
  - "[[main_cloud_abstraction]]"
  - "[[project_cloud_folders]]"
  - "[[webdav_datasource_tools]]"
  - "[[sudo_approval_plugin]]"
  - "[[sudo_permissions]]"
---

# Rclone Cloud Mount - Lazy Cloud Workspaces

**Status:** Phase 1 v4 implemented 2026-06-09. Later hydration-budget
approvals and indexing/search phases remain pending.

## 1. Goal

Replace the eager cloud-folder clone/pull path with an rclone-backed filesystem
mount, so the agent can work with the user's cloud files through ordinary Linux
paths without downloading the entire cloud surface at session startup.

The product goal from [[cloud_collaboration_model]] stays intact:

- The user's real cloud space remains the collaboration surface.
- The agent uses normal filesystem operations, shell commands, editors, and
  scripts instead of cloud-provider-specific tool calls.
- The user should not have to move files into a special "AI folder" before the
  agent can reason over them.

The data-plane goal changes:

- **Before:** resolve cloud mounts, recursively pull all files into the
  workspace, then let the agent operate locally.
- **After:** resolve cloud mounts, expose them as mounted filesystem trees, and
  materialize file bytes lazily as commands or tools read them.

## 2. Problem

The current cloud-sync model couples two different concepts:

1. **Scope:** which cloud space the agent is allowed to work with.
2. **Hydration:** how much of that cloud space must be downloaded before the
   session can start.

For a default-project session, that scope can be the user's full home cloud. If
that home contains large folders such as `Photos/`, the initial pull can become
100GB+. Even if the pull eventually succeeds, startup time, disk usage, and
failure modes are unacceptable.

We still want broad scope. What we do not want is eager hydration of the entire
scope.

## 3. Why rclone

rclone already provides the hard part of the virtual filesystem:

- `rclone mount` exposes remote storage as a FUSE filesystem on Linux.
- The VFS layer adapts object/WebDAV storage to normal file operations.
- Directory metadata is cached separately from file contents.
- `--vfs-cache-mode full` supports normal read/write behavior and sparse cached
  files.
- `--vfs-cache-max-size`, `--vfs-cache-max-age`, and related flags give us
  bounded local cache policy.
- rclone filters can hide ignored paths from the mount.

References:

- https://rclone.org/commands/rclone_mount/
- https://rclone.org/rc/
- https://rclone.org/filtering/
- https://rclone.org/webdav/
- https://rclone.org/drive/
- https://rclone.org/s3/
- https://rclone.org/install/
- https://kubernetes.io/docs/concepts/storage/volumes/#mount-propagation
- https://kubernetes.io/docs/tasks/configure-pod-container/security-context/

rclone does not solve product policy. It gives us the filesystem projection. We
still need mount lifecycle, credentials, cache limits, search/indexing, and
guardrails around commands that accidentally scan too much data.

### 3.1. What this replaces

This feature is intended to replace the current main-cloud workspace clone/sync
data plane.

Today the orchestrator uses the [[main_cloud_abstraction]] layer to resolve user
home folders, project folders, and session folders. The agent then uses
`src/services/cloud_sync/` to recursively pull and push those cloud surfaces,
mostly through WebDAV transport primitives.

That eager pull/push layer is what rclone mount replaces:

- no startup `pull_all()` of the full default-project home;
- no recursive clone before the agent can start;
- no turn-boundary full-tree diff as the only way to see cloud files;
- reads and writes go through a mounted filesystem with bounded local VFS cache.

The main-cloud abstraction remains. It continues to decide which cloud resource
belongs to a thread. rclone becomes the mount/runtime data plane for that
resource.

### 3.2. Control plane anchor

`thread_mounts` remains the source of truth for what a thread is allowed to see.
It already records mount kind, target path, backend id, cloud handle, and
backend-specific metadata. The rclone design should not replace that table with
another attachment model.

The change is payload shape:

- keep `thread_mounts` as the persistent control-plane record;
- add a new `cloud_mount` payload for rclone-capable runtimes;
- keep the current `cloud_sync` payload as a compatibility fallback during
  rollout;
- do not add more WebDAV-only fields to make non-WebDAV providers fit.

## 4. Non-Goals

- Do not build a custom FUSE filesystem in SRW for v1.
- Do not make cloud providers execute arbitrary POSIX commands server-side.
- Do not replace repository datasources or Git clone behavior.
- Do not remove WebDAV datasource tools; those remain for externally attached
  WebDAV datasources that are not part of the main-cloud workspace surface.
- Do not promise that `grep -R /cloud` is cheap. It is not.

## 5. Target Workspace Model

Introduce a dedicated cloud mount namespace instead of mounting directly over
the workspace root:

```text
/workspace/
  .srw/                 # agent/session state, not cloud-synced
  repos/                # repository datasources
  datasources.md
  workspace.md
  cloud -> /cloud/home  # default-only session shortcut

/cloud/
  home/                 # default project / personal cloud root
```

For sessions with additional project cloud mounts, `/workspace/cloud` becomes a
directory of symlinks instead of a direct symlink:

```text
/workspace/
  cloud/
    home -> /cloud/home
    <project-slug> -> /cloud/<project-slug>

/cloud/
  home/
  <project-slug>/
```

The agent shell still starts in `/workspace`. `/cloud` is the real mount
namespace, outside the workspace tree, and `/workspace/cloud` is only the
ergonomic entry point exposed to the agent.

Why not mount the default project directly at `/workspace`?

- A FUSE mount over `/workspace` would hide agent-managed files like
  `workspace.md`, `datasources.md`, `.srw/`, and `repos/`.
- If those files were moved into the cloud root, we would leak agent-internal
  state into the user's personal storage.
- A dedicated `/cloud` tree keeps the user's files clearly separated from
  SRW-owned state while still giving the model a normal filesystem.

**Decision:** keep the shell in `/workspace`. Use `/workspace/cloud` as the
ergonomic shortcut to the mounted cloud surface.

## 6. Agent Contract

The agent should be told:

- User cloud files are available from `/workspace/cloud`.
- `/workspace/cloud` is backed by real mounts under `/cloud`.
- Targeted filesystem operations are expected: `ls`, `find -name`, `cat`,
  `head`, editing a known file, running scripts against selected paths.
- Broad content scans over `/workspace/cloud` or `/cloud` are expensive and
  should use SRW search or request approval first.
- Use `srw-cloud-search` or a future typed search tool for "find all documents
  mentioning X" rather than blind recursive `grep`.

This preserves the Linux-filesystem advantage while steering broad discovery to
an index/search layer designed for cloud-scale data.

## 7. rclone Mount Lifecycle

### 7.1. Orchestrator payload

The orchestrator should continue to derive mounts from `thread_mounts`, but the
agent payload should use a new `cloud_mount` shape instead of overloading the
current sync-specific WebDAV config:

```json
{
  "cloud_mount": {
    "version": 1,
    "driver": "rclone",
    "mounts": [
      {
        "mount_id": "...",
        "mount_kind": "project_default",
        "target_path": "/cloud/home",
        "backend": "nextcloud",
        "source": {
          "type": "webdav",
          "url": "https://.../remote.php/dav/files/user/"
        },
        "auth": {
          "type": "basic",
          "username": "...",
          "password_ref": "session-secret-ref"
        },
        "access": "read_write",
        "filters_ref": "cloudignore-v1"
      }
    ]
  }
}
```

The exact secret transport can differ from this sketch. The important part is
that credentials are not printed in logs or persisted into workspace files.

`source.type` is an rclone backend type, not a promise that every provider uses
WebDAV. Nextcloud and generic WebDAV clouds should use `webdav`; Google Drive
should use rclone's `drive` backend; S3-compatible stores should use `s3`;
future OneDrive/SharePoint support should use whichever rclone backend is best
for that provider. The agent-side mount manager should not need provider
branches beyond "write this rclone config and mount this remote".

`cloud_sync` and `cloud_mount` should not be active for the same mount. A thread
uses rclone where every required mount has a supported rclone spec and the
workspace runtime advertises rclone/FUSE support. Otherwise it falls back to the
old sync path until that runtime/provider is supported.

### 7.2. Agent-side mount manager

Add an agent-side mount manager responsible for lifecycle, and run it in the
same workspace runtime where shell commands execute. In the current production
shape this means the SSH workspace container or VM, not merely the persistent
agent process.

Responsibilities:

- writing a per-session rclone config to a runtime-only path with mode `0600`;
- creating mount points under `/cloud`;
- starting one rclone process per mount;
- enabling rclone remote control on a local-only socket or localhost port, with
  per-session credentials;
- waiting until the mount is ready before the first turn;
- exposing mount status to the session event stream;
- flushing uploads and unmounting with `fusermount3 -u` on session shutdown;
- killing stuck rclone processes after a bounded timeout;
- cleaning stale mounts and caches on startup.

The existing `src/services/cloud_sync/` coordinator can be retired for rclone
mount sessions after the new path is stable. During rollout, keep a feature flag
such as `CLOUD_WORKSPACE_DRIVER=sync|rclone_mount`.

### 7.3. Cache policy

Suggested defaults for the first production profile:

```text
--vfs-cache-mode full
--vfs-cache-max-size 10G
--vfs-cache-max-age 24h
--vfs-cache-min-free-space 5G
--dir-cache-time 5m
--poll-interval 1m
--vfs-read-chunk-size 16M
--vfs-read-chunk-size-limit 128M
```

The exact numbers should be deployment-configurable. The invariant is that
cache size is bounded and per-session caches do not overlap.

Important: `--vfs-cache-max-size` is an eviction target, not a hard disk quota.
rclone can exceed it while open files are in use or before the cache poller
evicts old entries. The workspace runtime still needs disk monitoring and a
separate hard failure path when local storage is nearly full.

Initial policy:

- rclone VFS soft cache target: 10GB per session;
- SRW hard disk guard for the rclone cache: 20GB per session;
- when the hard guard is hit, stop or pause new cloud hydration and wait for
  cache cleanup or explicit approval before continuing;
- later deployments can tune both values per instance or per user tier.

The mount manager should use rclone RC for observability:

- `core/stats` for transfer counters;
- `core/transferred` for recently completed transfers;
- `vfs/stats` for disk cache bytes, queued uploads, and cache health;
- `vfs/refresh` or `vfs/forget` when `.cloudignore` or cloud-side state changes.

Do not inject partial cache state into the agent prompt by default. The agent
does not need to know which files are currently hydrated. Surface only actionable
state:

- mount degraded/unavailable;
- index unavailable or incomplete for a requested search;
- hydration/cache limit reached;
- explicit `srw-cloud-status` output when the agent asks for status.

### 7.4. MainCloudBackend-to-rclone contract

Provider-specific rclone configuration should live behind
[[main_cloud_abstraction]], not in the agent prompt and not scattered through
the shell tooling.

Add an optional main-cloud capability that converts a backend handle into a
mount descriptor:

```python
class SupportsRcloneMount(Protocol):
    async def build_rclone_mount_spec(
        self,
        *,
        handle: ProjectFolderHandle | SessionFolderHandle,
        mount_kind: str,
        target_path: str,
        access: Literal["read_only", "read_write"],
        subject: CloudMountSubject | None = None,
    ) -> RcloneMountSpec: ...
```

The returned spec should contain:

- the rclone backend type (`webdav`, `drive`, `s3`, `onedrive`, etc.);
- the provider-specific remote config fields;
- the remote root/path to mount;
- credential references or token-refresh command wiring;
- required provider flags;
- recommended cache/filter defaults;
- required runtime capabilities such as `fuse`, `rclone`, `rc`, or
  `token_helper`.

This keeps the boundary clean:

```text
MainCloudBackend     = cloud lifecycle, permissions, handles, provider details
RcloneMountSpec      = provider-specific rclone remote description
Agent mount manager  = generic rclone process/config/cache lifecycle
rclone               = filesystem projection and byte transport
```

The mount manager should be generic. It consumes `RcloneMountSpec` and starts
rclone. It should not contain business logic like "Nextcloud project folders
live here" or "Google Drive needs this OAuth shape"; that belongs to the owning
main-cloud backend.

The workspace runtime should advertise capabilities such as
`supports_rclone_mount` and `supports_fuse`. The orchestrator only emits
`cloud_mount` when both the backend and runtime support the requested mounts.

## 8. Guardrails

The main risk is not startup anymore. The new risk is accidental hydration:

```bash
grep -R "invoice" /cloud
tar -cf cloud.tar /cloud
du -sh /cloud
python scan_everything.py /cloud
```

These commands work because `/cloud` is a real filesystem. They may also read a
huge portion of the remote and cause rclone to download or stream massive
amounts of data.

### 8.1. Do not rely on command-string regex

A regex blocklist in `run_command` would be fragile:

- `grep -R` can be expressed many ways.
- `rg`, `ag`, `find -exec grep`, Python scripts, and shell loops have the same
  effect.
- A determined process can bypass shell aliases or PATH wrappers.

We can still use cheap preflight warnings for obvious commands, but the primary
control should be based on observed cloud hydration, not command text.

### 8.2. Cloud hydration guard

Add a guard analogous in product behavior to the sudo gate, but do not treat it
as the same security boundary. Sudo works because a privileged plugin intercepts
the operation before privilege is granted. Cloud hydration is ordinary file IO.
The reliable v1 control is policy plus rclone runtime telemetry.

Design:

1. Before a tool call starts, classify whether it may touch `/cloud`.
2. For likely broad scans, require approval before execution.
3. For allowed calls, record rclone per-mount transfer counters via RC.
4. Run shell commands in a tracked process group where possible.
5. Poll rclone stats while the tool call runs.
6. If the command downloads more than the configured command budget, pause or
   stop the process group and raise a `cloud_scan` approval request.
7. The cockpit shows the command, mount, bytes already read, current path if
   known, and the recommended alternative (`srw-cloud-search`, narrower path,
   add `.cloudignore`, etc.).
8. Approve resumes with a larger one-time budget. Deny terminates the command
   and returns an actionable message to the agent.

This catches accidental broad scans regardless of whether they came from
`grep`, `rg`, `cat`, Python, or a library.

The guard must run before tool execution generally, not only inside
`run_command`. Workspace tools such as `search_files` can also hydrate a large
mount because remote workspace search may shell out to recursive grep. Future
file/read/search/materialization tools should declare whether they can touch
cloud mounts so the same guard applies consistently.

`cloud_scan` approval should be able to force review even in autonomous mode.
Otherwise the mode that most needs runtime safety would bypass it.

Implementation note: pausing a shell command cleanly is harder than sudo because
there is no privileged plugin boundary before the operation. The v1 fallback can
stop or terminate the operation, wait for cache cleanup where possible, and ask
the agent to retry with a narrower command or after approval. A later VM-only
implementation can use process groups, cgroups, or a small root-owned guard
daemon for cleaner suspend/resume behavior.

### 8.3. Preflight warnings

Add a lightweight advisory layer for common obvious cases:

- recursive grep/ripgrep/ag over `/cloud` or `/workspace/cloud`;
- `du`, `tar`, `zip`, `rsync`, `cp -r`, `find -exec` against `/cloud`;
- shell cwd is `/cloud` and the command has no narrower target.

This should parse argv structurally where possible (`shlex`, known command
schemas), not match arbitrary command strings. Preflight can produce a nudge or
require approval before execution. Runtime hydration budgets remain the
enforcement layer.

Complex shell syntax that mentions `/cloud` and cannot be parsed safely should
fall back to review, not allow. The parser also needs canonical path handling so
aliases such as `/workspace/cloud` or symlinks cannot bypass the policy.

### 8.4. Budget defaults

Initial defaults should be conservative:

| Budget | Suggested default |
|---|---:|
| Per-user cloud hydration budget | Deployment configured |
| Per-session VFS cache soft target | 10GB |
| Per-session VFS cache hard guard | 20GB |
| Single file auto-read without approval | 512MB |
| Read-only large media default | Warn / require approval |

These are product defaults, not security boundaries. Admins should be able to
tune them per deployment, and later per project/user.

**Decision:** start with a per-user hydration budget. Add per-command or
per-turn budgets only if production behavior shows that broad commands consume
the user budget too quickly or create poor UX.

## 9. `.cloudignore` and Filters

Support a `.cloudignore` file at the cloud root. The syntax should start as a
small gitignore-style subset and compile to rclone filters.

Example:

```gitignore
Photos/
Videos/
*.iso
*.mov
*.zip
node_modules/
```

Behavior:

- ignored paths are not visible in the rclone mount;
- ignored paths are excluded from background indexing;
- changing `.cloudignore` requires remounting or refreshing the rclone VFS;
- SRW may also apply deployment-level default ignores before user rules.

For the immediate large-home incident, `.cloudignore` is the fastest manual
escape hatch. It should not be the only safety mechanism because non-technical
users will not reliably create one.

## 10. Search and Indexing

rclone mount makes the cloud look like a filesystem. It does not make broad
content search cheap.

The long-term discovery path should be:

1. Background indexer walks cloud mounts with budgets and `.cloudignore`.
2. Indexer extracts text/metadata where practical.
3. `srw-cloud-search` or a typed search tool supports:
   - filename/path search;
   - regex over indexed text;
   - vector search;
   - graph/relationship search;
   - filters by path, type, size, mtime, project, and owner.
4. Search returns candidate paths.
5. The agent opens only the candidate files through the normal filesystem.

This gives the model the filesystem UX for actual work while avoiding recursive
shell scans for discovery.

## 11. Provider Notes

### Nextcloud / generic WebDAV

This is the best first target. rclone's WebDAV backend supports Nextcloud and
basic credentials/app passwords. It matches the current homelab incident and
requires the least authentication machinery.

Project and session folders are easier than default user homes because the
service account can be granted access to project/session surfaces. Default
user-home mounts need an explicit auth decision. A WebDAV URL under
`/remote.php/dav/files/{username}/` is normally the connecting user's file
space; blindly pairing that URL with the agent-service credentials is not a
sound general model.

Considered options:

- per-user app password or user-authorized WebDAV credential;
- OIDC bearer/token helper if the provider supports it;
- explicit share of the user's home or selected root to the agent account;
- admin/impersonation if the provider supports it safely;
- fallback to a regular session folder when no safe user-home mount credential
  exists.

**Decision:** v1 default user-home mounts require explicit user-granted WebDAV
credentials, preferably an app password/app token. Do not use admin
impersonation for v1, and do not silently pair a user's home URL with the
agent-service credentials. If no safe user-home credential exists, fall back to
a regular session folder instead of refusing the session.

### OpenCloud

OpenCloud is still the greenfield main-cloud default in [[main_cloud_abstraction]].
If mounted through WebDAV with bearer tokens, we need a token-refresh plan. A
static short-lived bearer token inside an rclone config is not enough for long
sessions. Options:

- use a service credential/app password if available;
- restart the mount with a refreshed token before expiry;
- run an rclone wrapper/sidecar that can refresh credentials;
- use rclone WebDAV `bearer_token_command` to invoke a local token helper;
- keep OpenCloud on the current sync path until token refresh is solved.

**Decision:** use app-token/service credentials for stable service/project
spaces when available. For user-scoped mounts, treat `bearer_token_command` with
a local token helper as the long-term target. Until that helper is robust, fall
back to the session-folder path rather than mounting a user home with a static
short-lived bearer token.

### Google Drive

Do not force Google Drive through WebDAV unless that proves better for a
specific deployment. rclone has a `drive` backend, so a future Google Workspace
main-cloud adapter should emit an rclone `drive` spec with the right OAuth /
service-account strategy.

This is exactly why `MainCloudBackend` should own rclone remote construction:
the agent still sees `/cloud/home`, while the backend chooses whether the remote
is WebDAV, Drive, S3, OneDrive, or something else.

### S3 / object stores

rclone can mount S3-like stores, but object stores do not provide POSIX
semantics natively. Empty directories, renames, mtimes, hashes, and write
behavior may differ. Treat S3 mount support as a later adapter, not the first
acceptance target.

## 12. Deployment Constraints

The mount must exist inside the runtime where shell commands execute.

### 12.1. VM-backed workspaces

This should be the first implementation target. VMs avoid the Kubernetes
`/dev/fuse` and pod security-context problem, and they already have the VM sudo
gate for controlled privileged setup.

- install `rclone` and `fusermount3` in the VM image;
- mount under `/cloud`;
- run mount manager as the agent user where possible;
- keep a root-owned cleanup path for stale/busy mounts if needed.

### 12.2. Kubernetes pod/container workspaces

Container workspaces are the primary release runtime. The default workspace
container profile therefore supports rclone/FUSE directly and runs rclone in the
same container namespace as the agent shell. This path needs:

- `rclone`;
- `fuse3` / `fusermount3`;
- `/dev/fuse`;
- `CAP_SYS_ADMIN` or equivalent mount capability;
- AppArmor/seccomp compatibility;
- enough local storage for the VFS cache.

Kubernetes mount propagation is a low-level feature. The Kubernetes docs warn
that bidirectional mount propagation can damage the host and is privileged-only.
Avoid designs that require propagating rclone mounts back to the host or across
pods. Prefer running rclone and the agent shell in the same container/VM
namespace where possible.

Some clusters will not allow this for hardened pods. `WORKSPACE_FUSE_ENABLED`
and `CLOUD_RCLONE_ALLOW_CONTAINER` remain opt-outs, and `sync` remains a
compatibility fallback for deployments that cannot run FUSE.

k3d testing showed that `/dev/fuse` plus `SYS_ADMIN` is not sufficient on the
default local runtime. The default workspace FUSE profile is therefore
privileged with an unconfined seccomp profile when
`WORKSPACE_FUSE_PRIVILEGED=true`. Deployments that can run a narrower profile
can set `WORKSPACE_FUSE_PRIVILEGED=false`, which keeps `/dev/fuse` plus
`SYS_ADMIN` and the runtime-default seccomp profile.

### 12.3. Runtime-only state and snapshots

Do not store rclone config, token files, VFS cache, or mounted cloud paths under
paths captured by workspace snapshots.

Rules:

- mount cloud at `/cloud/...`, outside `/home/agent-host/workspace`;
- expose `/workspace/cloud` as a symlink or symlink directory, depending on how
  many cloud surfaces are attached;
- place rclone config in a runtime-only directory with strict permissions;
- place VFS cache in a bounded runtime-only cache directory;
- exclude `/cloud`, rclone config, and rclone cache from VM/workspace snapshots;
- make shutdown tolerant of already-deleted workspaces and stuck FUSE mounts.

This prevents snapshots from capturing user cloud data, credentials, hydrated
cache files, or hanging on mounted paths.

Cleanup order:

1. Ask rclone to flush and unmount cleanly.
2. Use `fusermount3 -u` after a bounded timeout.
3. Use lazy unmount (`fusermount3 -uz`) only when the mount is still busy.
4. Kill only rclone processes owned by the session.
5. Quarantine leftover cache/config paths before deletion if mount state is
   uncertain.

Never delete a mounted `/cloud/...` path recursively. Corrupting or deleting user
cloud data is worse than leaking a temporary cache directory for later cleanup.

## 13. Datasource Cleanup Interaction

This feature does not make legacy WebDAV datasources correct.

Separate cleanup is still needed for:

- old auto-created `Cloud Storage (<project>)` rows that expose a project folder
  through `webdav_*` tools even though the same folder is already a workspace
  surface;
- the global/admin Nextcloud datasource seeded by deployment defaults;
- `Cloud Storage (Personal)` rows that may expose the user's full home through
  imperative WebDAV tools.

The rclone mount model should be the main-cloud workspace data plane.
`webdav_*` tools should remain only for explicit external WebDAV datasources.

## 14. Implementation Status

Phase 1 v1 landed on 2026-06-09. Phase 1 v4 makes rclone mount the default
main-cloud workspace data plane for supported VM and container workspaces.

### Implemented Control Plane

- Added `CLOUD_WORKSPACE_DRIVER=sync|rclone_mount`.
- Helm exposes the same switch through `cloud.workspaceDriver`.
- The orchestrator emits a new `cloud_mount` payload only when the driver is
  `rclone_mount` and the workspace runtime can support rclone/FUSE.
- VM-backed workspaces are supported when the VM is ready and has an SSH
  endpoint.
- Container workspaces are supported by default when ready. Workspace pods mount
  `/dev/fuse`; the default FUSE profile is privileged/unconfined for reliable
  k3d/local operation, with a narrower `SYS_ADMIN` profile available by setting
  `WORKSPACE_FUSE_PRIVILEGED=false`.
- The workspace image installs `rclone` and `fuse3` and owns `/cloud` as
  `agent-host`.
- `CLOUD_RCLONE_ALLOW_CONTAINER=false` disables rclone payloads for container
  workspaces; `WORKSPACE_FUSE_ENABLED=false` disables the pod-level FUSE
  profile; `WORKSPACE_FUSE_PRIVILEGED=false` disables the privileged FUSE
  profile while keeping `/dev/fuse`/`SYS_ADMIN`.
- `cloud_mount` and `cloud_sync` are mutually exclusive for the same workspace
  response.
- If `rclone_mount` is requested but unavailable, the orchestrator falls back to
  the regular per-session cloud folder and does not eagerly clone `thread_mounts`
  such as a default user home.
- The regular session folder is kept provisioned while rclone is enabled so it
  can be used as a fallback mount.

Current payload shape:

```json
{
  "cloud_mount": {
    "version": 1,
    "driver": "rclone",
    "cloud_root": "/cloud",
    "workspace_entry": "cloud",
    "fallback": false,
    "mounts": [
      {
        "mount_id": "...",
        "mount_kind": "project_default",
        "backend": "nextcloud",
        "target_path": "/cloud/home",
        "workspace_name": "home",
        "access": "read_write",
        "source": {
          "type": "webdav",
          "config": {
            "url": "https://.../remote.php/dav/files/user/",
            "vendor": "nextcloud",
            "user": "..."
          }
        },
        "auth": {
          "type": "basic",
          "password": "..."
        },
        "cache": {
          "vfs_cache_mode": "full",
          "vfs_cache_max_size": "10G",
          "hard_cache_limit": "20G"
        }
      }
    ]
  }
}
```

The password is transported in the in-memory workspace payload and is written
only into the runtime-local rclone config on the workspace host. It must not be
logged or persisted into workspace files.

### Phase 1 v2 Delta

Implemented on 2026-06-09 after the initial rclone mount path:

- rclone mount startup now prepares a filter file for each mount, using
  deployment/session defaults plus the remote root `.cloudignore` file when it
  exists.
- `.cloudignore` support is intentionally a small gitignore-style subset:
  comments and blank lines are ignored, negation rules are skipped, directory
  patterns expand to recursive excludes, and unsafe parent traversal patterns
  are ignored.
- `RcloneMountManager` now tracks per-mount hard cache limits, can query cache
  usage from the runtime host, and can render an agent-safe status summary.
- Active sessions expose `srw_cloud_status` so the agent can explicitly ask for
  mount state, VFS cache usage, and rclone RC stats.
- Shell commands that name `/cloud` or `/workspace/cloud` now check the
  per-session hard cache guard before execution.
- Workspace `read_file`, `write_file`, and `edit_file` check the same hard
  cache guard before operating on `cloud/...` paths.
- Workspace `search_files` now refuses workspace-root or `cloud/...` searches
  when a cloud mount is active, because that would recursively scan the mounted
  cloud surface.

### Phase 1 v4 Delta

Implemented on 2026-06-09 while validating the default path against local k3d:

- `rclone_mount` is now the Helm default for the main-cloud workspace driver.
- The Tilt/local workflow builds the workspace container image with `rclone`,
  `fuse3`, and `/cloud` support.
- `.dockerignore` excludes generated workspace/VM image output directories so
  local workspace image builds do not send multi-GB build contexts.
- Container workspace FUSE defaults now use privileged/unconfined security
  context on k3d; `WORKSPACE_FUSE_PRIVILEGED=false` keeps the narrower
  `/dev/fuse`/`SYS_ADMIN` profile for clusters where that works.
- The mount manager resolves the actual remote backend workspace root when
  creating `/workspace/cloud`, so SSH-backed container workspaces link from
  `/home/agent-host/workspace/cloud` instead of the agent pod's logical
  `/workspace`.
- rclone cache flags are gated against the runtime's `rclone mount --help`.
  Ubuntu's packaged `rclone v1.60.1-DEV` lacks
  `--vfs-cache-min-free-space`, so the default spec no longer emits that flag
  unconditionally.
- Active rclone mount sessions now suppress both the explicit `cloud_sync`
  payload and the legacy `nc_session_folder` back-compat sync path, so the old
  recursive WebDAV sync coordinator is not started after a successful rclone
  mount.

### Phase 1 v5 Live k3d Validation Delta

Implemented on 2026-06-10 while exercising the default rclone-mount path through
Cockpit against the local k3d/Tilt deployment:

- Workspace NetworkPolicy now allows workspace pods to reach bundled main-cloud
  backends used by local deployments, including Nextcloud and OpenCloud service
  pods. This keeps the rclone/WebDAV mount path usable without opening broad
  in-cluster egress.
- Session preparation now treats a recent `metadata.agent_pod` provisioning
  marker as in-flight state. A create/prepare race no longer starts a duplicate
  session agent pod while the first pod is still binding.
- Cockpit now marks a persistent session ready when `/connection` reports a
  ready agent, even if the control WebSocket `session.state` frame is not the
  first readiness signal. This clears the startup card and enables the composer
  on the normal create-session path.
- Agent REST input now starts the persistent loop when no legacy control
  WebSocket has started it yet. UI messages sent through
  `/api/persistent/threads/{id}/input` are no longer accepted and left queued.
- The session model picker no longer turns an expert config's YAML model into a
  user override. Backend system defaults can therefore inject the deployment's
  configured local chat, auxiliary, and embedding models for new sessions.
- Cockpit retains the `approval_id` from `permission.request` events and
  prefers the durable REST approval endpoint
  `/api/persistent/threads/{thread_id}/approve/{approval_id}`. The older
  control-WebSocket approval method remains as a fallback for older events.

Live smoke result: session `71c47960-c20a-4455-8bcf-f72e2c9410e7` on local k3d
created exactly one session agent pod and one workspace pod. The workspace had
`/home/agent-host/workspace/cloud -> /cloud/home`, and `/cloud/home` was mounted
as `fuse.rclone`. A supervised shell turn approved through the durable REST
approval path returned `/home/agent-host/workspace` and confirmed the rclone
mount in the command output. Chat and embedding calls used the configured local
model endpoint.

### Implemented Provider Contract

- Added `CloudMountSubject`, `RcloneMountSpec`, and `SupportsRcloneMount` to the
  main-cloud abstraction.
- The agent-side mount manager consumes only the generic `RcloneMountSpec`
  payload. Provider-specific URL and auth decisions stay inside the backend.
- Nextcloud implements `SupportsRcloneMount` with rclone's `webdav` backend.
- Nextcloud project/session folders use the configured agent-service WebDAV
  credentials.
- Nextcloud default user-home mounts require explicit user-home credentials.
  The v1 supported deployment knobs are:
  - `NEXTCLOUD_RCLONE_USER_HOME_USERNAME`
  - `NEXTCLOUD_RCLONE_USER_HOME_PASSWORD`
  - `NEXTCLOUD_RCLONE_USER_HOME_PASSWORD_<SANITIZED_USERNAME>`
- Helm exposes `cloud.nextcloudRcloneUserHomeUsername`; the password is supplied
  as optional secret key `NEXTCLOUD_RCLONE_USER_HOME_PASSWORD`.
- If a default user-home mount lacks safe explicit credentials, the session uses
  the regular session-folder fallback instead of pairing the user's home URL with
  the agent-service password.

### Implemented Agent Runtime

- Added `src/services/cloud_mount/RcloneMountManager`.
- The mount manager writes a per-session rclone config under
  `~/.cache/srw/rclone/<thread_id>/...` in the remote workspace runtime.
- VFS cache lives under the same runtime-only state directory, not under
  `/workspace`.
- Mount targets are created under `/cloud/<workspace_name>`.
- `/workspace/cloud` is a symlink to `/cloud/home` for a single mount.
- For multiple mounts, `/workspace/cloud/` is a directory of symlinks to the
  individual `/cloud/<workspace_name>` mount targets.
- Startup unmounts stale mountpoints at the target before starting the new
  session-owned rclone process.
- rclone RC is enabled on localhost with per-session random credentials.
- `.cloudignore` is fetched from the remote cloud root before mount startup,
  compiled into an rclone exclude file, and passed to `rclone mount` with
  `--exclude-from`.
- Deployment/session default ignore patterns can be supplied in the mount
  payload or `SRW_CLOUD_MOUNT_DEFAULT_IGNORES`.
- Read-only cloud mounts pass `--read-only` through to rclone.
- The mount manager can report cache usage, mountpoint state, and rclone
  `core/stats` / `vfs/stats` without exposing RC credentials.
- Session shutdown asks rclone to quit, then attempts `fusermount3 -u`, lazy
  unmount, and finally kills the session-owned rclone process if needed.
- The VM image installs `rclone` and `fuse3`.
- The workspace container image installs `rclone` and `fuse3`.

### Implemented Session Integration

- Persistent session setup starts the cloud mount before shell/tools are
  initialized.
- When a cloud mount is active, the legacy initial `pull_all()` is skipped.
- When a cloud mount is active, the legacy `nc_session_folder` compatibility
  path is also skipped.
- `cloud_mount.ready` and `cloud_mount.error` events are broadcast to the
  session stream.
- Tool context receives `cloud_mount.active` so shell guardrails can be scoped
  to rclone-mounted sessions.
- Active cloud-mount sessions receive the `srw_cloud_status` tool, which
  exposes mount/cache/RC status on demand.

### Implemented Guardrails

- Added a lightweight shell preflight guard for obvious broad cloud scans.
- The guard is active only when the session has an active cloud mount.
- It currently covers common accidental hydration commands such as:
  - `grep -R` over `/cloud` or `/workspace/cloud`;
  - `rg` / `ag` pointed at a cloud mount;
  - `du`, `tar`, `zip`, `unzip`;
  - recursive `cp`;
  - `rsync` / `scp`;
  - `find -exec`;
  - complex cloud-touching pipelines that include likely content readers.
- Default behavior is block with an actionable message. `cloud_scan_guard=warn`
  or `SRW_CLOUD_SCAN_GUARD=warn` turns it into an advisory warning.
- The per-session hard cache guard now checks rclone VFS cache usage before
  cloud-touching shell commands and before cloud-targeted `read_file`,
  `write_file`, and `edit_file` operations. If the configured hard limit is
  reached, the operation is blocked with cache usage and limit details.
- `search_files` refuses workspace-root or `cloud/...` searches while a cloud
  mount is active, because that tool can otherwise recurse through
  `/workspace/cloud` without going through shell parsing.

This is not the full hydration guard from Section 8. It does not yet track
rclone transfer deltas during a running tool call, pause process groups, enforce
per-command byte budgets, or create Cockpit approval requests.

### Implemented Tests and Verification

Added focused coverage for:

- rclone driver keeping the session-folder fallback provisioned;
- `cloud_mount` payload generation for supported thread mounts;
- fallback to session folder when a requested mount cannot be represented;
- agent-side rclone mount script generation and workspace symlink installation,
  including remote backend workspace-root resolution;
- stale mountpoint cleanup before startup;
- runtime rclone cache flag gating for older rclone builds;
- active cloud mounts skipping legacy `nc_session_folder` sync fallback;
- `.cloudignore` / default ignore filter script generation;
- hard cache limit parsing and block messages;
- `srw_cloud_status` output;
- shell cloud-scan guard parsing and block/warn behavior;
- workspace `search_files` and `read_file` cloud guard behavior.

Latest local verification run:

```text
ruff format --check <touched Python files>
ruff check <touched Python files>
npm test -- persistent-chat.service.spec.ts model-group.component.spec.ts sessions-page.component.spec.ts
pytest tests/test_session_provisioning_state.py \
  tests/test_provision_or_assign_lifecycle.py::test_fresh_pod_path_waits_when_agent_pod_marker_in_flight \
  tests/test_sessions_router_prepare.py::test_do_prepare_waits_when_agent_pod_marker_in_flight \
  tests/test_thread_events_phase2.py::TestAgentRestInputEndpointsNoSession::test_api_input_starts_loop_without_websocket \
  tests/test_persistent_app.py::TestHandlePersistentWebsocketReadiness \
  -q --tb=short
helm lint helm/ -f helm/ci/test-values.yaml
helm lint helm/ -f helm/ci/customer-external-values.yaml
git diff --check
```

Result: 115 focused Cockpit tests passed, 10 focused backend regressions passed,
Ruff passed, both Helm lint runs passed, and `git diff --check` passed. The
earlier full `tests/test_container_provisioner.py` target timed out in the local
tool harness after printing progress dots, so it was not counted as a full-file
pass.

Earlier focused verification for the rclone mount path passed:

```text
ruff check
ruff format --check
focused pytest: 49 passed
helm lint helm/ -f helm/ci/test-values.yaml
helm lint helm/ -f helm/ci/customer-external-values.yaml
git diff --check
```

k3d smoke result: a local session started with
`CLOUD_WORKSPACE_DRIVER=rclone_mount`, the workspace pod mounted `/cloud/home`
with rclone, `/home/agent-host/workspace/cloud` linked to `/cloud/home`, and
`srw_cloud_status` was loaded for the session.

### Remaining Gaps

- Hard cache enforcement is preflight-only for cloud-touching tool calls; it is
  not yet a live disk monitor that can interrupt an already-running process.
- The full hydration-budget approval flow is not implemented.
- `.cloudignore` is implemented for rclone mounts, but not for the legacy sync
  path from Phase 0.
- Background indexing and `srw-cloud-search` are not implemented.
- OpenCloud user-scoped rclone mounts still need the token-helper strategy from
  Section 11.
- Some clusters may reject `/dev/fuse`/`SYS_ADMIN` workspace pods; those
  deployments must use the explicit fallback flags above until their runtime
  profile supports FUSE.

## 15. Rollout Plan

### Phase 0 - Safety bridge

- Add `.cloudignore` support to the existing sync path.
- Add hard startup pull caps so a large default home cannot brick a session.
- Keep sessions alive in degraded mode when caps are hit.

This phase is optional if the rclone prototype lands first, but it is the
lowest-risk mitigation for existing deployments.

### Phase 1 - Nextcloud rclone prototype

- DONE: Target VM-backed workspaces first.
- DONE: Add `rclone` and `fusermount3` to the VM workspace image.
- DONE: Add a workspace-runtime mount manager.
- DONE: Add `SupportsRcloneMount` for Nextcloud.
- DONE: Mount Nextcloud surfaces under `/cloud/<workspace_name>`.
- DONE: Disable initial `pull_all()` for rclone-mounted sessions.
- PARTIAL: Enable rclone RC. Mount-local RC is running and `srw_cloud_status`
  surfaces status; live transfer-delta telemetry for hydration approvals is
  still pending.
- DONE: Emit mount lifecycle/status events.
- DONE: Keep current sync as fallback behind a feature flag.

Acceptance:

- session starts quickly with a large Nextcloud home;
- `ls /cloud/home` works;
- `cat /cloud/home/small.txt` materializes only that file;
- edits to a file under `/cloud/home` write back to Nextcloud;
- rclone cache stays outside snapshotted workspace paths;
- rclone RC reports transfer/cache stats;
- session shutdown unmounts cleanly.

### Phase 2 - Multiple mounts and project sessions

- PARTIAL: Support multiple `thread_mounts` entries with v1 all-or-fallback
  semantics.
- DONE: Mount non-default projects under `/cloud/<project-slug>`.
- DONE: Preserve read-only/read-write behavior through rclone `--read-only`.
- DONE: Add `.cloudignore`/rclone filter compilation for rclone mounts.
- PARTIAL: Add cache cleanup and stale mount sweeper. Startup handles stale
  mountpoints; active cache hard-limit enforcement is preflight-only and cache
  directory cleanup remains conservative.

### Phase 3 - Hydration guard

- Track rclone transfer counters per shell command.
- Add per-command and per-turn hydration budgets.
- Add `cloud_scan` approval requests in the cockpit, following the sudo-gate UX
  pattern.
- DONE: Add advisory/blocking warnings for obvious recursive scans.
- PARTIAL: Apply the guard before broad workspace tools such as `search_files`,
  not only shell commands. `search_files` is covered; other future
  cloud-capable tools still need explicit declarations.
- Ensure `cloud_scan` can force review even in autonomous mode.

### Phase 4 - Search/index path

- Add `srw-cloud-search` or a typed search tool.
- Start background indexing with `.cloudignore` and resource budgets.
- Teach prompts to use search for broad discovery and filesystem reads for
  selected files.

### Phase 5 - Default driver hardening

- DONE: Make `rclone_mount` the default for supported runtimes/providers.
- Keep `sync` as compatibility fallback for providers/runtimes without FUSE.
- Retire eager clone/pull for main-cloud workspace surfaces where rclone is
  supported.

## 16. Open Questions

No blocking design questions remain for the v1 prototype. Provider-specific
details can still be refined during implementation, but the intended defaults
are now fixed in this document.

## 17. Decision Summary

Use rclone mount as the next main-cloud workspace data plane.

Do not clone the whole cloud home at session startup. Keep broad cloud scope, but
hydrate lazily. Add `.cloudignore`, bounded VFS cache, and hydration guardrails
so a normal filesystem remains ergonomic without making broad recursive scans
unbounded.
