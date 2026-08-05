# Session workspace wiped by the agent's `rm -rf` + clone on every attach

**Status: OPEN.** Diagnosed 2026-08-04 on the dev cluster. The durability half
(PVC-backed session workspaces) shipped in `52c1ba80` and is live; **this bug is
not fixed and now empties a durable volume instead of an ephemeral one.**

## Symptom

A persistent session loses every uploaded and agent-created file across an idle
cycle. The user sees the agent report `Not found` for paths it wrote itself
minutes or days earlier, then silently rebuild them from conversation memory.

Field case — thread `1930dec9-181d-4fd5-a030-90b3d0b363d6` (`backend: sandbox`,
12 turns over 3 days). It lost its work **twice**: once between 2026-08-01 and
2026-08-04, and again 50 minutes later the same morning.

## Root cause

`WorkspaceManager.initialize()` (`src/core/workspace.py:496-508`) runs, whenever
`git_versioning` and `git_remote_url` are both set:

```python
self._backend.shell_run(
    f"rm -rf {self._backend.root}/* {self._backend.root}/.[!.]* 2>/dev/null || true",
    ...
)
self._initialize_git()   # → git clone from Gitea
```

Sessions reach this unconditionally: `src/api/persistent_session.py:552` calls
`self.workspace_manager.initialize()` with no content probe and no resume gate.
So on **every** agent attach the workspace is emptied and re-cloned from the
thread's Gitea repo — and that repo only ever contains the scaffold, because the
one thing that would populate it (the idle-archive commit,
`src/api/persistent_app.py:6886`) runs *after* the wipe has already emptied the
tree. The net records nothing, forever.

The `rm -rf`'s own comment cites "static container pool reuse" — a Docker-Compose
era concern. On Kubernetes, where the pod is fresh and the orchestrator has just
restored a snapshot into it, it is purely destructive.

**Jobs do not have this bug.** `src/agent.py:2326-2506` guards `initialize()`
with a content probe via the real backend, a pod-handoff clone path and an
explicit resume-existing branch. Its comments name this exact failure — *"the
local-path gates below would miss it and clone/initialize() would…"*, *"letting
content-bearing git-less workspaces fall through to `initialize()`'s `rm -rf`"* —
and were written for PVC reattach on job crash-recovery. **The fix is to port
that guard to the session path, not to invent one.**

## Evidence

Live pod `ws-thread-1930dec9-181`, reflog — a single clone, no other history:

```
1f2aa7a HEAD@{2026-08-04 09:26:29 +0000}: clone: from http://srw-gitea:3000/srw/thread-1930dec9.git
```

Orchestrator log for the same restore, finishing **1.3 s after** that clone:

```
09:26:30.268 WARNING  Snapshot extraction had errors for thread 1930dec9… (rc=2)
09:26:30.274 INFO     Workspace restored from S3 for thread 1930dec9… (ssh_host=10.42.2.84)
```

The 09:07 snapshot (99 MB) **did** contain the user's files —
`home/agent-host/workspace/Bewerbungspaket_Dylan_Hall/…`, all five of them — and
`home/` occupies the first ~2 % of the tar (members 93-185 of 8742), so it was
already on disk when the clone landed and wiped it. The post-restore working
tree matched Gitea `HEAD` exactly: scaffold only, every file stamped `09:26`.

Two things kept it invisible:

- `_extract_snapshot()` returned `None` on every failure path and
  `restore_thread_workspace()` never checked it, stamping `status: ready` +
  `restored_at` regardless. **Fixed in `52c1ba80`** — it now returns `bool` and
  both callers record a failure state.
- Every pod restore exits `rc=2` anyway, because `EXTRACT_REMOTE_CMD` untars
  `/usr/local` as `agent-host`. A home-scoped `EXTRACT_HOME_REMOTE_CMD` already
  exists (`ssh_helpers.py:88`) and is wired only into `ide_session.py:999`.
  **Still open** — the rc=2 noise masks genuine extraction failures.

## Why the PVC work does not fix this

Shipped in `52c1ba80`: sessions get `pvc-ws-thread-<tid>` + `pvc-agent-s-<tid>`,
volumes survive idle reaps, reclaim happens only on thread hard-delete, and
restore skips the S3 extract on a genuine reattach. All of that is necessary and
none of it is sufficient — the agent still `rm -rf`s the reattached volume on the
next attach. **Durable storage plus an unconditional wipe is still data loss.**

Conversely, fixing the wipe alone would have prevented the field case: the
snapshot content had already landed and only the clone destroyed it.

## Fix

Port the job-path guard: probe the real backend for existing content before
initializing, and skip the `rm -rf` + clone when the workspace is already
populated. Note `git clone` requires an empty target, so the guard must skip
init entirely rather than clear-then-clone — or clone to a temp dir and merge.

`.workspace-initialized` is **not** a usable marker: it lives in
`docker/workspace-entrypoint.sh:16` and gates only the image's skel copy.

## Residual gaps (not addressed by either change)

- **Node loss.** `longhorn-ephemeral` is single-replica with Delete reclaim, so
  `WORKSPACE_REATTACH_FRESH_FALLBACK` discards the wedged PVC for an empty one.
  Jobs tolerate this by re-cloning Gitea and resuming a checkpoint; sessions have
  no equivalent, and their Gitea repo holds only the scaffold.
- **S3 snapshot is a single overwritten key** (`threads/<id>/env.tar.zst`, no
  versioning), so only the most recent suspend is ever recoverable. In the field
  case the 09:07 snapshot holding the user's files was overwritten by the 13:29
  suspend the same day.
- **Uploads have no durable copy at `sandbox` tier** — `thread_uploads.py` SFTPs
  them into the pod. Only the `virtual` tier writes to the object store.
- **Credential persistence.** `docs/features/scoped_git_push.md` relied on the
  workspace being `emptyDir` so the Gitea PAT store dies at teardown. On a PVC it
  survives for the life of the thread. Wipe on detach / reattach and rotate.
