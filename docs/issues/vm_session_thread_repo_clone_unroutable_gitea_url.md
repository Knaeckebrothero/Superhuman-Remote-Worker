---
tags:
  - issue
  - sessions
  - vm
  - git
  - topology
---

# VM sessions can't clone their thread repo — in-cluster Gitea URL is unroutable from the tailnet

**Status:** Filed 2026-08-01 from the VM-session re-gate (dev, image `sha-99c9aba`,
thread `b35346cf-b539-45b3-a857-9f0209c22ddc`). Reproduces deterministically on
every VM-backed session; not a regression from the attach fixes — the 07-28 gate
simply didn't check this.

**One line:** The agent seeds a session workspace by cloning
`http://srw:<token>@srw-gitea:3000/srw/thread-<id>.git`, but a VM workspace
executes that clone **inside the VM** (remote backend), where the cluster-internal
DNS name `srw-gitea` does not resolve. GitManager logs a WARNING and falls back to
local `git init`, so the session *appears* healthy while thread-repo versioning is
silently dead.

## Evidence (agent pod `srw-agent-j-87fc4e66`, 2026-07-31T22:54 UTC)

```
Connected to workspace 100.64.2.105:22
git clone failed for http://srw:***@srw-gitea:3000/srw/thread-b35346cf.git: Exit code: 128
fatal: unable to access 'http://srw-gitea:3000/srw/thread-b35346cf.git/': Could not resolve host: srw-gitea
ShellManager initialized ... Session attached (51 tools)
```

## Consequences

- Workspace starts empty/unseeded (no thread-repo contents on resume).
- Session git versioning (auto-commit/push each turn) runs against a local-only
  repo — Cockpit's Git/history UI stays empty; nothing survives the VM.
- On idle teardown + re-provision this compounds with snapshot loss
  (`srw_vm_session_suspend_needs_tailnet` / P0-1) — the VM tier currently has
  neither git push nor snapshots persisting work.

## Root cause class

Same as `workspace_upgrade_drops_cloud_mount.md` (RESOLVED): a URL chosen for
in-cluster consumers handed to a workspace that lives outside the cluster. That
fix made the OpenCloud WebDAV URL **topology-aware** (public URL when the
workspace is a VM); the Gitea remote needs the identical treatment — pick the
public/routable Gitea URL (or a tailnet-reachable service) when
`workspace.backend` is a VM tier, at every site that renders the thread-repo
remote (seed clone + `git_remote_url` in `metadata.workspace_container`).

## Related

- `docs/issues/gitmanager_local_git_fallback.md` — the fallback that masks this.
- `docs/done/workspace_upgrade_drops_cloud_mount.md` — the topology-aware
  URL pattern to copy.
- `docs/done/session_vm_backend_never_attaches.md` — re-gate that surfaced it.
