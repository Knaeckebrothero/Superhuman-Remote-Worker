---
tags:
  - issue
  - lifecycle
  - workspace-snapshots
  - dev-only
related:
  - "[[agent_lifecycle_management]]"
  - "[[canvas_durable_presentation]]"
---

# Workspace snapshot/IDE SSH fails in local dev — `0444` key "too open" (root container only)

**Filed:** 2026-06-22, from a Tilt-log triage on the k3d dev stack.
**Prod is NOT affected** — same chart, only the container user differs. See
"Why prod is fine" below.

## Symptom

The orchestrator logs an `ERROR` burst every snapshot sweep, one per live
workspace/thread:

```
SSH tar failed for job <id>: ... Permissions 0444 for '/run/secrets/vm-ssh-key'
are too open. ... This private key will be ignored. ... bad permissions
   (snapshot_service.py:430)
```

Snapshots are marked `capture_failed`; nothing is captured. This is the
mechanism behind dev workspace pods not getting snapshotted before GC.

## Root cause

The VM SSH key is mounted root:root mode `0444`
(`helm/templates/orchestrator/deployment.yaml`, the `vm-ssh-key` volume,
`defaultMode: 0444`). OpenSSH's CLI enforces its private-key permission
check **only when the key file is owned by the same uid running ssh**
(`sshkey_perm_ok()`: *"if the key [is] owned by a different user, then we
don't care"*).

- **Dev** runs the orchestrator as **root** —
  `docker/Dockerfile.orchestrator.dev` drops the user so Tilt can sync
  `/app`. uid 0 == key owner → check fires → `0444` rejected → `ssh -i`
  ignores the key → auth fails.
- **Prod** runs as non-root **`srw`** (`docker/Dockerfile.orchestrator:72`,
  `USER srw`). uid ≠ key owner → check skipped → the world-readable `0444`
  key is used fine.

Verified empirically in one dev orchestrator pod against a real workspace
pod (sshd on `:30022`): as **root** → `Permissions 0444 ... too open ...
ignored`; as **nobody** (uid 65534, the prod uid relationship) → key
accepted, no warning.

**Affected CLI paths:** `services/ssh_helpers.py` (`build_agent_ssh_cmd`,
used by snapshot capture + restore + `ide_settings`) and
`services/ide_session.py` (inline ssh builders ~L615 / L884).
**Unaffected:** the paramiko/SFTP path in `services/thread_uploads.py` — it
doesn't run OpenSSH's perms check, which is exactly why `0444` was chosen.

## Why prod is fine

Same chart, same `0444` in both environments — only the container user
differs, and non-root prod sidesteps the OpenSSH check entirely. No action
needed in prod.

## Non-fixes (footguns)

- `ssh -o StrictModes=no` — **invalid ssh *client* option** (`StrictModes`
  is an `sshd` directive; `ssh -G -o StrictModes=no` → exit 255 "Bad
  configuration option"). Adding it breaks the command.
- Lowering chart `defaultMode` to `0400` — fixes dev (root owns the file)
  but **breaks prod**: non-root `srw` cannot read a root-owned `0400` file
  (including the paramiko path).

## Fix (deferred — only if dev snapshots are worth it)

Env-agnostic: at container start, stage-copy the key to a `0600` file owned
by the runtime user (initContainer or entrypoint) and point `key_path` at
the copy. Works as root *or* `srw`. Alternative: align the dev image to
non-root (conflicts with Tilt's `/app` sync).

## Status

- [x] Diagnosed + verified empirically (2026-06-22)
- [x] Misleading chart comment corrected (`deployment.yaml`, `vm-ssh-key` volume)
- [x] **FIXED 2026-08-05** (`52c1ba80`) — `resolve_ssh_key_path` now stages a
      readable-but-not-private key to an atomic runtime-owned `0600` copy and
      returns that path, which is exactly the "Fix" shape proposed below. Works
      as root *or* `srw`; the projected Secret stays read-only at `0444`, so the
      chart is unchanged and paramiko is unaffected. Covered by
      `tests/test_ssh_key_path.py`. The k3d orchestrator now logs zero
      `0444`/"too open" errors.

**Follow-up owed:** the suspend → S3 → restore cycle should now actually
complete on k3d. That was the one acceptance criterion
`docs/features/canvas_durable_presentation.md` §12 could not verify locally
(criterion 13, worked around with pod deletion + re-provision). Running a real
suspend/restore there would close it for free.
- Re-confirmed still live on k3d 2026-06-30 while verifying
  `ide_settings_sweeper_probes_stale_workspace_endpoints.md`. The snapshot and
  `ide_settings` sweeps share a *no-eviction* root cause — both keep dialing
  ended/terminal workspaces that are never evicted — so this ERROR burst is
  amplified by that leak. Evicting dead records (the ide_settings fix) shrinks
  the burst even while this dev key-perms fix stays deferred.
- Re-confirmed again 2026-07-28 from a different angle, which adds one concrete
  cost to the "is dev snapshotting worth it?" question. While verifying
  `docs/features/canvas_durable_presentation.md` on k3d, an explicit
  `POST /api/agents/threads/{id}/suspend` returned 200 but did nothing: the tar
  failed on this exact `0444` warning, `suspend_thread_workspace` returned
  `False`, and the fallback kept the workspace pod alive
  (`main.py:_suspend_thread_resources`). So on dev the bug does not merely lose
  snapshots — **it makes suspend a silent no-op**, which in turn means the
  suspend → S3 → restore path cannot be exercised locally at all. That blocked
  one acceptance criterion there (canvas doc §12, criterion 13), which was
  closed by substituting pod deletion + re-provision.

  If a future session picks this up, the stage-copy fix below is still the
  right shape, and completing a real suspend → restore cycle on k3d would
  double as the missing canvas verification. Priority is unchanged: **prod is
  not affected**, so this stays a local-dev quality-of-life fix, not a
  data-loss bug.
