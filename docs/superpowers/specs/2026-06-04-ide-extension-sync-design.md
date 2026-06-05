# Per-User Extension & Profile Sync for code-server — Design

**Status:** designed, not yet implemented.
**Date:** 2026-06-04
**Builds on:** the per-user IDE settings sync (`orchestrator/services/ide_settings.py`, `code_server_settings_sweeper` in `orchestrator/main.py`, memory `code-server-settings-persistence`) — this extends that subsystem; it does not replace it. Also reuses the snapshot transport in `orchestrator/services/snapshot_service.py`.

> **TL;DR** — Today we sync the small `User/` config files (`settings.json`, `keybindings.json`, `snippets/`) per user, but **not extensions**. That makes theme persistence half a feature: a non-built-in theme such as **Monokai Pro** is provided by an *extension* that doesn't exist in a fresh workspace, so the stored `"workbench.colorTheme": "Monokai Pro"` resolves to nothing. This design adds **per-user extension + profile sync** with a **hybrid transport** dictated by the network reality: a workspace pod **can reach Open VSX directly** (`open-vsx.org → 200`) but **cannot reach MinIO** (internal service and `minio-s3.h4ll.app` ingress both `000`, by network-tier design). So binaries are **reinstalled in-pod at boot from Open VSX by `id@version`**, while the small things Open VSX can't provide — the **`globalStorage` license/activation state** (e.g. Monokai Pro's key) and the **rare non-Open-VSX extension's bytes** — travel the **orchestrator→MinIO→SSH** path the snapshot system already uses. A small **sentinel handshake** lets the entrypoint briefly block for the license bundle so paid themes are active on first paint, with **theme-providing extensions installed first**.

---

## 1. Motivation & goals

The IDE settings feature persists `users.settings['ide']['files']` across workspaces and seeds them on provision. It deliberately excluded extensions ("heavier; Open VSX + install-on-start — deferred"). But every theme beyond the built-ins (Default Dark/Light Modern, Monokai, Solarized, Abyss, …) ships as an **extension**, installed under `--extensions-dir /var/lib/code-server/extensions` — a tree we neither capture nor seed. A fresh workspace ships **zero** extensions (verified: `extensions/` contains only `extensions.json`). So a user who selects a marketplace theme loses it in every new workspace even though the setting persists.

**Goals**

1. A user's installed extensions follow them into every new workspace (container, VM, restored session) — full IDE-setup portability, not just themes.
2. **Paid/licensed themes work without re-activation.** Monokai Pro renders *and* is unlocked in a fresh workspace, with no license re-entry and no nag.
3. Reuse what exists: the `users.settings['ide']` store, the seed-ConfigMap + entrypoint path, the orchestrator pull-sweep, and the snapshot S3 transport. Don't invent a second object-storage subsystem or a workspace→orchestrator push path.
4. Preserve the network isolation: workspaces never get MinIO credentials or cluster reachability.

**Non-goals (explicitly deferred)**

- **A settings/extension-management UI.** Users manage extensions natively in code-server, exactly as with the file sync.
- **Cross-user / team extension sharing.** Per-user only.
- **Pinning the orchestrator's own Open VSX mirror.** We rely on public `open-vsx.org`; running an internal mirror is a separate ops concern.
- **Merging two divergent `globalStorage` states.** A shared SQLite (`state.vscdb`) cannot be merged; we take newest-bundle-wins (§4).

---

## 2. Background: what exists and the network facts that shape this

**Storage + seed today.** `IdeSettingsStore` (`orchestrator/services/ide_settings.py`) read-modify-writes the whole `ide` subtree of `users.settings` (because `PostgresDB.update_user_settings` is a shallow top-level `||` merge). `code_server_settings_sweeper` (`orchestrator/main.py:728`, `IDE_SETTINGS_SYNC_INTERVAL_S` default 600) enumerates active workspaces (`PostgresDB.list_active_ide_workspaces`) and pulls config over SSH; success is silent, only `rc!=0` logs. Containers seed via a ConfigMap (`container_provisioner._create_seed_configmap`) mounted at `/mnt/code-server-config`, run by `docker/workspace-entrypoint.sh` (section "2b") **before** code-server starts; VMs/restored sessions seed via orchestrator SSH on-ready (`nats_bridge._on_daemon_register`, `ide_session._restore_vm_session`). SSH uses `ssh_helpers.build_agent_ssh_cmd` (`agent-host@host`, key from `SSH_KEY_PATH`), container sshd on **30022** (`DEFAULT_WS_SSH_PORT`, `ide_settings.py:49`), code-server on **38080**.

> **Prerequisite:** seeding currently requires the orchestrator ServiceAccount to create ConfigMaps. That RBAC rule was missing and is added in `helm/templates/orchestrator/rbac.yaml` (`configmaps: [create,delete,get,list,patch,update]`). Extension sync depends on it.

**Snapshot transport (reused).** `snapshot_service.py` holds a boto3 S3 client (`S3_ENDPOINT`, `S3_BUCKET` default `srw-snapshots`, line 83) and captures via `ssh agent-host@host 'tar -cf - --exclude=… /home/agent-host/'` → zstd → temp file → `put_object` (~line 248-395), restoring in reverse (~line 462-560). It **excludes `/var/lib/code-server`** — exactly the tree we need. So extension sync is a narrower, parallel snapshot of the *complementary* directories.

**Network reality (measured 2026-06-04 from a live `internet-only` workspace pod).**

| From workspace pod → | Result |
|---|---|
| `srw-minio:9000`, `minio.minio:9000` (internal) | `000` (blocked) |
| `https://minio-s3.h4ll.app` (ingress) | `000` (resolves to internal LB; blocked + would violate home-IP exposure policy to expose publicly) |
| `https://open-vsx.org` | **`200`** |

The pod can reach the public marketplace but not object storage. This is the pivot for the whole design: **binaries pull from Open VSX in-pod; bytes-from-MinIO must be orchestrator-mediated.** Confirmed Monokai Pro is on Open VSX (`monokai/theme-monokai-pro-vscode`, v2.0.13).

---

## 3. Data model

`users.settings['ide']` keeps `['files']` unchanged. Add `['extensions']` — a small **manifest** (JSONB; bytes never go here):

```jsonc
"extensions": {
  "items": {
    "monokai.theme-monokai-pro-vscode": { "version": "2.0.13", "source": "openvsx" },
    "acme.private-linter":              { "version": "1.4.0",  "source": "bytes" }
  },
  "globalStorage": { "key": "ide-profiles/<user_id>/globalStorage.tar.zst",
                     "sig": "<sha256>", "captured_at": 1780557786.0 },
  "captured_at": 1780557786.0
}
```

- `source: "openvsx"` → reinstallable by id from Open VSX (the common case).
- `source: "bytes"` → not on Open VSX; bytes stored at `ide-profiles/<user_id>/ext/<id>/<version>.tar.zst`.
- `globalStorage` → one per-user bundle, newest-capture-wins.

**Object storage.** Reuse the snapshot S3 client; new prefix `ide-profiles/<user_id>/` in a bucket from `IDE_PROFILE_BUCKET` (default = `S3_BUCKET`, i.e. `srw-snapshots`). No new credentials or client.

---

## 4. Capture (orchestrator pull — existing sweep + graceful suspend)

Extend the per-workspace pull (today files-only) to also reconcile extensions. Per active workspace:

1. **List:** SSH `code-server --list-extensions --show-versions` (fallback: read `extensions/extensions.json`).
2. **Cheap change-signature:** SSH `find /var/lib/code-server/extensions /var/lib/code-server/User/globalStorage -printf '%p %s %T@\n' | sha256sum`. If unchanged since `captured_at`/`sig`, skip — byte-copy is expensive and must not run every cycle.
3. **Classify** each id against the Open VSX API (`GET /api/<ns>/<name>/<version>`), orchestrator-side, with an in-memory TTL cache → `openvsx` | `bytes`.
4. **Upload bytes only when changed:**
   - `bytes` extensions: SSH-tar that extension's folder → zstd → `ext/<id>/<version>.tar.zst` (dedup: skip if the key already exists).
   - `globalStorage`: SSH-tar `User/globalStorage/` → zstd → `globalStorage.tar.zst` (overwrite; newest wins).
5. **Write manifest** into `users.settings['ide']['extensions']` via the existing read-modify-write of the whole `ide` subtree.

**Merge across a user's workspaces.** Extension `items` = per-id **newest-version-wins union** (same spirit as per-file mtime for files), so extensions installed in workspace A and B both survive. `globalStorage` = **whole-bundle newest-capture-wins** (a shared `state.vscdb` SQLite cannot be merged). This asymmetry is intentional and documented.

Capture also fires opportunistically on graceful suspend (alongside the existing file pull in `workspace_suspension.py`), shrinking the loss window.

---

## 5. Seed (two channels, sequenced for first-paint correctness)

The manifest drives provisioning. Binaries and state arrive by different paths:

**(a) In-pod at boot — binaries (Open VSX).** The manifest rides in the existing seed ConfigMap (it's small JSON). `workspace-entrypoint.sh` gains an install step that, for each `source:openvsx` item, runs `code-server --install-extension <id>@<version> --extensions-dir /var/lib/code-server/extensions`. **Theme/icon-providing extensions (those referenced by `workbench.colorTheme` / `workbench.iconTheme` in the seeded `settings.json`) install first, synchronously**; the rest install in parallel/capped in the background. The pod reaches Open VSX directly — no orchestrator bottleneck, no large SSH transfer.

**(b) Orchestrator-mediated — state + rare bytes.** Because the pod can't reach MinIO, the orchestrator (already SSH-capable inward) downloads `globalStorage.tar.zst` (+ any `bytes` extension tarballs) from S3 and SSH-streams them into `/var/lib/code-server/`, then `touch`es a sentinel `/var/lib/code-server/.ide-seed-state-done`.

**Sequencing / handshake.** So a paid theme is active on first paint, the entrypoint, after the synchronous theme-extension install, **waits on the sentinel with a bounded cap (~30s)** before starting code-server. If the orchestrator stream is late, code-server still starts (degraded: theme renders but may nag until a reload). VMs and restored sessions reuse their existing orchestrator-side seed hooks (`nats_bridge`, `ide_session`) for channel (b); the wait is gated the same way.

**For containers, this adds one new orchestrator action** that doesn't exist today: a post-provision inward SSH step to stream the state bundle (today containers self-seed entirely from the ConfigMap). It mirrors the VM on-ready seed and is fire-and-forget except for the sentinel touch.

---

## 6. Edge cases & failure modes

- **Open VSX missing/yanked at seed time** for an `openvsx` item → `--install-extension` fails → log + continue; that extension (possibly the theme) is absent. Accepted (rare); user can reinstall. We do **not** silently fall back to bytes for `openvsx`-classified items (we didn't store their bytes).
- **Version not on Open VSX** → install nearest/latest available; note in logs. Acceptable for low-stakes prefs.
- **Large extension sets** → boot install latency; mitigated by theme-first + parallel/capped background install. The provisioning critical path only blocks on theme extensions + the ~30s state sentinel.
- **`globalStorage` token spillover** → the shared `state.vscdb` may carry other extensions' secrets/tokens. Accepted: it's the user's own data going into their own per-user store (decision locked during design).
- **Workspace unreachable / code-server down** → skip this cycle, retry next; never block teardown or provisioning (matches the file-sync contract; `pull_ide_config`-style "never raise out of the loop").
- **Stale enumeration** (pre-existing): the sweep already wastes cycles on dead workspaces; extension capture must inherit the same "skip on unreachable" and not amplify it (gate on the cheap signature before any tar).

---

## 7. Components & files

- `orchestrator/services/ide_settings.py` — new: extension list/classify, change-signature, manifest merge (per-id newest), seed-script extension-install emit, sentinel name. Keep functions small and unit-testable (mirrors existing `build_*`/`parse_*`/`reconcile_*` decomposition).
- New thin module (or section) for the **S3 profile store** — wraps the snapshot S3 client for `ide-profiles/<user_id>/...` put/get/exists; no second boto3 client.
- `orchestrator/main.py` — extend `code_server_settings_sweeper` to also reconcile extensions (same loop, gated by signature).
- `orchestrator/database/postgres.py` — no schema change (manifest lives in `users.settings` JSONB); reuse `get_user_settings`/`update_user_settings`.
- `orchestrator/services/container_provisioner.py` — manifest into the seed ConfigMap; add the post-provision inward state-stream + sentinel step.
- `docker/workspace-entrypoint.sh` — extension-install loop (theme-first), sentinel wait with bounded cap, before code-server start.
- `orchestrator/services/nats_bridge.py`, `ide_session.py`, `workspace_suspension.py` — extend the existing VM/restore/suspend hooks for channel (b) and opportunistic capture.
- `helm/templates/orchestrator/rbac.yaml` — already gained `configmaps`; confirm no new verbs needed (S3 is network, not k8s API).

---

## 8. Testing

**Unit (pytest, mocked SSH + S3):** extension list parsing; Open VSX classification (cached); change-signature skip logic; manifest merge (per-id newest-version union; globalStorage newest-bundle); seed-script generation (theme-first ordering, install loop); S3 profile-store put/get/exists/dedup; sentinel wait emit.

**Live (dev cluster, as the file-sync was verified):** in one session, install a free Open VSX extension **and** Monokai Pro + enter its license → confirm the manifest in `users.settings['ide']['extensions']` and `globalStorage.tar.zst` in S3 (+ a `bytes` tarball for a deliberately non-Open-VSX extension) → provision a **fresh** session → confirm (1) Open VSX reinstall populated `extensions/`, (2) globalStorage restored, (3) **Monokai Pro active with no nag** on first paint, (4) the non-Open-VSX extension restored from bytes. Run the orchestrator suite; CI (Py3.12) is the gate.

---

## 9. Out of scope (explicit)

Extension-management UI; team/cross-user sharing; internal Open VSX mirror; `globalStorage` merge across divergent workspaces; changing the network tiers or granting workspaces S3 access.
